"""Live run status and predictions (PRD 7.1, F17, F18).

One RunStatus object updates on every event; it renders as a rich panel, is written to
status.json every second, and the review screen reads that file. Predictions only report.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from rich.panel import Panel
from rich.text import Text

EARLY_PAGES = 15


def _fmt_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _mb(b: float) -> str:
    return f"{b / 1_000_000:.1f} MB"


@dataclass
class RunStatus:
    max_links: int
    max_runtime_s: float
    set_rpm: int
    target_candidates: int
    run_id: int | None = None
    state: str = "starting"   # starting | running | slowed | paused (network) | done | stopped (block)
    reason: str | None = None
    doing: str = "starting"
    started_at: float = field(default_factory=time.time)
    pages_done: int = 0
    requests: int = 0
    retries: int = 0
    errors: int = 0
    skipped_recent: int = 0
    skipped_robots: int = 0
    not_modified: int = 0
    unchanged_hash: int = 0
    queue: int = 0
    queue_kinds: dict = field(default_factory=dict)
    current_rpm: float = 0.0
    effective_rate: float = 0.0
    median_latency: float | None = None
    bytes_total: int = 0
    bytes_by_type: dict = field(default_factory=dict)
    pages_by_type: dict = field(default_factory=dict)
    hits_by_type: dict = field(default_factory=dict)
    new_qualifying: int = 0
    best_discount: float | None = None
    estimate_note: str | None = None
    hot_finds: list = field(default_factory=list)  # newest first, 95%+ alerts this run
    # finish times of the last 30 pages: the rate is measured over this window (moving average).
    # Per-page gaps are useless with concurrency: 3 pages finishing together look like 1000 pages/min.
    _page_times: deque = field(default_factory=lambda: deque(maxlen=30))

    # ------------------------------------------------------------------ events
    def page_done(self, kind: str, nbytes: int, new_hits: int) -> None:
        self._page_times.append(time.time())
        self.pages_done += 1
        self.pages_by_type[kind] = self.pages_by_type.get(kind, 0) + 1
        self.bytes_by_type[kind] = self.bytes_by_type.get(kind, 0) + nbytes
        self.hits_by_type[kind] = self.hits_by_type.get(kind, 0) + new_hits

    def add_bytes(self, nbytes: int) -> None:
        self.bytes_total += nbytes

    # ------------------------------------------------------------------ derived
    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def hit_rate_per_100(self) -> float:
        return 100.0 * self.new_qualifying / self.pages_done if self.pages_done else 0.0

    @property
    def pages_per_minute(self) -> float | None:
        """Pages per minute over the last (up to) 30 pages, counted from the first of them."""
        t = self._page_times
        if len(t) < 3:
            return None
        span = time.time() - t[0]
        return 60.0 * (len(t) - 1) / span if span > 0 else None

    def _avg_size(self, kind: str) -> float | None:
        n = self.pages_by_type.get(kind)
        return self.bytes_by_type.get(kind, 0) / n if n else None

    def predictions(self) -> dict:
        remaining_pages = max(0, self.max_links - self.pages_done)
        remaining_time = max(0.0, self.max_runtime_s - self.elapsed)
        rate = self.pages_per_minute or self.effective_rate or self.set_rpm
        rate = max(rate, 0.1)
        # only pages that will actually be fetched: cap by what is queued
        will_fetch = min(remaining_pages, self.queue) if self.queue else remaining_pages
        t_pages = will_fetch / rate * 60
        time_left = min(t_pages, remaining_time)
        # data: weighted by what is queued, per page type
        overall_avg = (self.bytes_total / self.pages_done) if self.pages_done else None
        data_left = 0.0
        if self.queue_kinds and overall_avg:
            total_q = sum(self.queue_kinds.values()) or 1
            for kind, n in self.queue_kinds.items():
                share = n / total_q
                data_left += will_fetch * share * (self._avg_size(kind) or overall_avg)
        elif overall_avg:
            data_left = will_fetch * overall_avg
        hits_per_page = self.new_qualifying / self.pages_done if self.pages_done else 0.0
        finds = hits_per_page * will_fetch
        need = self.target_candidates - self.new_qualifying
        eta_target = None
        if need <= 0:
            likely_end = "target reached"
        elif hits_per_page > 0 and need / hits_per_page <= will_fetch and need / hits_per_page / rate * 60 <= remaining_time:
            eta_target = need / hits_per_page / rate * 60
            likely_end = "target reached"
            time_left = min(time_left, eta_target)
        elif t_pages >= remaining_time:
            likely_end = "time cap"
        elif self.queue and self.queue < remaining_pages:
            likely_end = "queue empty"
        else:
            likely_end = "page cap"
        estimating = self.pages_done < EARLY_PAGES
        widen = 0.5 if estimating else (0.25 if self.estimate_note else 0.0)

        def rng(v: float) -> list[float]:
            return [max(0.0, v * (1 - widen)), v * (1 + widen)] if widen else [v, v]

        return {
            "time_left_s": rng(time_left),
            "data_left_bytes": rng(data_left),
            "predicted_total_bytes": rng(self.bytes_total + data_left),
            "finds_more": rng(finds),
            "eta_target_s": eta_target,
            "likely_end": likely_end,
            "estimating": estimating,
            "note": self.estimate_note,
        }

    # ------------------------------------------------------------------ outputs
    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["elapsed_s"] = self.elapsed
        d["hit_rate_per_100"] = self.hit_rate_per_100
        d["hit_rate_by_type"] = {
            k: 100.0 * self.hits_by_type.get(k, 0) / n for k, n in self.pages_by_type.items() if n
        }
        d["avg_bytes_by_type"] = {k: self._avg_size(k) for k in self.pages_by_type}
        d["predictions"] = self.predictions()
        d["updated_at"] = time.time()
        return d

    def write(self, path: Path) -> None:
        data = json.dumps(self.to_dict(), indent=1, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".status", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        for _ in range(5):  # atomic replace (retry: Windows may hold the target briefly)
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05)
        os.unlink(tmp)

    def panel(self) -> Panel:
        p = self.predictions()
        state = self.state + (f" ({self.reason})" if self.reason and self.state not in ("running",) else "")
        med = f"{self.median_latency:.1f} s" if self.median_latency else "-"
        avg = (self.bytes_total / self.pages_done) if self.pages_done else 0
        tl = p["time_left_s"]
        eta = f"~{tl[0] / 60:.0f}-{tl[1] / 60:.0f} min" if tl[0] != tl[1] else f"~{tl[0] / 60:.0f} min"
        if p["estimating"]:
            eta += " (estimating)"
        tot = p["predicted_total_bytes"]
        best = f"best {self.best_discount:.0f}%" if self.best_discount is not None else "best -"
        lines = [
            f"PromoScoutBot  {state:<14} elapsed {_fmt_mmss(self.elapsed)} / {_fmt_mmss(self.max_runtime_s)}",
            f"Now:    {self.doing}   queue {self.queue}",
            f"Pages:  {self.pages_done} / {self.max_links}   rate {self.current_rpm:.0f} rpm (set {self.set_rpm})"
            f"   median {med}   retries {self.retries}   errors {self.errors}",
            f"Skipped: {self.skipped_recent} recently checked   {self.skipped_robots} robots   "
            f"{self.not_modified} not modified   {self.unchanged_hash} unchanged",
            f"Data:   {_mb(self.bytes_total)} so far   avg {avg / 1000:.0f} KB/page   predicted total ~{_mb(sum(tot) / 2)}",
            "        by type: " + ("   ".join(f"{k} {(self._avg_size(k) or 0) / 1000:.0f} KB"
                                           for k in sorted(self.pages_by_type)) or "-"),
            f"Found:  {self.new_qualifying} new ({best})   hit rate {self.hit_rate_per_100:.1f} per 100 pages"
            f"   predicted finds +{p['finds_more'][0]:.0f}" + (
                f"-{p['finds_more'][1]:.0f}" if p["finds_more"][0] != p["finds_more"][1] else "") + (
                f"   target in ~{p['eta_target_s'] / 60:.0f} min" if p["eta_target_s"] else ""),
            f"ETA:    {eta}   likely end: {p['likely_end']}" + (f"   [{p['note']}]" if p["note"] else ""),
        ]
        for h in self.hot_finds[:3]:
            lines.append(f"HOT:    {h['discount_pct']:.1f}% off  {(h.get('title') or h['sku'])[:50]}  {h.get('url') or ''}")
        return Panel(Text("\n".join(lines)), title="PromoScoutBot", expand=False)

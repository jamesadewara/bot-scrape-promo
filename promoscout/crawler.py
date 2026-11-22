"""Best-first crawl loop (PRD 7, 6, 2.2.1)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass

import structlog

from .extract import PageResult, extract
from .fetcher import Fetcher, FetchResult, Outcome
from .frontier import (TIER_NEW, TIER_OTHER, TIER_STALE_QUALIFYING, Entry, Frontier, focus_bonus,
                       link_priority, section_for)
from .ratelimit import RateLimiter
from .robots import Robots
from .settings import PRUNE_MIN_DISCOUNT, ROBOTS_URL, Settings
from .scoring import in_price_range, inflated_old_price
from .sitemaps import child_sitemap_rank, parse_sitemap
from .status import RunStatus
from .store import Store
from urllib.parse import urlsplit

from .urls import has_filter_params, hunt_match, is_asset, normalize, page_family, page_number, page_type

log = structlog.get_logger("promoscout")

RETRYABLE = {Outcome.PUSHBACK, Outcome.SERVER_ERROR, Outcome.NETWORK_ERROR, Outcome.TRUNCATED}
NETWORKISH = {Outcome.NETWORK_ERROR, Outcome.TRUNCATED}


class StopRun(Exception):
    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state, self.reason = state, reason


@dataclass
class RunSummary:
    run_id: int | None
    state: str
    reason: str
    pages: int
    requests: int
    new_qualifying: int


class Crawler:
    def __init__(self, settings: Settings, store: Store, fetcher: Fetcher, *, limiter: RateLimiter | None = None,
                 live: bool = False):
        self.s = settings
        self.store = store
        self.fetcher = fetcher
        self.limiter = limiter or RateLimiter(
            settings.max_rpm, (settings.jitter_ms_min, settings.jitter_ms_max), settings.slow_factor)
        self.live = live
        self.robots: Robots | None = None
        self.frontier = Frontier()
        self.status = RunStatus(settings.max_links_per_run, settings.max_runtime_minutes * 60,
                                settings.max_rpm, settings.target_candidates)
        self.run_id: int | None = None
        self.pages_reserved = 0
        self.inflight = 0
        self.consecutive_429 = 0
        self.consecutive_net = 0
        self.failed_pauses = 0
        self.net_ok = asyncio.Event()
        self.net_ok.set()
        self.probe_lock = asyncio.Lock()
        self.stop: StopRun | None = None
        self.prune_counts: dict[str, int] = {}
        self.sitemap_lastmod: dict[str, str] = {}
        self.sitemaps_read = 0
        self.child_sitemaps_queued = 0
        self.hint_pages: dict[str, int] = {}
        self.hint_page_total = 0
        self._deadline = 0.0
        self.seeded = False
        self.family_pages: dict[str, int] = {}
        self.family_bonus: dict[str, float] = {}
        self.family_suppressed: set[str] = set()
        self.family_best: dict[str, float] = {}
        self.rediscover = False
        self.type_bonus: dict[str, float] = {}
        self.hot_sections: dict[str, float] = {}

    # ================================================================== entry
    async def run(self, seed: str | None = None, use_sitemaps: bool = True, resume: bool = False) -> RunSummary:
        now = time.time()
        lock_until = self.store.lock_until()
        if lock_until > now:
            reason = f"locked after block signal until {time.ctime(lock_until)} ({self.store.get_kv('lock_reason')})"
            log.warning("run_refused", reason=reason)
            return RunSummary(None, "refused", reason, 0, 0, 0)
        if self.store.runs_today() >= self.s.max_runs_per_day:
            reason = f"max_runs_per_day ({self.s.max_runs_per_day}) reached"
            log.warning("run_refused", reason=reason)
            return RunSummary(None, "refused", reason, 0, 0, 0)

        self.store.close_stale_runs()  # runs killed mid-way (e.g. second Ctrl+C) no longer show "running"
        self.run_id = self.store.start_run()
        self.status.run_id = self.run_id
        self._deadline = time.monotonic() + self.s.max_runtime_minutes * 60
        ticker = asyncio.create_task(self._tick())
        state, reason = "done", "frontier empty"
        try:
            await self._load_robots()
            self._seed(seed, use_sitemaps, resume)
            self.seeded = True
            self.store.set_kv("checkpoint", self.frontier.to_json())  # queue is safe from the start
            workers = [asyncio.create_task(self._worker(i)) for i in range(self.s.max_concurrency)]
            await asyncio.gather(*workers)
            if self.stop:
                state, reason = self.stop.state, self.stop.reason
        except StopRun as e:
            state, reason = e.state, e.reason
        except (asyncio.CancelledError, KeyboardInterrupt):
            state, reason = "stopped (interrupted)", "stopped by operator; resume with --resume"
            raise
        finally:
            if state == "stopped (block)":
                self.store.lock(self.s.block_lock_hours, reason)
            self.status.state, self.status.reason = state, reason
            self.status.doing = "finished"
            ticker.cancel()
            # each step on its own, so one failure (or a second Ctrl+C) cannot skip the others
            try:
                self.store.end_run(self.run_id, state, reason, pages=self.status.pages_done,
                                   requests=self.status.requests, bytes=self.status.bytes_total,
                                   new_qualifying=self.status.new_qualifying,
                                   extra={"hit_rate_by_type": self.status.to_dict()["hit_rate_by_type"],
                                          "pages_by_type": self.status.pages_by_type,
                                          "hits_by_type": self.status.hits_by_type,
                                          "bytes_by_type": self.status.bytes_by_type})
            except Exception as exc:  # noqa: BLE001
                log.error("end_run_failed", error=str(exc))
            try:
                hidden = self.store.auto_hide(self.s.review_hide_after_hours, self.s.review_keep_min_discount,
                                              self.s.review_stale_hours)
                if hidden:
                    log.info("auto_hidden", count=hidden)
            except Exception as exc:  # noqa: BLE001
                log.error("auto_hide_failed", error=str(exc))
            # F11: save what is left to fetch. A run that never got past robots.txt/seeding must NOT
            # overwrite the queue saved by earlier runs.
            if self.seeded:
                try:
                    self.store.set_kv("checkpoint", self.frontier.to_json() if len(self.frontier) else "")
                except Exception as exc:  # noqa: BLE001
                    log.error("checkpoint_failed", error=str(exc))
            try:
                self.status.write(self.s.status_path)
            except OSError:
                pass
            log.info("run_end", run_id=self.run_id, state=state, reason=reason, pages=self.status.pages_done,
                     requests=self.status.requests, new_qualifying=self.status.new_qualifying)
        return RunSummary(self.run_id, state, reason, self.status.pages_done, self.status.requests,
                          self.status.new_qualifying)

    # ================================================================== setup
    async def _load_robots(self) -> None:
        """C8: re-fetch robots.txt at the start of every run. No robots, no crawl."""
        self.status.doing = "fetching robots.txt"
        await self.limiter.acquire()
        res = await self.fetcher.fetch(ROBOTS_URL)
        self._account(res, "robots", "allow", 1)
        if res.outcome == Outcome.BLOCK:
            raise StopRun("stopped (block)", f"robots.txt: {res.reason}")
        if res.outcome != Outcome.OK or not res.body:
            raise StopRun("stopped (error)", f"could not read robots.txt ({res.reason or res.status})")
        self.robots = Robots(res.body)
        self.store.set_kv("robots_txt", res.body)  # kept so `promoscout check` can audit every request
        log.info("robots_loaded", sitemaps=self.robots.sitemaps)

    def _seed(self, seed: str | None, use_sitemaps: bool, resume: bool) -> None:
        assert self.robots is not None
        self._load_history()
        cp = self.store.get_kv("checkpoint")
        if resume and cp:
            # F11: continue the interrupted run exactly (frontier, visited set, pruned sections)
            self.frontier = Frontier.from_json(cp)
            log.info("resumed", queued=len(self.frontier), visited=len(self.frontier.visited))
            return
        if cp:
            # 6.1 memory across runs: URLs discovered earlier but never fetched (page cap / time cap)
            # are carried into this run, so successive runs keep covering new ground.
            carried = 0
            for e in Frontier.from_json(cp)._entries.values():
                if self._enqueue(e.url, depth=e.depth, priority=e.priority, parent_section=e.section,
                                 reason=e.reason or "carried", parent=e.parent):
                    carried += 1
            log.info("carried_over", queued=carried)
        if seed:
            u = normalize(seed)
            if not u:
                raise StopRun("stopped (error)", f"seed is not a jumia.com.ng URL: {seed}")
            self._enqueue(u, depth=0, priority=400, parent_section=None, reason="seed")
        for path in self.s.promo_seed_paths:  # known campaign pages (research), robots + due checks apply
            u = normalize(path)
            if u:
                self._enqueue(u, depth=0, priority=350, parent_section=None, reason="promo_seed")
        if use_sitemaps:
            for sm in self.robots.sitemaps:  # C7
                self._enqueue(sm, depth=0, priority=300, parent_section=None, reason="sitemap")
            if not len(self.frontier):
                # Nothing queued and the sitemaps were read recently: read them again so the run
                # discovers pages it has never fetched, instead of sitting idle.
                self.rediscover = True
                for sm in self.robots.sitemaps:
                    self.frontier.visited.pop(sm, None)  # was just marked "skipped_recent"
                    self.status.skipped_recent = max(0, self.status.skipped_recent - 1)
                    self._enqueue(sm, depth=0, priority=300, parent_section=None, reason="rediscover")
                log.info("rediscover", queued=len(self.frontier))
        if not len(self.frontier):
            raise StopRun("done", "nothing to crawl (all seeds disallowed or recently checked)")

    def _load_history(self) -> None:
        """6.7 Measure first: shift priorities toward page types and sections that gave hits before."""
        self.type_bonus: dict[str, float] = {}
        totals: dict[str, list[float]] = {}
        for (stats,) in self.store.db.execute("SELECT stats FROM runs WHERE stats IS NOT NULL"):
            try:
                d = json.loads(stats or "{}")
            except ValueError:
                continue
            pages = d.get("pages_by_type") or {}
            hits = d.get("hits_by_type") or {}
            for k, n in pages.items():
                t = totals.setdefault(k, [0.0, 0.0])
                t[0] += n
                t[1] += hits.get(k, 0)
        for k, (n, h) in totals.items():
            if n >= 10:  # only trust a measurement over at least 10 pages
                self.type_bonus[k] = min(20.0, 100.0 * h / n)  # +1 per hit per 100 pages, max +20
        # 6.7 per family of pages: qualifying finds per fetched listing/campaign page
        self.family_bonus: dict[str, float] = {}
        self.family_suppressed: set[str] = set()
        pages_per_fam: dict[str, int] = {}
        since = time.time() - self.s.learn_window_days * 86400  # recent only: events come and go
        for (url,) in self.store.db.execute(
                "SELECT url FROM pages WHERE page_type IN ('listing','campaign') AND last_fetched >= ?", (since,)):
            fam = page_family(url)
            pages_per_fam[fam] = pages_per_fam.get(fam, 0) + 1
        finds_per_fam: dict[str, int] = {}
        for (src,) in self.store.db.execute(
                "SELECT source_page FROM products WHERE qualifies=1 AND first_seen >= ?", (since,)):
            if src:
                fam = page_family(src)
                finds_per_fam[fam] = finds_per_fam.get(fam, 0) + 1
        for fam, n in pages_per_fam.items():
            yield_ = finds_per_fam.get(fam, 0) / n
            if n >= 3 and yield_ > 0:
                self.family_bonus[fam] = min(self.s.learn_max_bonus, yield_ * self.s.learn_bonus_per_find)
            if n >= self.s.learn_min_pages_for_penalty and yield_ < self.s.learn_poor_yield:
                # proven poor: penalty, and gadget/phone focus no longer lifts it
                self.family_bonus[fam] = -self.s.learn_penalty
                self.family_suppressed.add(fam)
        self.hot_sections: dict[str, float] = {}
        for url, best in self.store.db.execute(
                "SELECT url, best_discount FROM pages WHERE best_discount >= ? AND page_type IN ('listing','campaign')",
                (PRUNE_MIN_DISCOUNT,)):
            sec = section_for(url, None)
            self.hot_sections[sec] = max(self.hot_sections.get(sec, 0.0), best)
        if self.type_bonus or self.hot_sections or self.family_bonus:
            log.info("history_loaded", type_bonus=self.type_bonus, hot_sections=len(self.hot_sections),
                     families=len(self.family_bonus),
                     best=sorted(self.family_bonus.items(), key=lambda x: -x[1])[:5],
                     worst=sorted(self.family_bonus.items(), key=lambda x: x[1])[:5])

    def _family_open(self, url: str) -> bool:
        if hunt_match(urlsplit(url).path, self.s.hunt_keywords):
            return True  # hunt pages are never capped
        fam = page_family(url)
        if self.family_best.get(fam, 0.0) >= 79.5 or self.hot_sections.get(fam, 0.0) >= 79.5:
            return True  # a family that produced 80%+ finds stays open
        return self.family_pages.get(fam, 0) < self.s.family_cap_per_run

    def _focus(self, url: str) -> float:
        """Gadget/phone focus plus what past batches learned about this family of pages."""
        if hunt_match(urlsplit(url).path, self.s.hunt_keywords):
            return self.s.hunt_bonus  # mystery / surprise boxes first, whatever the family's history
        fam = page_family(url)
        learned = self.family_bonus.get(fam, 0.0)
        if fam in self.family_suppressed:
            return learned
        return (focus_bonus(url, self.s.focus_keywords, self.s.focus_bonus)
                + focus_bonus(url, self.s.phone_keywords, self.s.phone_bonus) + learned)

    def _history_bonus(self, url: str, kind: str, section: str) -> float:
        bonus = self.type_bonus.get(kind, 0.0)
        if kind != "sitemap":
            bonus += self._focus(url)  # gadgets and phones first
        best = self.hot_sections.get(section)
        if best is not None:
            bonus += 10.0 if best >= 79.5 else 4.0
        return bonus

    # ================================================================== queue
    def _tier_for(self, url: str) -> int:
        row = self.store.get_page(url)
        if row is None:
            return TIER_NEW
        if row["page_type"] == "product" and (row["best_discount"] or 0) >= 79.5:
            return TIER_STALE_QUALIFYING
        return TIER_OTHER

    def _enqueue(self, url: str, *, depth: int, priority: float, parent_section: str | None,
                 reason: str = "", parent: str | None = None) -> bool:
        assert self.robots is not None
        if self.frontier.seen(url):
            return False
        if not self.robots.allowed(url):  # C5: check at enqueue time
            self.status.skipped_robots += 1
            self.frontier.mark_visited(url, "disallowed")
            log.info("robots_skip", url=url, robots="disallow", stage="enqueue")
            return False
        if page_type(url) in ("listing", "campaign") and has_filter_params(url):
            self.frontier.mark_visited(url, "filter_variant")  # ?official_store= etc: subset of a page we read
            return False
        if is_asset(url):  # 2.2: no images or other assets, ever
            self.frontier.mark_visited(url, "asset")
            return False
        if not self._due(url) and not (self.rediscover and page_type(url) == "sitemap"):
            # 6.1: queued but not yet due -> skipped with no request and no budget, so the queue and
            # the predictions only count pages that will actually be fetched
            self.status.skipped_recent += 1
            self.frontier.mark_visited(url, "skipped_recent")
            return False
        kind = page_type(url)
        section = section_for(url, parent_section)
        e = Entry(url=url, depth=depth, priority=priority + self._history_bonus(url, kind, section),
                  tier=self._tier_for(url), kind=kind, section=section, parent=parent, reason=reason)
        return self.frontier.push(e)

    def _due(self, url: str) -> bool:
        row = self.store.get_page(url)
        if row is None:
            return True
        if row["next_check_at"] and row["next_check_at"] > time.time():
            return False
        lm = self.sitemap_lastmod.get(url)
        if lm and row["sitemap_lastmod"] == lm and row["last_fetched"]:
            return False  # 6.1: sitemap entry unchanged since last fetch
        return True

    # ================================================================== stop checks
    def _check_limits(self) -> None:
        if self.stop:
            raise self.stop
        if time.monotonic() >= self._deadline:
            self.stop = StopRun("done", "time cap (max_runtime_minutes)")
        elif self.status.new_qualifying >= self.s.target_candidates:
            self.stop = StopRun("done", f"target reached ({self.s.target_candidates} new qualifying)")
        elif self.status.errors > self.s.max_total_errors:
            self.stop = StopRun("stopped (errors)", f"more than max_total_errors ({self.s.max_total_errors})")
        if self.stop:
            raise self.stop

    # ================================================================== worker
    async def _worker(self, wid: int) -> None:
        while True:
            try:
                self._check_limits()
            except StopRun:
                return
            if self.pages_reserved >= self.s.max_links_per_run:
                if not self.stop:
                    self.stop = StopRun("done", "page cap (max_links_per_run)")
                return
            e = self.frontier.pop()
            self.status.queue = len(self.frontier)
            if e is None:
                if self.inflight == 0:
                    return
                await asyncio.sleep(0.2)
                continue
            if e.url in self.frontier.visited:
                continue
            assert self.robots is not None
            if not self.robots.allowed(e.url):  # C5: check again at fetch time
                self.status.skipped_robots += 1
                self.frontier.mark_visited(e.url, "disallowed")
                log.info("robots_skip", url=e.url, robots="disallow", stage="fetch")
                continue
            if (e.kind in ("listing", "campaign") and e.reason not in ("seed", "promo_seed")
                    and not self._family_open(e.url)):
                self.frontier.mark_visited(e.url, "family_cap")  # look-alike page: no request spent
                continue
            if not self._due(e.url) and not (self.rediscover and e.kind == "sitemap"):  # memory across runs
                self.status.skipped_recent += 1
                self.frontier.mark_visited(e.url, "skipped_recent")
                continue
            self.pages_reserved += 1
            self.inflight += 1
            try:
                await self._process(e)
            except StopRun as stop:
                if not self.stop:
                    self.stop = stop
                return
            finally:
                self.inflight -= 1

    async def _wait_network(self) -> bool:
        """During a network pause only one probe request goes out. Returns True if this worker is the probe."""
        if self.net_ok.is_set():
            return False
        if self.probe_lock.locked():
            await self.net_ok.wait()
            return False
        await self.probe_lock.acquire()
        return True

    async def _process(self, e: Entry) -> None:
        row = self.store.get_page(e.url)
        # no conditional request for sitemaps: a 304 has no body, and the URLs inside are needed
        etag = row["etag"] if row and e.kind != "sitemap" else None
        lastmod = row["last_modified"] if row and e.kind != "sitemap" else None
        kind_label = {"campaign": "campaign", "listing": "listing", "product": "product",
                      "sitemap": "sitemap"}.get(e.kind, e.kind)
        attempt = 0
        res: FetchResult | None = None
        page: PageResult | None = None
        while True:
            attempt += 1
            self._check_limits()
            probe = await self._wait_network()
            try:
                self.status.doing = f"fetching {e.url.replace('https://www.jumia.com.ng', '')} " \
                                    f"({kind_label}, depth {e.depth})" + (" [retry]" if attempt > 1 else "")
                await self.limiter.acquire()
                res = await self.fetcher.fetch(e.url, etag, lastmod)
                if res.outcome == Outcome.OK and res.body is not None and e.kind != "sitemap":
                    page = extract(res.body, e.url)
                    if page.truncated:  # incomplete download: treated as a network problem
                        res.outcome, res.reason = Outcome.TRUNCATED, page.truncated_reason
                self._account(res, e.kind, "allow", attempt)
                self._react(res, e.kind)
            finally:
                if probe:
                    self.probe_lock.release()
            if res.outcome in NETWORKISH:
                await self._network_failure(probe)
            else:
                # any real server response ends a network outage
                self.consecutive_net = 0
                if not self.net_ok.is_set():
                    self.net_ok.set()
                    self.failed_pauses = 0
                    if self.status.state == "paused (network)":
                        self.status.state = "running"
            if res.outcome not in RETRYABLE:
                break
            if not self._may_retry(attempt):
                break
            wait = max(self.s.retry_backoff_s, res.retry_after_s or 0) * (1 + random.uniform(0, 0.5))
            self.status.retries += 1
            self.status.estimate_note = f"retrying after {res.reason}"
            log.info("retry_scheduled", url=e.url, wait_s=round(wait, 1), reason=res.reason)
            await asyncio.sleep(wait)  # never alongside the original; then same limiter
        self._finish(e, res, page)

    def _may_retry(self, attempt: int) -> bool:
        if attempt >= self.s.max_tries_per_url:
            return False
        total = max(1, self.status.requests)
        return (self.status.retries + 1) <= self.s.max_retry_ratio * total

    # ================================================================== reactions
    def _account(self, res: FetchResult, kind: str, robots: str, attempt: int) -> None:
        self.status.requests += 1
        self.status.add_bytes(res.bytes_on_wire)
        self.status.current_rpm = self.limiter.requests_in_last_minute()
        self.status.effective_rate = self.limiter.rate
        if self.run_id is not None:
            self.store.log_request(self.run_id, res.url, kind, res.status, res.outcome.value, robots,
                                   res.bytes_on_wire, round(res.latency_s, 3), attempt)
        log.info("request", url=res.url, status=res.status, outcome=res.outcome.value, robots=robots,
                 bytes=res.bytes_on_wire, latency_s=round(res.latency_s, 3), attempt=attempt, reason=res.reason)

    def _react(self, res: FetchResult, kind: str = "page") -> None:
        """Kill switches and server pushback (2.2, 2.2.1)."""
        if res.outcome == Outcome.BLOCK:
            raise StopRun("stopped (block)", res.reason or "block signal")
        if res.outcome == Outcome.PUSHBACK and res.status == 429:
            self.consecutive_429 += 1
            if self.consecutive_429 >= 2:
                raise StopRun("stopped (block)", "second 429 in a row")
        elif res.status is not None:
            self.consecutive_429 = 0
        if res.outcome == Outcome.PUSHBACK:
            self.limiter.pause_for(res.retry_after_s or self.s.retry_backoff_s)  # Retry-After always honored
            self.limiter.server_pushback(5)
            self.status.state = "slowed"
            self.status.estimate_note = f"slowed: server pushback ({res.reason})"
        elif res.retry_after_s:
            self.limiter.pause_for(res.retry_after_s)
        if res.outcome in (Outcome.SERVER_ERROR, Outcome.NETWORK_ERROR, Outcome.TRUNCATED, Outcome.PUSHBACK):
            self.status.errors += 1
        if res.outcome not in NETWORKISH and res.status is not None:
            if self.limiter.record_response(res.latency_s, kind):
                self.status.state = "slowed"
                self.status.estimate_note = f"slowed: {self.limiter.slow_reason}"
            elif not self.limiter.slowed and res.outcome in (Outcome.OK, Outcome.NOT_MODIFIED):
                # a normal response after a slowdown, pause or retry: back to normal estimates
                if self.status.state in ("slowed", "starting", "paused (network)"):
                    self.status.state = "running"
                self.status.estimate_note = None
        self.status.median_latency = self.limiter.median_latency

    async def _network_failure(self, was_probe: bool) -> None:
        self.consecutive_net += 1
        if was_probe:
            self.failed_pauses += 1
        if self.failed_pauses >= self.s.max_network_pauses:
            # not a block: checkpoint is saved by run(), no 24-hour lock
            raise StopRun("stopped (network)", f"network still failing after {self.failed_pauses} pauses")
        if self.consecutive_net >= self.s.max_consecutive_network_errors or was_probe:
            self.consecutive_net = 0
            self.net_ok.clear()
            self.status.state = "paused (network)"
            self.status.estimate_note = f"paused: {self.s.max_consecutive_network_errors} network errors in a row"
            log.warning("network_pause", seconds=self.s.network_pause_s, failed_pauses=self.failed_pauses)
            # workers wait on net_ok; the next request after the pause is the single probe
            self.limiter.pause_for(self.s.network_pause_s)

    # ================================================================== results
    def _finish(self, e: Entry, res: FetchResult | None, page: PageResult | None) -> None:
        assert res is not None
        now = time.time()
        h = 3600.0
        self.frontier.mark_visited(e.url, res.outcome.value)
        nbytes = res.bytes_on_wire

        if res.outcome in RETRYABLE or res.outcome == Outcome.CLIENT_ERROR:
            self.store.upsert_page(e.url, page_type=e.kind, last_outcome=res.outcome.value,
                                   next_check_at=now + self.s.recheck_error_hours * h)
            self.status.page_done(e.kind, nbytes, 0)
            return
        if res.outcome == Outcome.GONE:
            self.store.upsert_page(e.url, page_type=e.kind, last_fetched=now, last_outcome="gone",
                                   next_check_at=now + self.s.recheck_product_below_hours * h)
            if e.kind == "product":
                for r in self.store.db.execute("SELECT sku FROM products WHERE url=?", (e.url,)):
                    self.store.mark_gone(r["sku"])
            self.status.page_done(e.kind, nbytes, 0)
            return
        if res.outcome == Outcome.REDIRECT:
            target = normalize(res.location or "", e.url)
            self.store.upsert_page(e.url, page_type=e.kind, last_fetched=now, last_outcome="redirect",
                                   next_check_at=now + self.s.recheck_listing_hours * h)
            if target:
                self._enqueue(target, depth=e.depth, priority=link_priority(target, None, False),
                              parent_section=e.section, parent=e.parent, reason=e.reason)
            self.status.page_done(e.kind, nbytes, 0)
            return

        prev = self.store.get_page(e.url)
        body = res.body or ""
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest() if body else None
        unchanged = res.outcome == Outcome.NOT_MODIFIED or (digest and prev is not None and prev["content_hash"] == digest)
        if e.kind == "sitemap" and res.outcome != Outcome.NOT_MODIFIED:
            unchanged = False  # always parse sitemaps: they are where URLs come from (dedupe happens at enqueue)
        next_at = now + self._recheck_hours(e, prev["best_discount"] if prev else None) * h
        if unchanged and prev is not None:
            # F12: identical content: skip parsing, keep the earlier result, push next_check_at forward
            if res.outcome == Outcome.NOT_MODIFIED:
                self.status.not_modified += 1
            else:
                self.status.unchanged_hash += 1
            self.store.upsert_page(e.url, last_fetched=now, last_outcome="unchanged", next_check_at=next_at,
                                   sitemap_lastmod=self.sitemap_lastmod.get(e.url, prev["sitemap_lastmod"]))
            self.status.page_done(e.kind, nbytes, 0)
            return

        if e.kind == "sitemap":
            self._handle_sitemap(e, body)
            self.store.upsert_page(e.url, page_type="sitemap", last_fetched=now, content_hash=digest,
                                   etag=res.etag, last_modified=res.last_modified, last_outcome="ok",
                                   next_check_at=now + self.s.recheck_listing_hours * h)
            self.status.page_done("sitemap", nbytes, 0)
            return

        assert page is not None
        new_hits = self._handle_products(e, page)
        best = page.best_discount
        next_at = now + self._recheck_hours(e, best) * h
        self.store.upsert_page(
            e.url, page_type=e.kind, title=page.title, last_fetched=now, content_hash=digest,
            best_discount=best, etag=res.etag, last_modified=res.last_modified, last_outcome="ok",
            next_check_at=next_at, sitemap_lastmod=self.sitemap_lastmod.get(e.url))
        self._update_prune(e, page)
        self._queue_links(e, page)
        fam = page_family(e.url)
        self.family_pages[fam] = self.family_pages.get(fam, 0) + 1
        if best is not None:
            self.family_best[fam] = max(self.family_best.get(fam, 0.0), best)
        self.status.page_done(e.kind, nbytes, new_hits)

    def _recheck_hours(self, e: Entry, best: float | None) -> float:
        """6.1 revisit table."""
        if e.kind != "product":
            return self.s.recheck_listing_hours
        prow = self.store.db.execute("SELECT review_status FROM products WHERE url=? AND qualifies=1",
                                     (e.url,)).fetchone()
        if prow is not None and prow["review_status"] in ("rejected", "hidden"):
            return self.s.recheck_rejected_days * 24
        if best is not None and best >= 79.5:
            return self.s.recheck_product_qualifying_hours  # also covers approved (6-hour re-verify)
        return self.s.recheck_product_below_hours

    def _handle_products(self, e: Entry, page: PageResult) -> int:
        now = time.time()
        new_hits = 0
        candidates = list(page.products)
        if page.kind == "product":
            candidates += page.extra_candidates  # F19: organic viewed-together cards
        seen: set[str] = set()
        for c in candidates:
            if not c.sku or c.sku in seen or c.verdict is None:
                continue
            seen.add(c.sku)
            v = c.verdict
            is_hunt = hunt_match(c.title, self.s.hunt_keywords)
            if is_hunt and "mystery_box" not in v.flags:
                v.flags.append("mystery_box")
            hunt_ok = is_hunt and v.discount_pct is not None and v.discount_pct >= self.s.hunt_min_discount
            qualifies = (v.approved or hunt_ok) and self._price_ok(c.price, c.old_price)
            if (qualifies and "old_price_inflated" not in v.flags
                    and inflated_old_price(c, candidates, self.s.inflation_factor)):
                v.flags.append("old_price_inflated")
            on_product_page = page.kind == "product" and c in page.products
            q = dict(
                title=c.title, price=c.price, old_price=c.old_price, currency="NGN", source_page=e.url,
                flags=v.flags, campaign_hint=c.campaign_hint, rating_avg=c.rating_avg,
                rating_count=c.rating_count, seller_score=c.seller_score, seller_quality=c.seller_quality,
                stock_text=c.stock_text, category=c.category,
                card_seen_at=None if on_product_page else (now if c.has_old_price else None),
                product_page_checked_at=now if on_product_page else None,
            )
            status = self.store.upsert_product(sku=c.sku, qualifies=qualifies, discount_pct=v.discount_pct,
                                               url=c.url or (e.url if on_product_page else None),
                                               run_id=self.run_id or 0, **q)
            if qualifies:
                if self.status.best_discount is None or (v.discount_pct or 0) > self.status.best_discount:
                    self.status.best_discount = v.discount_pct
                if status == "new":
                    new_hits += 1
                    self.status.new_qualifying += 1
                    log.info("new_qualifying", sku=c.sku, discount=v.discount_pct, url=c.url, flags=v.flags)
                if ((v.discount_pct or 0) >= self.s.alert_min_discount or is_hunt) and self.store.mark_alerted(c.sku):
                    self._hot_find(c, v, e.url)
        return new_hits

    def _hot_find(self, c, v, source: str) -> None:
        """Alert once per product: alerts.log, status.json / live panel, and the request log."""
        rec = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "sku": c.sku,
               "discount_pct": v.discount_pct, "price": c.price, "old_price": c.old_price,
               "title": c.title, "url": c.url, "flags": v.flags, "source_page": source}
        self.status.hot_finds = ([rec] + self.status.hot_finds)[:10]
        try:
            with open(self.s.alerts_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        log.warning("hot_find", **{k: rec[k] for k in ("sku", "discount_pct", "price", "old_price", "url")})

    def _price_ok(self, price: float | None, old_price: float | None) -> bool:
        return in_price_range(price, old_price, self.s.max_price_ngn, self.s.max_old_price_ngn)

    def _update_prune(self, e: Entry, page: PageResult) -> None:
        """F8: stop a section after prune_window consecutive pages with no card at 50%+."""
        if e.kind not in ("listing", "campaign"):
            return
        has = any(c.verdict and (c.verdict.discount_pct or 0) >= PRUNE_MIN_DISCOUNT
                  for c in page.products)
        if has:
            self.prune_counts[e.section] = 0
            return
        n = self.prune_counts.get(e.section, 0) + 1
        self.prune_counts[e.section] = n
        if n >= self.s.prune_window:
            self.frontier.prune(e.section)
            log.info("section_pruned", section=e.section, pages=n)

    def _page_hints(self, page: PageResult) -> bool:
        """Campaign hints count only if not site-wide boilerplate (seen on most pages)."""
        self.hint_page_total += 1
        useful = False
        for hnt in page.campaign_hints:
            self.hint_pages[hnt] = self.hint_pages.get(hnt, 0) + 1
            if self.hint_page_total < 3 or self.hint_pages[hnt] / self.hint_page_total < 0.6:
                useful = True
        return useful

    def _next_page_url(self, e: Entry, page: PageResult) -> str | None:
        """The ?page=N+1 link of the listing being read, if the page links to it."""
        if e.kind not in ("listing", "campaign"):
            return None
        base = urlsplit(e.url)
        want = page_number(e.url) + 1
        for link in page.links:
            u = urlsplit(link.url)
            if u.path == base.path and page_number(link.url) == want:
                return link.url
        return None

    def _queue_links(self, e: Entry, page: PageResult) -> None:
        """Pick the best max_outlinks_per_page links from this page (6.4), with a fair mix:
        1. the next page of this listing (paging inside one listing is not a new hop),
        2. product pages only when needed (6.3: missing old price, or verifying an 80%+ find),
        3. campaign and listing pages, each kind limited to half of the remaining slots so one
           kind (e.g. dozens of single-product mlp- pages in a menu) cannot crowd out the rest.
        """
        now = time.time()
        h = 3600.0
        parent_best = page.best_discount
        page_hint = self._page_hints(page)
        budget = self.s.max_outlinks_per_page
        pushed = 0

        nxt = self._next_page_url(e, page)
        if nxt and not self.frontier.seen(nxt):
            pr = link_priority(nxt, parent_best, False, page_hint) + 20
            if self._enqueue(nxt, depth=e.depth, priority=pr, parent_section=e.section,
                             reason="next_page", parent=e.url):
                pushed += 1

        # product pages only when needed (6.3); verifying a find is not discovery, so any depth
        products: list[tuple[float, str, str]] = []
        for c in page.products if page.kind != "product" else []:
            if not c.url or c.verdict is None:
                continue
            reason = None
            row = self.store.get_product(c.sku)
            if not c.has_old_price:
                if row is not None and row["card_seen_at"] and                         now - row["card_seen_at"] < self.s.recheck_product_below_hours * h:
                    continue  # a card with old price data was read within the revisit window
                reason = "no_old_price"
            elif (c.verdict.approved or (hunt_match(c.title, self.s.hunt_keywords)
                                         and (c.verdict.discount_pct or 0) >= self.s.hunt_min_discount)) \
                    and self._price_ok(c.price, c.old_price):
                if (row is not None and row["review_status"] in ("hidden", "rejected")
                        and row["price"] == c.price and row["old_price"] == c.old_price):
                    continue  # memo: hidden/rejected and unchanged -> no request spent on it
                if row is not None and row["product_page_checked_at"] and                         now - row["product_page_checked_at"] < self.s.recheck_product_qualifying_hours * h:
                    continue
                reason = "verify"
            if reason:
                pr = link_priority(c.url, parent_best, False) + self._focus(c.url) + (
                    c.verdict.discount_pct or 0) * (1.0 if reason == "verify" else 0.3)
                products.append((pr, c.url, reason))
        products.sort(key=lambda o: -o[0])
        for pr, url, reason in products:
            if pushed >= budget:
                return
            if self._enqueue(url, depth=e.depth + 1, priority=pr, parent_section=e.section, reason=reason,
                             parent=e.url):
                pushed += 1

        if e.depth >= self.s.max_depth:
            return  # max_depth: no new navigation hops from here
        nav: dict[str, list[tuple[float, str]]] = {"campaign": [], "listing": []}
        for link in page.links:
            k = page_type(link.url)
            if k not in nav or link.url == nxt or self.frontier.seen(link.url):
                continue
            if has_filter_params(link.url):
                continue  # ?official_store= / ?damage_grade= ...: a subset of a page we read anyway
            if not self._family_open(link.url):
                continue  # this family of look-alike pages already had its share this batch
            if not self.robots.allowed(link.url) or is_asset(link.url) or not self._due(link.url):
                # rejected links must not take a slot; _enqueue records why (robots / recently checked)
                self._enqueue(link.url, depth=e.depth + 1, priority=0, parent_section=e.section)
                continue
            hunt = self.s.hunt_bonus if (hunt_match(link.anchor, self.s.hunt_keywords)
                                         and not hunt_match(urlsplit(link.url).path, self.s.hunt_keywords)) else 0
            nav[k].append((link_priority(link.url, parent_best, link.campaign_hint, page_hint)
                           + self._focus(link.url) + hunt, link.url))
        for lst in nav.values():
            lst.sort(key=lambda o: -o[0])
        left = budget - pushed
        per_kind = max(1, (left + 1) // 2)
        chosen: list[tuple[float, str]] = []
        rest: list[tuple[float, str]] = []
        for lst in nav.values():
            chosen += lst[:per_kind]
            rest += lst[per_kind:]
        chosen.sort(key=lambda o: -o[0])
        rest.sort(key=lambda o: -o[0])
        for pr, url in chosen + rest:  # unused slots of one kind go to the other
            if pushed >= budget:
                break
            if self._enqueue(url, depth=e.depth + 1, priority=pr, parent_section=e.section, parent=e.url):
                pushed += 1

    def _handle_sitemap(self, e: Entry, body: str) -> None:
        sm = parse_sitemap(body)
        if sm.is_index:
            children = sorted(sm.entries, key=lambda x: child_sitemap_rank(x.loc))
            for child in children:
                # max_sitemaps_per_run is one cap across all three indexes, not per index
                if self.child_sitemaps_queued >= self.s.max_sitemaps_per_run:
                    break
                u = child.loc.strip()
                if child_sitemap_rank(u) >= 97:
                    continue  # blog / product sitemaps: never useful for listing discovery
                if child.lastmod:
                    self.sitemap_lastmod[u] = child.lastmod
                if self._enqueue(u, depth=0, priority=140 - child_sitemap_rank(u), parent_section=None,
                                 reason="sitemap"):
                    self.child_sitemaps_queued += 1
            return
        self.sitemaps_read += 1
        for ent in sm.entries:
            u = normalize(ent.loc)
            if not u:
                continue
            k = page_type(u)
            # 6.3: product pages are not seeded from sitemaps; they are fetched only when needed
            if k not in ("listing", "campaign", "sitemap"):
                continue
            if ent.lastmod:
                self.sitemap_lastmod[u] = ent.lastmod
            self._enqueue(u, depth=0, priority=link_priority(u, None, False), parent_section=None,
                          reason="sitemap")

    # ================================================================== status
    async def _tick(self) -> None:
        live = None
        if self.live:
            from rich.live import Live
            live = Live(self.status.panel(), refresh_per_second=2, transient=False)
            live.start()
        n = 0
        try:
            while True:
                self.status.queue = len(self.frontier)
                self.status.queue_kinds = self.frontier.peek_kinds()
                self.status.current_rpm = self.limiter.requests_in_last_minute()
                if self.status.state == "starting" and self.status.requests:
                    self.status.state = "running"
                try:
                    self.status.write(self.s.status_path)
                except OSError:
                    pass
                if live is not None:
                    live.update(self.status.panel())
                n += 1
                if n % 60 == 0 and len(self.frontier):  # F11: periodic checkpoint (every minute)
                    self.store.set_kv("checkpoint", self.frontier.to_json())
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            if live is not None:
                live.update(self.status.panel())
                live.stop()

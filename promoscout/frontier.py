"""Best-first frontier (F7) with a visited dictionary keyed by normalized URL (F4)."""
from __future__ import annotations

import heapq
import itertools
import json
import re
from dataclasses import asdict, dataclass

from .urls import page_number, page_type, section_of

# Run order (6.1): never-seen first, then stale qualifying products, then everything else due.
TIER_NEW, TIER_STALE_QUALIFYING, TIER_OTHER = 0, 1, 2

PATTERN_SCORE = {
    "sitemap": 100,
    "campaign": 70,   # mlp- landing pages, /flash-sales/, sp-
    "listing": 50,    # allowed category pages
    "product": 15,
    "other": 0,
}
PROMO_PATH_RE = re.compile(r"/(flash-sales|mlp-black-friday|mlp-[^/]+)/")


@dataclass
class Entry:
    url: str
    depth: int
    priority: float
    tier: int
    kind: str
    section: str
    parent: str | None = None
    reason: str = ""  # why a product page was queued: "no_old_price" | "verify"


def link_priority(url: str, parent_best: float | None, anchor_hint: bool, page_hints: bool = False) -> float:
    """6.4: URL pattern score + parent page's best discount + campaign hints in anchor text."""
    kind = page_type(url)
    score = PATTERN_SCORE.get(kind, 0)
    if PROMO_PATH_RE.search(url):
        score += 15
    if kind == "listing":
        score -= min(20, (page_number(url) - 1) * 2)  # later pages of a listing slightly lower
    score += (parent_best or 0) * 0.5
    if anchor_hint:
        score += 10
    if page_hints:
        score += 3
    return score


class Frontier:
    def __init__(self) -> None:
        self._heap: list[tuple[int, float, int, str]] = []
        self._entries: dict[str, Entry] = {}
        self.visited: dict[str, str] = {}  # url -> outcome
        self._seq = itertools.count()
        self.pruned_sections: set[str] = set()

    def __len__(self) -> int:
        return len(self._entries)

    def seen(self, url: str) -> bool:
        return url in self.visited or url in self._entries

    def push(self, e: Entry) -> bool:
        if e.url in self.visited or e.section in self.pruned_sections and e.kind != "sitemap":
            return False
        cur = self._entries.get(e.url)
        if cur is not None:
            if (e.tier, -e.priority) >= (cur.tier, -cur.priority):
                return False  # already queued at equal or better priority
        self._entries[e.url] = e
        heapq.heappush(self._heap, (e.tier, -e.priority, next(self._seq), e.url))
        return True

    def pop(self) -> Entry | None:
        while self._heap:
            tier, negp, _, url = heapq.heappop(self._heap)
            e = self._entries.get(url)
            if e is None or e.tier != tier or -e.priority != negp:
                continue  # stale heap item
            del self._entries[url]
            if e.section in self.pruned_sections and e.kind != "sitemap":
                continue
            return e
        return None

    def peek_kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._entries.values():
            out[e.kind] = out.get(e.kind, 0) + 1
        return out

    def prune(self, section: str) -> None:
        self.pruned_sections.add(section)

    def mark_visited(self, url: str, outcome: str) -> None:
        self.visited[url] = outcome
        self._entries.pop(url, None)

    # -------------------------------------------------------------- checkpoint (F11)
    def to_json(self) -> str:
        return json.dumps({
            "entries": [asdict(e) for e in self._entries.values()],
            "visited": self.visited,
            "pruned": sorted(self.pruned_sections),
        })

    @classmethod
    def from_json(cls, text: str) -> "Frontier":
        f = cls()
        data = json.loads(text)
        f.visited = dict(data.get("visited", {}))
        f.pruned_sections = set(data.get("pruned", []))
        for d in data.get("entries", []):
            f.push(Entry(**d))
        return f


def section_for(url: str, parent_section: str | None) -> str:
    """Products inherit the section of the listing they were found on."""
    return parent_section if page_type(url) == "product" and parent_section else section_of(url)

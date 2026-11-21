"""robots.txt rules (C5-C8, F3). Protego gives wildcard + longest-match semantics."""
from __future__ import annotations

from urllib.parse import urlsplit

from protego import Protego

from .settings import USER_AGENT

# Belt-and-braces: paths the PRD (C6) says must never be fetched, even if robots.txt changes
# to allow them. robots.txt is still the primary authority; this only ever denies more.
HARD_DENY_SUBSTRINGS = ("/cart/", "/customer/", "/fragment/", "__cf_chl_rt_tk")


class Robots:
    def __init__(self, text: str):
        self.text = text
        self._p = Protego.parse(text)
        self.sitemaps = list(self._p.sitemaps)

    def allowed(self, url: str) -> bool:
        if any(s in url for s in HARD_DENY_SUBSTRINGS):
            return False
        host = urlsplit(url).hostname or ""
        # Sitemap files on the static host are explicitly published in robots.txt (C7).
        if host == "static.jumia.com.ng":
            return url in self.sitemaps or url.endswith((".xml", ".xml.gz"))
        return bool(self._p.can_fetch(url, USER_AGENT))

    def decision(self, url: str) -> str:
        return "allow" if self.allowed(url) else "disallow"

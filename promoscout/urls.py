"""URL normalization and page-type classification (F4)."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .settings import ALLOWED_HOSTS, BASE_URL

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "mc_cid", "mc_eid", "_ga", "_gl",
    "ref", "referrer", "source", "tkwl", "tkfs", "spm",
}

PRODUCT_RE = re.compile(r"-\d+\.html$")
CAMPAIGN_RE = re.compile(r"^/(mlp-[^/]+|sp-[^/]+|flash-sales)/$")


def normalize(url: str, base: str = BASE_URL) -> str | None:
    """Absolute https URL, lowercase host, no fragment, no tracking params, sorted query.

    Returns None for anything off-site or non-http.
    """
    if not url:
        return None
    url = url.strip()
    if url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    parts = urlsplit(urljoin(base, url))
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return None
    if host == "jumia.com.ng":
        host = "www.jumia.com.ng"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_KEYS
    ]
    query.sort()
    path = parts.path or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def page_type(url: str) -> str:
    """sitemap | product | campaign | listing | other."""
    parts = urlsplit(url)
    path = parts.path
    if path.endswith((".xml", ".xml.gz")) or "/sitemap" in path:
        return "sitemap"
    if PRODUCT_RE.search(path):
        return "product"
    if CAMPAIGN_RE.match(path):
        return "campaign"
    # one-segment category paths like /tablets/ (paging via ?page=N stays a listing)
    if re.fullmatch(r"/[a-z0-9][a-z0-9\-]*/", path) and not path.startswith("/blog/"):
        return "listing"
    return "other"


def section_of(url: str) -> str:
    """Section key for pruning (F8): the first path segment."""
    path = urlsplit(url).path.strip("/")
    return path.split("/")[0] if path else "/"


def page_number(url: str) -> int:
    for k, v in parse_qsl(urlsplit(url).query):
        if k == "page" and v.isdigit():
            return int(v)
    return 1


ASSET_RE = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|mjs|map|woff2?|ttf|eot|otf|mp4|webm|mp3|pdf|zip)$", re.I)


def is_asset(url: str) -> bool:
    """2.2: the bot never fetches images or other assets."""
    path = urlsplit(url).path
    return bool(ASSET_RE.search(path)) or path.startswith(("/assets", "/cdn-cgi/"))

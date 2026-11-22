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

# product pages: /name-419065810.html and product-group pages /name-mpg12085122.html
PRODUCT_RE = re.compile(r"-(?:mpg)?\d+\.html$")
# campaign pages (5.3 promo patterns): /mlp-*/, /flash-sales/ and per-category /<category>/flash-sales/
CAMPAIGN_RE = re.compile(r"^/(?:mlp-[^/]+|flash-sales|[a-z0-9][a-z0-9-]*/flash-sales)/$")
SEG = r"[a-z0-9][a-z0-9\-]*"
SLP_RE = re.compile(r"^/slp/(?!nc/)[a-z0-9][a-z0-9\-]*/?$")
# listing pages: /tablets/ and brand-in-category /tablets/apple/ (robots.txt still decides per URL)
LISTING_RE = re.compile(rf"^/{SEG}/(?:{SEG}/)?$")
NOT_LISTING_FIRST = ("sp-", "blog", "customer", "cart", "catalog", "checkout", "wishlist", "en", "ur",
                     "assets", "fragment", "recommendation", "suggestion", "ratingreview", "mobapi")


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
    if SLP_RE.match(path):
        return "listing"  # keyword landing pages, e.g. /slp/hello-kitty-mystery-box
    # category paths like /tablets/ and /tablets/apple/ (paging via ?page=N stays a listing)
    if LISTING_RE.match(path):
        first = path.strip("/").split("/")[0]
        if not first.startswith(NOT_LISTING_FIRST[0]) and first not in NOT_LISTING_FIRST[1:]:
            return "listing"
    return "other"


def path_depth(url: str) -> int:
    return len([x for x in urlsplit(url).path.split("/") if x])


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


# query keys that only filter/sort the same listing; paging (?page=N) is the one we follow
FOLLOW_QUERY_KEYS = {"page"}


def has_filter_params(url: str) -> bool:
    """True if the URL has query parameters other than ?page= (filtered copy of a page)."""
    return any(k not in FOLLOW_QUERY_KEYS for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))


def page_family(url: str) -> str:
    """Family of look-alike pages: first path segment cut to its first two words.
    /mlp-samsung-galaxy-s4/ and /mlp-samsung-j2-core/ -> "mlp-samsung"; /tablets/apple/ -> "tablets"."""
    first = urlsplit(url).path.strip("/").split("/")[0]
    return "-".join(first.split("-")[:2]) if first else "/"


def hunt_match(text: str | None, keywords: list[str]) -> bool:
    """True if the text (URL path or title) contains a hunt keyword, ignoring case and -/_ vs space."""
    if not text:
        return False
    t = text.lower().replace("_", " ").replace("-", " ")
    return any(k.lower().replace("-", " ") in t for k in keywords)

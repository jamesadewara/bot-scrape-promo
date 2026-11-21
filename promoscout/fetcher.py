"""HTTP fetcher with failure classification and block detection (PRD 2.2, 2.2.1, F15).

- Fixed user-agent, single session, no proxies, no headless browser, HTML only.
- Waits up to read_timeout_s for the whole body; never cancels early to refetch.
- Redirects are NOT followed automatically: the target is returned so it can be
  normalized and robots-checked before any request is made to it.
"""
from __future__ import annotations

import asyncio
import gzip
import time
from dataclasses import dataclass
from enum import Enum

import aiohttp

from .settings import USER_AGENT, Settings

GZIP_MAGIC = bytes([0x1F, 0x8B])
CHALLENGE_TOKEN = "__cf_chl_rt_tk"
CHALLENGE_BODY_MARKERS = (
    "challenge-platform", "cf-challenge", "cf_chl_opt", "Just a moment...",
    "Attention Required! | Cloudflare", "cf-turnstile",
)
CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "hcaptcha.com", "captcha-delivery", "px-captcha")


class Outcome(str, Enum):
    OK = "ok"
    NOT_MODIFIED = "not_modified"
    REDIRECT = "redirect"
    BLOCK = "block"            # 403, captcha, Cloudflare challenge, second 429 in a row
    PUSHBACK = "pushback"      # single 429 / 503
    SERVER_ERROR = "server_error"
    NETWORK_ERROR = "network_error"
    TRUNCATED = "truncated"
    GONE = "gone"              # 404 / 410: page no longer exists
    CLIENT_ERROR = "client_error"


@dataclass
class FetchResult:
    url: str
    outcome: Outcome
    status: int | None = None
    body: str | None = None
    bytes_on_wire: int = 0
    latency_s: float = 0.0
    etag: str | None = None
    last_modified: str | None = None
    location: str | None = None
    retry_after_s: float | None = None
    reason: str | None = None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def detect_block(status: int, headers, body: str | None, url: str, location: str | None) -> str | None:
    """Return a reason if this response is a block signal (kill switch)."""
    if status == 403:
        return "HTTP 403"
    if CHALLENGE_TOKEN in url or (location and CHALLENGE_TOKEN in location):
        return "Cloudflare challenge token in URL/redirect"
    if headers is not None:
        if (headers.get("cf-mitigated") or "").lower() == "challenge":
            return "Cloudflare challenge header"
        if headers.get("cf-chl-bypass") or headers.get("cf-chl-gen"):
            return "Cloudflare challenge header"
    if body:
        head = body[:20000]
        if any(m in head for m in CHALLENGE_BODY_MARKERS) and "window.__STORE__" not in body:
            return "Cloudflare challenge interstitial"
        low = head.lower()
        if any(m in low for m in CAPTCHA_MARKERS) and "window.__STORE__" not in body:
            return "captcha"
    return None


class Fetcher:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None):
        self.s = settings
        self._session = session
        self._own_session = session is None

    async def __aenter__(self) -> "Fetcher":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=self.s.connect_timeout_s,
                sock_connect=self.s.connect_timeout_s,
                sock_read=self.s.read_timeout_s,
            )
            connector = aiohttp.TCPConnector(limit=self.s.max_concurrency, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8",
                    "Accept-Language": "en",
                    "Accept-Encoding": "gzip, deflate",
                },
                trust_env=False,  # C3: never route through an environment proxy
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()

    async def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        assert self._session is not None
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        t0 = time.monotonic()
        try:
            # whole-body wait is bounded by read_timeout_s overall (slow is not a failure)
            async with asyncio.timeout(self.s.connect_timeout_s + self.s.read_timeout_s):
                async with self._session.get(url, headers=headers, allow_redirects=False) as resp:
                    status = resp.status
                    location = resp.headers.get("Location")
                    raw = await resp.read()
                    latency = time.monotonic() - t0
                    clen = resp.headers.get("Content-Length")
                    wire = int(clen) if clen and clen.isdigit() else len(raw)
                    if raw[:2] == GZIP_MAGIC:  # gzipped sitemap file (.xml.gz), not transfer encoding
                        try:
                            raw = gzip.decompress(raw)
                        except (OSError, EOFError):
                            return FetchResult(url, Outcome.TRUNCATED, status=status, bytes_on_wire=wire,
                                               latency_s=latency, reason="incomplete gzip body")
                    body = raw.decode(resp.charset or "utf-8", errors="replace") if raw else ""
                    res = FetchResult(
                        url=url, outcome=Outcome.OK, status=status, bytes_on_wire=wire, latency_s=latency,
                        etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"),
                        location=location, retry_after_s=parse_retry_after(resp.headers.get("Retry-After")),
                    )
                    block = detect_block(status, resp.headers, body, str(resp.url), location)
                    if block:
                        res.outcome, res.reason = Outcome.BLOCK, block
                        return res
                    if status == 304:
                        res.outcome = Outcome.NOT_MODIFIED
                    elif status in (301, 302, 303, 307, 308):
                        res.outcome, res.reason = Outcome.REDIRECT, f"redirect to {location}"
                    elif status in (429, 503):
                        res.outcome, res.reason = Outcome.PUSHBACK, f"HTTP {status}"
                    elif 500 <= status < 600:
                        res.outcome, res.reason = Outcome.SERVER_ERROR, f"HTTP {status}"
                    elif status in (404, 410):
                        res.outcome, res.reason = Outcome.GONE, f"HTTP {status}"
                    elif 400 <= status < 500:
                        res.outcome, res.reason = Outcome.CLIENT_ERROR, f"HTTP {status}"
                    else:
                        res.body = body
                    return res
        except (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError) as e:
            return FetchResult(url, Outcome.TRUNCATED, latency_s=time.monotonic() - t0,
                               reason=f"incomplete body: {type(e).__name__}")
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            return FetchResult(url, Outcome.NETWORK_ERROR, latency_s=time.monotonic() - t0,
                               reason=f"{type(e).__name__}: {e}"[:200])

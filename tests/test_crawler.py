"""Crawl loop end-to-end with a fake fetcher (no network): rule, memory, kill switches, retries."""
import json

import pytest

from promoscout.crawler import Crawler
from promoscout.fetcher import FetchResult, Outcome, detect_block
from promoscout.settings import ROBOTS_URL, Settings
from promoscout.store import Store

B = "https://www.jumia.com.ng"


class NoWaitLimiter:
    """Counts acquisitions; pacing itself is covered by test_ratelimit."""
    rate = 30.0
    slowed = False
    slow_reason = None
    median_latency = 0.1

    def __init__(self):
        self.acquired = 0
        self.pauses = []
        self.pushbacks = 0

    async def acquire(self):
        self.acquired += 1

    def pause_for(self, s):
        self.pauses.append(s)

    def server_pushback(self, minutes=5):
        self.pushbacks += 1

    def record_response(self, latency, kind="page"):
        return False

    def requests_in_last_minute(self):
        return 0


class FakeFetcher:
    """routes: url -> list of responses (status, body, headers) consumed in order; last one repeats."""

    def __init__(self, routes, default=(404, "", {})):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.default = default
        self.calls = []

    async def fetch(self, url, etag=None, last_modified=None):
        self.calls.append(url)
        seq = self.routes.get(url)
        spec = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else self.default
        if spec == "network":
            return FetchResult(url, Outcome.NETWORK_ERROR, reason="ConnectionResetError")
        status, body, headers = spec
        res = FetchResult(url, Outcome.OK, status=status, bytes_on_wire=len(body), latency_s=0.1,
                          retry_after_s=float(headers["Retry-After"]) if "Retry-After" in headers else None,
                          location=headers.get("Location"))
        block = detect_block(status, headers, body, url, res.location)
        if block:
            res.outcome, res.reason = Outcome.BLOCK, block
        elif status in (429, 503):
            res.outcome, res.reason = Outcome.PUSHBACK, f"HTTP {status}"
        elif status >= 500:
            res.outcome = Outcome.SERVER_ERROR
        elif status == 404:
            res.outcome = Outcome.GONE
        elif status in (301, 302):
            res.outcome = Outcome.REDIRECT
        else:
            res.body = body
        return res


@pytest.fixture
def env(tmp_path, fixture_text):
    robots = (200, fixture_text("robots.txt"), {})

    def make(routes, default=(404, "", {}), **overrides):
        kw = dict(data_dir=tmp_path, max_runs_per_day=99, retry_backoff_s=0.01, network_pause_s=0.01)
        kw.update(overrides)
        s = Settings(**kw)
        store = Store(s.db_path)
        routes = {ROBOTS_URL: robots, **routes}
        f = FakeFetcher(routes, default)
        lim = NoWaitLimiter()
        return Crawler(s, store, f, limiter=lim), f, store, lim

    return make


def html(fixture_text, name):
    return (200, fixture_text(name), {})


async def test_listing_run_applies_rule_and_stays_in_robots(env, fixture_text):
    # product pages answer 500 here (no retry at the 10% cap), so verification leaves results as-is
    c, f, store, lim = env({B + "/tablets/": html(fixture_text, "listing_edge_cases.html")},
                           default=(500, "", {}))
    summary = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert summary.state == "done"
    q = {r["sku"]: r for r in store.qualifying()}
    assert set(q) == {"DEEP000001", "FREE000001", "EDGE795001", "BADGE00001", "TWIN000001", "FROM000001"}
    assert "check_price" in q["FREE000001"]["flags"]
    assert all(r["discount_pct"] >= 79.5 for r in q.values())                # discount accuracy metric
    below = store.get_product("SMALL00001")
    assert below["qualifies"] == 0 and below["title"] is None                # minimal form
    assert store.get_product("SPON000001") is None                           # sponsored never stored
    # every request went through the limiter and was allowed by robots
    assert lim.acquired == len(f.calls)
    assert all(c.robots.allowed(u) for u in f.calls if u != ROBOTS_URL)
    logged = store.db.execute("SELECT url, robots FROM requests").fetchall()
    assert len(logged) == len(f.calls) and all(r["robots"] == "allow" for r in logged)
    # qualifying cards queued their product pages for verification (6.3)
    assert B + "/deep-deal-400000100.html" in f.calls
    assert B + "/small-deal-400000101.html" not in f.calls                   # below 80: not fetched
    assert B + "/no-old-price-400000102.html" in f.calls                     # card lacks old price


async def test_disallowed_links_never_requested(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_tablets.html")}, max_depth=2)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    for bad in ("sort=", "q=", "/fragment/", "/cart/", "/customer/", "utm_"):
        assert not any(bad in u for u in f.calls), bad
    assert B + "/tablets/?page=2" in f.calls
    assert c.status.skipped_robots > 0


async def test_block_403_kills_and_locks(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": (403, "Forbidden", {})})
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s.state == "stopped (block)" and "403" in s.reason
    assert store.lock_until() > 0
    c2, f2, _, _ = env({})
    s2 = await c2.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s2.state == "refused" and f2.calls == []                          # no workaround


async def test_cloudflare_challenge_kills(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": (200, fixture_text("cloudflare_challenge.html"), {})})
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s.state == "stopped (block)" and "Cloudflare" in s.reason


async def test_second_429_in_a_row_is_block(env, fixture_text):
    c, f, store, lim = env({B + "/tablets/": [(429, "", {"Retry-After": "7"}), (429, "", {})]},
                           max_retry_ratio=1.0)
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s.state == "stopped (block)" and "second 429" in s.reason
    assert 7.0 in lim.pauses and lim.pushbacks == 1                           # Retry-After honored


async def test_single_429_then_ok_retries_once(env, fixture_text):
    c, f, store, lim = env({B + "/tablets/": [(429, "", {}), html(fixture_text, "listing_edge_cases.html")]},
                           default=(500, "", {}), max_retry_ratio=0.5, max_total_errors=100)
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s.state == "done"
    assert f.calls.count(B + "/tablets/") == 2 and c.status.retries >= 1   # one retry, not more
    assert store.get_product("DEEP000001")["qualifies"] == 1


async def test_retry_ratio_cap(env, fixture_text):
    # with the default 10% cap, the very first failure in a run gets no retry
    c, f, store, _ = env({B + "/tablets/": [(500, "", {}), html(fixture_text, "listing_edge_cases.html")]})
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert f.calls.count(B + "/tablets/") == 1
    assert store.get_page(B + "/tablets/")["last_outcome"] == "server_error"


async def test_truncated_retried_never_scored(env, fixture_text):
    full = fixture_text("listing_edge_cases.html")
    c, f, store, _ = env({B + "/tablets/": [(200, full[:400], {}), (200, full, {})]}, default=(500, "", {}),
                         max_retry_ratio=0.5)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert f.calls.count(B + "/tablets/") == 2
    assert store.get_product("DEEP000001")["qualifies"] == 1


async def test_truncated_twice_is_error_not_no_discount(env, fixture_text):
    full = fixture_text("listing_edge_cases.html")
    c, f, store, _ = env({B + "/tablets/": (200, full[:400], {})}, max_retry_ratio=1.0)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    page = store.get_page(B + "/tablets/")
    assert page["last_outcome"] == "truncated" and page["best_discount"] is None
    assert store.db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


async def test_memory_across_runs_skips_recent(env, fixture_text):
    routes = {B + "/tablets/": html(fixture_text, "listing_edge_cases.html")}
    c, f, store, _ = env(routes)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    c2, f2, _, _ = env(routes)
    s2 = await c2.run(seed=B + "/tablets/", use_sitemaps=False)
    assert f2.calls == [ROBOTS_URL]                       # robots re-fetched every run (C8), nothing else
    assert c2.status.skipped_recent == 1 and s2.pages == 0


async def test_content_hash_unchanged_skips_parse(env, fixture_text):
    routes = {B + "/tablets/": html(fixture_text, "listing_edge_cases.html")}
    c, f, store, _ = env(routes)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    store.db.execute("UPDATE pages SET next_check_at=0")
    c2, f2, _, _ = env(routes)
    await c2.run(seed=B + "/tablets/", use_sitemaps=False)
    assert c2.status.unchanged_hash == 1


async def test_network_outage_pauses_then_stops_without_lock(env, fixture_text):
    c, f, store, lim = env({B + "/tablets/": "network"}, max_retry_ratio=1.0, max_total_errors=100,
                           max_consecutive_network_errors=1)
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s.state in ("stopped (network)", "done")
    assert store.lock_until() == 0                        # not a block: no 24-hour lock
    assert 0.01 in lim.pauses


async def test_network_three_failed_pauses_stop(env, fixture_text):
    routes = {B + f"/cat{i}/": "network" for i in range(10)}
    listing = fixture_text("listing_edge_cases.html").replace(
        "</main>", "".join(f'<a href="/cat{i}/">c</a>' for i in range(10)) + "</main>")
    routes[B + "/tablets/"] = (200, listing, {})
    c, f, store, lim = env(routes, max_retry_ratio=1.0, max_total_errors=100,
                           max_consecutive_network_errors=2, max_concurrency=1)
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s.state == "stopped (network)"
    assert store.lock_until() == 0
    assert store.get_kv("checkpoint")                     # checkpoint saved


async def test_max_runs_per_day(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_edge_cases.html")}, max_runs_per_day=1)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    c2, f2, _, _ = env({}, max_runs_per_day=1)
    s2 = await c2.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s2.state == "refused" and f2.calls == []


async def test_page_cap(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_tablets.html")}, max_links_per_run=2)
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert s.pages <= 2 and len([u for u in f.calls if u != ROBOTS_URL]) <= 2


async def test_target_candidates_stops_early(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_edge_cases.html")}, target_candidates=3)
    s = await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert "target reached" in s.reason


async def test_rejected_shown_again_only_if_price_changed(env, fixture_text):
    routes = {B + "/tablets/": html(fixture_text, "listing_edge_cases.html")}
    c, f, store, _ = env(routes)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    store.set_review("DEEP000001", "rejected")
    store.db.execute("UPDATE pages SET next_check_at=0, content_hash=NULL")
    c2, _, _, _ = env(routes)
    await c2.run(seed=B + "/tablets/", use_sitemaps=False)
    assert store.get_product("DEEP000001")["review_status"] == "rejected"   # same price: stays hidden
    changed = fixture_text("listing_edge_cases.html").replace("₦ 5,000", "₦ 4,000")
    store.db.execute("UPDATE pages SET next_check_at=0")
    c3, _, _, _ = env({B + "/tablets/": (200, changed, {})})
    await c3.run(seed=B + "/tablets/", use_sitemaps=False)
    assert store.get_product("DEEP000001")["review_status"] == "pending"    # price changed: back in queue


async def test_status_file_written(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_edge_cases.html")})
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    st = json.loads(c.s.status_path.read_text(encoding="utf-8"))
    assert st["state"] == "done" and st["pages_done"] >= 1 and "predictions" in st
    assert st["predictions"]["estimating"] is True


async def test_sitemap_seeding(env, fixture_text):
    index = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             '<sitemap><loc>https://static.jumia.com.ng/sitemap-categories.xml</loc></sitemap></sitemapindex>')
    cats = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{B}/tablets/</loc><lastmod>2026-11-20</lastmod></url>'
            f'<url><loc>{B}/some-product-400000999.html</loc></url>'
            f'<url><loc>{B}/a/b/c/</loc></url></urlset>')
    c, f, store, _ = env({
        "https://static.jumia.com.ng/index-sitemap.xml": (200, index, {}),
        "https://static.jumia.com.ng/sitemap-categories.xml": (200, cats, {}),
        B + "/tablets/": html(fixture_text, "listing_edge_cases.html"),
    })
    await c.run(use_sitemaps=True)
    assert B + "/tablets/" in f.calls
    assert B + "/some-product-400000999.html" not in f.calls    # products not seeded from sitemaps
    assert B + "/a/b/c/" not in f.calls                         # robots
    assert store.get_page(B + "/tablets/")["sitemap_lastmod"] == "2026-11-20"


async def test_verify_404_marks_gone(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_edge_cases.html")})
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert store.get_product("DEEP000001")["qualifies"] == 0     # deal ended / page gone


async def test_product_page_verification_updates(env, fixture_text):
    page = fixture_text("product_nodizz.html")
    listing = fixture_text("listing_edge_cases.html").replace(
        "/deep-deal-400000100.html", "/nodizz-npad-702-kids-tablet-4gb-ram-128gb-blue-400000001.html").replace(
        "DEEP000001", "NO437MP6VV2E7NAFAMZ")
    c, f, store, _ = env({B + "/tablets/": (200, listing, {}),
                          B + "/nodizz-npad-702-kids-tablet-4gb-ram-128gb-blue-400000001.html": (200, page, {})},
                         default=(500, "", {}))
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    p = store.get_product("NO437MP6VV2E7NAFAMZ")
    assert p["qualifies"] == 1 and p["discount_pct"] == 81.9
    assert p["seller_score"] == 76 and p["stock_text"] == "Few units left"
    assert "listing_mismatch" in json.loads(p["flags"])
    assert p["product_page_checked_at"]


async def test_gz_child_sitemaps_ranked_and_capped(env, fixture_text):
    import gzip
    from promoscout.sitemaps import child_sitemap_rank
    assert child_sitemap_rank("https://static.jumia.com.ng/categories-sitemap.xml.gz") < \
        child_sitemap_rank("https://static.jumia.com.ng/mlps-sitemap.xml.gz") < \
        child_sitemap_rank(B + "/slp/nc/sitemap/sitemap-en-1.xml") < \
        child_sitemap_rank(B + "/blog/wp-sitemap-posts-post-1.xml")
    index = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             + "".join(f"<sitemap><loc>https://static.jumia.com.ng/{n}</loc></sitemap>" for n in (
                 "brands-sitemap-1.xml.gz", "categories-sitemap.xml.gz", "mlps-sitemap.xml.gz",
                 "products-sitemap-1.xml.gz")) + "</sitemapindex>")
    cats = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{B}/tablets/</loc></url></urlset>')
    c, f, store, _ = env({
        "https://static.jumia.com.ng/index-sitemap.xml": (200, index, {}),
        "https://static.jumia.com.ng/categories-sitemap.xml.gz": (200, cats, {}),
        B + "/tablets/": html(fixture_text, "listing_edge_cases.html"),
    }, max_sitemaps_per_run=2, default=(500, "", {}))
    await c.run(use_sitemaps=True)
    assert "https://static.jumia.com.ng/categories-sitemap.xml.gz" in f.calls
    assert "https://static.jumia.com.ng/mlps-sitemap.xml.gz" in f.calls
    assert "https://static.jumia.com.ng/brands-sitemap-1.xml.gz" not in f.calls      # global cap of 2
    assert "https://static.jumia.com.ng/products-sitemap-1.xml.gz" not in f.calls
    assert B + "/tablets/" in f.calls
    assert gzip.decompress(gzip.compress(b"x")) == b"x"


async def test_unfetched_urls_carry_into_next_run(env, fixture_text):
    routes = {B + "/tablets/": html(fixture_text, "listing_tablets.html")}
    c, f, store, _ = env(routes, max_links_per_run=1)
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert f.calls == [ROBOTS_URL, B + "/tablets/"]
    c2, f2, _, _ = env(routes, max_links_per_run=3)
    await c2.run(use_sitemaps=False)                       # no seed: work comes from the carry-over
    fetched = [u for u in f2.calls if u != ROBOTS_URL]
    assert fetched and B + "/tablets/" not in fetched       # already checked: not refetched


async def test_assets_never_fetched(env, fixture_text):
    listing = fixture_text("listing_edge_cases.html").replace(
        "</main>", '<a href="/logo.png">x</a><a href="/assets_he/app.js">y</a></main>')
    c, f, store, _ = env({B + "/tablets/": (200, listing, {})}, default=(500, "", {}))
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert not any(u.endswith((".png", ".js")) for u in f.calls)


async def test_history_bonus_prefers_types_with_hits(env, fixture_text):
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_edge_cases.html")}, default=(500, "", {}))
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    store.db.execute("UPDATE runs SET stats=? ", (json.dumps(
        {"pages_by_type": {"campaign": 20, "listing": 20}, "hits_by_type": {"campaign": 4, "listing": 0}}),))
    c2, _, _, _ = env({})
    c2._load_history()
    assert c2.type_bonus["campaign"] > c2.type_bonus.get("listing", 0)
    assert c2.hot_sections.get("tablets", 0) >= 79.5      # section that had 80%+ before


def test_check_command(env, fixture_text, capsys):
    import asyncio as _a
    from promoscout.cli import cmd_check
    c, f, store, _ = env({B + "/tablets/": html(fixture_text, "listing_edge_cases.html")}, default=(500, "", {}))
    _a.run(c.run(seed=B + "/tablets/", use_sitemaps=False))
    store.close()
    rc = cmd_check(None, c.s)
    out = capsys.readouterr().out
    assert "requests to disallowed URLs: 0" in out and "OVERALL: OK" in out and rc == 0


async def test_price_filter_applied(env, fixture_text):
    # DEEP: price 5,000 -> in; make a 90% card with price 200,000 and old 2,000,000 -> out
    listing = fixture_text("listing_edge_cases.html").replace(
        '<div class="prc">₦ 9,000</div><div class="s-prc-w"><div class="old">₦ 10,000</div>',
        '<div class="prc">₦ 200,000</div><div class="s-prc-w"><div class="old">₦ 2,000,000</div>')
    c, f, store, _ = env({B + "/tablets/": (200, listing, {})}, default=(500, "", {}))
    await c.run(seed=B + "/tablets/", use_sitemaps=False)
    assert store.get_product("SMALL00001")["qualifies"] == 0            # 90% but outside the price filter
    assert B + "/small-deal-400000101.html" not in f.calls              # no verify request spent on it
    assert store.get_product("DEEP000001")["qualifies"] == 1
    assert {r["sku"] for r in store.qualifying(20_000, 1_000_000)} >= {"DEEP000001"}

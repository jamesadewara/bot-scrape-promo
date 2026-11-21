"""Extraction against the appendix fixtures and the trimmed raw-fetch listing (5, F5, F14, F19)."""
from promoscout.extract import extract, listing_mismatch

B = "https://www.jumia.com.ng"


def by_sku(cands):
    return {c.sku: c for c in cands}


# --------------------------------------------------------------------------- HANSEN
def test_hansen_not_approved(fixture_text):
    r = extract(fixture_text("product_hansen.html"), B + "/hansen-18-inch-standing-fan-400000020.html")
    assert r.kind == "product" and not r.truncated
    assert len(r.products) == 1
    p = r.products[0]
    assert p.sku == "HA958HA85XRFDNAFAMZ"
    assert (p.price, p.old_price) == (17330, 20060)
    assert p.verdict.discount_pct == 13.6 and not p.verdict.approved
    assert "discount_mismatch" not in p.verdict.flags       # badge 14 and JSON -14 match
    assert p.campaign_hint and "BLF" in p.campaign_hint      # soft hint only
    assert r.extra_candidates == []                          # empty carousels: nothing extracted


def test_hansen_euro_fields_ignored(fixture_text):
    p = extract(fixture_text("product_hansen.html"), B + "/hansen-400000020.html").products[0]
    assert p.price not in (11.66, 1.84) and p.old_price not in (13.50,)


# --------------------------------------------------------------------------- NODIZZ
def nodizz(fixture_text):
    return extract(fixture_text("product_nodizz.html"),
                   B + "/nodizz-npad-702-kids-tablet-4gb-ram-128gb-blue-400000001.html")


def test_nodizz_approved(fixture_text):
    r = nodizz(fixture_text)
    p = r.products[0]
    assert p.sku == "NO437MP6VV2E7NAFAMZ"
    assert (p.price, p.old_price) == (69984, 385920)
    assert p.verdict.discount_pct == 81.9 and p.verdict.approved
    assert "discount_mismatch" not in p.verdict.flags
    assert "old_price_unverified" in p.verdict.flags


def test_nodizz_review_hints(fixture_text):
    p = nodizz(fixture_text).products[0]
    assert (p.rating_avg, p.rating_count) == (2.5, 18)
    assert p.seller_score == 76 and p.seller_quality == "Poor"
    assert p.stock_text == "Few units left"


def test_nodizz_flags(fixture_text):
    flags = nodizz(fixture_text).products[0].verdict.flags
    assert "old_price_inconsistent" in flags   # pink variant: same price, old price 241,920
    assert "listing_mismatch" in flags         # title 4GB/128GB vs details 1GB/16GB


def test_nodizz_sponsored_excluded_and_viewed_together_checked(fixture_text):
    r = nodizz(fixture_text)
    extra = by_sku(r.extra_candidates)
    assert not {"SM111MP0000001NAFAMZ", "TH222MP0000002NAFAMZ", "MK333MP0000003NAFAMZ"} & set(extra)
    assert set(extra) == {"NO437MP6VV2E8NAFAMZ", "KI444BK0000004NAFAMZ"}
    assert extra["NO437MP6VV2E8NAFAMZ"].verdict.discount_pct == 71.1
    assert "price_from" in extra["KI444BK0000004NAFAMZ"].verdict.flags
    assert not any(c.verdict.approved for c in r.extra_candidates)


def test_boilerplate_banner_ignored(fixture_text):
    r = nodizz(fixture_text)
    # the Hansen brand-day banner lives in the site-wide <aside>; it must not leak in
    assert not any("hansen" in l.url for l in r.links)
    assert "BLF26" not in r.campaign_hints and "Hansen Brand Day" not in r.campaign_hints
    assert not any("/cart/" in l.url for l in r.links)


# --------------------------------------------------------------------------- raw listing
def test_raw_listing_fixture(fixture_text):
    r = extract(fixture_text("listing_tablets.html"), B + "/tablets/")
    assert r.kind == "listing"
    c = by_sku(r.products)
    assert set(c) == {"TI527MP5Y6603NAFAMZ", "BR823MP7BVPCQNAFAMZ", "MK913MP7ZACB2NAFAMZ", "TI527MP6N9657NAFAMZ"}
    top = c["TI527MP5Y6603NAFAMZ"]
    assert (top.price, top.old_price) == (102999, 630000)
    assert top.verdict.discount_pct == 83.7 and top.verdict.approved     # badge says 84%
    assert "discount_mismatch" not in top.verdict.flags
    assert not c["BR823MP7BVPCQNAFAMZ"].verdict.approved                 # 75.8%
    assert c["MK913MP7ZACB2NAFAMZ"].verdict.discount_pct == 22.5
    links = {l.url for l in r.links}
    assert B + "/tablets/?page=2" in links                               # fragment stripped
    assert B + "/smartphones/" in links                                  # utm stripped


def test_listing_html_cards_without_store(fixture_text):
    r = extract(fixture_text("listing_edge_cases.html"), B + "/tablets/")
    c = by_sku(r.products)
    assert "SPON000001" not in c                                          # sponsored excluded
    assert c["TWIN000001"].title == "Twin organic copy"                   # organic kept, deduped
    assert c["DEEP000001"].verdict.approved and c["DEEP000001"].verdict.discount_pct == 90  # data-oprc
    assert c["DEEP000001"].price == 5000                                  # not the euro 3.00
    assert not c["SMALL00001"].verdict.approved
    assert c["NOOLD00001"].verdict.discount_pct is None and not c["NOOLD00001"].verdict.approved
    assert not c["NOOLD00001"].has_old_price
    free = c["FREE000001"].verdict
    assert free.approved and "check_price" in free.flags
    assert c["EDGE795001"].verdict.approved and not c["EDGE794001"].verdict.approved
    assert "discount_mismatch" in c["BADGE00001"].verdict.flags
    assert "price_from" in c["FROM000001"].verdict.flags and c["FROM000001"].verdict.approved


# --------------------------------------------------------------------------- truncation
def test_truncated_page_never_scored(fixture_text):
    html = fixture_text("product_nodizz.html")
    r = extract(html[: len(html) // 2], B + "/nodizz-400000001.html")
    assert r.truncated and r.products == []
    html2 = html.replace("window.__STORE__", "window.__OTHER__")
    r2 = extract(html2, B + "/nodizz-400000001.html")
    assert r2.truncated and "__STORE__" in r2.truncated_reason


def test_listing_mismatch_only_on_gb_disagreement():
    assert listing_mismatch("Tab 4GB RAM 128GB ROM", "RAM: 1GB ROM: 16GB")
    assert not listing_mismatch("Tab 4GB RAM 64GB ROM", "RAM: 4GB, ROM: 64GB")
    assert not listing_mismatch("Standing fan 18 inch", "Power 60W")


def test_product_html_price_block_fallback(fixture_text):
    # __STORE__ present but without the product entry -> 5.1 second source: HTML price block
    html = fixture_text("product_nodizz.html")
    i = html.index("<script>window.__STORE__")
    html = html[:i] + '<script>window.__STORE__={"view":"Product","sku":"NO437MP6VV2E7NAFAMZ","products":[]};</script></body></html>'
    r = extract(html, B + "/nodizz-npad-702-kids-tablet-4gb-ram-128gb-blue-400000001.html")
    p = r.products[0]
    assert p.sku == "NO437MP6VV2E7NAFAMZ" and (p.price, p.old_price) == (69984, 385920)
    assert p.verdict.approved and p.verdict.discount_pct == 81.9 and p.seller_score == 76


def test_asset_urls():
    from promoscout.urls import is_asset
    assert is_asset(B + "/x/logo.png") and is_asset(B + "/assets_he/a.css") and not is_asset(B + "/tablets/")

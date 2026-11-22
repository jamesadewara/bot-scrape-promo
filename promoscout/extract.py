"""Price extraction from listing cards and product pages (PRD 5, F5, F14, F19).

Source order (5.1): window.__STORE__ JSON -> HTML badge + struck-through price -> JSON-LD.
Euro fields (priceEuro, discountEuro, data-ga4-*) are never read.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from .scoring import Verdict, apply_rule, parse_naira, parse_percent
from .urls import normalize, page_type

STORE_MARKER = "window.__STORE__"
CAMPAIGN_TEXT_RE = re.compile(
    r"black[\s-]?friday|flash[\s-]?sale|deal|clearance|% ?off|mega[\s-]?sale|promo|blf\d*"
    r"|treasure[\s-]?hunt|snatch|anniversary|11[.:\s-]?11|12[.:\s-]?12|price[\s-]?drop|crazy|giveaway"
    r"|9\d ?%|mystery|surprise[\s-]?box|blind[\s-]?box|lucky[\s-]?box", re.I
)
CAMPAIGN_IMG_RE = re.compile(r"(BLF\d+|black-friday/\d{4})", re.I)
DELIVERY_RE = re.compile(r"within the next \d+\s*hrs?", re.I)


@dataclass
class Candidate:
    sku: str
    title: str | None
    price: float | None
    old_price: float | None
    url: str | None
    source: str  # "store" | "card" | "product" | "jsonld"
    cross_checks: list[float | None] = field(default_factory=list)
    price_from: bool = False
    campaign_hint: str | None = None
    category: str | None = None  # e.g. "Phones & Tablets > Tablets > Android Tablets"
    rating_avg: float | None = None
    rating_count: int | None = None
    stock_text: str | None = None
    seller_score: int | None = None
    seller_quality: str | None = None
    extra_flags: list[str] = field(default_factory=list)
    verdict: Verdict | None = None

    def score(self) -> "Candidate":
        self.verdict = apply_rule(
            self.price, self.old_price, cross_checks=self.cross_checks, price_from=self.price_from
        )
        for f in self.extra_flags:
            if f not in self.verdict.flags:
                self.verdict.flags.append(f)
        return self

    @property
    def has_old_price(self) -> bool:
        return self.old_price is not None


@dataclass
class Link:
    url: str
    anchor: str
    campaign_hint: bool


@dataclass
class PageResult:
    url: str
    kind: str
    title: str | None = None
    products: list[Candidate] = field(default_factory=list)  # the page's own products
    extra_candidates: list[Candidate] = field(default_factory=list)  # F19 organic carousels
    links: list[Link] = field(default_factory=list)
    campaign_hints: list[str] = field(default_factory=list)
    truncated: bool = False
    truncated_reason: str | None = None

    @property
    def best_discount(self) -> float | None:
        vals = [c.verdict.discount_pct for c in self.products + self.extra_candidates
                if c.verdict and c.verdict.discount_pct is not None]
        return max(vals) if vals else None


# ----------------------------------------------------------------------------- helpers
def parse_store(html: str) -> dict | None:
    i = html.find(STORE_MARKER)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, j)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def check_truncated(html: str, kind: str) -> str | None:
    """Incomplete download (2.2.1). Returns a reason or None."""
    if not html or "</html>" not in html[-4096:].lower():
        return "no closing </html>"
    if kind == "product" and STORE_MARKER not in html:
        return "no window.__STORE__ on product page"
    if kind == "product" and parse_store(html) is None:
        return "window.__STORE__ unreadable"
    return None


def _txt(node) -> str | None:
    if node is None:
        return None
    t = node.text(strip=True)
    return t or None


def _tags_hint(tags: str | None, badge_name: str | None = None) -> str | None:
    parts = []
    if badge_name:
        parts.append(badge_name)
    if tags:
        known = [t for t in tags.split("|") if t in ("BLF", "JBF", "CFS") or t.startswith("BLF")]
        if known:
            parts.append("|".join(sorted(set(known))))
    return "; ".join(parts) or None


def _candidate_from_json(p: dict, source: str) -> Candidate:
    prices = p.get("prices") or {}
    price = parse_naira(prices.get("rawPrice")) if prices.get("rawPrice") else parse_naira(prices.get("price"))
    old = parse_naira(prices.get("oldPrice"))
    display = p.get("displayPrices") or {}
    price_text = str(prices.get("price") or display.get("price") or "")
    rating = p.get("rating") or {}
    badge = ((p.get("badges") or {}).get("campaign") or {}).get("name")
    stock = None
    for s in p.get("simples") or []:
        st = (s.get("stockInfo") or {}).get("text")
        if st:
            stock = st
            break
    return Candidate(
        sku=str(p.get("sku")),
        title=p.get("displayName") or p.get("name"),
        price=price,
        old_price=old,
        url=normalize(p.get("url") or ""),
        source=source,
        cross_checks=[parse_percent(prices.get("discount")), parse_percent(display.get("discount"))],
        price_from=price_text.lower().startswith("from"),
        campaign_hint=_tags_hint(p.get("tags"), badge),
        category=" > ".join(str(x) for x in (p.get("categories") or []) if x) or None,
        rating_avg=rating.get("average"),
        rating_count=rating.get("totalRatings"),
        stock_text=stock,
    )


def _is_sponsored_card(article, anchor) -> bool:
    a = anchor.attributes
    if a.get("data-gtm-dimension44") == "1":
        return True
    if "data-gtm-sponadinfo" in a or any(k.startswith("data-mirakl") for k in a):
        return True
    for node in (article, anchor):
        cls = (node.attributes.get("class") or "").split()
        if "_spn" in cls:
            return True
        if any(k.startswith("data-mirakl") or k == "data-gtm-sponadinfo" for k in node.attributes):
            return True
    # "Sponsored" heading on the enclosing section
    sec = article.parent
    while sec is not None and sec.tag not in ("section", "body"):
        sec = sec.parent
    if sec is not None and sec.tag == "section":
        h = sec.css_first("h2, h3")
        if h is not None and "sponsored" in (h.text() or "").lower():
            return True
    return False


def parse_cards(tree: LexborHTMLParser | object, root=None) -> list[Candidate]:
    """Organic listing/carousel cards (5.1): .prc, data-oprc or .old, .bdg._dsct, data-gtm-id, a.core."""
    scope = root if root is not None else tree
    out: list[Candidate] = []
    for art in scope.css("article.prd"):
        a = art.css_first("a.core")
        if a is None:
            continue
        if _is_sponsored_card(art, a):
            continue
        sku = a.attributes.get("data-gtm-id") or a.attributes.get("data-ga4-item_id")
        if not sku:
            continue
        prc = a.css_first(".prc")
        price_text = _txt(prc) or ""
        old_text = (prc.attributes.get("data-oprc") if prc is not None else None) or _txt(a.css_first(".old"))
        badge = a.css_first(".bdg._dsct")
        badge_val = parse_percent(badge.attributes.get("data-disc") or _txt(badge)) if badge is not None else None
        tags = a.attributes.get("data-ga4-tags") or a.attributes.get("data-gtm-dimension43")
        camp = art.css_first(".bdg._camp, .camp")
        out.append(Candidate(
            sku=sku,
            title=_txt(a.css_first(".name")) or a.attributes.get("data-gtm-name"),
            price=parse_naira(price_text),
            old_price=parse_naira(old_text),
            url=normalize(a.attributes.get("href") or ""),
            source="card",
            cross_checks=[badge_val],
            price_from=price_text.lower().startswith("from"),
            campaign_hint=_tags_hint(tags, _txt(camp)),
            category=" > ".join(x.strip() for x in (a.attributes.get("data-gtm-category") or "").split("/")
                                if x.strip()) or None,
            rating_avg=parse_naira(_txt(a.css_first(".rv-avg"))),
            rating_count=int(parse_naira(_txt(a.css_first(".rv-cnt"))) or 0) or None,
        ))
    return out


def strip_boilerplate(tree: LexborHTMLParser) -> None:
    """F14 / 5.3.1: drop site-wide header, nav, footer, top banners, delivery blocks, scripts."""
    # Raw Jumia skeleton: #jm > aside.banner, nav, header, main, footer, aside.banner-pop, div#pop.
    # Only <main> is page content; everything around it repeats on every page.
    for sel in ("#jm > aside", "#jm > nav", "#jm > header", "#jm > footer", "#jm > #pop",
                "body > header", "body > footer", "script", "style", "noscript"):
        for n in tree.css(sel):
            n.decompose()
    # the delivery countdown block ("order within the next 18hrs") is not promo urgency
    for n in tree.css("div, p"):
        t = n.text(deep=False) or ""
        if DELIVERY_RE.search(t):
            n.decompose()


def _campaign_hints(tree: LexborHTMLParser) -> list[str]:
    hints: set[str] = set()
    for n in tree.css("[data-ga4-promotion_name]"):
        v = n.attributes.get("data-ga4-promotion_name")
        if v:
            hints.add(v.strip())
    for img in tree.css("img[alt], img[data-src], img[src]"):
        for attr in ("data-src", "src"):
            m = CAMPAIGN_IMG_RE.search(img.attributes.get(attr) or "")
            if m:
                hints.add(m.group(1))
    for n in tree.css("[data-end]"):
        hints.add(f"ends:{n.attributes.get('data-end')}")
    return sorted(hints)


def _links(tree: LexborHTMLParser, base_url: str) -> list[Link]:
    out: dict[str, Link] = {}
    for a in tree.css("a[href]"):
        u = normalize(a.attributes.get("href") or "", base_url)
        if not u or u in out:
            continue
        anchor = " ".join(filter(None, [a.text(strip=True), a.attributes.get("title"),
                                        a.attributes.get("data-ga4-promotion_name")]))[:200]
        out[u] = Link(u, anchor, bool(CAMPAIGN_TEXT_RE.search(anchor)))
    return list(out.values())


SELLER_SCORE_RE = re.compile(r"<bdo[^>]*>\s*(\d{1,3})%\s*</bdo>\s*Seller Score", re.I)
QUALITY_RE = re.compile(r"Quality Score:\s*<span[^>]*>\s*([A-Za-z ]+?)\s*</span>", re.I)
GB_TITLE_RE = re.compile(r"(\d+)\s*GB", re.I)
RAM_RE = re.compile(r"(?:RAM\D{0,12}(\d+)\s*GB|(\d+)\s*GB\s*RAM)", re.I)
ROM_RE = re.compile(r"(?:(?:ROM|storage|internal memory)\D{0,12}(\d+)\s*GB|(\d+)\s*GB\s*(?:ROM|storage))", re.I)


def _gb_pair(text: str) -> tuple[set[str], set[str]]:
    ram = {a or b for a, b in RAM_RE.findall(text)}
    rom = {a or b for a, b in ROM_RE.findall(text)}
    return ram, rom


def listing_mismatch(title: str | None, details: str | None) -> bool:
    """Flag only when GB figures disagree between title and product details (5.4)."""
    if not title or not details:
        return False
    t_ram, t_rom = _gb_pair(title)
    d_ram, d_rom = _gb_pair(details)
    if t_ram and d_ram and not (t_ram & d_ram):
        return True
    if t_rom and d_rom and not (t_rom & d_rom):
        return True
    return False


def _old_price_inconsistent(main: Candidate, others: list[Candidate]) -> bool:
    """Same price, very different old price on a variant or sibling card (5.4)."""
    if main.price is None or main.old_price is None:
        return False
    for o in others:
        if o.sku == main.sku or o.price != main.price or o.old_price is None:
            continue
        if abs(o.old_price - main.old_price) / main.old_price > 0.15:
            return True
    return False


def _jsonld_price(tree: LexborHTMLParser) -> float | None:
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.text() or "")
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            offers = d.get("offers") if isinstance(d, dict) else None
            if isinstance(offers, dict) and offers.get("price") is not None:
                return parse_naira(offers.get("price"))
    return None


# ----------------------------------------------------------------------------- entry
def extract(html: str, url: str) -> PageResult:
    kind = page_type(url)
    res = PageResult(url=url, kind=kind)
    reason = check_truncated(html, kind)
    if reason:
        res.truncated, res.truncated_reason = True, reason
        return res  # never scored as "no discount"

    store = parse_store(html)
    tree = LexborHTMLParser(html)
    t = tree.css_first("title")
    res.title = _txt(t)
    jsonld = _jsonld_price(tree) if kind == "product" else None  # read before scripts are stripped
    strip_boilerplate(tree)
    res.campaign_hints = _campaign_hints(tree)
    res.links = _links(tree, url)
    cards = parse_cards(tree)

    if kind == "product":
        res.products, res.extra_candidates = _extract_product(url, html, tree, store, cards, jsonld)
    else:
        res.products = _extract_listing(store, cards)

    for c in res.products + res.extra_candidates:
        c.score()
    return res


def _extract_listing(store: dict | None, cards: list[Candidate]) -> list[Candidate]:
    by_sku: dict[str, Candidate] = {}
    # JSON first: top-level products is the page's own grid; skip sponsored entries
    for p in (store or {}).get("products") or []:
        if not isinstance(p, dict) or p.get("isSponsored") or not p.get("sku"):
            continue
        by_sku[str(p["sku"])] = _candidate_from_json(p, "store")
    # HTML cards fill in anything JSON lacked, and add the badge as a cross-check
    for c in cards:
        j = by_sku.get(c.sku)
        if j is None:
            by_sku[c.sku] = c
            continue
        j.cross_checks.extend(c.cross_checks)
        if not j.category and c.category:
            j.category = c.category
        if j.old_price is None and c.old_price is not None:
            j.old_price = c.old_price
        if j.price is None:
            j.price = c.price
    return list(by_sku.values())


def _extract_product(url, html, tree, store, cards, jsonld_price):
    store = store or {}
    page_sku = store.get("sku")
    main_json = None
    for p in store.get("products") or []:
        if isinstance(p, dict) and (p.get("sku") == page_sku or page_sku is None):
            main_json = p
            break
    products: list[Candidate] = []
    if main_json is not None:
        base = _candidate_from_json(main_json, "product")
        # HTML price block badge as an extra cross-check
        badge = tree.css_first("[data-disc]")
        if badge is not None:
            base.cross_checks.append(parse_percent(badge.attributes.get("data-disc")))
        m = SELLER_SCORE_RE.search(html)
        base.seller_score = int(m.group(1)) if m else None
        q = QUALITY_RE.search(html)
        base.seller_quality = q.group(1).strip() if q else None
        details = " ".join(
            n.text(separator=" ", strip=True) for n in tree.css("#product-details, .-mhm.-pvl, .markup, article.card ul")
        )
        if listing_mismatch(base.title, details):
            base.extra_flags.append("listing_mismatch")
        # variants: evaluate each buyable simple; the product passes if any one passes
        variants: list[Candidate] = []
        for s in main_json.get("simples") or []:
            if not s.get("isBuyable", True):
                continue
            sp = s.get("prices") or {}
            if not sp:
                continue
            v = Candidate(
                sku=base.sku, title=base.title,
                price=parse_naira(sp.get("rawPrice") or sp.get("price")),
                old_price=parse_naira(sp.get("oldPrice")),
                url=base.url, source="product",
                cross_checks=[parse_percent(sp.get("discount"))],
            ).score()
            variants.append(v)
        if variants:
            passing = [v for v in variants if v.verdict.approved]
            pool = passing or ([v for v in variants if v.verdict.discount_pct is not None]
                               if base.old_price is None else [])
            if pool:
                pick = max(pool, key=lambda v: v.verdict.discount_pct)
                base.price, base.old_price, base.cross_checks = pick.price, pick.old_price, pick.cross_checks
            olds = [v.old_price for v in variants if v.price == base.price and v.old_price]
            if olds and (max(olds) - min(olds)) / max(olds) > 0.15:
                base.extra_flags.append("old_price_inconsistent")
        organic = [c for c in cards if c.sku != base.sku]
        if _old_price_inconsistent(base, organic) and "old_price_inconsistent" not in base.extra_flags:
            base.extra_flags.append("old_price_inconsistent")
        products.append(base)
        extra = organic  # F19: organic "viewed together" cards present in raw HTML
    else:
        products = []
        sku = page_sku or f"url:{urlsplit(url).path}"
        # 5.1 second source: the product's own HTML price block (badge + struck-through price)
        price_el = tree.css_first("main span.-fs24, main .-b.-ubpt.-tal")
        old_el = tree.css_first("main span.-lthr")
        badge = tree.css_first("main [data-disc]")
        if price_el is not None:
            h1 = tree.css_first("main h1")
            c = Candidate(sku=sku, title=_txt(h1), price=parse_naira(_txt(price_el)),
                          old_price=parse_naira(_txt(old_el)), url=url, source="product",
                          cross_checks=[parse_percent(badge.attributes.get("data-disc")) if badge else None])
            c.price_from = (_txt(price_el) or "").lower().startswith("from")
            m = SELLER_SCORE_RE.search(html)
            c.seller_score = int(m.group(1)) if m else None
            q = QUALITY_RE.search(html)
            c.seller_quality = q.group(1).strip() if q else None
            products.append(c)
        elif jsonld_price is not None:
            # third source: JSON-LD has the price only, so no discount can be proven
            products.append(Candidate(sku=f"url:{urlsplit(url).path}", title=None, price=jsonld_price,
                                      old_price=None, url=url, source="jsonld"))
        extra = cards
    return products, extra

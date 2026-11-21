"""C5, C6, F3, F4 against Jumia's real robots.txt (saved 2026-11-21)."""
import pytest

from promoscout.robots import Robots
from promoscout.urls import normalize, page_type

B = "https://www.jumia.com.ng"


@pytest.fixture
def robots(fixture_text):
    return Robots(fixture_text("robots.txt"))


def test_three_sitemaps_listed(robots):  # C7
    assert robots.sitemaps == [
        "https://static.jumia.com.ng/index-sitemap.xml",
        "https://www.jumia.com.ng/slp/nc/sitemap/sitemap.xml",
        "https://www.jumia.com.ng/blog/wp-sitemap.xml",
    ]


@pytest.mark.parametrize("path", [
    "/tablets/", "/tablets/?page=2", "/flash-sales/", "/mlp-black-friday/",
    "/tilecc-10.1-inch-android-tablet-419065810.html",
    "/catalog/productspecifications/sku/X/", "/catalog/productratingsreviews/sku/X/",
    "/slp/nc/sitemap/sitemap.xml",
])
def test_allowed(robots, path):
    assert robots.allowed(B + path), path


@pytest.mark.parametrize("path", [
    "/a/b/c/",                          # /*/*/*/
    "/mlp-black-friday/phones/",        # subpaths under /mlp-*/
    "/mlp-black-friday/?page=2",        # /mlp-*/*page=
    "/sp-help/sub/",                    # subpaths under /sp-*/
    "/flash-sales/?flashsale=123",      # /flash-sales/*flashsale=
    "/electronics-clearance-sale/",
    "/catalog/", "/catalog/other/",     # /catalog/ except spec & review paths
    "/cart/", "/customer/account/login/",
    "/tablets/?q=ipad", "/catalog/?q=fan",
    "/tablets/?utm_source=x", "/tablets/?a=1&gclid=1", "/tablets/?fbclid=1",
    "/tablets/?sort=lowest-price", "/tablets/?price=100-200", "/tablets/?color=red",
    "/fragment/recommendations/viewed-together/X/",
    "/samsung--apple/",                 # *--*
    "/en/tablets/",
])
def test_disallowed(robots, path):
    assert not robots.allowed(B + path), path


def test_longest_match_allow_beats_disallow(robots):
    # Disallow: /catalog/ vs Allow: /catalog/productspecifications/ -> longer Allow wins
    assert robots.allowed(B + "/catalog/productspecifications/sku/ABC/")
    assert not robots.allowed(B + "/catalog/productsomethingelse/")


def test_normalize_strips_tracking_fragment_sorts_lowercases():
    assert normalize("HTTPS://WWW.Jumia.com.ng/tablets/?page=2&utm_source=x&a=1#catalog-listing") == \
        "https://www.jumia.com.ng/tablets/?a=1&page=2"
    assert normalize("/tablets/?gclid=abc") == "https://www.jumia.com.ng/tablets/"
    assert normalize("http://jumia.com.ng/tablets/") == "https://www.jumia.com.ng/tablets/"
    assert normalize("https://example.com/x") is None
    assert normalize("javascript:void(0)") is None


def test_page_types():
    assert page_type(B + "/tablets/") == "listing"
    assert page_type(B + "/tablets/?page=3") == "listing"
    assert page_type(B + "/mlp-black-friday/") == "campaign"
    assert page_type(B + "/flash-sales/") == "campaign"
    assert page_type(B + "/x-y-419065810.html") == "product"
    assert page_type("https://static.jumia.com.ng/index-sitemap.xml") == "sitemap"


def test_page_types_from_real_jumia_links(robots):
    # seen in the bot's own raw fetch of /tablets/ and /flash-sales/ (2026-11-21)
    assert page_type(B + "/tablets/apple/") == "listing" and robots.allowed(B + "/tablets/apple/")
    assert page_type(B + "/phones-tablets/flash-sales/") == "campaign"
    assert robots.allowed(B + "/phones-tablets/flash-sales/")
    assert page_type(B + "/tk17-pro-max-tablet-tk-mpg12085122.html") == "product"
    assert page_type(B + "/sp-help/") == "other"                     # help pages, not promos
    assert page_type(B + "/blog/") == "other"
    assert not robots.allowed(B + "/tablets/generic/")              # */generic/ stays disallowed
    assert not robots.allowed(B + "/mlp-black-friday/xiaomi/")       # /mlp-*/*/ stays disallowed
    assert not robots.allowed(B + "/flash-sales/fashion-brand/")

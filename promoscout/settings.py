"""Run settings (PRD 2.2). Every limit lives here; the RPM ceiling is clamped in code."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# C1 / C4: fixed bot identity. Deliberately NOT a setting so it can never be changed or rotated.
USER_AGENT = "PromoScoutBot/1.0 (contact: jamesadewara1@gmail.com)"

# C2: hard ceiling from Jumia's robots.txt.
RPM_CEILING = 200
RPM_FLOOR = 10  # adaptive slowdown never goes below this (2.2.1)

BASE_URL = "https://www.jumia.com.ng"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
ALLOWED_HOSTS = {"www.jumia.com.ng", "jumia.com.ng", "static.jumia.com.ng"}

# Discount gate (1.2)
APPROVE_GATE = 80
CHECK_PRICE_ABOVE = 99
PRUNE_MIN_DISCOUNT = 50  # F8: a section "has something" if a card is at 50%+
MISMATCH_POINTS = 2  # 5.1 cross-check tolerance


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROMOSCOUT_", env_file=".env", extra="ignore")

    max_rpm: int = 30
    max_concurrency: int = 3
    jitter_ms_min: int = 100
    jitter_ms_max: int = 400
    max_links_per_run: int = 300
    max_depth: int = 2
    max_outlinks_per_page: int = 10
    max_tries_per_url: int = 2
    max_total_errors: int = 10
    max_runs_per_day: int = 2
    max_runtime_minutes: float = 30
    block_lock_hours: float = 24
    connect_timeout_s: float = 15
    read_timeout_s: float = 45
    retry_backoff_s: float = 10
    max_retry_ratio: float = 0.10
    slow_factor: float = 3
    max_consecutive_network_errors: int = 5
    network_pause_s: float = 120
    max_network_pauses: int = 3
    recheck_listing_hours: float = 6
    recheck_product_below_hours: float = 24
    recheck_product_qualifying_hours: float = 6
    recheck_error_hours: float = 2
    recheck_rejected_days: float = 7
    prune_window: int = 10
    target_candidates: int = 30
    # Sitemaps count against max_links_per_run; this caps how many child sitemaps one run reads.
    max_sitemaps_per_run: int = 5
    # Promo seeds: known campaign pages checked every batch (6 h recheck; robots.txt still decides).
    # Missing pages answer 404 once and are rechecked after a day.
    promo_seed_paths: list[str] = [
        "/phones-tablets/flash-sales/", "/electronics/flash-sales/", "/computing/flash-sales/",
        "/phones-tablets/", "/smartphones/", "/electronics/", "/computing/",
        "/flash-sales/", "/mlp-black-friday/", "/mlp-clearance/", "/mlp-clearance-sale/",
        "/mlp-anniversary/", "/mlp-treasure-hunt/", "/mlp-jumia-black-friday-snatch-it-challenge/",
        "/mlp-11-11/", "/mlp-12-12/",
    ]
    # Focus: URLs whose path names one of these get +focus_bonus priority (crawled first, others later)
    focus_keywords: list[str] = [
        "phones-tablets", "smartphones", "mobile-phones", "phones", "tablets", "ipads", "computing",
        "laptops", "computers", "desktops", "monitors", "printers", "electronics", "televisions", "tvs",
        "smart-tvs", "home-audio", "audio", "speakers", "headphones", "earphones", "earbuds", "airpods",
        "smart-watches", "smartwatches", "wearable", "wearable-technology", "gaming", "playstation",
        "xbox", "nintendo", "consoles", "cameras", "camera", "drones", "power-banks", "power-bank",
        "chargers", "phone-accessories", "computer-accessories", "routers", "mifi", "networking",
        "external-hard-drives", "flash-drives",
        "memory-cards", "smart-home", "projectors", "iphone", "apple", "samsung", "xiaomi", "redmi",
        "tecno", "infinix", "itel", "oraimo", "hp", "lenovo", "dell", "asus", "hisense", "lg", "sony",
        "jbl", "anker", "nokia", "oppo", "vivo", "realme", "gadgets",
    ]
    focus_bonus: float = 40
    # extra on top of focus_bonus for phones (the main target)
    # phone accessories first: measured 80%+ finds come from accessories, almost never from handsets
    phone_keywords: list[str] = [
        "mobile-accessories", "phone-accessories", "cases", "phone-cases", "screen-protectors",
        "chargers", "cables", "power-banks", "power-bank", "powerbank", "earbuds", "earphones",
        "headphones", "headsets", "bluetooth", "smart-watches", "smartwatch", "phone-holders",
        "selfie", "ring-light", "memory-cards", "adapters", "phones-tablets", "smartphones",
        "mobile-phones", "iphone", "samsung", "xiaomi", "redmi", "tecno", "infinix", "itel", "oraimo",
    ]
    phone_bonus: float = 20
    # Learning (PRD 6.7): each family of pages (e.g. mlp-clearance, mlp-samsung) gets
    # +learn_bonus_per_find x (finds per page), max learn_max_bonus. A family with
    # learn_min_pages_for_penalty+ pages and under learn_poor_yield finds per page gets -learn_penalty
    # and loses the gadget/phone focus bonus (measured: handset landing pages ~0 finds per page).
    learn_bonus_per_find: float = 20
    learn_max_bonus: float = 40
    learn_penalty: float = 25
    learn_min_pages_for_penalty: int = 6
    learn_poor_yield: float = 0.1
    learn_window_days: float = 1  # only the last N days count, so an event page is judged on recent results
    # Look-alike pages (same family, e.g. /mlp-samsung-*/): at most this many per batch unless the family
    # already gave an 80%+ find. Stops dozens of near-identical landing pages from eating the batch.
    family_cap_per_run: int = 6
    # Review screen "Gadgets only": categories (top level or any level) that count as gadgets
    gadget_categories: list[str] = [
        "Phones & Tablets", "Electronics", "Computing", "Gaming", "Mobile Phones", "Tablets",
        "Mobile Accessories", "Laptops", "Televisions", "Home Audio", "Headphones", "Cameras",
        "Smart Watches", "Wearable Technology", "Video Games", "Computer Accessories", "Networking",
        "Data Storage", "Printers", "Power Banks",
    ]
    # Review queue: auto-hide unreviewed finds below review_keep_min_discount after
    # review_hide_after_hours, and any find not seen for review_stale_hours.
    # Hidden finds are not re-verified (no extra requests) and return only if their price changes.
    review_hide_after_hours: float = 48
    review_keep_min_discount: float = 90
    review_stale_hours: float = 72
    # Fake-discount check: flag old_price_inflated when the old price is more than this many times
    # the median selling price of similar items on the same page (in memory, no extra requests)
    inflation_factor: float = 3.0
    # Problem emails (block, Jumia unreachable, auto stopped): at most one per kind per N hours
    problem_email_every_hours: float = 6
    # Hot-find alert: a qualifying product at or above this discount is highlighted
    alert_min_discount: float = 95
    # Email (Gmail SMTP with an app password). Off unless enabled and user/password are set.
    email_enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False)
    email_to: str = ""       # comma-separated list; default: smtp_user
    email_from: str = ""     # default: smtp_user
    email_trigger: str = "hot"   # hot = new finds >= alert_min_discount; new = any new 80%+ find
    email_top_n: int = 10
    review_url: str = "http://localhost:8501"
    # Review page password. Required when the page is reachable on a public IP (docker-compose
    # publishes port 8501). Empty = no password (only safe on localhost / SSH tunnel).
    review_password: str = Field(default="", repr=False)
    # Price filter (operator rule): a product is sourced only if price < max_price_ngn
    # OR old price < max_old_price_ngn. Applied on top of the 80% discount rule.
    max_price_ngn: float = 20_000
    max_old_price_ngn: float = 1_000_000
    # auto mode: rest after each batch = batch duration x auto_rest_factor, kept between the
    # min and max below; the max is used after any server pushback or network trouble.
    auto_rest_factor: float = 1.0
    auto_min_cooldown_minutes: float = 3
    auto_max_cooldown_minutes: float = 60
    auto_retry_minutes: float = 2  # after a batch that failed without doing work (doubles each time)
    auto_max_runs_per_day: int = 24
    # Event days: start each batch just after the full hour (treasure-hunt items appear on the hour)
    auto_align_to_hour: bool = False
    auto_align_minute: int = 1
    auto_max_batches: int = 0  # 0 = keep going until stopped

    data_dir: Path = Path("data")

    @field_validator("max_rpm")
    @classmethod
    def clamp_rpm(cls, v: int) -> int:  # C2: never above 200, never below the floor
        return max(RPM_FLOOR, min(int(v), RPM_CEILING))

    @field_validator("max_tries_per_url")
    @classmethod
    def clamp_tries(cls, v: int) -> int:  # "one retry at most"
        return max(1, min(int(v), 2))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "promoscout.sqlite3"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def alerts_path(self) -> Path:
        return self.data_dir / "alerts.log"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "requests.log"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

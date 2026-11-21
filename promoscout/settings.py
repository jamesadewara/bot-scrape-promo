"""Run settings (PRD 2.2). Every limit lives here; the RPM ceiling is clamped in code."""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
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
    def log_path(self) -> Path:
        return self.data_dir / "requests.log"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

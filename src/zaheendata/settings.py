"""Central configuration. Every value can be overridden with an environment variable
prefixed ``ZAHEEN_`` (e.g. ``ZAHEEN_DATABASE_URL``) or placed in a ``.env`` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZAHEEN_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- runtime -----------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = False
    config_dir: Path = DEFAULT_CONFIG_DIR
    data_dir: Path = PROJECT_ROOT / "data"

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/zaheen.db"
    http_cache_path: Path = PROJECT_ROOT / "data" / "http_cache.sqlite"
    http_cache_ttl_seconds: int = 7 * 24 * 3600
    redis_url: str | None = None

    # --- crawling / politeness ---------------------------------------------
    user_agent: str = "ZaheendataBot/0.1 (+https://example.com/bot; business-directory research)"
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 4
    global_concurrency: int = 16
    per_host_concurrency: int = 2
    per_host_requests_per_second: float = 1.0
    google_places_requests_per_second: float = Field(
        default=0.35,
        description="Places API (New) QPS. Default stays under Google's 60 QPM starter quota.",
    )
    defer_google_places: bool = Field(
        default=True,
        description="Do not call Places during collect/enrich. Crawl websites first, shortlist, then Maps.",
    )
    google_places_shortlist_multiplier: int = Field(
        default=2,
        ge=1,
        description="Maps lookups on the shortlist pool: short_list_target × this (200 for a 100-lead list).",
    )
    website_discover_limit: int = Field(
        default=800,
        ge=0,
        description="Max free website/address lookups (DuckDuckGo + Nominatim + SAM/Yelp) per run.",
    )
    respect_robots_txt: bool = True
    website_max_pages: int = 6
    website_max_bytes: int = 2_000_000

    # --- API keys (all optional; a source is disabled when its key is missing) --
    # No paid list vendors: long lists come from government registries (SAM.gov, SBA PPP,
    # state tax-permit / licence rolls on Socrata), Google Maps and public directories.
    google_maps_api_key: SecretStr | None = None
    yelp_api_key: SecretStr | None = None
    sam_gov_api_key: SecretStr | None = Field(
        default=None, description="Free key from api.data.gov; unlocks SAM.gov entity search."
    )
    census_api_key: SecretStr | None = Field(
        default=None, description="Free key from api.census.gov; required for `zaheen plan` (CBP counts)."
    )
    openai_api_key: SecretStr | None = None

    # --- bulk public datasets -----------------------------------------------
    ppp_data_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "ppp",
        description="Folder holding SBA PPP FOIA CSVs (public_150k_plus_*.csv, public_up_to_150k_*.csv).",
    )
    public_dataset_refresh_days: int = 7

    # --- source feature flags ---------------------------------------------
    enable_google_maps_scraper: bool = False  # Playwright based; see docs/DATA_SOURCES.md for ToS notes
    enable_overpass: bool = True
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_fallback_urls: list[str] = Field(
        default_factory=lambda: [
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ]
    )
    nominatim_url: str = "https://nominatim.openstreetmap.org"

    # --- pipeline defaults (from the method in the brief) -------------------
    long_list_target: int = Field(
        default=400, description="Businesses to collect before filtering (300-500)."
    )
    short_list_target: int = Field(default=100, description="Businesses to keep after scoring.")
    min_google_reviews: int = 10
    busy_reviews_threshold: int = 50
    recent_review_window_days: int = 90
    recent_reviews_min: int = 3
    employee_min: int = 5
    employee_max: int = 19
    enforce_employee_range: bool = False  # vendor data has it, Maps does not; opt-in hard filter

    # --- API server -------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: SecretStr | None = None

    def secret(self, name: str) -> str | None:
        value = getattr(self, name)
        return value.get_secret_value() if value is not None else None


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings

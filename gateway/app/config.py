"""Every knob is env-driven, so local -> VPS is a config change and not a rewrite."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- upstream SearXNG -------------------------------------------------
    searxng_base_url: str = "http://127.0.0.1:8888"
    upstream_timeout: float = 15.0
    upstream_concurrency: int = 3
    upstream_time_budget: float = 12.0

    # A SearXNG page holds a variable number of results (25-35 observed), so 3
    # pages is roughly 60-90: comfortably past Google result page 5, which is
    # where essentially all real client traffic lives. Every extra page here is
    # another real scrape of Google/DDG/Brave, so raising it raises how hard we
    # hit them -- and how fast this host's IP gets captcha'd.
    max_upstream_pages: int = 3
    max_results: int = 100  # Google's own ceiling: start + num - 1 <= 100

    # --- cache ------------------------------------------------------------
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://127.0.0.1:6379/0"
    cache_ttl_fresh: int = 600  # 10m: serve straight from cache
    cache_ttl_max: int = 21600  # 6h: hard expiry, but usable as stale-on-error
    cache_ttl_fresh_recent: int = 120  # time_range=day / news: much shorter
    cache_max_entries: int = 5000  # memory backend only

    # --- circuit breaker --------------------------------------------------
    breaker_fail_threshold: int = 5
    breaker_reset_seconds: float = 30.0

    # --- auth -------------------------------------------------------------
    # Unset DATABASE_URL to run in dev mode: DEV_API_KEY is the only valid key
    # and no quota/usage tracking happens. Never do this on a public host.
    database_url: str | None = None
    dev_api_key: str | None = None
    key_cache_ttl: float = 30.0  # revocation lands within this window
    key_negative_cache_ttl: float = 10.0  # blunts key-scanning bots
    admin_token: str | None = None  # guards /admin/*

    rate_limit_backend: Literal["memory", "redis"] = "memory"
    usage_queue_size: int = 10_000
    usage_flush_seconds: float = 1.0
    usage_flush_rows: int = 500
    log_raw_queries: bool = False  # storing customers' search text is a liability

    # --- google compat ----------------------------------------------------
    # "lower_bound" is honest: totalResults == what we actually hold.
    # "inflated" fabricates a Google-looking number for clients that demand one.
    total_results_mode: Literal["lower_bound", "inflated"] = "lower_bound"
    strict_params: bool = False  # True -> 400 on unsupported CSE params
    site_search_post_filter: bool = True
    public_base_url: str = "http://127.0.0.1:8080"

    # --- server -----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8080
    profiles_path: str = "profiles.yml"


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Configuration loading from .env and environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv


def _parse_bool(val: str | None, default: bool = True) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_config(env_file: str | Path | None = None) -> dict[str, Any]:
    """Load configuration from .env file and environment variables."""
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if env_file is not None and str(env_file).strip() != "":
        custom_env = Path(env_file).expanduser()
        if not custom_env.is_file():
            raise FileNotFoundError(f"--env-file: '{env_file}' is not an existing file")
        load_dotenv(custom_env, override=True)
    elif env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    flaresolverr_url = os.getenv("FLARESOLVERR_URL", "http://localhost:8193").rstrip("/")
    flaresolverr_max_timeout = int(os.getenv("FLARESOLVERR_MAX_TIMEOUT", "60000"))
    proxy_file = os.getenv("PROXY_FILE", str(project_root / "proxies.txt"))
    output_dir = os.getenv("OUTPUT_DIR", str(project_root / "artifacts"))
    concurrency = int(os.getenv("CONCURRENCY", "4"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    max_pages = int(os.getenv("MAX_PAGES", "1000"))
    max_depth = int(os.getenv("MAX_DEPTH", "5"))
    chunk_size = int(os.getenv("CHUNK_SIZE", "15"))
    log_level = os.getenv("LOG_LEVEL", "INFO")

    # Mode & feature toggles
    enable_competitor = _parse_bool(os.getenv("ENABLE_COMPETITOR"), default=True)
    use_sitemap = _parse_bool(os.getenv("USE_SITEMAP"), default=True)
    fast_http_first = _parse_bool(os.getenv("FAST_HTTP_FIRST"), default=True)
    preserve_trailing_slash = _parse_bool(os.getenv("PRESERVE_TRAILING_SLASH"), default=True)
    verify_all_links = _parse_bool(os.getenv("VERIFY_ALL_LINKS"), default=False)

    # Queue & storage settings (SQLite is primary persistence)
    redis_url = os.getenv("REDIS_URL", "").strip()
    sqlite_db_name = os.getenv("SQLITE_DB_NAME", "crawl_results.db").strip()

    # Export & archive settings
    export_format = os.getenv("EXPORT_FORMAT", "json").strip().lower()
    if export_format == "excel":
        export_format = "xlsx"
    export_mode = os.getenv("EXPORT_MODE", "single").strip().lower()
    export_chunk_size = int(os.getenv("EXPORT_CHUNK_SIZE", "250"))
    archive_format = os.getenv("ARCHIVE_FORMAT", "none").strip().lower()
    tar_compression = os.getenv("TAR_COMPRESSION", "gz").strip().lower()

    # General SEO Audit Thresholds
    title_min_len = int(os.getenv("SEO_TITLE_MIN_LEN", "30"))
    title_max_len = int(os.getenv("SEO_TITLE_MAX_LEN", "60"))
    desc_min_len = int(os.getenv("SEO_DESC_MIN_LEN", "120"))
    desc_max_len = int(os.getenv("SEO_DESC_MAX_LEN", "160"))
    min_word_count = int(os.getenv("SEO_MIN_WORD_COUNT", "300"))
    dup_threshold = float(os.getenv("SEO_DUP_THRESHOLD", "0.85"))
    slow_warn_ms = int(os.getenv("SEO_SLOW_WARN_MS", "1000"))
    slow_err_ms = int(os.getenv("SEO_SLOW_ERR_MS", "3000"))
    large_warn_bytes = int(os.getenv("SEO_LARGE_WARN_BYTES", "1048576"))
    large_err_bytes = int(os.getenv("SEO_LARGE_ERR_BYTES", "3145728"))

    # Post-crawl classification & performance
    hub_min_links = int(os.getenv("HUB_MIN_LINKS", "10"))
    locations_file = os.getenv("LOCATIONS_FILE", "").strip()
    corporate_share = float(os.getenv("CORPORATE_SHARE", "0.40"))
    timing_sample = int(os.getenv("TIMING_SAMPLE", "0"))
    psi_api_key = os.getenv("PSI_API_KEY", "").strip()
    psi_sample = int(os.getenv("PSI_SAMPLE", "0"))

    # Google API Integration (multi-profile)
    enable_google_enrichment = _parse_bool(os.getenv("ENABLE_GOOGLE_ENRICHMENT"), default=False)
    google_auth_profile = os.getenv("GOOGLE_AUTH_PROFILE", "").strip()
    google_profiles_dir = os.getenv("GOOGLE_PROFILES_DIR", "").strip()
    google_ga4_property_id = os.getenv("GOOGLE_GA4_PROPERTY_ID", "").strip()
    google_gsc_site_url = os.getenv("GOOGLE_GSC_SITE_URL", "").strip()
    google_gbp_account_id = os.getenv("GOOGLE_GBP_ACCOUNT_ID", "").strip()
    google_gbp_location_ids = tuple(
        x.strip() for x in os.getenv("GOOGLE_GBP_LOCATION_IDS", "").split(",") if x.strip()
    )
    lookback_raw = os.getenv("GOOGLE_LOOKBACK_DAYS", "").strip()
    google_lookback_days = int(lookback_raw) if lookback_raw else 28

    return {
        "project_root": str(project_root),
        "flaresolverr_url": flaresolverr_url,
        "flaresolverr_max_timeout": flaresolverr_max_timeout,
        "proxy_file": proxy_file,
        "output_dir": output_dir,
        "concurrency": concurrency,
        "max_retries": max_retries,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "chunk_size": chunk_size,
        "log_level": log_level,
        "enable_competitor": enable_competitor,
        "use_sitemap": use_sitemap,
        "fast_http_first": fast_http_first,
        "preserve_trailing_slash": preserve_trailing_slash,
        "verify_all_links": verify_all_links,
        "redis_url": redis_url,
        "sqlite_db_name": sqlite_db_name,
        "export_format": export_format,
        "export_mode": export_mode,
        "export_chunk_size": export_chunk_size,
        "archive_format": archive_format,
        "tar_compression": tar_compression,
        # SEO Audit thresholds
        "title_min_len": title_min_len,
        "title_max_len": title_max_len,
        "desc_min_len": desc_min_len,
        "desc_max_len": desc_max_len,
        "min_word_count": min_word_count,
        "dup_threshold": dup_threshold,
        "slow_warn_ms": slow_warn_ms,
        "slow_err_ms": slow_err_ms,
        "large_warn_bytes": large_warn_bytes,
        "large_err_bytes": large_err_bytes,
        "hub_min_links": hub_min_links,
        "locations_file": locations_file,
        "corporate_share": corporate_share,
        "timing_sample": timing_sample,
        "psi_api_key": psi_api_key,
        "psi_sample": psi_sample,
        "enable_google_enrichment": enable_google_enrichment,
        "google_auth_profile": google_auth_profile,
        "google_profiles_dir": google_profiles_dir,
        "google_ga4_property_id": google_ga4_property_id,
        "google_gsc_site_url": google_gsc_site_url,
        "google_gbp_account_id": google_gbp_account_id,
        "google_gbp_location_ids": google_gbp_location_ids,
        "google_lookback_days": google_lookback_days,
    }


def load_proxies(proxy_file: str) -> list[str]:
    """Load proxy URLs from a file (one per line, blank lines and comments ignored)."""
    path = Path(proxy_file)
    if not path.is_absolute():
        project_root = Path(__file__).resolve().parent.parent
        path = project_root / proxy_file

    if not path.exists():
        return []

    proxies: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            proxies.append(line)
    return proxies


def extract_proxy_port(proxy_url: str | None) -> int | None:
    """Extract integer port from a proxy URL (e.g. http://user:pass@host:10002 -> 10002)."""
    if not proxy_url:
        return None
    try:
        parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        return parsed.port
    except Exception:
        return None

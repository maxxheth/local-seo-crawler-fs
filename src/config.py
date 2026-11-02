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


def load_config() -> dict[str, Any]:
    """Load configuration from .env file and environment variables."""
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
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

    # Queue & storage settings
    redis_url = os.getenv("REDIS_URL", "").strip()
    sqlite_db_name = os.getenv("SQLITE_DB_NAME", "crawl_results.db").strip()

    # Export & archive settings
    export_format = os.getenv("EXPORT_FORMAT", "json").strip().lower()
    export_mode = os.getenv("EXPORT_MODE", "single").strip().lower()
    export_chunk_size = int(os.getenv("EXPORT_CHUNK_SIZE", "250"))
    archive_format = os.getenv("ARCHIVE_FORMAT", "none").strip().lower()
    tar_compression = os.getenv("TAR_COMPRESSION", "gz").strip().lower()

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
        "redis_url": redis_url,
        "sqlite_db_name": sqlite_db_name,
        "export_format": export_format,
        "export_mode": export_mode,
        "export_chunk_size": export_chunk_size,
        "archive_format": archive_format,
        "tar_compression": tar_compression,
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
    """Extract integer port from a proxy URL (e.g. http://u:p@host:10002 -> 10002)."""
    if not proxy_url:
        return None
    try:
        parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        return parsed.port
    except Exception:
        return None

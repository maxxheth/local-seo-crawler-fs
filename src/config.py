"""Configuration loading from .env and environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv


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
    log_level = os.getenv("LOG_LEVEL", "INFO")

    return {
        "project_root": str(project_root),
        "flaresolverr_url": flaresolverr_url,
        "flaresolverr_max_timeout": flaresolverr_max_timeout,
        "proxy_file": proxy_file,
        "output_dir": output_dir,
        "concurrency": concurrency,
        "max_retries": max_retries,
        "log_level": log_level,
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
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        return parsed.port
    except Exception:
        return None

"""FlareSolverr /v1 HTTP client for Cloudflare challenge bypass and JS rendering."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


@dataclass
class FlareSolverrResponse:
    """Parsed response from FlareSolverr."""
    session_id: str | None = None
    status: int = 0
    html: str = ""
    final_url: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    solution: dict[str, Any] | None = None


def proxy_url_to_flaresolverr(proxy_url: str) -> dict[str, str]:
    """Convert proxy URL (http://user:pass@host:port) to FlareSolverr dict format."""
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port
    host_port = f"{host}:{port}" if port else host

    payload: dict[str, str] = {
        "url": f"{scheme}://{host_port}",
    }
    if parsed.username:
        payload["username"] = parsed.username
    if parsed.password:
        payload["password"] = parsed.password
    return payload


class FlareSolverrClient:
    """Client for FlareSolverr server."""

    def __init__(
        self,
        base_url: str = "http://localhost:8193",
        max_timeout: int = 60000,
        http_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_timeout = max_timeout
        self.http_timeout = http_timeout
        self._http = httpx.Client(timeout=httpx.Timeout(http_timeout))

    def create_session(self, proxy_url: str | None = None) -> str:
        """Create a new FlareSolverr session pinned to an optional proxy."""
        payload: dict[str, Any] = {
            "cmd": "sessions.create",
        }
        if proxy_url:
            payload["proxy"] = proxy_url_to_flaresolverr(proxy_url)

        resp = self._http.post(f"{self.base_url}/v1", json=payload)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "ok":
            msg = data.get("message", str(data))
            raise RuntimeError(f"FlareSolverr session creation failed: {msg}")

        session_id: str = data["session"]
        logger.info("Created FlareSolverr session %s (proxy=%s)", session_id, proxy_url)
        return session_id

    def destroy_session(self, session_id: str) -> bool:
        """Destroy an existing FlareSolverr session."""
        try:
            resp = self._http.post(
                f"{self.base_url}/v1",
                json={
                    "cmd": "sessions.destroy",
                    "session": session_id,
                },
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Failed to destroy FlareSolverr session %s: %s", session_id, exc)
            return False

    def request(
        self,
        url: str,
        session_id: str | None = None,
        proxy_url: str | None = None,
        max_timeout: int | None = None,
        return_raw_html: bool = False,
    ) -> FlareSolverrResponse:
        """Dispatch a page render request to FlareSolverr."""
        timeout_val = max_timeout or self.max_timeout
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": timeout_val,
        }
        if session_id:
            payload["session"] = session_id
        elif proxy_url:
            payload["proxy"] = proxy_url_to_flaresolverr(proxy_url)

        if return_raw_html:
            payload["returnRawHtml"] = True

        try:
            resp = self._http.post(f"{self.base_url}/v1", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("HTTP error connecting to FlareSolverr: %s", exc)
            return FlareSolverrResponse(
                session_id=session_id,
                status=500,
                error=f"FlareSolverr HTTP transport error: {exc}",
            )

        result = FlareSolverrResponse(session_id=session_id)
        if data.get("status") != "ok":
            result.error = data.get("message", "Unknown FlareSolverr error")
            result.status = 500
            return result

        solution = data.get("solution", {})
        result.solution = solution
        result.status = solution.get("status", 200)
        result.cookies = solution.get("cookies", [])
        result.html = solution.get("response", "")
        result.final_url = solution.get("url", url)
        return result

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

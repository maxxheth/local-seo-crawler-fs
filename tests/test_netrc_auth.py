"""Tests for the opt-in --netrc Basic Auth support."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from src.auth import load_netrc_auth
from src.sitemap import SitemapDiscovery


def _netrc(tmp_path: Path, extra: str = "") -> str:
    p = tmp_path / "site.netrc"
    p.write_text(f"machine staging.example.com login tester password s3cret\n{extra}", encoding="utf-8")
    return str(p)


def _echo_client(auth: httpx.Auth | None) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    return httpx.Client(transport=httpx.MockTransport(handler), auth=auth), seen


def test_empty_path_disables_auth() -> None:
    assert load_netrc_auth("") is None


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an existing file"):
        load_netrc_auth(str(tmp_path / "nope"))


def test_auth_sent_only_to_named_machine(tmp_path: Path) -> None:
    client, seen = _echo_client(load_netrc_auth(_netrc(tmp_path)))
    client.get("https://staging.example.com/")
    client.get("https://other.example.org/")
    expected = "Basic " + base64.b64encode(b"tester:s3cret").decode()
    assert seen[0].headers["Authorization"] == expected
    assert "Authorization" not in seen[1].headers


def test_default_entry_is_not_used_for_other_hosts(tmp_path: Path) -> None:
    path = _netrc(tmp_path, "default login anyone password leaky\n")
    client, seen = _echo_client(load_netrc_auth(path))
    client.get("https://other.example.org/")
    assert "Authorization" not in seen[0].headers


def test_sitemap_discovery_uses_auth(tmp_path: Path) -> None:
    client, seen = _echo_client(load_netrc_auth(_netrc(tmp_path)))
    SitemapDiscovery("staging.example.com", http_client=client).discover_urls()
    assert seen and all("Authorization" in r.headers for r in seen)

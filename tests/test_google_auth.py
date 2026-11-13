"""Unit tests for multi-profile Google auth manager, .env config wiring, and read-only signals client."""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.adapters.google_signals import GoogleSignalsClient, redact_secrets
from src.config import load_config
from src.google_auth import (
    CLOUD_PLATFORM_SCOPE,
    MARVENIO_PRESET,
    GoogleAuthProfile,
    ProfileStore,
    load_credentials,
    login_profile,
    logout_profile,
    main as google_auth_main,
    profile_status,
    resolve_profile_name,
    validate_client_file,
    verify_private_file,
)
from src.main import main as crawler_main


def _write_private_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def _marvenio_client_payload(
    *,
    project_id: str = "macro-anchor-513817-n3",
    client_id: str = "87201275884-82eip26b0mqa7q617ubu3hrt0o8si27o.apps.googleusercontent.com",
    client_secret: str = "fake-client-secret-value",
    top_key: str = "installed",
) -> dict[str, Any]:
    return {
        top_key: {
            "client_id": client_id,
            "project_id": project_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
        }
    }


def _make_profile(
    tmp_path: Path,
    *,
    name: str = "marvenio",
    client_file: Path,
    scopes: tuple[str, ...] | None = None,
    remote_revoke_allowed: bool = False,
) -> GoogleAuthProfile:
    return GoogleAuthProfile(
        name=name,
        auth_mode="oauth_desktop",
        client_secrets_file=str(client_file),
        token_file=str(tmp_path / "profiles" / name / "token.json"),
        scopes=scopes or MARVENIO_PRESET["scopes"],
        expected_project_id=MARVENIO_PRESET["expected_project_id"],
        expected_client_id=MARVENIO_PRESET["expected_client_id"],
        expected_email=MARVENIO_PRESET["expected_email"],
        expected_hd=MARVENIO_PRESET["expected_hd"],
        remote_revoke_allowed=remote_revoke_allowed,
    )


class _DummyCreds:
    def __init__(self, token: str = "ya29.fake-access-token", valid: bool = True) -> None:
        self.token = token
        self.valid = valid
        self.refresh_calls = 0

    def refresh(self, _request: Any) -> None:
        self.refresh_calls += 1
        self.valid = True
        self.token = "ya29.refreshed-token"


@pytest.fixture(autouse=True)
def _clean_google_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "GOOGLE_AUTH_PROFILE",
        "GOOGLE_PROFILES_DIR",
        "GOOGLE_GA4_PROPERTY_ID",
        "GOOGLE_GSC_SITE_URL",
        "GOOGLE_GBP_ACCOUNT_ID",
        "GOOGLE_GBP_LOCATION_IDS",
        "GOOGLE_LOOKBACK_DAYS",
    ):
        monkeypatch.delenv(key, raising=False)


# ── 1. Profile store, permissions & client validation ──────────────────────────


def test_profile_store_add_marvenio_and_default_multi_site(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "vault"
    marv_client = _write_private_json(tmp_path / "marv-client.json", _marvenio_client_payload())
    gh_client = _write_private_json(
        tmp_path / "gh-client.json",
        _marvenio_client_payload(project_id="edfx-seo", client_id="123-gh.apps.googleusercontent.com"),
    )

    rc = google_auth_main(
        [
            "--profiles-dir",
            str(profiles_dir),
            "profile",
            "add",
            "marvenio",
            "--client-file",
            str(marv_client),
            "--preset",
            "marvenio",
        ]
    )
    assert rc == 0

    rc2 = google_auth_main(
        [
            "--profiles-dir",
            str(profiles_dir),
            "profile",
            "add",
            "groundhogs",
            "--client-file",
            str(gh_client),
            "--copy-client",
        ]
    )
    assert rc2 == 0

    assert stat.S_IMODE(profiles_dir.stat().st_mode) == 0o700
    manifest = profiles_dir / "profiles.yaml"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    copied = profiles_dir / "groundhogs" / "client.json"
    assert copied.is_file() and stat.S_IMODE(copied.stat().st_mode) == 0o600

    store = ProfileStore(profiles_dir)
    names = [p.name for p in store.list_profiles()]
    assert names == ["groundhogs", "marvenio"]
    marv = store.get_profile("marvenio")
    assert marv.expected_email == "admin@marvenio.com"
    assert marv.remote_revoke_allowed is False
    assert CLOUD_PLATFORM_SCOPE not in marv.scopes


@pytest.mark.parametrize("bad_mode", [0o644, 0o664, 0o640])
def test_verify_private_file_rejects_group_or_world_readable(tmp_path: Path, bad_mode: int) -> None:
    path = _write_private_json(tmp_path / "client.json", _marvenio_client_payload(), mode=bad_mode)
    with pytest.raises(PermissionError, match="insecure permissions"):
        verify_private_file(path, "OAuth client file")


def test_validate_client_file_rejects_mismatches_without_leaking_secret(tmp_path: Path) -> None:
    secret_marker = "super-sensitive-secret-do-not-print"

    bad_proj = _write_private_json(
        tmp_path / "bad_proj.json",
        _marvenio_client_payload(project_id="wrong-proj", client_secret=secret_marker),
    )
    with pytest.raises(ValueError, match="project_id") as exc_proj:
        validate_client_file(_make_profile(tmp_path, client_file=bad_proj))
    assert secret_marker not in str(exc_proj.value)

    bad_id = _write_private_json(
        tmp_path / "bad_id.json",
        _marvenio_client_payload(client_id="wrong-id", client_secret=secret_marker),
    )
    with pytest.raises(ValueError, match="client_id") as exc_id:
        validate_client_file(_make_profile(tmp_path, client_file=bad_id))
    assert secret_marker not in str(exc_id.value)

    web_client = _write_private_json(
        tmp_path / "web.json",
        _marvenio_client_payload(top_key="web", client_secret=secret_marker),
    )
    with pytest.raises(ValueError, match="installed") as exc_web:
        validate_client_file(_make_profile(tmp_path, client_file=web_client))
    assert secret_marker not in str(exc_web.value)


# ── 2. OAuth login, identity pin & scope checks ────────────────────────────────


def _mock_oauth_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scopes: list[str] | None = None,
    refresh_token: str | None = "1//fake-refresh-token",
    access_token: str | None = "ya29.fake-access-token",
    client_secret: str = "fake-client-secret-value",
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeFlow:
        def run_local_server(self, **kwargs: Any) -> Any:
            captured["run_kwargs"] = kwargs
            return SimpleNamespace(
                token=access_token,
                refresh_token=refresh_token,
                scopes=scopes if scopes is not None else list(MARVENIO_PRESET["scopes"]),
                to_json=lambda: json.dumps(
                    {
                        "type": "authorized_user",
                        "client_id": MARVENIO_PRESET["expected_client_id"],
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "token": access_token,
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "scopes": scopes if scopes is not None else list(MARVENIO_PRESET["scopes"]),
                    }
                ),
            )

    def fake_from_file(path: str, scopes: list[str]) -> FakeFlow:
        captured["client_file"] = path
        captured["requested_scopes"] = scopes
        return FakeFlow()

    monkeypatch.setattr("src.google_auth.InstalledAppFlow.from_client_secrets_file", fake_from_file)
    return captured


def _userinfo_client(payload: dict[str, Any], status_code: int = 200) -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_login_profile_happy_path_writes_0600_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client_file = _write_private_json(tmp_path / "client.json", _marvenio_client_payload())
    profile = _make_profile(tmp_path, client_file=client_file)
    captured = _mock_oauth_flow(monkeypatch)
    http_client = _userinfo_client(
        {"email": "admin@marvenio.com", "email_verified": True, "hd": "marvenio.com", "sub": "11223344"}
    )

    res = login_profile(profile, launch_browser=False, http_client=http_client)
    assert res["authenticated"] is True
    assert res["email"] == "admin@marvenio.com"
    assert CLOUD_PLATFORM_SCOPE not in captured["requested_scopes"]
    assert captured["run_kwargs"]["login_hint"] == "admin@marvenio.com"
    assert captured["run_kwargs"]["hd"] == "marvenio.com"
    assert captured["run_kwargs"]["include_granted_scopes"] == "false"

    tok_path = Path(profile.token_file)
    assert tok_path.is_file()
    assert stat.S_IMODE(tok_path.stat().st_mode) == 0o600
    stored = json.loads(tok_path.read_text(encoding="utf-8"))
    assert stored["type"] == "authorized_user"
    assert "token" not in stored
    assert stored["_verified_email"] == "admin@marvenio.com"

    status = profile_status(profile, check_live=False)
    assert status["authenticated"] is True
    assert status["email"] == "admin@marvenio.com"


@pytest.mark.parametrize(
    ("userinfo", "scopes", "refresh_tok", "err_match"),
    [
        (
            {"email": "maximillian.heth@gmail.com", "email_verified": True, "hd": ""},
            None,
            "1//rt",
            "does not match expected",
        ),
        (
            {"email": "admin@marvenio.com", "email_verified": False, "hd": "marvenio.com"},
            None,
            "1//rt",
            "unverified",
        ),
        (
            {"email": "admin@marvenio.com", "email_verified": True, "hd": "other.com"},
            None,
            "1//rt",
            "workspace domain",
        ),
        (
            {"email": "admin@marvenio.com", "email_verified": True, "hd": "marvenio.com"},
            [*MARVENIO_PRESET["scopes"], CLOUD_PLATFORM_SCOPE],
            "1//rt",
            "cloud-platform",
        ),
        (
            {"email": "admin@marvenio.com", "email_verified": True, "hd": "marvenio.com"},
            None,
            None,
            "refresh token",
        ),
    ],
)
def test_login_profile_rejects_bad_identity_scope_or_missing_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    userinfo: dict[str, Any],
    scopes: list[str] | None,
    refresh_tok: str | None,
    err_match: str,
) -> None:
    client_file = _write_private_json(tmp_path / "client.json", _marvenio_client_payload())
    profile = _make_profile(tmp_path, client_file=client_file)
    _mock_oauth_flow(monkeypatch, scopes=scopes, refresh_token=refresh_tok)
    http_client = _userinfo_client(userinfo)

    with pytest.raises((PermissionError, RuntimeError), match=err_match):
        login_profile(profile, launch_browser=False, http_client=http_client)
    assert not Path(profile.token_file).exists()


def test_load_credentials_rejects_tampered_scopes_on_disk(tmp_path: Path) -> None:
    client_file = _write_private_json(tmp_path / "client.json", _marvenio_client_payload())
    profile = _make_profile(tmp_path, client_file=client_file)
    _write_private_json(
        Path(profile.token_file),
        {
            "type": "authorized_user",
            "client_id": MARVENIO_PRESET["expected_client_id"],
            "client_secret": "fake-secret",
            "refresh_token": "1//fake-refresh",
            "scopes": [*MARVENIO_PRESET["scopes"], CLOUD_PLATFORM_SCOPE],
        },
        mode=0o600,
    )
    with pytest.raises(PermissionError, match="cloud-platform"):
        load_credentials(profile, refresh=False)


# ── 3. .env profile resolution & main.py --env-file wiring ─────────────────────


def test_profile_resolution_and_crawler_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_AUTH_PROFILE", "from-default-env")
    env_gh = tmp_path / ".env.groundhogs"
    env_gh.write_text(
        "\n".join(
            [
                "GOOGLE_AUTH_PROFILE=groundhogs",
                "GOOGLE_GA4_PROPERTY_ID=111222333",
                "GOOGLE_GSC_SITE_URL=sc-domain:groundhogstx.com",
                "GOOGLE_GBP_ACCOUNT_ID=auto",
                "GOOGLE_LOOKBACK_DAYS=14",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert resolve_profile_name("explicit-cli", env_gh) == "explicit-cli"
    assert resolve_profile_name("", env_gh) == "groundhogs"

    cfg = load_config(env_gh)
    assert cfg["google_auth_profile"] == "groundhogs"
    assert cfg["google_ga4_property_id"] == "111222333"
    assert cfg["google_gsc_site_url"] == "sc-domain:groundhogstx.com"
    assert cfg["google_gbp_account_id"] == "auto"
    assert cfg["google_lookback_days"] == 14

    # CLI honors GOOGLE_PROFILES_DIR + GOOGLE_AUTH_PROFILE from --env-file
    custom_vault = tmp_path / "custom-vault"
    marv_client = _write_private_json(tmp_path / "marv-client.json", _marvenio_client_payload())
    assert (
        google_auth_main(
            [
                "--profiles-dir",
                str(custom_vault),
                "profile",
                "add",
                "groundhogs",
                "--client-file",
                str(marv_client),
            ]
        )
        == 0
    )
    env_gh.write_text(
        env_gh.read_text(encoding="utf-8") + f"GOOGLE_PROFILES_DIR={custom_vault}\n",
        encoding="utf-8",
    )
    assert google_auth_main(["status", "--env-file", str(env_gh), "--json"]) == 0

    # main.py fails fast on missing --env-file
    with pytest.raises(SystemExit) as exc:
        crawler_main(["--env-file", str(tmp_path / "missing.env"), "--mode", "competitor", "--domain", "x.com"])
    assert exc.value.code == 2


# ── 4. Read-only GoogleSignalsClient & secret redaction ────────────────────────


def test_signals_client_smoke_calls_omit_user_project_and_classify_0_qpm() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        assert "X-Goog-User-Project" not in {k.title() for k in request.headers}
        assert "x-goog-user-project" not in {k.lower() for k in request.headers}
        if "analyticsdata.googleapis.com" in url:
            return httpx.Response(200, json={"rows": [{"dimensionValues": [{"value": "20261110"}]}]})
        if "webmasters/v3" in url:
            return httpx.Response(200, json={"rows": [{"keys": ["2026-11-10"], "clicks": 3}]})
        if "mybusinessaccountmanagement.googleapis.com" in url:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Quota exceeded for quota metric 'Requests' (Bearer ya29.leak)",
                    }
                },
            )
        if "tagmanager.googleapis.com" in url:
            return httpx.Response(
                200,
                json={"accountId": "6402945713", "containerId": "285508822", "publicId": "GTM-P4T5T6C9"},
            )
        raise AssertionError(f"Unexpected URL: {url}")

    client = GoogleSignalsClient(_DummyCreds(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    ga4 = client.verify_ga4_property("properties/562840962", "2026-11-04", "2026-11-10")
    assert ga4.ok is True and ga4.http_status == 200 and ga4.row_count == 1

    gsc = client.verify_gsc_site("sc-domain:facetofacespa.com", "2026-11-04", "2026-11-10")
    assert gsc.ok is True and gsc.http_status == 200 and gsc.row_count == 1

    gbp = client.verify_gbp_access("auto")
    assert gbp.ok is False and gbp.http_status == 429 and gbp.external_gate == "GBP_QUOTA_ZERO_QPM"
    assert "ya29.leak" not in gbp.detail

    gtm = client.verify_gtm_container("6402945713", "285508822", "GTM-P4T5T6C9")
    assert gtm.ok is True and gtm.http_status == 200 and gtm.row_count == 1
    assert len(seen) == 4


def test_signals_client_blocks_mutating_requests_and_redacts_secrets() -> None:
    client = GoogleSignalsClient(_DummyCreds(), http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))

    with pytest.raises(ValueError, match="prohibited"):
        client._request("DELETE", "https://mybusinessbusinessinformation.googleapis.com/v1/locations/123")
    with pytest.raises(ValueError, match="prohibited"):
        client._request("PATCH", "https://mybusinessbusinessinformation.googleapis.com/v1/locations/123")
    with pytest.raises(ValueError, match="prohibited"):
        client._request("POST", "https://www.googleapis.com/webmasters/v3/sites/foo/sitemaps")
    with pytest.raises(ValueError, match="read-only SELECT"):
        client._request(
            "POST",
            "https://googleads.googleapis.com/v26/customers/123/googleAds:search",
            json_body={"query": "UPDATE campaign SET status='ENABLED'"},
        )
    with pytest.raises(ValueError, match="allowlist"):
        client._request("GET", "https://oauth2.googleapis.com/revoke")

    sample = (
        'Authorization: Bearer ya29.abc-XYZ {"access_token":"tok1-val","refresh_token":"1//ref-val","client_secret":"my-secret-xyz"}'
        " https://localhost/?code=4/0Asecret&state=ok"
    )
    scrubbed = redact_secrets(sample)
    for leaked in ("ya29.abc-XYZ", "tok1-val", "1//ref-val", "my-secret-xyz", "4/0Asecret"):
        assert leaked not in scrubbed


def test_gbp_fetch_daily_metrics_uses_repeated_query_params() -> None:
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(200, json={"multiDailyMetricTimeSeries": []})

    client = GoogleSignalsClient(_DummyCreds(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    data = client.fetch_gbp_daily_metrics("locations/12345", date(2026, 11, 1), date(2026, 11, 7))
    assert data == {"multiDailyMetricTimeSeries": []}
    assert "dailyMetrics=BUSINESS_IMPRESSIONS_DESKTOP_MAPS" in captured_urls[0]
    assert "dailyMetrics=CALL_CLICKS" in captured_urls[0]


# ── 5. Safe local-only logout & cross-profile isolation ────────────────────────


def test_logout_profile_unlinks_only_target_and_blocks_remote_revoke(tmp_path: Path) -> None:
    marv_client = _write_private_json(tmp_path / "marv-client.json", _marvenio_client_payload())
    gh_client = _write_private_json(
        tmp_path / "gh-client.json",
        _marvenio_client_payload(project_id="edfx-seo", client_id="123-gh"),
    )
    marv = _make_profile(tmp_path, name="marvenio", client_file=marv_client, remote_revoke_allowed=False)
    gh = GoogleAuthProfile(
        name="groundhogs",
        auth_mode="oauth_desktop",
        client_secrets_file=str(gh_client),
        token_file=str(tmp_path / "profiles" / "groundhogs" / "token.json"),
        scopes=MARVENIO_PRESET["scopes"],
        remote_revoke_allowed=True,
    )
    _write_private_json(Path(marv.token_file), {"refresh_token": "1//marv-secret"}, mode=0o600)
    _write_private_json(Path(gh.token_file), {"refresh_token": "1//gh-secret"}, mode=0o600)

    # Remote revoke blocked on shared-project marvenio profile
    with pytest.raises(PermissionError, match="forbids remote revocation"):
        logout_profile(marv, remote_revoke=True)
    assert Path(marv.token_file).exists()

    # Local logout deletes ONLY marvenio token with zero HTTP calls
    calls: list[str] = []
    http_client = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(str(r.url)), httpx.Response(200))[1]))
    res = logout_profile(marv, remote_revoke=False, http_client=http_client)
    assert res == {"profile": "marvenio", "local_token_removed": True, "remote_revoked": False}
    assert calls == []
    assert not Path(marv.token_file).exists()
    assert Path(gh.token_file).is_file()

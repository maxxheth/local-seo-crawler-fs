"""Multi-profile Google API authentication manager and CLI for local-seo-crawler-fs."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence

from google.auth import load_credentials_from_file
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
import httpx
import yaml

from src.adapters.google_signals import GoogleSignalsClient, redact_secrets
from src.config import load_config

DEFAULT_PROFILES_DIR = Path.home() / ".config" / "local-seo-crawler" / "google"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
VALID_AUTH_MODES = frozenset({"oauth_desktop", "service_account", "external_account"})
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

DEFAULT_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/business.manage",
)

MARVENIO_PRESET: dict[str, Any] = {
    "expected_project_id": "macro-anchor-513817-n3",
    "expected_client_id": "87201275884-82eip26b0mqa7q617ubu3hrt0o8si27o.apps.googleusercontent.com",
    "expected_email": "admin@marvenio.com",
    "expected_hd": "marvenio.com",
    "remote_revoke_allowed": False,
    "scopes": (
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/webmasters.readonly",
        "https://www.googleapis.com/auth/tagmanager.readonly",
        "https://www.googleapis.com/auth/adwords",
        "https://www.googleapis.com/auth/business.manage",
    ),
}


def validate_profile_name(name: str) -> str:
    cleaned = name.strip()
    if not _PROFILE_NAME_RE.match(cleaned):
        raise ValueError(
            f"Invalid profile name '{name}'; use 1-63 lowercase letters, digits, '-' or '_'"
        )
    return cleaned


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current_mode = stat.S_IMODE(path.stat().st_mode)
    if (current_mode & 0o077) != 0:
        path.chmod(0o700)
    return path


def verify_private_file(path: Path, label: str = "credential file") -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"Missing {label}: '{path}'")
    mode = stat.S_IMODE(path.stat().st_mode)
    if (mode & 0o077) != 0:
        raise PermissionError(
            f"{label} '{path}' has insecure permissions {oct(mode)}; run chmod 0600 '{path}'"
        )
    return path


def atomic_write_private(path: Path, content: bytes) -> None:
    ensure_private_dir(path.parent)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def secure_unlink(path: Path) -> bool:
    if not path.exists():
        return False
    size = path.stat().st_size
    if size > 0:
        with open(path, "r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    path.unlink()
    return True


@dataclass(frozen=True)
class GoogleAuthProfile:
    """Non-secret metadata and scope policy for a named Google auth profile."""

    name: str
    auth_mode: str
    client_secrets_file: str
    token_file: str
    scopes: tuple[str, ...]
    expected_project_id: str | None = None
    expected_client_id: str | None = None
    expected_email: str | None = None
    expected_hd: str | None = None
    remote_revoke_allowed: bool = False

    def __post_init__(self) -> None:
        validate_profile_name(self.name)
        if self.auth_mode not in VALID_AUTH_MODES:
            raise ValueError(
                f"Unsupported auth_mode '{self.auth_mode}'; expected one of {sorted(VALID_AUTH_MODES)}"
            )
        if not self.scopes:
            raise ValueError(f"Profile '{self.name}' must define at least one OAuth scope")

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> GoogleAuthProfile:
        scopes_raw = raw.get("scopes") or ()
        scopes = tuple(str(s).strip() for s in scopes_raw if str(s).strip())
        return cls(
            name=name,
            auth_mode=str(raw.get("auth_mode") or "oauth_desktop"),
            client_secrets_file=str(raw.get("client_secrets_file") or ""),
            token_file=str(raw.get("token_file") or ""),
            scopes=scopes,
            expected_project_id=(str(raw["expected_project_id"]) if raw.get("expected_project_id") else None),
            expected_client_id=(str(raw["expected_client_id"]) if raw.get("expected_client_id") else None),
            expected_email=(str(raw["expected_email"]) if raw.get("expected_email") else None),
            expected_hd=(str(raw["expected_hd"]) if raw.get("expected_hd") else None),
            remote_revoke_allowed=bool(raw.get("remote_revoke_allowed", False)),
        )

    def to_manifest_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("name", None)
        data["scopes"] = list(self.scopes)
        return data


class ProfileStore:
    """Manages named Google auth profiles in a private 0700 directory."""

    def __init__(self, profiles_dir: str | Path | None = None) -> None:
        if profiles_dir is not None and str(profiles_dir).strip():
            resolved = Path(profiles_dir).expanduser()
        else:
            env_dir = os.getenv("GOOGLE_PROFILES_DIR", "").strip()
            resolved = Path(env_dir).expanduser() if env_dir else DEFAULT_PROFILES_DIR
        self.profiles_dir = resolved
        self.manifest_path = self.profiles_dir / "profiles.yaml"

    def _load_manifest(self) -> dict[str, GoogleAuthProfile]:
        if not self.manifest_path.exists():
            return {}
        verify_private_file(self.manifest_path, "profiles manifest")
        raw_text = self.manifest_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw_text) or {}
        if not isinstance(parsed, dict):
            raise ValueError(f"Profiles manifest '{self.manifest_path}' is malformed")
        profiles_raw = parsed.get("profiles") or {}
        if not isinstance(profiles_raw, dict):
            raise ValueError(f"Profiles manifest '{self.manifest_path}' has invalid 'profiles' mapping")
        out: dict[str, GoogleAuthProfile] = {}
        for name, item in profiles_raw.items():
            if not isinstance(item, dict):
                raise ValueError(f"Profile '{name}' entry must be a mapping")
            out[str(name)] = GoogleAuthProfile.from_dict(str(name), item)
        return out

    def _write_manifest(self, profiles: dict[str, GoogleAuthProfile]) -> None:
        ensure_private_dir(self.profiles_dir)
        payload = {"profiles": {name: profiles[name].to_manifest_dict() for name in sorted(profiles)}}
        rendered = yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
        atomic_write_private(self.manifest_path, rendered)

    def list_profiles(self) -> list[GoogleAuthProfile]:
        profiles = self._load_manifest()
        return [profiles[name] for name in sorted(profiles)]

    def get_profile(self, name: str) -> GoogleAuthProfile:
        cleaned = validate_profile_name(name)
        profiles = self._load_manifest()
        if cleaned not in profiles:
            raise KeyError(f"Google auth profile '{cleaned}' not found in '{self.manifest_path}'")
        return profiles[cleaned]

    def save_profile(self, profile: GoogleAuthProfile) -> GoogleAuthProfile:
        ensure_private_dir(self.profiles_dir)
        if profile.auth_mode == "oauth_desktop":
            ensure_private_dir(Path(profile.token_file).parent)
        profiles = self._load_manifest()
        profiles[profile.name] = profile
        self._write_manifest(profiles)
        return profile

    def default_token_path(self, name: str) -> Path:
        cleaned = validate_profile_name(name)
        return self.profiles_dir / cleaned / "token.json"

    def default_client_copy_path(self, name: str) -> Path:
        cleaned = validate_profile_name(name)
        return self.profiles_dir / cleaned / "client.json"


def resolve_profile_name(
    explicit_profile: str = "",
    env_file: str | Path | None = None,
) -> str:
    if explicit_profile.strip():
        return validate_profile_name(explicit_profile)
    cfg = load_config(env_file=env_file)
    from_env = str(cfg.get("google_auth_profile") or "").strip()
    if from_env:
        return validate_profile_name(from_env)
    raise ValueError(
        "No Google auth profile specified; pass --profile or set GOOGLE_AUTH_PROFILE in .env"
    )


def validate_client_file(profile: GoogleAuthProfile) -> dict[str, str]:
    client_path = Path(profile.client_secrets_file).expanduser()
    verify_private_file(client_path, "Google credential file")
    try:
        data = json.loads(client_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"Credential file '{client_path}' is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError(f"Credential file '{client_path}' must contain a JSON object")

    if profile.auth_mode == "oauth_desktop":
        installed = data.get("installed")
        if not isinstance(installed, dict):
            raise ValueError(
                f"OAuth client file '{client_path}' must contain a Desktop 'installed' client block"
            )
        for required in ("client_id", "client_secret", "auth_uri", "token_uri"):
            val = installed.get(required)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"OAuth client file '{client_path}' is missing '{required}'")
        project_id = str(installed.get("project_id") or "")
        client_id = str(installed.get("client_id") or "")
        if profile.expected_project_id and project_id != profile.expected_project_id:
            raise ValueError(
                f"OAuth client project_id '{project_id}' does not match expected '{profile.expected_project_id}'"
            )
        if profile.expected_client_id and client_id != profile.expected_client_id:
            raise ValueError(
                f"OAuth client_id '{client_id}' does not match expected '{profile.expected_client_id}'"
            )
        return {"project_id": project_id, "client_id": client_id}

    if profile.auth_mode == "service_account":
        if data.get("type") != "service_account" or not data.get("client_email") or not data.get("private_key"):
            raise ValueError(f"Service account file '{client_path}' is invalid")
        project_id = str(data.get("project_id") or "")
        if profile.expected_project_id and project_id != profile.expected_project_id:
            raise ValueError(
                f"Service account project_id '{project_id}' does not match expected '{profile.expected_project_id}'"
            )
        return {"project_id": project_id, "client_email": str(data.get("client_email"))}

    if data.get("type") != "external_account" or not data.get("audience") or not data.get("token_url"):
        raise ValueError(f"External account credential file '{client_path}' is invalid")
    return {"audience": str(data.get("audience"))}


def validate_granted_scopes(
    granted_scopes: Sequence[str] | None,
    profile: GoogleAuthProfile,
) -> None:
    if CLOUD_PLATFORM_SCOPE not in profile.scopes and granted_scopes and CLOUD_PLATFORM_SCOPE in granted_scopes:
        raise PermissionError(
            f"Unauthorized '{CLOUD_PLATFORM_SCOPE}' scope detected for profile '{profile.name}'"
        )
    if granted_scopes:
        expected = set(profile.scopes)
        actual = {s for s in granted_scopes if s}
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise PermissionError(
                f"Granted OAuth scopes mismatch for '{profile.name}' (missing={missing}, extra={extra})"
            )


def verify_userinfo(
    access_token: str,
    profile: GoogleAuthProfile,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    if not access_token:
        raise PermissionError("Missing access token for userinfo verification")
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=15.0, follow_redirects=False)
    try:
        resp = client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
    except httpx.TransportError as exc:
        raise RuntimeError(f"OIDC userinfo request failed: {redact_secrets(type(exc).__name__)}") from None
    finally:
        if owns_client:
            client.close()

    if resp.status_code != 200:
        raise PermissionError(f"OIDC userinfo verification failed (HTTP {resp.status_code})")
    try:
        info = resp.json()
    except ValueError:
        raise PermissionError("OIDC userinfo returned invalid JSON") from None
    if not isinstance(info, dict):
        raise PermissionError("OIDC userinfo response was not an object")

    email = str(info.get("email") or "").strip()
    verified = info.get("email_verified") is True
    hd = str(info.get("hd") or "").strip()
    if not email or not verified:
        raise PermissionError("Google account email is missing or unverified")
    if profile.expected_email and email.lower() != profile.expected_email.lower():
        raise PermissionError(
            f"Signed-in account '{email}' does not match expected '{profile.expected_email}'"
        )
    if profile.expected_hd and hd.lower() != profile.expected_hd.lower():
        raise PermissionError(
            f"Signed-in workspace domain '{hd}' does not match expected '{profile.expected_hd}'"
        )
    return {"email": email, "email_verified": verified, "hd": hd, "sub": str(info.get("sub") or "")}


def login_profile(
    profile: GoogleAuthProfile,
    *,
    launch_browser: bool = True,
    host: str = "localhost",
    port: int = 0,
    timeout_seconds: int = 300,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    if profile.auth_mode != "oauth_desktop":
        raise ValueError(
            f"Profile '{profile.name}' uses auth_mode='{profile.auth_mode}', which does not use interactive browser login"
        )
    validate_client_file(profile)
    flow = InstalledAppFlow.from_client_secrets_file(
        profile.client_secrets_file,
        scopes=list(profile.scopes),
    )
    auth_kwargs: dict[str, str] = {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    if profile.expected_email:
        auth_kwargs["login_hint"] = profile.expected_email
    if profile.expected_hd:
        auth_kwargs["hd"] = profile.expected_hd

    with redirect_stdout(sys.stderr):
        creds = flow.run_local_server(
            host=host,
            port=port,
            open_browser=launch_browser,
            timeout_seconds=timeout_seconds,
            authorization_prompt_message="Open this URL in your browser to authorize Google access:\n{url}\n",
            **auth_kwargs,
        )

    refresh_token = getattr(creds, "refresh_token", None)
    access_token = getattr(creds, "token", None)
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("OAuth login completed without an offline refresh token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("OAuth login completed without an access token")

    validate_granted_scopes(getattr(creds, "scopes", None), profile)
    userinfo = verify_userinfo(access_token, profile, http_client=http_client)

    payload = json.loads(creds.to_json())
    payload.pop("token", None)
    payload.pop("expiry", None)
    payload["_profile"] = profile.name
    payload["_verified_email"] = userinfo["email"]

    token_path = Path(profile.token_file).expanduser()
    atomic_write_private(token_path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))

    return {
        "profile": profile.name,
        "authenticated": True,
        "email": userinfo["email"],
        "hd": userinfo["hd"],
        "token_file": str(token_path),
        "scopes": list(profile.scopes),
    }


def load_credentials(profile: GoogleAuthProfile, *, refresh: bool = True) -> Any:
    validate_client_file(profile)
    if profile.auth_mode == "oauth_desktop":
        token_path = Path(profile.token_file).expanduser()
        verify_private_file(token_path, "OAuth token file")
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError(f"OAuth token file '{token_path}' is not valid JSON") from None
        if not isinstance(data, dict) or data.get("type") != "authorized_user":
            raise ValueError(f"OAuth token file '{token_path}' is not an authorized_user credential")
        stored_client_id = str(data.get("client_id") or "")
        if profile.expected_client_id and stored_client_id != profile.expected_client_id:
            raise PermissionError(
                f"Token file client_id '{stored_client_id}' does not match profile '{profile.name}'"
            )
        stored_scopes = data.get("scopes")
        if stored_scopes is not None:
            if not isinstance(stored_scopes, list):
                raise ValueError(f"OAuth token file '{token_path}' has malformed 'scopes'")
            validate_granted_scopes([str(s) for s in stored_scopes], profile)
        creds = UserCredentials.from_authorized_user_info(data, scopes=list(profile.scopes))
        validate_granted_scopes(getattr(creds, "scopes", None), profile)
        if refresh:
            try:
                creds.refresh(GoogleAuthRequest())
            except Exception as exc:
                raise RuntimeError(f"Failed to refresh Google OAuth token: {redact_secrets(str(exc))}") from None
        return creds

    if profile.auth_mode == "service_account":
        creds = service_account.Credentials.from_service_account_file(
            profile.client_secrets_file,
            scopes=list(profile.scopes),
        )
        if refresh:
            creds.refresh(GoogleAuthRequest())
        return creds

    creds, _ = load_credentials_from_file(profile.client_secrets_file, scopes=list(profile.scopes))
    if refresh:
        creds.refresh(GoogleAuthRequest())
    return creds


def profile_status(
    profile: GoogleAuthProfile,
    *,
    check_live: bool = False,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    client_meta = validate_client_file(profile)
    token_path = Path(profile.token_file).expanduser() if profile.token_file else None
    token_exists = bool(token_path and token_path.exists())
    result: dict[str, Any] = {
        "profile": profile.name,
        "auth_mode": profile.auth_mode,
        "client_file": profile.client_secrets_file,
        "client_meta": client_meta,
        "token_file": str(token_path) if token_path else None,
        "authenticated": False if profile.auth_mode == "oauth_desktop" else True,
        "scopes": list(profile.scopes),
        "remote_revoke_allowed": profile.remote_revoke_allowed,
    }
    if profile.auth_mode == "oauth_desktop":
        if not token_exists:
            return result
        assert token_path is not None
        verify_private_file(token_path, "OAuth token file")
        result["token_mode"] = oct(stat.S_IMODE(token_path.stat().st_mode))
        creds = load_credentials(profile, refresh=check_live)
        result["authenticated"] = True
        if check_live and getattr(creds, "token", None):
            info = verify_userinfo(str(creds.token), profile, http_client=http_client)
            result["email"] = info["email"]
            result["hd"] = info["hd"]
        else:
            try:
                raw_tok = json.loads(token_path.read_text(encoding="utf-8"))
                if isinstance(raw_tok, dict) and raw_tok.get("_verified_email"):
                    result["email"] = str(raw_tok["_verified_email"])
            except Exception:
                pass
    return result


def logout_profile(
    profile: GoogleAuthProfile,
    *,
    remote_revoke: bool = False,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    if remote_revoke and not profile.remote_revoke_allowed:
        raise PermissionError(
            f"Profile '{profile.name}' forbids remote revocation (remote_revoke_allowed=false) "
            "to protect shared GCP project tokens; run logout without --remote-revoke for safe local removal."
        )
    token_path = Path(profile.token_file).expanduser()
    remote_revoked = False
    if remote_revoke and token_path.is_file():
        verify_private_file(token_path, "OAuth token file")
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            tok = data.get("refresh_token") if isinstance(data, dict) else None
        except Exception:
            tok = None
        if isinstance(tok, str) and tok:
            owns_client = http_client is None
            client = http_client or httpx.Client(timeout=15.0, follow_redirects=False)
            try:
                resp = client.post(
                    "https://oauth2.googleapis.com/revoke",
                    data={"token": tok},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                remote_revoked = resp.status_code == 200
            finally:
                if owns_client:
                    client.close()

    removed = secure_unlink(token_path) if profile.auth_mode == "oauth_desktop" else False
    return {
        "profile": profile.name,
        "local_token_removed": removed,
        "remote_revoked": remote_revoked,
    }


def _parse_scopes_args(raw_scopes: Sequence[str] | None) -> tuple[str, ...]:
    if not raw_scopes:
        return ()
    parsed: list[str] = []
    for item in raw_scopes:
        for piece in item.split(","):
            cleaned = piece.strip()
            if cleaned and cleaned not in parsed:
                parsed.append(cleaned)
    return tuple(parsed)


def _print_output(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(yaml.safe_dump(payload, sort_keys=False).rstrip())


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawler-google-auth",
        description="Multi-profile Google API authentication and verification CLI for local-seo-crawler-fs.",
    )
    parser.add_argument(
        "--profiles-dir",
        default="",
        help=f"Override profile storage directory [default: GOOGLE_PROFILES_DIR or {DEFAULT_PROFILES_DIR}]",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # profile ...
    profile_cmd = sub.add_parser("profile", help="Manage named Google auth profiles")
    profile_sub = profile_cmd.add_subparsers(dest="profile_command", required=True)

    p_add = profile_sub.add_parser("add", help="Register or update a named Google auth profile")
    p_add.add_argument("name", help="Profile identifier (e.g. groundhogs, marvenio)")
    p_add.add_argument("--client-file", required=True, help="Path to OAuth client, SA, or WIF JSON file")
    p_add.add_argument(
        "--mode",
        default="oauth_desktop",
        choices=sorted(VALID_AUTH_MODES),
        help="Authentication mode [default: oauth_desktop]",
    )
    p_add.add_argument(
        "--preset",
        default="default",
        choices=["default", "marvenio"],
        help="Apply preset scope/identity pins [default: default]",
    )
    p_add.add_argument(
        "--copy-client",
        action="store_true",
        help="Copy client JSON into the profile directory with 0600 permissions",
    )
    p_add.add_argument("--expected-project", default="", help="Pin expected GCP project_id")
    p_add.add_argument("--expected-client-id", default="", help="Pin expected OAuth client_id")
    p_add.add_argument("--expected-email", default="", help="Pin expected Google account email")
    p_add.add_argument("--expected-hd", default="", help="Pin expected Google Workspace domain (hd)")
    p_add.add_argument(
        "--scope",
        action="append",
        default=[],
        help="OAuth scope (repeatable or comma-separated); overrides preset scopes",
    )
    p_add.add_argument(
        "--allow-remote-revoke",
        action="store_true",
        help="Allow --remote-revoke on logout (unsafe for shared GCP projects)",
    )
    p_add.add_argument("--json", action="store_true", help="Output JSON")

    p_list = profile_sub.add_parser("list", help="List configured profiles")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    # status
    p_status = sub.add_parser("status", help="Inspect profile and token status")
    p_status.add_argument("--profile", default="", help="Profile name [default: GOOGLE_AUTH_PROFILE]")
    p_status.add_argument("--env-file", default="", help="Optional .env file for GOOGLE_AUTH_PROFILE")
    p_status.add_argument("--live", action="store_true", help="Refresh token and verify OIDC userinfo")
    p_status.add_argument("--json", action="store_true", help="Output JSON")

    # login
    p_login = sub.add_parser("login", help="Run interactive browser OAuth login for a profile")
    p_login.add_argument("--profile", default="", help="Profile name [default: GOOGLE_AUTH_PROFILE]")
    p_login.add_argument("--env-file", default="", help="Optional .env file for GOOGLE_AUTH_PROFILE")
    p_login.add_argument("--no-launch-browser", action="store_true", help="Print URL instead of opening browser")
    p_login.add_argument("--timeout", type=int, default=300, help="OAuth callback timeout in seconds [default: 300]")
    p_login.add_argument("--json", action="store_true", help="Output JSON")

    # verify
    p_verify = sub.add_parser("verify", help="Run strictly read-only bounded smoke calls (GA4, GSC, GBP)")
    p_verify.add_argument("--profile", default="", help="Profile name [default: GOOGLE_AUTH_PROFILE]")
    p_verify.add_argument("--env-file", default="", help="Optional .env file providing profile and asset IDs")
    p_verify.add_argument("--ga4-property", default="", help="GA4 numeric property ID [default: GOOGLE_GA4_PROPERTY_ID]")
    p_verify.add_argument("--gsc-site", default="", help="GSC site URL [default: GOOGLE_GSC_SITE_URL]")
    p_verify.add_argument(
        "--gbp-account",
        default="",
        help="GBP account ID, 'auto', or 'skip' [default: GOOGLE_GBP_ACCOUNT_ID or skip]",
    )
    p_verify.add_argument(
        "--gtm-container",
        default="",
        help="Optional GTM check in format account_id:container_id[:expected_public_id]",
    )
    p_verify.add_argument("--lookback-days", type=int, default=0, help="Lookback days for smoke window [default: 7]")
    p_verify.add_argument("--json", action="store_true", help="Output JSON")

    # logout
    p_logout = sub.add_parser("logout", help="Delete local profile token securely")
    p_logout.add_argument("--profile", default="", help="Profile name [default: GOOGLE_AUTH_PROFILE]")
    p_logout.add_argument("--env-file", default="", help="Optional .env file for GOOGLE_AUTH_PROFILE")
    p_logout.add_argument(
        "--remote-revoke",
        action="store_true",
        help="Also call Google's remote /revoke endpoint (blocked unless profile permits)",
    )
    p_logout.add_argument("--json", action="store_true", help="Output JSON")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    store = ProfileStore(args.profiles_dir or None)

    try:
        if args.command == "profile":
            if args.profile_command == "list":
                items = []
                for p in store.list_profiles():
                    tok = Path(p.token_file).expanduser() if p.token_file else None
                    items.append(
                        {
                            "name": p.name,
                            "auth_mode": p.auth_mode,
                            "expected_project_id": p.expected_project_id,
                            "expected_email": p.expected_email,
                            "token_present": bool(tok and tok.is_file()),
                            "scopes": list(p.scopes),
                        }
                    )
                _print_output({"profiles_dir": str(store.profiles_dir), "profiles": items}, as_json=args.json)
                return 0

            if args.profile_command == "add":
                name = validate_profile_name(args.name)
                preset = MARVENIO_PRESET if args.preset == "marvenio" else {}
                scopes = _parse_scopes_args(args.scope) or tuple(preset.get("scopes", DEFAULT_SCOPES))
                src_client = Path(args.client_file).expanduser().resolve()
                if args.copy_client:
                    verify_private_file(src_client, "source credential file")
                    dest_client = store.default_client_copy_path(name)
                    atomic_write_private(dest_client, src_client.read_bytes())
                    client_path_str = str(dest_client)
                else:
                    client_path_str = str(src_client)

                token_path_str = str(store.default_token_path(name)) if args.mode == "oauth_desktop" else ""
                profile = GoogleAuthProfile(
                    name=name,
                    auth_mode=args.mode,
                    client_secrets_file=client_path_str,
                    token_file=token_path_str,
                    scopes=scopes,
                    expected_project_id=args.expected_project or preset.get("expected_project_id"),
                    expected_client_id=args.expected_client_id or preset.get("expected_client_id"),
                    expected_email=args.expected_email or preset.get("expected_email"),
                    expected_hd=args.expected_hd or preset.get("expected_hd"),
                    remote_revoke_allowed=bool(
                        args.allow_remote_revoke or preset.get("remote_revoke_allowed", False)
                    ),
                )
                meta = validate_client_file(profile)
                store.save_profile(profile)
                _print_output(
                    {
                        "saved": True,
                        "profile": profile.name,
                        "manifest": str(store.manifest_path),
                        "client_meta": meta,
                    },
                    as_json=args.json,
                )
                return 0

        cfg = load_config(env_file=args.env_file or None)
        if not args.profiles_dir and cfg.get("google_profiles_dir"):
            store = ProfileStore(str(cfg["google_profiles_dir"]))
        profile_name = resolve_profile_name(args.profile, args.env_file or None)
        profile = store.get_profile(profile_name)

        if args.command == "status":
            res = profile_status(profile, check_live=args.live)
            _print_output(res, as_json=args.json)
            return 0

        if args.command == "login":
            res = login_profile(
                profile,
                launch_browser=not args.no_launch_browser,
                timeout_seconds=args.timeout,
            )
            _print_output(res, as_json=args.json)
            return 0

        if args.command == "logout":
            res = logout_profile(profile, remote_revoke=args.remote_revoke)
            _print_output(res, as_json=args.json)
            return 0

        if args.command == "verify":
            ga4_prop = args.ga4_property.strip() or str(cfg.get("google_ga4_property_id") or "")
            gsc_site = args.gsc_site.strip() or str(cfg.get("google_gsc_site_url") or "")
            gbp_acct = args.gbp_account.strip() or str(cfg.get("google_gbp_account_id") or "")
            lookback = args.lookback_days or 7
            end_d = date.today() - timedelta(days=3)
            start_d = end_d - timedelta(days=max(lookback - 1, 1))
            start_s, end_s = start_d.isoformat(), end_d.isoformat()

            creds = load_credentials(profile, refresh=True)
            checks: list[dict[str, Any]] = []
            with GoogleSignalsClient(creds) as client:
                if ga4_prop:
                    checks.append(client.verify_ga4_property(ga4_prop, start_s, end_s).to_dict())
                if gsc_site:
                    checks.append(client.verify_gsc_site(gsc_site, start_s, end_s).to_dict())
                if gbp_acct and gbp_acct.lower() != "skip":
                    checks.append(client.verify_gbp_access(gbp_acct).to_dict())
                if args.gtm_container.strip():
                    parts = args.gtm_container.strip().split(":", 2)
                    if len(parts) < 2:
                        raise ValueError("--gtm-container expects account_id:container_id[:public_id]")
                    pub = parts[2] if len(parts) == 3 else ""
                    checks.append(client.verify_gtm_container(parts[0], parts[1], pub).to_dict())

            if not checks:
                raise ValueError(
                    "Nothing to verify; pass --ga4-property, --gsc-site, --gbp-account, --gtm-container, or set them in .env"
                )
            _print_output(
                {
                    "profile": profile.name,
                    "window": {"start_date": start_s, "end_date": end_s},
                    "checks": checks,
                },
                as_json=args.json,
            )
            all_ok_or_gated = all(c["ok"] or c["external_gate"] is not None for c in checks)
            return 0 if all_ok_or_gated else 1

    except Exception as exc:
        print(redact_secrets(f"Error: {exc}"), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

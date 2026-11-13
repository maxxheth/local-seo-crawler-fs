"""Read-only Google Analytics 4, Search Console, and Business Profile signals adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import re
from typing import Any
from urllib.parse import quote, urlparse

from google.auth.transport.requests import Request as GoogleAuthRequest
import httpx

ALLOWED_API_HOSTS = frozenset(
    {
        "analyticsdata.googleapis.com",
        "www.googleapis.com",
        "searchconsole.googleapis.com",
        "mybusinessaccountmanagement.googleapis.com",
        "mybusinessbusinessinformation.googleapis.com",
        "businessprofileperformance.googleapis.com",
        "tagmanager.googleapis.com",
        "googleads.googleapis.com",
    }
)

ALLOWED_POST_SUFFIXES = (
    ":runReport",
    ":batchRunReports",
    "/searchAnalytics/query",
    ":fetchMultiDailyMetricsTimeSeries",
    "/googleAds:search",
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (
        re.compile(r'("(?:access_token|refresh_token|id_token|client_secret)"\s*:\s*")[^"]*(")'),
        r"\1[REDACTED]\2",
    ),
    (
        re.compile(r"([?&](?:code|refresh_token|access_token|client_secret)=)[^&\s]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
)


def redact_secrets(text: str) -> str:
    """Scrub OAuth tokens, authorization codes, and client secrets from text."""
    scrubbed = text
    for pattern, replacement in _SECRET_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed


@dataclass(frozen=True)
class SignalCheckResult:
    """Sanitized result of a bounded, read-only Google API verification call."""

    target: str
    product: str
    resource: str
    ok: bool
    http_status: int | None
    row_count: int | None
    error_status: str | None = None
    error_reason: str | None = None
    external_gate: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_ga4_property(property_id: str) -> str:
    cleaned = property_id.strip().removeprefix("properties/").strip()
    if not cleaned or not cleaned.isdecimal():
        raise ValueError(f"Invalid GA4 property ID '{property_id}'; expected numeric ID")
    return cleaned


def _validate_read_only_request(
    method: str,
    url: str,
    json_body: dict[str, Any] | None = None,
) -> str:
    verb = method.strip().upper()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_API_HOSTS:
        raise ValueError(f"Google API URL host '{host}' is not on the read-only allowlist")
    if verb == "GET":
        return verb
    if verb == "POST" and parsed.path.endswith(ALLOWED_POST_SUFFIXES):
        if parsed.path.endswith("/googleAds:search"):
            query = json_body.get("query") if isinstance(json_body, dict) else None
            if not isinstance(query, str) or not query.strip().upper().startswith("SELECT "):
                raise ValueError("Google Ads requests must be read-only SELECT queries")
        return verb
    raise ValueError(f"Write or mutating Google API requests are prohibited ({verb} {parsed.path})")


def _extract_google_error(payload: dict[str, Any]) -> tuple[str | None, str | None, str]:
    err = payload.get("error")
    if not isinstance(err, dict):
        return None, None, ""
    status = err.get("status")
    status_str = str(status) if status is not None else None
    reason: str | None = None
    details = err.get("details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                break
    message = redact_secrets(str(err.get("message") or ""))
    if len(message) > 200:
        message = message[:197] + "..."
    return status_str, reason, message


class GoogleSignalsClient:
    """Strictly read-only Google Analytics 4, Search Console, and Business Profile client."""

    def __init__(
        self,
        credentials: Any,
        http_client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._credentials = credentials
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout, follow_redirects=False)

    def __enter__(self) -> GoogleSignalsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _access_token(self) -> str:
        valid = bool(getattr(self._credentials, "valid", False))
        token = getattr(self._credentials, "token", None)
        if not valid or not isinstance(token, str) or not token:
            try:
                self._credentials.refresh(GoogleAuthRequest())
            except Exception as exc:
                raise RuntimeError(f"Google credential refresh failed: {redact_secrets(str(exc))}") from None
            token = getattr(self._credentials, "token", None)
        if not isinstance(token, str) or not token:
            raise RuntimeError("Google credentials returned no access token")
        return token

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        verb = _validate_read_only_request(method, url, json_body)
        token = self._access_token()
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if extra_headers:
            for key, value in extra_headers.items():
                if key.lower() != "x-goog-user-project":
                    headers[key] = value
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = self._client.request(
                    verb,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                )
                if resp.status_code in (502, 503, 504) and attempt == 0:
                    continue
                try:
                    data = resp.json()
                    if not isinstance(data, dict):
                        data = {}
                except ValueError:
                    data = {}
                return resp.status_code, data
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == 0:
                    continue
        raise RuntimeError(
            f"Google API request failed: {redact_secrets(type(last_exc).__name__ if last_exc else 'TransportError')}"
        )

    def verify_ga4_property(self, property_id: str, start_date: str, end_date: str) -> SignalCheckResult:
        num_id = _normalize_ga4_property(property_id)
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{num_id}:runReport"
        status, payload = self._request(
            "POST",
            url,
            json_body={
                "dateRanges": [{"startDate": start_date, "endDate": end_date}],
                "dimensions": [{"name": "date"}],
                "metrics": [{"name": "activeUsers"}],
                "limit": 1,
            },
        )
        if status == 200:
            rows = payload.get("rows")
            count = len(rows) if isinstance(rows, list) else 0
            return SignalCheckResult(
                target=f"ga4:{num_id}",
                product="ga4",
                resource=num_id,
                ok=True,
                http_status=status,
                row_count=count,
                detail="GA4 Data API read succeeded",
            )
        err_status, err_reason, detail = _extract_google_error(payload)
        return SignalCheckResult(
            target=f"ga4:{num_id}",
            product="ga4",
            resource=num_id,
            ok=False,
            http_status=status,
            row_count=None,
            error_status=err_status,
            error_reason=err_reason,
            detail=detail,
        )

    def fetch_ga4_landing_pages(
        self,
        property_id: str,
        start_date: str,
        end_date: str,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        num_id = _normalize_ga4_property(property_id)
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{num_id}:runReport"
        status, payload = self._request(
            "POST",
            url,
            json_body={
                "dateRanges": [{"startDate": start_date, "endDate": end_date}],
                "dimensions": [
                    {"name": "date"},
                    {"name": "landingPage"},
                    {"name": "sessionDefaultChannelGroup"},
                ],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "engagedSessions"},
                    {"name": "activeUsers"},
                    {"name": "keyEvents"},
                ],
                "limit": min(max(int(limit), 1), 10000),
            },
        )
        if status != 200:
            err_status, _, detail = _extract_google_error(payload)
            raise RuntimeError(f"GA4 runReport failed (HTTP {status}, {err_status}): {detail}")
        rows = payload.get("rows")
        return list(rows) if isinstance(rows, list) else []

    def verify_gsc_site(self, site_url: str, start_date: str, end_date: str) -> SignalCheckResult:
        cleaned = site_url.strip()
        if not cleaned:
            raise ValueError("Search Console site URL must not be empty")
        encoded = quote(cleaned, safe="")
        url = f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query"
        status, payload = self._request(
            "POST",
            url,
            json_body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["date"],
                "rowLimit": 1,
            },
        )
        if status == 200:
            rows = payload.get("rows")
            count = len(rows) if isinstance(rows, list) else 0
            return SignalCheckResult(
                target=f"gsc:{cleaned}",
                product="gsc",
                resource=cleaned,
                ok=True,
                http_status=status,
                row_count=count,
                detail="Search Console API read succeeded",
            )
        err_status, err_reason, detail = _extract_google_error(payload)
        return SignalCheckResult(
            target=f"gsc:{cleaned}",
            product="gsc",
            resource=cleaned,
            ok=False,
            http_status=status,
            row_count=None,
            error_status=err_status,
            error_reason=err_reason,
            detail=detail,
        )

    def fetch_gsc_page_metrics(
        self,
        site_url: str,
        start_date: str,
        end_date: str,
        row_limit: int = 5000,
    ) -> list[dict[str, Any]]:
        cleaned = site_url.strip()
        if not cleaned:
            raise ValueError("Search Console site URL must not be empty")
        encoded = quote(cleaned, safe="")
        url = f"https://www.googleapis.com/webmasters/v3/sites/{encoded}/searchAnalytics/query"
        status, payload = self._request(
            "POST",
            url,
            json_body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["date", "page"],
                "rowLimit": min(max(int(row_limit), 1), 25000),
            },
        )
        if status != 200:
            err_status, _, detail = _extract_google_error(payload)
            raise RuntimeError(f"GSC query failed (HTTP {status}, {err_status}): {detail}")
        rows = payload.get("rows")
        return list(rows) if isinstance(rows, list) else []

    def list_gbp_accounts(self, page_size: int = 20) -> list[dict[str, Any]]:
        status, payload = self._request(
            "GET",
            "https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
            params={"pageSize": min(max(int(page_size), 1), 20)},
        )
        if status != 200:
            err_status, _, detail = _extract_google_error(payload)
            raise RuntimeError(f"GBP accounts.list failed (HTTP {status}, {err_status}): {detail}")
        accounts = payload.get("accounts")
        return list(accounts) if isinstance(accounts, list) else []

    def list_gbp_locations(self, account_name: str, page_size: int = 100) -> list[dict[str, Any]]:
        acct = account_name.strip()
        if not acct.startswith("accounts/"):
            acct = f"accounts/{acct}"
        status, payload = self._request(
            "GET",
            f"https://mybusinessbusinessinformation.googleapis.com/v1/{acct}/locations",
            params={
                "pageSize": min(max(int(page_size), 1), 100),
                "readMask": "name,title,storeCode,websiteUri,metadata",
            },
        )
        if status != 200:
            err_status, _, detail = _extract_google_error(payload)
            raise RuntimeError(f"GBP locations.list failed (HTTP {status}, {err_status}): {detail}")
        locations = payload.get("locations")
        return list(locations) if isinstance(locations, list) else []

    def fetch_gbp_daily_metrics(
        self,
        location_name: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        loc = location_name.strip()
        if not loc.startswith("locations/"):
            loc = f"locations/{loc}"
        status, payload = self._request(
            "GET",
            f"https://businessprofileperformance.googleapis.com/v1/{loc}:fetchMultiDailyMetricsTimeSeries",
            params={
                "dailyMetrics": [
                    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
                    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
                    "WEBSITE_CLICKS",
                    "CALL_CLICKS",
                ],
                "dailyRange.start_date.year": start_date.year,
                "dailyRange.start_date.month": start_date.month,
                "dailyRange.start_date.day": start_date.day,
                "dailyRange.end_date.year": end_date.year,
                "dailyRange.end_date.month": end_date.month,
                "dailyRange.end_date.day": end_date.day,
            },
        )
        if status != 200:
            err_status, _, detail = _extract_google_error(payload)
            raise RuntimeError(f"GBP performance read failed (HTTP {status}, {err_status}): {detail}")
        return payload

    def verify_gbp_access(self, account_id: str = "auto") -> SignalCheckResult:
        cleaned = account_id.strip() or "auto"
        status, payload = self._request(
            "GET",
            "https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
            params={"pageSize": 5},
        )
        if status == 200:
            accounts = payload.get("accounts")
            acct_list = list(accounts) if isinstance(accounts, list) else []
            if cleaned == "auto":
                return SignalCheckResult(
                    target="gbp:auto",
                    product="gbp",
                    resource="accounts",
                    ok=True,
                    http_status=status,
                    row_count=len(acct_list),
                    detail="GBP Account Management read succeeded",
                )
            acct = cleaned if cleaned.startswith("accounts/") else f"accounts/{cleaned}"
            loc_status, loc_payload = self._request(
                "GET",
                f"https://mybusinessbusinessinformation.googleapis.com/v1/{acct}/locations",
                params={"pageSize": 1, "readMask": "name,title"},
            )
            if loc_status == 200:
                locs = loc_payload.get("locations")
                loc_count = len(locs) if isinstance(locs, list) else 0
                return SignalCheckResult(
                    target=f"gbp:{acct}",
                    product="gbp",
                    resource=acct,
                    ok=True,
                    http_status=loc_status,
                    row_count=loc_count,
                    detail="GBP Business Information locations read succeeded",
                )
            err_status, err_reason, detail = _extract_google_error(loc_payload)
            gate = "GBP_QUOTA_ZERO_QPM" if (loc_status == 429 or err_status == "RESOURCE_EXHAUSTED") else None
            return SignalCheckResult(
                target=f"gbp:{acct}",
                product="gbp",
                resource=acct,
                ok=False,
                http_status=loc_status,
                row_count=None,
                error_status=err_status,
                error_reason=err_reason,
                external_gate=gate,
                detail=(
                    "Google Business Profile Basic API Access quota is 0 QPM (pending Google approval)"
                    if gate
                    else detail
                ),
            )

        err_status, err_reason, detail = _extract_google_error(payload)
        gate = "GBP_QUOTA_ZERO_QPM" if (status == 429 or err_status == "RESOURCE_EXHAUSTED") else None
        return SignalCheckResult(
            target=f"gbp:{cleaned}",
            product="gbp",
            resource=cleaned,
            ok=False,
            http_status=status,
            row_count=None,
            error_status=err_status,
            error_reason=err_reason,
            external_gate=gate,
            detail=(
                "Google Business Profile Basic API Access quota is 0 QPM (pending Google approval)"
                if gate
                else detail
            ),
        )

    def verify_gtm_container(
        self,
        account_id: str,
        container_id: str,
        expected_public_id: str = "",
    ) -> SignalCheckResult:
        acct = account_id.strip()
        cont = container_id.strip()
        if not acct.isdecimal() or not cont.isdecimal():
            raise ValueError("GTM account_id and container_id must be numeric")
        url = f"https://tagmanager.googleapis.com/tagmanager/v2/accounts/{acct}/containers/{cont}"
        status, payload = self._request("GET", url)
        if status == 200:
            matched = (
                str(payload.get("accountId", "")) == acct
                and str(payload.get("containerId", "")) == cont
                and (not expected_public_id or str(payload.get("publicId", "")) == expected_public_id)
            )
            return SignalCheckResult(
                target=f"gtm:{acct}/{cont}",
                product="gtm",
                resource=str(payload.get("publicId") or f"{acct}/{cont}"),
                ok=matched,
                http_status=status,
                row_count=1 if matched else 0,
                detail="Tag Manager container matched" if matched else "Tag Manager container ID mismatch",
            )
        err_status, err_reason, detail = _extract_google_error(payload)
        return SignalCheckResult(
            target=f"gtm:{acct}/{cont}",
            product="gtm",
            resource=expected_public_id or f"{acct}/{cont}",
            ok=False,
            http_status=status,
            row_count=None,
            error_status=err_status,
            error_reason=err_reason,
            detail=detail,
        )

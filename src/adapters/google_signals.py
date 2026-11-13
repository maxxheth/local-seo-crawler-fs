"""Read-only Google Analytics 4, Search Console, and Business Profile signals adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import re
from typing import Any, NoReturn, Sequence
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


class GoogleAPIError(RuntimeError):
    """Sanitized Google API failure preserving HTTP status and external gate metadata."""

    def __init__(
        self,
        message: str,
        *,
        product: str,
        http_status: int,
        error_status: str | None = None,
        error_reason: str | None = None,
        external_gate: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(redact_secrets(message))
        self.product = product
        self.http_status = http_status
        self.error_status = error_status
        self.error_reason = error_reason
        self.external_gate = external_gate
        self.detail = redact_secrets(detail)


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


def _raise_google_api_error(
    product: str,
    action: str,
    status: int,
    payload: dict[str, Any],
) -> NoReturn:
    err_status, err_reason, raw_detail = _extract_google_error(payload)
    gate: str | None = None
    detail = raw_detail
    if product == "gbp" and (status == 429 or err_status == "RESOURCE_EXHAUSTED"):
        gate = "GBP_QUOTA_ZERO_QPM"
        detail = "Google Business Profile Basic API Access quota is 0 QPM (pending Google approval)"
    raise GoogleAPIError(
        f"{action} failed (HTTP {status}, {err_status}): {detail}",
        product=product,
        http_status=status,
        error_status=err_status,
        error_reason=err_reason,
        external_gate=gate,
        detail=detail,
    )


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
            _raise_google_api_error("ga4", "GA4 runReport", status, payload)
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
            _raise_google_api_error("gsc", "GSC query", status, payload)
        rows = payload.get("rows")
        return list(rows) if isinstance(rows, list) else []

    def list_gbp_accounts(self, page_size: int = 20) -> list[dict[str, Any]]:
        status, payload = self._request(
            "GET",
            "https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
            params={"pageSize": min(max(int(page_size), 1), 20)},
        )
        if status != 200:
            _raise_google_api_error("gbp", "GBP accounts.list", status, payload)
        accounts = payload.get("accounts")
        return list(accounts) if isinstance(accounts, list) else []

    def list_gbp_locations(
        self,
        account_name: str,
        page_size: int = 100,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        acct = account_name.strip()
        if not acct.startswith("accounts/"):
            acct = f"accounts/{acct}"
        all_locations: list[dict[str, Any]] = []
        next_token = ""
        pages = 0
        limit_pages = max(int(max_pages), 1)
        while pages < limit_pages:
            params: dict[str, Any] = {
                "pageSize": min(max(int(page_size), 1), 100),
                "readMask": "name,title,storeCode,websiteUri,metadata",
            }
            if next_token:
                params["pageToken"] = next_token
            status, payload = self._request(
                "GET",
                f"https://mybusinessbusinessinformation.googleapis.com/v1/{acct}/locations",
                params=params,
            )
            if status != 200:
                _raise_google_api_error("gbp", "GBP locations.list", status, payload)
            locations = payload.get("locations")
            if isinstance(locations, list):
                all_locations.extend(locations)
            next_token = str(payload.get("nextPageToken") or "").strip()
            pages += 1
            if not next_token:
                break
        return all_locations

    def fetch_gbp_daily_metrics(
        self,
        location_name: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        loc = location_name.strip()
        if "/locations/" in loc:
            loc = f"locations/{loc.rsplit('/', 1)[-1]}"
        elif not loc.startswith("locations/"):
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
            _raise_google_api_error("gbp", "GBP performance read", status, payload)
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


def _ga4_dim(dims: Sequence[Any], idx: int) -> str:
    if idx < len(dims) and isinstance(dims[idx], dict):
        return str(dims[idx].get("value") or "")
    return ""


def _ga4_metric(mets: Sequence[Any], idx: int) -> float:
    if idx < len(mets) and isinstance(mets[idx], dict):
        raw = mets[idx].get("value")
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _parse_ga4_date(raw_date: str) -> str:
    cleaned = raw_date.strip()
    if len(cleaned) == 8 and cleaned.isdigit():
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    return cleaned


def parse_ga4_landing_page_rows(
    property_id: str,
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize GA4 Data API landing-page report rows for SQLite storage."""
    num_id = _normalize_ga4_property(property_id)
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dims = row.get("dimensionValues") or ()
        mets = row.get("metricValues") or ()
        parsed.append(
            {
                "property_id": num_id,
                "metric_date": _parse_ga4_date(_ga4_dim(dims, 0)),
                "landing_page": _ga4_dim(dims, 1),
                "channel_group": _ga4_dim(dims, 2),
                "sessions": int(round(_ga4_metric(mets, 0))),
                "engaged_sessions": int(round(_ga4_metric(mets, 1))),
                "active_users": int(round(_ga4_metric(mets, 2))),
                "key_events": _ga4_metric(mets, 3),
            }
        )
    return parsed


def parse_gsc_page_metric_rows(
    site_url: str,
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize Search Console API date+page rows for SQLite storage."""
    cleaned_site = site_url.strip()
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        keys = row.get("keys") or ()
        parsed.append(
            {
                "site_url": cleaned_site,
                "metric_date": str(keys[0]) if len(keys) > 0 else "",
                "page_url": str(keys[1]) if len(keys) > 1 else "",
                "clicks": float(row.get("clicks", 0.0) or 0.0),
                "impressions": float(row.get("impressions", 0.0) or 0.0),
                "ctr": float(row.get("ctr", 0.0) or 0.0),
                "position": float(row.get("position", 0.0) or 0.0),
            }
        )
    return parsed


def _location_id_variants(location_id: str) -> tuple[str, ...]:
    cleaned = location_id.strip().rstrip("/")
    if not cleaned:
        return ()
    tail = cleaned.rsplit("/", 1)[-1]
    return (cleaned, tail, f"locations/{tail}")


def _capture_source_step(action: Any) -> dict[str, Any]:
    try:
        return action()
    except GoogleAPIError as exc:
        return {
            "configured": True,
            "ok": False,
            "rows_saved": 0,
            "http_status": exc.http_status,
            "error_status": exc.error_status,
            "external_gate": exc.external_gate,
            "detail": redact_secrets(exc.detail or str(exc)),
        }
    except Exception as exc:
        return {
            "configured": True,
            "ok": False,
            "rows_saved": 0,
            "http_status": None,
            "error_status": type(exc).__name__,
            "external_gate": None,
            "detail": redact_secrets(str(exc)),
        }


def _skipped_source(detail: str = "Not configured") -> dict[str, Any]:
    return {
        "configured": False,
        "ok": True,
        "rows_saved": 0,
        "http_status": None,
        "error_status": None,
        "external_gate": None,
        "detail": detail,
    }


def _enrich_gbp_source(
    storage: Any,
    client: GoogleSignalsClient,
    *,
    gbp_acct: str,
    loc_filters: tuple[str, ...],
    start_d: date,
    end_d: date,
) -> dict[str, Any]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    if gbp_acct.lower() == "auto":
        for acct in client.list_gbp_accounts():
            acct_name = str(acct.get("name") or "").strip()
            if not acct_name:
                continue
            for loc in client.list_gbp_locations(acct_name):
                candidates.append((acct_name, loc))
    elif gbp_acct:
        norm_acct = gbp_acct if gbp_acct.startswith("accounts/") else f"accounts/{gbp_acct}"
        for loc in client.list_gbp_locations(norm_acct):
            candidates.append((norm_acct, loc))
    else:
        for loc_id in loc_filters:
            tail = loc_id.rsplit("/", 1)[-1]
            candidates.append(("", {"name": f"locations/{tail}"}))

    filter_tokens = {tok for raw in loc_filters for tok in _location_id_variants(raw)}
    if filter_tokens:
        candidates = [
            (acct_id, loc)
            for acct_id, loc in candidates
            if any(tok in filter_tokens for tok in _location_id_variants(str(loc.get("name") or "")))
        ]

    start_s, end_s = start_d.isoformat(), end_d.isoformat()
    location_rows: list[dict[str, Any]] = []
    metric_errors: list[Exception] = []
    for acct_id, loc in candidates:
        loc_name = str(loc.get("name") or "").strip()
        if not loc_name:
            continue
        metrics: dict[str, Any]
        try:
            metrics = client.fetch_gbp_daily_metrics(loc_name, start_d, end_d)
        except GoogleAPIError as exc:
            metrics = {
                "error": redact_secrets(exc.detail or str(exc)),
                "http_status": exc.http_status,
                "error_status": exc.error_status,
                "external_gate": exc.external_gate,
            }
            metric_errors.append(exc)
        except Exception as exc:
            metrics = {"error": redact_secrets(str(exc))}
            metric_errors.append(exc)

        location_rows.append(
            {
                "location_name": loc_name,
                "account_id": acct_id,
                "title": str(loc.get("title") or ""),
                "store_code": str(loc.get("storeCode") or ""),
                "website_uri": str(loc.get("websiteUri") or ""),
                "raw": loc,
                "metric_start_date": start_s,
                "metric_end_date": end_s,
                "metrics": metrics,
            }
        )

    saved = storage.save_google_gbp_locations_batch(location_rows)
    if metric_errors:
        first_err = metric_errors[0]
        return {
            "configured": True,
            "ok": False,
            "rows_saved": saved,
            "http_status": getattr(first_err, "http_status", None),
            "error_status": getattr(first_err, "error_status", type(first_err).__name__),
            "external_gate": getattr(first_err, "external_gate", None),
            "detail": redact_secrets(
                f"Saved {saved} GBP locations; daily metrics failed for {len(metric_errors)} location(s): {first_err}"
            ),
        }
    return {
        "configured": True,
        "ok": True,
        "rows_saved": saved,
        "http_status": 200,
        "error_status": None,
        "external_gate": None,
        "detail": f"Saved {saved} GBP locations",
    }


def collect_and_persist_google_signals(
    storage: Any,
    client: GoogleSignalsClient,
    *,
    profile_name: str,
    ga4_property_id: str = "",
    gsc_site_url: str = "",
    gbp_account_id: str = "",
    gbp_location_ids: Sequence[str] = (),
    lookback_days: int = 28,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Fetch read-only GA4, GSC, and GBP signals and persist them to crawl_results.db."""
    days = max(int(lookback_days), 1)
    end_d = end_date or (date.today() - timedelta(days=3))
    start_d = end_d - timedelta(days=days - 1)
    start_s, end_s = start_d.isoformat(), end_d.isoformat()

    ga4_prop = ga4_property_id.strip()
    gsc_site = gsc_site_url.strip()
    gbp_acct = gbp_account_id.strip()
    loc_filters = tuple(str(x).strip() for x in gbp_location_ids if str(x).strip())

    def _run_ga4() -> dict[str, Any]:
        raw_rows = client.fetch_ga4_landing_pages(ga4_prop, start_s, end_s, limit=10000)
        parsed = parse_ga4_landing_page_rows(ga4_prop, raw_rows)
        saved = storage.save_google_ga4_pages_batch(parsed)
        return {
            "configured": True,
            "ok": True,
            "rows_saved": saved,
            "http_status": 200,
            "error_status": None,
            "external_gate": None,
            "detail": f"Saved {saved} GA4 landing-page rows",
        }

    def _run_gsc() -> dict[str, Any]:
        raw_rows = client.fetch_gsc_page_metrics(gsc_site, start_s, end_s, row_limit=25000)
        parsed = parse_gsc_page_metric_rows(gsc_site, raw_rows)
        saved = storage.save_google_gsc_pages_batch(parsed)
        return {
            "configured": True,
            "ok": True,
            "rows_saved": saved,
            "http_status": 200,
            "error_status": None,
            "external_gate": None,
            "detail": f"Saved {saved} GSC page metric rows",
        }

    sources: dict[str, dict[str, Any]] = {
        "ga4": _capture_source_step(_run_ga4) if ga4_prop else _skipped_source(),
        "gsc": _capture_source_step(_run_gsc) if gsc_site else _skipped_source(),
        "gbp": (
            _capture_source_step(
                lambda: _enrich_gbp_source(
                    storage,
                    client,
                    gbp_acct=gbp_acct,
                    loc_filters=loc_filters,
                    start_d=start_d,
                    end_d=end_d,
                )
            )
            if ((gbp_acct and gbp_acct.lower() != "skip") or loc_filters)
            else _skipped_source()
        ),
    }

    configured = [src for src in sources.values() if src["configured"]]
    all_ok = all(bool(src["ok"]) for src in configured)
    summary: dict[str, Any] = {
        "enabled": True,
        "profile": profile_name,
        "window": {
            "start_date": start_s,
            "end_date": end_s,
            "lookback_days": days,
        },
        "configured_sources": len(configured),
        "ok": all_ok,
        "has_errors": any(not bool(src["ok"]) for src in configured),
        "sources": sources,
    }
    storage.set_meta("google_enrichment", summary)
    return summary

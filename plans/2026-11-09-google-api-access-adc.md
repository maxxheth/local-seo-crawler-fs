# Plan: Multi-Profile Google API Access for `local-seo-crawler-fs`

**Date:** 2026-11-13 *(updated from 2026-11-09)*
**Status:** Implemented (2026-11-13) — Multi-Profile OAuth & `.env` Configuration
**Runs from:** local workstation (multi-site Local SEO audits)
**APIs:** Google Analytics 4 (`analyticsdata.googleapis.com`), Google Search Console (`webmasters/v3`), Google Business Profile (`mybusinessaccountmanagement`, `mybusinessbusinessinformation`, `businessprofileperformance`), plus optional Tag Manager verification (`tagmanager.googleapis.com`).

## Why This Architecture Replaced `gcloud` ADC

1. **Multi-Site Isolation (`profiles.yaml` + `.env`):** `local-seo-crawler-fs` audits multiple independent sites and Google organizations (e.g. Groundhogs TX, Face to Face Spa, Marvenio). A single workstation ADC file cannot cleanly isolate client credentials across brands, so profiles live in `~/.config/local-seo-crawler/google/` (`0700` dir, `0600` manifests/tokens) and are selected per run via `.env` (`GOOGLE_AUTH_PROFILE`, `GOOGLE_GA4_PROPERTY_ID`, `GOOGLE_GSC_SITE_URL`, `GOOGLE_GBP_ACCOUNT_ID`) or `--env-file` / `--google-profile`.
2. **`gcloud auth application-default login` Scope Trap:** `gcloud` (SDK 593.0.0) rejects custom `--scopes` unless `https://www.googleapis.com/auth/cloud-platform` is included. Shared/restricted OAuth clients without `cloud-platform` fail under `gcloud`, so `crawler-google-auth` uses `google_auth_oauthlib.flow.InstalledAppFlow` over loopback `http://localhost` with each profile's exact scope list.
3. **`gcloud auth application-default revoke` Cross-Client Hazard:** `gcloud ... revoke` calls Google's remote `https://oauth2.googleapis.com/revoke` endpoint, which in shared GCP projects (such as `macro-anchor-513817-n3`) can invalidate tokens issued to other OAuth clients in the same project. `crawler-google-auth logout` zero-overwrites, fsyncs, and unlinks only the target profile's local `token.json` by default (`remote_revoke_allowed: false`).
4. **Decoupling from Marvenio Persistent Platform Auth:** Marvenio's persistent server-to-server broker (`https://google-connect.marvenio.com` in AWS account `030796672277`, `aws-nexus` `scripts/google_reporting_broker/signed_canary.py`) is tracked separately from this standalone workstation crawler.

## Profile Store Layout (`~/.config/local-seo-crawler/google/`)

```text
~/.config/local-seo-crawler/google/      (0700)
├── profiles.yaml                        (0600, non-secret profile metadata & scope pins)
├── groundhogs/
│   ├── client.json                      (0600, Desktop OAuth client JSON)
│   └── token.json                       (0600, offline authorized_user refresh credential)
└── marvenio/
    └── token.json                       (0600; client_secrets_file points to ~/.config/marvenio/google/max-local-reporting-oauth-client.json)
```

- Personal `~/.config/gcloud/application_default_credentials.json` is never read or modified.
- `GOOGLE_PROFILES_DIR` can override `~/.config/local-seo-crawler/google` per `.env` if needed.

## `.env` Per-Site Variables

See `.env.example`:

```dotenv
ENABLE_GOOGLE_ENRICHMENT=false
GOOGLE_AUTH_PROFILE=
GOOGLE_PROFILES_DIR=
GOOGLE_GA4_PROPERTY_ID=
GOOGLE_GSC_SITE_URL=
GOOGLE_GBP_ACCOUNT_ID=
GOOGLE_GBP_LOCATION_IDS=
GOOGLE_LOOKBACK_DAYS=28
```

Pass `--env-file /path/to/.env.site` to either `crawler-google-auth` or `local-seo-crawler` to switch sites cleanly without editing the root `.env`. Pass `--enable-google-enrichment` (or set `ENABLE_GOOGLE_ENRICHMENT=true`) to fetch read-only Google signals into `crawl_results.db` after crawling.

## CLI Workflow (`crawler-google-auth`)

### 1. Register a Profile
```bash
# Generic site profile (GA4 + Search Console + Business Profile)
uv run crawler-google-auth profile add groundhogs \
  --client-file ~/.config/edfx/groundhogs-oauth-client.json \
  --copy-client

# Marvenio preset (pins project macro-anchor-513817-n3, Desktop client ID, admin@marvenio.com, exact 7 scopes)
uv run crawler-google-auth profile add marvenio \
  --client-file ~/.config/marvenio/google/max-local-reporting-oauth-client.json \
  --preset marvenio
```

### 2. List & Check Status
```bash
uv run crawler-google-auth profile list
uv run crawler-google-auth status --profile marvenio
uv run crawler-google-auth status --env-file .env.facetofacespa --live
```

### 3. Sign In (Browser Loopback OAuth)
```bash
uv run crawler-google-auth login --profile marvenio
# Or via site .env:
uv run crawler-google-auth login --env-file .env.groundhogs
```
- Verifies granted scopes and OIDC `userinfo` (`email_verified=true` plus any pinned `expected_email` / `expected_hd`) **before** saving `0600` `token.json`.

### 4. Bounded Read-Only Verification (`verify`)
```bash
# Face to Face Spa via Marvenio profile
uv run crawler-google-auth verify --profile marvenio \
  --ga4-property 562840962 \
  --gsc-site sc-domain:facetofacespa.com \
  --gbp-account auto \
  --json

# Marvenio site via Marvenio profile (or values from .env)
uv run crawler-google-auth verify --profile marvenio \
  --ga4-property 557343330 \
  --gsc-site sc-domain:marvenio.com \
  --json
```
- `GoogleSignalsClient` enforces strictly read-only endpoints/methods, omits `X-Goog-User-Project` (so `cloud-platform` scope is not required), scrubs Bearer/token strings from errors, and classifies GBP `HTTP 429 RESOURCE_EXHAUSTED` as `GBP_QUOTA_ZERO_QPM`.

### 5. Optional Post-Crawl SQLite Enrichment (`ENABLE_GOOGLE_ENRICHMENT`)
When `ENABLE_GOOGLE_ENRICHMENT=true` (or `--enable-google-enrichment`) is enabled for a crawl, `CrawlerOrchestrator` runs `collect_and_persist_google_signals(...)` post-crawl and upserts normalized rows into deterministic tables inside `crawl_results.db`:
- `google_ga4_pages` — keyed by `(property_id, metric_date, landing_page, channel_group)`
- `google_gsc_pages` — keyed by `(site_url, metric_date, page_url)`
- `google_gbp_locations` — keyed by `(location_name)` with location metadata + daily performance time-series JSON
- `crawl_metadata['google_enrichment']` — sanitized run summary (`window`, per-source `ok`/`rows_saved`/`external_gate`)

Failures on an individual Google API (such as `GBP_QUOTA_ZERO_QPM`) or an unauthenticated profile are recorded in `crawl_metadata` with OAuth tokens scrubbed and never abort crawl artifact export.

### 6. Local Credential Removal (`logout`)
```bash
uv run crawler-google-auth logout --profile marvenio
```
- Overwrites `<profile>/token.json` with zero bytes, fsyncs, and unlinks locally without calling Google's remote `/revoke` endpoint.

## Google Business Profile Note

- Each owning GCP project still requires Google's **Basic API Access** approval (`0 QPM` -> `HTTP 429 RESOURCE_EXHAUSTED` until approved).
- If you also run Marvenio's internal AWS reporting broker checks (`aws-nexus` PR 70), keep that workflow separate via `aws sso login --profile marvenio-internal-admin` and `scripts/google_reporting_broker/signed_canary.py`.

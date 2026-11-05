# Local SEO Crawler FS (`local-seo-crawler-fs`)

A high-performance, concurrent Python Local SEO crawling and auditing engine designed for production SEO audits, competitor page analysis, NAP citation verification, and Google Local 3-Pack SERP extraction.

Built with pure Python 3.12+, `httpx`, `BeautifulSoup4`, `pydantic`, `openpyxl`, and FlareSolverr CAPTCHA bypass integration.

---

## Key Features

- **Concurrent Multi-Mode Engine**:
  - `competitor`: Deep site crawl with link graph analysis, SEO auditing, and duplicate content detection.
  - `citations`: Parallel NAP citation verification across major local directories.
  - `serp`: Extract Google Local 3-Pack rankings, organic positions, and PAA queries.
- **Relational Multi-Worksheet Excel (`.xlsx`) Export**:
  - Exports relational multi-tab spreadsheets with styled headers (`#1E293B`), auto-filters, frozen headers, and soft status coloring.
  - 1 atomic fact per cell—nested arrays/lists (headings, issues, links, schema, NAP, social) are normalized into dedicated relational worksheets keyed by URL.
- **SQLite Single Source of Truth**:
  - All crawled pages, internal link edges, citations, and EVD-01 evidence are committed directly into SQLite with WAL mode and idempotent sync.
- **Flexible Serialization & Archiving**:
  - Supports `json`, `yaml`, `csv`, and `xlsx` / `excel` exports.
  - Multi-part chunked dumps and automatic archive packaging (`zip`, `tar.gz`, `tar.bz2`, `tar.xz`, `tar.zst`).
- **EVD-01 Verifiable Audit Proofs**:
  - Every finding is backed by SHA-256 cryptographic proofs, DOM selectors, HTTP statuses, and verbatim text snippets.

---

## Requirements

- Python 3.11+ (Python 3.12 or 3.13 recommended)
- `uv` (recommended) or `pip`
- Docker (for FlareSolverr)
- *(Optional)* **Redis** for distributed queue scaling

## Quickstart

```bash
# 1. Set up Python virtual environment and dependencies
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# 2. Start FlareSolverr & Redis (via Docker Compose)
docker compose up -d

# 3. Verify citations concurrently (with Excel export)
local-seo-crawler --mode citations --input citations.json --export-format xlsx --output-dir ./artifacts

# 4. Large-scale competitor crawl with SEO audit & link graph analysis
local-seo-crawler --mode competitor --domain competitor.com \
  --max-pages 1500 \
  --chunk-size 20 \
  --concurrency 6 \
  --export-format xlsx \
  --archive-format zip \
  --output-dir ./artifacts

# 5. Local 3-Pack & entity test
local-seo-crawler --mode serp --query "drain cleaning dallas" --location "Dallas, TX" --export-format xlsx --output-dir ./artifacts

# 6. Deterministic report from an existing crawl database (never crawls)
local-seo-crawler --mode report --db ./artifacts/crawl_results.db
#    writes crawl_report.md + report_summary.json next to the database;
#    --verify-narrative notes.md exits non-zero if a hand/AI-written narrative contradicts the data
```

## Post-Crawl Analysis

After link verification, a post-crawl stage (`src/post_crawl.py`) runs over the whole page set and link graph:

- **Page classification** (`src/classifier.py`): `clinic_location`, `clinic_service`, `directory_hub`,
  `utility_page`, `service_page`, `blog_post`, `blog_archive`, `other`, with `page_type_confidence` and
  `page_type_reasons`. Directory hubs are recognised from **body-placement** links only (site-wide menus
  are ignored); clinic pages are the hubs' entries, scored with DOM signals (local phone, address, map,
  "in City, ST" headings, booking CTA, child service pages). `--locations-file` overrides detection.
  If no directory hub is found (e.g. a small site listing fewer than `--hub-min-links` locations),
  clinics are inferred from pages showing a local address plus a map or local phone, at lower
  confidence; the report states which detection method was used, or that none found clinics.
- **Location status** for clinic pages: `open`, `coming_soon`, `temporarily_closed`, `moved`,
  `ownership_transferred`, `closed`, `unknown` — from `--locations-file`, then directory labels, then
  status notices in the page's opening section. A move is `moved` (with `moved_to`) only when the page
  displays another clinic page's address; a move to a new address is `open` with `relocated = true`.
- **Site-context rules** (`seo_auditor.audit_site_context`): Missing LocalBusiness schema (open/unknown
  clinics only), closed/moved and ownership-transferred clinic notices, missing visible address, stale
  status notices, duplicate clinic addresses, indexable thank-you pages, prototype/staging pages (whole
  slug words only), and staging hostname references.
- **Link-graph rules** (`storage.post_crawl_sync`): broken targets (HTTP ≥ 400 *or* transport failures
  such as `tls_hostname_mismatch`, `dns_failure`, `timeout`), malformed relative links, and internal
  links to redirects.

Link resolution follows RFC 3986: every scheme-less `href` resolves against `<base href>` or the final URL
after redirects, exactly as browsers do. A scheme-less href that looks like a domain (`www.youtube.com/…`)
is annotated `malformed_relative` and reported, not silently rewritten. Such targets are never queued for
crawling (on sites that answer unknown paths with 200 they would grow without bound); each is status-checked
once after the crawl instead.

Broken means HTTP ≥ 400 or a recorded transport failure. A page that failed without one (status 0, e.g. a
challenge page or exhausted retries) is "not verified", not broken.

## Configuration & Environment Variables

You can configure options in `.env` or pass CLI flags:

| Parameter | CLI Flag | Env Var | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Competitor Mode Toggle** | `--disable-competitor` | `ENABLE_COMPETITOR` | `true` | Toggle competitor crawling mode on or off |
| **Max Pages** | `--max-pages` | `MAX_PAGES` | `1000` | Target page cap for site crawl |
| **Max Depth** | `--max-depth` | `MAX_DEPTH` | `5` | Maximum crawl link depth |
| **Chunk Size** | `--chunk-size` | `CHUNK_SIZE` | `15` | Number of URLs assigned per worker batch |
| **Sitemap Seeding** | `--no-sitemap` | `USE_SITEMAP` | `true` | Auto-discover `robots.txt` and `sitemap.xml` |
| **Fast HTTP First** | `--no-fast-http` | `FAST_HTTP_FIRST` | `true` | Try direct/proxy HTTP before FlareSolverr |
| **Queue Backend** | `--redis-url` | `REDIS_URL` | `""` (SQLite) | Set Redis connection string for distributed queue |
| **Export Format** | `--export-format` | `EXPORT_FORMAT` | `json` | `json`, `yaml`, `csv`, `xlsx`, or `excel` |
| **Export Mode** | `--export-mode` | `EXPORT_MODE` | `single` | `single`, `multi`, or `chunked` |
| **Export Chunk Size** | `--export-chunk-size`| `EXPORT_CHUNK_SIZE`| `250` | Record count per part file in multi mode |
| **Archive Format** | `--archive-format` | `ARCHIVE_FORMAT` | `none` | `none`, `zip`, or `tar` |
| **Tar Compression** | `--tar-compression` | `TAR_COMPRESSION` | `gz` | `gz`, `bz2`, `xz`, `zst`, or `none` |
| **Title Length Range** | `--title-min-len` / `--title-max-len` | `SEO_TITLE_MIN_LEN` / `SEO_TITLE_MAX_LEN` | `30` / `60` | Recommended title character bounds |
| **Meta Desc Length Range** | `--desc-min-len` / `--desc-max-len` | `SEO_DESC_MIN_LEN` / `SEO_DESC_MAX_LEN` | `120` / `160` | Recommended description character bounds |
| **Thin Content Word Count**| `--min-word-count` | `SEO_MIN_WORD_COUNT` | `300` | Minimum word count before flagging thin content |
| **Duplicate Content Ratio**| `--dup-threshold` | `SEO_DUP_THRESHOLD` | `0.85` | Dice token similarity ratio (0.0–1.0) |
| **Slow Response Warnings** | `--slow-warn-ms` / `--slow-err-ms` | `SEO_SLOW_WARN_MS` / `SEO_SLOW_ERR_MS` | `1000` / `3000` | Response time thresholds in ms |
| **Large Page Warnings** | `--large-warn-bytes` / `--large-err-bytes` | `SEO_LARGE_WARN_BYTES` / `SEO_LARGE_ERR_BYTES` | `1MB` / `3MB` | HTML payload size thresholds in bytes |
| **Directory Hub Threshold** | `--hub-min-links` | `HUB_MIN_LINKS` | `10` | Distinct body links to same-shape pages for a directory hub |
| **Forced Directory Hubs** | `--directory-url` (repeatable) | — | — | Treat these URLs as directory hubs |
| **Locations File** | `--locations-file` | `LOCATIONS_FILE` | `""` | Authoritative CSV/JSON store list (url, name, address, phone, status) |
| **Status Markers** | `--status-markers` | — | built-in English phrases | JSON file/inline JSON of status phrases, merged over defaults |
| **Utility / Prototype Tokens** | `--utility-tokens` / `--prototype-tokens` | — | see `src/classifier.py` | Comma-separated whole slug words; `^tok` = segment starts with it, `tok$` = ends with it, `^tok$` = whole segment (defaults match `new`/`old`/`copy` only as suffixes, so `/new-braunfels/` is not a prototype) |
| **Prototype Allowlist** | `--prototype-allowlist` (repeatable) | — | — | URLs exempt from prototype detection |
| **Staging Host Patterns** | `--staging-host-patterns` | — | `staging*,*.staging.*,*-staging.*,*.on-forge.com,dev.*` | Glob patterns for staging hosts |
| **Corporate Share** | `--corporate-share` | `CORPORATE_SHARE` | `0.40` | Share of pages above which a phone/address counts as corporate |
| **Timing Sample** | `--timing-sample` | `TIMING_SAMPLE` | `0` (off) | Sequential, cache-busted TTFB sample of N URLs after the crawl |
| **PageSpeed Insights** | `--psi-api-key` / `--psi-sample` | `PSI_API_KEY` / `PSI_SAMPLE` | off | Chrome UX Report field data (TTFB, LCP, INP, CLS) |
| **Report Mode** | `--mode report --db` / `--output` / `--format md\|json` / `--sample-size` / `--verify-narrative` | — | `md`, 10 | Render a report from an existing database |

## Changelog

### 2026-11-05: Crawler accuracy & deterministic reporting

Behaviour changes (current behaviour was a defect):

- **Link resolution (WS1):** scheme-less bare-domain hrefs are no longer rewritten to `https://<href>`;
  they resolve relative to the document base (`<base href>` or the final URL after redirects) and are
  reported as "Malformed Relative Link" (error if the resolved URL fails, warning otherwise).
- **Transport-broken links (WS2):** link targets that fail with TLS/DNS/timeout errors — including
  redirects whose final hop fails — now count as broken (`target_status = 0`, `target_error_type`).
- **Timing severity (WS6):** crawler-measured timing is now "Slow Crawl Response (crawler-measured)"
  at **info** severity; only `--timing-sample` measurements can raise a warning.
- **Context rules moved post-crawl (WS4/WS5):** LocalBusiness, thank-you and prototype rules now run in
  `audit_site_context` from page type instead of URL substrings.

Additive: `page_type`/`location_status` columns and fields, `Page Types` Excel sheet, internal-link
columns (`malformed_relative`, `intended_host`, `raw_href`, `target_error_type`), `duplicate_pairs`
table, "Internal Link to Redirect" info issues, and `--mode report`. Schema changes are idempotent
`ALTER TABLE … ADD COLUMN` migrations; older databases still open.

## Running Tests

```bash
source .venv/bin/activate
pytest -v
```

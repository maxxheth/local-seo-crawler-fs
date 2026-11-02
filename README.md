# Local SEO Crawler FS (`local-seo-crawler-fs`)

High-throughput, concurrent multi-source Local SEO scraper orchestrating worker slots and deterministic parsing in **Python**. Optimized for large site crawls (1,000+ pages) with chunked frontier queues, SQLite storage checkpoints, fast-HTTP escalation, on-page SEO defect auditing, link graph analysis, near-duplicate page detection, and multi-format archiving.

## Architecture

```
                 Concurrent Orchestrator (src/orchestrator.py)
                     │ ├── FrontierQueue (src/queue.py) [SQLite / Redis]
                     │ ├── SessionPool (src/session_pool.py) [threading.Lock]
                     │ ├── Sitemap & Robots Seeder (src/sitemap.py)
                     │ ├── SQLite Result Storage (src/storage.py) [WAL Mode]
                     │ └── SEO Issue & Duplication Auditor (src/seo_auditor.py)
                     ▼
          Core Scraper Module (src/scraper.py)
                     │
         ┌───────────┴──────────────┐
         ▼                          ▼
 FlareSolverr Client        Pluggable Adapters (src/adapters/)
 (src/flaresolverr.py)      ├── CitationAdapter (Yelp, BBB, YellowPages)
         │                  ├── CompetitorSiteAdapter (On-site 1,000+ pages)
         ▼                  └── LocalSerpAdapter (Google 3-Pack, SERP)
FlareSolverr Container              │
(Headless Chromium + Proxies)       ▼
                            Output Manager (src/output.py)
                            ├── SQLite DB: crawl_results.db (pages, links, issues)
                            ├── Single / Multi-Part JSON, YAML, CSV
                            └── Archives: .zip, .tar.gz, .tar.bz2, .tar.xz, .tar.zst
```

## Features

- **Frontier Queue (`src/queue.py`):** Hybrid queue supporting SQLite (default, zero-config) and Redis (`REDIS_URL` for multi-worker distributed setups) with atomic URL deduplication and chunked popping.
- **Fast-HTTP Escalation & Session Recycling:** Scrapers use keep-alive `httpx.Client` connections and escalate to FlareSolverr only when encountering Cloudflare/CAPTCHA challenges, caching solved `cf_clearance` cookies across the worker's chunk.
- **Comprehensive On-Page SEO Extraction (`src/adapters/competitor.py`):**
  - **Meta & Technical:** `title`, `meta_description`, `h1`/`h2`/`h3`, `canonical_url`, `<html lang>`, `charset`, `viewport`, `meta_robots`, `generator` (CMS detection), `hreflang`, OpenGraph, Twitter Cards, `response_time_ms`, `page_size_bytes`.
  - **Image & Accessibility Audit:** Distinguishes missing `alt` attributes (defects) from `alt=""` (valid decorative), checks dimensions (`width`/`height`).
  - **Marketing & Call-Tracking Stack:** Detects GA4, GTM, Facebook Pixel, Microsoft Clarity, Hotjar, Mixpanel, CallRail, WhatConverts, ServiceTitan, Housecall Pro, Podium, Birdeye.
  - **Link Graph & Placement:** Classifies outlinks by DOM placement (`navigation`, `footer`, `body`, `image`), tracks `nofollow`, extracts anchor text, and records target HTTP status codes.
- **Automated SEO Rule Engine & Duplication Analyzer (`src/seo_auditor.py`):**
  - Flags missing/short/long titles and descriptions, thin content (<300 words), multiple or missing H1s, `noindex`/`nofollow`, canonical mismatches, missing viewports, slow response times, and broken internal links.
  - Token-based Dice similarity algorithm detects near-duplicate / doorway pages using rarity prefix filters without $O(N^2)$ slowdowns.
- **Incremental SQLite Checkpoints (`src/storage.py`):** Results, links, issues, and evidence are committed to SQLite (`WAL` mode) incrementally after each page/chunk, enabling resumable crawls and minimal memory consumption.
- **Configurable Multi-Format Exporter (`src/output.py`):** Supports `json`, `yaml`, and `csv` export in `single` or `chunked`/`multi`-file splits, with automated archiving in `zip` or `tar` (compressed with `gz`, `bz2`, `xz`, `zst`, or uncompressed).

## Requirements

- **Python 3.10+** (managed via `uv`)
- **FlareSolverr** running on `http://localhost:8193` (or configured via `FLARESOLVERR_URL`)
- *(Optional)* **Redis** for distributed queue scaling

## Quickstart

```bash
# 1. Set up Python virtual environment and dependencies
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# 2. Start FlareSolverr & Redis (via Docker Compose)
docker compose up -d

# 3. Verify citations concurrently
local-seo-crawler --mode citations --input citations.json --concurrency 4 --output-dir ./artifacts

# 4. Large-scale competitor crawl with SEO audit & link graph analysis
local-seo-crawler --mode competitor --domain competitor.com \
  --max-pages 1500 \
  --chunk-size 20 \
  --concurrency 6 \
  --export-format json \
  --export-mode multi \
  --export-chunk-size 250 \
  --archive-format tar \
  --tar-compression gz \
  --output-dir ./artifacts

# 5. Local 3-Pack & entity test
local-seo-crawler --mode serp --query "drain cleaning dallas" --location "Dallas, TX" --output-dir ./artifacts
```

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
| **Export Format** | `--export-format` | `EXPORT_FORMAT` | `json` | `json`, `yaml`, or `csv` |
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

## Running Tests

```bash
source .venv/bin/activate
pytest -v
```

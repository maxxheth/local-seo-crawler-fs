# TASK SPECIFICATION: Concurrent Multi-Source Local SEO Scraper (`local-seo-crawler-fs`)

## 1. Context & Objectives
You are building `local-seo-crawler-fs`, a high-throughput, hybrid **Mojo + Python** concurrent CLI scraping utility:
- **Concurrent Orchestrator (Mojo):** Handles CLI dispatch, target manifest scheduling, high-concurrency worker pool management, retry backoff state machines, and thread-safe FlareSolverr session slot leasing.
- **Core Scraper & Parsers (Python):** Handles FlareSolverr `/v1` HTTP transport, DOM rendering extraction (`scrapling` / `selectolax` / `beautifulsoup4`), pluggable domain adapters, and Pydantic schema validation/serialization.

### Foundation
The system is architecturally derived from `upwork-scraper-fs`:
- Uses **Mojo** for systems-level concurrent orchestration, parallel task scheduling, and strict session-slot locking across worker threads.
- Keeps the core scraper in **Python**, invoked from Mojo (via Mojo's `python` interop module or managed Python worker bridges) to preserve full compatibility with Python's scraping ecosystem.
- Uses FlareSolverr (`/v1` API) backed by headless Chromium to render JavaScript (Nuxt, Next, React SPA/SSR) and solve Cloudflare/anti-bot challenges.
- Implements round-robin proxy rotation across a pool of rotating/ISP proxy ports (e.g., Decodo).
- Reuses persistent FlareSolverr sessions per proxy port to avoid recurring TLS handshakes and Captcha penalties.
- Uses `scrapling` / `selectolax` / `beautifulsoup4` in the Python core for deterministic HTML parsing.

### Core Problem Solved
Standard HTTP crawlers get blocked by directories (Yelp, YellowPages, BBB, Angi, Houzz) and bot-protected competitor sites. This utility uses a Mojo concurrent orchestrator to route parallel crawl jobs through persistent FlareSolverr sessions across isolated proxy ports, delegating DOM extraction to the Python core scraper and returning raw rendered DOMs and structured JSON artifacts specifically engineered for ingestion by our downstream Local SEO agent skills (`local-seo-audit`, `lseo-doctrine`, `local-citation-tracker`, `core-30-architect`).

---

## 2. Mojo Concurrent Orchestrator & FlareSolverr Session Architecture

### A. Mojo Worker Pool & Queue (`src/orchestrator.mojo`)
- Implement the concurrent worker pipeline and task scheduler in **Mojo** (using `algorithm.parallelize` / Mojo async task primitives).
- Manage a configurable worker pool size (`--concurrency N`, default: `4`).
- Support a multi-URL/multi-source input feed (JSON manifest, CSV, or CLI targets).
- Bridge into the Python core scraper (`src/scraper.py`) via `from python import Python, PythonObject`, passing leased session credentials (`session_id`, `proxy_url`, `proxy_port`) and target metadata to execute each scrape unit of work.

### B. FlareSolverr Session Pooling (`src/session_pool.mojo` + `src/flaresolverr.py`)
- FlareSolverr sessions are expensive (each spins up/maintains a Chrome tab). **Do not create a new FlareSolverr session per URL.**
- Maintain a pool of `M` persistent FlareSolverr sessions in Mojo mapped 1:1 to available proxy ports from `proxies.txt`:
  `struct SessionSlot { var session_id: String; var proxy_url: String; var proxy_port: Int; var in_use: Bool; var active_requests: Int; var error_count: Int }`
- The Mojo orchestrator manages exclusive slot leasing: workers acquire an available `SessionSlot` under a mutex/atomic lock, pass the leased `session_id` and `proxy_url` to the Python `flaresolverr.py` client to dispatch the `request.get` command, and release the slot upon completion.
- Implement exponential backoff retry in the Mojo orchestrator (up to `--max-retries`, default: 3). If the Python scraper reports a Cloudflare ban or timeout (status 500/timeout), the Mojo orchestrator instructs `flaresolverr.py` to destroy that FlareSolverr session, re-create it with a fresh proxy port, update the `SessionSlot`, and retry.

---

## 3. Python Core Scraper & Pluggable Scrape Adapters (`src/adapters/`)

The core scraping engine, HTTP transport, and HTML extraction logic remain in **Python**, exposing clean callable entrypoints for the Mojo orchestrator and modular adapters for three distinct scraping targets:

### 1. `CitationAdapter` (Directory & NAP Verification)
- **Targets:** Yelp, BBB, YellowPages, Bing Places, Apple Maps, Houzz, Angi, Facebook, Dun & Bradstreet, Chamber of Commerce.
- **Extraction:**
  - HTTP status & challenge resolution state (`live`, `blocked`, `not_found`, `captcha`).
  - Target business name, visible street address, city, state, zip code.
  - Normalized phone numbers (primary and secondary digits extracted from raw DOM and `tel:` links).
  - Business operating hours, website link destination, verification badges (BBB accredited, verified owner).
  - Verbatim text snippet containing the NAP block.

### 2. `CompetitorSiteAdapter` (On-Site & Core 30 Analysis)
- **Targets:** Competitor homepages, category hubs, service pages, and contact pages.
- **Extraction:**
  - Page URL, final redirected URL, HTTP status code.
  - Title tag, meta description, canonical URL tag.
  - Headings hierarchy: H1, H2, H3 tags in DOM order.
  - Full Schema.org graph extraction (parsed JSON-LD script tags + microdata).
  - On-page NAP detection (footer/header address strings, raw phone links).
  - Word count, internal link count, external link count.
  - SSR validation: check if primary content is in raw HTML vs. client-rendered only.

### 3. `LocalSerpAdapter` (Google Local 3-Pack & Maps Entity Test)
- **Targets:** Google Maps search & localized SERPs (`"[Service] [City]"`, `"[Category] [City]"`).
- **Extraction:**
  - Top 3 Local Pack rankings: Business names, rating, review count, address/submarket snippet, categories.
  - Top 10 organic URLs, titles, and snippets.
  - "People Also Ask" (PAA) questions and direct answer snippets.

---

## 4. Required Output Artifacts (Strict Schema Compatibility)

All runs must output deterministic JSON files into `--output-dir` timestamped ISO-8601, formatted to match the exact ingestion expectations of Hermes Local SEO skills:

### Artifact 1: `citations_manifest.json` (for `local-citation-tracker` & `CIT-01`)
Matches the existing `citations.json` schema:
```json
{
  "business": "Target Business Name",
  "checked_at": "2026-10-27T14:30:00Z",
  "summary": {
    "total": 12,
    "live": 10,
    "blocked": 1,
    "mismatch": 1
  },
  "citations": [
    {
      "platform": "yelp",
      "label": "Yelp",
      "url": "https://www.yelp.com/biz/example",
      "status": "live",
      "nap_match": "match",
      "observed": {
        "name": "Target Business Name",
        "address": "123 Main St, Dallas, TX 75001",
        "phone": "(214) 555-0199"
      },
      "confidence": "high",
      "evidence_snippet": "Target Business Name - 123 Main St, Dallas, TX - (214) 555-0199",
      "proxy_port": 10002
    }
  ]
}
```

### Artifact 2: `site_crawl_artifact.json` (for `local-seo-audit`, `core-30-architect`, `TEC-01`)
Captures full technical and structural data:
```json
{
  "target_domain": "competitor.com",
  "crawl_depth": 2,
  "pages": [
    {
      "url": "https://competitor.com/drain-cleaning-dallas/",
      "status_code": 200,
      "title": "Drain Cleaning Dallas | 24/7 Rooter Service",
      "meta_description": "Fast Dallas drain cleaning...",
      "h1": ["Emergency Drain Cleaning in Dallas, TX"],
      "h2": ["Clogged Drain Symptoms", "Our Hydro-Jetting Process", "Service Areas"],
      "word_count": 842,
      "internal_outlinks_count": 34,
      "external_outlinks_count": 4,
      "schema_types": ["PlumbingService", "LocalBusiness", "FAQPage"],
      "schema_jsonld": [ ... ],
      "nap_found": {
        "phones": ["+12145550199"],
        "addresses": ["Dallas, TX"]
      }
    }
  ]
}
```

### Artifact 3: `evidence_manifest.json` (for `lseo-doctrine` Rule `EVD-01`)
Enforces the Absolute Evidence Rule:
```json
{
  "manifest_id": "evd_20261027_143000",
  "records": [
    {
      "claim_type": "citation_verification",
      "source_url": "https://www.bbb.org/us/tx/dallas/profile/...",
      "timestamp": "2026-10-27T14:30:15Z",
      "http_status": 200,
      "dom_selector": ".business-details-contact",
      "verbatim_text": "Accredited Since: 2018 | Phone: (214) 555-0199",
      "hash_sha256": "abc123..."
    }
  ]
}
```

---

## 5. CLI & Execution Specifications

### Project Structure
```text
local-seo-crawler-fs/
├── pixi.toml / mojoproject.toml  # Mojo toolchain & hybrid environment config
├── pyproject.toml                # Python core dependencies via uv (httpx, scrapling, selectolax, beautifulsoup4, pydantic, rich)
├── Dockerfile                    # Modular MAX/Mojo + Python slim runner
├── docker-compose.yml            # Hybrid Scraper + flaresolverr/flaresolverr:latest service
├── proxies.txt                   # Decodo/proxy endpoint list (user:pass@host:port)
├── src/
│   ├── main.mojo                 # Mojo CLI entry point & argument parser
│   ├── orchestrator.mojo         # Mojo concurrent worker pool, task queue & retry state machine
│   ├── session_pool.mojo         # Mojo thread-safe SessionSlot pool & proxy-port lease manager
│   ├── scraper.py                # Python core scraper bridge invoked by Mojo workers
│   ├── config.py                 # Environment, proxy, timeout loaders
│   ├── flaresolverr.py           # FlareSolverr /v1 HTTP client (session create/destroy & request dispatch)
│   ├── models.py                 # Pydantic models for targets, results, artifacts
│   ├── adapters/
│   │   ├── citation.py           # Directory & citation parsers
│   │   ├── competitor.py         # Competitor on-page & Core 30 parsers
│   │   └── serp.py               # Local Pack & Maps SERP parsers
│   └── output.py                 # Normalization & deterministic JSON artifact exporters
└── tests/
    ├── test_orchestrator.mojo    # Mojo session pool locking & concurrency tests
    └── test_parsers.py           # Python adapter & schema unit tests
```

### CLI Interface
```bash
# Verify citations concurrently from an existing citations.json via Mojo orchestrator
mojo run src/main.mojo --mode citations --input citations.json --concurrency 4 --output-dir ./artifacts

# Deep audit / crawl of competitor service silos
mojo run src/main.mojo --mode competitor --domain competitor.com --max-pages 30 --concurrency 3

# Local 3-Pack & entity test check
mojo run src/main.mojo --mode serp --query "drain cleaning dallas" --location "Dallas, TX"

# Optional: Compile standalone orchestrator binary
mojo build src/main.mojo -o bin/local-seo-crawler
./bin/local-seo-crawler --mode citations --input citations.json --concurrency 4 --output-dir ./artifacts
```

---

## 6. Acceptance Criteria
1. **Zero Uncaught FlareSolverr Timeouts:** Handled gracefully across the Mojo-Python boundary with automatic FlareSolverr session recycle and exponential backoff retry.
2. **Mojo Concurrency Safety:** FlareSolverr `SessionSlot` instances in Mojo are pinned 1:1 to individual proxy ports and locked during in-flight requests; no two concurrent Mojo workers share the same `session_id` at the same moment, and Python interop calls are properly synchronized.
3. **Clean Mojo–Python Separation:** All concurrency, queuing, retry orchestration, and session slot leasing live in Mojo (`*.mojo`), while FlareSolverr HTTP calls, DOM parsing (`scrapling`/`selectolax`/`bs4`), and Pydantic artifact serialization remain in Python (`src/*.py`).
4. **Format Parity:** Output `citations_manifest.json` can directly overwrite `local-citation-tracker/citations.json` with zero schema translation.
5. **No Hallucinations / EVD-01 Enforcement:** Every extracted field must preserve the exact source URL, HTTP status code, and verbatim snippet evidence.

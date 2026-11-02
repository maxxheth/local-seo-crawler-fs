# Local SEO Crawler FS (`local-seo-crawler-fs`)

High-throughput, concurrent CLI scraper orchestrating worker slots and deterministic parsing in **Python**.

## Architecture

```
                 Concurrent Orchestrator (src/orchestrator.py)
                     │ ├── SessionPool (src/session_pool.py) [threading.Lock]
                     │ └── Worker Queue & Retry State Machine
                     ▼
          Core Scraper Module (src/scraper.py)
                     │
         ┌───────────┴──────────────┐
         ▼                          ▼
 FlareSolverr Client        Pluggable Adapters (src/adapters/)
 (src/flaresolverr.py)      ├── CitationAdapter (Yelp, BBB, YellowPages)
         │                  ├── CompetitorSiteAdapter (Core 30 on-site)
         ▼                  └── LocalSerpAdapter (Google 3-Pack, SERP)
FlareSolverr Container              │
(Headless Chromium + Proxies)       ▼
                            Output Manager (src/output.py)
                            ├── citations_manifest.json
                            ├── site_crawl_artifact.json
                            ├── serp_artifact.json
                            └── evidence_manifest.json (EVD-01)
```

- **Concurrent Orchestrator (`src/orchestrator.py`, `src/session_pool.py`):** Worker pool management (`ThreadPoolExecutor`), sliding-window task scheduling, thread-safe `SessionSlot` proxy leasing, and session recycling retry state machine.
- **Core Scraper & Parsers (`src/scraper.py`, `src/adapters/`):** FlareSolverr `/v1` HTTP protocol, HTML rendering extraction using `scrapling` and `selectolax` (lexbor engine), Pydantic schemas, and deterministic JSON export.

## Requirements

- **Python 3.10+** (managed via `uv`)
- **FlareSolverr** running on `http://localhost:8193` (or configured via `FLARESOLVERR_URL`)

## Quickstart

```bash
# 1. Set up Python virtual environment and dependencies
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# 2. Start FlareSolverr (via Docker Compose)
docker compose up -d flaresolverr

# 3. Verify citations concurrently
local-seo-crawler --mode citations --input citations.json --concurrency 4 --output-dir ./artifacts

# 4. Audit competitor domain
local-seo-crawler --mode competitor --domain competitor.com --concurrency 3 --output-dir ./artifacts

# 5. Local 3-Pack & entity test
local-seo-crawler --mode serp --query "drain cleaning dallas" --location "Dallas, TX" --output-dir ./artifacts
```

## Running Tests

```bash
source .venv/bin/activate

# Run adapter, schema, orchestrator, and session pool unit tests
pytest -v
```

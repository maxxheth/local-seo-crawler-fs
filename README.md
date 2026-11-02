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

## Running Tests

```bash
source .venv/bin/activate
pytest -v
```

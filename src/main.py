"""CLI Entry Point for local-seo-crawler-fs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from src import scraper
from src.classifier import DEFAULT_PROTOTYPE_TOKENS, DEFAULT_STATUS_MARKERS, DEFAULT_UTILITY_TOKENS
from src.seo_auditor import DEFAULT_STAGING_HOST_PATTERNS


def _csv_list(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _load_status_markers(value: str) -> dict[str, tuple[str, ...]] | None:
    """--status-markers accepts inline JSON or a JSON file path: {"status": ["phrase", ...]}.

    Raises ValueError with a user-facing message on bad input.
    """
    if not value:
        return None
    stripped = value.strip()
    if stripped.startswith("{"):
        raw = stripped  # inline JSON; never treated as a path (long values exceed NAME_MAX)
    else:
        try:
            path = Path(stripped)
            if not path.is_file():
                raise ValueError(f"--status-markers: '{value}' is neither inline JSON nor an existing file")
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"--status-markers: cannot read '{value}': {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--status-markers: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError('--status-markers: expected an object like {"temporarily_closed": ["..."]}')
    unknown = sorted(set(data) - set(DEFAULT_STATUS_MARKERS))
    if unknown:
        raise ValueError(
            f"--status-markers: unknown status {', '.join(unknown)}; expected one of "
            f"{', '.join(sorted(DEFAULT_STATUS_MARKERS))}"
        )
    out: dict[str, tuple[str, ...]] = {}
    for k, v in data.items():
        if isinstance(v, str) or not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"--status-markers: '{k}' must map to a list of phrases")
        out[str(k)] = tuple(v)
    return out


def build_parser(defaults: dict[str, object]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-seo-crawler",
        description="High-throughput, concurrent multi-source Local SEO scraper.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="citations",
        help="Mode: 'citations', 'competitor', 'serp', or 'report' (render a report from an existing --db) [default: citations]",
    )
    parser.add_argument(
        "--input",
        dest="input_path",
        type=str,
        default="",
        help="Path to input manifest (e.g. citations.json)",
    )
    parser.add_argument(
        "--domain",
        type=str,
        default="",
        help="Target competitor domain (e.g. competitor.com)",
    )
    parser.add_argument(
        "--query",
        type=str,
        default="",
        help='Search query for Local SERP (e.g. "drain cleaning dallas")',
    )
    parser.add_argument(
        "--location",
        type=str,
        default="",
        help='Geographic location for SERP (e.g. "Dallas, TX")',
    )
    parser.add_argument(
        "--business",
        dest="business_name",
        type=str,
        default="",
        help="Target business name for verification",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(defaults.get("concurrency", 4)),
        help="Number of parallel worker slots [default: 4]",
    )
    parser.add_argument(
        "--max-retries",
        dest="max_retries",
        type=int,
        default=int(defaults.get("max_retries", 3)),
        help="Maximum retry attempts on Cloudflare block/timeout [default: 3]",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        type=str,
        default=str(defaults.get("output_dir", "artifacts")),
        help="Directory to store output artifacts [default: artifacts]",
    )
    parser.add_argument(
        "--max-pages",
        dest="max_pages",
        type=int,
        default=int(defaults.get("max_pages", 1000)),
        help="Maximum pages to crawl in competitor mode [default: 1000]",
    )
    parser.add_argument(
        "--max-depth",
        dest="max_depth",
        type=int,
        default=int(defaults.get("max_depth", 5)),
        help="Maximum link crawl depth [default: 5]",
    )
    parser.add_argument(
        "--chunk-size",
        dest="chunk_size",
        type=int,
        default=int(defaults.get("chunk_size", 15)),
        help="Number of URLs per worker chunk batch [default: 15]",
    )
    parser.add_argument(
        "--flaresolverr-url",
        dest="flaresolverr_url",
        type=str,
        default=str(defaults.get("flaresolverr_url", "http://localhost:8193")),
        help="FlareSolverr API base URL [default: http://localhost:8193]",
    )
    parser.add_argument(
        "--redis-url",
        dest="redis_url",
        type=str,
        default=str(defaults.get("redis_url", "")),
        help="Optional Redis URL for distributed frontier queue (e.g. redis://localhost:6379/0)",
    )
    parser.add_argument(
        "--sqlite-db-name",
        dest="sqlite_db_name",
        type=str,
        default=str(defaults.get("sqlite_db_name", "crawl_results.db")),
        help="SQLite database filename inside output-dir [default: crawl_results.db]",
    )
    parser.add_argument(
        "--no-sitemap",
        dest="use_sitemap",
        action="store_false",
        default=bool(defaults.get("use_sitemap", True)),
        help="Disable automatic sitemap.xml / robots.txt discovery",
    )
    parser.add_argument(
        "--no-fast-http",
        dest="fast_http_first",
        action="store_false",
        default=bool(defaults.get("fast_http_first", True)),
        help="Disable fast direct HTTP attempts and send all requests directly to FlareSolverr",
    )
    parser.add_argument(
        "--disable-competitor",
        dest="enable_competitor",
        action="store_false",
        default=bool(defaults.get("enable_competitor", True)),
        help="Toggle competitor crawling mode off",
    )
    parser.add_argument(
        "--export-format",
        dest="export_format",
        type=str,
        choices=["json", "yaml", "csv", "xlsx", "excel"],
        default=str(defaults.get("export_format", "json")),
        help="Output serialization format: json, yaml, csv, or xlsx/excel [default: json]",
    )
    parser.add_argument(
        "--export-mode",
        dest="export_mode",
        type=str,
        choices=["single", "multi", "chunked"],
        default=str(defaults.get("export_mode", "single")),
        help="Single file output or chunked multi-file data dump [default: single]",
    )
    parser.add_argument(
        "--export-chunk-size",
        dest="export_chunk_size",
        type=int,
        default=int(defaults.get("export_chunk_size", 250)),
        help="Records per file chunk when export-mode is multi/chunked [default: 250]",
    )
    parser.add_argument(
        "--archive-format",
        dest="archive_format",
        type=str,
        choices=["none", "zip", "tar"],
        default=str(defaults.get("archive_format", "none")),
        help="Package output artifacts into an archive: none, zip, or tar [default: none]",
    )
    parser.add_argument(
        "--tar-compression",
        dest="tar_compression",
        type=str,
        choices=["gz", "bz2", "xz", "zst", "none"],
        default=str(defaults.get("tar_compression", "gz")),
        help="Tarball compression algorithm: gz, bz2, xz, zst, or none [default: gz]",
    )

    # Configurable SEO Audit Threshold Flags
    parser.add_argument(
        "--preserve-trailing-slash",
        dest="preserve_trailing_slash",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults.get("preserve_trailing_slash", True)),
        help="Preserve trailing slashes in normalized URLs (supports --preserve-trailing-slash / --no-preserve-trailing-slash) [default: True]",
    )
    parser.add_argument(
        "--verify-all-links",
        dest="verify_all_links",
        action="store_true",
        default=bool(defaults.get("verify_all_links", False)),
        help="Perform HTTP verification for unvisited internal link targets after crawling [default: False]",
    )
    parser.add_argument(
        "--title-min-len",
        dest="title_min_len",
        type=int,
        default=int(defaults.get("title_min_len", 30)),
        help="Minimum recommended page title length [default: 30]",
    )
    parser.add_argument(
        "--title-max-len",
        dest="title_max_len",
        type=int,
        default=int(defaults.get("title_max_len", 60)),
        help="Maximum recommended page title length [default: 60]",
    )
    parser.add_argument(
        "--desc-min-len",
        dest="desc_min_len",
        type=int,
        default=int(defaults.get("desc_min_len", 120)),
        help="Minimum recommended meta description length [default: 120]",
    )
    parser.add_argument(
        "--desc-max-len",
        dest="desc_max_len",
        type=int,
        default=int(defaults.get("desc_max_len", 160)),
        help="Maximum recommended meta description length [default: 160]",
    )
    parser.add_argument(
        "--min-word-count",
        dest="min_word_count",
        type=int,
        default=int(defaults.get("min_word_count", 300)),
        help="Thin content minimum word threshold [default: 300]",
    )
    parser.add_argument(
        "--dup-threshold",
        dest="dup_threshold",
        type=float,
        default=float(defaults.get("dup_threshold", 0.85)),
        help="Near-duplicate content similarity threshold ratio (0.0-1.0) [default: 0.85]",
    )
    parser.add_argument(
        "--slow-warn-ms",
        dest="slow_warn_ms",
        type=int,
        default=int(defaults.get("slow_warn_ms", 1000)),
        help="Response time warning threshold in milliseconds [default: 1000]",
    )
    parser.add_argument(
        "--slow-err-ms",
        dest="slow_err_ms",
        type=int,
        default=int(defaults.get("slow_err_ms", 3000)),
        help="Response time error threshold in milliseconds [default: 3000]",
    )
    parser.add_argument(
        "--large-warn-bytes",
        dest="large_warn_bytes",
        type=int,
        default=int(defaults.get("large_warn_bytes", 1048576)),
        help="HTML page size warning threshold in bytes [default: 1048576]",
    )
    parser.add_argument(
        "--large-err-bytes",
        dest="large_err_bytes",
        type=int,
        default=int(defaults.get("large_err_bytes", 3145728)),
        help="HTML page size error threshold in bytes [default: 3145728]",
    )

    # Post-crawl page classification (WS4/WS5)
    parser.add_argument(
        "--hub-min-links",
        dest="hub_min_links",
        type=int,
        default=int(defaults.get("hub_min_links", 10)),
        help="Minimum distinct body links to same-shape pages for a directory hub [default: 10]",
    )
    parser.add_argument(
        "--directory-url",
        dest="directory_urls",
        action="append",
        default=[],
        help="Force a URL to be treated as a directory hub (repeatable)",
    )
    parser.add_argument(
        "--locations-file",
        dest="locations_file",
        type=str,
        default=str(defaults.get("locations_file", "")),
        help="Authoritative CSV/JSON store list (url, name, address, phone, status); overrides detected clinics",
    )
    parser.add_argument(
        "--status-markers",
        dest="status_markers",
        type=str,
        default="",
        help='Status phrases as JSON file or inline JSON, merged over defaults: {"temporarily_closed": ["..."]}',
    )
    parser.add_argument(
        "--utility-tokens",
        dest="utility_tokens",
        type=_csv_list,
        default=DEFAULT_UTILITY_TOKENS,
        help="Comma-separated slug words marking utility pages [default: thank-you,thankyou,confirmation,...]",
    )
    parser.add_argument(
        "--prototype-tokens",
        dest="prototype_tokens",
        type=_csv_list,
        default=DEFAULT_PROTOTYPE_TOKENS,
        help="Comma-separated whole slug words marking prototype/staging pages [default: demo,old,new,test,...]",
    )
    parser.add_argument(
        "--prototype-allowlist",
        dest="prototype_allowlist",
        action="append",
        default=[],
        help="URL exempt from prototype detection (repeatable)",
    )
    parser.add_argument(
        "--staging-host-patterns",
        dest="staging_host_patterns",
        type=_csv_list,
        default=DEFAULT_STAGING_HOST_PATTERNS,
        help="Comma-separated glob patterns for staging hosts [default: staging*,*.staging.*,*-staging.*,*.on-forge.com,dev.*]",
    )
    parser.add_argument(
        "--corporate-share",
        dest="corporate_share",
        type=float,
        default=float(defaults.get("corporate_share", 0.40)),
        help="Share of pages above which a phone/address counts as corporate, not local [default: 0.40]",
    )

    # Performance (WS6)
    parser.add_argument(
        "--timing-sample",
        dest="timing_sample",
        type=int,
        default=int(defaults.get("timing_sample", 0)),
        help="After the crawl, fetch N URLs sequentially (cache-busted) and report TTFB median/p95 [default: 0 = off]",
    )
    parser.add_argument(
        "--psi-api-key",
        dest="psi_api_key",
        type=str,
        default=str(defaults.get("psi_api_key", "")),
        help="PageSpeed Insights API key for Chrome UX Report field data",
    )
    parser.add_argument(
        "--psi-sample",
        dest="psi_sample",
        type=int,
        default=int(defaults.get("psi_sample", 0)),
        help="Number of URLs to query in PageSpeed Insights (requires --psi-api-key) [default: 0]",
    )

    parser.add_argument(
        "--netrc",
        dest="netrc",
        type=str,
        default="",
        help="netrc file with HTTP Basic Auth credentials (e.g. a staging site). Sent only to hosts named as a machine in the file [default: off]",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        type=str,
        default="",
        help="Optional site-specific .env file to load before parsing options",
    )
    parser.add_argument(
        "--google-profile",
        dest="google_auth_profile",
        type=str,
        default=str(defaults.get("google_auth_profile", "")),
        help="Named Google API auth profile [default: GOOGLE_AUTH_PROFILE from .env]",
    )

    # Report mode (WS3)
    parser.add_argument("--db", dest="db", type=str, default="", help="Crawl database for --mode report")
    parser.add_argument(
        "--output", dest="output", type=str, default="",
        help="Report output path for --mode report [default: <db dir>/crawl_report.md]",
    )
    parser.add_argument(
        "--format", dest="report_format", type=str, choices=["md", "json"], default="md",
        help="Report format for --mode report; md also writes report_summary.json [default: md]",
    )
    parser.add_argument(
        "--sample-size", dest="report_sample_size", type=int, default=10,
        help="Sample URLs listed per report finding [default: 10]",
    )
    parser.add_argument(
        "--verify-narrative", dest="verify_narrative", type=str, default="",
        help="Check an externally written Markdown narrative against the report data; non-zero exit on mismatch",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", dest="env_file", type=str, default="")
    pre_args, _ = pre.parse_known_args(argv)

    if pre_args.env_file:
        try:
            cfg = scraper.init_scraper(env_file=pre_args.env_file)
        except (FileNotFoundError, ValueError) as exc:
            build_parser({}).error(str(exc))
    else:
        try:
            cfg = scraper.init_scraper()
        except Exception:
            cfg = {
            "concurrency": 4,
            "max_retries": 3,
            "max_pages": 1000,
            "max_depth": 5,
            "chunk_size": 15,
            "output_dir": "artifacts",
            "flaresolverr_url": "http://localhost:8193",
            "sqlite_db_name": "crawl_results.db",
            "use_sitemap": True,
            "fast_http_first": True,
            "enable_competitor": True,
            "export_format": "json",
            "export_mode": "single",
            "export_chunk_size": 250,
            "archive_format": "none",
            "tar_compression": "gz",
            "title_min_len": 30,
            "title_max_len": 60,
            "desc_min_len": 120,
            "desc_max_len": 160,
            "min_word_count": 300,
            "dup_threshold": 0.85,
            "slow_warn_ms": 1000,
            "slow_err_ms": 3000,
            "large_warn_bytes": 1048576,
            "large_err_bytes": 3145728,
        }

    parser = build_parser(cfg)
    args = parser.parse_args(argv)

    if args.mode == "report":
        if not args.db:
            parser.error("--mode report requires --db <crawl_results.db>")
        from src.report import run_report

        sys.exit(
            run_report(
                db=args.db,
                output=args.output,
                fmt=args.report_format,
                sample_limit=args.report_sample_size,
                verify_narrative_path=args.verify_narrative,
            )
        )

    from src.orchestrator import CrawlerOrchestrator

    try:
        status_markers = _load_status_markers(args.status_markers)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        orchestrator = CrawlerOrchestrator(
            mode=args.mode,
            input_path=args.input_path,
            domain=args.domain,
            query=args.query,
            location=args.location,
            business_name=args.business_name,
            concurrency=args.concurrency,
            max_retries=args.max_retries,
            output_dir=args.output_dir,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            chunk_size=args.chunk_size,
            flaresolverr_url=args.flaresolverr_url,
            use_sitemap=args.use_sitemap,
            fast_http_first=args.fast_http_first,
            redis_url=args.redis_url,
            sqlite_db_name=args.sqlite_db_name,
            export_format=args.export_format,
            export_mode=args.export_mode,
            export_chunk_size=args.export_chunk_size,
            archive_format=args.archive_format,
            tar_compression=args.tar_compression,
            enable_competitor=args.enable_competitor,
            preserve_trailing_slash=args.preserve_trailing_slash,
            verify_all_links=args.verify_all_links,
            title_min_len=args.title_min_len,
            title_max_len=args.title_max_len,
            desc_min_len=args.desc_min_len,
            desc_max_len=args.desc_max_len,
            min_word_count=args.min_word_count,
            dup_threshold=args.dup_threshold,
            slow_warn_ms=args.slow_warn_ms,
            slow_err_ms=args.slow_err_ms,
            large_warn_bytes=args.large_warn_bytes,
            large_err_bytes=args.large_err_bytes,
            hub_min_links=args.hub_min_links,
            directory_urls=args.directory_urls,
            locations_file=args.locations_file,
            status_markers=status_markers,
            utility_tokens=args.utility_tokens,
            prototype_tokens=args.prototype_tokens,
            prototype_allowlist=tuple(args.prototype_allowlist),
            staging_host_patterns=args.staging_host_patterns,
            corporate_share=args.corporate_share,
            timing_sample=args.timing_sample,
            psi_api_key=args.psi_api_key,
            psi_sample=args.psi_sample,
            netrc_path=args.netrc,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    orchestrator.run()
    if orchestrator.post_crawl_error:
        sys.exit(1)


if __name__ == "__main__":
    main()

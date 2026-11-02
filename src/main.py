"""CLI Entry Point for local-seo-crawler-fs."""

from __future__ import annotations

import argparse
from typing import Sequence

from src import scraper
from src.orchestrator import CrawlerOrchestrator


def build_parser(defaults: dict[str, object]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-seo-crawler",
        description="High-throughput, concurrent multi-source Local SEO scraper.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="citations",
        help="Target mode: 'citations', 'competitor', 'serp' [default: citations]",
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
    return parser


def main(argv: Sequence[str] | None = None) -> None:
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
    )
    orchestrator.run()


if __name__ == "__main__":
    main()

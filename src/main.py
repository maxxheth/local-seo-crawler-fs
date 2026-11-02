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
        default=30,
        help="Maximum pages to crawl in competitor mode [default: 30]",
    )
    parser.add_argument(
        "--flaresolverr-url",
        dest="flaresolverr_url",
        type=str,
        default=str(defaults.get("flaresolverr_url", "http://localhost:8193")),
        help="FlareSolverr API base URL [default: http://localhost:8193]",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        cfg = scraper.init_scraper()
    except Exception:
        cfg = {
            "concurrency": 4,
            "max_retries": 3,
            "output_dir": "artifacts",
            "flaresolverr_url": "http://localhost:8193",
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
        flaresolverr_url=args.flaresolverr_url,
    )
    orchestrator.run()


if __name__ == "__main__":
    main()

"""Concurrent Orchestrator: High-throughput chunked worker pool & retry state machine."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import logging
from pathlib import Path
from typing import Any

import httpx

from src import scraper
from src.config import extract_proxy_port
from src.models import PageTechnicalRecord
from src.output import OutputManager
from src.queue import FrontierQueue, create_frontier_queue
from src.seo_auditor import SEOAuditConfig, detect_duplicate_pages
from src.session_pool import SessionPool
from src.sitemap import is_internal_url
from src.storage import SQLiteStorage

logger = logging.getLogger(__name__)


def _process_chunk_worker(
    chunk: list[dict[str, Any]],
    worker_id: int,
    slot_idx: int,
    session_id: str,
    proxy_url: str,
    proxy_port: int,
    flaresolverr_url: str,
    fast_http_first: bool,
    max_retries: int,
    audit_cfg: SEOAuditConfig | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int, str]:
    """Worker function executing a chunk of URLs using a persistent HTTP client and FlareSolverr session."""
    completed_results: list[dict[str, Any]] = []
    retry_targets: list[dict[str, Any]] = []
    discovered_new_urls: list[dict[str, Any]] = []
    collected_link_edges: list[dict[str, Any]] = []
    errors_encountered = 0
    active_session_id = session_id
    cookie_jar: dict[str, str] = {}

    client_kwargs: dict[str, Any] = {
        "timeout": 15.0,
        "follow_redirects": True,
        "headers": dict(scraper.DEFAULT_HEADERS),
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    with httpx.Client(**client_kwargs) as shared_http_client:
        for item in chunk:
            target_info = item if "url" in item else item.get("target", {})
            retries = int(item.get("retries", 0))
            target_url = str(target_info.get("url", ""))

            req_job: dict[str, Any] = {
                "mode": target_info.get("mode", "competitor"),
                "url": target_url,
                "flaresolverr_url": flaresolverr_url,
                "session_id": active_session_id,
                "proxy_url": proxy_url,
                "proxy_port": proxy_port,
                "fast_http_first": fast_http_first,
            }
            for k in (
                "depth",
                "platform",
                "label",
                "business_name",
                "expected_address",
                "expected_phone",
                "query",
                "location",
                "domain",
                "preserve_trailing_slash",
            ):
                if k in target_info:
                    req_job[k] = target_info[k]

            res = scraper.execute_single_scrape(
                req_job,
                shared_client=shared_http_client,
                cookie_jar=cookie_jar if cookie_jar else None,
                audit_cfg=audit_cfg,
            )

            # Ingest solved cookies & user-agent from FlareSolverr into shared_http_client
            if res.get("solved_cookies"):
                for c in res["solved_cookies"]:
                    if isinstance(c, dict) and "name" in c and "value" in c:
                        cookie_jar[c["name"]] = c["value"]
                        shared_http_client.cookies.set(c["name"], c["value"])
            if res.get("solved_user_agent"):
                shared_http_client.headers["User-Agent"] = res["solved_user_agent"]

            success = bool(res.get("success"))
            status_code = int(res.get("status_code", 0))

            if success:
                print(f"  [SUCCESS] (HTTP {status_code}) {target_url}")
                completed_results.append(res)

                # Collect link edges for link graph
                if res.get("link_edges"):
                    collected_link_edges.extend(res["link_edges"])

                # Collect discovered URLs for queue expansion
                if "internal_urls" in res and target_info.get("mode", "competitor") == "competitor":
                    cur_depth = int(target_info.get("depth", 0))
                    for next_url in res["internal_urls"]:
                        discovered_new_urls.append({
                            "url": next_url,
                            "domain": target_info.get("domain", ""),
                            "mode": "competitor",
                            "depth": cur_depth + 1,
                            "retries": 0,
                            "preserve_trailing_slash": target_info.get("preserve_trailing_slash", True),
                        })
            else:
                errors_encountered += 1
                if retries < max_retries:
                    next_retries = retries + 1
                    print(
                        f"  [RETRY {next_retries} / {max_retries}] (HTTP {status_code}) {target_url} - Recycling session..."
                    )
                    if active_session_id:
                        scraper.destroy_flaresolverr_session(flaresolverr_url, active_session_id)
                    try:
                        active_session_id = str(
                            scraper.create_flaresolverr_session(flaresolverr_url, proxy_url or None)
                        )
                    except Exception:
                        active_session_id = ""

                    item_retry = dict(item)
                    item_retry["retries"] = next_retries
                    retry_targets.append(item_retry)
                else:
                    print(f"  [FAILED]  Max retries reached for {target_url}")
                    completed_results.append(res)

    return (
        completed_results,
        retry_targets,
        discovered_new_urls,
        collected_link_edges,
        errors_encountered,
        active_session_id,
    )


class CrawlerOrchestrator:
    """Manages worker pool concurrency, session slot leases, frontier queue, and SQLite storage."""

    def __init__(
        self,
        mode: str = "citations",
        input_path: str = "",
        domain: str = "",
        query: str = "",
        location: str = "",
        business_name: str = "",
        concurrency: int = 4,
        max_retries: int = 3,
        output_dir: str = "artifacts",
        max_pages: int = 1000,
        max_depth: int = 5,
        chunk_size: int = 15,
        flaresolverr_url: str = "http://localhost:8193",
        use_sitemap: bool = True,
        fast_http_first: bool = True,
        redis_url: str = "",
        sqlite_db_name: str = "crawl_results.db",
        export_format: str = "json",
        export_mode: str = "single",
        export_chunk_size: int = 250,
        archive_format: str = "none",
        tar_compression: str = "gz",
        enable_competitor: bool = True,
        preserve_trailing_slash: bool = True,
        verify_all_links: bool = False,
        # SEO Audit thresholds
        title_min_len: int = 30,
        title_max_len: int = 60,
        desc_min_len: int = 120,
        desc_max_len: int = 160,
        min_word_count: int = 300,
        dup_threshold: float = 0.85,
        slow_warn_ms: int = 1000,
        slow_err_ms: int = 3000,
        large_warn_bytes: int = 1048576,
        large_err_bytes: int = 3145728,
    ) -> None:
        self.mode = mode
        self.input_path = input_path
        self.domain = domain
        self.query = query
        self.location = location
        self.business_name = business_name
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.output_dir = output_dir
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.chunk_size = chunk_size
        self.flaresolverr_url = flaresolverr_url
        self.use_sitemap = use_sitemap
        self.fast_http_first = fast_http_first
        self.redis_url = redis_url
        self.enable_competitor = enable_competitor
        self.preserve_trailing_slash = preserve_trailing_slash
        self.verify_all_links = verify_all_links

        # Output & Archiving parameters
        self.export_format = export_format
        self.export_mode = export_mode
        self.export_chunk_size = export_chunk_size
        self.archive_format = archive_format
        self.tar_compression = tar_compression

        # Audit Config
        self.audit_cfg = SEOAuditConfig(
            title_min_len=title_min_len,
            title_max_len=title_max_len,
            desc_min_len=desc_min_len,
            desc_max_len=desc_max_len,
            min_word_count=min_word_count,
            dup_threshold=dup_threshold,
            slow_warn_ms=slow_warn_ms,
            slow_err_ms=slow_err_ms,
            large_warn_bytes=large_warn_bytes,
            large_err_bytes=large_err_bytes,
        )

        self.pool = SessionPool(flaresolverr_url)
        db_path = Path(output_dir) / sqlite_db_name
        self.storage = SQLiteStorage(db_path)
        self.queue: FrontierQueue = create_frontier_queue(
            redis_url=redis_url or None,
            sqlite_db_path=Path(output_dir) / "frontier.db",
        )

    def setup_pool(self, proxy_file: str = "proxies.txt") -> None:
        """Create persistent FlareSolverr sessions mapped 1:1 to proxies (or direct slots)."""
        proxies = scraper.parse_proxies_file(proxy_file)
        num_proxies = len(proxies)

        if num_proxies > 0:
            print(f"Initializing session pool with {num_proxies} proxy endpoint(s)...")
            for proxy_url in proxies:
                try:
                    sess_id = str(
                        scraper.create_flaresolverr_session(self.flaresolverr_url, proxy_url)
                    )
                except Exception:
                    sess_id = ""

                port = extract_proxy_port(proxy_url) or 0
                self.pool.add_slot(sess_id, proxy_url, int(port))
        else:
            print(
                f"No proxy list detected; creating {self.concurrency} direct FlareSolverr session slot(s)..."
            )
            for _ in range(self.concurrency):
                try:
                    sess_id = str(
                        scraper.create_flaresolverr_session(self.flaresolverr_url, None)
                    )
                except Exception:
                    sess_id = ""
                self.pool.add_slot(sess_id, "", 0)

    def run(self) -> dict[str, str]:
        """Execute chunked concurrent crawling workflow with SQLite checkpointing and multi-format export."""
        if self.mode == "competitor" and not self.enable_competitor:
            print("Competitor mode is currently toggled OFF (ENABLE_COMPETITOR=false). Exiting.")
            return {}

        initial_targets = scraper.load_targets(
            self.mode,
            self.input_path,
            self.domain,
            self.query,
            self.location,
            self.business_name,
            use_sitemap=self.use_sitemap,
            max_pages=self.max_pages,
        )

        for t in initial_targets:
            t["mode"] = self.mode

        completed_urls = self.storage.get_completed_urls(self.mode)
        self.queue.reconcile(completed_urls)
        fresh_targets = [t for t in initial_targets if t.get("url") not in completed_urls]
        for t in fresh_targets:
            t["preserve_trailing_slash"] = self.preserve_trailing_slash

        self.queue.push_targets(fresh_targets)

        queue_size = self.queue.size()
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  LOCAL-SEO-CRAWLER-FS (Chunked Concurrent Orchestrator)")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("Mode:           ", self.mode)
        print("Queue Targets:  ", queue_size)
        print("Max Pages:      ", self.max_pages)
        print("Max Depth:      ", self.max_depth)
        print("Concurrency:    ", self.concurrency)
        print("Chunk Size:     ", self.chunk_size)
        print("Export Format:  ", self.export_format)
        print("Export Mode:    ", self.export_mode)
        print("Archive Format: ", self.archive_format)
        print("Storage:        ", f"SQLite ({self.storage.db_path})")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        if queue_size == 0 and not completed_urls:
            print("No targets found to crawl.")
            return {}

        if self.pool.size() == 0:
            self.setup_pool("proxies.txt")

        pool_capacity = self.pool.size()
        effective_concurrency = self.concurrency
        if 0 < pool_capacity < effective_concurrency:
            effective_concurrency = pool_capacity
            print("Clamping concurrency to pool capacity:", effective_concurrency)

        in_flight: dict[Future[Any], dict[str, Any]] = {}
        next_worker_id = 1
        processed_count = len(completed_urls)

        with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
            while (self.queue.has_pending() or in_flight) and processed_count < self.max_pages:
                in_flight_pages = sum(m["chunk_size"] for m in in_flight.values())
                while (
                    len(in_flight) < effective_concurrency
                    and self.queue.has_pending()
                    and (processed_count + in_flight_pages) < self.max_pages
                ):
                    remaining_budget = self.max_pages - (processed_count + in_flight_pages)
                    pop_limit = min(self.chunk_size, remaining_budget)
                    if pop_limit <= 0:
                        break

                    worker_id = next_worker_id
                    next_worker_id = (next_worker_id % 1000) + 1

                    slot_idx = self.pool.acquire_slot(worker_id)
                    if slot_idx < 0:
                        break

                    chunk = self.queue.pop_chunk(pop_limit)
                    if not chunk:
                        self.pool.release_slot(worker_id, slot_idx)
                        break

                    sess_id = self.pool.get_session_id(slot_idx, worker_id)
                    p_url = self.pool.get_proxy_url(slot_idx, worker_id)
                    p_port = self.pool.get_proxy_port(slot_idx, worker_id)

                    fut = executor.submit(
                        _process_chunk_worker,
                        chunk=chunk,
                        worker_id=worker_id,
                        slot_idx=slot_idx,
                        session_id=sess_id,
                        proxy_url=p_url,
                        proxy_port=p_port,
                        flaresolverr_url=self.flaresolverr_url,
                        fast_http_first=self.fast_http_first,
                        max_retries=self.max_retries,
                        audit_cfg=self.audit_cfg,
                    )
                    in_flight[fut] = {
                        "slot_idx": slot_idx,
                        "worker_id": worker_id,
                        "chunk_size": len(chunk),
                        "chunk": chunk,
                    }
                    in_flight_pages += len(chunk)

                if in_flight:
                    done_set, _ = wait(
                        list(in_flight.keys()),
                        timeout=0.1,
                        return_when=FIRST_COMPLETED,
                    )

                    for fut in done_set:
                        slot_meta = in_flight.pop(fut)
                        slot_idx = slot_meta["slot_idx"]
                        worker_id = slot_meta["worker_id"]

                        (
                            completed_results,
                            retry_targets,
                            discovered_links,
                            collected_edges,
                            errors,
                            final_sess_id,
                        ) = fut.result()

                        if errors > 0:
                            self.pool.record_error(worker_id, slot_idx)
                        self.pool.update_session(worker_id, slot_idx, final_sess_id)
                        self.pool.release_slot(worker_id, slot_idx)

                        # Mark chunk URLs as done in queue
                        chunk_urls = [
                            item.get("url") or item.get("target", {}).get("url", "")
                            for item in slot_meta.get("chunk", [])
                        ]
                        self.queue.mark_done([u for u in chunk_urls if u])

                        if retry_targets:
                            for rt in retry_targets:
                                rt["preserve_trailing_slash"] = self.preserve_trailing_slash
                            self.queue.push_targets(retry_targets, allow_seen=True)

                        # Save link edges into SQLite
                        if collected_edges:
                            self.storage.save_internal_links_batch(collected_edges)

                        # Checkpoint completed results incrementally
                        for r in completed_results:
                            processed_count += 1
                            if self.mode == "competitor":
                                self.storage.save_page(
                                    r.get("result", {}),
                                    domain=self.domain,
                                    depth=r.get("depth", 0),
                                )
                                if r.get("issues"):
                                    self.storage.save_seo_issues_batch(r["issues"])
                            elif self.mode == "citations":
                                self.storage.save_citation(r.get("result", {}))
                            elif self.mode == "serp":
                                self.storage.save_serp(
                                    self.query, self.location, r.get("result", {})
                                )

                            if r.get("evidence"):
                                self.storage.save_evidence_batch(r["evidence"])

                        # Expand frontier queue with new internal URLs
                        if (
                            self.mode == "competitor"
                            and discovered_links
                            and processed_count < self.max_pages
                        ):
                            valid_new: list[dict[str, Any]] = []
                            for link_info in discovered_links:
                                u_str = str(link_info.get("url", ""))
                                depth = int(link_info.get("depth", 0))
                                if depth <= self.max_depth and is_internal_url(u_str, self.domain, preserve_trailing_slash=self.preserve_trailing_slash):
                                    link_info["preserve_trailing_slash"] = self.preserve_trailing_slash
                                    valid_new.append(link_info)
                            if valid_new:
                                self.queue.push_targets(valid_new)

        # Post-Crawl Graph & Duplication Analysis Pass for competitor mode
        if self.mode == "competitor":
            # If verify_all_links enabled, inspect any unvisited link targets
            if self.verify_all_links:
                unvisited = self.storage.get_unvisited_link_targets()
                if unvisited:
                    print(f"Verifying {len(unvisited)} unvisited link targets (--verify-all-links)...")
                    status_map: dict[str, int] = {}
                    with httpx.Client(timeout=10.0, follow_redirects=True, headers=dict(scraper.DEFAULT_HEADERS)) as v_client:
                        for u_target in unvisited:
                            try:
                                v_res = v_client.get(u_target)
                                status_map[u_target] = v_res.status_code
                            except Exception:
                                status_map[u_target] = 500
                    self.storage.update_link_target_statuses(status_map)

            print("Analyzing site link graph and checking for near-duplicate pages...")
            raw_pages = self.storage.fetch_all_pages()
            page_objs = [PageTechnicalRecord(**p) for p in raw_pages]
            duplicate_issues = detect_duplicate_pages(
                page_objs,
                threshold=self.audit_cfg.dup_threshold,
            )
            # Backfill target status, inlinks count, broken outlinks, and duplicate issues in SQLite
            self.storage.post_crawl_sync(duplicate_issues)

        # Export final artifacts from SQLite storage
        meta: dict[str, Any] = {
            "business": self.business_name,
            "domain": self.domain,
            "query": self.query,
            "location": self.location,
            "max_depth": self.max_depth,
        }

        output_mgr = OutputManager(
            output_dir=self.output_dir,
            export_format=self.export_format,
            export_mode=self.export_mode,
            export_chunk_size=self.export_chunk_size,
            archive_format=self.archive_format,
            tar_compression=self.tar_compression,
        )

        artifacts = output_mgr.export_from_storage(self.storage, mode=self.mode, meta=meta)

        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  CRAWL COMPLETE")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("Processed Targets: ", processed_count)
        print("Database:          ", str(self.storage.db_path))
        print("Artifacts generated in:", self.output_dir)
        for key, val in artifacts.items():
            print("  -", key, ":", val)
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # Session teardown
        for s in range(self.pool.size()):
            sess_id = self.pool.get_session_id(s)
            if sess_id:
                scraper.destroy_flaresolverr_session(self.flaresolverr_url, sess_id)

        return artifacts

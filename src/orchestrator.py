"""Concurrent Orchestrator: High-throughput worker pool & retry state machine."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

from src import scraper
from src.config import extract_proxy_port
from src.session_pool import SessionPool


class CrawlerOrchestrator:
    """Manages worker pool concurrency, session slot leases, and retries."""

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
        max_pages: int = 30,
        flaresolverr_url: str = "http://localhost:8193",
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
        self.flaresolverr_url = flaresolverr_url
        self.pool = SessionPool(flaresolverr_url)

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
        """Execute concurrent crawling workflow with sliding window and slot safety."""
        targets = scraper.load_targets(
            self.mode,
            self.input_path,
            self.domain,
            self.query,
            self.location,
            self.business_name,
        )

        initial_target_count = len(targets)
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  LOCAL-SEO-CRAWLER-FS (Concurrent Orchestrator)")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("Mode:       ", self.mode)
        print("Targets:    ", initial_target_count)
        print("Concurrency:", self.concurrency)
        print("Output Dir: ", self.output_dir)
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        if initial_target_count == 0:
            print("No targets found to crawl.")
            return {}

        if self.pool.size() == 0:
            self.setup_pool("proxies.txt")

        # Clamp effective concurrency to pool size to guarantee slot safety
        pool_capacity = self.pool.size()
        effective_concurrency = self.concurrency
        if 0 < pool_capacity < effective_concurrency:
            effective_concurrency = pool_capacity
            print("Clamping concurrency to pool capacity:", effective_concurrency)

        all_results: list[dict[str, Any]] = []
        pending_jobs: list[dict[str, Any]] = []
        visited_urls: set[str] = set()

        for target in targets:
            pending_jobs.append({"target": target, "retries": 0})
            visited_urls.add(str(target["url"]))

        completed_count = 0
        in_flight: dict[Future[dict[str, Any]], dict[str, Any]] = {}
        next_worker_id = 1

        optional_keys = (
            "depth",
            "platform",
            "label",
            "business_name",
            "expected_address",
            "expected_phone",
            "query",
            "location",
            "domain",
        )

        with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
            while pending_jobs or in_flight:
                # 1. Dispatch pending jobs into available slots up to effective_concurrency
                while len(in_flight) < effective_concurrency and pending_jobs:
                    worker_id = next_worker_id
                    next_worker_id = (next_worker_id % 1000) + 1

                    slot_idx = self.pool.acquire_slot(worker_id)
                    if slot_idx < 0:
                        break

                    current_job = pending_jobs.pop(0)
                    target_info = current_job["target"]

                    req_job: dict[str, Any] = {
                        "mode": self.mode,
                        "url": target_info["url"],
                        "flaresolverr_url": self.flaresolverr_url,
                        "session_id": self.pool.get_session_id(slot_idx, worker_id),
                        "proxy_url": self.pool.get_proxy_url(slot_idx, worker_id),
                        "proxy_port": self.pool.get_proxy_port(slot_idx, worker_id),
                    }
                    for key in optional_keys:
                        if key in target_info:
                            req_job[key] = target_info[key]

                    fut = executor.submit(scraper.execute_single_scrape, req_job)
                    in_flight[fut] = {
                        "job": current_job,
                        "slot_idx": slot_idx,
                        "worker_id": worker_id,
                    }

                # 2. Wait for at least one in-flight task to complete (sliding window)
                if in_flight:
                    done_set, _ = wait(
                        list(in_flight.keys()),
                        timeout=0.1,
                        return_when=FIRST_COMPLETED,
                    )

                    for fut in done_set:
                        tracked = in_flight.pop(fut)
                        job_item = tracked["job"]
                        slot_idx = int(tracked["slot_idx"])
                        worker_id = int(tracked["worker_id"])

                        target_info = job_item["target"]
                        retries = int(job_item["retries"])
                        target_url = str(target_info["url"])

                        res = fut.result()
                        success = bool(res.get("success"))
                        status_code = int(res.get("status_code", 0))

                        if success:
                            completed_count += 1
                            all_results.append(res)
                            self.pool.release_slot(worker_id, slot_idx)
                            print(f"  [SUCCESS] (HTTP {status_code}) {target_url}")

                            # Internal link discovery for competitor mode
                            if self.mode == "competitor" and "internal_urls" in res:
                                cur_depth = int(target_info.get("depth", 0))
                                if cur_depth < 2 and len(visited_urls) < self.max_pages:
                                    for next_url in res["internal_urls"]:
                                        next_url_str = str(next_url)
                                        if (
                                            next_url_str not in visited_urls
                                            and len(visited_urls) < self.max_pages
                                        ):
                                            visited_urls.add(next_url_str)
                                            new_target = {
                                                "url": next_url_str,
                                                "domain": self.domain,
                                                "depth": cur_depth + 1,
                                            }
                                            pending_jobs.append(
                                                {"target": new_target, "retries": 0}
                                            )
                        else:
                            self.pool.record_error(worker_id, slot_idx)
                            if retries < self.max_retries:
                                next_retries = retries + 1
                                print(
                                    f"  [RETRY {next_retries} / {self.max_retries}] "
                                    f"(HTTP {status_code}) {target_url} - Recycling session slot..."
                                )

                                # Recycle FlareSolverr session
                                old_sess = self.pool.get_session_id(slot_idx, worker_id)
                                if old_sess:
                                    scraper.destroy_flaresolverr_session(
                                        self.flaresolverr_url, old_sess
                                    )

                                p_url = self.pool.get_proxy_url(slot_idx, worker_id) or None
                                try:
                                    new_sess = str(
                                        scraper.create_flaresolverr_session(
                                            self.flaresolverr_url, p_url
                                        )
                                    )
                                except Exception:
                                    new_sess = ""

                                self.pool.update_session(worker_id, slot_idx, new_sess)
                                self.pool.release_slot(worker_id, slot_idx)

                                job_item["retries"] = next_retries
                                pending_jobs.append(job_item)
                            else:
                                completed_count += 1
                                all_results.append(res)
                                self.pool.release_slot(worker_id, slot_idx)
                                print(f"  [FAILED]  Max retries reached for {target_url}")

        # Write artifacts
        meta: dict[str, Any] = {}
        if self.business_name:
            meta["business"] = self.business_name
        if self.domain:
            meta["domain"] = self.domain
        if self.query:
            meta["query"] = self.query
        if self.location:
            meta["location"] = self.location

        artifacts = scraper.write_artifacts(self.mode, all_results, self.output_dir, meta)

        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  CRAWL COMPLETE")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("Processed Targets:", completed_count)
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

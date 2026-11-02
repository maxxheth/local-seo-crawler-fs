"""Unit and integration tests for Python SessionPool, Storage, Queue, Sitemaps, and CrawlerOrchestrator."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import tarfile
from typing import Any
import zipfile

from src import scraper
from src.adapters.competitor import CompetitorSiteAdapter
from src.orchestrator import CrawlerOrchestrator
from src.output import OutputManager
from src.queue import SQLiteFrontierQueue
from src.session_pool import SessionPool, SessionSlot
from src.sitemap import is_internal_url, normalize_url
from src.storage import SQLiteStorage


def test_session_slot_init() -> None:
    slot = SessionSlot("sess_alpha", "http://user:pass@proxy.decodo.com:10001", 10001)
    assert slot.session_id == "sess_alpha"
    assert slot.proxy_port == 10001
    assert slot.in_use is False
    assert slot.error_count == 0


def test_session_pool_acquisition_and_release() -> None:
    pool = SessionPool("http://localhost:8193")
    pool.add_slot("sess_1", "http://proxy:10001", 10001)
    pool.add_slot("sess_2", "http://proxy:10002", 10002)

    assert pool.size() == 2

    # Worker 1 acquires slot 0
    s0 = pool.acquire_slot(1)
    assert s0 == 0

    # Worker 2 acquires slot 1
    s1 = pool.acquire_slot(2)
    assert s1 == 1

    # Worker 3 tries to acquire when all slots are busy
    s2 = pool.acquire_slot(3)
    assert s2 == -1

    # Worker 1 releases slot 0
    pool.release_slot(1, s0)

    # Worker 3 now acquires the released slot 0
    s3 = pool.acquire_slot(3)
    assert s3 == 0


def test_url_normalization() -> None:
    assert normalize_url("https://example.com/test/?utm_source=google&ref=123#section") == "https://example.com/test"
    assert normalize_url("https://example.com/IMAGE.JPG") is None
    assert is_internal_url("https://sub.example.com/page", "example.com") is True
    assert is_internal_url("https://another.com/page", "example.com") is False


def test_sqlite_storage(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "test.db")
    storage.save_page({"url": "https://example.com/1", "title": "Page 1", "status_code": 200}, domain="example.com")
    storage.save_page({"url": "https://example.com/2", "title": "Page 2", "status_code": 200}, domain="example.com")

    completed = storage.get_completed_urls("competitor")
    assert "https://example.com/1" in completed
    assert "https://example.com/2" in completed

    pages = storage.fetch_all_pages()
    assert len(pages) == 2
    assert pages[0]["title"] == "Page 1"


def test_sqlite_frontier_queue(tmp_path: Path) -> None:
    queue = SQLiteFrontierQueue(tmp_path / "queue.db")
    targets = [
        {"url": "https://example.com/a"},
        {"url": "https://example.com/b"},
        {"url": "https://example.com/a"},  # duplicate
    ]
    added = queue.push_targets(targets)
    assert added == 2
    assert queue.size() == 2

    chunk = queue.pop_chunk(chunk_size=1)
    assert len(chunk) == 1
    assert chunk[0]["url"] == "https://example.com/a"
    assert queue.size() == 1


def test_multi_format_and_archive_export(tmp_path: Path) -> None:
    pages = [
        {"url": f"https://example.com/{i}", "title": f"Page {i}", "status_code": 200}
        for i in range(10)
    ]
    evidence = [
        {
            "claim_type": "audit",
            "source_url": f"https://example.com/{i}",
            "timestamp": "2026-11-02T12:00:00Z",
            "http_status": 200,
            "dom_selector": "title",
            "verbatim_text": f"Page {i}",
            "hash_sha256": "testhash",
        }
        for i in range(10)
    ]

    # Test YAML + Multi chunked + Tarball (gz)
    mgr = OutputManager(
        output_dir=str(tmp_path / "yaml_export"),
        export_format="yaml",
        export_mode="multi",
        export_chunk_size=4,
        archive_format="tar",
        tar_compression="gz",
    )
    result = mgr.export_site_crawl("example.com", pages, evidence_records=evidence)

    assert "site_crawl_artifact" in result
    assert "archive" in result
    archive_path = Path(result["archive"])
    assert archive_path.exists()
    assert archive_path.name.endswith(".tar.gz")

    with tarfile.open(archive_path, "r:gz") as tf:
        names = tf.getnames()
        assert any("part_0001.yaml" in n for n in names)
        assert any("part_0003.yaml" in n for n in names)

    # Test CSV + Zip archive
    mgr_csv = OutputManager(
        output_dir=str(tmp_path / "csv_export"),
        export_format="csv",
        export_mode="single",
        archive_format="zip",
    )
    res_csv = mgr_csv.export_site_crawl("example.com", pages, evidence_records=evidence)
    zip_path = Path(res_csv["archive"])
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        znames = zf.namelist()
        assert "site_crawl_artifact.csv" in znames


def test_competitor_mode_toggle_off(tmp_path: Path) -> None:
    orch = CrawlerOrchestrator(
        mode="competitor",
        domain="example.com",
        enable_competitor=False,
        output_dir=str(tmp_path),
    )
    res = orch.run()
    assert res == {}


def test_orchestrator_chunked_run(monkeypatch: Any, tmp_path: Path) -> None:
    created_sessions: list[str] = []

    def fake_create_session(base_url: str, proxy_url: str | None = None) -> str:
        sess = f"sess_{len(created_sessions) + 1}"
        created_sessions.append(sess)
        return sess

    def fake_destroy_session(base_url: str, session_id: str) -> bool:
        return True

    def fake_execute_single_scrape(
        job: dict[str, Any], shared_client: Any = None, cookie_jar: Any = None
    ) -> dict[str, Any]:
        u = job["url"]
        return {
            "success": True,
            "status_code": 200,
            "domain": "example.com",
            "depth": 0,
            "internal_urls": ["https://example.com/subpage-1"] if u == "https://example.com/" else [],
            "result": {
                "url": u,
                "title": "Title",
                "status_code": 200,
            },
            "evidence": [],
        }

    monkeypatch.setattr(scraper, "parse_proxies_file", lambda _: [])
    monkeypatch.setattr(scraper, "create_flaresolverr_session", fake_create_session)
    monkeypatch.setattr(scraper, "destroy_flaresolverr_session", fake_destroy_session)
    monkeypatch.setattr(scraper, "execute_single_scrape", fake_execute_single_scrape)

    orch = CrawlerOrchestrator(
        mode="competitor",
        domain="example.com",
        concurrency=2,
        chunk_size=5,
        max_pages=10,
        max_depth=3,
        use_sitemap=False,
        output_dir=str(tmp_path),
        export_format="json",
    )
    artifacts = orch.run()

    assert "site_crawl_artifact" in artifacts
    # Verify records were saved into SQLite
    storage = SQLiteStorage(Path(tmp_path) / "crawl_results.db")
    pages = storage.fetch_all_pages()
    assert len(pages) == 2  # homepage and discovered subpage-1

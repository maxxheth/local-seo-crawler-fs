"""Unit and integration tests for Python SessionPool and CrawlerOrchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src import scraper
from src.orchestrator import CrawlerOrchestrator
from src.session_pool import SessionPool, SessionSlot


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


def test_session_pool_error_and_recycle() -> None:
    pool = SessionPool("http://localhost:8193")
    pool.add_slot("old_sess", "http://proxy:10003", 10003)

    s = pool.acquire_slot(1)
    pool.record_error(1, s)
    assert pool.get_error_count(s) == 1

    pool.update_session(1, s, "new_recycled_sess")
    assert pool.get_session_id(s) == "new_recycled_sess"

    pool.release_slot(1, s)


def test_python_scraper_bridge() -> None:
    cfg = scraper.init_scraper()
    assert cfg

    targets = scraper.load_targets("competitor", "", "example.com", "", "", "")
    assert len(targets) == 1
    assert targets[0]["url"] == "https://example.com/"


def test_orchestrator_retry_and_session_recycle(monkeypatch: Any, tmp_path: Path) -> None:
    attempts: list[str] = []
    destroyed_sessions: list[str] = []
    created_sessions: list[str] = []

    def fake_create_session(base_url: str, proxy_url: str | None = None) -> str:
        sess = f"sess_{len(created_sessions) + 1}"
        created_sessions.append(sess)
        return sess

    def fake_destroy_session(base_url: str, session_id: str) -> bool:
        destroyed_sessions.append(session_id)
        return True

    def fake_execute_single_scrape(job: dict[str, Any]) -> dict[str, Any]:
        attempts.append(str(job.get("session_id")))
        if len(attempts) == 1:
            return {"success": False, "status_code": 403, "error": "Blocked"}
        return {
            "success": True,
            "status_code": 200,
            "domain": "example.com",
            "depth": 0,
            "internal_urls": [],
            "result": {
                "url": job["url"],
                "final_url": job["url"],
                "status_code": 200,
                "title": "Example",
                "meta_description": "",
                "canonical_url": job["url"],
                "h1": ["Example"],
                "h2": [],
                "h3": [],
                "word_count": 10,
                "internal_outlinks_count": 0,
                "external_outlinks_count": 0,
                "internal_urls": [],
                "schema_types": [],
                "nap_found": {"names": [], "addresses": [], "phones": []},
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
        concurrency=1,
        max_retries=2,
        output_dir=str(tmp_path),
    )
    artifacts = orch.run()

    assert len(attempts) == 2
    assert attempts == ["sess_1", "sess_2"]
    assert "sess_1" in destroyed_sessions
    assert "site_crawl_artifact" in artifacts

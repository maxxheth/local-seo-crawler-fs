"""Unit and integration tests for Python SessionPool, Storage, Queue, Sitemaps, and CrawlerOrchestrator."""

from __future__ import annotations

from pathlib import Path
import tarfile
from typing import Any
import zipfile

import openpyxl

from src import scraper
from src.models import PageTechnicalRecord
from src.orchestrator import CrawlerOrchestrator
from src.output import OutputManager
from src.queue import SQLiteFrontierQueue
from src.seo_auditor import SEOAuditConfig, audit_page, detect_duplicate_pages
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


def test_sqlite_storage_and_idempotent_sync(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "test.db")
    storage.save_page({"url": "https://example.com/1", "title": "Page 1", "status_code": 200}, domain="example.com")
    storage.save_page({"url": "https://example.com/broken", "title": "404", "status_code": 404}, domain="example.com")
    storage.save_internal_links_batch([
        {
            "source_url": "https://example.com/1",
            "target_url": "https://example.com/broken",
            "anchor_text": "Broken Link",
            "placement": "body",
            "is_nofollow": False,
        },
        {
            "source_url": "https://example.com/1",
            "target_url": "https://example.com/broken",
            "anchor_text": "Duplicate Broken Link",
            "placement": "footer",
            "is_nofollow": False,
        },
    ])

    # Run post_crawl_sync twice to verify idempotency and deduplication
    storage.post_crawl_sync()
    storage.post_crawl_sync()

    pages = storage.fetch_all_pages()
    p1 = next(p for p in pages if p["url"] == "https://example.com/1")
    assert p1["broken_outlinks"] == ["https://example.com/broken"]
    broken_issues = [i for i in p1.get("issues", []) if i["issue"] == "Broken Internal Link"]
    assert len(broken_issues) == 1


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

    # Re-enqueueing seen target with allow_seen=True works for retries
    requeued = queue.push_targets([{"url": "https://example.com/a", "retries": 1}], allow_seen=True)
    assert requeued == 1
    assert queue.size() == 2


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


def test_seo_auditor_rules_and_duplication() -> None:
    cfg = SEOAuditConfig(
        title_min_len=20,
        title_max_len=50,
        desc_min_len=50,
        desc_max_len=150,
        min_word_count=100,
    )

    page = PageTechnicalRecord(
        url="https://example.com/bad",
        title="Short",  # too short
        meta_description="",  # missing
        h1=[],  # missing H1
        word_count=50,  # thin content
        viewport="",  # missing viewport
        meta_robots="noindex",  # noindex
        canonical_url="https://example.com/bad/",  # trailing slash equivalent to url
        response_time_ms=1500,  # slow
    )
    issues = audit_page(page, cfg)
    issue_names = [i.issue for i in issues]
    assert "Title Too Short" in issue_names
    assert "Missing Meta Description" in issue_names
    assert "Missing H1 Tag" in issue_names
    assert "Thin Content" in issue_names
    assert "Noindex Tag Present" in issue_names
    assert "Missing Viewport Meta Tag" in issue_names
    assert "Slow Response Time" in issue_names
    assert "Canonical Points to Different URL" not in issue_names

    # Test Duplicate Detection
    p1 = PageTechnicalRecord(
        url="https://example.com/service-dallas",
        title="Plumbing Service in Dallas TX",
        meta_description="Fast 24/7 plumbing and emergency rooter services in Dallas.",
        h1=["Plumbing Dallas"],
        word_count=350,
    )
    p2 = PageTechnicalRecord(
        url="https://example.com/service-fort-worth",
        title="Plumbing Service in Dallas TX",  # duplicate title
        meta_description="Fast 24/7 plumbing and emergency rooter services in Dallas.",
        h1=["Plumbing Dallas"],
        word_count=350,
    )
    dup_map = detect_duplicate_pages([p1, p2], threshold=0.85)
    assert "https://example.com/service-dallas" in dup_map
    assert "https://example.com/service-fort-worth" in dup_map
    assert dup_map["https://example.com/service-dallas"].category == "Duplication"


def test_competitor_mode_toggle_off(tmp_path: Path) -> None:
    orch = CrawlerOrchestrator(
        mode="competitor",
        domain="example.com",
        enable_competitor=False,
        output_dir=str(tmp_path),
    )
    res = orch.run()
    assert res == {}


def test_orchestrator_chunked_run_with_retry(monkeypatch: Any, tmp_path: Path) -> None:
    created_sessions: list[str] = []
    attempt_counts: dict[str, int] = {}

    def fake_create_session(base_url: str, proxy_url: str | None = None) -> str:
        sess = f"sess_{len(created_sessions) + 1}"
        created_sessions.append(sess)
        return sess

    def fake_destroy_session(base_url: str, session_id: str) -> bool:
        return True

    def fake_execute_single_scrape(
        job: dict[str, Any], shared_client: Any = None, cookie_jar: Any = None, audit_cfg: Any = None
    ) -> dict[str, Any]:
        u = job["url"]
        attempt_counts[u] = attempt_counts.get(u, 0) + 1

        # Fail the first attempt on subpage-1 to test retry re-enqueueing and session recycling
        if u == "https://example.com/subpage-1" and attempt_counts[u] == 1:
            return {
                "success": False,
                "status_code": 503,
                "error": "Cloudflare challenge",
                "result": {"url": u, "status_code": 503},
                "evidence": [],
            }

        return {
            "success": True,
            "status_code": 200,
            "domain": "example.com",
            "depth": 0,
            "internal_urls": ["https://example.com/subpage-1"] if u == "https://example.com/" else [],
            "link_edges": [
                {
                    "source_url": u,
                    "target_url": "https://example.com/subpage-1",
                    "anchor_text": "Subpage",
                    "placement": "navigation",
                    "is_nofollow": False,
                }
            ] if u == "https://example.com/" else [],
            "issues": [],
            "result": {
                "url": u,
                "title": "Title for " + u,
                "meta_description": "A valid meta description for testing this page properly.",
                "h1": ["Heading One"],
                "word_count": 350,
                "status_code": 200,
                "viewport": "width=device-width",
                "lang": "en",
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
        max_retries=2,
        chunk_size=5,
        max_pages=10,
        max_depth=3,
        use_sitemap=False,
        output_dir=str(tmp_path),
        export_format="json",
    )
    artifacts = orch.run()

    assert "site_crawl_artifact" in artifacts
    assert attempt_counts["https://example.com/subpage-1"] == 2
    storage = SQLiteStorage(Path(tmp_path) / "crawl_results.db")
    pages = storage.fetch_all_pages()
    assert len(pages) == 2  # homepage and discovered subpage-1

    # Verify link graph post-crawl sync populated inlinks_count
    subpage = next(p for p in pages if p["url"] == "https://example.com/subpage-1")
    assert subpage["inlinks_count"] == 1


def test_excel_export_multi_worksheet(tmp_path: Path) -> None:
    """Test Excel export produces rich multi-worksheet workbooks with atomic cell values."""
    pages = [
        {
            "url": "https://example.com/",
            "title": "Homepage Example",
            "meta_description": "Home page meta description for local business testing.",
            "status_code": 200,
            "word_count": 520,
            "response_time_ms": 240,
            "h1": ["Main Business Title"],
            "h2": ["Our Services"],
            "issues": [
                {
                    "url": "https://example.com/",
                    "severity": "warning",
                    "category": "meta",
                    "issue": "Missing OG Image",
                    "details": "OG image tag absent",
                }
            ],
            "images": {
                "total_images": 5,
                "missing_alt_count": 1,
                "decorative_alt_count": 0,
                "missing_dimensions_count": 0,
                "missing_alt_sample_urls": ["https://example.com/logo.png"],
            },
            "schema_types": ["LocalBusiness", "PostalAddress"],
            "nap_found": {"phones": ["(214) 555-0199"], "addresses": ["123 Main St, Dallas, TX"]},
            "og_tags": {"og:title": "Homepage Example"},
            "twitter_tags": {"twitter:card": "summary_large_image"},
            "analytics": {"ga4_id": "G-ABC1234", "gtm_id": "GTM-XYZ789"},
            "broken_outlinks": ["https://example.com/missing-page"],
        }
    ]
    evidence = [
        {
            "claim_type": "audit",
            "source_url": "https://example.com/",
            "timestamp": "2026-11-02T12:00:00Z",
            "http_status": 200,
            "dom_selector": "h1",
            "verbatim_text": "Main Business Title",
            "hash_sha256": "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
        }
    ]
    links = [
        {
            "source_url": "https://example.com/",
            "target_url": "https://example.com/services",
            "anchor_text": "Our Services",
            "placement": "header",
            "is_nofollow": False,
        }
    ]

    mgr = OutputManager(
        output_dir=str(tmp_path / "excel_export"),
        export_format="xlsx",
        export_mode="single",
    )
    result = mgr.export_site_crawl(
        target_domain="example.com",
        pages=pages,
        evidence_records=evidence,
        link_edges=links,
    )

    assert "site_crawl_excel" in result
    xlsx_path = Path(result["site_crawl_excel"])
    assert xlsx_path.exists()

    wb = openpyxl.load_workbook(str(xlsx_path))
    expected_sheets = [
        "Audit Summary",
        "Pages Overview",
        "SEO Issues",
        "Headings",
        "Internal Links & Broken",
        "Images & Media",
        "Structured Data & NAP",
        "Social & Analytics",
        "Evidence (EVD-01)",
    ]
    for s_name in expected_sheets:
        assert s_name in wb.sheetnames, f"Expected sheet '{s_name}' missing from workbook"

    # Verify atomic values in Headings sheet
    ws_headings = wb["Headings"]
    assert ws_headings.max_row >= 2
    row2_vals = [ws_headings.cell(row=2, column=c).value for c in range(1, 6)]
    assert row2_vals[0] == "https://example.com/"
    assert row2_vals[1] == "H1"
    assert row2_vals[3] == "Main Business Title"
    # Ensure no JSON string dumped in any cell
    for r in range(1, ws_headings.max_row + 1):
        for c in range(1, ws_headings.max_column + 1):
            val = str(ws_headings.cell(row=r, column=c).value or "")
            assert not (val.startswith("{") and val.endswith("}")), f"Found JSON blob in {r},{c}: {val}"

    # Test Citations Excel export
    citations_data = [
        {
            "platform": "Google Business Profile",
            "label": "Google Maps",
            "url": "https://google.com/maps/place/test",
            "status": "live",
            "nap_match": "exact",
            "phone": "(214) 555-0199",
            "address": "123 Main St, Dallas, TX",
            "business_name": "Test Plumbing Dallas",
            "rating": 4.9,
            "review_count": 87,
            "claimed": True,
            "evidence_id": "evd_1001",
        }
    ]
    res_cit = mgr.export_citations(
        business_name="Test Plumbing Dallas",
        citations=citations_data,
        evidence_records=evidence,
    )
    assert "citations_excel" in res_cit
    wb_cit = openpyxl.load_workbook(res_cit["citations_excel"])
    assert "Summary" in wb_cit.sheetnames
    assert "Citations Overview" in wb_cit.sheetnames
    assert "Secondary Phones & Badges" in wb_cit.sheetnames
    assert "Evidence (EVD-01)" in wb_cit.sheetnames

    # Test SERP Excel export
    serp_data = {
        "query": "plumber dallas",
        "location": "Dallas, TX",
        "local_pack": [
            {
                "position": 1,
                "name": "Dallas 24/7 Plumbers",
                "rating": 4.8,
                "review_count": 120,
                "categories": ["Plumber", "Emergency Service"],
                "address_snippet": "Downtown Dallas",
            }
        ],
        "organic": [
            {
                "position": 1,
                "url": "https://example.com/",
                "title": "Top Dallas Plumbers",
                "snippet": "Local trusted plumbing services in Dallas.",
            }
        ],
        "people_also_ask": [
            {
                "position": 1,
                "question": "How much does a plumber cost in Dallas?",
                "snippet": "Typical costs range from $150 to $400.",
            }
        ],
    }
    res_serp = mgr.export_serp(serp_data=serp_data, evidence_records=evidence)
    assert "serp_excel" in res_serp
    wb_serp = openpyxl.load_workbook(res_serp["serp_excel"])
    assert "Query Summary" in wb_serp.sheetnames
    assert "Local Pack" in wb_serp.sheetnames
    assert "Local Pack Categories" in wb_serp.sheetnames
    assert "Organic Rankings" in wb_serp.sheetnames
    assert "People Also Ask (PAA)" in wb_serp.sheetnames


def test_sqlite_source_of_truth_with_excel_export(tmp_path: Path) -> None:
    """Verify SQLite serves as primary persistence and exports directly into multi-worksheet Excel."""
    db_path = tmp_path / "canonical_truth.db"
    storage = SQLiteStorage(db_path)

    # Persist pages and link graph in SQLite
    storage.save_page(
        {
            "url": "https://example.com/home",
            "title": "Home Page",
            "status_code": 200,
            "word_count": 400,
            "h1": ["Welcome Home"],
        },
        domain="example.com",
    )
    storage.save_page(
        {
            "url": "https://example.com/about",
            "title": "About Us",
            "status_code": 200,
            "word_count": 250,
            "h1": ["About Our Company"],
        },
        domain="example.com",
    )
    storage.save_internal_links_batch([
        {
            "source_url": "https://example.com/home",
            "target_url": "https://example.com/about",
            "anchor_text": "About Us",
            "placement": "navigation",
            "is_nofollow": False,
        }
    ])
    storage.save_evidence_batch([
        {
            "claim_type": "audit",
            "source_url": "https://example.com/home",
            "timestamp": "2026-11-02T12:00:00Z",
            "http_status": 200,
            "dom_selector": "title",
            "verbatim_text": "Home Page",
            "hash_sha256": "abcdef1234567890",
        }
    ])

    # Ensure SQLite retains everything
    pages_in_db = storage.fetch_all_pages()
    assert len(pages_in_db) == 2
    links_in_db = storage.fetch_all_internal_links()
    assert len(links_in_db) == 1

    # Export from SQLite storage to Excel
    out_mgr = OutputManager(
        output_dir=str(tmp_path / "sqlite_excel_artifacts"),
        export_format="xlsx",
    )
    artifacts = out_mgr.export_from_storage(storage, mode="competitor", meta={"domain": "example.com"})

    assert "site_crawl_excel" in artifacts
    wb = openpyxl.load_workbook(artifacts["site_crawl_excel"])
    ws_links = wb["Internal Links & Broken"]
    assert ws_links.max_row >= 2
    # Verify link from SQLite graph is populated in Excel worksheet
    assert ws_links.cell(row=2, column=1).value == "https://example.com/home"
    assert ws_links.cell(row=2, column=2).value == "https://example.com/about"
    assert ws_links.cell(row=2, column=3).value == "Internal Link"
    assert ws_links.cell(row=2, column=4).value == "About Us"


def test_clean_csv_export_without_json_blobs(tmp_path: Path) -> None:
    """Verify CSV export sanitizes nested lists/dicts into clean scalar counts and summaries."""
    pages = [
        {
            "url": "https://example.com/test",
            "title": "Test Page",
            "status_code": 200,
            "h1": ["Primary Headline"],
            "h2": ["Secondary Subtitle"],
            "issues": [
                {
                    "url": "https://example.com/test",
                    "severity": "critical",
                    "category": "meta",
                    "issue": "Missing Title",
                    "details": "Page has empty title tag",
                },
                {
                    "url": "https://example.com/test",
                    "severity": "warning",
                    "category": "content",
                    "issue": "Thin Content",
                    "details": "Word count below threshold",
                },
            ],
            "nap_found": {"phones": ["123-456-7890"], "addresses": ["Dallas, TX"]},
            "og_tags": {"og:title": "Social Headline"},
        }
    ]
    mgr = OutputManager(
        output_dir=str(tmp_path / "clean_csv"),
        export_format="csv",
        export_mode="single",
    )
    mgr.export_site_crawl(target_domain="example.com", pages=pages)
    csv_file = tmp_path / "clean_csv" / "site_crawl_artifact.csv"
    assert csv_file.exists()

    content = csv_file.read_text(encoding="utf-8")
    # Verify no raw json syntax like {"level": or [{"severity":
    assert '{"level":' not in content
    assert '[{"severity":' not in content
    # Verify clean scalar summaries exist
    assert "Primary Headline" in content
    assert "123-456-7890" in content


def test_excel_xml_control_char_sanitization_and_chunked_export(tmp_path: Path) -> None:
    """Verify illegal XML control characters are stripped and multi-part chunked Excel exports scope records."""
    pages = [
        {
            "url": f"https://example.com/page-{i}",
            "title": f"Page {i}\x00\x0b\x1f Title",
            "meta_description": "Description with control chars \x07\x08",
            "status_code": 200,
            "h1": [f"H1 Tag {i}\x00"],
            "og_tags": {"og:title": f"OG {i}"},
            "twitter_tags": {"twitter:card": "summary"},
        }
        for i in range(5)
    ]
    evidence = [
        {
            "claim_type": "audit",
            "source_url": f"https://example.com/page-{i}",
            "timestamp": "2026-11-02T12:00:00Z",
            "http_status": 200,
            "dom_selector": "title",
            "verbatim_text": f"Verbatim \x00\x0c {i}",
            "hash_sha256": f"hash_{i}",
        }
        for i in range(5)
    ]
    links = [
        {
            "source_url": f"https://example.com/page-{i}",
            "target_url": "https://example.com/target",
            "anchor_text": f"Anchor \x00 {i}",
            "placement": "body",
            "is_nofollow": False,
        }
        for i in range(5)
    ]

    mgr = OutputManager(
        output_dir=str(tmp_path / "chunked_excel"),
        export_format="xlsx",
        export_mode="multi",
        export_chunk_size=2,
    )
    res = mgr.export_site_crawl(
        target_domain="example.com",
        pages=pages,
        evidence_records=evidence,
        link_edges=links,
    )
    assert "site_crawl_excel" in res
    part1 = Path(tmp_path / "chunked_excel" / "site_crawl_artifact_part_0001.xlsx")
    part3 = Path(tmp_path / "chunked_excel" / "site_crawl_artifact_part_0003.xlsx")
    assert part1.exists()
    assert part3.exists()

    wb1 = openpyxl.load_workbook(part1)
    # Part 1 has 2 pages -> Pages Overview has header + 2 rows = 3 rows
    assert wb1["Pages Overview"].max_row == 3
    # Title had \x00\x0b\x1f stripped cleanly
    assert wb1["Pages Overview"].cell(row=2, column=4).value == "Page 0 Title"
    # Social & Analytics populated og:title
    assert wb1["Social & Analytics"].cell(row=2, column=2).value == "OG 0"
    # Links scoped to the 2 pages in chunk 1 (header + 2 rows = 3)
    assert wb1["Internal Links & Broken"].max_row == 3
    # Evidence scoped to the 2 pages in chunk 1 (header + 2 rows = 3)
    assert wb1["Evidence (EVD-01)"].max_row == 3

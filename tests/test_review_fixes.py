"""Regression tests for the code-review findings on the crawler-accuracy change."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from conftest import SITE, page_html
from src import scraper
from src.adapters.competitor import CompetitorSiteAdapter
from src.classifier import DEFAULT_PROTOTYPE_TOKENS, ClassifierConfig, classify_pages, token_in_segments
from src.main import _load_status_markers, main
from src.orchestrator import CrawlerOrchestrator
from src.post_crawl import run_post_crawl_stage
from src.report import build_report_data, render_markdown
from src.seo_auditor import SEOAuditConfig
from src.storage import SQLiteStorage
from src.transport import classify_transport_error


def _fake_orchestrator_env(monkeypatch: pytest.MonkeyPatch, pages: dict[str, dict[str, Any]]) -> list[str]:
    """Patch scraper so the orchestrator 'crawls' the given {url: result-dict} map offline."""
    fetched: list[str] = []

    def fake_scrape(job: dict[str, Any], shared_client: Any = None, cookie_jar: Any = None, audit_cfg: Any = None) -> dict[str, Any]:
        fetched.append(job["url"])
        spec = pages.get(job["url"], {"status_code": 404, "internal_urls": [], "link_edges": []})
        status = spec.get("status_code", 200)
        return {
            "success": True,
            "status_code": status,
            "domain": "example.com",
            "depth": job.get("depth", 0),
            "internal_urls": spec.get("internal_urls", []),
            "link_edges": spec.get("link_edges", []),
            "issues": [],
            "result": {"url": job["url"], "status_code": status, "title": "T", "viewport": "w", "lang": "en"},
            "evidence": [],
        }

    monkeypatch.setattr(scraper, "parse_proxies_file", lambda _: [])
    monkeypatch.setattr(scraper, "create_flaresolverr_session", lambda *a, **k: "sess")
    monkeypatch.setattr(scraper, "destroy_flaresolverr_session", lambda *a, **k: True)
    monkeypatch.setattr(scraper, "execute_single_scrape", fake_scrape)
    return fetched


# ── 1. Crawl trap: malformed targets are verified, never crawled ────────────

def test_malformed_targets_are_verified_not_crawled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = "https://example.com/"
    trap = "https://example.com/www.facebook.com/foo"
    fetched = _fake_orchestrator_env(monkeypatch, {
        home: {
            "internal_urls": [],  # the adapter never lists malformed targets for queueing
            "link_edges": [{
                "source_url": home, "target_url": trap, "placement": "footer",
                "malformed_relative": True, "intended_host": "www.facebook.com", "raw_href": "www.facebook.com/foo",
            }],
        },
    })
    verified: list[list[str]] = []

    def fake_verify(urls: list[str], client: Any = None, timeout: float = 10.0):
        verified.append(list(urls))
        return {u: 200 for u in urls}, {}

    monkeypatch.setattr(scraper, "verify_link_targets", fake_verify)
    orch = CrawlerOrchestrator(mode="competitor", domain="example.com", use_sitemap=False,
                               output_dir=str(tmp_path), max_pages=10, concurrency=1)
    orch.run()
    assert fetched == [home]
    assert verified == [[trap]]
    page = SQLiteStorage(tmp_path / "crawl_results.db").fetch_all_pages()[0]
    malformed = [i for i in page["issues"] if i["issue"] == "Malformed Relative Link"]
    assert [i["severity"] for i in malformed] == ["warning"]  # resolved URL returns 200


def test_soft_404_site_does_not_grow_paths() -> None:
    """Even when the resolved URL serves the same footer again, nothing is queued."""
    html = "<html><body><footer><a href='www.facebook.com/foo'>fb</a></footer></body></html>"
    adapter = CompetitorSiteAdapter()
    _, _, discovered, edges = adapter.parse(url="https://e.com/a/", html=html, http_status=200)
    assert discovered == [] and edges[0].malformed_relative
    _, _, discovered2, _ = adapter.parse(url=edges[0].target_url, html=html, http_status=200)
    assert discovered2 == []


def test_unvisited_malformed_only_and_verify_helper(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    storage.save_page({"url": "https://e.com/", "status_code": 200})
    storage.save_internal_links_batch([
        {"source_url": "https://e.com/", "target_url": "https://e.com/www.x.com", "malformed_relative": True},
        {"source_url": "https://e.com/", "target_url": "https://e.com/never-crawled"},
    ])
    assert storage.get_unvisited_link_targets(malformed_only=True) == ["https://e.com/www.x.com"]
    assert storage.get_unvisited_link_targets() == ["https://e.com/never-crawled", "https://e.com/www.x.com"]

    client = MagicMock(spec=httpx.Client)
    r404 = MagicMock(spec=httpx.Response)
    r404.status_code = 404
    r404.headers = {}
    client.get.side_effect = [r404, httpx.ConnectError("[Errno -2] Name or service not known")]
    status, errors = scraper.verify_link_targets(["https://e.com/a", "https://e.com/b"], client=client)
    assert status == {"https://e.com/a": 404}
    assert errors == {"https://e.com/b": "dns_failure"}


# ── 2. Prototype tokens ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/careers-old/", "/memberships-new/", "/full-locations-demo/", "/map-test-page/",
                                  "/home-copy-2/", "/test/", "/staging-home/", "/about-draft/"])
def test_prototype_tokens_flag(path: str) -> None:
    assert token_in_segments("https://e.com" + path, DEFAULT_PROTOTYPE_TOKENS), path


@pytest.mark.parametrize("path", ["/new-braunfels/", "/new-patients/", "/old-town-spa/", "/covid-test/",
                                  "/reviews-and-testimonials/", "/newest-offers/", "/copywriting/", "/whats-new-in-skincare/"])
def test_prototype_tokens_do_not_flag_real_pages(path: str) -> None:
    assert token_in_segments("https://e.com" + path, DEFAULT_PROTOTYPE_TOKENS) is None, path


def test_prototype_allowlist_reaches_classifier(tmp_path: Path) -> None:
    """An allowlisted page that matches a token can still be a directory hub."""
    storage = SQLiteStorage(tmp_path / "x.db")
    adapter = CompetitorSiteAdapter()
    clinics = [f"clinic-{i}" for i in range(10)]
    hub = SITE + "/locations-new/"
    hub_html = page_html("Locations", "<h1>Locations</h1>" + "".join(f"<a href='/{c}/'>Spa at {c}</a>" for c in clinics))
    for url, html in [(hub, hub_html)] + [
        (f"{SITE}/{c}/", page_html("Med Spa in Austin, TX", f"<h1>Spa</h1><a href='tel:51255501{i:02d}'>x</a>"))
        for i, c in enumerate(clinics)
    ]:
        rec, _, _, edges = adapter.parse(url=url, html=html, http_status=200, final_url=url)
        storage.save_page(rec.model_dump())
        storage.save_internal_links_batch(edges)
    run_post_crawl_stage(storage, audit_cfg=SEOAuditConfig(prototype_allowlist=(hub,)), crawl_date=date(2026, 11, 5))
    pages = {p["url"]: p for p in storage.fetch_all_pages()}
    assert pages[hub]["page_type"] == "directory_hub"
    assert not [i for i in pages[hub]["issues"] if "Prototype" in i["issue"]]


# ── 3. Small multi-location site without a directory hub ────────────────────

def _small_site(tmp_path: Path, with_locations: bool = True) -> SQLiteStorage:
    storage = SQLiteStorage(tmp_path / "crawl_results.db")
    adapter = CompetitorSiteAdapter()
    locs = {
        "austin": ("100 Main St, Austin, TX 78701", "512-555-0101"),
        "dallas": ("200 Elm St, Dallas, TX 75201", "214-555-0102"),
        "houston": ("300 Oak Ave, Houston, TX 77002", "713-555-0103"),
        "waco": ("400 Pine Rd, Waco, TX 76701", "254-555-0104"),
    }
    pages = {"/": page_html("Home", "<h1>Home</h1>"),
             "/contact/": page_html("Contact", "<h1>Contact us</h1><p>Email us any time.</p>")}
    if with_locations:
        pages["/locations/"] = page_html("Locations", "<h1>Locations</h1>" + "".join(f"<a href='/{s}/'>{s}</a>" for s in locs))
        for slug, (addr, phone) in locs.items():
            body = (f"<h1>Med Spa in {slug.title()}, TX</h1><p class='address'>{addr}</p><a href='tel:{phone}'>{phone}</a>"
                    "<iframe src='https://www.google.com/maps/embed?pb=!2d-97.1!3d30.1'></iframe>")
            pages[f"/{slug}/"] = page_html(f"{slug.title()} Location", body)
        pages["/austin/botox/"] = page_html("Botox Austin", "<h1>Botox</h1><p class='address'>100 Main St, Austin, TX 78701</p>"
                                            "<iframe src='https://www.google.com/maps/embed?pb=!2d-97.1!3d30.1'></iframe>")
    for path, html in pages.items():
        rec, _, _, edges = adapter.parse(url=SITE + path, html=html, http_status=200, final_url=SITE + path)
        storage.save_page(rec.model_dump())
        storage.save_internal_links_batch(edges)
    run_post_crawl_stage(storage, crawl_date=date(2026, 11, 5))
    return storage


def test_small_site_clinics_inferred_from_on_page_signals(tmp_path: Path) -> None:
    storage = _small_site(tmp_path)
    pages = {p["url"].replace(SITE, ""): p for p in storage.fetch_all_pages()}
    for slug in ("austin", "dallas", "houston", "waco"):
        p = pages[f"/{slug}/"]
        assert p["page_type"] == "clinic_location", slug
        assert any(i["issue"] == "Missing LocalBusiness / Clinic Schema" for i in p["issues"]), slug
    assert pages["/austin/botox/"]["page_type"] == "clinic_service"
    assert pages["/contact/"]["page_type"] == "other"
    assert pages["/locations/"]["page_type"] != "directory_hub"  # only 4 entries (< hub_min_links)

    md = render_markdown(build_report_data(tmp_path / "crawl_results.db"))
    assert "No directory hub was detected; clinic pages were inferred" in md


def test_no_clinics_is_explained_in_report(tmp_path: Path) -> None:
    _small_site(tmp_path, with_locations=False)
    md = render_markdown(build_report_data(tmp_path / "crawl_results.db"))
    assert "No clinic location pages were detected, so LocalBusiness schema checks did not run" in md


def test_fallback_not_used_when_directory_exists(site_storage: SQLiteStorage) -> None:
    from conftest import pages_by_path

    reasons = [r for p in pages_by_path(site_storage).values() for r in p.get("page_type_reasons", [])]
    assert not any("no directory hub detected" in r for r in reasons)


# ── 4. Transport classification ─────────────────────────────────────────────

def test_tls_handshake_timeout_is_timeout() -> None:
    assert classify_transport_error("_ssl.c:990: The handshake operation timed out") == "timeout"
    assert classify_transport_error("[Errno 104] Connection reset by peer (_ssl.c:2427)") == "connection_reset"
    assert classify_transport_error("[SSL: WRONG_VERSION_NUMBER] wrong version number") == "tls_verify_failed"


# ── 5. --status-markers parsing ─────────────────────────────────────────────

def test_long_inline_status_markers(tmp_path: Path) -> None:
    phrases = [f"we are closed for renovation phase {i}" for i in range(12)]
    inline = '{"temporarily_closed": [' + ", ".join(f'"{p}"' for p in phrases) + "]}"
    assert len(inline) > 255
    assert _load_status_markers(inline) == {"temporarily_closed": tuple(phrases)}

    f = tmp_path / "markers.json"
    f.write_text('{"coming_soon": ["opening this spring"]}')
    assert _load_status_markers(str(f)) == {"coming_soon": ("opening this spring",)}


@pytest.mark.parametrize("value, msg", [
    ('{"shut": ["x"]}', "unknown status"),
    ('{"closed": "x"}', "list of phrases"),
    ("{not json", "invalid JSON"),
    ("/no/such/file.json", "neither inline JSON nor an existing file"),
])
def test_bad_status_markers_rejected(value: str, msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        _load_status_markers(value)


def test_bad_status_markers_is_cli_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "competitor", "--domain", "e.com", "--status-markers", '{"shut": ["x"]}'])
    assert exc.value.code == 2
    assert "unknown status" in capsys.readouterr().err


# ── 6. --locations-file validated before crawling; post-crawl failure still exports ──

def test_missing_locations_file_fails_before_crawl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(CrawlerOrchestrator, "run", lambda self: pytest.fail("must not crawl"))
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "competitor", "--domain", "e.com", "--output-dir", str(tmp_path),
              "--locations-file", str(tmp_path / "missing.csv")])
    assert exc.value.code == 2
    assert "Locations file not found" in capsys.readouterr().err

    empty = tmp_path / "empty.csv"
    empty.write_text("name,status\nX,open\n")
    with pytest.raises(ValueError, match="no rows with a 'url' column"):
        CrawlerOrchestrator(mode="competitor", domain="e.com", output_dir=str(tmp_path), locations_file=str(empty))


def test_post_crawl_failure_still_exports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.orchestrator as orch_mod

    home = "https://example.com/"
    _fake_orchestrator_env(monkeypatch, {home: {
        "link_edges": [{"source_url": home, "target_url": "https://example.com/gone", "placement": "body"}],
    }})

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(orch_mod, "run_post_crawl_stage", boom)
    orch = CrawlerOrchestrator(mode="competitor", domain="example.com", use_sitemap=False,
                               output_dir=str(tmp_path), max_pages=10, concurrency=1, verify_all_links=False)
    artifacts = orch.run()
    assert "site_crawl_artifact" in artifacts
    assert "classifier exploded" in orch.post_crawl_error
    # Link-graph sync still ran: inlinks/broken fields exist
    page = SQLiteStorage(tmp_path / "crawl_results.db").fetch_all_pages()[0]
    assert "broken_outlinks" in page


# ── 7. Secondary hubs need clinic names in headings ─────────────────────────

def test_secondary_hub_requires_named_clinics(site_storage: SQLiteStorage) -> None:
    adapter = CompetitorSiteAdapter()
    url = SITE + "/our-spas/"
    html = page_html("Our spas", "<h1>Visit us</h1><h2>Book today</h2><a href='/oak-hill/'>one</a><a href='/aspen/'>two</a>")
    rec, _, _, edges = adapter.parse(url=url, html=html, http_status=200, final_url=url)
    site_storage.save_page(rec.model_dump())
    site_storage.save_internal_links_batch(edges)
    res = classify_pages(site_storage.fetch_all_pages(), site_storage.fetch_all_internal_links(),
                         ClassifierConfig(crawl_date=date(2026, 11, 5)))
    assert res[url]["page_type"] != "directory_hub"
    assert res[SITE + "/washington/"]["page_type"] == "directory_hub"  # names Maple St and Birch Bay


# ── 8. Status 0 without a transport error is not a broken link ──────────────

def test_status_zero_without_transport_error_is_not_broken(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    storage.save_page({"url": "https://e.com/", "status_code": 200})
    storage.save_page({"url": "https://e.com/challenged", "status_code": 0})
    storage.save_page({"url": "https://e.com/tls", "status_code": 0, "transport_error": "Hostname mismatch, certificate is not valid"})
    storage.save_internal_links_batch([
        {"source_url": "https://e.com/", "target_url": "https://e.com/challenged"},
        {"source_url": "https://e.com/", "target_url": "https://e.com/tls"},
    ])
    storage.post_crawl_sync()
    home = next(p for p in storage.fetch_all_pages() if p["url"] == "https://e.com/")
    assert home["broken_outlinks"] == ["https://e.com/tls"]
    assert home["broken_outlink_details"][0]["error_type"] == "tls_hostname_mismatch"
    data = build_report_data(tmp_path / "x.db")
    assert [b["target_url"] for b in data["broken_links"]["items"]] == ["https://e.com/tls"]


# ── 9. Empty duplicate_pairs table is "no duplicates", not "old crawl" ───────

def test_empty_duplicate_pairs_has_no_legacy_note(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "crawl_results.db")
    storage.save_page({"url": "https://e.com/", "status_code": 200})
    storage.save_duplicate_pairs([])
    data = build_report_data(tmp_path / "crawl_results.db")
    assert data["duplicates"]["source"] == "duplicate_pairs"
    assert "no stored duplicate edges" not in render_markdown(data)

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        "CREATE TABLE pages (url TEXT PRIMARY KEY, raw_json TEXT NOT NULL);"
        "CREATE TABLE internal_links (source_url TEXT, target_url TEXT, target_status INTEGER);"
        "CREATE TABLE seo_issues (url TEXT, severity TEXT, category TEXT, issue TEXT, details TEXT);"
        "INSERT INTO pages VALUES ('https://e.com/', '{\"url\": \"https://e.com/\", \"status_code\": 200}');"
    )
    conn.commit()
    conn.close()
    assert build_report_data(legacy)["duplicates"]["source"] == "issue_details"

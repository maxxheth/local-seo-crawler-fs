"""Tests for plans/2026-11-05-crawler-accuracy-and-deterministic-reporting.md (WS1, WS2, WS4–WS7).

Fixtures are synthetic (see conftest.py); assertions name specific URLs, never fixed totals.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from conftest import SITE, issues_for, pages_by_path
from src.adapters.competitor import CompetitorSiteAdapter
from src.classifier import ClassifierConfig, classify_pages, find_stale_dates, token_in_segments
from src.models import PageTechnicalRecord
from src.performance import fetch_psi_field_data, run_timing_sample
from src.scraper import classify_transport_error, execute_single_scrape
from src.seo_auditor import SEOAuditConfig, audit_page, audit_site_context, generate_site_summary
from src.storage import SQLiteStorage


def _parse(url: str, html: str, final_url: str | None = None):
    return CompetitorSiteAdapter().parse(url=url, html=html, http_status=200, final_url=final_url or url)


# ── WS1: RFC 3986 link resolution ───────────────────────────────────────────

def test_ws1_bare_domain_resolves_against_final_url_after_redirect() -> None:
    html = "<html><body><main><a href='www.youtube.com/watch?v=x'>video</a></main></body></html>"
    record, _, discovered, edges = _parse(
        "https://example.com/blog/post", html, final_url="https://example.com/blog/post/"
    )
    assert edges[0].target_url == "https://example.com/blog/post/www.youtube.com/watch?v=x"
    assert edges[0].target_url != "https://example.com/blog/www.youtube.com/watch?v=x"
    # Recorded but never queued: a site answering unknown paths with 200 would otherwise
    # yield /post/www.youtube.com/www.youtube.com/... on every hop (crawl trap)
    assert discovered == []
    assert edges[0].malformed_relative is True
    assert edges[0].intended_host == "www.youtube.com"
    assert edges[0].raw_href == "www.youtube.com/watch?v=x"
    assert record.external_outlinks_count == 0


def test_ws1_base_href_is_honoured() -> None:
    html = "<html><head><base href='/foo/'></head><body><a href='bar/'>x</a><a href='/abs/'>y</a></body></html>"
    _, _, discovered, _ = _parse("https://example.com/some/page/", html)
    assert discovered == ["https://example.com/foo/bar/", "https://example.com/abs/"]


def test_ws1_regular_links_unchanged() -> None:
    html = """<html><body>
      <a href="mailto:a@b.com">m</a><a href="tel:5125550000">t</a><a href="javascript:void(0)">j</a>
      <a href="#frag">f</a><a href="//cdn.example.net/x">cdn</a><a href="https://example.com/abs/">abs</a>
      <a href="https://other.org/page">ext</a><a href="relative/">rel</a><a href="../up/">up</a>
    </body></html>"""
    record, _, discovered, edges = _parse("https://example.com/a/b/", html)
    assert discovered == [
        "https://example.com/abs/",
        "https://example.com/a/b/relative/",
        "https://example.com/a/up/",
    ]
    assert record.external_outlinks_count == 2  # //cdn.example.net and other.org
    assert sorted(record.external_link_hosts) == ["cdn.example.net", "other.org"]
    assert not any(e.malformed_relative for e in edges)


def test_ws1_malformed_link_severity_and_broken_outlinks(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    post = pages["/blog/new-location-at-oak-hill/"]
    malformed = {i["severity"]: i["details"] for i in issues_for(post, "Malformed Relative Link")}
    # target 404 -> error; target 301 -> 200 -> warning
    assert "www.youtube.com/watch?v=abc" in malformed["error"]
    assert "HTTP 404" in malformed["error"]
    assert "www.youtube.com/watch?v=ok" in malformed["warning"]
    broken = SITE + "/blog/new-location-at-oak-hill/www.youtube.com/watch?v=abc"
    assert broken in post["broken_outlinks"]
    assert SITE + "/blog/new-location-at-oak-hill/www.youtube.com/watch?v=ok" not in post["broken_outlinks"]
    # Reported once, as Malformed Relative Link, not also as a generic Broken Internal Link
    assert not any(broken in i["details"] for i in issues_for(post, "Broken Internal Link"))


# ── WS2: transport-error propagation ────────────────────────────────────────

@pytest.mark.parametrize(
    "error, expected",
    [
        ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, certificate is not valid for 'x'.", "tls_hostname_mismatch"),
        ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired", "tls_verify_failed"),
        (httpx.ConnectError("[Errno -2] Name or service not known"), "dns_failure"),
        (httpx.ReadTimeout(""), "timeout"),
        (httpx.ConnectError("[Errno 111] Connection refused"), "connection_refused"),
        (httpx.ReadError("[Errno 104] Connection reset by peer"), "connection_reset"),
        ("something odd", "other"),
        ("", ""),
    ],
)
def test_ws2_transport_error_classification(error, expected) -> None:
    assert classify_transport_error(error) == expected


def test_ws2_scrape_records_transport_error_type() -> None:
    client = MagicMock(spec=httpx.Client)
    hop1 = MagicMock(spec=httpx.Response)
    hop1.status_code = 301
    hop1.headers = {"location": "https://franchise.example.org/"}
    client.get.side_effect = [hop1, httpx.ConnectError("[Errno -2] Name or service not known")]
    res = execute_single_scrape({"mode": "competitor", "url": "https://example.com/franchising/", "domain": "example.com"}, shared_client=client)
    assert res["success"] is True and res["status_code"] == 301
    assert res["result"]["transport_error_type"] == "dns_failure"


def test_ws2_redirect_to_tls_failure_is_a_broken_outlink(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    home = pages["/"]
    franchising = SITE + "/franchising/"
    assert franchising in home["broken_outlinks"]
    detail = next(d for d in home["broken_outlink_details"] if d["target_url"] == franchising)
    assert detail["error_type"] == "tls_hostname_mismatch"
    assert detail["target_status"] == 0
    assert any(franchising in i["details"] and "tls_hostname_mismatch" in i["details"] for i in issues_for(home, "Broken Internal Link"))

    summary = generate_site_summary([PageTechnicalRecord(**p) for p in site_storage.fetch_all_pages()])
    assert summary.broken_targets_by_error_type.get("tls_hostname_mismatch") == 1
    assert summary.broken_internal_links_count == sum(summary.broken_targets_by_error_type.values())


def test_ws2_verify_links_error_map(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    storage.save_page({"url": "https://e.com/", "status_code": 200})
    storage.save_internal_links_batch([{"source_url": "https://e.com/", "target_url": "https://e.com/gone", "placement": "body"}])
    storage.update_link_target_statuses({}, {"https://e.com/gone": "timeout"})
    storage.post_crawl_sync()
    page = storage.fetch_all_pages()[0]
    assert page["broken_outlink_details"] == [{"target_url": "https://e.com/gone", "target_status": 0, "error_type": "timeout"}]


# ── D9: address extraction ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text, expected",
    [
        ("NPOC 3989 Boston Street Baltimore, MD 21224 410-555-0000", "3989 Boston Street Baltimore, MD 21224"),
        ("relocated to 560 W 800 N, Suite 121 Orem, Utah 84057 801", "560 W 800 N, Suite 121 Orem, Utah 84057"),
        ("14900 Avery Ranch Blvd, C-100 Austin, TX 78717", "14900 Avery Ranch Blvd, C-100 Austin, TX 78717"),
    ],
)
def test_d9_address_formats_extracted(text: str, expected: str) -> None:
    record, _, _, _ = _parse("https://e.com/x/", f"<html><body><main><p>{text}</p></main></body></html>")
    assert expected in record.nap_found.addresses


# ── WS4: classification ─────────────────────────────────────────────────────

def test_ws4_page_types(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    # Mega-menu on every page lists all clinics but never creates hubs
    assert pages["/"]["page_type"] == "other"
    assert pages["/reviews-and-testimonials/"]["page_type"] == "other"
    assert pages["/locations/"]["page_type"] == "directory_hub"
    assert pages["/washington/"]["page_type"] == "directory_hub"  # regional (secondary) hub
    assert pages["/full-locations-demo/"]["page_type"] != "directory_hub"  # prototype copy
    assert pages["/locations/thank-you-oak-hill-location/"]["page_type"] == "utility_page"
    assert pages["/locations/thank-you-cedar-ridge-location/"]["page_type"] == "utility_page"
    assert pages["/oak-hill/botox/"]["page_type"] == "clinic_service"
    assert pages["/book/"]["page_type"] != "clinic_location"
    assert pages["/blog/new-location-at-oak-hill/"]["page_type"] == "blog_post"
    for c in ("oak-hill", "pine-grove", "elm-park", "cedar-ridge", "maple-st", "birch-bay", "willow", "harbor", "aspen", "spruce"):
        assert pages[f"/{c}/"]["page_type"] == "clinic_location", c
        assert pages[f"/{c}/"]["page_type_reasons"]


def test_ws4_location_statuses(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    assert pages["/cedar-ridge/"]["location_status"] == "temporarily_closed"
    # Moved: displayed address matches another clinic page, even with a map embed present
    assert pages["/maple-st/"]["location_status"] == "moved"
    assert pages["/maple-st/"]["moved_to"] == SITE + "/birch-bay/"
    # Relocated to an address no other clinic uses -> still open
    assert pages["/willow/"]["location_status"] == "open"
    assert pages["/willow/"]["relocated"] is True
    # Ownership transferred is never mapped to moved
    assert pages["/harbor/"]["location_status"] == "ownership_transferred"
    # "Coming soon" outside the opening section does not change status
    assert pages["/pine-grove/"]["location_status"] == "open"
    # Phone but no address: still a clinic, flagged
    assert pages["/elm-park/"]["location_status"] == "open"
    assert issues_for(pages["/elm-park/"], "Clinic Page Missing Visible Address")[0]["severity"] == "warning"


def test_ws4_schema_rule_applies_only_to_open_or_unknown_clinics(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    name = "Missing LocalBusiness / Clinic Schema"
    for c in ("oak-hill", "elm-park", "willow", "aspen", "birch-bay"):
        assert issues_for(pages[f"/{c}/"], name), c
    assert not issues_for(pages["/pine-grove/"], name)  # has DaySpa schema
    for c in ("cedar-ridge", "maple-st", "harbor"):
        assert not issues_for(pages[f"/{c}/"], name), c
    assert issues_for(pages["/cedar-ridge/"], "Closed/moved clinic page: confirm indexing and schema approach")
    moved_info = issues_for(pages["/maple-st/"], "Closed/moved clinic page: confirm indexing and schema approach")[0]
    assert SITE + "/birch-bay/" in moved_info["details"]
    assert issues_for(pages["/harbor/"], "Ownership transferred: confirm LocalBusiness schema")[0]["severity"] == "info"
    # Thank-you pages under /locations/ are not clinics
    assert not issues_for(pages["/locations/thank-you-oak-hill-location/"], name)
    assert issues_for(pages["/locations/thank-you-oak-hill-location/"], "Indexable Thank-You / Confirmation Page")


def test_ws4_stale_notice_and_duplicate_address(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    stale = issues_for(pages["/cedar-ridge/"], "Stale Status Notice")
    assert stale and stale[0]["severity"] == "warning" and "October" in stale[0]["details"]
    assert issues_for(pages["/aspen/"], "Duplicate Clinic Address")
    assert issues_for(pages["/spruce/"], "Duplicate Clinic Address")
    # The moved_to relationship explains the shared Birch Bay address
    assert not issues_for(pages["/maple-st/"], "Duplicate Clinic Address")
    assert not issues_for(pages["/birch-bay/"], "Duplicate Clinic Address")


def test_ws4_stale_date_parsing() -> None:
    nov5 = date(2026, 11, 5)
    assert find_stale_dates("We anticipate reopening in October", nov5) == ["in October"]
    assert find_stale_dates("We anticipate reopening in December", nov5) == []
    assert find_stale_dates("Reopening in November", nov5) == []
    assert find_stale_dates("Closed until October 15, 2026", nov5) == ["October 15, 2026"]
    assert find_stale_dates("Closed until January 2027", nov5) == []
    assert find_stale_dates("You may reach us by phone", nov5) == []


def test_ws4_locations_file_overrides_detection(tmp_path: Path, site_storage: SQLiteStorage) -> None:
    loc = tmp_path / "stores.csv"
    loc.write_text(f"url,name,status\n{SITE}/oak-hill/,Oak Hill,coming soon\n{SITE}/book/,Booking,open\n")
    res = classify_pages(
        site_storage.fetch_all_pages(),
        site_storage.fetch_all_internal_links(),
        ClassifierConfig(locations_file=str(loc), crawl_date=date(2026, 11, 5)),
    )
    clinics = {u for u, r in res.items() if r["page_type"] == "clinic_location"}
    assert clinics == {SITE + "/oak-hill/", SITE + "/book/"}
    assert res[SITE + "/oak-hill/"]["location_status"] == "coming_soon"
    assert res[SITE + "/oak-hill/"]["status_evidence"]["source"] == "locations_file"


def test_ws4_directory_url_override(site_storage: SQLiteStorage) -> None:
    res = classify_pages(
        site_storage.fetch_all_pages(),
        site_storage.fetch_all_internal_links(),
        ClassifierConfig(directory_urls=(SITE + "/reviews-and-testimonials/",), crawl_date=date(2026, 11, 5)),
    )
    assert res[SITE + "/reviews-and-testimonials/"]["page_type"] == "directory_hub"


def test_ws4_post_crawl_stage_is_idempotent(site_storage: SQLiteStorage) -> None:
    from src.post_crawl import run_post_crawl_stage

    before = sorted((p["url"], i["issue"]) for p in site_storage.fetch_all_pages() for i in p["issues"])
    run_post_crawl_stage(site_storage, crawl_date=date(2026, 11, 5))
    after = sorted((p["url"], i["issue"]) for p in site_storage.fetch_all_pages() for i in p["issues"])
    assert before == after
    with site_storage._get_connection() as conn:
        table = sorted((r["url"], r["issue"]) for r in conn.execute("SELECT url, issue FROM seo_issues"))
    assert table == after


# ── WS5: prototype & staging detection ──────────────────────────────────────

def test_ws5_prototype_pages(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    proto = "Indexable Prototype / Staging Page"
    assert issues_for(pages["/careers-old/"], proto)[0]["severity"] == "error"  # indexable + in sitemap
    assert issues_for(pages["/memberships-new/"], proto)[0]["severity"] == "warning"  # indexable only
    assert issues_for(pages["/full-locations-demo/"], proto)
    assert issues_for(pages["/map-test-page/"], "Prototype / Staging Page")[0]["severity"] == "info"  # noindex
    for path in ("/reviews-and-testimonials/", "/blog/new-location-at-oak-hill/", "/blog/congratulations-to-our-newest-team/"):
        assert not issues_for(pages[path], proto), path
        assert not issues_for(pages[path], "Prototype / Staging Page"), path


def test_ws5_token_matching_is_whole_word() -> None:
    toks = ("test", "new", "old", "demo")
    assert token_in_segments("https://e.com/map-test-page/", toks) == ("test", "map-test-page")
    assert token_in_segments("https://e.com/reviews-and-testimonials/", toks) is None
    assert token_in_segments("https://e.com/newest-offers/", toks) is None
    assert token_in_segments("https://e.com/bold-look/", toks) is None


def test_ws5_staging_canonical_flagged(site_storage: SQLiteStorage) -> None:
    pages = pages_by_path(site_storage)
    iss = issues_for(pages["/services/facials/"], "Staging Hostname Reference")
    assert iss and iss[0]["severity"] == "error" and "staging-2026.example.com" in iss[0]["details"]


# ── WS6: performance ────────────────────────────────────────────────────────

def test_ws6_crawl_timing_is_info_by_default() -> None:
    rec = PageTechnicalRecord(url="https://e.com/", status_code=200, response_time_ms=5000, viewport="w", title="t" * 40)
    slow = [i for i in audit_page(rec, SEOAuditConfig()) if i.category == "Performance"]
    assert [(i.issue, i.severity) for i in slow] == [("Slow Crawl Response (crawler-measured)", "info")]


def test_ws6_sampled_ttfb_can_warn() -> None:
    pages = [{"url": "https://e.com/slow/", "status_code": 200}, {"url": "https://e.com/fast/", "status_code": 200}]
    sample = {"samples": [
        {"url": "https://e.com/slow/", "ttfb_ms": 1800, "cache_status": "MISS"},
        {"url": "https://e.com/fast/", "ttfb_ms": 120, "cache_status": "HIT"},
    ]}
    ctx = audit_site_context(pages, [], SEOAuditConfig(), timing_sample=sample)
    assert [(i.issue, i.severity) for i in ctx["https://e.com/slow/"]] == [("Slow Server Response (sequential sample)", "warning")]
    assert "https://e.com/fast/" not in ctx


def test_ws6_timing_sample_is_sequential_and_cache_busted() -> None:
    calls: list[str] = []
    active = {"n": 0, "max": 0}

    class FakeStream:
        def __init__(self, url: str) -> None:
            self.url = url
            self.status_code = 200
            self.headers = {"cf-cache-status": "MISS"}

        def __enter__(self):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            return self

        def __exit__(self, *a):
            active["n"] -= 1

    client = MagicMock()
    client.stream.side_effect = lambda method, url: (calls.append(url), FakeStream(url))[1]
    ticks = iter([0.0, 0.2, 1.0, 1.5, 2.0, 2.1])
    out = run_timing_sample(["https://e.com/a/", "https://e.com/b/?x=1", "https://e.com/c/"], client=client, clock=lambda: next(ticks))
    assert active["max"] == 1
    assert [c.split("?")[0] for c in calls] == ["https://e.com/a/", "https://e.com/b/", "https://e.com/c/"]
    assert all("_lsc_cb=" in c for c in calls) and "x=1" in calls[1]
    assert [s["ttfb_ms"] for s in out["samples"]] == [200, 500, 100]
    assert out["median_ttfb_ms"] == 200.0 and out["p95_ttfb_ms"] == 500.0
    assert out["samples"][0]["cache_status"] == "MISS"


def test_ws6_psi_client_mocked_and_absent() -> None:
    assert fetch_psi_field_data("", ["https://e.com/"])["measured"] is False

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "loadingExperience": {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2100, "category": "FAST"}}},
        "originLoadingExperience": {
            "metrics": {
                "EXPERIMENTAL_TIME_TO_FIRST_BYTE": {"percentile": 800, "category": "AVERAGE"},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5, "category": "FAST"},
            }
        },
    }
    client = MagicMock()
    client.get.return_value = resp
    out = fetch_psi_field_data("KEY", ["https://e.com/"], client=client)
    assert out["measured"] is True
    assert out["origin"]["ttfb_ms"] == {"p75": 800, "category": "AVERAGE"}
    assert out["origin"]["cls"]["p75"] == 0.05
    assert out["urls"][0]["metrics"]["lcp_ms"]["p75"] == 2100

    client.get.side_effect = httpx.ConnectTimeout("")
    failed = fetch_psi_field_data("KEY", ["https://e.com/"], client=client)
    assert failed["measured"] is False and failed["errors"][0]["error"] == "timeout"


# ── WS7: internal links to redirects ────────────────────────────────────────

def test_ws7_internal_link_to_redirect(tmp_path: Path) -> None:
    from src.report import build_report_data

    storage = SQLiteStorage(tmp_path / "crawl_results.db")
    storage.save_page({"url": "https://e.com/", "status_code": 200})
    storage.save_page({"url": "https://e.com/x", "status_code": 301, "final_url": "https://e.com/x/"})
    storage.save_page({"url": "https://e.com/x/", "status_code": 200})
    storage.save_page({"url": "https://e.com/services/microneedling/", "status_code": 301, "final_url": "https://e.com/services/micro-needling/"})
    storage.save_page({"url": "https://e.com/services/micro-needling/", "status_code": 200})
    storage.save_internal_links_batch([
        {"source_url": "https://e.com/", "target_url": "https://e.com/x", "placement": "navigation"},
        {"source_url": "https://e.com/", "target_url": "https://e.com/services/microneedling/", "placement": "body"},
    ])
    storage.post_crawl_sync()
    home = storage.fetch_all_pages()[0]
    redirects = issues_for(home, "Internal Link to Redirect")
    assert {i["severity"] for i in redirects} == {"info"}
    assert len(redirects) == 2

    data = build_report_data(tmp_path / "crawl_results.db")["links_to_redirects"]
    assert [e["target_url"] for e in data["slash_normalisation"]["items"]] == ["https://e.com/x"]
    assert data["other"]["items"] == [{
        "target_url": "https://e.com/services/microneedling/",
        "final_url": "https://e.com/services/micro-needling/",
        "linking_pages": 1,
    }]

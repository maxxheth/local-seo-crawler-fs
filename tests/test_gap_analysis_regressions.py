"""Regression tests covering all 8 root-cause discrepancies identified in
LOCAL-SEO-GAP-ANALYSIS-2026-11-04.md.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import httpx

from src.adapters.competitor import CompetitorSiteAdapter
from src.models import (
    EvidenceManifest,
    PageTechnicalRecord,
)
from src.output import OutputManager
from src.scraper import execute_single_scrape, fetch_with_redirect_inspection
from src.seo_auditor import audit_page, detect_duplicate_pages
from src.sitemap import canonical_url_key, normalize_url
from src.storage import SQLiteStorage


# ---------------------------------------------------------------------------
# GAP 1: Trailing-Slash Truncation & Self-Referential Canonical Mismatches
# ---------------------------------------------------------------------------

def test_gap1_trailing_slash_preservation_and_canonical_matching() -> None:
    """Verify normalize_url respects preserve_trailing_slash and canonical matching ignores trailing slashes."""
    url_with_slash = "https://facetofacespa.com/locations/sugar-land-tx/"
    url_without_slash = "https://facetofacespa.com/locations/sugar-land-tx"

    # Default preserves trailing slash
    assert normalize_url(url_with_slash, preserve_trailing_slash=True) == url_with_slash
    assert normalize_url(url_without_slash, preserve_trailing_slash=True) == url_without_slash

    # Configurable stripping
    assert normalize_url(url_with_slash, preserve_trailing_slash=False) == url_without_slash

    # Canonical equivalence key strips trailing slash for matching
    assert canonical_url_key(url_with_slash) == canonical_url_key(url_without_slash)

    # audit_page should NOT flag self-referential canonical mismatch when only trailing slash differs
    record = PageTechnicalRecord(
        url=url_with_slash,
        canonical_url=url_without_slash,
        status_code=200,
        title="Sugar Land Spa | Face to Face Spa",
        meta_description="Premier spa services in Sugar Land Texas.",
        h1=["Sugar Land Med Spa"],
        viewport="width=device-width, initial-scale=1.0",
    )
    issues = audit_page(record)
    canonical_issues = [i for i in issues if "canonical" in i.issue.lower()]
    assert len(canonical_issues) == 0, f"Unexpected canonical issues: {canonical_issues}"


# ---------------------------------------------------------------------------
# GAP 2: Unfollowed/Masked 301 Redirects
# ---------------------------------------------------------------------------

def test_gap2_unfollowed_redirect_inspection_captures_chain_and_queues_target() -> None:
    """Verify fetch_with_redirect_inspection records initial 301 status, hop chain, and does not conflate target body."""
    mock_client = MagicMock(spec=httpx.Client)

    # Hop 1 response (301 redirect)
    resp_hop1 = MagicMock(spec=httpx.Response)
    resp_hop1.status_code = 301
    resp_hop1.is_redirect = True
    resp_hop1.headers = {
        "location": "https://facetofacefranchising.com/",
        "server": "nginx",
    }
    resp_hop1.url = httpx.URL("https://facetofacespa.com/franchising")

    # Hop 2 response (200 final target)
    resp_hop2 = MagicMock(spec=httpx.Response)
    resp_hop2.status_code = 200
    resp_hop2.is_redirect = False
    resp_hop2.headers = {
        "content-type": "text/html; charset=UTF-8",
        "server": "Apache",
        "cf-cache-status": "HIT",
    }
    resp_hop2.text = "<html><head><title>Franchise Target</title></head><body>Target Content</body></html>"
    resp_hop2.url = httpx.URL("https://facetofacefranchising.com/")

    mock_client.get.side_effect = [resp_hop1, resp_hop2]

    # Direct fetch_with_redirect_inspection test
    last_resp, chain, dest_url, init_s, target_s, trans_err, ttfb = fetch_with_redirect_inspection(
        mock_client, "https://facetofacespa.com/franchising"
    )

    assert init_s == 301
    assert target_s == 200
    assert chain == ["301 -> https://facetofacefranchising.com/"]
    assert dest_url == "https://facetofacefranchising.com/"
    assert trans_err is None

    # Full execute_single_scrape orchestration test
    mock_client.get.side_effect = [resp_hop1, resp_hop2]
    job = {
        "mode": "competitor",
        "url": "https://facetofacespa.com/franchising",
        "domain": "facetofacespa.com",
    }
    scrape_res = execute_single_scrape(job, shared_client=mock_client)
    assert scrape_res["status_code"] == 301
    assert scrape_res["success"] is True

    record = scrape_res["result"]
    assert record["status_code"] == 301
    assert record["initial_status_code"] == 301
    assert record["redirect_target_status"] == 200
    assert record["title"] == ""  # Source title is not contaminated with destination body
    assert record["word_count"] == 0


# ---------------------------------------------------------------------------
# GAP 3: External Certificate Expiry Escalation to 500 & FlareSolverr Storm
# ---------------------------------------------------------------------------

def test_gap3_external_ssl_error_on_redirect_does_not_escalate_to_500() -> None:
    """Verify an external destination with an expired/invalid SSL cert records 301 with transport_error and is_success=True."""
    mock_client = MagicMock(spec=httpx.Client)

    # Hop 1 response (301 redirect to external domain)
    resp_hop1 = MagicMock(spec=httpx.Response)
    resp_hop1.status_code = 301
    resp_hop1.is_redirect = True
    resp_hop1.headers = {"location": "https://facetofacefranchising.com/"}
    resp_hop1.url = httpx.URL("https://facetofacespa.com/franchising")

    # Hop 2 raises SSL / Connect error
    mock_client.get.side_effect = [
        resp_hop1,
        httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired"),
    ]

    last_resp, chain, dest_url, init_s, target_s, trans_err, ttfb = fetch_with_redirect_inspection(
        mock_client, "https://facetofacespa.com/franchising"
    )

    # 1. Recorded as 301 (not 500 fatal)
    assert init_s == 301
    # 2. Transport error captured from hop 2
    assert trans_err is not None
    assert "certificate verify failed" in trans_err.lower()
    # 3. Destination status is None
    assert target_s is None

    # Full execute_single_scrape test: verify it marks success=True (no retry / no FlareSolverr escalation)
    mock_client.get.side_effect = [
        resp_hop1,
        httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
    ]
    job = {
        "mode": "competitor",
        "url": "https://facetofacespa.com/franchising",
        "domain": "facetofacespa.com",
    }
    scrape_res = execute_single_scrape(job, shared_client=mock_client)
    assert scrape_res["status_code"] == 301
    assert scrape_res["success"] is True  # Definitive completion, no retry loop
    rec = scrape_res["result"]
    assert rec["initial_status_code"] == 301
    assert "certificate verify failed" in rec["transport_error"].lower()


# ---------------------------------------------------------------------------
# GAP 4: False Positive 404 Auditing (Missing H1, Meta Description, Viewport)
# ---------------------------------------------------------------------------

def test_gap4_non_200_pages_do_not_emit_content_seo_issues() -> None:
    """Verify audit_page does not emit Missing H1/Meta Description/Viewport for 404, 500, or transport errors."""
    record_404 = PageTechnicalRecord(
        url="https://facetofacespa.com/non-existent-page",
        status_code=404,
        title="",
        meta_description="",
        h1=[],
        viewport="",
    )
    issues_404 = audit_page(record_404)
    content_issues_404 = [
        i for i in issues_404
        if i.issue in ("Missing H1 Tag", "Missing Meta Description", "Missing Viewport Tag", "Thin Content (< 300 words)")
    ]
    assert len(content_issues_404) == 0, f"Expected 0 content issues for 404 page, got: {content_issues_404}"
    assert any(i.issue == "Broken Page (HTTP 404)" for i in issues_404)

    record_transport = PageTechnicalRecord(
        url="https://facetofacespa.com/unreachable",
        status_code=0,
        transport_error="Connection timed out",
        title="",
        meta_description="",
        h1=[],
        viewport="",
    )
    issues_transport = audit_page(record_transport)
    content_issues_transport = [
        i for i in issues_transport
        if i.issue in ("Missing H1 Tag", "Missing Meta Description", "Missing Viewport Tag")
    ]
    assert len(content_issues_transport) == 0, f"Expected 0 content issues for transport error, got: {content_issues_transport}"
    assert any(i.issue == "Transport Error" for i in issues_transport)


# ---------------------------------------------------------------------------
# GAP 5: Protocol-less Bare Domain Outlink Extraction
# ---------------------------------------------------------------------------

def test_gap5_bare_domain_link_extraction() -> None:
    """Scheme-less hrefs that look like domains resolve relative to the document, as browsers do.

    (Supersedes the 2026-11-04 behaviour that rewrote them to https://<href>; see WS1 of
    plans/2026-11-05-crawler-accuracy-and-deterministic-reporting.md.)
    """
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Locations</title></head>
    <body>
      <h1>Our Locations</h1>
      <a href="/locations/austin/">Austin Location</a>
      <a href="facetofacefranchising.com/opportunities">Franchise Opportunities</a>
      <a href="www.externalportal.com">Partner Portal</a>
      <a href="https://www.youtube.com/watch?v=abc">Real external</a>
    </body>
    </html>
    """
    adapter = CompetitorSiteAdapter()
    record, _, discovered_urls, link_edges = adapter.parse(
        url="https://facetofacespa.com/locations/",
        html=html,
        http_status=200,
        final_url="https://facetofacespa.com/locations/",
    )

    # Bare-domain hrefs are relative paths under the document base (internal), not external
    assert record.internal_outlinks_count == 3
    assert record.external_outlinks_count == 1
    # Malformed targets are recorded as edges but never queued (crawl-trap guard)
    assert discovered_urls == ["https://facetofacespa.com/locations/austin/"]

    by_target = {e.target_url: e for e in link_edges}
    normal = by_target["https://facetofacespa.com/locations/austin/"]
    assert normal.malformed_relative is False and normal.intended_host == ""

    franchise = by_target["https://facetofacespa.com/locations/facetofacefranchising.com/opportunities"]
    assert franchise.malformed_relative is True
    assert franchise.intended_host == "facetofacefranchising.com"
    assert franchise.raw_href == "facetofacefranchising.com/opportunities"

    portal = by_target["https://facetofacespa.com/locations/www.externalportal.com"]
    assert portal.malformed_relative is True
    assert portal.intended_host == "www.externalportal.com"


# ---------------------------------------------------------------------------
# GAP 6: Non-Visible Tag Extraction Leaking Phone Numbers
# ---------------------------------------------------------------------------

def test_gap6_script_and_style_tags_do_not_leak_nap_data() -> None:
    """Verify phone numbers and addresses inside script/style/svg tags are excluded from page NAP."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
      <title>Contact Page</title>
      <script>
        var config = {
          headquartersPhone: "512-555-9999",
          corporateAddress: "999 Corporate Blvd, Austin, TX 78701"
        };
      </script>
      <style>
        /* .phone-512-555-8888 */
      </style>
    </head>
    <body>
      <h1>Contact Us</h1>
      <div class="local-branch">
        <a href="tel:7135551234">(713) 555-1234</a>
        <address>123 Main St, Houston, TX 77002</address>
      </div>
    </body>
    </html>
    """
    adapter = CompetitorSiteAdapter()
    record, _, _, _ = adapter.parse(
        url="https://facetofacespa.com/contact/",
        html=html,
        http_status=200,
        final_url="https://facetofacespa.com/contact/",
    )

    # Script phone 512-555-9999 must NOT be in phones
    assert "512-555-9999" not in record.nap_found.phones
    assert "(512) 555-9999" not in record.nap_found.phones

    # Visible / tel phone must be captured and deduplicated (no separate unformatted '7135551234' in phones)
    assert record.nap_found.phones == ["(713) 555-1234"]
    assert "7135551234" in record.nap_found.tel_links

    # Address must match visible address only and NOT include trailing digits '1234' from preceding phone number
    assert record.nap_found.addresses == ["123 Main St, Houston, TX 77002"]
    assert not any("Corporate Blvd" in a for a in record.nap_found.addresses)


# ---------------------------------------------------------------------------
# GAP 7: New SEO Audit Rules & Duplicate Exclusion
# ---------------------------------------------------------------------------

def test_gap7_new_seo_audit_rules() -> None:
    """Noindex-in-sitemap stays a per-page rule; thank-you, prototype and LocalBusiness rules
    are site-context rules keyed on post-crawl page type (WS4/WS5)."""
    from src.seo_auditor import audit_site_context

    # 1. Noindex Page in Sitemap (per-page)
    rec_sitemap_noindex = PageTechnicalRecord(
        url="https://facetofacespa.com/landing-page/",
        status_code=200,
        in_sitemap=True,
        meta_robots="noindex, follow",
        title="Landing",
        meta_description="Desc",
        h1=["H1"],
        viewport="width=device-width",
    )
    issues1 = audit_page(rec_sitemap_noindex)
    assert any(i.issue == "Noindex Page in Sitemap" and i.severity == "error" for i in issues1)

    # Context rules are no longer emitted by the per-page audit
    rec_location_url = PageTechnicalRecord(
        url="https://facetofacespa.com/locations/sugar-land-tx/",
        status_code=200,
        schema_types=["WebPage"],
        title="Sugar Land",
        meta_description="Desc",
        h1=["H1"],
        viewport="width=device-width",
    )
    assert not any(i.issue == "Missing LocalBusiness / Clinic Schema" for i in audit_page(rec_location_url))

    # 2-4. Site-context rules from page type
    pages = [
        {
            "url": "https://facetofacespa.com/appointment-confirmation-thank-you/",
            "status_code": 200,
            "meta_robots": "index, follow",
            "page_type": "utility_page",
        },
        {
            "url": "https://facetofacespa.com/staging-prototype-demo/",
            "status_code": 200,
            "meta_robots": "index",
            "page_type": "other",
        },
        {
            "url": "https://facetofacespa.com/sugar-land/",
            "status_code": 200,
            "meta_robots": "index",
            "schema_types": ["WebPage", "BreadcrumbList"],
            "page_type": "clinic_location",
            "location_status": "open",
            "nap_found": {"addresses": ["123 Main St, Sugar Land, TX 77478"]},
        },
    ]
    ctx = audit_site_context(pages, links=[])
    names = {(u, i.issue, i.severity) for u, iss in ctx.items() for i in iss}
    assert ("https://facetofacespa.com/appointment-confirmation-thank-you/", "Indexable Thank-You / Confirmation Page", "warning") in names
    assert ("https://facetofacespa.com/staging-prototype-demo/", "Indexable Prototype / Staging Page", "warning") in names
    assert ("https://facetofacespa.com/sugar-land/", "Missing LocalBusiness / Clinic Schema", "warning") in names

    # 5. Duplicate page detection excludes redirects and 404s
    page_redirect = {
        "url": "https://facetofacespa.com/franchising",
        "status_code": 301,
        "title": "Same Title",
        "h1": ["Same H1"],
        "body_tokens_sample": ["face", "spa", "treatment"],
    }
    page_live = {
        "url": "https://facetofacespa.com/franchise-target",
        "status_code": 200,
        "title": "Same Title",
        "h1": ["Same H1"],
        "body_tokens_sample": ["face", "spa", "treatment"],
    }
    dupes = detect_duplicate_pages([page_redirect, page_live])
    assert len(dupes) == 0, "Redirects must be excluded from duplicate content detection"


# ---------------------------------------------------------------------------
# GAP 8: Unvisited Link Target Verification & Evidence Persistence
# ---------------------------------------------------------------------------

def test_gap8_link_target_sync_and_evidence_manifest_persistence(tmp_path: Path) -> None:
    """Verify SQLite link target tracking/syncing and evidence manifest persistence on re-runs."""
    # 1. Storage link target tracking
    db_path = tmp_path / "test_gap8.db"
    storage = SQLiteStorage(db_path)

    # Save page with an unvisited target link
    storage.save_page({"url": "https://facetofacespa.com/services/", "status_code": 200}, domain="facetofacespa.com")
    storage.save_internal_links_batch([
        {
            "source_url": "https://facetofacespa.com/services/",
            "target_url": "https://facetofacespa.com/unvisited-pdf-or-page",
            "anchor_text": "Download Guide",
            "placement": "body",
            "is_nofollow": False,
        }
    ])

    unvisited = storage.get_unvisited_link_targets()
    assert "https://facetofacespa.com/unvisited-pdf-or-page" in unvisited

    # Update verified status
    storage.update_link_target_statuses({"https://facetofacespa.com/unvisited-pdf-or-page": 200})
    unvisited_after = storage.get_unvisited_link_targets()
    assert len(unvisited_after) == 0

    # 2. OutputManager Evidence Manifest Persistence
    out_dir = tmp_path / "crawl_out"
    out_dir.mkdir(parents=True)
    evidence_file = out_dir / "evidence_manifest.json"

    # Pre-populate evidence manifest (from prior run)
    initial_manifest = {
        "manifest_id": "evd-prior-001",
        "records": [
            {
                "claim_type": "technical_page_audit",
                "source_url": "https://facetofacespa.com/",
                "timestamp": "2026-11-04T12:00:00Z",
                "http_status": 200,
                "dom_selector": "title",
                "verbatim_text": "Face to Face Spa",
                "hash_sha256": "abcdef123456",
            }
        ],
    }
    evidence_file.write_text(EvidenceManifest(**initial_manifest).model_dump_json(indent=2))

    mgr = OutputManager(output_dir=str(out_dir))
    # Run export with empty evidence list (simulating incremental or partial run)
    mgr.export_site_crawl(
        target_domain="facetofacespa.com",
        pages=[{"url": "https://facetofacespa.com/", "status_code": 200}],
        evidence_records=[],
    )

    # Verify existing evidence was NOT obliterated into 0 records
    loaded_manifest = EvidenceManifest.model_validate_json(evidence_file.read_text())
    assert len(loaded_manifest.records) == 1
    assert loaded_manifest.records[0].verbatim_text == "Face to Face Spa"


def test_code_review_fixes_backfill_and_summary_rollup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify code review fixes:
    1. get_unvisited_link_targets() backfills known page statuses first (does not re-verify crawled pages).
    2. HTTP 500 issues use severity='error' and are counted in SiteAuditSummary.errors_count.
    3. Non-200 pages (e.g. 404/301) with 0 inlinks are not counted as orphan pages.
    4. load_targets propagates in_sitemap=True and preserve_trailing_slash to seeded targets.
    """
    import src.scraper as scraper
    from src.seo_auditor import generate_site_summary

    # 1. get_unvisited_link_targets backfills already-crawled pages
    db_path = tmp_path / "test_review.db"
    storage = SQLiteStorage(db_path)
    storage.save_page({"url": "https://facetofacespa.com/", "status_code": 200}, domain="facetofacespa.com")
    storage.save_page({"url": "https://facetofacespa.com/about/", "status_code": 200}, domain="facetofacespa.com")
    storage.save_internal_links_batch([
        {
            "source_url": "https://facetofacespa.com/",
            "target_url": "https://facetofacespa.com/about",  # trailing-slash variant of crawled page
            "anchor_text": "About Us",
            "placement": "navigation",
            "is_nofollow": False,
        },
        {
            "source_url": "https://facetofacespa.com/",
            "target_url": "https://facetofacespa.com/brochure.pdf",  # genuinely unvisited
            "anchor_text": "Brochure",
            "placement": "body",
            "is_nofollow": False,
        },
    ])

    unvisited = storage.get_unvisited_link_targets()
    assert unvisited == ["https://facetofacespa.com/brochure.pdf"]

    # 2 & 3. HTTP 500 severity rollup and non-200 orphan exclusion
    p_root = PageTechnicalRecord(url="https://facetofacespa.com/", status_code=200, inlinks_count=0)
    p_500 = PageTechnicalRecord(url="https://facetofacespa.com/broken-500", status_code=500, inlinks_count=0)
    p_500.issues = audit_page(p_500)
    p_404 = PageTechnicalRecord(url="https://facetofacespa.com/missing-404", status_code=404, inlinks_count=0)
    p_404.issues = audit_page(p_404)

    summary = generate_site_summary([p_root, p_500, p_404])
    assert summary.errors_count == 2  # Both 500 and 404 counted in errors_count
    assert summary.total_issues == 2
    assert summary.orphan_pages_count == 0  # Neither 500 nor 404 is counted as an orphan page

    # 4. load_targets sets in_sitemap and respects preserve_trailing_slash=False
    monkeypatch.setattr(
        "src.scraper.SitemapDiscovery.discover_urls",
        lambda self: ["https://facetofacespa.com/services/hydrafacial"],
    )
    targets = scraper.load_targets(
        mode="competitor",
        input_path="",
        domain="facetofacespa.com",
        query="",
        location="",
        business_name="",
        use_sitemap=True,
        max_pages=50,
        preserve_trailing_slash=False,
    )
    by_url = {t["url"]: t for t in targets}
    assert "https://facetofacespa.com/services/hydrafacial" in by_url
    assert by_url["https://facetofacespa.com/services/hydrafacial"]["in_sitemap"] is True
    assert by_url["https://facetofacespa.com/services/hydrafacial"]["preserve_trailing_slash"] is False
    assert by_url["https://facetofacespa.com/"]["in_sitemap"] is False


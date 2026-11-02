"""Comprehensive unit tests for scrape adapters, schema parity, and output artifacts."""

import json
from pathlib import Path
from src.adapters.citation import CitationAdapter, normalize_phone, compute_sha256
from src.adapters.competitor import CompetitorSiteAdapter
from src.adapters.serp import LocalSerpAdapter, parse_review_count
from src.models import (
    CitationsManifest,
    EvidenceManifest,
    SiteCrawlArtifact,
)
from src.output import OutputManager


SAMPLE_CITATION_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Dallas Premier Plumber - Yelp</title>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "PlumbingService",
    "name": "Dallas Premier Plumber",
    "telephone": "+12145550199",
    "url": "https://dallaspremierplumber.com",
    "openingHours": ["Mo-Fr 08:00-18:00", "Sa 09:00-14:00"],
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "123 Main St",
      "addressLocality": "Dallas",
      "addressRegion": "TX",
      "postalCode": "75001"
    }
  }
  </script>
</head>
<body>
  <h1>Dallas Premier Plumber</h1>
  <div class="contact">
    <a href="tel:2145550199">(214) 555-0199</a>
    <a href="tel:2145550200">(214) 555-0200</a>
    <address>123 Main St, Dallas, TX 75001</address>
  </div>
  <div class="badges">BBB Accredited A+ | Verified License</div>
</body>
</html>
"""

SAMPLE_BLOCKED_HTML = """
<html>
<head><title>Attention Required! | Cloudflare</title></head>
<body>
  <h1>Please complete the security check to access Yelp</h1>
  <div id="cf-browser-verification">Just a moment...</div>
</body>
</html>
"""

SAMPLE_COMPETITOR_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Dallas Rooter & Drain Cleaning | 24/7 Service</title>
  <meta name="description" content="Fast Dallas drain cleaning and rooter plumbing." />
  <link rel="canonical" href="https://competitor.com/drain-cleaning-dallas/" />
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "LocalBusiness",
    "name": "Dallas Rooter",
    "telephone": "214-555-0199"
  }
  </script>
</head>
<body>
  <h1>Emergency Drain Cleaning in Dallas, TX</h1>
  <h2>Clogged Drain Symptoms</h2>
  <h3>Slow Draining Sinks</h3>
  <h3>Gurgling Toilets</h3>
  <p>We provide full service hydro-jetting and camera inspections for residential drains.</p>
  <h2>Our Hydro-Jetting Process</h2>
  <p>Learn more about our advanced plumbing solutions and pipeline rehabilitation.</p>
  <a href="/service-areas/">Service Areas</a>
  <a href="/pricing/">Pricing</a>
  <a href="https://externalsupplier.com/pipes">Supplier</a>
  <footer>
    <address>Dallas, TX</address>
    <a href="tel:2145550199">(214) 555-0199</a>
  </footer>
</body>
</html>
"""

SAMPLE_SERP_HTML = """
<!DOCTYPE html>
<html>
<head><title>drain cleaning dallas - Google Search</title></head>
<body>
  <div id="search">
    <!-- Local Pack -->
    <div class="VkpGBb" data-attrid="kc:/local:pack">
      <div role="heading">Dallas Top Drain Pros</div>
      <span class="yi40Hd">4.9</span>
      <span class="RDApEe">(1.1K reviews)</span>
      <div class="rllt__details">Plumber · Dallas, TX · (214) 555-0111</div>
    </div>
    <div class="VkpGBb" data-attrid="kc:/local:pack">
      <div role="heading">Lone Star Rooter</div>
      <span class="yi40Hd">4.8</span>
      <span class="RDApEe">(85)</span>
      <div class="rllt__details">Plumber · Dallas, TX · (214) 555-0222</div>
    </div>

    <!-- Organic Rankings -->
    <div class="g">
      <a href="https://competitor.com/drain-cleaning-dallas/">
        <h3>Drain Cleaning Dallas | 24/7 Rooter Service</h3>
      </a>
      <div class="VwiC3b">Fast Dallas drain cleaning and rooter plumbing solutions.</div>
    </div>
    <div class="g">
      <a href="https://secondcompetitor.com/services">
        <h3>Dallas Plumbing & Drain Specialists</h3>
      </a>
      <div class="VwiC3b">Trusted local plumbing experts servicing Dallas-Fort Worth.</div>
    </div>

    <!-- PAA -->
    <div class="related-question-pair">
      <div role="button">How much does drain cleaning cost in Dallas?</div>
      <div class="L39u4d">Typical professional drain cleaning costs between $150 and $450 in Dallas.</div>
    </div>
  </div>
</body>
</html>
"""


def test_phone_normalization():
    assert normalize_phone("+1 (214) 555-0199") == "(214) 555-0199"
    assert normalize_phone("2145550199") == "(214) 555-0199"
    assert normalize_phone("214.555.0199") == "(214) 555-0199"


def test_review_count_parsing():
    assert parse_review_count("(1.1K reviews)") == 1100
    assert parse_review_count("4.9(1.1K)") == 1100
    assert parse_review_count("(85)") == 85
    assert parse_review_count("(379 reviews)") == 379


def test_citation_adapter_live():
    adapter = CitationAdapter(platform="yelp")
    item, evidence = adapter.parse(
        url="https://www.yelp.com/biz/dallas-premier-plumber",
        html=SAMPLE_CITATION_HTML,
        http_status=200,
        expected_business="Dallas Premier Plumber",
        expected_address="123 Main St, Dallas, TX 75001",
        expected_phone="(214) 555-0199",
        proxy_port=10002,
    )

    assert item.status == "live"
    assert item.nap_match == "match"
    assert item.observed is not None
    assert item.observed.name == "Dallas Premier Plumber"
    assert "(214) 555-0199" in item.observed.phone
    assert "123 Main St" in item.observed.address
    assert item.proxy_port == 10002
    assert item.confidence == "high"

    # New fields verification
    assert item.website_url == "https://dallaspremierplumber.com"
    assert "Mo-Fr" in item.operating_hours
    assert "(214) 555-0200" in item.secondary_phones
    assert any("BBB Accredited" in b for b in item.badges)

    # Evidence test
    assert evidence.claim_type == "citation_verification"
    assert evidence.http_status == 200
    assert evidence.hash_sha256 == compute_sha256(item.evidence_snippet)


def test_citation_adapter_blocked():
    adapter = CitationAdapter(platform="yelp")
    item, evidence = adapter.parse(
        url="https://www.yelp.com/biz/test",
        html=SAMPLE_BLOCKED_HTML,
        http_status=403,
    )

    assert item.status in ("captcha", "blocked")
    assert item.nap_match == "unknown"
    assert item.observed is None
    assert evidence.http_status == 403


def test_competitor_site_adapter():
    adapter = CompetitorSiteAdapter()
    record, evidence_list = adapter.parse(
        url="https://competitor.com/drain-cleaning-dallas/",
        html=SAMPLE_COMPETITOR_HTML,
        http_status=200,
        final_url="https://competitor.com/drain-cleaning-dallas/",
    )

    assert record.status_code == 200
    assert record.title == "Dallas Rooter & Drain Cleaning | 24/7 Service"
    assert "Fast Dallas drain cleaning" in record.meta_description
    assert record.canonical_url == "https://competitor.com/drain-cleaning-dallas/"
    assert record.h1 == ["Emergency Drain Cleaning in Dallas, TX"]
    assert record.h2 == ["Clogged Drain Symptoms", "Our Hydro-Jetting Process"]
    assert record.h3 == ["Slow Draining Sinks", "Gurgling Toilets"]
    assert record.word_count > 20
    assert record.internal_outlinks_count == 2
    assert record.external_outlinks_count == 1
    assert any("service-areas" in u for u in record.internal_urls)
    assert "LocalBusiness" in record.schema_types
    assert len(record.nap_found.phones) > 0
    assert len(evidence_list) == 1
    assert evidence_list[0].claim_type == "technical_page_audit"


def test_local_serp_adapter():
    adapter = LocalSerpAdapter()
    artifact, evidence_list = adapter.parse(
        query="drain cleaning dallas",
        location="Dallas, TX",
        html=SAMPLE_SERP_HTML,
        http_status=200,
        source_url="https://www.google.com/search?q=drain+cleaning+dallas",
    )

    assert artifact.query == "drain cleaning dallas"
    assert artifact.location == "Dallas, TX"
    assert len(artifact.local_pack) == 2
    assert artifact.local_pack[0].name == "Dallas Top Drain Pros"
    assert artifact.local_pack[0].rating == 4.9
    assert artifact.local_pack[0].review_count == 1100

    assert len(artifact.organic) == 2
    assert artifact.organic[0].title == "Drain Cleaning Dallas | 24/7 Rooter Service"
    assert "competitor.com" in artifact.organic[0].url

    assert len(artifact.paa) == 1
    assert "How much does drain cleaning cost" in artifact.paa[0].question
    assert "150" in artifact.paa[0].snippet


def test_output_artifacts_strict_schema(tmp_path: Path):
    mgr = OutputManager(output_dir=str(tmp_path))

    # Test Citation Export
    citation_data = [
        {
            "platform": "yelp",
            "label": "Yelp",
            "url": "https://www.yelp.com/biz/example",
            "status": "live",
            "nap_match": "match",
            "observed": {
                "name": "Target Business Name",
                "address": "123 Main St, Dallas, TX 75001",
                "phone": "(214) 555-0199",
            },
            "confidence": "high",
            "evidence_snippet": "Target Business Name - 123 Main St - (214) 555-0199",
            "proxy_port": 10002,
            "secondary_phones": ["(214) 555-0200"],
            "website_url": "https://example.com",
            "operating_hours": "Mo-Fr 08:00-18:00",
            "badges": ["BBB Accredited"],
        }
    ]
    evidence_data = [
        {
            "claim_type": "citation_verification",
            "source_url": "https://www.yelp.com/biz/example",
            "timestamp": "2026-10-27T14:30:15Z",
            "http_status": 200,
            "dom_selector": "h1",
            "verbatim_text": "Target Business Name",
            "hash_sha256": "abc123hash",
        }
    ]

    exported = mgr.export_citations(
        business_name="Target Business Name",
        citations=citation_data,
        evidence_records=evidence_data,
    )

    citations_file = Path(exported["citations_manifest"])
    evidence_file = Path(exported["evidence_manifest"])
    assert citations_file.exists()
    assert evidence_file.exists()

    with open(citations_file) as f:
        c_manifest = CitationsManifest.model_validate_json(f.read())
        assert c_manifest.business == "Target Business Name"
        assert c_manifest.summary.total == 1
        assert c_manifest.summary.live == 1
        assert c_manifest.summary.mismatch == 0
        assert len(c_manifest.citations) == 1
        assert c_manifest.citations[0].platform == "yelp"
        assert c_manifest.citations[0].secondary_phones == ["(214) 555-0200"]

    with open(evidence_file) as f:
        e_manifest = EvidenceManifest.model_validate_json(f.read())
        assert len(e_manifest.records) == 1
        assert e_manifest.records[0].claim_type == "citation_verification"
        assert e_manifest.records[0].hash_sha256 == "abc123hash"

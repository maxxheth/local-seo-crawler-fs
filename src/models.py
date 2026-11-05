"""Pydantic data models enforcing strict schema compatibility."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class ObservedNAP(BaseModel):
    name: str = ""
    address: str = ""
    phone: str = ""


class CitationItem(BaseModel):
    platform: str
    label: str
    url: str
    status: str  # "live" | "blocked" | "not_found" | "captcha" | "error"
    nap_match: str = "unknown"  # "match" | "mismatch" | "partial" | "unknown"
    observed: ObservedNAP | None = None
    confidence: str = "high"  # "high" | "medium" | "low"
    evidence_snippet: str = ""
    proxy_port: int | None = None
    secondary_phones: list[str] = Field(default_factory=list)
    website_url: str = ""
    operating_hours: str = ""
    badges: list[str] = Field(default_factory=list)


class CitationSummary(BaseModel):
    total: int = 0
    live: int = 0
    blocked: int = 0
    mismatch: int = 0


class CitationsManifest(BaseModel):
    business: str
    checked_at: str
    summary: CitationSummary
    citations: list[CitationItem] = Field(default_factory=list)


class FoundNAP(BaseModel):
    phones: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    tel_links: list[str] = Field(default_factory=list)
    map_links: list[str] = Field(default_factory=list)
    map_embed_coords: list[str] = Field(default_factory=list)


class ImageAuditSummary(BaseModel):
    total_images: int = 0
    missing_alt_count: int = 0
    decorative_alt_count: int = 0
    missing_dimensions_count: int = 0
    missing_alt_sample_urls: list[str] = Field(default_factory=list)
    webp_count: int = 0
    avif_count: int = 0
    jpg_count: int = 0
    png_count: int = 0
    svg_count: int = 0


class AnalyticsAndTracking(BaseModel):
    # General Analytics
    google_analytics: bool = False
    gtag: bool = False
    ga4_id: str = ""
    gtm_id: str = ""
    facebook_pixel: bool = False
    hotjar: bool = False
    mixpanel: bool = False
    microsoft_clarity: bool = False
    # Local SEO Call-Tracking & Field Service / Review Widgets
    callrail: bool = False
    whatconverts: bool = False
    servicetitan: bool = False
    housecall_pro: bool = False
    podium: bool = False
    birdeye: bool = False


class InternalLinkEdge(BaseModel):
    source_url: str
    target_url: str
    anchor_text: str = ""
    placement: str = "body"  # "navigation" | "header" | "footer" | "body" | "image"
    is_nofollow: bool = False
    target_status: int | None = None
    # WS1: scheme-less hrefs that look like hostnames (e.g. href="www.youtube.com/x")
    # resolve relative to the document base, exactly as browsers do. They are kept
    # as internal edges but annotated so they can be reported as malformed.
    malformed_relative: bool = False
    intended_host: str = ""
    raw_href: str = ""
    # WS2: structured transport failure of the target (tls_hostname_mismatch, dns_failure, ...)
    target_error_type: str = ""


class SEOIssue(BaseModel):
    url: str
    severity: str  # "error" | "warning" | "info"
    category: str  # "SEO" | "Content" | "Technical" | "Indexability" | "Mobile" | "Accessibility" | "Social" | "Structured Data" | "Performance" | "Duplication" | "Links"
    issue: str
    details: str


class PageTechnicalRecord(BaseModel):
    url: str
    final_url: str = ""
    status_code: int = 200
    initial_status_code: int = 200
    redirect_chain: list[str] = Field(default_factory=list)
    redirect_target_status: int | None = None
    content_type: str = ""
    transport_error: str = ""
    transport_error_type: str = ""
    fetch_method: str = "fast_http"
    ttfb_ms: int = 0
    server_header: str = ""
    cache_status: str = ""
    x_robots_tag: str = ""
    in_sitemap: bool = False
    title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    h1: list[str] = Field(default_factory=list)
    h2: list[str] = Field(default_factory=list)
    h3: list[str] = Field(default_factory=list)
    word_count: int = 0
    first_50_words: str = ""
    body_tokens_sample: list[str] = Field(default_factory=list)
    internal_outlinks_count: int = 0
    external_outlinks_count: int = 0
    internal_urls: list[str] = Field(default_factory=list)
    schema_types: list[str] = Field(default_factory=list)
    schema_jsonld: list[dict[str, Any]] = Field(default_factory=list)
    nap_found: FoundNAP = Field(default_factory=FoundNAP)
    is_ssr: bool = True

    # General & Local SEO Extensions
    lang: str = ""
    charset: str = ""
    viewport: str = ""
    meta_robots: str = ""
    generator: str = ""
    hreflang: list[dict[str, str]] = Field(default_factory=list)
    og_tags: dict[str, str] = Field(default_factory=dict)
    twitter_tags: dict[str, str] = Field(default_factory=dict)
    response_time_ms: int = 0
    page_size_bytes: int = 0

    # Sub-Audits
    images: ImageAuditSummary = Field(default_factory=ImageAuditSummary)
    analytics: AnalyticsAndTracking = Field(default_factory=AnalyticsAndTracking)

    # Link Graph Rollups
    inlinks_count: int = 0
    nofollow_outlinks_count: int = 0
    link_placements: dict[str, int] = Field(default_factory=dict)
    broken_outlinks: list[str] = Field(default_factory=list)
    # [{"target_url", "target_status", "error_type"}] for each entry in broken_outlinks
    broken_outlink_details: list[dict[str, Any]] = Field(default_factory=list)
    external_link_hosts: list[str] = Field(default_factory=list)

    # Opening section text (hero / first content block) used for status notices
    opening_text: str = ""

    # WS4: post-crawl classification
    page_type: str = ""
    page_type_confidence: float = 0.0
    page_type_reasons: list[str] = Field(default_factory=list)
    location_status: str = ""
    moved_to: str = ""
    relocated: bool = False
    status_evidence: dict[str, str] = Field(default_factory=dict)

    # Automated SEO Issues
    issues: list[SEOIssue] = Field(default_factory=list)


class SiteAuditSummary(BaseModel):
    total_pages: int = 0
    total_issues: int = 0
    errors_count: int = 0
    warnings_count: int = 0
    info_count: int = 0
    duplicate_pages_count: int = 0
    broken_internal_links_count: int = 0
    orphan_pages_count: int = 0
    avg_response_time_ms: float = 0.0
    median_response_time_ms: float = 0.0
    p95_response_time_ms: float = 0.0
    cms_generators_detected: list[str] = Field(default_factory=list)
    analytics_detected: list[str] = Field(default_factory=list)
    server_stack_detected: list[str] = Field(default_factory=list)
    caching_detected: list[str] = Field(default_factory=list)
    issues_by_category: dict[str, int] = Field(default_factory=dict)
    broken_targets_by_error_type: dict[str, int] = Field(default_factory=dict)
    timing_source: str = "crawler"
    page_type_counts: dict[str, int] = Field(default_factory=dict)
    location_status_counts: dict[str, int] = Field(default_factory=dict)


class SiteCrawlArtifact(BaseModel):
    target_domain: str
    crawl_depth: int = 1
    pages: list[PageTechnicalRecord] = Field(default_factory=list)
    audit_summary: SiteAuditSummary = Field(default_factory=SiteAuditSummary)


class EvidenceRecord(BaseModel):
    claim_type: str
    source_url: str
    timestamp: str
    http_status: int
    dom_selector: str
    verbatim_text: str
    hash_sha256: str


class EvidenceManifest(BaseModel):
    manifest_id: str
    records: list[EvidenceRecord] = Field(default_factory=list)


class LocalPackItem(BaseModel):
    position: int
    name: str
    rating: float | None = None
    review_count: int | None = None
    address_snippet: str = ""
    categories: list[str] = Field(default_factory=list)


class OrganicRankingItem(BaseModel):
    position: int
    url: str
    title: str
    snippet: str


class PAAItem(BaseModel):
    question: str
    snippet: str


class SerpCrawlArtifact(BaseModel):
    query: str
    location: str
    checked_at: str
    local_pack: list[LocalPackItem] = Field(default_factory=list)
    organic: list[OrganicRankingItem] = Field(default_factory=list)
    paa: list[PAAItem] = Field(default_factory=list)


class ScrapeTarget(BaseModel):
    url: str
    mode: str = "citation"  # "citation" | "competitor" | "serp"
    platform: str | None = None
    label: str | None = None
    business_name: str | None = None
    expected_address: str | None = None
    expected_phone: str | None = None
    query: str | None = None
    location: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

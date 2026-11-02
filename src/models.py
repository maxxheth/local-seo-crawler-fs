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


class PageTechnicalRecord(BaseModel):
    url: str
    final_url: str = ""
    status_code: int = 200
    title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    h1: list[str] = Field(default_factory=list)
    h2: list[str] = Field(default_factory=list)
    h3: list[str] = Field(default_factory=list)
    word_count: int = 0
    internal_outlinks_count: int = 0
    external_outlinks_count: int = 0
    internal_urls: list[str] = Field(default_factory=list)
    schema_types: list[str] = Field(default_factory=list)
    schema_jsonld: list[dict[str, Any]] = Field(default_factory=list)
    nap_found: FoundNAP = Field(default_factory=FoundNAP)
    is_ssr: bool = True


class SiteCrawlArtifact(BaseModel):
    target_domain: str
    crawl_depth: int = 1
    pages: list[PageTechnicalRecord] = Field(default_factory=list)


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

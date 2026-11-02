"""SEO rule engine, automated issue detection, and duplication analyzer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
import re
import time
from typing import Any

from src.models import PageTechnicalRecord, SEOIssue, SiteAuditSummary
from src.sitemap import normalize_url

_TOKEN_SPLIT = re.compile(r"\W+")
DUPLICATION_TIME_BUDGET_SECONDS = 10.0


@dataclass
class SEOAuditConfig:
    """Configurable thresholds for SEO rule validation and duplication detection."""

    title_min_len: int = 30
    title_max_len: int = 60
    desc_min_len: int = 120
    desc_max_len: int = 160
    min_word_count: int = 300
    dup_threshold: float = 0.85
    slow_warn_ms: int = 1000
    slow_err_ms: int = 3000
    large_warn_bytes: int = 1048576  # 1 MB
    large_err_bytes: int = 3145728   # 3 MB


def audit_page(record: PageTechnicalRecord, cfg: SEOAuditConfig) -> list[SEOIssue]:
    """Execute per-page SEO defect checks against configured thresholds."""
    issues: list[SEOIssue] = []
    url = record.url

    # 1. HTTP Status & Technical Transport
    status = record.status_code
    if status >= 500:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Technical",
                issue=f"HTTP {status} Server Error",
                details=f"Page returned server error status code {status}",
            )
        )
    elif status >= 400:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Technical",
                issue=f"HTTP {status} Client Error",
                details=f"Page returned client error status code {status}",
            )
        )
    elif status >= 300:
        issues.append(
            SEOIssue(
                url=url,
                severity="info",
                category="Technical",
                issue=f"HTTP {status} Redirect",
                details=f"Page redirected to {record.final_url or 'another location'}",
            )
        )

    # 2. Title Checks
    title = (record.title or "").strip()
    if not title:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="SEO",
                issue="Missing Page Title",
                details="Page has no <title> tag or title is empty",
            )
        )
    elif len(title) < cfg.title_min_len:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="SEO",
                issue="Title Too Short",
                details=f"Title is {len(title)} characters (recommended: {cfg.title_min_len}-{cfg.title_max_len})",
            )
        )
    elif len(title) > cfg.title_max_len:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="SEO",
                issue="Title Too Long",
                details=f"Title is {len(title)} characters (recommended: {cfg.title_min_len}-{cfg.title_max_len})",
            )
        )

    # 3. Meta Description Checks
    desc = (record.meta_description or "").strip()
    if not desc:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="SEO",
                issue="Missing Meta Description",
                details="Page has no meta description tag",
            )
        )
    elif len(desc) < cfg.desc_min_len:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="SEO",
                issue="Meta Description Too Short",
                details=f"Description is {len(desc)} characters (recommended: {cfg.desc_min_len}-{cfg.desc_max_len})",
            )
        )
    elif len(desc) > cfg.desc_max_len:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="SEO",
                issue="Meta Description Too Long",
                details=f"Description is {len(desc)} characters (recommended: {cfg.desc_min_len}-{cfg.desc_max_len})",
            )
        )

    # 4. Heading Hierarchy (H1)
    h1s = record.h1 or []
    if not h1s:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="SEO",
                issue="Missing H1 Tag",
                details="Page has no H1 heading tag",
            )
        )
    elif len(h1s) > 1:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="SEO",
                issue="Multiple H1 Tags",
                details=f"Page has {len(h1s)} H1 tags (recommended: exactly 1)",
            )
        )

    # 5. Content Length & Thin Content
    words = record.word_count
    if words < cfg.min_word_count and status == 200:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Content",
                issue="Thin Content",
                details=f"Page has only {words} words (recommended: >={cfg.min_word_count})",
            )
        )

    # 6. Indexability & Directives
    robots = (record.meta_robots or "").lower()
    if "noindex" in robots:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Indexability",
                issue="Noindex Tag Present",
                details="Page has meta robots 'noindex' directive blocking search engines",
            )
        )
    if "nofollow" in robots:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Indexability",
                issue="Nofollow Tag Present",
                details="Page has meta robots 'nofollow' directive preventing link graph crawl",
            )
        )

    # Canonical URL
    canon = (record.canonical_url or "").strip()
    effective_url = record.final_url or url
    if not canon:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Technical",
                issue="Missing Canonical Tag",
                details="Page does not declare a canonical URL",
            )
        )
    else:
        norm_canon = normalize_url(canon, base_url=effective_url) or canon.rstrip("/")
        norm_page = normalize_url(effective_url) or effective_url.rstrip("/")
        if norm_canon != norm_page:
            issues.append(
                SEOIssue(
                    url=url,
                    severity="info",
                    category="Technical",
                    issue="Canonical Points to Different URL",
                    details=f"Canonical tag specifies: {canon}",
                )
            )

    # SSR validation
    if not record.is_ssr and status == 200:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Technical",
                issue="Client-Side Rendered (CSR Only)",
                details="Initial HTML body contained less than 15% of the rendered page content",
            )
        )

    # 7. Mobile & Accessibility
    if not record.viewport:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Mobile",
                issue="Missing Viewport Meta Tag",
                details="Page lacks a viewport meta tag for mobile responsiveness",
            )
        )
    if not record.lang:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Accessibility",
                issue="Missing Language Attribute",
                details="HTML root tag has no lang attribute",
            )
        )

    # Image Alt Audit
    img_audit = record.images
    if img_audit.missing_alt_count > 0:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Accessibility",
                issue="Images Missing Alt Text",
                details=f"{img_audit.missing_alt_count} of {img_audit.total_images} images lack an alt attribute",
            )
        )

    # 8. Social Sharing (OG & Twitter)
    if not record.og_tags:
        issues.append(
            SEOIssue(
                url=url,
                severity="info",
                category="Social",
                issue="Missing OpenGraph Tags",
                details="Page has no og:title or og:image tags for social sharing",
            )
        )
    if not record.twitter_tags:
        issues.append(
            SEOIssue(
                url=url,
                severity="info",
                category="Social",
                issue="Missing Twitter Card Tags",
                details="Page has no twitter:card tags for Twitter previews",
            )
        )

    # 9. Structured Data
    if not record.schema_types and not record.schema_jsonld:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Structured Data",
                issue="Missing Structured Data",
                details="Page has no Schema.org JSON-LD or Microdata markup",
            )
        )

    # 10. Performance (Response Time & Size)
    resp_time = record.response_time_ms
    if resp_time > cfg.slow_err_ms:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Performance",
                issue="Severe Response Time Delay",
                details=f"Page response time was {resp_time}ms (exceeds {cfg.slow_err_ms}ms limit)",
            )
        )
    elif resp_time > cfg.slow_warn_ms:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Performance",
                issue="Slow Response Time",
                details=f"Page response time was {resp_time}ms (recommended: <{cfg.slow_warn_ms}ms)",
            )
        )

    size = record.page_size_bytes
    if size > cfg.large_err_bytes:
        mb = size / (1024 * 1024)
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Performance",
                issue="Oversized HTML Payload",
                details=f"Page HTML payload is {mb:.2f}MB (exceeds {cfg.large_err_bytes // (1024*1024)}MB limit)",
            )
        )
    elif size > cfg.large_warn_bytes:
        mb = size / (1024 * 1024)
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Performance",
                issue="Large HTML Payload",
                details=f"Page HTML payload is {mb:.2f}MB (recommended: <{cfg.large_warn_bytes // (1024*1024)}MB)",
            )
        )

    return issues


# ── Near-Duplicate Content Detection (Token-Dice with Prefix Filtering) ───────

_DUP_WEIGHTS = {
    "title": 0.35,
    "desc": 0.35,
    "h1": 0.20,
    "word_count": 0.10,
}


def _normalise_text(value: Any) -> str:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), "")
    if not isinstance(value, str):
        return ""
    return " ".join(value.lower().split())


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_SPLIT.split(text) if t)


def _dice(tokens_a: frozenset[str], tokens_b: frozenset[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    return 2.0 * len(tokens_a & tokens_b) / (len(tokens_a) + len(tokens_b))


def _length_similarity(count_a: int, count_b: int) -> float:
    if not count_a or not count_b:
        return 0.0
    return min(count_a, count_b) / max(count_a, count_b)


def _duplication_similarity(a: dict[str, Any], b: dict[str, Any], threshold: float) -> float | None:
    weights = _DUP_WEIGHTS
    score = _dice(a["title_tokens"], b["title_tokens"]) * weights["title"]
    if score + weights["desc"] + weights["h1"] + weights["word_count"] < threshold:
        return None
    score += _dice(a["desc_tokens"], b["desc_tokens"]) * weights["desc"]
    if score + weights["h1"] + weights["word_count"] < threshold:
        return None
    score += _dice(a["h1_tokens"], b["h1_tokens"]) * weights["h1"]
    if score + weights["word_count"] < threshold:
        return None
    score += _length_similarity(a["word_count"], b["word_count"]) * weights["word_count"]
    return score if score >= threshold else None


def detect_duplicate_pages(
    pages: list[PageTechnicalRecord],
    threshold: float = 0.85,
    time_budget: float = DUPLICATION_TIME_BUDGET_SECONDS,
) -> dict[str, SEOIssue]:
    """Detect near-duplicate or doorway pages across crawled records using Dice token similarity.

    Returns mapping of url -> SEOIssue.
    """
    candidates = []
    for p in pages:
        if p.status_code != 200:
            continue
        title = _normalise_text(p.title)
        desc = _normalise_text(p.meta_description)
        h1 = _normalise_text(p.h1)
        if not title and not desc:
            continue
        candidates.append({
            "url": p.url,
            "title": title,
            "desc": desc,
            "h1": h1,
            "title_tokens": _tokens(title),
            "desc_tokens": _tokens(desc),
            "h1_tokens": _tokens(h1),
            "word_count": p.word_count,
        })

    if len(candidates) < 2:
        return {}

    # 1. Group identical title + desc + h1 signatures
    group_index: dict[tuple[str, str, str], int] = {}
    groups: list[list[dict[str, Any]]] = []
    for c in candidates:
        sig = (c["title"], c["desc"], c["h1"])
        if sig not in group_index:
            group_index[sig] = len(groups)
            groups.append([])
        groups[group_index[sig]].append(c)

    representatives = [g[0] for g in groups]

    # 2. Candidate pair generation via rarity prefix filter
    weights = _DUP_WEIGHTS
    min_title = (threshold - (1.0 - weights["title"])) / weights["title"]
    freq: dict[str, int] = defaultdict(int)
    for rep in representatives:
        for t in rep["title_tokens"]:
            freq[t] += 1

    prefixes = []
    index: dict[str, list[int]] = defaultdict(list)
    for i, rep in enumerate(representatives):
        ordered = sorted(rep["title_tokens"], key=lambda tok: (freq[tok], tok))
        need = math.ceil(min_title * len(ordered) / (2 - min_title)) if min_title > 0 else 1
        prefix = ordered[: max(1, len(ordered) - need + 1)] if ordered else []
        prefixes.append(prefix)
        for tok in prefix:
            index[tok].append(i)

    started = time.monotonic()
    similar: dict[int, list[tuple[float, int]]] = defaultdict(list)
    compared = 0

    for i, prefix in enumerate(prefixes):
        seen: set[int] = set()
        for tok in prefix:
            for j in index[tok]:
                if j < i and j not in seen:
                    seen.add(j)
                    compared += 1
                    if compared % 2000 == 0 and time.monotonic() - started > time_budget:
                        break
                    sim = _duplication_similarity(representatives[i], representatives[j], threshold)
                    if sim is not None:
                        similar[i].append((sim, j))
                        similar[j].append((sim, i))
            if compared % 2000 == 0 and time.monotonic() - started > time_budget:
                break

    # 3. Emit at most one consolidated issue per affected page
    duplicate_issues: dict[str, SEOIssue] = {}
    for idx, group in enumerate(groups):
        partners: list[tuple[float, str, bool]] = [(1.0, member["url"], True) for member in group]
        for sim, j in similar.get(idx, []):
            for other in groups[j]:
                partners.append((sim, other["url"], False))

        if len(partners) < 2:
            continue

        partners.sort(key=lambda p: (-p[0], p[1]))
        total_partners = len(partners) - 1

        for member in group:
            examples = [p for p in partners if p[1] != member["url"]][:3]
            desc_parts = [
                f"{u} (identical title & h1)" if ident else f"{u} ({sim*100:.1f}% similar)"
                for sim, u, ident in examples
            ]
            more = total_partners - len(examples)
            detail_str = f"{total_partners} similar page{'s' if total_partners != 1 else ''}: {', '.join(desc_parts)}"
            if more > 0:
                detail_str += f" (+{more} more)"

            duplicate_issues[member["url"]] = SEOIssue(
                url=member["url"],
                severity="warning",
                category="Duplication",
                issue="Duplicate Content Detected",
                details=detail_str,
            )

    return duplicate_issues


def generate_site_summary(pages: list[PageTechnicalRecord]) -> SiteAuditSummary:
    """Generate aggregate site-wide health rollup from audited pages."""
    total_pages = len(pages)
    if total_pages == 0:
        return SiteAuditSummary()

    errors_count = 0
    warnings_count = 0
    info_count = 0
    issues_by_cat: dict[str, int] = defaultdict(int)
    generators: set[str] = set()
    analytics_found: set[str] = set()
    broken_links_set: set[str] = set()
    orphans = 0
    duplicate_pages = 0
    total_resp_time = 0

    for p in pages:
        total_resp_time += p.response_time_ms
        if p.generator:
            generators.add(p.generator)

        # Inlinks
        if p.inlinks_count == 0 and p.url != pages[0].url:
            orphans += 1

        # Broken outlinks
        for bo in p.broken_outlinks:
            broken_links_set.add(bo)

        # Analytics
        a = p.analytics
        if a.google_analytics or a.ga4_id:
            analytics_found.add(f"GA4 ({a.ga4_id})" if a.ga4_id else "Google Analytics")
        if a.gtm_id:
            analytics_found.add(f"GTM ({a.gtm_id})")
        if a.facebook_pixel:
            analytics_found.add("Facebook Pixel")
        if a.microsoft_clarity:
            analytics_found.add("Microsoft Clarity")
        if a.hotjar:
            analytics_found.add("Hotjar")
        if a.callrail:
            analytics_found.add("CallRail (Call Tracking)")
        if a.whatconverts:
            analytics_found.add("WhatConverts (Call Tracking)")
        if a.servicetitan:
            analytics_found.add("ServiceTitan")
        if a.housecall_pro:
            analytics_found.add("Housecall Pro")
        if a.podium:
            analytics_found.add("Podium (Reviews/Chat)")
        if a.birdeye:
            analytics_found.add("Birdeye (Reviews/Chat)")

        # Issues
        for iss in p.issues:
            issues_by_cat[iss.category] += 1
            if iss.severity == "error":
                errors_count += 1
            elif iss.severity == "warning":
                warnings_count += 1
            elif iss.severity == "info":
                info_count += 1
            if iss.category == "Duplication":
                duplicate_pages += 1

    return SiteAuditSummary(
        total_pages=total_pages,
        total_issues=errors_count + warnings_count + info_count,
        errors_count=errors_count,
        warnings_count=warnings_count,
        info_count=info_count,
        duplicate_pages_count=duplicate_pages,
        broken_internal_links_count=len(broken_links_set),
        orphan_pages_count=orphans,
        avg_response_time_ms=round(total_resp_time / max(1, total_pages), 1),
        cms_generators_detected=sorted(list(generators)),
        analytics_detected=sorted(list(analytics_found)),
        issues_by_category=dict(sorted(issues_by_cat.items(), key=lambda x: -x[1])),
    )

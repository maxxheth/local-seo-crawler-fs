"""SEO rule engine, automated issue detection, and duplication analyzer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatch
import math
import re
import time
from typing import Any
from urllib.parse import urlparse

from src.classifier import (
    CONVERSION_UTILITY_TOKENS,
    DEFAULT_PROTOTYPE_TOKENS,
    LOCAL_SCHEMA_TYPES,
    corporate_values,
    find_stale_dates,
    is_noindex,
    is_prototype_url,
    page_address_keys,
    token_in_segments,
    url_key,
)
from src.models import PageTechnicalRecord, SEOIssue, SiteAuditSummary
from src.sitemap import canonical_url_key, normalize_url
from src.storage import SITE_CONTEXT_ISSUE_NAMES  # noqa: F401 (re-exported)

SLOW_CRAWL_RESPONSE = "Slow Crawl Response (crawler-measured)"
SLOW_SAMPLED_RESPONSE = "Slow Server Response (sequential sample)"
ISSUE_MISSING_LOCAL_SCHEMA = "Missing LocalBusiness / Clinic Schema"
ISSUE_THANK_YOU = "Indexable Thank-You / Confirmation Page"
ISSUE_PROTOTYPE = "Indexable Prototype / Staging Page"
ISSUE_PROTOTYPE_NOINDEX = "Prototype / Staging Page"
ISSUE_STAGING_HOST = "Staging Hostname Reference"
ISSUE_MISSING_ADDRESS = "Clinic Page Missing Visible Address"
ISSUE_CLOSED_CLINIC = "Closed/moved clinic page: confirm indexing and schema approach"
ISSUE_OWNERSHIP = "Ownership transferred: confirm LocalBusiness schema"
ISSUE_STALE_NOTICE = "Stale Status Notice"
ISSUE_DUPLICATE_ADDRESS = "Duplicate Clinic Address"

DEFAULT_STAGING_HOST_PATTERNS = ("staging*", "*.staging.*", "*-staging.*", "*.on-forge.com", "dev.*")

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
    # Site-context (post-crawl) rules
    prototype_tokens: tuple[str, ...] = DEFAULT_PROTOTYPE_TOKENS
    prototype_allowlist: tuple[str, ...] = ()
    staging_host_patterns: tuple[str, ...] = DEFAULT_STAGING_HOST_PATTERNS
    corporate_share: float = 0.40
    crawl_date: date | None = None


def audit_page(record: PageTechnicalRecord, cfg: SEOAuditConfig | None = None) -> list[SEOIssue]:
    """Execute per-page SEO defect checks against configured thresholds."""
    if cfg is None:
        cfg = SEOAuditConfig()
    issues: list[SEOIssue] = []
    url = record.url
    status = record.status_code

    # 1. HTTP Status & Technical Transport
    if record.transport_error:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Technical",
                issue="Transport Error",
                details=(
                    f"[{record.transport_error_type}] {record.transport_error}"
                    if record.transport_error_type
                    else record.transport_error
                ),
            )
        )
        if status != 200:
            return issues

    if status in (301, 302, 303, 307, 308) or (record.initial_status_code in (301, 302, 303, 307, 308)):
        chain_str = " -> ".join(record.redirect_chain) if record.redirect_chain else f"HTTP {status}"
        if record.redirect_target_status == 404:
            issues.append(
                SEOIssue(
                    url=url,
                    severity="error",
                    category="Technical",
                    issue="Redirect Target Returns 404",
                    details=f"Redirect {chain_str} leads to broken 404 target: {record.final_url}",
                )
            )
        elif record.transport_error:
            issues.append(
                SEOIssue(
                    url=url,
                    severity="error",
                    category="Technical",
                    issue="Redirect Target Transport Error",
                    details=(
                        f"Redirect {chain_str} target failed connection/SSL"
                        f"{' [' + record.transport_error_type + ']' if record.transport_error_type else ''}: "
                        f"{record.transport_error}"
                    ),
                )
            )
        else:
            issues.append(
                SEOIssue(
                    url=url,
                    severity="info",
                    category="Technical",
                    issue=f"HTTP {status} Redirect",
                    details=f"Page redirected to {record.final_url or 'another location'} ({chain_str})",
                )
            )
        return issues

    if status == 404:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Technical",
                issue="Broken Page (HTTP 404)",
                details="Page returned 404 Not Found",
            )
        )
        return issues
    elif status >= 400 and status < 500:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Technical",
                issue=f"HTTP {status} Client Error",
                details=f"Page returned client error status code {status}",
            )
        )
        return issues

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
        return issues

    if status != 200:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Technical",
                issue=f"HTTP {status} Unexpected Status",
                details=f"Page returned status {status}",
            )
        )
        return issues

    if record.content_type and not any(ht in record.content_type.lower() for ht in ("text/html", "application/xhtml+xml")):
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Technical",
                issue="Non-HTML Asset Response",
                details=f"Page returned content type '{record.content_type}' instead of HTML",
            )
        )
        return issues

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
    x_robots = (record.x_robots_tag or "").lower()
    is_noindex = "noindex" in robots or "noindex" in x_robots
    if is_noindex:
        issues.append(
            SEOIssue(
                url=url,
                severity="error",
                category="Indexability",
                issue="Noindex Tag Present",
                details="Page has meta robots or X-Robots-Tag 'noindex' directive blocking search engines",
            )
        )
        if record.in_sitemap:
            issues.append(
                SEOIssue(
                    url=url,
                    severity="error",
                    category="Indexability",
                    issue="Noindex Page in Sitemap",
                    details="Page has a noindex directive but is submitted in the XML sitemap",
                )
            )

    if "nofollow" in robots or "nofollow" in x_robots:
        issues.append(
            SEOIssue(
                url=url,
                severity="warning",
                category="Indexability",
                issue="Nofollow Tag Present",
                details="Page has meta robots 'nofollow' directive preventing link graph crawl",
            )
        )

    # Thank-you, prototype/staging and LocalBusiness rules depend on page type and the
    # link graph; they run post-crawl in audit_site_context().

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
        if canonical_url_key(norm_canon) != canonical_url_key(norm_page):
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

    # 10. Performance. Crawl timings reflect crawler concurrency and network path, not
    # user experience, so they are informational only. Sequentially sampled TTFB
    # (--timing-sample) is evaluated post-crawl in audit_site_context().
    resp_time = record.response_time_ms
    if resp_time > cfg.slow_warn_ms:
        limit = cfg.slow_err_ms if resp_time > cfg.slow_err_ms else cfg.slow_warn_ms
        issues.append(
            SEOIssue(
                url=url,
                severity="info",
                category="Performance",
                issue=SLOW_CRAWL_RESPONSE,
                details=(
                    f"Crawler-measured fetch time was {resp_time}ms (above {limit}ms); "
                    "reflects crawler concurrency and network path, not user experience"
                ),
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


# ── Site-context rules (post-crawl; need page types and the link graph) ───────

def _page_get(p: PageTechnicalRecord | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(p, dict):
        return p.get(key, default)
    return getattr(p, key, default)


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    host = (host or "").lower().split(":")[0]
    return bool(host) and any(fnmatch(host, pat.lower()) for pat in patterns)


def audit_site_context(
    pages: list[PageTechnicalRecord] | list[dict[str, Any]],
    links: list[dict[str, Any]],
    cfg: SEOAuditConfig | None = None,
    timing_sample: dict[str, Any] | None = None,
) -> dict[str, list[SEOIssue]]:
    """Run rules that depend on post-crawl page classification and the link graph.

    Returns {url: [SEOIssue, ...]}. Pages must already carry classification fields
    (page_type, location_status, moved_to, status_evidence) from src.classifier.
    """
    cfg = cfg or SEOAuditConfig()
    crawl_date = cfg.crawl_date or date.today()
    raw = [p if isinstance(p, dict) else p.model_dump() for p in pages]
    out: dict[str, list[SEOIssue]] = defaultdict(list)

    def add(url: str, severity: str, category: str, issue: str, details: str) -> None:
        out[url].append(SEOIssue(url=url, severity=severity, category=category, issue=issue, details=details))

    inbound: dict[str, int] = defaultdict(int)
    for e in links:
        s_key, t_key = url_key(e.get("source_url", "")), url_key(e.get("target_url", ""))
        if s_key != t_key:
            inbound[t_key] += 1

    _corp_phones, corp_addrs = corporate_values(raw, cfg.corporate_share)
    by_key = {url_key(p.get("url", "")): p for p in raw}
    site_hosts = {urlparse(p.get("url", "")).netloc.lower() for p in raw}

    # Address keys per clinic page, computed once
    clinic_addr_keys: dict[str, set[str]] = {}
    addr_groups: dict[str, list[str]] = defaultdict(list)
    for p in raw:
        if p.get("page_type") == "clinic_location" and int(p.get("status_code") or 0) == 200:
            keys = page_address_keys(p, corp_addrs)
            clinic_addr_keys[p["url"]] = keys
            for k in keys:
                addr_groups[k].append(p["url"])

    # Duplicate clinic addresses: pages sharing an address, minus those whose moved_to points
    # at another page in the same group (computed once per address group)
    unexplained_groups: dict[str, list[str]] = {}
    for k, group in addr_groups.items():
        if len(group) < 2:
            continue
        group_keys = {url_key(g) for g in group}
        explained = {
            g for g in group
            if (by_key.get(url_key(g)) or {}).get("moved_to")
            and url_key(by_key[url_key(g)]["moved_to"]) in group_keys
        }
        remaining = sorted(set(group) - explained)
        if len(remaining) >= 2:
            unexplained_groups[k] = remaining

    allowlist = tuple(cfg.prototype_allowlist)

    sampled = {}
    if timing_sample:
        for row in timing_sample.get("samples", []):
            if row.get("ttfb_ms") is not None:
                sampled[url_key(row.get("url", ""))] = row

    for p in raw:
        url = p.get("url", "")
        status = int(p.get("status_code") or 0)
        if status != 200:
            continue
        noindex = is_noindex(p)
        ptype = p.get("page_type") or ""

        # Clinic pages
        if ptype == "clinic_location":
            loc_status = p.get("location_status") or "unknown"
            evidence = p.get("status_evidence") or {}
            schema_types = set(p.get("schema_types") or [])
            has_local_schema = bool(schema_types & set(LOCAL_SCHEMA_TYPES))
            if loc_status in ("open", "unknown"):
                if not noindex and not has_local_schema:
                    add(url, "warning", "Structured Data", ISSUE_MISSING_LOCAL_SCHEMA,
                        f"Clinic page ({loc_status}) lacks LocalBusiness/MedicalBusiness/DaySpa Schema.org structured data")
            elif loc_status in ("closed", "temporarily_closed", "moved"):
                detail = f"Clinic page status is {loc_status}"
                if p.get("moved_to"):
                    detail += f" (now served by {p['moved_to']})"
                if evidence.get("source"):
                    detail += f"; evidence: {evidence['source']}"
                add(url, "info", "Local SEO", ISSUE_CLOSED_CLINIC,
                    detail + "; confirm whether it should stay indexed and which LocalBusiness schema (if any) it carries")
            elif loc_status == "ownership_transferred":
                add(url, "info", "Local SEO", ISSUE_OWNERSHIP,
                    "Ownership transferred: confirm whether this page should carry the brand's LocalBusiness schema "
                    "(business decision, not inferred by the crawler)")
            own_keys = clinic_addr_keys.get(url, set())
            if not own_keys:
                sev = "warning" if loc_status in ("open", "unknown", "coming_soon") else "info"
                add(url, sev, "Local SEO", ISSUE_MISSING_ADDRESS,
                    f"Clinic page ({loc_status}) shows no visible street address")
            notice = evidence.get("page_notice") or evidence.get("directory_label") or ""
            stale = find_stale_dates(notice, crawl_date)
            if stale:
                add(url, "warning", "Local SEO", ISSUE_STALE_NOTICE,
                    f"Status notice references a date already past at crawl time ({crawl_date.isoformat()}): "
                    f"{'; '.join(stale)} — notice: \"{notice[:200]}\"")
            dup_with = sorted({
                g for k in own_keys for g in unexplained_groups.get(k, []) if url in unexplained_groups[k] and g != url
            })
            if dup_with:
                add(url, "warning", "Local SEO", ISSUE_DUPLICATE_ADDRESS,
                    f"Displays the same address as {', '.join(dup_with)} with no moved_to relationship")

        # Conversion confirmation pages
        if ptype == "utility_page" and not noindex and token_in_segments(url, CONVERSION_UTILITY_TOKENS):
            add(url, "warning", "Indexability", ISSUE_THANK_YOU,
                "Conversion confirmation page is indexable by search engines; recommend adding 'noindex' directive")

        # Prototype / staging pages (whole slug words only; never blog posts or archives)
        if ptype not in ("blog_post", "blog_archive"):
            hit = is_prototype_url(url, cfg.prototype_tokens, allowlist)
            if hit:
                linked = inbound.get(url_key(url), 0) > 0
                in_sitemap = bool(p.get("in_sitemap"))
                reason = f"slug word '{hit[0]}' in segment '{hit[1]}'"
                if noindex:
                    add(url, "info", "Indexability", ISSUE_PROTOTYPE_NOINDEX,
                        f"Prototype/staging page ({reason}) is noindex; consider removing it or restricting access")
                else:
                    exposure = [x for x, flag in (("in XML sitemap", in_sitemap), ("linked internally", linked)) if flag]
                    sev = "error" if exposure else "warning"
                    add(url, sev, "Indexability", ISSUE_PROTOTYPE,
                        f"Prototype/staging page ({reason}) is indexable"
                        + (f" and {' and '.join(exposure)}" if exposure else "")
                        + "; recommend noindex, removal, or access restriction")

        # References to staging hosts
        refs: list[tuple[str, str]] = []
        for h in p.get("external_link_hosts") or []:
            refs.append(("link", h))
        canon = p.get("canonical_url") or ""
        if canon:
            refs.append(("canonical", urlparse(canon).netloc))
        og_url = (p.get("og_tags") or {}).get("url") or ""
        if og_url:
            refs.append(("og:url", urlparse(og_url).netloc))
        for hl in p.get("hreflang") or []:
            refs.append(("hreflang", urlparse(hl.get("url", "")).netloc))
        seen_refs: set[tuple[str, str]] = set()
        for kind, host in refs:
            host = (host or "").lower()
            if not host or host in site_hosts or (kind, host) in seen_refs:
                continue
            if _host_matches(host, cfg.staging_host_patterns):
                seen_refs.add((kind, host))
                add(url, "error" if kind != "link" else "warning", "Technical", ISSUE_STAGING_HOST,
                    f"{kind} points at staging-like host {host}")

        # Sequential timing sample (WS6): only these timings can raise a warning
        srow = sampled.get(url_key(url))
        if srow and int(srow.get("ttfb_ms") or 0) > cfg.slow_warn_ms:
            add(url, "warning", "Performance", SLOW_SAMPLED_RESPONSE,
                f"Sequentially sampled TTFB was {srow['ttfb_ms']}ms (recommended: <{cfg.slow_warn_ms}ms; "
                f"cache: {srow.get('cache_status') or 'n/a'})")

    return dict(out)


# ── Near-Duplicate Content Detection (Token-Dice with Prefix Filtering) ───────

_DUP_WEIGHTS = {
    "title": 0.40 / 1.20,  # Minimum normalized title weight when both desc (0.25) and body (0.20) are present
    "desc": 0.25 / 1.20,
    "h1": 0.25 / 1.20,
    "word_count": 0.10 / 1.20,
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
    has_desc = bool(a.get("desc_tokens") and b.get("desc_tokens"))
    has_body = bool(a.get("body_tokens") and b.get("body_tokens"))

    base_weights: dict[str, float] = {
        "title": 0.40,
        "h1": 0.25,
        "word_count": 0.10,
    }
    if has_desc:
        base_weights["desc"] = 0.25
    if has_body:
        base_weights["body"] = 0.20

    total_w = sum(base_weights.values())
    weights = {k: v / total_w for k, v in base_weights.items()}

    score = _dice(a["title_tokens"], b["title_tokens"]) * weights["title"]
    if score + (1.0 - weights["title"]) < threshold:
        return None

    if has_desc:
        score += _dice(a["desc_tokens"], b["desc_tokens"]) * weights["desc"]

    score += _dice(a["h1_tokens"], b["h1_tokens"]) * weights["h1"]

    if has_body:
        score += _dice(a["body_tokens"], b["body_tokens"]) * weights["body"]

    score += _length_similarity(a["word_count"], b["word_count"]) * weights["word_count"]
    return score if score >= threshold else None


def detect_duplicate_pages(
    pages: list[PageTechnicalRecord] | list[dict[str, Any]],
    threshold: float = 0.85,
    time_budget: float = DUPLICATION_TIME_BUDGET_SECONDS,
    pairs_out: list[tuple[str, str, float]] | None = None,
) -> dict[str, SEOIssue]:
    """Detect near-duplicate or doorway pages across crawled records using Dice token similarity.

    Accepts both PageTechnicalRecord instances and raw page dicts from storage.
    Returns mapping of url -> SEOIssue. If pairs_out is given, it is filled with
    (url_a, url_b, similarity) edges whose connected components are the duplicate clusters.
    """
    candidates = []
    for p in pages:
        status_code = p.get("status_code", 0) if isinstance(p, dict) else getattr(p, "status_code", 0)
        if status_code != 200:
            continue
        redirect_chain = p.get("redirect_chain", []) if isinstance(p, dict) else getattr(p, "redirect_chain", [])
        initial_status = p.get("initial_status_code", 0) if isinstance(p, dict) else getattr(p, "initial_status_code", 0)
        if redirect_chain or (initial_status in (301, 302, 303, 307, 308)):
            continue
        meta_robots = p.get("meta_robots", "") if isinstance(p, dict) else getattr(p, "meta_robots", "")
        x_robots_tag = p.get("x_robots_tag", "") if isinstance(p, dict) else getattr(p, "x_robots_tag", "")
        robots = (meta_robots or "").lower()
        x_robots = (x_robots_tag or "").lower()
        if "noindex" in robots or "noindex" in x_robots:
            continue
        url = p.get("url", "") if isinstance(p, dict) else getattr(p, "url", "")
        if _page_get(p, "page_type") == "utility_page":
            continue
        if any(pat in url.lower() for pat in ("thank-you", "thankyou", "confirmation", "form-received", "quote-received")):
            continue

        raw_title = p.get("title", "") if isinstance(p, dict) else getattr(p, "title", "")
        raw_desc = p.get("meta_description", "") if isinstance(p, dict) else getattr(p, "meta_description", "")
        raw_h1 = p.get("h1", []) if isinstance(p, dict) else getattr(p, "h1", [])
        raw_body_tokens = p.get("body_tokens_sample", []) if isinstance(p, dict) else getattr(p, "body_tokens_sample", [])
        raw_first_50 = p.get("first_50_words", "") if isinstance(p, dict) else getattr(p, "first_50_words", "")
        word_count = p.get("word_count", 0) if isinstance(p, dict) else getattr(p, "word_count", 0)

        title = _normalise_text(raw_title)
        desc = _normalise_text(raw_desc)
        h1 = _normalise_text(raw_h1)
        body = _normalise_text(raw_body_tokens or raw_first_50)
        if not title and not desc and not h1:
            continue
        candidates.append({
            "url": url,
            "title": title,
            "desc": desc,
            "h1": h1,
            "body": body,
            "title_tokens": _tokens(title),
            "desc_tokens": _tokens(desc),
            "h1_tokens": _tokens(h1),
            "body_tokens": _tokens(body),
            "word_count": word_count,
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

    if pairs_out is not None:
        for g in groups:
            for member in g[1:]:
                pairs_out.append((g[0]["url"], member["url"], 1.0))
        for i, partners_i in sorted(similar.items()):
            for sim, j in partners_i:
                if i < j:
                    pairs_out.append((representatives[i]["url"], representatives[j]["url"], round(sim, 4)))

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
    broken_types: dict[str, str] = {}
    page_type_counts: dict[str, int] = defaultdict(int)
    location_status_counts: dict[str, int] = defaultdict(int)
    orphans = 0
    duplicate_pages = 0
    total_resp_time = 0

    for p in pages:
        total_resp_time += p.response_time_ms
        if p.generator:
            generators.add(p.generator)

        # Inlinks (only count indexable 200 OK pages after root as orphans)
        if p.status_code == 200 and p.inlinks_count == 0 and p.url != pages[0].url:
            orphans += 1

        # Broken outlinks
        for bo in p.broken_outlinks:
            broken_links_set.add(bo)
        for d in p.broken_outlink_details:
            t = d.get("target_url", "")
            err = d.get("error_type") or ""
            st = d.get("target_status")
            broken_types.setdefault(t, err if err else f"http_{st}")

        if p.page_type:
            page_type_counts[p.page_type] += 1
        if p.location_status:
            location_status_counts[p.location_status] += 1

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
            if iss.severity in ("error", "critical"):
                errors_count += 1
            elif iss.severity == "warning":
                warnings_count += 1
            elif iss.severity == "info":
                info_count += 1
            if iss.category == "Duplication":
                duplicate_pages += 1

    resp_times = sorted([p.response_time_ms for p in pages if p.response_time_ms > 0])
    if resp_times:
        import statistics
        median_resp = round(float(statistics.median(resp_times)), 1)
        p95_idx = int(math.ceil(0.95 * len(resp_times))) - 1
        p95_resp = round(float(resp_times[max(0, min(p95_idx, len(resp_times) - 1))]), 1)
    else:
        median_resp = 0.0
        p95_resp = 0.0

    servers = {p.server_header for p in pages if p.server_header}
    caching = {p.cache_status for p in pages if p.cache_status}

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
        median_response_time_ms=median_resp,
        p95_response_time_ms=p95_resp,
        server_stack_detected=sorted(list(servers)),
        caching_detected=sorted(list(caching)),
        cms_generators_detected=sorted(list(generators)),
        analytics_detected=sorted(list(analytics_found)),
        issues_by_category=dict(sorted(issues_by_cat.items(), key=lambda x: -x[1])),
        broken_targets_by_error_type=dict(sorted(_count_values(broken_types).items())),
        timing_source="crawler",
        page_type_counts=dict(sorted(page_type_counts.items())),
        location_status_counts=dict(sorted(location_status_counts.items())),
    )


def _count_values(mapping: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for v in mapping.values():
        counts[v] += 1
    return dict(counts)

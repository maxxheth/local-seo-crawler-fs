"""Post-crawl analysis stage: classification, site-context audit, duplicates, link sync.

Runs after link verification and before export. Kept separate from the orchestrator so
it can be exercised on an existing database (tests, re-analysis) without crawling.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import logging
from typing import Any

from src.classifier import ClassifierConfig, classify_pages, clinic_detection_method
from src.models import PageTechnicalRecord
from src.seo_auditor import SEOAuditConfig, audit_site_context, detect_duplicate_pages
from src.storage import SQLiteStorage

logger = logging.getLogger(__name__)


def run_post_crawl_stage(
    storage: SQLiteStorage,
    audit_cfg: SEOAuditConfig | None = None,
    classifier_cfg: ClassifierConfig | None = None,
    crawl_date: date | None = None,
    timing_sample: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify pages, run site-context rules, detect duplicates, and sync link-graph issues."""
    crawl_date = crawl_date or date.today()
    audit_cfg = replace(audit_cfg or SEOAuditConfig(), crawl_date=crawl_date)
    classifier_cfg = replace(
        classifier_cfg or ClassifierConfig(),
        crawl_date=crawl_date,
        prototype_tokens=audit_cfg.prototype_tokens,
        prototype_allowlist=audit_cfg.prototype_allowlist,
        corporate_share=audit_cfg.corporate_share,
    )
    storage.set_meta("crawl_date", crawl_date.isoformat())

    raw_pages = storage.fetch_all_pages()
    links = storage.fetch_all_internal_links()

    classifications = classify_pages(raw_pages, links, classifier_cfg)
    storage.save_classifications(classifications)
    for p in raw_pages:  # same update in memory; no need to reload every page
        p.update(classifications.get(p.get("url", ""), {}))

    if timing_sample is None:
        timing_sample = storage.get_meta("timing_sample")
    context_issues = audit_site_context(raw_pages, links, audit_cfg, timing_sample=timing_sample)

    pairs: list[tuple[str, str, float]] = []
    page_objs = [PageTechnicalRecord(**p) for p in raw_pages]
    duplicate_issues = detect_duplicate_pages(page_objs, threshold=audit_cfg.dup_threshold, pairs_out=pairs)
    storage.save_duplicate_pairs(pairs)

    storage.post_crawl_sync(duplicate_issues, context_issues=context_issues)

    counts: dict[str, int] = {}
    for c in classifications.values():
        counts[c["page_type"]] = counts.get(c["page_type"], 0) + 1
    return {
        "page_types": counts,
        "clinic_detection": clinic_detection_method(raw_pages),
        "context_issue_pages": len(context_issues),
        "duplicate_pairs": len(pairs),
    }

"""Excel (.xlsx) exporter with relational multi-worksheet layout and visual styling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import re

from src.storage import is_broken_target
from src.models import (
    CitationItem,
    EvidenceRecord,
    PageTechnicalRecord,
    SiteAuditSummary,
)

# XML 1.0 illegal control characters (0x00-0x08, 0x0B-0x0C, 0x0E-0x1F)
_ILLEGAL_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_data(obj: Any) -> Any:
    """Recursively strip XML 1.0 illegal control characters from strings, lists, dicts, and Pydantic models."""
    if isinstance(obj, str):
        return _ILLEGAL_XML_CHARS_RE.sub("", obj)
    if isinstance(obj, list):
        return [_sanitize_data(x) for x in obj]
    if isinstance(obj, dict):
        return {_sanitize_data(k): _sanitize_data(v) for k, v in obj.items()}
    if hasattr(obj, "model_dump"):
        cls = type(obj)
        return cls(**_sanitize_data(obj.model_dump()))
    return obj

# Color Palette
NAVY_HEADER = "1E293B"       # Slate 800
WHITE_TEXT = "FFFFFF"
BORDER_COLOR = "CBD5E1"     # Slate 300
ZEBRA_FILL = "F8FAFC"       # Slate 50

# Severity & Status colors
RED_BG = "FEE2E2"           # Red 100
RED_TXT = "991B1B"          # Red 800
YELLOW_BG = "FEF3C7"        # Amber 100
YELLOW_TXT = "92400E"       # Amber 800
GREEN_BG = "DCFCE7"         # Green 100
GREEN_TXT = "166534"        # Green 800
BLUE_BG = "E0F2FE"          # Sky 100
BLUE_TXT = "075985"         # Sky 800

HEADER_FONT = Font(name="Calibri", size=11, bold=True, color=WHITE_TEXT)
HEADER_FILL = PatternFill(start_color=NAVY_HEADER, end_color=NAVY_HEADER, fill_type="solid")
REGULAR_FONT = Font(name="Calibri", size=10)
BOLD_FONT = Font(name="Calibri", size=10, bold=True)
TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="0F172A")
SUBTITLE_FONT = Font(name="Calibri", size=10, italic=True, color="64748B")

THIN_BORDER = Border(
    left=Side(style="thin", color=BORDER_COLOR),
    right=Side(style="thin", color=BORDER_COLOR),
    top=Side(style="thin", color=BORDER_COLOR),
    bottom=Side(style="thin", color=BORDER_COLOR),
)


def _apply_table_styling(
    ws: Any,
    start_row: int = 1,
    freeze_header: bool = True,
    auto_filter: bool = True,
    max_col_width: int = 60,
) -> None:
    """Format table headers, borders, zebra striping, auto-filters, and auto-adjusted column widths."""
    max_row = ws.max_row
    max_col = ws.max_column

    if max_row < start_row or max_col < 1:
        return

    # 1. Format Header Row
    for col_idx in range(1, max_col + 1):
        cell = ws.cell(row=start_row, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[start_row].height = 28

    # 2. Format Data Rows & Column Widths
    col_widths: dict[int, int] = {}
    for col_idx in range(1, max_col + 1):
        header_val = str(ws.cell(row=start_row, column=col_idx).value or "")
        col_widths[col_idx] = max(len(header_val) + 4, 12)

    for r_idx in range(start_row + 1, max_row + 1):
        ws.row_dimensions[r_idx].height = 20
        is_even = (r_idx % 2 == 0)
        for c_idx in range(1, max_col + 1):
            cell = ws.cell(row=r_idx, column=c_idx)
            if isinstance(cell.value, str):
                cell.value = _ILLEGAL_XML_CHARS_RE.sub("", cell.value)
            if not cell.font or cell.font.name != "Calibri":
                cell.font = REGULAR_FONT
            cell.border = THIN_BORDER

            # Default zebra stripe if not specifically colored
            if not cell.fill or cell.fill.fill_type is None:
                if is_even:
                    cell.fill = PatternFill(start_color=ZEBRA_FILL, end_color=ZEBRA_FILL, fill_type="solid")

            # Update column width tracker
            val_str = str(cell.value or "")
            if "\n" in val_str:
                val_str = max(val_str.split("\n"), key=len)
            col_widths[c_idx] = max(col_widths[c_idx], min(len(val_str) + 3, max_col_width))

    # Apply computed column widths
    for c_idx, width in col_widths.items():
        col_letter = get_column_letter(c_idx)
        ws.column_dimensions[col_letter].width = width

    # Auto-filter and freeze pane
    if auto_filter and max_row >= start_row:
        start_letter = get_column_letter(1)
        end_letter = get_column_letter(max_col)
        ws.auto_filter.ref = f"{start_letter}{start_row}:{end_letter}{max_row}"

    if freeze_header:
        ws.freeze_panes = f"A{start_row + 1}"


def _style_status_cell(cell: Any, status: Any) -> None:
    """Apply soft status coloring to a cell."""
    st = str(status).strip().lower()
    if st in ("error", "blocked", "captcha", "not_found", "mismatch", "failed", "500", "502", "503", "404", "403"):
        cell.fill = PatternFill(start_color=RED_BG, end_color=RED_BG, fill_type="solid")
        cell.font = Font(name="Calibri", size=10, bold=True, color=RED_TXT)
    elif st in ("warning", "partial", "slow", "thin", "301", "302"):
        cell.fill = PatternFill(start_color=YELLOW_BG, end_color=YELLOW_BG, fill_type="solid")
        cell.font = Font(name="Calibri", size=10, bold=True, color=YELLOW_TXT)
    elif st in ("live", "match", "success", "ok", "200"):
        cell.fill = PatternFill(start_color=GREEN_BG, end_color=GREEN_BG, fill_type="solid")
        cell.font = Font(name="Calibri", size=10, bold=True, color=GREEN_TXT)
    elif st in ("info",):
        cell.fill = PatternFill(start_color=BLUE_BG, end_color=BLUE_BG, fill_type="solid")
        cell.font = Font(name="Calibri", size=10, bold=True, color=BLUE_TXT)


def write_site_crawl_excel(
    file_path: Path | str,
    target_domain: str,
    pages: list[PageTechnicalRecord | dict[str, Any]],
    audit_summary: SiteAuditSummary | None = None,
    crawl_depth: int = 1,
    evidence_records: list[EvidenceRecord | dict[str, Any]] | None = None,
    link_edges: list[dict[str, Any]] | None = None,
) -> str:
    """Generate structured multi-worksheet Excel workbook for competitor site crawl."""
    wb = openpyxl.Workbook()
    # Remove default sheet
    default_sheet = wb.active
    wb.remove(default_sheet)

    target_domain = _sanitize_data(target_domain)
    pages = _sanitize_data(pages)
    evidence_records = _sanitize_data(evidence_records)
    link_edges = _sanitize_data(link_edges)
    audit_summary = _sanitize_data(audit_summary)

    page_objs = [p if isinstance(p, PageTechnicalRecord) else PageTechnicalRecord(**p) for p in pages]
    evidence_objs = [
        e if isinstance(e, EvidenceRecord) else EvidenceRecord(**e)
        for e in (evidence_records or [])
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 1: Audit Summary
    # ─────────────────────────────────────────────────────────────────────────
    ws_summary = wb.create_sheet(title="Audit Summary")
    ws_summary.views.sheetView[0].showGridLines = True

    # Title block
    ws_summary.cell(row=1, column=1, value=f"Local SEO Site Audit Summary — {target_domain}").font = TITLE_FONT
    ws_summary.cell(row=2, column=1, value=f"Target Domain: {target_domain} | Total Pages Audited: {len(page_objs)}").font = SUBTITLE_FONT

    headers_kpi = ["Audit KPI Metric", "Value", "Status / Notes"]
    ws_summary.row_dimensions[4].height = 24
    for c_idx, h in enumerate(headers_kpi, start=1):
        cell = ws_summary.cell(row=4, column=c_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER

    summary = audit_summary
    total_issues = summary.total_issues if summary else sum(len(p.issues) for p in page_objs)
    errors = summary.errors_count if summary else sum(sum(1 for i in p.issues if i.severity == "error") for p in page_objs)
    warnings = summary.warnings_count if summary else sum(sum(1 for i in p.issues if i.severity == "warning") for p in page_objs)
    info = summary.info_count if summary else sum(sum(1 for i in p.issues if i.severity == "info") for p in page_objs)
    dup_pages = summary.duplicate_pages_count if summary else 0
    broken_links = summary.broken_internal_links_count if summary else sum(len(p.broken_outlinks) for p in page_objs)
    orphan_pages = summary.orphan_pages_count if summary else sum(1 for idx, p in enumerate(page_objs) if p.status_code == 200 and p.inlinks_count == 0 and idx > 0)
    avg_speed = summary.avg_response_time_ms if summary else round(sum(p.response_time_ms for p in page_objs) / max(1, len(page_objs)), 1)
    median_speed = summary.median_response_time_ms if summary else avg_speed
    p95_speed = summary.p95_response_time_ms if summary else avg_speed

    kpis = [
        ("Total Pages Crawled", len(page_objs), "Indexable and non-indexable discovered URLs"),
        ("Total Discovered SEO Issues", total_issues, "Across 25+ automated on-page SEO rules"),
        ("Critical Errors", errors, "Severe defects (broken links, 5xx/4xx, missing viewport/title)"),
        ("Warnings", warnings, "Sub-optimal elements (title/meta lengths, thin content, missing H1/alt)"),
        ("Info / Notices", info, "Redirects, external canonical pointers, missing OG tags"),
        ("Near-Duplicate Content Pages", dup_pages, "Dice similarity >= threshold with near-identical copy"),
        ("Broken Internal Link Targets", broken_links, "Internal targets returning 4xx/5xx or failing at transport level (TLS, DNS, timeout)"),
        ("Orphan Pages (0 Inlinks)", orphan_pages, "Pages not linked from any internal crawl page"),
        ("Average Crawl Response Time (crawler-measured)", f"{avg_speed} ms", "Reflects crawler concurrency and network path, not user experience"),
        ("Median Crawl Response Time (crawler-measured)", f"{median_speed} ms", "50th percentile of crawler-measured fetch time"),
        ("p95 Crawl Response Time (crawler-measured)", f"{p95_speed} ms", "95th percentile of crawler-measured fetch time"),
    ]

    for row_idx, (metric, val, note) in enumerate(kpis, start=5):
        c1 = ws_summary.cell(row=row_idx, column=1, value=metric)
        c2 = ws_summary.cell(row=row_idx, column=2, value=val)
        c3 = ws_summary.cell(row=row_idx, column=3, value=note)
        c1.font = BOLD_FONT
        c2.font = BOLD_FONT
        c1.border = THIN_BORDER
        c2.border = THIN_BORDER
        c3.border = THIN_BORDER
        c2.alignment = Alignment(horizontal="center")
        if metric == "Critical Errors" and errors > 0:
            _style_status_cell(c2, "error")
        elif metric == "Warnings" and warnings > 0:
            _style_status_cell(c2, "warning")
        elif metric in ("Total Pages Crawled", "Average Server Response Time", "Median Server Response Time"):
            _style_status_cell(c2, "ok")

    # Section: Issues by Category
    start_cat_row = 5 + len(kpis) + 2
    ws_summary.cell(row=start_cat_row, column=1, value="SEO Defects by Category").font = Font(name="Calibri", size=12, bold=True)
    cat_headers = ["Category", "Issues Count", "Distribution"]
    for c_idx, h in enumerate(cat_headers, start=1):
        cell = ws_summary.cell(row=start_cat_row + 1, column=c_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER

    cat_counts: dict[str, int] = {}
    if summary and summary.issues_by_category:
        cat_counts = summary.issues_by_category
    else:
        for p in page_objs:
            for iss in p.issues:
                cat_counts[iss.category] = cat_counts.get(iss.category, 0) + 1

    r = start_cat_row + 2
    for cat_name, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        pct = f"{(cnt / max(1, total_issues)) * 100:.1f}%"
        ws_summary.cell(row=r, column=1, value=cat_name).border = THIN_BORDER
        c_cnt = ws_summary.cell(row=r, column=2, value=cnt)
        c_cnt.border = THIN_BORDER
        c_cnt.alignment = Alignment(horizontal="center")
        ws_summary.cell(row=r, column=3, value=pct).border = THIN_BORDER
        r += 1

    # Section: Tech & Marketing Stack Detected
    stack_row = r + 2
    ws_summary.cell(row=stack_row, column=1, value="Marketing & Technology Stack Detected").font = Font(name="Calibri", size=12, bold=True)
    ws_summary.cell(row=stack_row + 1, column=1, value="Tool / Technology").font = HEADER_FONT
    ws_summary.cell(row=stack_row + 1, column=1).fill = HEADER_FILL
    ws_summary.cell(row=stack_row + 1, column=2, value="Category / Type").font = HEADER_FONT
    ws_summary.cell(row=stack_row + 1, column=2).fill = HEADER_FILL

    detected_stack: list[tuple[str, str]] = []
    if summary and summary.analytics_detected:
        for a_name in summary.analytics_detected:
            detected_stack.append((a_name, "Analytics & Tracking"))
    if summary and summary.cms_generators_detected:
        for gen in summary.cms_generators_detected:
            detected_stack.append((gen, "CMS / Framework"))
    if summary and summary.server_stack_detected:
        for srv in summary.server_stack_detected:
            detected_stack.append((srv, "Web Server / CDN"))
    if summary and summary.caching_detected:
        for c_hdr in summary.caching_detected:
            detected_stack.append((c_hdr, "HTTP Cache Status"))

    if not detected_stack:
        # Fallback inspection across pages
        for p in page_objs:
            if p.generator:
                item = (p.generator, "CMS / Framework")
                if item not in detected_stack:
                    detected_stack.append(item)
            a = p.analytics
            if (a.google_analytics or a.ga4_id) and ("Google Analytics / GA4", "Analytics & Tracking") not in detected_stack:
                detected_stack.append(("Google Analytics / GA4", "Analytics & Tracking"))
            if a.gtm_id and (f"GTM ({a.gtm_id})", "Tag Management") not in detected_stack:
                detected_stack.append((f"GTM ({a.gtm_id})", "Tag Management"))
            if a.callrail and ("CallRail", "Call Tracking") not in detected_stack:
                detected_stack.append(("CallRail", "Call Tracking"))
            if a.servicetitan and ("ServiceTitan", "Field Service") not in detected_stack:
                detected_stack.append(("ServiceTitan", "Field Service"))

    sr = stack_row + 2
    if not detected_stack:
        ws_summary.cell(row=sr, column=1, value="None explicitly detected in DOM").border = THIN_BORDER
        ws_summary.cell(row=sr, column=2, value="—").border = THIN_BORDER
    else:
        for tool_name, t_cat in detected_stack:
            ws_summary.cell(row=sr, column=1, value=tool_name).border = THIN_BORDER
            ws_summary.cell(row=sr, column=2, value=t_cat).border = THIN_BORDER
            sr += 1

    ws_summary.column_dimensions["A"].width = 38
    ws_summary.column_dimensions["B"].width = 25
    ws_summary.column_dimensions["C"].width = 50

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 2: Pages Overview
    # ─────────────────────────────────────────────────────────────────────────
    ws_pages = wb.create_sheet(title="Pages Overview")
    ws_pages.views.sheetView[0].showGridLines = True
    page_headers = [
        "URL",
        "Final URL",
        "HTTP Status",
        "Title",
        "Title Length",
        "Meta Description",
        "Desc Length",
        "Canonical URL",
        "Primary H1",
        "H1 Count",
        "H2 Count",
        "H3 Count",
        "Word Count",
        "Inlinks",
        "Internal Outlinks",
        "External Outlinks",
        "Nofollow Outlinks",
        "Broken Outlinks Count",
        "SEO Issues Count",
        "Response Time (ms)",
        "Page Size (KB)",
        "SSR Rendered",
        "Language",
        "Charset",
        "Viewport Tag",
        "Robots Meta",
        "Generator / CMS",
        "Initial HTTP Status",
        "Redirect Chain",
        "TTFB (ms)",
        "Server Header",
        "Cache Status",
        "In Sitemap",
        "Page Type",
        "Location Status",
        "Transport Error Type",
    ]
    for c_idx, h in enumerate(page_headers, start=1):
        ws_pages.cell(row=1, column=c_idx, value=h)

    for r_idx, p in enumerate(page_objs, start=2):
        ws_pages.cell(row=r_idx, column=1, value=p.url)
        ws_pages.cell(row=r_idx, column=2, value=p.final_url or p.url)
        c_status = ws_pages.cell(row=r_idx, column=3, value=p.status_code)
        _style_status_cell(c_status, p.status_code)

        ws_pages.cell(row=r_idx, column=4, value=p.title)
        ws_pages.cell(row=r_idx, column=5, value=len(p.title) if p.title else 0)
        ws_pages.cell(row=r_idx, column=6, value=p.meta_description)
        ws_pages.cell(row=r_idx, column=7, value=len(p.meta_description) if p.meta_description else 0)
        ws_pages.cell(row=r_idx, column=8, value=p.canonical_url)

        primary_h1 = p.h1[0] if p.h1 else ""
        ws_pages.cell(row=r_idx, column=9, value=primary_h1)
        ws_pages.cell(row=r_idx, column=10, value=len(p.h1))
        ws_pages.cell(row=r_idx, column=11, value=len(p.h2))
        ws_pages.cell(row=r_idx, column=12, value=len(p.h3))
        ws_pages.cell(row=r_idx, column=13, value=p.word_count)

        ws_pages.cell(row=r_idx, column=14, value=p.inlinks_count)
        ws_pages.cell(row=r_idx, column=15, value=p.internal_outlinks_count)
        ws_pages.cell(row=r_idx, column=16, value=p.external_outlinks_count)
        ws_pages.cell(row=r_idx, column=17, value=p.nofollow_outlinks_count)
        ws_pages.cell(row=r_idx, column=18, value=len(p.broken_outlinks))
        c_iss_cnt = ws_pages.cell(row=r_idx, column=19, value=len(p.issues))
        if len(p.issues) > 0:
            _style_status_cell(c_iss_cnt, "warning")

        ws_pages.cell(row=r_idx, column=20, value=p.response_time_ms)
        ws_pages.cell(row=r_idx, column=21, value=round(p.page_size_bytes / 1024, 1))
        ws_pages.cell(row=r_idx, column=22, value="Yes" if p.is_ssr else "No")
        ws_pages.cell(row=r_idx, column=23, value=p.lang)
        ws_pages.cell(row=r_idx, column=24, value=p.charset)
        ws_pages.cell(row=r_idx, column=25, value=p.viewport)
        ws_pages.cell(row=r_idx, column=26, value=p.meta_robots)
        ws_pages.cell(row=r_idx, column=27, value=p.generator)
        ws_pages.cell(row=r_idx, column=28, value=p.initial_status_code or p.status_code)
        ws_pages.cell(row=r_idx, column=29, value=" -> ".join(p.redirect_chain) if p.redirect_chain else "")
        ws_pages.cell(row=r_idx, column=30, value=p.ttfb_ms)
        ws_pages.cell(row=r_idx, column=31, value=p.server_header)
        ws_pages.cell(row=r_idx, column=32, value=p.cache_status)
        ws_pages.cell(row=r_idx, column=33, value="Yes" if p.in_sitemap else "No")
        ws_pages.cell(row=r_idx, column=34, value=p.page_type)
        ws_pages.cell(row=r_idx, column=35, value=p.location_status)
        ws_pages.cell(row=r_idx, column=36, value=p.transport_error_type)

    _apply_table_styling(ws_pages, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 2b: Page Types (post-crawl classification)
    # ─────────────────────────────────────────────────────────────────────────
    ws_types = wb.create_sheet(title="Page Types")
    ws_types.views.sheetView[0].showGridLines = True
    type_headers = [
        "URL",
        "Page Type",
        "Confidence",
        "Classification Reasons",
        "Location Status",
        "Moved To",
        "Relocated",
        "Status Evidence",
        "HTTP Status",
    ]
    for c_idx, h in enumerate(type_headers, start=1):
        ws_types.cell(row=1, column=c_idx, value=h)
    type_order = {
        "clinic_location": 0, "directory_hub": 1, "clinic_service": 2, "service_page": 3,
        "utility_page": 4, "blog_post": 5, "blog_archive": 6, "other": 7, "": 8,
    }
    t_row = 2
    for p in sorted(page_objs, key=lambda x: (type_order.get(x.page_type, 9), x.url)):
        ws_types.cell(row=t_row, column=1, value=p.url)
        ws_types.cell(row=t_row, column=2, value=p.page_type or "unclassified")
        ws_types.cell(row=t_row, column=3, value=p.page_type_confidence)
        ws_types.cell(row=t_row, column=4, value="; ".join(p.page_type_reasons))
        ws_types.cell(row=t_row, column=5, value=p.location_status)
        ws_types.cell(row=t_row, column=6, value=p.moved_to)
        ws_types.cell(row=t_row, column=7, value="Yes" if p.relocated else "")
        ws_types.cell(row=t_row, column=8, value="; ".join(f"{k}={v}" for k, v in sorted(p.status_evidence.items())))
        ws_types.cell(row=t_row, column=9, value=p.status_code)
        t_row += 1
    _apply_table_styling(ws_types, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 3: SEO Issues
    # ─────────────────────────────────────────────────────────────────────────
    ws_issues = wb.create_sheet(title="SEO Issues")
    ws_issues.views.sheetView[0].showGridLines = True
    issue_headers = ["Page URL", "Severity", "Category", "Issue Description", "Details"]
    for c_idx, h in enumerate(issue_headers, start=1):
        ws_issues.cell(row=1, column=c_idx, value=h)

    cur_row = 2
    for p in page_objs:
        for iss in p.issues:
            ws_issues.cell(row=cur_row, column=1, value=iss.url)
            c_sev = ws_issues.cell(row=cur_row, column=2, value=iss.severity.capitalize())
            _style_status_cell(c_sev, iss.severity)
            ws_issues.cell(row=cur_row, column=3, value=iss.category)
            ws_issues.cell(row=cur_row, column=4, value=iss.issue)
            ws_issues.cell(row=cur_row, column=5, value=iss.details)
            cur_row += 1

    _apply_table_styling(ws_issues, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 4: Headings
    # ─────────────────────────────────────────────────────────────────────────
    ws_headings = wb.create_sheet(title="Headings")
    ws_headings.views.sheetView[0].showGridLines = True
    headings_headers = ["Page URL", "Tag Level", "DOM Position", "Heading Text", "Character Length"]
    for c_idx, h in enumerate(headings_headers, start=1):
        ws_headings.cell(row=1, column=c_idx, value=h)

    h_row = 2
    for p in page_objs:
        pos = 1
        for h_text in p.h1:
            ws_headings.cell(row=h_row, column=1, value=p.url)
            c_tag = ws_headings.cell(row=h_row, column=2, value="H1")
            c_tag.font = BOLD_FONT
            ws_headings.cell(row=h_row, column=3, value=pos)
            ws_headings.cell(row=h_row, column=4, value=h_text)
            ws_headings.cell(row=h_row, column=5, value=len(h_text))
            pos += 1
            h_row += 1

        for h_text in p.h2:
            ws_headings.cell(row=h_row, column=1, value=p.url)
            ws_headings.cell(row=h_row, column=2, value="H2")
            ws_headings.cell(row=h_row, column=3, value=pos)
            ws_headings.cell(row=h_row, column=4, value=h_text)
            ws_headings.cell(row=h_row, column=5, value=len(h_text))
            pos += 1
            h_row += 1

        for h_text in p.h3:
            ws_headings.cell(row=h_row, column=1, value=p.url)
            ws_headings.cell(row=h_row, column=2, value="H3")
            ws_headings.cell(row=h_row, column=3, value=pos)
            ws_headings.cell(row=h_row, column=4, value=h_text)
            ws_headings.cell(row=h_row, column=5, value=len(h_text))
            pos += 1
            h_row += 1

    _apply_table_styling(ws_headings, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 5: Internal Links & Broken
    # ─────────────────────────────────────────────────────────────────────────
    ws_links = wb.create_sheet(title="Internal Links & Broken")
    ws_links.views.sheetView[0].showGridLines = True
    links_headers = [
        "Source URL",
        "Target URL",
        "Link Classification",
        "Anchor Text",
        "DOM Placement",
        "Nofollow",
        "Target HTTP Status",
        "Target Error Type",
        "Malformed Relative Href",
        "Intended Host",
    ]
    for c_idx, h in enumerate(links_headers, start=1):
        ws_links.cell(row=1, column=c_idx, value=h)

    l_row = 2
    seen_links: set[tuple[str, str]] = set()

    # Ingest explicit link_edges if provided from storage
    if link_edges:
        for edge in link_edges:
            src = edge.get("source_url", "")
            tgt = edge.get("target_url", "")
            seen_links.add((src, tgt))
            ws_links.cell(row=l_row, column=1, value=src)
            ws_links.cell(row=l_row, column=2, value=tgt)
            st = edge.get("target_status")
            err_type = edge.get("target_error_type") or ""
            is_broken = is_broken_target(st, err_type)
            if is_broken:
                link_class = "Broken Link"
            elif edge.get("malformed_relative"):
                link_class = "Malformed Relative Link"
            elif st in (301, 302, 303, 307, 308):
                link_class = "Link to Redirect"
            else:
                link_class = "Internal Link"
            c_class = ws_links.cell(row=l_row, column=3, value=link_class)
            if is_broken:
                _style_status_cell(c_class, "error")

            ws_links.cell(row=l_row, column=4, value=edge.get("anchor_text", ""))
            ws_links.cell(row=l_row, column=5, value=edge.get("placement", "body"))
            ws_links.cell(row=l_row, column=6, value="Yes" if edge.get("is_nofollow") else "No")
            c_t_stat = ws_links.cell(row=l_row, column=7, value=st if st is not None else "")
            if is_broken:
                _style_status_cell(c_t_stat, st if st else "error")
            ws_links.cell(row=l_row, column=8, value=err_type)
            ws_links.cell(row=l_row, column=9, value=edge.get("raw_href", "") if edge.get("malformed_relative") else "")
            ws_links.cell(row=l_row, column=10, value=edge.get("intended_host", ""))
            l_row += 1

    # Also make sure broken outlinks from pages are included
    for p in page_objs:
        detail_by_target = {d.get("target_url"): d for d in p.broken_outlink_details}
        for broken_url in p.broken_outlinks:
            if (p.url, broken_url) not in seen_links:
                detail = detail_by_target.get(broken_url, {})
                ws_links.cell(row=l_row, column=1, value=p.url)
                ws_links.cell(row=l_row, column=2, value=broken_url)
                c_class = ws_links.cell(row=l_row, column=3, value="Broken Link")
                _style_status_cell(c_class, "error")
                ws_links.cell(row=l_row, column=4, value="—")
                ws_links.cell(row=l_row, column=5, value="body")
                ws_links.cell(row=l_row, column=6, value="No")
                c_st = ws_links.cell(row=l_row, column=7, value=detail.get("target_status", ">= 400"))
                _style_status_cell(c_st, "error")
                ws_links.cell(row=l_row, column=8, value=detail.get("error_type", ""))
                seen_links.add((p.url, broken_url))
                l_row += 1

    _apply_table_styling(ws_links, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 6: Images & Media
    # ─────────────────────────────────────────────────────────────────────────
    ws_images = wb.create_sheet(title="Images & Media")
    ws_images.views.sheetView[0].showGridLines = True
    img_headers = [
        "Page URL",
        "Total Images",
        "Missing Alt Count",
        "Decorative Alt Count",
        "Missing Dimensions Count",
        "WebP Count",
        "AVIF Count",
        "JPG Count",
        "PNG Count",
        "SVG Count",
        "Sample Missing Alt Image URL",
    ]
    for c_idx, h in enumerate(img_headers, start=1):
        ws_images.cell(row=1, column=c_idx, value=h)

    im_row = 2
    for p in page_objs:
        img_info = p.images
        sample_urls = img_info.missing_alt_sample_urls or [""]
        for sample_img in sample_urls:
            ws_images.cell(row=im_row, column=1, value=p.url)
            ws_images.cell(row=im_row, column=2, value=img_info.total_images)
            c_missing = ws_images.cell(row=im_row, column=3, value=img_info.missing_alt_count)
            if img_info.missing_alt_count > 0:
                _style_status_cell(c_missing, "warning")
            ws_images.cell(row=im_row, column=4, value=img_info.decorative_alt_count)
            ws_images.cell(row=im_row, column=5, value=img_info.missing_dimensions_count)
            ws_images.cell(row=im_row, column=6, value=img_info.webp_count)
            ws_images.cell(row=im_row, column=7, value=img_info.avif_count)
            ws_images.cell(row=im_row, column=8, value=img_info.jpg_count)
            ws_images.cell(row=im_row, column=9, value=img_info.png_count)
            ws_images.cell(row=im_row, column=10, value=img_info.svg_count)
            ws_images.cell(row=im_row, column=11, value=sample_img)
            im_row += 1

    _apply_table_styling(ws_images, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 7: Structured Data & NAP
    # ─────────────────────────────────────────────────────────────────────────
    ws_schema = wb.create_sheet(title="Structured Data & NAP")
    ws_schema.views.sheetView[0].showGridLines = True
    schema_headers = ["Page URL", "Item Category", "Detected Value / Type", "Extra Context"]
    for c_idx, h in enumerate(schema_headers, start=1):
        ws_schema.cell(row=1, column=c_idx, value=h)

    s_row = 2
    for p in page_objs:
        # Schema Types
        for stype in p.schema_types:
            ws_schema.cell(row=s_row, column=1, value=p.url)
            ws_schema.cell(row=s_row, column=2, value="Schema.org Type")
            ws_schema.cell(row=s_row, column=3, value=stype)
            ws_schema.cell(row=s_row, column=4, value="JSON-LD / Microdata")
            s_row += 1

        # Found NAP: Phones
        for phone in p.nap_found.phones:
            ws_schema.cell(row=s_row, column=1, value=p.url)
            ws_schema.cell(row=s_row, column=2, value="Found Phone")
            ws_schema.cell(row=s_row, column=3, value=phone)
            ws_schema.cell(row=s_row, column=4, value="DOM tel: link or regex match")
            s_row += 1

        # Found NAP: Tel Links
        for tel_l in getattr(p.nap_found, "tel_links", []):
            ws_schema.cell(row=s_row, column=1, value=p.url)
            ws_schema.cell(row=s_row, column=2, value="Tel Link")
            ws_schema.cell(row=s_row, column=3, value=tel_l)
            ws_schema.cell(row=s_row, column=4, value="HTML <a href='tel:...'>")
            s_row += 1

        # Found NAP: Addresses
        for addr in p.nap_found.addresses:
            ws_schema.cell(row=s_row, column=1, value=p.url)
            ws_schema.cell(row=s_row, column=2, value="Found Address")
            ws_schema.cell(row=s_row, column=3, value=addr)
            ws_schema.cell(row=s_row, column=4, value="DOM address tag")
            s_row += 1

        # Found NAP: Maps links & embed coords
        for map_l in getattr(p.nap_found, "map_links", []):
            ws_schema.cell(row=s_row, column=1, value=p.url)
            ws_schema.cell(row=s_row, column=2, value="Map Link / Embed")
            ws_schema.cell(row=s_row, column=3, value=map_l)
            ws_schema.cell(row=s_row, column=4, value="Google Maps Link or iframe")
            s_row += 1

        for coord in getattr(p.nap_found, "map_embed_coords", []):
            ws_schema.cell(row=s_row, column=1, value=p.url)
            ws_schema.cell(row=s_row, column=2, value="Map Embed Coords")
            ws_schema.cell(row=s_row, column=3, value=coord)
            ws_schema.cell(row=s_row, column=4, value="Extracted Lat,Lng")
            s_row += 1

        # Hreflang
        for hl in p.hreflang:
            ws_schema.cell(row=s_row, column=1, value=p.url)
            ws_schema.cell(row=s_row, column=2, value=f"Hreflang ({hl.get('lang', '')})")
            ws_schema.cell(row=s_row, column=3, value=hl.get("url", ""))
            ws_schema.cell(row=s_row, column=4, value="Alternate language URL")
            s_row += 1

    _apply_table_styling(ws_schema, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 8: Social & Analytics
    # ─────────────────────────────────────────────────────────────────────────
    ws_social = wb.create_sheet(title="Social & Analytics")
    ws_social.views.sheetView[0].showGridLines = True
    social_headers = [
        "Page URL",
        "OG Title",
        "OG Type",
        "OG Image",
        "OG URL",
        "Twitter Card",
        "Twitter Title",
        "GA4 Measurement ID",
        "GTM Container ID",
        "Google Analytics",
        "Facebook Pixel",
        "Microsoft Clarity",
        "Hotjar",
        "CallRail",
        "WhatConverts",
        "ServiceTitan",
        "Housecall Pro",
        "Podium",
        "Birdeye",
    ]
    for c_idx, h in enumerate(social_headers, start=1):
        ws_social.cell(row=1, column=c_idx, value=h)

    soc_row = 2
    for p in page_objs:
        a = p.analytics
        og = p.og_tags
        tw = p.twitter_tags

        ws_social.cell(row=soc_row, column=1, value=p.url)
        ws_social.cell(row=soc_row, column=2, value=og.get("title") or og.get("og:title", ""))
        ws_social.cell(row=soc_row, column=3, value=og.get("type") or og.get("og:type", ""))
        ws_social.cell(row=soc_row, column=4, value=og.get("image") or og.get("og:image", ""))
        ws_social.cell(row=soc_row, column=5, value=og.get("url") or og.get("og:url", ""))
        ws_social.cell(row=soc_row, column=6, value=tw.get("card") or tw.get("twitter:card", ""))
        ws_social.cell(row=soc_row, column=7, value=tw.get("title") or tw.get("twitter:title", ""))

        ws_social.cell(row=soc_row, column=8, value=a.ga4_id)
        ws_social.cell(row=soc_row, column=9, value=a.gtm_id)
        ws_social.cell(row=soc_row, column=10, value="Yes" if (a.google_analytics or a.ga4_id) else "No")
        ws_social.cell(row=soc_row, column=11, value="Yes" if a.facebook_pixel else "No")
        ws_social.cell(row=soc_row, column=12, value="Yes" if a.microsoft_clarity else "No")
        ws_social.cell(row=soc_row, column=13, value="Yes" if a.hotjar else "No")
        ws_social.cell(row=soc_row, column=14, value="Yes" if a.callrail else "No")
        ws_social.cell(row=soc_row, column=15, value="Yes" if a.whatconverts else "No")
        ws_social.cell(row=soc_row, column=16, value="Yes" if a.servicetitan else "No")
        ws_social.cell(row=soc_row, column=17, value="Yes" if a.housecall_pro else "No")
        ws_social.cell(row=soc_row, column=18, value="Yes" if a.podium else "No")
        ws_social.cell(row=soc_row, column=19, value="Yes" if a.birdeye else "No")
        soc_row += 1

    _apply_table_styling(ws_social, start_row=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Sheet 9: Evidence (EVD-01)
    # ─────────────────────────────────────────────────────────────────────────
    ws_ev = wb.create_sheet(title="Evidence (EVD-01)")
    ws_ev.views.sheetView[0].showGridLines = True
    ev_headers = [
        "Source URL",
        "Claim Type",
        "HTTP Status",
        "DOM Selector",
        "Verbatim Text Excerpt",
        "SHA-256 Proof Hash",
        "Timestamp",
    ]
    for c_idx, h in enumerate(ev_headers, start=1):
        ws_ev.cell(row=1, column=c_idx, value=h)

    ev_r = 2
    for ev in evidence_objs:
        ws_ev.cell(row=ev_r, column=1, value=ev.source_url)
        ws_ev.cell(row=ev_r, column=2, value=ev.claim_type)
        c_st = ws_ev.cell(row=ev_r, column=3, value=ev.http_status)
        _style_status_cell(c_st, ev.http_status)
        ws_ev.cell(row=ev_r, column=4, value=ev.dom_selector)
        ws_ev.cell(row=ev_r, column=5, value=ev.verbatim_text)
        ws_ev.cell(row=ev_r, column=6, value=ev.hash_sha256)
        ws_ev.cell(row=ev_r, column=7, value=ev.timestamp)
        ev_r += 1

    _apply_table_styling(ws_ev, start_row=1)

    # Save workbook
    out_path = Path(file_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path.resolve()))
    return str(out_path.resolve())


def write_citations_excel(
    file_path: Path | str,
    business_name: str,
    citations: list[CitationItem | dict[str, Any]],
    evidence_records: list[EvidenceRecord | dict[str, Any]] | None = None,
) -> str:
    """Generate structured multi-worksheet Excel workbook for citation verification."""
    wb = openpyxl.Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)

    business_name = _sanitize_data(business_name)
    citations = _sanitize_data(citations)
    evidence_records = _sanitize_data(evidence_records)

    citation_objs = [c if isinstance(c, CitationItem) else CitationItem(**c) for c in citations]
    evidence_objs = [
        e if isinstance(e, EvidenceRecord) else EvidenceRecord(**e)
        for e in (evidence_records or [])
    ]

    # Sheet 1: Summary
    ws_sum = wb.create_sheet(title="Summary")
    ws_sum.views.sheetView[0].showGridLines = True
    ws_sum.cell(row=1, column=1, value=f"Local Citation Audit Summary — {business_name}").font = TITLE_FONT
    ws_sum.cell(row=2, column=1, value="Forensic citation directory verification per rule CIT-01").font = SUBTITLE_FONT

    headers_kpi = ["Citation Metric", "Count", "Description"]
    for c_idx, h in enumerate(headers_kpi, start=1):
        cell = ws_sum.cell(row=4, column=c_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER

    total = len(citation_objs)
    live = sum(1 for c in citation_objs if c.status == "live")
    blocked = sum(1 for c in citation_objs if c.status in ("blocked", "captcha"))
    mismatch = sum(1 for c in citation_objs if c.nap_match == "mismatch")
    partial = sum(1 for c in citation_objs if c.nap_match == "partial")
    match = sum(1 for c in citation_objs if c.nap_match == "match")

    kpis = [
        ("Total Directory Citations Checked", total, "Total platforms evaluated"),
        ("Live & Active Listings", live, "Listing confirmed present and responding with 200 OK"),
        ("Blocked / Captcha (Anti-Bot)", blocked, "Protected directory requiring session bypass / FlareSolverr"),
        ("Exact NAP Matches", match, "Name, Address, and Phone match canonical business profile"),
        ("Partial NAP Matches", partial, "Phone or address matches with minor formatting discrepancies"),
        ("NAP Mismatches (Drift)", mismatch, "Conflicting phone numbers or addresses detected"),
    ]

    for r_idx, (m, cnt, desc) in enumerate(kpis, start=5):
        ws_sum.cell(row=r_idx, column=1, value=m).font = BOLD_FONT
        c_cnt = ws_sum.cell(row=r_idx, column=2, value=cnt)
        c_cnt.font = BOLD_FONT
        c_cnt.alignment = Alignment(horizontal="center")
        ws_sum.cell(row=r_idx, column=3, value=desc)
        ws_sum.cell(row=r_idx, column=1).border = THIN_BORDER
        c_cnt.border = THIN_BORDER
        ws_sum.cell(row=r_idx, column=3).border = THIN_BORDER
        if "Mismatches" in m and cnt > 0:
            _style_status_cell(c_cnt, "error")
        elif "Blocked" in m and cnt > 0:
            _style_status_cell(c_cnt, "warning")
        elif "Live" in m or "Exact" in m:
            _style_status_cell(c_cnt, "ok")

    ws_sum.column_dimensions["A"].width = 38
    ws_sum.column_dimensions["B"].width = 15
    ws_sum.column_dimensions["C"].width = 55

    # Sheet 2: Citations Overview
    ws_cits = wb.create_sheet(title="Citations Overview")
    ws_cits.views.sheetView[0].showGridLines = True
    cit_headers = [
        "Platform",
        "Directory Label",
        "Listing URL",
        "Listing Status",
        "NAP Match",
        "Confidence",
        "Observed Business Name",
        "Observed Address",
        "Observed Phone",
        "Website URL",
        "Operating Hours",
        "Proxy Port",
        "Evidence Snippet",
    ]
    for c_idx, h in enumerate(cit_headers, start=1):
        ws_cits.cell(row=1, column=c_idx, value=h)

    for r_idx, c in enumerate(citation_objs, start=2):
        ws_cits.cell(row=r_idx, column=1, value=c.platform)
        ws_cits.cell(row=r_idx, column=2, value=c.label)
        ws_cits.cell(row=r_idx, column=3, value=c.url)
        c_st = ws_cits.cell(row=r_idx, column=4, value=c.status.capitalize())
        _style_status_cell(c_st, c.status)

        c_match = ws_cits.cell(row=r_idx, column=5, value=c.nap_match.capitalize())
        _style_status_cell(c_match, c.nap_match)

        c_conf = ws_cits.cell(row=r_idx, column=6, value=c.confidence.capitalize())
        _style_status_cell(c_conf, "ok" if c.confidence == "high" else "warning")

        obs = c.observed
        ws_cits.cell(row=r_idx, column=7, value=obs.name if obs else "")
        ws_cits.cell(row=r_idx, column=8, value=obs.address if obs else "")
        ws_cits.cell(row=r_idx, column=9, value=obs.phone if obs else "")
        ws_cits.cell(row=r_idx, column=10, value=c.website_url)
        ws_cits.cell(row=r_idx, column=11, value=c.operating_hours)
        ws_cits.cell(row=r_idx, column=12, value=c.proxy_port or "")
        ws_cits.cell(row=r_idx, column=13, value=c.evidence_snippet)

    _apply_table_styling(ws_cits, start_row=1)

    # Sheet 3: Secondary Phones & Badges
    ws_sec = wb.create_sheet(title="Secondary Phones & Badges")
    ws_sec.views.sheetView[0].showGridLines = True
    sec_headers = ["Platform", "Listing URL", "Attribute Type", "Value"]
    for c_idx, h in enumerate(sec_headers, start=1):
        ws_sec.cell(row=1, column=c_idx, value=h)

    sec_r = 2
    for c in citation_objs:
        for ph in c.secondary_phones:
            ws_sec.cell(row=sec_r, column=1, value=c.platform)
            ws_sec.cell(row=sec_r, column=2, value=c.url)
            ws_sec.cell(row=sec_r, column=3, value="Secondary Phone")
            ws_sec.cell(row=sec_r, column=4, value=ph)
            sec_r += 1
        for b in c.badges:
            ws_sec.cell(row=sec_r, column=1, value=c.platform)
            ws_sec.cell(row=sec_r, column=2, value=c.url)
            ws_sec.cell(row=sec_r, column=3, value="Badge / Accreditation")
            ws_sec.cell(row=sec_r, column=4, value=b)
            sec_r += 1

    _apply_table_styling(ws_sec, start_row=1)

    # Sheet 4: Evidence (EVD-01)
    ws_ev = wb.create_sheet(title="Evidence (EVD-01)")
    ws_ev.views.sheetView[0].showGridLines = True
    ev_headers = [
        "Source URL",
        "Claim Type",
        "HTTP Status",
        "DOM Selector",
        "Verbatim Text Excerpt",
        "SHA-256 Proof Hash",
        "Timestamp",
    ]
    for c_idx, h in enumerate(ev_headers, start=1):
        ws_ev.cell(row=1, column=c_idx, value=h)

    ev_r = 2
    for ev in evidence_objs:
        ws_ev.cell(row=ev_r, column=1, value=ev.source_url)
        ws_ev.cell(row=ev_r, column=2, value=ev.claim_type)
        c_st = ws_ev.cell(row=ev_r, column=3, value=ev.http_status)
        _style_status_cell(c_st, ev.http_status)
        ws_ev.cell(row=ev_r, column=4, value=ev.dom_selector)
        ws_ev.cell(row=ev_r, column=5, value=ev.verbatim_text)
        ws_ev.cell(row=ev_r, column=6, value=ev.hash_sha256)
        ws_ev.cell(row=ev_r, column=7, value=ev.timestamp)
        ev_r += 1

    _apply_table_styling(ws_ev, start_row=1)

    out_path = Path(file_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path.resolve()))
    return str(out_path.resolve())


def write_serp_excel(
    file_path: Path | str,
    serp_data: dict[str, Any],
    evidence_records: list[EvidenceRecord | dict[str, Any]] | None = None,
) -> str:
    """Generate structured multi-worksheet Excel workbook for Local SERP & Maps Entity audit."""
    wb = openpyxl.Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)

    serp_data = _sanitize_data(serp_data)
    evidence_records = _sanitize_data(evidence_records)

    evidence_objs = [
        e if isinstance(e, EvidenceRecord) else EvidenceRecord(**e)
        for e in (evidence_records or [])
    ]

    query = serp_data.get("query", "")
    location = serp_data.get("location", "")
    checked_at = serp_data.get("checked_at", "")
    local_pack = serp_data.get("local_pack", [])
    organic = serp_data.get("organic", [])
    paa = serp_data.get("paa") or serp_data.get("people_also_ask", [])

    # Sheet 1: Query Summary
    ws_sum = wb.create_sheet(title="Query Summary")
    ws_sum.views.sheetView[0].showGridLines = True
    ws_sum.cell(row=1, column=1, value="Google Local SERP & Maps Entity Audit").font = TITLE_FONT
    ws_sum.cell(row=2, column=1, value=f"Search Query: {query} | Location: {location}").font = SUBTITLE_FONT

    headers_kpi = ["SERP Element", "Count / Value", "Notes"]
    for c_idx, h in enumerate(headers_kpi, start=1):
        cell = ws_sum.cell(row=4, column=c_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER

    items = [
        ("Query String", query, "Search phrase tested in localized Google SERP"),
        ("Geographic Location", location, "Target metro area / municipality tested"),
        ("Checked At", checked_at, "UTC timestamp of capture"),
        ("Local 3-Pack Results Found", len(local_pack), "Map pack business entities recognized by Google"),
        ("Organic Top 10 Results Found", len(organic), "Organic non-ad landing pages ranking in SERP"),
        ("People Also Ask (PAA) Questions", len(paa), "Related conversational queries returned by Google"),
    ]

    for r_idx, (m, cnt, desc) in enumerate(items, start=5):
        ws_sum.cell(row=r_idx, column=1, value=m).font = BOLD_FONT
        c_cnt = ws_sum.cell(row=r_idx, column=2, value=cnt)
        c_cnt.font = BOLD_FONT
        c_cnt.alignment = Alignment(horizontal="center")
        ws_sum.cell(row=r_idx, column=3, value=desc)
        ws_sum.cell(row=r_idx, column=1).border = THIN_BORDER
        c_cnt.border = THIN_BORDER
        ws_sum.cell(row=r_idx, column=3).border = THIN_BORDER
        if "Found" in m or "Questions" in m:
            _style_status_cell(c_cnt, "ok" if cnt > 0 else "warning")

    ws_sum.column_dimensions["A"].width = 35
    ws_sum.column_dimensions["B"].width = 25
    ws_sum.column_dimensions["C"].width = 50

    # Sheet 2: Local Pack
    ws_pack = wb.create_sheet(title="Local Pack")
    ws_pack.views.sheetView[0].showGridLines = True
    pack_headers = [
        "Rank Position",
        "Business Name",
        "Star Rating",
        "Review Count",
        "Primary Category",
        "Address / Vicinity Snippet",
    ]
    for c_idx, h in enumerate(pack_headers, start=1):
        ws_pack.cell(row=1, column=c_idx, value=h)

    for r_idx, item in enumerate(local_pack, start=2):
        ws_pack.cell(row=r_idx, column=1, value=item.get("position", r_idx - 1))
        ws_pack.cell(row=r_idx, column=2, value=item.get("name", ""))
        c_rt = ws_pack.cell(row=r_idx, column=3, value=item.get("rating") or "")
        if item.get("rating") and float(item["rating"]) >= 4.5:
            _style_status_cell(c_rt, "ok")
        ws_pack.cell(row=r_idx, column=4, value=item.get("review_count") or 0)
        cats = item.get("categories", [])
        ws_pack.cell(row=r_idx, column=5, value=cats[0] if cats else "")
        ws_pack.cell(row=r_idx, column=6, value=item.get("address_snippet", ""))

    _apply_table_styling(ws_pack, start_row=1)

    # Sheet 3: Local Pack Categories
    ws_cats = wb.create_sheet(title="Local Pack Categories")
    ws_cats.views.sheetView[0].showGridLines = True
    cat_headers = ["Rank Position", "Business Name", "Google Maps Category"]
    for c_idx, h in enumerate(cat_headers, start=1):
        ws_cats.cell(row=1, column=c_idx, value=h)

    cat_r = 2
    for item in local_pack:
        pos = item.get("position", 1)
        name = item.get("name", "")
        for cat in item.get("categories", []):
            ws_cats.cell(row=cat_r, column=1, value=pos)
            ws_cats.cell(row=cat_r, column=2, value=name)
            ws_cats.cell(row=cat_r, column=3, value=cat)
            cat_r += 1

    _apply_table_styling(ws_cats, start_row=1)

    # Sheet 4: Organic Rankings
    ws_org = wb.create_sheet(title="Organic Rankings")
    ws_org.views.sheetView[0].showGridLines = True
    org_headers = ["Position", "Ranking URL", "Page Title", "SERP Snippet Description"]
    for c_idx, h in enumerate(org_headers, start=1):
        ws_org.cell(row=1, column=c_idx, value=h)

    for r_idx, org in enumerate(organic, start=2):
        ws_org.cell(row=r_idx, column=1, value=org.get("position", r_idx - 1))
        ws_org.cell(row=r_idx, column=2, value=org.get("url", ""))
        ws_org.cell(row=r_idx, column=3, value=org.get("title", ""))
        ws_org.cell(row=r_idx, column=4, value=org.get("snippet", ""))

    _apply_table_styling(ws_org, start_row=1)

    # Sheet 5: People Also Ask (PAA)
    ws_paa = wb.create_sheet(title="People Also Ask (PAA)")
    ws_paa.views.sheetView[0].showGridLines = True
    paa_headers = ["Position", "Question Query", "Answer Snippet Excerpt"]
    for c_idx, h in enumerate(paa_headers, start=1):
        ws_paa.cell(row=1, column=c_idx, value=h)

    for r_idx, p_item in enumerate(paa, start=2):
        ws_paa.cell(row=r_idx, column=1, value=p_item.get("position", r_idx - 1))
        ws_paa.cell(row=r_idx, column=2, value=p_item.get("question", ""))
        ws_paa.cell(row=r_idx, column=3, value=p_item.get("snippet", ""))

    _apply_table_styling(ws_paa, start_row=1)

    # Sheet 6: Evidence (EVD-01)
    ws_ev = wb.create_sheet(title="Evidence (EVD-01)")
    ws_ev.views.sheetView[0].showGridLines = True
    ev_headers = [
        "Source URL",
        "Claim Type",
        "HTTP Status",
        "DOM Selector",
        "Verbatim Text Excerpt",
        "SHA-256 Proof Hash",
        "Timestamp",
    ]
    for c_idx, h in enumerate(ev_headers, start=1):
        ws_ev.cell(row=1, column=c_idx, value=h)

    ev_r = 2
    for ev in evidence_objs:
        ws_ev.cell(row=ev_r, column=1, value=ev.source_url)
        ws_ev.cell(row=ev_r, column=2, value=ev.claim_type)
        c_st = ws_ev.cell(row=ev_r, column=3, value=ev.http_status)
        _style_status_cell(c_st, ev.http_status)
        ws_ev.cell(row=ev_r, column=4, value=ev.dom_selector)
        ws_ev.cell(row=ev_r, column=5, value=ev.verbatim_text)
        ws_ev.cell(row=ev_r, column=6, value=ev.hash_sha256)
        ws_ev.cell(row=ev_r, column=7, value=ev.timestamp)
        ev_r += 1

    _apply_table_styling(ws_ev, start_row=1)

    out_path = Path(file_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path.resolve()))
    return str(out_path.resolve())

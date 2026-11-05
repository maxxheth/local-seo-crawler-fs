"""Deterministic report generation from a crawl database (WS3).

`--mode report --db <crawl_results.db>` reads an existing SQLite crawl database (opened
read-only; it never crawls or migrates) and renders:

- a Markdown report from a fixed template with conditional sections, and
- report_summary.json carrying exactly the numbers the Markdown shows.

Every number and URL in the report comes from a query result; nothing is free text.
If something wasn't measured, the report says "not measured". Stack statements come
only from observed response headers and generator meta tags.

`--verify-narrative <md>` checks an externally written narrative (e.g. AI commentary)
against report_summary.json and fails on contradictions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
from typing import Any
from urllib.parse import urlparse

from src.classifier import LOCAL_SCHEMA_TYPES, clinic_detection_method
from src.storage import is_broken_target

SEVERITY_ORDER = {"critical": 0, "error": 0, "warning": 1, "info": 2}
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
REDIRECT_PATTERN_LABELS = {
    "slash_normalisation": "Trailing-slash normalisation",
    "case_normalisation": "Case normalisation",
    "scheme": "Scheme change (http/https)",
    "host_normalisation": "Host normalisation (www / apex)",
    "cross_domain": "Cross-domain",
    "content": "Content redirect (different path)",
    "unknown": "Unknown destination",
}
EXCEL_SHEETS = {
    "issues": "SEO Issues",
    "links": "Internal Links & Broken",
    "pages": "Pages Overview",
    "page_types": "Page Types",
    "schema": "Structured Data & NAP",
}
CRAWL_TIMING_NOTE = "reflect crawler concurrency and network path, not user experience"


# ── Data access ──────────────────────────────────────────────────────────────

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM pages LIMIT 1")
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _meta(conn: sqlite3.Connection, key: str) -> Any:
    if not _columns(conn, "crawl_metadata"):
        return None
    row = conn.execute("SELECT value FROM crawl_metadata WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def _key(url: str) -> str:
    return (url or "").rstrip("/")


def redirect_pattern(source: str, final: str) -> str:
    """Classify a redirect by what changes between source and destination."""
    if not final:
        return "unknown"
    ps, pf = urlparse(source), urlparse(final)
    hs, hf = ps.netloc.lower(), pf.netloc.lower()
    if hs != hf:
        strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
        return "host_normalisation" if strip(hs) == strip(hf) else "cross_domain"
    if ps.scheme.lower() != pf.scheme.lower():
        return "scheme"
    if ps.path.rstrip("/") == pf.path.rstrip("/"):
        return "slash_normalisation"
    if ps.path.rstrip("/").lower() == pf.path.rstrip("/").lower():
        return "case_normalisation"
    return "content"


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = int(math.ceil(pct / 100.0 * len(ordered))) - 1
    return float(ordered[max(0, min(idx, len(ordered) - 1))])


def _timing_stats(values: list[int]) -> dict[str, Any]:
    vals = [float(v) for v in values if v and v > 0]
    if not vals:
        return {"measured": False}
    return {
        "measured": True,
        "pages": len(vals),
        "mean_ms": round(sum(vals) / len(vals), 1),
        "median_ms": round(float(statistics.median(vals)), 1),
        "p95_ms": round(_percentile(vals, 95), 1),
    }


# ── Aggregation ──────────────────────────────────────────────────────────────

def build_report_data(db_path: str | Path, sample_limit: int = 10) -> dict[str, Any]:
    """Aggregate the crawl database into the report data model (== report_summary.json)."""
    db_path = Path(db_path)
    conn = _connect_ro(db_path)
    try:
        return _build(conn, db_path, sample_limit)
    finally:
        conn.close()


def _build(conn: sqlite3.Connection, db_path: Path, n: int) -> dict[str, Any]:
    page_cols = _columns(conn, "pages")
    link_cols = _columns(conn, "internal_links")
    pages = [json.loads(r["raw_json"]) for r in conn.execute("SELECT raw_json FROM pages ORDER BY url")]
    by_key = {_key(p.get("url", "")): p for p in pages}

    domain = _meta(conn, "domain") or ""
    if not domain and "domain" in page_cols:
        row = conn.execute(
            "SELECT domain, COUNT(*) c FROM pages WHERE domain != '' GROUP BY domain ORDER BY c DESC, domain LIMIT 1"
        ).fetchone()
        domain = row["domain"] if row else ""
    if not domain and pages:
        domain = urlparse(pages[0].get("url", "")).netloc
    crawl_date = _meta(conn, "crawl_date") or ""
    if not crawl_date and "created_at" in page_cols:
        row = conn.execute("SELECT MAX(created_at) m FROM pages").fetchone()
        crawl_date = (row["m"] or "")[:10] if row else ""

    data: dict[str, Any] = {
        "report_version": 1,
        "domain": domain,
        "crawl_date": crawl_date or "unknown",
        "database": db_path.name,
        "sample_limit": n,
        "excel_sheets": EXCEL_SHEETS,
    }

    # Totals & status distribution
    status_counts = Counter(int(p.get("status_code") or 0) for p in pages)
    data["totals"] = {
        "pages_crawled": len(pages),
        "status_distribution": [{"status": s, "pages": c} for s, c in sorted(status_counts.items())],
    }

    # Issues
    issue_rows = conn.execute("SELECT url, severity, category, issue FROM seo_issues").fetchall()
    sev_counts = Counter()
    cat_counts = Counter()
    type_rows: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for r in issue_rows:
        sev = "error" if r["severity"] == "critical" else r["severity"]
        sev_counts[sev] += 1
        cat_counts[r["category"]] += 1
        type_rows[(r["issue"], sev, r["category"])].append(r["url"])
    by_type = []
    for (issue, sev, cat), urls in type_rows.items():
        distinct = sorted(set(urls))
        by_type.append({
            "issue": issue,
            "severity": sev,
            "category": cat,
            "occurrences": len(urls),
            "pages": len(distinct),
            "sample_urls": distinct[:n],
        })
    by_type.sort(key=lambda t: (SEVERITY_ORDER.get(t["severity"], 3), -t["occurrences"], t["issue"]))
    data["issues"] = {
        "total": len(issue_rows),
        "errors": sev_counts.get("error", 0),
        "warnings": sev_counts.get("warning", 0),
        "info": sev_counts.get("info", 0),
        "by_category": [{"category": c, "occurrences": k} for c, k in sorted(cat_counts.items(), key=lambda x: (-x[1], x[0]))],
        "by_type": by_type,
    }

    # Link rows
    has_malformed = "malformed_relative" in link_cols
    def col(name: str, expr: str, fallback: str) -> str:
        return expr if name in link_cols else fallback

    select_cols = ", ".join([
        "source_url",
        "target_url",
        "target_status",
        col("target_error_type", "COALESCE(target_error_type, '')", "''") + " AS error_type",
        col("malformed_relative", "COALESCE(malformed_relative, 0)", "0") + " AS malformed",
        col("raw_href", "COALESCE(raw_href, '')", "''") + " AS raw_href",
        col("intended_host", "COALESCE(intended_host, '')", "''") + " AS intended_host",
    ])
    has_malformed = "malformed_relative" in link_cols
    links = [
        dict(r)
        for r in conn.execute(f"SELECT {select_cols} FROM internal_links ORDER BY source_url, target_url")
    ]

    # Broken targets (HTTP >= 400 or transport failure)
    broken: dict[str, dict[str, Any]] = {}
    for l in links:
        st = l["target_status"]
        err = l["error_type"] or ""
        if st is None and not err:
            continue
        if not is_broken_target(st, err):
            continue
        b = broken.setdefault(l["target_url"], {"status": st, "error_type": err, "sources": set()})
        b["sources"].add(l["source_url"])
    broken_list = []
    for t in sorted(broken, key=lambda t: (-len(broken[t]["sources"]), t)):
        b = broken[t]
        srcs = sorted(b["sources"])
        broken_list.append({
            "target_url": t,
            "status": b["status"] if b["status"] else 0,
            "error_type": b["error_type"],
            "linking_pages": len(srcs),
            "sample_sources": srcs[:n],
        })
    err_counts = Counter(b["error_type"] or f"http_{b['status']}" for b in broken_list)

    # Crawled URLs that are broken but have no internal links (e.g. discovered via the XML sitemap)
    linked_keys = {_key(t) for t in broken} | {_key(l["target_url"]) for l in links}
    unlinked = []
    for p in pages:
        st = int(p.get("status_code") or 0)
        err = p.get("transport_error_type") or ("other" if p.get("transport_error") and st != 200 else "")
        if st == 200 or not is_broken_target(st, err):
            continue
        if _key(p.get("url", "")) in linked_keys:
            continue
        unlinked.append({
            "url": p["url"],
            "status": st,
            "error_type": err,
            "final_url": (p.get("final_url") or "") if err else "",
            "in_sitemap": bool(p.get("in_sitemap")),
        })
    data["broken_links"] = {
        "targets": len(broken_list),
        "linking_pages": len({s for b in broken.values() for s in b["sources"]}),
        "by_error_type": [{"error_type": k, "targets": v} for k, v in sorted(err_counts.items())],
        "items": broken_list,
        "unlinked_broken_urls": unlinked,
    }

    # Malformed relative links
    if has_malformed:
        seen = set()
        items = []
        for l in links:
            if not l["malformed"] or (l["source_url"], l["target_url"]) in seen:
                continue
            seen.add((l["source_url"], l["target_url"]))
            st, err = l["target_status"], l["error_type"]
            is_broken = is_broken_target(st, err)
            if err:
                outcome = f"transport failure ({err})"
            elif not st:
                outcome = "not verified"
            else:
                outcome = f"HTTP {st}"
            items.append({
                "source_url": l["source_url"],
                "raw_href": l["raw_href"],
                "resolved_url": l["target_url"],
                "intended_host": l["intended_host"],
                "outcome": outcome,
                "severity": "error" if is_broken else "warning",
            })
        items.sort(key=lambda m: (SEVERITY_ORDER[m["severity"]], m["source_url"], m["resolved_url"]))
        data["malformed_links"] = {
            "available": True,
            "total": len(items),
            "errors": sum(1 for m in items if m["severity"] == "error"),
            "warnings": sum(1 for m in items if m["severity"] == "warning"),
            "items": items,
        }
    else:
        data["malformed_links"] = {"available": False}

    # Redirecting pages grouped by pattern
    redirect_pages = [p for p in pages if int(p.get("status_code") or 0) in REDIRECT_STATUSES]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in redirect_pages:
        groups[redirect_pattern(p["url"], p.get("final_url") or "")].append(p)
    data["redirects"] = {
        "pages": len(redirect_pages),
        "by_pattern": [
            {
                "pattern": pat,
                "label": REDIRECT_PATTERN_LABELS[pat],
                "pages": len(ps),
                "samples": [{"url": p["url"], "final_url": p.get("final_url") or ""} for p in sorted(ps, key=lambda x: x["url"])[:n]],
            }
            for pat, ps in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
    }

    # Internal links to redirects (WS7)
    redirect_targets: dict[str, set[str]] = defaultdict(set)
    for l in links:
        if l["target_status"] in REDIRECT_STATUSES and not l["malformed"] and not l["error_type"]:
            redirect_targets[l["target_url"]].add(l["source_url"])
    slash_targets, content_targets = [], []
    by_url = {p.get("url", ""): p for p in pages}
    for t, srcs in redirect_targets.items():
        tp = by_url.get(t) or by_key.get(_key(t)) or {}
        final = tp.get("final_url") or ""
        entry = {"target_url": t, "final_url": final, "linking_pages": len(srcs)}
        (slash_targets if redirect_pattern(t, final) == "slash_normalisation" else content_targets).append(entry)
    order = lambda e: (-e["linking_pages"], e["target_url"])  # noqa: E731
    slash_targets.sort(key=order)
    content_targets.sort(key=order)
    data["links_to_redirects"] = {
        "targets": len(redirect_targets),
        "links": sum(len(s) for s in redirect_targets.values()),
        "slash_normalisation": {
            "targets": len(slash_targets),
            "links": sum(e["linking_pages"] for e in slash_targets),
            "items": slash_targets[:n],
        },
        "other": {
            "targets": len(content_targets),
            "links": sum(e["linking_pages"] for e in content_targets),
            "items": content_targets[:n],
        },
    }

    # Duplicate clusters (connected components of duplicate edges)
    data["duplicates"] = _duplicate_clusters(conn, n)

    # Page classification (WS4)
    has_types = "page_type" in page_cols and any(p.get("page_type") for p in pages)
    if has_types:
        type_counts = Counter(p.get("page_type") or "unclassified" for p in pages)
        coverage = []
        for t in sorted(type_counts):
            ok = [p for p in pages if (p.get("page_type") or "unclassified") == t and int(p.get("status_code") or 0) == 200]
            coverage.append({
                "page_type": t,
                "pages": type_counts[t],
                "ok_pages": len(ok),
                "with_local_schema": sum(1 for p in ok if set(p.get("schema_types") or []) & set(LOCAL_SCHEMA_TYPES)),
                "with_any_schema": sum(1 for p in ok if p.get("schema_types")),
            })
        clinics = sorted((p for p in pages if p.get("page_type") == "clinic_location"), key=lambda p: p["url"])
        status_counts_c = Counter(p.get("location_status") or "unknown" for p in clinics)
        data["page_types"] = {
            "available": True,
            "coverage": coverage,
            "directory_hubs": sorted(p["url"] for p in pages if p.get("page_type") == "directory_hub"),
        }
        data["clinics"] = {
            "available": True,
            "detection": clinic_detection_method(pages),
            "total": len(clinics),
            "by_status": [{"status": s, "pages": c} for s, c in sorted(status_counts_c.items())],
            "non_open": [
                {
                    "url": p["url"],
                    "status": p.get("location_status") or "unknown",
                    "moved_to": p.get("moved_to") or "",
                    "relocated": bool(p.get("relocated")),
                    "evidence_source": (p.get("status_evidence") or {}).get("source", ""),
                }
                for p in clinics
                if (p.get("location_status") or "unknown") != "open" or p.get("relocated")
            ],
        }
    else:
        data["page_types"] = {"available": False}
        data["clinics"] = {"available": False}

    # Prototype / staging pages
    proto_names = ("Indexable Prototype / Staging Page", "Prototype / Staging Page")
    staging_name = "Staging Hostname Reference"
    data["prototype_pages"] = {
        "pages": len({r["url"] for r in issue_rows if r["issue"] in proto_names}),
        "staging_reference_pages": len({r["url"] for r in issue_rows if r["issue"] == staging_name}),
        "items": [
            {"url": r["url"], "issue": r["issue"], "severity": "error" if r["severity"] == "critical" else r["severity"]}
            for r in sorted(issue_rows, key=lambda r: (r["issue"] == staging_name, r["url"], r["issue"]))
            if r["issue"] in proto_names or r["issue"] == staging_name
        ],
    }

    # Observed stack
    def value_counts(values: list[str]) -> list[dict[str, Any]]:
        c = Counter(v for v in values if v)
        return [{"value": v, "pages": k} for v, k in sorted(c.items(), key=lambda x: (-x[1], x[0]))]

    cache_headers = Counter()
    for p in pages:
        cs = p.get("cache_status") or ""
        if cs:
            cache_headers[cs.split(":")[0].strip()] += 1
    data["stack"] = {
        "server_headers": value_counts([p.get("server_header") or "" for p in pages]),
        "cache_headers": [{"value": h, "pages": c} for h, c in sorted(cache_headers.items(), key=lambda x: (-x[1], x[0]))],
        "cache_statuses": value_counts([p.get("cache_status") or "" for p in pages])[:n],
        "generators": value_counts([p.get("generator") or "" for p in pages]),
    }

    # Timing
    timing_sample = _meta(conn, "timing_sample")
    psi = _meta(conn, "psi")
    data["performance"] = {
        "crawler_response_time": _timing_stats([int(p.get("response_time_ms") or 0) for p in pages]),
        "crawler_ttfb": _timing_stats([int(p.get("ttfb_ms") or 0) for p in pages]),
        "crawler_note": CRAWL_TIMING_NOTE,
        "sequential_sample": (
            {
                "measured": True,
                "sample_size": timing_sample.get("sample_size", 0),
                "measured_urls": timing_sample.get("measured", 0),
                "median_ttfb_ms": timing_sample.get("median_ttfb_ms"),
                "p95_ttfb_ms": timing_sample.get("p95_ttfb_ms"),
            }
            if timing_sample and timing_sample.get("measured")
            else {"measured": False}
        ),
        "real_user": _psi_summary(psi),
    }
    return data


def _psi_summary(psi: dict[str, Any] | None) -> dict[str, Any]:
    if not psi or not psi.get("measured"):
        return {"measured": False}
    origin = psi.get("origin") or {}
    return {
        "measured": True,
        "strategy": psi.get("strategy", "mobile"),
        "origin": [
            {"metric": m, "p75": origin[m]["p75"], "category": origin[m].get("category", "")}
            for m in ("ttfb_ms", "lcp_ms", "inp_ms", "cls")
            if m in origin
        ],
        "urls_with_field_data": sum(1 for u in psi.get("urls", []) if u.get("metrics")),
        "urls_requested": len(psi.get("urls", [])) + len(psi.get("errors", [])),
    }


def _duplicate_clusters(conn: sqlite3.Connection, n: int) -> dict[str, Any]:
    pairs: list[tuple[str, str]] = []
    source = "duplicate_pairs"
    if _columns(conn, "duplicate_pairs"):
        # Table exists: an empty table means "no duplicates", not "unknown"
        pairs = [(r["url_a"], r["url_b"]) for r in conn.execute("SELECT url_a, url_b FROM duplicate_pairs")]
    else:
        # Older crawls: rebuild edges from issue details (may list only the closest partners)
        source = "issue_details"
        for r in conn.execute("SELECT url, details FROM seo_issues WHERE issue = 'Duplicate Content Detected'"):
            for other in re.findall(r"(https?://\S+?) \(", r["details"] or ""):
                pairs.append((r["url"], other))
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    comps: dict[str, list[str]] = defaultdict(list)
    for x in list(parent):
        comps[find(x)].append(x)
    clusters = sorted((sorted(c) for c in comps.values() if len(c) >= 2), key=lambda c: (-len(c), c[0]))
    return {
        "source": source,
        "clusters": len(clusters),
        "pages": sum(len(c) for c in clusters),
        "items": [
            {"cluster": i, "size": len(c), "urls": c, "more": max(0, len(c) - n)}
            for i, c in enumerate(clusters, start=1)
        ],
    }


# ── Rendering ────────────────────────────────────────────────────────────────

def _code(text: Any) -> str:
    s = str(text).replace("`", "'").replace("|", "\\|")
    return f"`{s}`" if s else "—"


def _u(url: str, domain: str) -> str:
    """Display URL: path for same-site URLs, full URL otherwise; always in code span."""
    p = urlparse(url or "")
    if p.netloc and p.netloc.lower() == (domain or "").lower():
        disp = p.path + (f"?{p.query}" if p.query else "")
    else:
        disp = url
    return _code(disp or url)


def _num(v: Any) -> str:
    return str(v)


def render_markdown(data: dict[str, Any]) -> str:
    d = data["domain"]
    n = data["sample_limit"]
    sheets = data["excel_sheets"]
    L: list[str] = []
    w = L.append

    w(f"# Local SEO Crawl Report: {_code(d)}")
    w("")
    w(f"- Crawl date: {_code(data['crawl_date'])}")
    w(f"- Database: {_code(data['database'])}")
    w(f"- Sample URLs per finding: {n}")
    w("- Generated from the crawl database. Every number and URL below is a query result; "
      "anything that wasn't measured is reported as \"not measured\".")
    w("")

    t, iss = data["totals"], data["issues"]
    w("## Summary")
    w("")
    w(f"- Pages crawled: {t['pages_crawled']}")
    w(f"- Issues: {iss['total']} total ({iss['errors']} errors, {iss['warnings']} warnings, {iss['info']} info)")
    bl = data["broken_links"]
    w(f"- Broken internal link targets: {bl['targets']} (linked from {bl['linking_pages']} pages)")
    w(f"- Redirecting URLs crawled: {data['redirects']['pages']}")
    dup = data["duplicates"]
    w(f"- Duplicate content clusters: {dup['clusters']} ({dup['pages']} pages)")
    w("")
    w("| HTTP status | Pages |")
    w("| ---: | ---: |")
    for row in t["status_distribution"]:
        w(f"| {row['status']} | {row['pages']} |")
    w("")
    w("A status of 0 means the request failed before any HTTP response (transport error).")
    w("")

    w("## Issues by Type")
    w("")
    w(f"Full list: Excel sheet *{sheets['issues']}*.")
    w("")
    if iss["by_type"]:
        w("| Severity | Issue | Category | Occurrences | Pages |")
        w("| --- | --- | --- | ---: | ---: |")
        for it in iss["by_type"]:
            w(f"| {it['severity']} | {_code(it['issue'])} | {_code(it['category'])} | {it['occurrences']} | {it['pages']} |")
        w("")
        w("| Category | Occurrences |")
        w("| --- | ---: |")
        for c in iss["by_category"]:
            w(f"| {_code(c['category'])} | {c['occurrences']} |")
    else:
        w("No issues recorded.")
    w("")

    w("## Broken Internal Link Targets")
    w("")
    w(f"Targets returning HTTP 4xx/5xx or failing at transport level (TLS, DNS, timeout), "
      f"including redirects whose final hop fails. Full list: Excel sheet *{sheets['links']}*.")
    w("")
    if bl["items"]:
        w("| Error type | Targets |")
        w("| --- | ---: |")
        for e in bl["by_error_type"]:
            w(f"| {_code(e['error_type'])} | {e['targets']} |")
        w("")
        w("| Target | HTTP status | Error type | Linking pages | Sample sources |")
        w("| --- | ---: | --- | ---: | --- |")
        for b in bl["items"]:
            srcs = ", ".join(_u(s, d) for s in b["sample_sources"])
            w(f"| {_u(b['target_url'], d)} | {b['status']} | {_code(b['error_type'])} | {b['linking_pages']} | {srcs} |")
    else:
        w("No broken internal link targets found.")
    w("")
    if bl["unlinked_broken_urls"]:
        w("**Broken URLs with no internal links** (crawled from the XML sitemap or redirects):")
        w("")
        w("| URL | HTTP status | Error type | Redirects to | In sitemap |")
        w("| --- | ---: | --- | --- | --- |")
        for u in bl["unlinked_broken_urls"]:
            w(f"| {_u(u['url'], d)} | {u['status']} | {_code(u['error_type'])} | "
              f"{_u(u['final_url'], d) if u['final_url'] else '—'} | {'yes' if u['in_sitemap'] else 'no'} |")
        w("")

    ml = data["malformed_links"]
    w("## Malformed Relative Links")
    w("")
    if not ml["available"]:
        w("Not measured: this crawl predates malformed-link annotation.")
    elif not ml["items"]:
        w("No scheme-less links that look like domain names were found.")
    else:
        w(f"Links whose `href` has no scheme but looks like a domain name: {ml['total']}. Browsers resolve "
          f"these relative to the page, so they never reach the intended site.")
        w("")
        w(f"- Resolved URL fails (severity error): {ml['errors']}")
        w(f"- Resolved URL works but misses the intended site (severity warning): {ml['warnings']}")
        w("")
        w("| Severity | Source page | href | Resolves to | Outcome |")
        w("| --- | --- | --- | --- | --- |")
        for m in ml["items"]:
            w(f"| {m['severity']} | {_u(m['source_url'], d)} | {_code(m['raw_href'])} | {_u(m['resolved_url'], d)} | {_code(m['outcome'])} |")
    w("")

    rd = data["redirects"]
    w("## Redirects")
    w("")
    if rd["by_pattern"]:
        w("| Pattern | Pages | Samples |")
        w("| --- | ---: | --- |")
        for g in rd["by_pattern"]:
            samples = ", ".join(f"{_u(s['url'], d)} → {_u(s['final_url'], d)}" for s in g["samples"])
            w(f"| {g['label']} | {g['pages']} | {samples} |")
    else:
        w("No redirecting URLs were crawled.")
    w("")

    lr = data["links_to_redirects"]
    w("## Internal Links to Redirects")
    w("")
    if lr["targets"]:
        w(f"Redirecting URLs linked internally: {lr['targets']} ({lr['links']} source/target pairs).")
        w("")
        sl, ot = lr["slash_normalisation"], lr["other"]
        w(f"- Trailing-slash normalisation: {sl['targets']} targets, {sl['links']} links")
        w(f"- Other redirects: {ot['targets']} targets, {ot['links']} links")
        w("")
        for title, grp in (("Other redirects", ot), ("Trailing-slash normalisation", sl)):
            if grp["items"]:
                w(f"**{title}**")
                w("")
                w("| Linked URL | Redirects to | Linking pages |")
                w("| --- | --- | ---: |")
                for e in grp["items"]:
                    w(f"| {_u(e['target_url'], d)} | {_u(e['final_url'], d)} | {e['linking_pages']} |")
                w("")
    else:
        w("No internal links point at redirecting URLs.")
        w("")

    w("## Duplicate Content Clusters")
    w("")
    if dup["source"] == "issue_details":
        w("Note: this crawl has no stored duplicate edges; clusters are rebuilt from issue details and may be incomplete.")
        w("")
    if dup["items"]:
        for c in dup["items"]:
            more = f" (+{c['more']} more)" if c["more"] else ""
            w(f"- Cluster {c['cluster']} ({c['size']} pages): {', '.join(_u(u, d) for u in c['urls'][:n])}{more}")
    else:
        w("No duplicate content clusters detected.")
    w("")

    pt = data["page_types"]
    w("## Page Classification")
    w("")
    if not pt["available"]:
        w("Not available: this crawl predates post-crawl page classification. Page-type and clinic sections are omitted.")
        w("")
    else:
        w(f"Per-page types: Excel sheet *{sheets['page_types']}*.")
        w("")
        w("| Page type | Pages | HTTP 200 | LocalBusiness-family schema | Any schema |")
        w("| --- | ---: | ---: | ---: | ---: |")
        for c in pt["coverage"]:
            w(f"| {_code(c['page_type'])} | {c['pages']} | {c['ok_pages']} | {c['with_local_schema']} | {c['with_any_schema']} |")
        w("")
        if pt["directory_hubs"]:
            w("Directory hubs: " + ", ".join(_u(u, d) for u in pt["directory_hubs"]))
            w("")
        cl = data["clinics"]
        w("## Clinic Locations")
        w("")
        w(f"Clinic location pages: {cl['total']}.")
        w("")
        w({
            "locations_file": "Clinic pages come from the authoritative --locations-file.",
            "directory": "Clinic pages were detected as entries of the directory hubs above.",
            "on_page_signals": "No directory hub was detected; clinic pages were inferred from on-page address "
                               "and map/phone signals (lower confidence). Use --directory-url or --locations-file to confirm.",
            "none": "No clinic location pages were detected, so LocalBusiness schema checks did not run. "
                    "Use --directory-url or --locations-file if the site has locations.",
        }[cl["detection"]])
        w("")
        if cl["by_status"]:
            w("| Location status | Pages |")
            w("| --- | ---: |")
            for s in cl["by_status"]:
                w(f"| {_code(s['status'])} | {s['pages']} |")
            w("")
        if cl["non_open"]:
            w("| Clinic page | Status | Moved to | Relocated | Evidence |")
            w("| --- | --- | --- | --- | --- |")
            for c in cl["non_open"]:
                w(f"| {_u(c['url'], d)} | {_code(c['status'])} | {_u(c['moved_to'], d) if c['moved_to'] else '—'} | "
                  f"{'yes' if c['relocated'] else 'no'} | {_code(c['evidence_source'])} |")
            w("")

    pp = data["prototype_pages"]
    w("## Prototype and Staging Pages")
    w("")
    w(f"- Prototype or staging pages: {pp['pages']}")
    w(f"- Pages referencing staging hosts: {pp['staging_reference_pages']}")
    w("")
    for it in pp["items"]:
        w(f"- {it['severity']}: {_u(it['url'], d)} ({_code(it['issue'])})")
    if pp["items"]:
        w("")

    st = data["stack"]
    w("## Server and Stack (Observed Headers Only)")
    w("")
    w("Only values seen in response headers and generator meta tags are reported.")
    w("")
    for label, key in (("`Server` header", "server_headers"), ("Cache header", "cache_headers"), ("Generator meta tag", "generators")):
        vals = st[key]
        if vals:
            w(f"- {label}: " + ", ".join(f"{_code(v['value'])} ({v['pages']} pages)" for v in vals))
        else:
            w(f"- {label}: not observed")
    if st["cache_statuses"]:
        w("- Cache status values: " + ", ".join(f"{_code(v['value'])} ({v['pages']} pages)" for v in st["cache_statuses"]))
    w("")

    pf = data["performance"]
    w("## Performance")
    w("")
    w(f"Crawl timings are crawler-measured: they {pf['crawler_note']}.")
    w("")
    for label, key in (("Crawler-measured fetch time", "crawler_response_time"), ("Crawler-measured TTFB", "crawler_ttfb")):
        s = pf[key]
        if s["measured"]:
            w(f"- {label}: median {_num(s['median_ms'])} ms, p95 {_num(s['p95_ms'])} ms, "
              f"mean {_num(s['mean_ms'])} ms ({s['pages']} pages)")
        else:
            w(f"- {label}: not measured")
    sq = pf["sequential_sample"]
    if sq["measured"]:
        w(f"- Sequential TTFB sample (concurrency 1, cache-busted): median {_num(sq['median_ttfb_ms'])} ms, "
          f"p95 {_num(sq['p95_ttfb_ms'])} ms ({sq['measured_urls']} of {sq['sample_size']} URLs measured)")
    else:
        w("- Sequential TTFB sample: not measured")
    ru = pf["real_user"]
    if ru["measured"]:
        parts = ", ".join(f"{m['metric']} {_num(m['p75'])} ({m['category'] or 'n/a'})" for m in ru["origin"])
        w(f"- Real-user field data (CrUX via PageSpeed Insights, {ru['strategy']}, origin p75): {parts or 'no origin data'}; "
          f"{ru['urls_with_field_data']} of {ru['urls_requested']} sample URLs have URL-level field data")
    else:
        w("- Real-user field data (CrUX): not measured")
    w("")
    return "\n".join(L)


# ── Narrative verification ───────────────────────────────────────────────────

STACK_TERMS = (
    "apache", "nginx", "litespeed", "openresty", "caddy", "iis", "cloudflare", "varnish", "fastly",
    "akamai", "cloudfront", "sucuri", "wp engine", "kinsta", "wordpress", "drupal", "joomla",
    "shopify", "wix", "squarespace", "webflow",
)
_NUM = r"(\d[\d,]*(?:\.\d+)?)"


def _to_num(s: str) -> float:
    return float(s.replace(",", ""))


def _metric_patterns(data: dict[str, Any]) -> list[tuple[str, re.Pattern[str], set[float]]]:
    iss = data["issues"]
    status = {r["status"]: r["pages"] for r in data["totals"]["status_distribution"]}
    redirects = data["redirects"]["pages"]
    broken_issue = next((t for t in iss["by_type"] if t["issue"] == "Broken Internal Link"), None)
    broken_vals = {float(data["broken_links"]["targets"])}
    if broken_issue:
        broken_vals |= {float(broken_issue["occurrences"]), float(broken_issue["pages"])}
    dup = data["duplicates"]
    dup_issue = next((t for t in iss["by_type"] if t["issue"] == "Duplicate Content Detected"), None)
    dup_vals = {float(dup["clusters"]), float(dup["pages"])}
    if dup_issue:
        dup_vals.add(float(dup_issue["occurrences"]))
    pats: list[tuple[str, re.Pattern[str], set[float]]] = [
        ("pages crawled", re.compile(rf"{_NUM}\s+(?:total\s+(?:urls|pages)|(?:urls|pages)\s+(?:crawled|analy[sz]ed|processed))\b", re.I),
         {float(data["totals"]["pages_crawled"])}),
        ("total issues", re.compile(rf"{_NUM}\s+total\s+issues\b", re.I), {float(iss["total"])}),
        ("errors", re.compile(rf"(?:{_NUM}\s+errors\b|\berrors\s*\(\s*{_NUM}\s*\))", re.I), {float(iss["errors"])}),
        ("warnings", re.compile(rf"(?:{_NUM}\s+warnings\b|\bwarnings\s*\(\s*{_NUM}\s*\))", re.I), {float(iss["warnings"])}),
        ("info notices", re.compile(rf"(?:{_NUM}\s+info(?:rmational)?(?:\s+notices)?\b|\binfo(?:rmational)?\s*\(\s*{_NUM}\s*\))", re.I), {float(iss["info"])}),
        ("broken internal links", re.compile(rf"{_NUM}\s+broken\s+(?:internal\s+)?links?\b", re.I), broken_vals),
        ("HTTP 404 pages", re.compile(rf"{_NUM}\s+(?:hard\s+)?(?:http\s+)?404(?:s|\s+(?:responses|pages|errors))\b", re.I), {float(status.get(404, 0))}),
        ("redirects", re.compile(rf"{_NUM}\s+(?:http\s+)?(?:3xx\s+|301\s+)?(?:permanent\s+)?redirects\b", re.I),
         {float(redirects), float(status.get(301, 0))}
         | {float(t["occurrences"]) for t in iss["by_type"] if re.match(r"HTTP 3\d\d Redirect", t["issue"])}),
        ("duplicates", re.compile(rf"{_NUM}\s+(?:near-)?duplicate", re.I), dup_vals),
    ]
    for t in iss["by_type"]:
        words = [w for w in re.findall(r"[a-z0-9]+", t["issue"].lower())]
        if len(words) < 2:
            continue
        body = r"[\s/-]+".join(re.escape(w) for w in words[:-1]) + r"[\s/-]+" + re.escape(words[-1]) + r"(?:s|es)?"
        pats.append((t["issue"], re.compile(rf"{_NUM}\s+(?:[a-z-]+\s+){{0,2}}?{body}\b", re.I),
                     {float(t["occurrences"]), float(t["pages"])}))
    return pats


def verify_narrative(text: str, data: dict[str, Any]) -> list[str]:
    """Return contradictions between an externally written narrative and the report data."""
    problems: list[str] = []
    plain = re.sub(r"[*_]", "", text)

    # 1. Counts attached to known metrics
    observed_statuses = {float(r["status"]) for r in data["totals"]["status_distribution"] if r["status"] >= 100}
    for name, pat, allowed in _metric_patterns(data):
        for m in pat.finditer(plain):
            if re.search(r"\bhttp\s*$", plain[max(0, m.start() - 6) : m.start()], re.I):
                continue  # "HTTP 404 errors" names a status code, not a count
            raw = next(g for g in m.groups() if g)
            val = _to_num(raw)
            if val not in allowed and val in observed_statuses and "," not in raw:
                continue  # "triggers 404 errors": a status code observed in this crawl, not a count
            if val not in allowed:
                exp = ", ".join(_num(int(a) if a == int(a) else a) for a in sorted(allowed))
                problems.append(f"Count mismatch for {name}: narrative says {raw} (\"{m.group(0).strip()}\"), data says {exp}")

    # 2. Stack claims must be backed by observed headers / generator tags
    observed = " ".join(
        v["value"].lower()
        for key in ("server_headers", "cache_headers", "cache_statuses", "generators")
        for v in data["stack"][key]
    )
    for term in STACK_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", plain, re.I) and term not in observed:
            seen = ", ".join(v["value"] for v in data["stack"]["server_headers"]) or "none"
            problems.append(f"Stack claim not supported by observed headers: narrative mentions '{term}', observed Server headers: {seen}")

    # 3. Duplicate claims must name pages that are in a duplicate cluster
    domain = data["domain"].lower()
    clustered = set()
    for c in data["duplicates"]["items"]:
        clustered |= {_path_key(u, domain) for u in c["urls"]}
    for sentence in re.split(r"(?<=[.!?])\s+|\n", plain):
        if not re.search(r"\bduplicat|\bidentical title|\bsame title", sentence, re.I):
            continue
        for ref in _url_refs(sentence, domain):
            if ref not in clustered:
                problems.append(f"Duplicate claim not in duplicate clusters: {ref}")

    return problems


def _path_key(url: str, domain: str) -> str:
    p = urlparse(url)
    if p.netloc and p.netloc.lower() != domain:
        return url.rstrip("/")
    return (p.path or "/").rstrip("/") or "/"


def _url_refs(text: str, domain: str) -> list[str]:
    refs = []
    for m in re.finditer(r"https?://[^\s`)\]>,;]+|(?<![\w.])/[a-z0-9][a-z0-9/_.-]*", text, re.I):
        s = m.group(0).rstrip(".")
        if "*" in s:
            continue
        p = urlparse(s)
        if p.netloc and p.netloc.lower() != domain:
            continue
        refs.append(_path_key(s, domain))
    return refs


# ── CLI entry ────────────────────────────────────────────────────────────────

def run_report(
    db: str,
    output: str = "",
    fmt: str = "md",
    sample_limit: int = 10,
    verify_narrative_path: str = "",
) -> int:
    """Build the report; write Markdown/JSON; optionally verify a narrative. Returns exit code."""
    data = build_report_data(db, sample_limit=sample_limit)
    db_dir = Path(db).resolve().parent
    if fmt == "json":
        out_path = Path(output) if output else db_dir / "report_summary.json"
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Report summary written: {out_path}")
    else:
        out_path = Path(output) if output else db_dir / "crawl_report.md"
        out_path.write_text(render_markdown(data) + "\n", encoding="utf-8")
        json_path = out_path.with_name("report_summary.json")
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Report written: {out_path}")
        print(f"Report summary written: {json_path}")

    if verify_narrative_path:
        text = Path(verify_narrative_path).read_text(encoding="utf-8")
        problems = verify_narrative(text, data)
        if problems:
            print(f"Narrative verification FAILED ({len(problems)} problem(s)):")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("Narrative verification passed.")
    return 0

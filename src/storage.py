"""Persistent SQLite storage for crawl state, records, link graph, and evidence."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from src.models import (
    InternalLinkEdge,
    SEOIssue,
)


class SQLiteStorage:
    """Manages an ACID, WAL-mode SQLite database for checkpointing crawl results and link graph."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS crawl_metadata (\n                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                domain TEXT,
                depth INTEGER,
                status_code INTEGER,
                title TEXT,
                meta_description TEXT,
                canonical_url TEXT,
                word_count INTEGER,
                internal_outlinks_count INTEGER,
                external_outlinks_count INTEGER,
                response_time_ms INTEGER DEFAULT 0,
                page_size_bytes INTEGER DEFAULT 0,
                inlinks_count INTEGER DEFAULT 0,
                is_ssr INTEGER,
                raw_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS internal_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT NOT NULL,
                target_url TEXT NOT NULL,
                anchor_text TEXT,
                placement TEXT,
                is_nofollow INTEGER DEFAULT 0,
                target_status INTEGER
            );

            CREATE TABLE IF NOT EXISTS seo_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                issue TEXT NOT NULL,
                details TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS citations (
                url TEXT PRIMARY KEY,
                platform TEXT,
                label TEXT,
                status TEXT,
                nap_match TEXT,
                confidence TEXT,
                proxy_port INTEGER,
                raw_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS serp (
                query TEXT,
                location TEXT,
                raw_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_type TEXT,
                source_url TEXT,
                timestamp TEXT,
                http_status INTEGER,
                dom_selector TEXT,
                verbatim_text TEXT,
                hash_sha256 TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);
            CREATE INDEX IF NOT EXISTS idx_internal_links_source ON internal_links(source_url);
            CREATE INDEX IF NOT EXISTS idx_internal_links_target ON internal_links(target_url);
            CREATE INDEX IF NOT EXISTS idx_seo_issues_url ON seo_issues(url);
            CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_url);
            """)

    def set_meta(self, key: str, value: Any) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO crawl_metadata (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )

    def get_meta(self, key: str) -> Any:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM crawl_metadata WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else None

    def save_page(self, page_data: dict[str, Any], domain: str = "", depth: int = 0) -> None:
        url = page_data.get("url", "")
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pages (
                    url, domain, depth, status_code, title, meta_description,
                    canonical_url, word_count, internal_outlinks_count,
                    external_outlinks_count, response_time_ms, page_size_bytes,
                    inlinks_count, is_ssr, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    domain,
                    depth,
                    page_data.get("status_code", 200),
                    page_data.get("title", ""),
                    page_data.get("meta_description", ""),
                    page_data.get("canonical_url", ""),
                    page_data.get("word_count", 0),
                    page_data.get("internal_outlinks_count", 0),
                    page_data.get("external_outlinks_count", 0),
                    page_data.get("response_time_ms", 0),
                    page_data.get("page_size_bytes", 0),
                    page_data.get("inlinks_count", 0),
                    1 if page_data.get("is_ssr", True) else 0,
                    json.dumps(page_data, ensure_ascii=False),
                ),
            )

    def save_internal_links_batch(self, links: list[InternalLinkEdge | dict[str, Any]]) -> None:
        if not links:
            return
        source_urls = {
            (l.source_url if isinstance(l, InternalLinkEdge) else l.get("source_url", ""))
            for l in links
        }
        rows = [
            (
                l.source_url if isinstance(l, InternalLinkEdge) else l.get("source_url", ""),
                l.target_url if isinstance(l, InternalLinkEdge) else l.get("target_url", ""),
                l.anchor_text if isinstance(l, InternalLinkEdge) else l.get("anchor_text", ""),
                l.placement if isinstance(l, InternalLinkEdge) else l.get("placement", "body"),
                1 if (l.is_nofollow if isinstance(l, InternalLinkEdge) else l.get("is_nofollow", False)) else 0,
                l.target_status if isinstance(l, InternalLinkEdge) else l.get("target_status"),
            )
            for l in links
        ]
        with self._get_connection() as conn:
            for src in source_urls:
                if src:
                    conn.execute("DELETE FROM internal_links WHERE source_url = ?", (src,))
            conn.executemany(
                """
                INSERT INTO internal_links (
                    source_url, target_url, anchor_text, placement, is_nofollow, target_status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def save_seo_issues_batch(self, issues: list[SEOIssue | dict[str, Any]]) -> None:
        if not issues:
            return
        rows = [
            (
                i.url if isinstance(i, SEOIssue) else i.get("url", ""),
                i.severity if isinstance(i, SEOIssue) else i.get("severity", "warning"),
                i.category if isinstance(i, SEOIssue) else i.get("category", "SEO"),
                i.issue if isinstance(i, SEOIssue) else i.get("issue", ""),
                i.details if isinstance(i, SEOIssue) else i.get("details", ""),
            )
            for i in issues
        ]
        with self._get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO seo_issues (url, severity, category, issue, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def backfill_link_statuses_from_pages(self) -> None:
        """Backfill target_status on internal_links from crawled pages (exact or trailing-slash matched)."""
        with self._get_connection() as conn:
            conn.execute("""
            UPDATE internal_links
            SET target_status = COALESCE(
                (SELECT p.status_code FROM pages p WHERE p.url = internal_links.target_url LIMIT 1),
                (SELECT p.status_code FROM pages p WHERE rtrim(p.url, '/') = rtrim(internal_links.target_url, '/') LIMIT 1)
            )
            WHERE target_status IS NULL
            """)

    def post_crawl_sync(self, duplicate_issues: dict[str, SEOIssue] | None = None) -> None:
        """Run post-crawl link status backfill, compute inlinks, and update pages with broken links."""
        self.backfill_link_statuses_from_pages()

        with self._get_connection() as conn:
            # 1b. If a link points to a 3xx redirect page whose final destination returned >= 400, mark link as broken
            page_rows = conn.execute("SELECT url, raw_json FROM pages").fetchall()
            redirect_broken_targets: dict[str, int] = {}
            redirect_final_map: dict[str, str] = {}
            for pr in page_rows:
                u = pr["url"]
                p_data = json.loads(pr["raw_json"])
                st = int(p_data.get("status_code", 0))
                r_target_st = p_data.get("redirect_target_status")
                f_url = p_data.get("final_url") or ""
                if st in (301, 302, 303, 307, 308):
                    if f_url and f_url != u:
                        redirect_final_map[u.rstrip("/")] = f_url.rstrip("/")
                    if isinstance(r_target_st, int) and r_target_st >= 400:
                        redirect_broken_targets[u] = r_target_st
                        redirect_broken_targets[u.rstrip("/")] = r_target_st

            if redirect_broken_targets:
                for r_url, r_st in redirect_broken_targets.items():
                    conn.execute(
                        "UPDATE internal_links SET target_status = ? WHERE target_url = ? OR rtrim(target_url, '/') = ?",
                        (r_st, r_url, r_url.rstrip("/")),
                    )

            # 2. Compute inlinks_count per page (matching both exact URL and trailing-slash normalized URL, plus redirect resolution)
            inlink_rows = conn.execute("""
            SELECT target_url, COUNT(*) as cnt
            FROM internal_links
            GROUP BY target_url
            """).fetchall()
            inlinks_exact: dict[str, int] = {}
            inlinks_rtrim: dict[str, int] = {}
            for r in inlink_rows:
                t_url = r["target_url"] or ""
                cnt = int(r["cnt"])
                inlinks_exact[t_url] = inlinks_exact.get(t_url, 0) + cnt
                norm_key = t_url.rstrip("/")
                inlinks_rtrim[norm_key] = inlinks_rtrim.get(norm_key, 0) + cnt
                # Credit final destination of a redirect with inlinks pointing to the redirecting URL
                if norm_key in redirect_final_map:
                    dest_key = redirect_final_map[norm_key]
                    if dest_key != norm_key:
                        inlinks_rtrim[dest_key] = inlinks_rtrim.get(dest_key, 0) + cnt

            # 3. Find broken outlinks (target_status >= 400), deduplicated per (source_url, target_url)
            broken_rows = conn.execute("""
            SELECT DISTINCT source_url, target_url, target_status
            FROM internal_links
            WHERE target_status >= 400
            """).fetchall()
            broken_map: dict[str, list[str]] = {}
            for br in broken_rows:
                broken_map.setdefault(br["source_url"], []).append(br["target_url"])

            # Clear any previously synced post-crawl issues so re-runs are idempotent
            conn.execute(
                "DELETE FROM seo_issues WHERE issue IN ('Broken Internal Link', 'Duplicate Content Detected')"
            )

            # 4. Update raw_json and inlinks_count for all pages
            updated_pages = []
            new_post_crawl_issues: list[tuple[str, str, str, str, str]] = []

            for pr in page_rows:
                u = pr["url"]
                data = json.loads(pr["raw_json"])
                inlinks_val = max(inlinks_exact.get(u, 0), inlinks_rtrim.get(u.rstrip("/"), 0))
                data["inlinks_count"] = inlinks_val
                b_links = broken_map.get(u, [])
                data["broken_outlinks"] = b_links

                # Filter out stale post-crawl issues before re-adding
                existing_issues = [
                    iss
                    for iss in data.get("issues", [])
                    if iss.get("issue") not in ("Broken Internal Link", "Duplicate Content Detected")
                ]

                # Add broken link issues
                for bl in b_links:
                    iss = {
                        "url": u,
                        "severity": "error",
                        "category": "Links",
                        "issue": "Broken Internal Link",
                        "details": f"Page links to broken internal URL: {bl}",
                    }
                    existing_issues.append(iss)
                    new_post_crawl_issues.append((
                        iss["url"],
                        iss["severity"],
                        iss["category"],
                        iss["issue"],
                        iss["details"],
                    ))

                # Add duplicate content issues if flagged
                if duplicate_issues and u in duplicate_issues:
                    dup_iss = duplicate_issues[u].model_dump()
                    existing_issues.append(dup_iss)
                    new_post_crawl_issues.append((
                        dup_iss["url"],
                        dup_iss["severity"],
                        dup_iss["category"],
                        dup_iss["issue"],
                        dup_iss["details"],
                    ))

                data["issues"] = existing_issues
                updated_pages.append((data["inlinks_count"], json.dumps(data, ensure_ascii=False), u))

            conn.executemany(
                "UPDATE pages SET inlinks_count = ?, raw_json = ? WHERE url = ?",
                updated_pages,
            )

            if new_post_crawl_issues:
                conn.executemany(
                    """
                    INSERT INTO seo_issues (url, severity, category, issue, details)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    new_post_crawl_issues,
                )

    def save_citation(self, citation_data: dict[str, Any]) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO citations (
                    url, platform, label, status, nap_match, confidence, proxy_port, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    citation_data.get("url", ""),
                    citation_data.get("platform", ""),
                    citation_data.get("label", ""),
                    citation_data.get("status", ""),
                    citation_data.get("nap_match", ""),
                    citation_data.get("confidence", ""),
                    citation_data.get("proxy_port"),
                    json.dumps(citation_data, ensure_ascii=False),
                ),
            )

    def save_serp(self, query: str, location: str, serp_data: dict[str, Any]) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO serp (query, location, raw_json) VALUES (?, ?, ?)",
                (query, location, json.dumps(serp_data, ensure_ascii=False)),
            )

    def save_evidence_batch(self, evidence_list: list[dict[str, Any]]) -> None:
        if not evidence_list:
            return
        rows = [
            (
                e.get("claim_type", ""),
                e.get("source_url", ""),
                e.get("timestamp", ""),
                e.get("http_status", 0),
                e.get("dom_selector", ""),
                e.get("verbatim_text", ""),
                e.get("hash_sha256", ""),
                json.dumps(e, ensure_ascii=False),
            )
            for e in evidence_list
        ]
        with self._get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO evidence (
                    claim_type, source_url, timestamp, http_status,
                    dom_selector, verbatim_text, hash_sha256, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_completed_urls(self, mode: str) -> set[str]:
        """Fetch all URLs that have already been persisted."""
        table = "pages" if mode == "competitor" else ("citations" if mode == "citations" else "serp")
        if table == "serp":
            return set()
        with self._get_connection() as conn:
            rows = conn.execute(f"SELECT url FROM {table}").fetchall()
            return {r["url"] for r in rows}

    def fetch_all_pages(self) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT raw_json FROM pages ORDER BY rowid ASC").fetchall()
            return [json.loads(r["raw_json"]) for r in rows]

    def fetch_all_internal_links(self) -> list[dict[str, Any]]:
        """Fetch all stored internal link edges with status and anchor text."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT source_url, target_url, anchor_text, placement, is_nofollow, target_status FROM internal_links ORDER BY id ASC"
            ).fetchall()
            return [
                {
                    "source_url": r["source_url"],
                    "target_url": r["target_url"],
                    "anchor_text": r["anchor_text"] or "",
                    "placement": r["placement"] or "body",
                    "is_nofollow": bool(r["is_nofollow"]),
                    "target_status": r["target_status"],
                }
                for r in rows
            ]

    def get_unvisited_link_targets(self) -> list[str]:
        """Fetch distinct target URLs in internal_links where target_status is still NULL after backfilling known pages."""
        self.backfill_link_statuses_from_pages()
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT target_url FROM internal_links WHERE target_status IS NULL"
            ).fetchall()
            return [r["target_url"] for r in rows if r["target_url"]]

    def update_link_target_statuses(self, status_map: dict[str, int]) -> None:
        """Update target_status for specific target URLs."""
        if not status_map:
            return
        with self._get_connection() as conn:
            conn.executemany(
                "UPDATE internal_links SET target_status = ? WHERE target_url = ?",
                [(status, url) for url, status in status_map.items()],
            )

    def fetch_all_citations(self) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT raw_json FROM citations ORDER BY rowid ASC").fetchall()
            return [json.loads(r["raw_json"]) for r in rows]

    def fetch_all_serp(self) -> dict[str, Any] | None:
        with self._get_connection() as conn:
            row = conn.execute("SELECT raw_json FROM serp ORDER BY rowid DESC LIMIT 1").fetchone()
            return json.loads(row["raw_json"]) if row else None

    def fetch_all_evidence(self) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT raw_json FROM evidence ORDER BY rowid ASC").fetchall()
            return [json.loads(r["raw_json"]) for r in rows]

    def count_results(self, mode: str) -> int:
        table = "pages" if mode == "competitor" else ("citations" if mode == "citations" else "serp")
        with self._get_connection() as conn:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            return row["cnt"] if row else 0

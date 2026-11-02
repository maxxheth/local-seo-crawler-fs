"""Persistent SQLite storage for crawl state, records, and evidence."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any


class SQLiteStorage:
    """Manages an ACID, WAL-mode SQLite database for checkpointing crawl results."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        # High performance concurrent settings
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS crawl_metadata (
                key TEXT PRIMARY KEY,
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
                is_ssr INTEGER,
                raw_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pages (
                    url, domain, depth, status_code, title, meta_description,
                    canonical_url, word_count, internal_outlinks_count,
                    external_outlinks_count, is_ssr, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page_data.get("url", ""),
                    domain,
                    depth,
                    page_data.get("status_code", 200),
                    page_data.get("title", ""),
                    page_data.get("meta_description", ""),
                    page_data.get("canonical_url", ""),
                    page_data.get("word_count", 0),
                    page_data.get("internal_outlinks_count", 0),
                    page_data.get("external_outlinks_count", 0),
                    1 if page_data.get("is_ssr", True) else 0,
                    json.dumps(page_data, ensure_ascii=False),
                ),
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

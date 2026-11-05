"""Job queue abstraction supporting SQLite and Redis for chunked scraping."""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
from pathlib import Path
import sqlite3
from typing import Any

from src.sitemap import normalize_url


class FrontierQueue(ABC):
    """Abstract frontier queue interface for chunked scraping."""

    @abstractmethod
    def push_targets(self, targets: list[dict[str, Any]], allow_seen: bool = False) -> int:
        """Add targets to frontier if not already seen (or unconditionally if allow_seen=True)."""
        ...

    @abstractmethod
    def pop_chunk(self, chunk_size: int) -> list[dict[str, Any]]:
        """Pop up to chunk_size pending targets."""
        ...

    @abstractmethod
    def has_pending(self) -> bool:
        """True if queue still has pending items or in-flight leases."""
        ...

    @abstractmethod
    def size(self) -> int:
        """Total number of pending targets."""
        ...

    @abstractmethod
    def seen_count(self) -> int:
        """Total number of unique URLs ever discovered."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Clear queue state."""
        ...

    @abstractmethod
    def mark_done(self, urls: list[str]) -> None:
        """Mark URLs as completed and remove from pending/in-flight jobs."""
        ...

    @abstractmethod
    def reconcile(self, completed_urls: set[str] | None = None) -> None:
        """Reset any abandoned in-flight jobs back to pending and remove uncompleted seen URLs."""
        ...


class SQLiteFrontierQueue(FrontierQueue):
    """Thread-safe SQLite-backed frontier queue with atomic chunk popping and seen URL tracking."""

    def __init__(self, db_path: Path | str, queue_name: str = "frontier") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_name = queue_name
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS {self.queue_name}_seen (
                url TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS {self.queue_name}_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                data TEXT NOT NULL,
                retries INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending' -- 'pending', 'in_flight', 'done'
            );

            CREATE INDEX IF NOT EXISTS idx_{self.queue_name}_status ON {self.queue_name}_jobs(status);
            """)

    def push_targets(self, targets: list[dict[str, Any]], allow_seen: bool = False) -> int:
        if not targets:
            return 0
        added = 0
        with self._get_conn() as conn:
            for t in targets:
                raw_u = t.get("url", "")
                norm_u = normalize_url(raw_u) or raw_u
                t_copy = dict(t)
                t_copy["url"] = norm_u

                if allow_seen:
                    conn.execute(
                        f"INSERT OR IGNORE INTO {self.queue_name}_seen (url) VALUES (?)",
                        (norm_u,),
                    )
                    conn.execute(
                        f"INSERT INTO {self.queue_name}_jobs (url, data, retries, status) VALUES (?, ?, ?, 'pending')",
                        (norm_u, json.dumps(t_copy, ensure_ascii=False), int(t_copy.get("retries", 0))),
                    )
                    added += 1
                else:
                    try:
                        conn.execute(
                            f"INSERT INTO {self.queue_name}_seen (url) VALUES (?)",
                            (norm_u,),
                        )
                        conn.execute(
                            f"INSERT INTO {self.queue_name}_jobs (url, data, retries, status) VALUES (?, ?, ?, 'pending')",
                            (norm_u, json.dumps(t_copy, ensure_ascii=False), int(t_copy.get("retries", 0))),
                        )
                        added += 1
                    except sqlite3.IntegrityError:
                        # Already seen
                        continue
        return added

    def pop_chunk(self, chunk_size: int) -> list[dict[str, Any]]:
        if chunk_size <= 0:
            return []
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, data FROM {self.queue_name}_jobs WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                (chunk_size,),
            ).fetchall()

            if not rows:
                return []

            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE {self.queue_name}_jobs SET status = 'in_flight' WHERE id IN ({placeholders})",
                ids,
            )

            return [json.loads(r["data"]) for r in rows]

    def mark_done(self, urls: list[str]) -> None:
        if not urls:
            return
        with self._get_conn() as conn:
            for i in range(0, len(urls), 500):
                batch = urls[i : i + 500]
                placeholders = ",".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM {self.queue_name}_jobs WHERE url IN ({placeholders})",
                    batch,
                )

    def reconcile(self, completed_urls: set[str] | None = None) -> None:
        """Reset any abandoned in_flight jobs back to pending, and remove uncompleted seen URLs."""
        with self._get_conn() as conn:
            conn.execute(
                f"UPDATE {self.queue_name}_jobs SET status = 'pending' WHERE status = 'in_flight'"
            )
            if completed_urls is not None:
                job_urls = {r["url"] for r in conn.execute(f"SELECT url FROM {self.queue_name}_jobs").fetchall()}
                seen_urls = {r["url"] for r in conn.execute(f"SELECT url FROM {self.queue_name}_seen").fetchall()}
                orphaned_seen = seen_urls - completed_urls - job_urls
                if orphaned_seen:
                    orphaned_list = list(orphaned_seen)
                    for i in range(0, len(orphaned_list), 500):
                        batch = orphaned_list[i : i + 500]
                        ph = ",".join("?" for _ in batch)
                        conn.execute(f"DELETE FROM {self.queue_name}_seen WHERE url IN ({ph})", batch)

    def has_pending(self) -> bool:
        with self._get_conn() as conn:
            row = conn.execute(
                f"SELECT 1 FROM {self.queue_name}_jobs WHERE status = 'pending' LIMIT 1"
            ).fetchone()
            return row is not None

    def size(self) -> int:
        with self._get_conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM {self.queue_name}_jobs WHERE status = 'pending'"
            ).fetchone()
            return row["cnt"] if row else 0

    def seen_count(self) -> int:
        with self._get_conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM {self.queue_name}_seen"
            ).fetchone()
            return row["cnt"] if row else 0

    def clear(self) -> None:
        with self._get_conn() as conn:
            conn.execute(f"DELETE FROM {self.queue_name}_jobs")
            conn.execute(f"DELETE FROM {self.queue_name}_seen")


class RedisFrontierQueue(FrontierQueue):
    """Redis-backed frontier queue for distributed parallel workers."""

    def __init__(self, redis_url: str, queue_name: str = "local_seo_crawler") -> None:
        import redis
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.queue_key = f"{queue_name}:jobs"
        self.seen_key = f"{queue_name}:seen"

    def push_targets(self, targets: list[dict[str, Any]], allow_seen: bool = False) -> int:
        if not targets:
            return 0
        added = 0
        pipeline = self.client.pipeline()
        for t in targets:
            raw_u = t.get("url", "")
            norm_u = normalize_url(raw_u) or raw_u
            t_copy = dict(t)
            t_copy["url"] = norm_u

            if allow_seen:
                self.client.sadd(self.seen_key, norm_u)
                pipeline.rpush(self.queue_key, json.dumps(t_copy, ensure_ascii=False))
                added += 1
            else:
                # SADD returns 1 if new, 0 if already in set
                is_new = self.client.sadd(self.seen_key, norm_u)
                if is_new:
                    pipeline.rpush(self.queue_key, json.dumps(t_copy, ensure_ascii=False))
                    added += 1
        pipeline.execute()
        return added

    def pop_chunk(self, chunk_size: int) -> list[dict[str, Any]]:
        if chunk_size <= 0:
            return []
        chunk: list[dict[str, Any]] = []
        for _ in range(chunk_size):
            item = self.client.lpop(self.queue_key)
            if not item:
                break
            chunk.append(json.loads(item))
        return chunk

    def has_pending(self) -> bool:
        return self.size() > 0

    def size(self) -> int:
        return self.client.llen(self.queue_key)

    def seen_count(self) -> int:
        return self.client.scard(self.seen_key)

    def clear(self) -> None:
        self.client.delete(self.queue_key, self.seen_key)

    def mark_done(self, urls: list[str]) -> None:
        pass

    def reconcile(self, completed_urls: set[str] | None = None) -> None:
        pass


def create_frontier_queue(
    redis_url: str | None = None,
    sqlite_db_path: Path | str = "artifacts/frontier.db",
    queue_name: str = "frontier",
) -> FrontierQueue:
    """Factory creating RedisFrontierQueue if redis_url is set, else SQLiteFrontierQueue."""
    if redis_url:
        try:
            rq = RedisFrontierQueue(redis_url, queue_name=queue_name)
            rq.client.ping()
            return rq
        except Exception:
            pass  # Fallback to SQLite on connection error
    return SQLiteFrontierQueue(sqlite_db_path, queue_name=queue_name)

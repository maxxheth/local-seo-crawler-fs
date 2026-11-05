"""WS3: deterministic report command."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3

import pytest

from conftest import build_site_db
from src.main import main
from src.report import build_report_data, render_markdown, run_report, verify_narrative

GOLDEN = Path(__file__).parent / "fixtures" / "report_golden.md"


@pytest.fixture()
def site_db(tmp_path: Path) -> Path:
    db = tmp_path / "crawl_results.db"
    build_site_db(db)
    return db


def _numbers_in_markdown(md: str) -> set[float]:
    """Numbers in prose/tables, ignoring code spans (URLs, issue names, header values)."""
    text = re.sub(r"`[^`]*`", " ", md)
    return {float(m) for m in re.findall(r"(?<![\w.+-])\d+(?:\.\d+)?(?![\w.])", text)} | {
        float(m) for m in re.findall(r"(?<=\+)\d+(?=\s+more)", text)
    }


def _numbers_in_json(obj) -> set[float]:
    out: set[float] = set()
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out |= _numbers_in_json(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _numbers_in_json(v)
    return out


def test_report_matches_golden_file(site_db: Path) -> None:
    md = render_markdown(build_report_data(site_db)) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":  # regenerate deliberately: UPDATE_GOLDEN=1 pytest
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(md, encoding="utf-8")
    assert GOLDEN.exists(), "golden file missing; run UPDATE_GOLDEN=1 pytest tests/test_report.py"
    assert md == GOLDEN.read_text(encoding="utf-8")


def test_report_is_deterministic(site_db: Path) -> None:
    assert render_markdown(build_report_data(site_db)) == render_markdown(build_report_data(site_db))


def test_every_number_in_report_is_in_summary_json_and_vice_versa(site_db: Path) -> None:
    data = build_report_data(site_db)
    md_nums = _numbers_in_markdown(render_markdown(data))
    json_nums = _numbers_in_json(data)
    assert md_nums - json_nums == set()
    assert json_nums - md_nums == set()


def test_report_content_reflects_data(site_db: Path) -> None:
    data = build_report_data(site_db)
    md = render_markdown(data)
    assert "`/franchising/` | 0 | `tls_hostname_mismatch`" in md
    assert "`www.youtube.com/watch?v=abc`" in md
    assert "Sequential TTFB sample: not measured" in md
    assert "Real-user field data (CrUX): not measured" in md
    assert "not user experience" in md
    clinics = {c["url"]: c for c in data["clinics"]["non_open"]}
    assert clinics["https://spa.example.com/maple-st/"]["moved_to"] == "https://spa.example.com/birch-bay/"


def test_run_report_writes_md_and_json(site_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "report.md"
    out.parent.mkdir()
    assert run_report(str(site_db), output=str(out)) == 0
    summary = json.loads((out.parent / "report_summary.json").read_text())
    assert summary == json.loads(json.dumps(build_report_data(site_db)))
    assert out.read_text().startswith("# Local SEO Crawl Report")


def test_report_mode_cli_never_crawls(site_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.orchestrator as orch

    monkeypatch.setattr(orch.CrawlerOrchestrator, "run", lambda self: pytest.fail("report mode must not crawl"))
    out = tmp_path / "r.json"
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "report", "--db", str(site_db), "--format", "json", "--output", str(out)])
    assert exc.value.code == 0
    assert json.loads(out.read_text())["totals"]["pages_crawled"] > 0


def test_verify_narrative_passes_on_rendered_report(site_db: Path) -> None:
    data = build_report_data(site_db)
    assert verify_narrative(render_markdown(data), data) == []


def test_verify_narrative_catches_wrong_count_stack_and_duplicate(site_db: Path, tmp_path: Path) -> None:
    data = build_report_data(site_db)
    pages = data["totals"]["pages_crawled"]
    errors = data["issues"]["errors"]
    clustered = {u for c in data["duplicates"]["items"] for u in c["urls"]}
    assert "https://spa.example.com/harbor/" not in clustered
    narrative = (
        f"The crawler processed {pages} total URLs and found {errors + 7} errors.\n"
        "The site runs on Apache behind a CDN.\n"
        "The Harbor page `/harbor/` shares an identical title with `/oak-hill/`.\n"
    )
    problems = verify_narrative(narrative, data)
    assert any("errors" in p and str(errors + 7) in p for p in problems)
    assert any("apache" in p.lower() for p in problems)
    assert any("/harbor" in p for p in problems)
    assert not any("/oak-hill" in p for p in problems)  # genuinely in a duplicate cluster
    assert not any("pages crawled" in p for p in problems)

    path = tmp_path / "narrative.md"
    path.write_text(narrative)
    assert run_report(str(site_db), output=str(tmp_path / "r.md"), verify_narrative_path=str(path)) == 1


def test_report_on_old_database_degrades(tmp_path: Path) -> None:
    """A crawl DB created before WS1–WS4 (no new columns/tables) still renders."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE crawl_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE pages (url TEXT PRIMARY KEY, domain TEXT, depth INTEGER, status_code INTEGER, title TEXT,
            meta_description TEXT, canonical_url TEXT, word_count INTEGER, internal_outlinks_count INTEGER,
            external_outlinks_count INTEGER, response_time_ms INTEGER DEFAULT 0, page_size_bytes INTEGER DEFAULT 0,
            inlinks_count INTEGER DEFAULT 0, is_ssr INTEGER, raw_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT '2026-11-01 10:00:00');
        CREATE TABLE internal_links (id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL,
            target_url TEXT NOT NULL, anchor_text TEXT, placement TEXT, is_nofollow INTEGER DEFAULT 0, target_status INTEGER);
        CREATE TABLE seo_issues (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, severity TEXT NOT NULL,
            category TEXT NOT NULL, issue TEXT NOT NULL, details TEXT NOT NULL);
    """)
    conn.execute(
        "INSERT INTO pages (url, domain, status_code, raw_json) VALUES (?, ?, ?, ?)",
        ("https://e.com/", "e.com", 200, json.dumps({"url": "https://e.com/", "status_code": 200, "server_header": "nginx", "response_time_ms": 420})),
    )
    conn.execute("INSERT INTO internal_links (source_url, target_url, placement, target_status) VALUES ('https://e.com/', 'https://e.com/gone', 'body', 404)")
    conn.execute("INSERT INTO seo_issues (url, severity, category, issue, details) VALUES ('https://e.com/', 'error', 'Links', 'Broken Internal Link', 'x')")
    conn.commit()
    conn.close()

    data = build_report_data(db)
    md = render_markdown(data)
    assert data["page_types"] == {"available": False}
    assert "predates post-crawl page classification" in md
    assert "predates malformed-link annotation" in md
    assert data["broken_links"]["targets"] == 1
    assert data["crawl_date"] == "2026-11-01"
    # Opened read-only: no migration happened
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(pages)")}
    assert "page_type" not in cols

"""Python Core Scraper: Bridge module called by Mojo Concurrent Orchestrator."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from src.adapters.citation import CitationAdapter
from src.adapters.competitor import CompetitorSiteAdapter
from src.adapters.serp import LocalSerpAdapter
from src.config import extract_proxy_port, load_config, load_proxies
from src.flaresolverr import FlareSolverrClient
from src.output import OutputManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def init_scraper(config_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Initialize scraper configuration from .env and environment."""
    cfg = load_config()
    if config_overrides:
        cfg.update(config_overrides)
    return cfg


def parse_proxies_file(proxy_file: str) -> list[str]:
    """Parse proxies list from file."""
    return load_proxies(proxy_file)


def create_flaresolverr_session(flaresolverr_url: str, proxy_url: str | None = None) -> str:
    """Create a new FlareSolverr session pinned to proxy_url."""
    client = FlareSolverrClient(base_url=flaresolverr_url)
    try:
        session_id = client.create_session(proxy_url=proxy_url)
        return session_id
    finally:
        client.close()


def destroy_flaresolverr_session(flaresolverr_url: str, session_id: str) -> bool:
    """Destroy an active FlareSolverr session."""
    client = FlareSolverrClient(base_url=flaresolverr_url)
    try:
        return client.destroy_session(session_id=session_id)
    finally:
        client.close()


def load_targets(
    mode: str,
    input_path: str = "",
    domain: str = "",
    query: str = "",
    location: str = "",
    business_name: str = "",
) -> list[dict[str, Any]]:
    """Load initial targets from input file or CLI parameters."""
    targets: list[dict[str, Any]] = []

    if mode == "citations":
        if input_path and Path(input_path).exists():
            with open(input_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            b_name = data.get("business", business_name)
            nap_obj = data.get("nap", {})
            default_address = nap_obj.get("address", "").strip('"')
            default_phone = nap_obj.get("phone_primary", "").strip('"')

            citations_list = data.get("citations", [])
            for c in citations_list:
                targets.append({
                    "url": c.get("url", ""),
                    "platform": c.get("platform", ""),
                    "label": c.get("label", ""),
                    "business_name": b_name,
                    "expected_address": c.get("expected_address") or default_address,
                    "expected_phone": c.get("expected_phone") or default_phone,
                })
        else:
            logger.warning("No input citations file found at %s", input_path)

    elif mode == "competitor":
        clean_domain = domain.replace("https://", "").replace("http://", "").strip("/")
        root_url = f"https://{clean_domain}/"
        targets.append({
            "url": root_url,
            "domain": clean_domain,
            "target_type": "homepage",
            "depth": 0,
        })

    elif mode == "serp":
        clean_query = query.strip()
        clean_location = location.strip()
        search_query = f"{clean_query} {clean_location}".strip()
        encoded = quote_plus(search_query)
        serp_url = f"https://www.google.com/search?q={encoded}"
        targets.append({
            "url": serp_url,
            "query": clean_query,
            "location": clean_location,
        })

    return targets


def execute_single_scrape(job: dict[str, Any]) -> dict[str, Any]:
    """Execute a single scraping job with FlareSolverr (and proxy-safe direct fallback)."""
    mode = job.get("mode", "citations")
    url = job.get("url", "")
    session_id = job.get("session_id")
    proxy_url = job.get("proxy_url")
    proxy_port = job.get("proxy_port") or extract_proxy_port(proxy_url)
    flaresolverr_url = job.get("flaresolverr_url", "http://localhost:8193")
    max_timeout = job.get("max_timeout", 60000)

    html = ""
    final_url = url
    status_code = 0
    error_msg: str | None = None

    # Try FlareSolverr first
    client = FlareSolverrClient(base_url=flaresolverr_url, max_timeout=max_timeout)
    try:
        fs_resp = client.request(
            url=url,
            session_id=session_id,
            proxy_url=proxy_url,
            max_timeout=max_timeout,
        )
        if fs_resp.error:
            error_msg = fs_resp.error
            status_code = fs_resp.status or 500
        else:
            html = fs_resp.html
            final_url = fs_resp.final_url or url
            status_code = fs_resp.status or 200
    except Exception as exc:
        logger.warning("FlareSolverr request to %s failed: %s", url, exc)
        error_msg = str(exc)
        status_code = 500
    finally:
        client.close()

    # Fallback only if explicitly enabled or during local development
    allow_direct = os.getenv("ALLOW_DIRECT_FALLBACK", "true").lower() in ("true", "1")
    if not html and error_msg and ("connect" in error_msg.lower() or "connection" in error_msg.lower()) and allow_direct:
        try:
            logger.info("Falling back to direct HTTP for %s", url)
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
            client_kwargs: dict[str, Any] = {
                "timeout": 15.0,
                "follow_redirects": True,
                "headers": headers,
            }
            if proxy_url:
                client_kwargs["proxy"] = proxy_url

            with httpx.Client(**client_kwargs) as h_client:
                r = h_client.get(url)
                html = r.text
                final_url = str(r.url)
                status_code = r.status_code
                error_msg = None
        except Exception as fallback_exc:
            logger.error("Direct HTTP fallback also failed for %s: %s", url, fallback_exc)
            error_msg = f"{error_msg} | Fallback failed: {fallback_exc}"

    # Route to appropriate adapter
    if mode == "citations":
        adapter = CitationAdapter(platform=job.get("platform"))
        citation_item, evidence_rec = adapter.parse(
            url=url,
            html=html,
            http_status=status_code,
            label=job.get("label"),
            expected_business=job.get("business_name"),
            expected_address=job.get("expected_address"),
            expected_phone=job.get("expected_phone"),
            proxy_port=proxy_port,
        )
        is_success = (status_code == 200) and (citation_item.status == "live")
        return {
            "success": is_success,
            "status_code": status_code,
            "error": error_msg or (None if is_success else f"Status: {citation_item.status}"),
            "business_name": job.get("business_name"),
            "result": citation_item.model_dump(),
            "evidence": [evidence_rec.model_dump()],
        }

    elif mode == "competitor":
        comp_adapter = CompetitorSiteAdapter()
        page_rec, ev_list = comp_adapter.parse(
            url=url,
            html=html,
            http_status=status_code,
            final_url=final_url,
        )
        lower_html = html.lower()
        is_challenge = "just a moment..." in lower_html or "cf-browser-verification" in lower_html or "access denied" in lower_html
        is_success = (status_code == 200) and bool(html) and not is_challenge
        return {
            "success": is_success,
            "status_code": status_code,
            "error": error_msg or (None if is_success else ("Cloudflare challenge detected" if is_challenge else "HTTP error")),
            "domain": job.get("domain"),
            "depth": job.get("depth", 0),
            "internal_urls": page_rec.internal_urls,
            "result": page_rec.model_dump(),
            "evidence": [e.model_dump() for e in ev_list],
        }

    elif mode == "serp":
        serp_adapter = LocalSerpAdapter()
        serp_artifact, ev_list = serp_adapter.parse(
            query=job.get("query", ""),
            location=job.get("location", ""),
            html=html,
            http_status=status_code,
            source_url=url,
        )
        lower_html = html.lower()
        is_captcha = "our systems have detected unusual traffic" in lower_html or "sorry/index" in lower_html
        is_success = (status_code == 200) and bool(html) and not is_captcha
        return {
            "success": is_success,
            "status_code": status_code,
            "error": error_msg or (None if is_success else "Google CAPTCHA / Block detected"),
            "result": serp_artifact.model_dump(),
            "evidence": [e.model_dump() for e in ev_list],
        }

    return {
        "success": False,
        "status_code": status_code,
        "error": f"Unknown mode: {mode}",
        "result": {},
        "evidence": [],
    }


def write_artifacts(
    mode: str,
    results: list[dict[str, Any]],
    output_dir: str = "artifacts",
    meta: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Write output artifacts into output_dir based on mode."""
    mgr = OutputManager(output_dir=output_dir)
    meta = meta or {}
    evidence_all: list[dict[str, Any]] = []

    for r in results:
        evs = r.get("evidence", [])
        evidence_all.extend(evs)

    if mode == "citations":
        business_name = meta.get("business") or meta.get("business_name")
        if not business_name:
            for r in results:
                b = r.get("business_name")
                if b:
                    business_name = b
                    break
        if not business_name:
            business_name = "Target Business Name"

        citations_list = [r["result"] for r in results if "result" in r]
        return mgr.export_citations(
            business_name=business_name,
            citations=citations_list,
            evidence_records=evidence_all,
        )

    elif mode == "competitor":
        target_domain = meta.get("domain", "competitor.com")
        pages = [r["result"] for r in results if "result" in r]
        crawl_depth = meta.get("crawl_depth", 1)
        return mgr.export_site_crawl(
            target_domain=target_domain,
            pages=pages,
            crawl_depth=crawl_depth,
            evidence_records=evidence_all,
        )

    elif mode == "serp":
        serp_data = results[0]["result"] if results and "result" in results[0] else {}
        return mgr.export_serp(
            serp_data=serp_data,
            evidence_records=evidence_all,
        )

    return {}

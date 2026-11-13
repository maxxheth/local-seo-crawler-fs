"""Python Core Scraper: Fast-HTTP first with FlareSolverr escalation, timing, and parsing."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from src.adapters.citation import CitationAdapter
from src.adapters.competitor import CompetitorSiteAdapter
from src.adapters.serp import LocalSerpAdapter
from src.config import extract_proxy_port, load_config, load_proxies
from src.flaresolverr import FlareSolverrClient
from src.output import OutputManager
from src.seo_auditor import SEOAuditConfig, audit_page
from src.sitemap import SitemapDiscovery, is_internal_url, normalize_url
from src.transport import TRANSPORT_ERROR_TYPES, classify_transport_error  # noqa: F401 (re-exported)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def init_scraper(
    config_overrides: dict[str, Any] | None = None,
    env_file: str | Path | None = None,
) -> dict[str, Any]:
    """Initialize scraper configuration from .env and environment."""
    cfg = load_config(env_file=env_file)
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
    use_sitemap: bool = True,
    max_pages: int = 1000,
    preserve_trailing_slash: bool = True,
    auth: httpx.Auth | None = None,
) -> list[dict[str, Any]]:
    """Load initial targets from input file, sitemap, or CLI parameters."""
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
                c_url = c.get("url", "")
                norm_c = normalize_url(c_url, preserve_trailing_slash=preserve_trailing_slash) or c_url
                targets.append({
                    "url": norm_c,
                    "platform": c.get("platform", ""),
                    "label": c.get("label", ""),
                    "business_name": b_name,
                    "expected_address": c.get("expected_address") or default_address,
                    "expected_phone": c.get("expected_phone") or default_phone,
                    "preserve_trailing_slash": preserve_trailing_slash,
                })
        else:
            logger.warning("No input citations file found at %s", input_path)

    elif mode == "competitor":
        clean_domain = domain.replace("https://", "").replace("http://", "").strip("/")
        root_url = f"https://{clean_domain}/"
        norm_root = normalize_url(root_url, preserve_trailing_slash=preserve_trailing_slash) or root_url

        # Sitemap seeding
        sitemap_urls: list[str] = []
        if use_sitemap:
            try:
                logger.info("Discovering sitemap URLs for %s...", clean_domain)
                seeder = SitemapDiscovery(
                    clean_domain,
                    max_urls=max_pages,
                    preserve_trailing_slash=preserve_trailing_slash,
                    auth=auth,
                )
                sitemap_urls = seeder.discover_urls()
                logger.info("Sitemap discovery found %d initial URLs", len(sitemap_urls))
            except Exception as sm_exc:
                logger.warning("Sitemap discovery failed or timed out: %s", sm_exc)

        discovered_sitemap_set = set(sitemap_urls)
        if not sitemap_urls:
            sitemap_urls = [norm_root]
        elif norm_root not in sitemap_urls:
            sitemap_urls.insert(0, norm_root)

        for u in sitemap_urls[:max_pages]:
            targets.append({
                "url": u,
                "domain": clean_domain,
                "target_type": "homepage" if u == norm_root else "sitemap_page",
                "depth": 0,
                "in_sitemap": u in discovered_sitemap_set,
                "preserve_trailing_slash": preserve_trailing_slash,
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


def _detect_challenge(html: str) -> bool:
    """Check if HTML content indicates Cloudflare challenge or access block."""
    if not html:
        return False
    lower = html.lower()
    return (
        "just a moment..." in lower
        or "cf-browser-verification" in lower
        or "attention required! | cloudflare" in lower
        or "checking your browser before accessing" in lower
        or "please complete the security check" in lower
        or ("access denied" in lower and ("cloudflare" in lower or "ray id:" in lower))
    )


def _extract_cache_header(headers: httpx.Headers | dict[str, str]) -> str:
    for k in (
        "cf-cache-status",
        "x-f2f-fastcgi-cache",
        "x-fastcgi-cache",
        "x-cache",
        "x-cache-status",
        "x-proxy-cache",
        "x-varnish",
        "cache-control",
    ):
        v = headers.get(k)
        if v:
            return f"{k}: {v}"
    return ""


def _error_message(e: BaseException) -> str:
    msg = str(e)
    return msg if msg else type(e).__name__


def fetch_with_redirect_inspection(
    client: httpx.Client,
    url: str,
    max_hops: int = 5,
    timeout: float = 15.0,
) -> tuple[httpx.Response | None, list[str], str, int, int | None, str | None, int]:
    """Fetch URL with hop-by-hop redirect tracking and safe target SSL inspection.

    Returns:
        (last_response, redirect_chain, final_url, initial_status, redirect_target_status, transport_error, ttfb_ms)
    """
    curr = url
    redirect_chain: list[str] = []
    initial_status = 200
    first_resp: httpx.Response | None = None
    last_resp: httpx.Response | None = None
    ttfb_ms = 0

    for hop in range(max_hops):
        t_hop = time.monotonic()
        try:
            r = client.get(curr, follow_redirects=False, timeout=timeout)
        except Exception as e:
            if hop == 0:
                return None, [], curr, 0, None, _error_message(e), 0
            else:
                return first_resp, redirect_chain, curr, initial_status, None, _error_message(e), ttfb_ms

        if hop == 0:
            initial_status = r.status_code
            first_resp = r
            ttfb_ms = int((time.monotonic() - t_hop) * 1000)

        last_resp = r

        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location") or r.headers.get("location")
            if not loc:
                break
            target_url = urljoin(curr, loc)
            redirect_chain.append(f"{r.status_code} -> {loc}")
            curr = target_url
        else:
            break

    target_status = last_resp.status_code if (last_resp and redirect_chain) else None
    return (last_resp, redirect_chain, curr, initial_status, target_status, None, ttfb_ms)


def verify_link_targets(
    urls: list[str],
    client: httpx.Client | None = None,
    timeout: float = 10.0,
    auth: httpx.Auth | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    """Status-check link targets without parsing or queueing them.

    Returns (status_map, error_map): the final HTTP status per URL (after following
    redirects), and the transport error type for URLs that failed at any hop.
    """
    status_map: dict[str, int] = {}
    error_map: dict[str, str] = {}
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=False, headers=dict(DEFAULT_HEADERS), auth=auth)
    try:
        for url in urls:
            _resp, _chain, _dest, init_s, target_s, trans_err, _ttfb = fetch_with_redirect_inspection(
                client, url, timeout=timeout
            )
            if trans_err:
                error_map[url] = classify_transport_error(trans_err) or "other"
                continue
            status = int(target_s or init_s or 0)
            if status > 0:
                status_map[url] = status
    finally:
        if own_client:
            client.close()
    return status_map, error_map


def execute_single_scrape(
    job: dict[str, Any],
    shared_client: httpx.Client | None = None,
    cookie_jar: dict[str, str] | None = None,
    audit_cfg: SEOAuditConfig | None = None,
) -> dict[str, Any]:
    """Execute scrape with fast-HTTP first, timing measurement, and SEO issue audit."""
    mode = job.get("mode", "citations")
    url = job.get("url", "")
    session_id = job.get("session_id")
    proxy_url = job.get("proxy_url")
    proxy_port = job.get("proxy_port") or extract_proxy_port(proxy_url)
    flaresolverr_url = job.get("flaresolverr_url", "http://localhost:8193")
    max_timeout = job.get("max_timeout", 60000)
    fast_http_first = job.get("fast_http_first", True)

    html = ""
    final_url = url
    status_code = 0
    initial_status_code = 0
    redirect_chain: list[str] = []
    redirect_target_status: int | None = None
    content_type = ""
    server_header = ""
    cache_status = ""
    x_robots_tag = ""
    ttfb_ms = 0
    transport_error: str | None = None
    fetch_method = "fast_http"
    error_msg: str | None = None
    solved_cookies: list[dict[str, Any]] = []
    solved_user_agent: str | None = None

    t0 = time.monotonic()

    # Step 1: Fast HTTP attempt (if enabled and mode permits)
    should_try_fast_http = fast_http_first and (mode in ("competitor", "citations"))
    if should_try_fast_http:
        try:
            client_kwargs: dict[str, Any] = {
                "timeout": 15.0,
                "headers": dict(DEFAULT_HEADERS),
            }
            if cookie_jar:
                client_kwargs["cookies"] = cookie_jar
            if proxy_url:
                client_kwargs["proxy"] = proxy_url

            if shared_client is not None:
                if cookie_jar:
                    for ck, cv in cookie_jar.items():
                        shared_client.cookies.set(ck, cv)
                r, chain, dest_url, init_s, target_s, trans_err, hop_ttfb = fetch_with_redirect_inspection(
                    shared_client, url, timeout=15.0
                )
            else:
                with httpx.Client(**client_kwargs) as h_client:
                    r, chain, dest_url, init_s, target_s, trans_err, hop_ttfb = fetch_with_redirect_inspection(
                        h_client, url, timeout=15.0
                    )

            ttfb_ms = hop_ttfb
            redirect_chain = chain
            initial_status_code = init_s
            redirect_target_status = target_s
            transport_error = trans_err
            final_url = dest_url

            if r is not None:
                headers = r.headers
                server_header = headers.get("server", "")
                cache_status = _extract_cache_header(headers)
                content_type = headers.get("content-type", "")
                x_robots_tag = headers.get("x-robots-tag", "")

                if initial_status_code in (301, 302, 303, 307, 308):
                    status_code = initial_status_code
                    html = ""
                else:
                    status_code = r.status_code
                    html = r.text
            else:
                status_code = initial_status_code

            if status_code in (403, 429, 503) or (html and _detect_challenge(html)):
                logger.debug("Fast HTTP encountered challenge (HTTP %d) on %s; escalating", status_code, url)
                html = ""
                status_code = 0
        except Exception as fast_exc:
            logger.debug("Fast HTTP error on %s: %s; escalating to FlareSolverr", url, fast_exc)
            html = ""

    # Step 2: Escalate to FlareSolverr ONLY if HTML is empty AND status was not a definitive status code (e.g. not 200, 301, 302, 303, 307, 308, 404, 410)
    if not html and status_code not in (200, 301, 302, 303, 307, 308, 404, 410):
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
                solved_cookies = fs_resp.cookies
                solved_user_agent = fs_resp.user_agent
                fetch_method = "flaresolverr"
        except Exception as exc:
            logger.warning("FlareSolverr request to %s failed: %s", url, exc)
            error_msg = str(exc)
            status_code = 500
        finally:
            client.close()

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    page_size_bytes = len(html.encode("utf-8")) if html else 0

    # Step 3: Parse according to adapter
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
            "solved_cookies": solved_cookies,
            "solved_user_agent": solved_user_agent,
            "response_time_ms": elapsed_ms,
        }

    elif mode == "competitor":
        is_challenge = _detect_challenge(html) if html else False
        is_html = (not content_type) or any(ht in content_type.lower() for ht in ("text/html", "application/xhtml+xml"))

        # If it's a redirect or non-HTML content, do not parse HTML body
        effective_html = html if (status_code == 200 and is_html and initial_status_code not in (301, 302, 303, 307, 308)) else ""

        comp_adapter = CompetitorSiteAdapter()
        page_rec, ev_list, discovered_links, link_edges = comp_adapter.parse(
            url=url,
            html=effective_html,
            http_status=status_code,
            final_url=final_url,
            response_time_ms=elapsed_ms,
            page_size_bytes=page_size_bytes,
            initial_status_code=initial_status_code or status_code,
            redirect_chain=redirect_chain,
            redirect_target_status=redirect_target_status,
            content_type=content_type,
            transport_error=transport_error or "",
            fetch_method=fetch_method,
            ttfb_ms=ttfb_ms,
            server_header=server_header,
            cache_status=cache_status,
            x_robots_tag=x_robots_tag,
            in_sitemap=job.get("in_sitemap", False),
            preserve_trailing_slash=job.get("preserve_trailing_slash", True),
        )

        # If redirect occurred, queue the final destination URL if internal
        if initial_status_code in (301, 302, 303, 307, 308) and final_url:
            target_domain = job.get("domain") or urlparse(url).netloc.lower()
            preserve_ts = job.get("preserve_trailing_slash", True)
            norm_final = normalize_url(final_url, preserve_trailing_slash=preserve_ts) or final_url
            if is_internal_url(norm_final, target_domain, preserve_trailing_slash=preserve_ts):
                if norm_final not in discovered_links:
                    discovered_links.append(norm_final)

        # Definitive status codes: 200, 301, 302, 303, 307, 308, 404, 410 (never retried!)
        is_success = (status_code in (200, 301, 302, 303, 307, 308, 404, 410)) and not is_challenge

        if transport_error:
            page_rec.transport_error_type = classify_transport_error(transport_error)

        # Run per-page SEO issues check
        cfg = audit_cfg or SEOAuditConfig()
        issues = audit_page(page_rec, cfg)
        page_rec.issues = issues

        return {
            "success": is_success,
            "status_code": status_code,
            "error": error_msg or (None if is_success else ("Cloudflare challenge detected" if is_challenge else "HTTP error")),
            "domain": job.get("domain"),
            "depth": job.get("depth", 0),
            "internal_urls": discovered_links,
            "link_edges": [e.model_dump() for e in link_edges],
            "issues": [i.model_dump() for i in issues],
            "result": page_rec.model_dump(),
            "evidence": [e.model_dump() for e in ev_list],
            "solved_cookies": solved_cookies,
            "solved_user_agent": solved_user_agent,
            "response_time_ms": elapsed_ms,
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
            "solved_cookies": solved_cookies,
            "solved_user_agent": solved_user_agent,
            "response_time_ms": elapsed_ms,
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

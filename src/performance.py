"""Honest performance measurement (WS6).

Crawl timings are distorted by crawler concurrency and network path. This module adds
two optional, clearly-labelled sources:

- run_timing_sample(): a sequential (concurrency 1), cache-busted TTFB sample of N URLs.
- fetch_psi_field_data(): Chrome UX Report field data (TTFB, LCP, INP, CLS) via the
  PageSpeed Insights API, for the origin and N sample URLs.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from src.transport import classify_transport_error

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_METRICS = {
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "ttfb_ms",
    "LARGEST_CONTENTFUL_PAINT_MS": "lcp_ms",
    "INTERACTION_TO_NEXT_PAINT": "inp_ms",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls",
}


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0-100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(-(-pct * len(ordered) // 100)) - 1))
    return float(ordered[idx])


def _cache_bust(url: str, token: str) -> str:
    parts = urlparse(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("_lsc_cb", token))
    return urlunparse(parts._replace(query=urlencode(query)))


def select_sample_urls(pages: list[dict[str, Any]], links: list[dict[str, Any]], n: int) -> list[str]:
    """Deterministic sample: 200 HTML pages ordered by inbound internal links, then URL."""
    inbound: dict[str, int] = {}
    for e in links:
        t = (e.get("target_url") or "").rstrip("/")
        inbound[t] = inbound.get(t, 0) + 1
    candidates = [
        p["url"]
        for p in pages
        if int(p.get("status_code") or 0) == 200
        and (not p.get("content_type") or "html" in (p.get("content_type") or "").lower())
    ]
    candidates.sort(key=lambda u: (-inbound.get(u.rstrip("/"), 0), u))
    return candidates[: max(0, n)]


def run_timing_sample(
    urls: list[str],
    client: httpx.Client | None = None,
    headers: dict[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    auth: httpx.Auth | None = None,
) -> dict[str, Any]:
    """Fetch URLs one at a time with a cache-busting query string and record TTFB.

    TTFB is measured until response headers arrive (the body is not read).
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0, follow_redirects=False, headers=headers or {}, auth=auth)
    samples: list[dict[str, Any]] = []
    try:
        run_token = str(int(time.time()))
        for i, url in enumerate(urls):
            busted = _cache_bust(url, f"{run_token}-{i}")
            t0 = clock()
            try:
                with client.stream("GET", busted) as resp:
                    ttfb = int((clock() - t0) * 1000)
                    samples.append({
                        "url": url,
                        "status_code": resp.status_code,
                        "ttfb_ms": ttfb,
                        "cache_status": resp.headers.get("cf-cache-status", "") or resp.headers.get("x-cache", ""),
                        "error_type": "",
                    })
            except Exception as exc:  # transport failure: record, do not time
                samples.append({
                    "url": url,
                    "status_code": 0,
                    "ttfb_ms": None,
                    "cache_status": "",
                    "error_type": classify_transport_error(exc),
                })
    finally:
        if own_client:
            client.close()

    ttfbs = [float(s["ttfb_ms"]) for s in samples if s["ttfb_ms"] is not None]
    return {
        "method": "sequential, concurrency 1, cache-busting query string",
        "sample_size": len(samples),
        "measured": len(ttfbs),
        "median_ttfb_ms": round(statistics.median(ttfbs), 1) if ttfbs else None,
        "p95_ttfb_ms": round(percentile(ttfbs, 95), 1) if ttfbs else None,
        "samples": samples,
    }


def _parse_psi_metrics(block: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    metrics = (block or {}).get("metrics") or {}
    for psi_key, name in PSI_METRICS.items():
        m = metrics.get(psi_key)
        if not m or "percentile" not in m:
            continue
        val = m["percentile"]
        if name == "cls":
            val = round(float(val) / 100.0, 2)  # PSI reports CLS x100
        out[name] = {"p75": val, "category": m.get("category", "")}
    if block and block.get("overall_category"):
        out["overall_category"] = block["overall_category"]
    return out


def fetch_psi_field_data(
    api_key: str,
    urls: list[str],
    client: httpx.Client | None = None,
    strategy: str = "mobile",
) -> dict[str, Any]:
    """Pull CrUX field data from the PageSpeed Insights API for the origin and sample URLs."""
    if not api_key:
        return {"measured": False, "reason": "no PSI API key"}
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=90.0)
    result: dict[str, Any] = {"measured": True, "strategy": strategy, "origin": {}, "urls": [], "errors": []}
    try:
        for url in urls:
            try:
                resp = client.get(PSI_ENDPOINT, params={"url": url, "key": api_key, "strategy": strategy, "category": "performance"})
                if resp.status_code != 200:
                    result["errors"].append({"url": url, "error": f"HTTP {resp.status_code}"})
                    continue
                data = resp.json()
            except Exception as exc:
                result["errors"].append({"url": url, "error": classify_transport_error(exc) or str(exc)})
                continue
            if not result["origin"]:
                result["origin"] = _parse_psi_metrics(data.get("originLoadingExperience"))
            result["urls"].append({"url": url, "metrics": _parse_psi_metrics(data.get("loadingExperience"))})
    finally:
        if own_client:
            client.close()
    if not result["origin"] and not result["urls"]:
        result["measured"] = False
        result["reason"] = "PSI returned no field data"
    return result

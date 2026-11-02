"""Sitemap and robots.txt parser and URL canonicalizer."""

from __future__ import annotations

import gzip
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

TRACKING_PARAM_PATTERNS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "msclkid",
    "mc_eid",
    "ref",
}

IGNORED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".xml",
    ".json",
}


def normalize_url(url: str, base_url: str | None = None) -> str | None:
    """Normalize and canonicalize URL: lowercase host, strip fragment, strip tracking params, trailing slashes."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None

    if base_url:
        url = urljoin(base_url, url)

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # Normalize default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = parsed.path or "/"
    # Check ignored extensions
    path_lower = path.lower()
    for ext in IGNORED_EXTENSIONS:
        if path_lower.endswith(ext):
            return None

    # Filter tracking query parameters
    query_parts = []
    if parsed.query:
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            if k.lower() not in TRACKING_PARAM_PATTERNS:
                query_parts.append((k, v))
    query_parts.sort(key=lambda x: x[0])
    query = urlencode(query_parts) if query_parts else ""

    # Normalize trailing slash: collapse multi-slashes
    path = re.sub(r"/{2,}", "/", path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, "", query, ""))


def is_internal_url(url: str, target_domain: str) -> bool:
    """Check if URL belongs to the target domain (matching or subdomain/apex)."""
    norm_url = normalize_url(url)
    if not norm_url:
        return False
    parsed = urlparse(norm_url)
    netloc = parsed.netloc.lower()
    target_clean = target_domain.lower().replace("https://", "").replace("http://", "").strip("/")
    if target_clean.startswith("www."):
        target_apex = target_clean[4:]
    else:
        target_apex = target_clean

    return (
        netloc == target_clean
        or netloc == f"www.{target_clean}"
        or netloc == target_apex
        or netloc.endswith(f".{target_apex}")
    )


class SitemapDiscovery:
    """Discovers and parses sitemaps recursively up to max_urls."""

    def __init__(
        self,
        target_domain: str,
        http_client: httpx.Client | None = None,
        max_urls: int = 1000,
    ) -> None:
        clean_domain = target_domain.lower().replace("https://", "").replace("http://", "").strip("/")
        self.target_domain = clean_domain
        self.base_url = f"https://{clean_domain}/"
        self._client = http_client or httpx.Client(timeout=15.0, follow_redirects=True)
        self._owns_client = http_client is None
        self.max_urls = max_urls

    def discover_urls(self) -> list[str]:
        """Fetch robots.txt for sitemap declarations, then check common sitemap endpoints."""
        sitemap_candidates: list[str] = []

        # 1. Fetch robots.txt
        try:
            robots_url = urljoin(self.base_url, "/robots.txt")
            resp = self._client.get(robots_url)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        if sm_url and sm_url not in sitemap_candidates:
                            sitemap_candidates.append(sm_url)
        except Exception as exc:
            logger.debug("Could not fetch robots.txt for %s: %s", self.target_domain, exc)

        # 2. Add common default candidates
        defaults = [
            urljoin(self.base_url, "/sitemap.xml"),
            urljoin(self.base_url, "/sitemap_index.xml"),
            urljoin(self.base_url, "/wp-sitemap.xml"),
        ]
        for d in defaults:
            if d not in sitemap_candidates:
                sitemap_candidates.append(d)

        discovered: set[str] = set()
        visited_sitemaps: set[str] = set()

        for sm_url in sitemap_candidates:
            if len(discovered) >= self.max_urls:
                break
            self._fetch_and_parse_sitemap(sm_url, discovered, visited_sitemaps)

        if self._owns_client:
            self._client.close()

        # Always ensure base_url is present
        normalized_root = normalize_url(self.base_url)
        if normalized_root:
            discovered.add(normalized_root)

        # Retain only internal URLs up to max_urls
        valid = [u for u in discovered if is_internal_url(u, self.target_domain)]
        return valid[: self.max_urls]

    def _fetch_and_parse_sitemap(
        self,
        sitemap_url: str,
        results: set[str],
        visited_sitemaps: set[str],
    ) -> None:
        if sitemap_url in visited_sitemaps or len(results) >= self.max_urls:
            return
        visited_sitemaps.add(sitemap_url)

        try:
            resp = self._client.get(sitemap_url)
            if resp.status_code != 200:
                return

            content = resp.content
            if sitemap_url.endswith(".gz") or content.startswith(b"\x1f\x8b"):
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass

            xml_str = content.decode("utf-8", errors="ignore")
            # Clean XML of namespaces, prefixes and schema locations for reliable parsing
            xml_clean = re.sub(r'<([a-zA-Z0-9_-]+):', r'<\1_', xml_str)
            xml_clean = re.sub(r'</([a-zA-Z0-9_-]+):', r'</\1_', xml_clean)
            xml_clean = re.sub(r'\sxmlns(?::[a-zA-Z0-9_-]+)?="[^"]*"', '', xml_clean)
            xml_clean = re.sub(r'\sxsi:[a-zA-Z0-9_-]+="[^"]*"', '', xml_clean)
            root = ET.fromstring(xml_clean)

            # Check if sitemap index
            if root.tag.endswith("sitemapindex"):
                for sitemap_elem in root.findall(".//sitemap"):
                    loc = sitemap_elem.find("loc")
                    if loc is not None and loc.text:
                        sub_sm = loc.text.strip()
                        self._fetch_and_parse_sitemap(sub_sm, results, visited_sitemaps)
                        if len(results) >= self.max_urls:
                            break
            # Or standard urlset
            elif root.tag.endswith("urlset"):
                for url_elem in root.findall(".//url"):
                    loc = url_elem.find("loc")
                    if loc is not None and loc.text:
                        norm = normalize_url(loc.text.strip())
                        if norm and is_internal_url(norm, self.target_domain):
                            results.add(norm)
                            if len(results) >= self.max_urls:
                                break
        except Exception as exc:
            logger.debug("Failed parsing sitemap at %s: %s", sitemap_url, exc)

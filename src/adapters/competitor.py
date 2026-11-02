"""CompetitorSiteAdapter: On-Site & Core 30 technical analysis parser."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from src.models import (
    AnalyticsAndTracking,
    EvidenceRecord,
    FoundNAP,
    ImageAuditSummary,
    InternalLinkEdge,
    PageTechnicalRecord,
)
from src.sitemap import is_internal_url, normalize_url

PHONE_PATTERN = re.compile(
    r"(?:(?:\+?1\s*(?:[.-]\s*)?)?(?:\(\s*([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9])\s*\)|([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9]))\s*(?:[.-]\s*)?)?([2-9]1[02-9]|[2-9][02-9]1|[2-9][02-9]{2})\s*(?:[.-]\s*)?([0-9]{4})",
    re.IGNORECASE,
)

GA4_PATTERN = re.compile(r"G-[A-Z0-9]{10}")
GTM_PATTERN = re.compile(r"GTM-[A-Z0-9]+")


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _classify_link_placement(node: Any) -> str:
    """Classify link placement in DOM tree: navigation, footer, image, or body."""
    # Check if link wraps only an image
    if node.css_first("img") and not node.text().strip():
        return "image"

    curr = node.parent
    while curr is not None and curr.tag != "body" and curr.tag != "html":
        tag = curr.tag
        if tag == "footer":
            return "footer"
        if tag in ("nav", "header"):
            return "navigation"

        attrs = curr.attributes
        class_str = (attrs.get("class") or "").lower()
        id_str = (attrs.get("id") or "").lower()
        combined = f"{class_str} {id_str}"

        if "footer" in combined:
            return "footer"
        if any(k in combined for k in ("nav", "menu", "header", "navbar", "topbar")):
            return "navigation"

        curr = curr.parent

    return "body"


def _extract_analytics(html: str) -> AnalyticsAndTracking:
    """Detect tracking, analytics, call tracking, and review widgets via regex."""
    a = AnalyticsAndTracking()
    lower_html = html.lower()

    # GA4 & GTM
    ga4_m = GA4_PATTERN.search(html)
    if ga4_m:
        a.ga4_id = ga4_m.group(0)
        a.gtag = True
        a.google_analytics = True

    gtm_m = GTM_PATTERN.search(html)
    if gtm_m:
        a.gtm_id = gtm_m.group(0)

    if any(pat in lower_html for pat in ("gtag(", "ga(", "googleanalyticsobject", "google-analytics.com", "googletagmanager.com")):
        a.google_analytics = True

    if "fbq(" in lower_html or "facebook.com/tr" in lower_html or "connect.facebook.net" in lower_html:
        a.facebook_pixel = True

    if "clarity.ms" in lower_html or "clarity(" in lower_html:
        a.microsoft_clarity = True

    if "hotjar.com" in lower_html or "hj(" in lower_html:
        a.hotjar = True

    if "mixpanel.com" in lower_html or "mixpanel.track" in lower_html:
        a.mixpanel = True

    # Local SEO Call-Tracking
    if "callrail.com" in lower_html or "cdn.callrail.com" in lower_html or "swap.js" in lower_html:
        a.callrail = True

    if "whatconverts.com" in lower_html or "ksrnd.com" in lower_html:
        a.whatconverts = True

    # Local Field Service & Lead/Review Widgets
    if "servicetitan.com" in lower_html or "st-widget" in lower_html:
        a.servicetitan = True

    if "housecallpro.com" in lower_html or "housecall_pro" in lower_html:
        a.housecall_pro = True

    if "podium.com" in lower_html or "connect.podium.com" in lower_html:
        a.podium = True

    if "birdeye.com" in lower_html or "birdeye-widget" in lower_html:
        a.birdeye = True

    return a


class CompetitorSiteAdapter:
    """Analyzes competitor page structure, heading hierarchy, Schema.org, on-page NAP, and general SEO metrics."""

    def parse(
        self,
        url: str,
        html: str,
        http_status: int = 200,
        final_url: str | None = None,
        raw_pre_js_html: str | None = None,
        response_time_ms: int = 0,
        page_size_bytes: int = 0,
    ) -> tuple[PageTechnicalRecord, list[EvidenceRecord], list[str], list[InternalLinkEdge]]:
        """Parse competitor page.

        Returns:
            (record, evidence_list, all_discovered_internal_urls, link_edges)
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        parsed_url = urlparse(url)
        base_domain = parsed_url.netloc.lower()
        effective_final_url = final_url or url
        calc_size = page_size_bytes or len(html.encode("utf-8")) if html else 0

        if not html:
            empty_record = PageTechnicalRecord(
                url=url,
                final_url=effective_final_url,
                status_code=http_status,
                title="",
                meta_description="",
                canonical_url="",
                h1=[],
                h2=[],
                h3=[],
                word_count=0,
                internal_outlinks_count=0,
                external_outlinks_count=0,
                internal_urls=[],
                schema_types=[],
                schema_jsonld=[],
                nap_found=FoundNAP(),
                is_ssr=False,
                response_time_ms=response_time_ms,
                page_size_bytes=calc_size,
            )
            evidence = [
                EvidenceRecord(
                    claim_type="technical_page_audit",
                    source_url=url,
                    timestamp=timestamp,
                    http_status=http_status,
                    dom_selector="head",
                    verbatim_text=f"Empty content - HTTP {http_status}",
                    hash_sha256=compute_sha256(f"Empty content - HTTP {http_status}"),
                )
            ]
            return empty_record, evidence, [], []

        tree = HTMLParser(html)

        # 1. Metadata, Directives & International
        title = ""
        title_node = tree.css_first("title")
        if title_node and title_node.text():
            title = title_node.text().strip()

        meta_desc = ""
        desc_node = tree.css_first("meta[name='description']") or tree.css_first("meta[property='og:description']")
        if desc_node and desc_node.attributes.get("content"):
            meta_desc = desc_node.attributes["content"].strip()

        canonical_url = ""
        canon_node = tree.css_first("link[rel='canonical']")
        if canon_node and canon_node.attributes.get("href"):
            canonical_url = canon_node.attributes["href"].strip()

        # Language, charset, viewport, robots, generator
        html_tag = tree.css_first("html")
        lang = (html_tag.attributes.get("lang") or "").strip() if html_tag else ""

        charset = ""
        charset_node = tree.css_first("meta[charset]")
        if charset_node:
            charset = charset_node.attributes.get("charset", "")
        else:
            http_equiv = tree.css_first("meta[http-equiv='Content-Type'], meta[http-equiv='content-type']")
            if http_equiv and "charset=" in (http_equiv.attributes.get("content") or "").lower():
                charset = http_equiv.attributes["content"].lower().split("charset=")[-1].strip()

        viewport = ""
        vp_node = tree.css_first("meta[name='viewport']")
        if vp_node:
            viewport = vp_node.attributes.get("content", "").strip()

        meta_robots = ""
        robots_node = tree.css_first("meta[name='robots']")
        if robots_node:
            meta_robots = robots_node.attributes.get("content", "").strip()

        generator = ""
        gen_node = tree.css_first("meta[name='generator']")
        if gen_node:
            generator = gen_node.attributes.get("content", "").strip()

        # Hreflang
        hreflang_list: list[dict[str, str]] = []
        for hl in tree.css("link[rel='alternate'][hreflang]"):
            hl_code = hl.attributes.get("hreflang", "").strip()
            hl_href = hl.attributes.get("href", "").strip()
            if hl_code and hl_href:
                hreflang_list.append({"lang": hl_code, "url": hl_href})

        # Social Meta: OpenGraph & Twitter
        og_tags: dict[str, str] = {}
        for og_el in tree.css("meta[property^='og:']"):
            prop = og_el.attributes.get("property", "").replace("og:", "").strip()
            cnt = og_el.attributes.get("content", "").strip()
            if prop and cnt:
                og_tags[prop] = cnt

        twitter_tags: dict[str, str] = {}
        for tw_el in tree.css("meta[name^='twitter:']"):
            name = tw_el.attributes.get("name", "").replace("twitter:", "").strip()
            cnt = tw_el.attributes.get("content", "").strip()
            if name and cnt:
                twitter_tags[name] = cnt

        # 2. Heading hierarchy in DOM order (H1, H2, H3)
        h1_list: list[str] = []
        h2_list: list[str] = []
        h3_list: list[str] = []
        for tag in tree.css("h1, h2, h3"):
            text = tag.text()
            if text:
                cleaned = " ".join(text.split()).strip()
                if tag.tag == "h1":
                    h1_list.append(cleaned)
                elif tag.tag == "h2":
                    h2_list.append(cleaned)
                elif tag.tag == "h3":
                    h3_list.append(cleaned)

        # 3. Schema.org graph extraction (JSON-LD + Microdata)
        schema_jsonld: list[dict[str, Any]] = []
        schema_types: set[str] = set()

        for script in tree.css("script[type='application/ld+json']"):
            try:
                content = script.text()
                if content:
                    data = json.loads(content)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if isinstance(item, dict):
                            schema_jsonld.append(item)
                            stype = item.get("@type")
                            if isinstance(stype, list):
                                schema_types.update(str(t) for t in stype)
                            elif isinstance(stype, str):
                                schema_types.add(stype)
                            graph = item.get("@graph")
                            if isinstance(graph, list):
                                for g_item in graph:
                                    if isinstance(g_item, dict):
                                        schema_jsonld.append(g_item)
                                        gt = g_item.get("@type")
                                        if isinstance(gt, list):
                                            schema_types.update(str(t) for t in gt)
                                        elif isinstance(gt, str):
                                            schema_types.add(gt)
            except Exception:
                continue

        for item in tree.css("[itemtype]"):
            itype = item.attributes.get("itemtype", "")
            if itype:
                type_name = itype.split("/")[-1]
                if type_name:
                    schema_types.add(type_name)

        # 4. Image & Accessibility Audit
        total_images = 0
        missing_alt = 0
        decorative_alt = 0
        missing_dims = 0
        sample_missing_alts: list[str] = []

        for img in tree.css("img"):
            total_images += 1
            src_val = img.attributes.get("src", "").strip()
            # Distinguish missing alt attribute from alt=""
            if "alt" not in img.attributes:
                missing_alt += 1
                if src_val and len(sample_missing_alts) < 10:
                    sample_missing_alts.append(urljoin(url, src_val))
            elif (img.attributes.get("alt") or "").strip() == "":
                decorative_alt += 1

            has_width = bool(img.attributes.get("width"))
            has_height = bool(img.attributes.get("height"))
            if not (has_width and has_height):
                missing_dims += 1

        img_audit = ImageAuditSummary(
            total_images=total_images,
            missing_alt_count=missing_alt,
            decorative_alt_count=decorative_alt,
            missing_dimensions_count=missing_dims,
            missing_alt_sample_urls=sample_missing_alts,
        )

        # 5. Outlink Analysis, Link Placement & Graph Extraction
        internal_count = 0
        external_count = 0
        nofollow_count = 0
        discovered_internal: list[str] = []
        link_edges: list[InternalLinkEdge] = []
        seen_hrefs: set[str] = set()
        placements: dict[str, int] = defaultdict(int)

        for a_tag in tree.css("a[href]"):
            href = a_tag.attributes.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue

            rel_val = (a_tag.attributes.get("rel") or "").lower()
            is_nofollow = "nofollow" in rel_val
            if is_nofollow:
                nofollow_count += 1

            normalized_link = normalize_url(href, base_url=url)
            if not normalized_link:
                continue

            if is_internal_url(normalized_link, base_domain):
                internal_count += 1
                placement = _classify_link_placement(a_tag)
                placements[placement] += 1

                anchor = " ".join(a_tag.text().split()).strip() if a_tag.text() else ""
                edge = InternalLinkEdge(
                    source_url=url,
                    target_url=normalized_link,
                    anchor_text=anchor[:100],
                    placement=placement,
                    is_nofollow=is_nofollow,
                )
                link_edges.append(edge)

                if normalized_link not in seen_hrefs:
                    seen_hrefs.add(normalized_link)
                    discovered_internal.append(normalized_link)
            else:
                external_count += 1

        # 6. Word count & SSR Validation
        body = tree.css_first("body")
        text_content = body.text() if body else ""
        words = [w for w in re.findall(r"\b\w+\b", text_content) if len(w) > 1]
        word_count = len(words)

        is_ssr = True
        if raw_pre_js_html:
            raw_tree = HTMLParser(raw_pre_js_html)
            raw_body = raw_tree.css_first("body")
            raw_text = raw_body.text() if raw_body else ""
            raw_words = len([w for w in re.findall(r"\b\w+\b", raw_text) if len(w) > 1])
            if word_count > 100 and (raw_words / max(word_count, 1)) < 0.15:
                is_ssr = False

        # 7. NAP Detection
        found_phones: set[str] = set()
        found_addresses: set[str] = set()

        for tel in tree.css("a[href^='tel:']"):
            href = tel.attributes.get("href", "")
            clean = re.sub(r"[^\d+]", "", href.replace("tel:", "").split("?")[0])
            if clean:
                found_phones.add(clean)

        for phone_match in PHONE_PATTERN.finditer(html):
            matched = phone_match.group(0).strip()
            if len(re.sub(r"\D", "", matched)) >= 10:
                found_phones.add(matched)

        footer = tree.css_first("footer")
        if footer:
            for addr_tag in footer.css("address, .address"):
                txt = " ".join(addr_tag.text().split()).strip()
                if txt:
                    found_addresses.add(txt)

        # 8. Analytics & Local Marketing Stack Detection
        analytics_info = _extract_analytics(html)

        record = PageTechnicalRecord(
            url=url,
            final_url=effective_final_url,
            status_code=http_status,
            title=title,
            meta_description=meta_desc,
            canonical_url=canonical_url,
            h1=h1_list,
            h2=h2_list,
            h3=h3_list,
            word_count=word_count,
            internal_outlinks_count=internal_count,
            external_outlinks_count=external_count,
            internal_urls=discovered_internal[:50],
            schema_types=sorted(list(schema_types)),
            schema_jsonld=schema_jsonld,
            nap_found=FoundNAP(
                phones=sorted(list(found_phones))[:10],
                addresses=sorted(list(found_addresses))[:5],
            ),
            is_ssr=is_ssr,
            lang=lang,
            charset=charset,
            viewport=viewport,
            meta_robots=meta_robots,
            generator=generator,
            hreflang=hreflang_list,
            og_tags=og_tags,
            twitter_tags=twitter_tags,
            response_time_ms=response_time_ms,
            page_size_bytes=calc_size,
            images=img_audit,
            analytics=analytics_info,
            nofollow_outlinks_count=nofollow_count,
            link_placements=dict(placements),
        )

        evidence = [
            EvidenceRecord(
                claim_type="technical_page_audit",
                source_url=url,
                timestamp=timestamp,
                http_status=http_status,
                dom_selector="head > title",
                verbatim_text=f"Title: {title} | Words: {word_count} | H1s: {len(h1_list)} | Canonical: {canonical_url}",
                hash_sha256=compute_sha256(title or url),
            )
        ]

        return record, evidence, discovered_internal, link_edges

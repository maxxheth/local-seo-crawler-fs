"""CompetitorSiteAdapter: On-Site & Core 30 technical analysis parser."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from src.models import EvidenceRecord, FoundNAP, PageTechnicalRecord

PHONE_PATTERN = re.compile(
    r"(?:(?:\+?1\s*(?:[.-]\s*)?)?(?:\(\s*([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9])\s*\)|([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9]))\s*(?:[.-]\s*)?)?([2-9]1[02-9]|[2-9][02-9]1|[2-9][02-9]{2})\s*(?:[.-]\s*)?([0-9]{4})",
    re.IGNORECASE,
)


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CompetitorSiteAdapter:
    """Analyzes competitor page structure, heading hierarchy, Schema.org and on-page NAP."""

    def parse(
        self,
        url: str,
        html: str,
        http_status: int = 200,
        final_url: str | None = None,
        raw_pre_js_html: str | None = None,
    ) -> tuple[PageTechnicalRecord, list[EvidenceRecord]]:
        timestamp = datetime.now(timezone.utc).isoformat()
        parsed_url = urlparse(url)
        base_domain = parsed_url.netloc.lower()
        effective_final_url = final_url or url

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
            return empty_record, evidence

        tree = HTMLParser(html)

        # 1. Metadata
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
                            # Handle @graph
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

        # Microdata types (itemtype)
        for item in tree.css("[itemtype]"):
            itype = item.attributes.get("itemtype", "")
            if itype:
                type_name = itype.split("/")[-1]
                if type_name:
                    schema_types.add(type_name)

        # 4. Outlink analysis & Internal Link Discovery
        internal_count = 0
        external_count = 0
        discovered_internal: list[str] = []
        seen_hrefs: set[str] = set()

        for a_tag in tree.css("a[href]"):
            href = a_tag.attributes.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full_link = urljoin(url, href).split("#")[0]
            link_parsed = urlparse(full_link)
            link_domain = link_parsed.netloc.lower()

            if link_domain == base_domain or not link_domain:
                internal_count += 1
                if full_link not in seen_hrefs and (link_parsed.scheme in ("http", "https")):
                    seen_hrefs.add(full_link)
                    discovered_internal.append(full_link)
            else:
                external_count += 1

        # 5. Word count
        body = tree.css_first("body")
        text_content = body.text() if body else ""
        words = [w for w in re.findall(r"\b\w+\b", text_content) if len(w) > 1]
        word_count = len(words)

        # 6. SSR Validation
        is_ssr = True
        if raw_pre_js_html:
            raw_tree = HTMLParser(raw_pre_js_html)
            raw_body = raw_tree.css_first("body")
            raw_text = raw_body.text() if raw_body else ""
            raw_words = len([w for w in re.findall(r"\b\w+\b", raw_text) if len(w) > 1])
            # If raw HTML has < 15% of the rendered words, it is client-rendered only (CSR)
            if word_count > 100 and (raw_words / max(word_count, 1)) < 0.15:
                is_ssr = False

        # 7. NAP Detection in footer/header/contact sections
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

        # Addresses from semantic elements or footer
        footer = tree.css_first("footer")
        if footer:
            for addr_tag in footer.css("address, .address"):
                txt = " ".join(addr_tag.text().split()).strip()
                if txt:
                    found_addresses.add(txt)

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

        return record, evidence

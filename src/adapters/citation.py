"""CitationAdapter: Directory & NAP verification parser."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from src.models import CitationItem, EvidenceRecord, ObservedNAP

# Common phone regex matching various US/international formats
PHONE_REGEX = re.compile(
    r"(?:(?:\+?1\s*(?:[.-]\s*)?)?(?:\(\s*([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9])\s*\)|([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9]))\s*(?:[.-]\s*)?)?([2-9]1[02-9]|[2-9][02-9]1|[2-9][02-9]{2})\s*(?:[.-]\s*)?([0-9]{4})(?:\s*(?:#|x\.?|ext\.?|extension)\s*(\d+))?",
    re.IGNORECASE,
)

# Zip code regex
ZIP_REGEX = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def normalize_phone(raw_phone: str) -> str:
    """Normalize phone number to digits or standard formatted string."""
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw_phone.strip()


def compute_sha256(text: str) -> str:
    """Compute deterministic SHA-256 hash for EVD-01 evidence."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CitationAdapter:
    """Extracts NAP and verification data from local directory pages."""

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform.lower() if platform else "generic"

    def parse(
        self,
        url: str,
        html: str,
        http_status: int,
        label: str | None = None,
        expected_business: str | None = None,
        expected_address: str | None = None,
        expected_phone: str | None = None,
        proxy_port: int | None = None,
    ) -> tuple[CitationItem, EvidenceRecord]:
        """Parse directory HTML into CitationItem and EvidenceRecord."""
        timestamp = datetime.now(timezone.utc).isoformat()
        platform_name = self.platform or urlparse(url).netloc.replace("www.", "").split(".")[0]
        effective_label = label or platform_name.capitalize()

        # 1. Detect challenge / status resolution
        status_state = self._detect_status(html, http_status)

        if status_state in ("blocked", "captcha", "not_found", "error") or not html:
            empty_item = CitationItem(
                platform=platform_name,
                label=effective_label,
                url=url,
                status=status_state,
                nap_match="unknown",
                observed=None,
                confidence="low",
                evidence_snippet=f"HTTP {http_status} - Status: {status_state}",
                proxy_port=proxy_port,
            )
            empty_evidence = EvidenceRecord(
                claim_type="citation_verification",
                source_url=url,
                timestamp=timestamp,
                http_status=http_status,
                dom_selector="body",
                verbatim_text=f"HTTP {http_status} - Status: {status_state}",
                hash_sha256=compute_sha256(empty_item.evidence_snippet),
            )
            return empty_item, empty_evidence

        # 2. Extract DOM data via selectolax
        tree = HTMLParser(html)
        nap_data = self._extract_nap(tree, html)

        name = nap_data["name"]
        address = nap_data["address"]
        primary_phone = nap_data["primary_phone"]
        secondary_phones = nap_data["secondary_phones"]
        website_url = nap_data["website_url"]
        operating_hours = nap_data["operating_hours"]
        badges = nap_data["badges"]
        selector = nap_data["selector"]
        evidence_snippet = nap_data["snippet"]

        observed = ObservedNAP(
            name=name,
            address=address,
            phone=primary_phone,
        )

        # 3. Assess NAP match against expected values
        nap_match = self._compare_nap(observed, expected_business, expected_address, expected_phone)
        confidence = "high" if (observed.name and observed.phone) else ("medium" if observed.name else "low")

        citation_item = CitationItem(
            platform=platform_name,
            label=effective_label,
            url=url,
            status=status_state,
            nap_match=nap_match,
            observed=observed,
            confidence=confidence,
            evidence_snippet=evidence_snippet,
            proxy_port=proxy_port,
            secondary_phones=secondary_phones,
            website_url=website_url,
            operating_hours=operating_hours,
            badges=badges,
        )

        evidence_record = EvidenceRecord(
            claim_type="citation_verification",
            source_url=url,
            timestamp=timestamp,
            http_status=http_status,
            dom_selector=selector,
            verbatim_text=evidence_snippet,
            hash_sha256=compute_sha256(evidence_snippet),
        )

        return citation_item, evidence_record

    def _detect_status(self, html: str, status_code: int) -> str:
        """Detect live, blocked, captcha, or not_found state."""
        if status_code == 404:
            return "not_found"
        if status_code in (403, 503):
            return "blocked"
        if not html:
            return "error"

        lower_html = html.lower()
        if "just a moment..." in lower_html or "cf-browser-verification" in lower_html or "challenge-running" in lower_html:
            return "captcha"
        if "access denied" in lower_html or "attention required! | cloudflare" in lower_html:
            return "blocked"
        if "page not found" in lower_html or "404 not found" in lower_html or "we couldn't find" in lower_html:
            return "not_found"

        return "live"

    def _extract_nap(self, tree: HTMLParser, raw_html: str) -> dict[str, Any]:
        """Extract Business Name, Address, Phones, Hours, Badges, DOM selector, and snippet."""
        name = ""
        address = ""
        primary_phone = ""
        all_phones: list[str] = []
        website_url = ""
        operating_hours = ""
        badges: list[str] = []
        selector = "body"
        snippet = ""

        # Check Schema.org JSON-LD first
        for script in tree.css("script[type='application/ld+json']"):
            try:
                data = json.loads(script.text() or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    type_str = str(item.get("@type", "")).lower()
                    if isinstance(item, dict) and any(
                        t in type_str
                        for t in ["business", "organization", "service", "plumb", "store", "restaurant", "contractor", "medical"]
                    ):
                        if not name and item.get("name"):
                            name = str(item["name"]).strip()
                        if item.get("telephone"):
                            norm = normalize_phone(str(item["telephone"]))
                            if norm not in all_phones:
                                all_phones.append(norm)
                        if not address and item.get("address"):
                            addr = item["address"]
                            if isinstance(addr, dict):
                                parts = [
                                    addr.get("streetAddress", ""),
                                    addr.get("addressLocality", ""),
                                    addr.get("addressRegion", ""),
                                    addr.get("postalCode", ""),
                                ]
                                address = ", ".join(p for p in parts if p).strip()
                            elif isinstance(addr, str):
                                address = addr.strip()
                        if not website_url and item.get("url"):
                            website_url = str(item["url"]).strip()
                        if not operating_hours and item.get("openingHours"):
                            hours_raw = item["openingHours"]
                            operating_hours = ", ".join(hours_raw) if isinstance(hours_raw, list) else str(hours_raw)
                        if name and (all_phones or address):
                            selector = "script[type='application/ld+json']"
            except Exception:
                continue

        # Extract name from primary header
        if not name:
            h1 = tree.css_first("h1")
            if h1 and h1.text():
                name = h1.text().strip()
                selector = "h1"

        if not name:
            og_title = tree.css_first("meta[property='og:title']")
            if og_title and og_title.attributes.get("content"):
                name = og_title.attributes["content"].strip()
                selector = "meta[property='og:title']"

        # Extract phones from tel: links
        for tel_link in tree.css("a[href^='tel:']"):
            href = tel_link.attributes.get("href", "")
            raw_tel = href.replace("tel:", "").split("?")[0]
            norm_tel = normalize_phone(raw_tel)
            if norm_tel and norm_tel not in all_phones:
                all_phones.append(norm_tel)
                if not selector or selector == "body":
                    selector = "a[href^='tel:']"

        # If phone still missing, search text using regex
        if not all_phones:
            for phone_match in PHONE_REGEX.finditer(raw_html):
                norm_m = normalize_phone(phone_match.group(0))
                if norm_m and norm_m not in all_phones:
                    all_phones.append(norm_m)

        if all_phones:
            primary_phone = all_phones[0]
            secondary_phones = all_phones[1:5]
        else:
            primary_phone = ""
            secondary_phones = []

        # Extract address from semantic elements
        if not address:
            addr_node = tree.css_first("address, .address, .business-address, [itemprop='address']")
            if addr_node and addr_node.text():
                address = " ".join(addr_node.text().split()).strip()
                if selector == "body":
                    selector = addr_node.tag

        # Extract website link
        if not website_url:
            web_node = tree.css_first("a[href^='http'][rel*='nofollow'], a.biz-website, a[data-testid='website-link']")
            if web_node:
                website_url = web_node.attributes.get("href", "").strip()

        # Extract badges (BBB accredited, verified owner, etc.)
        lower_html = raw_html.lower()
        if "bbb accredited" in lower_html or "accredited since" in lower_html:
            badges.append("BBB Accredited")
        if "verified owner" in lower_html or "claimed" in lower_html:
            badges.append("Verified Owner / Claimed")
        if "license verified" in lower_html or "verified license" in lower_html:
            badges.append("Verified License")

        # Extract operating hours from text if missing
        if not operating_hours:
            hours_node = tree.css_first(".hours-list, .business-hours, [itemprop='openingHours']")
            if hours_node and hours_node.text():
                operating_hours = " ".join(hours_node.text().split()).strip()

        # Construct evidence snippet
        parts = [p for p in [name, address, primary_phone] if p]
        if parts:
            snippet = " - ".join(parts)
        else:
            snippet = (name or "No NAP observed").strip()

        return {
            "name": name,
            "address": address,
            "primary_phone": primary_phone,
            "secondary_phones": secondary_phones,
            "website_url": website_url,
            "operating_hours": operating_hours,
            "badges": badges,
            "selector": selector,
            "snippet": snippet,
        }

    def _compare_nap(
        self,
        observed: ObservedNAP,
        expected_business: str | None,
        expected_address: str | None,
        expected_phone: str | None,
    ) -> str:
        """Compare observed values against expected target values."""
        if not expected_business and not expected_address and not expected_phone:
            return "match" if (observed.name or observed.phone) else "unknown"

        matches = 0
        total_checks = 0

        if expected_business:
            total_checks += 1
            if observed.name and (
                expected_business.lower() in observed.name.lower()
                or observed.name.lower() in expected_business.lower()
            ):
                matches += 1

        if expected_phone:
            total_checks += 1
            norm_exp = normalize_phone(expected_phone)
            if observed.phone and (norm_exp == observed.phone or re.sub(r"\D", "", norm_exp) == re.sub(r"\D", "", observed.phone)):
                matches += 1

        if expected_address:
            total_checks += 1
            # Check city/state/street tokens
            exp_tokens = set(re.findall(r"\w+", expected_address.lower()))
            obs_tokens = set(re.findall(r"\w+", observed.address.lower()))
            if exp_tokens and obs_tokens and len(exp_tokens.intersection(obs_tokens)) >= 2:
                matches += 1

        if matches == total_checks and total_checks > 0:
            return "match"
        elif matches > 0:
            return "partial"
        else:
            return "mismatch"

"""LocalSerpAdapter: Google Local 3-Pack, organic SERP, and PAA parser."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from src.models import EvidenceRecord, LocalPackItem, OrganicRankingItem, PAAItem, SerpCrawlArtifact


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_review_count(raw_text: str) -> int | None:
    """Parse review count supporting formats like (1.1K), (379), 124 reviews."""
    m = re.search(r"\(([\d,.]+)\s*([kKmM])?\b|\(([\d,.]+)\)", raw_text)
    if not m:
        m = re.search(r"\b([\d,.]+)\s*([kKmM])?\s+reviews?\b", raw_text, re.IGNORECASE)
    if m:
        num_str = (m.group(1) or m.group(3) or "").replace(",", "")
        multiplier = m.group(2)
        try:
            val = float(num_str)
            if multiplier and multiplier.lower() == "k":
                val *= 1000
            elif multiplier and multiplier.lower() == "m":
                val *= 1000000
            return int(val)
        except ValueError:
            return None
    return None


class LocalSerpAdapter:
    """Parses Google Local 3-Pack, organic search results, and PAA queries."""

    def parse(
        self,
        query: str,
        location: str,
        html: str,
        http_status: int = 200,
        source_url: str = "",
    ) -> tuple[SerpCrawlArtifact, list[EvidenceRecord]]:
        timestamp = datetime.now(timezone.utc).isoformat()
        tree = HTMLParser(html) if html else None

        local_pack: list[LocalPackItem] = []
        organic: list[OrganicRankingItem] = []
        paa: list[PAAItem] = []
        evidence_list: list[EvidenceRecord] = []

        if not tree or not html:
            artifact = SerpCrawlArtifact(
                query=query,
                location=location,
                checked_at=timestamp,
                local_pack=[],
                organic=[],
                paa=[],
            )
            evidence_list.append(
                EvidenceRecord(
                    claim_type="local_serp_check",
                    source_url=source_url or "https://www.google.com/search",
                    timestamp=timestamp,
                    http_status=http_status,
                    dom_selector="body",
                    verbatim_text=f"Empty SERP HTML - HTTP {http_status}",
                    hash_sha256=compute_sha256(f"{query} {location} empty"),
                )
            )
            return artifact, evidence_list

        # 1. Parse Local 3-Pack
        pack_nodes = tree.css("div[data-attrid^='kc:/local:'], .VkpGBb, div.C85Er")
        if not pack_nodes:
            pack_nodes = tree.css("div[jscontroller='AtSb']")

        seen_names: set[str] = set()
        pos = 1
        for node in pack_nodes:
            name_node = node.css_first("div[role='heading'], span.OSrXXb, .dbg0pd, span[aria-level='3']")
            if not name_node or not name_node.text():
                continue
            name = name_node.text().strip()
            if name in seen_names:
                continue
            seen_names.add(name)

            # Rating
            rating: float | None = None
            rating_node = node.css_first("span.yi40Hd, span.Y0A0hc, span[aria-label*='stars']")
            if rating_node and rating_node.text():
                try:
                    rating = float(rating_node.text().strip().split()[0])
                except ValueError:
                    pass
            if rating is None:
                # Search inside node text for rating pattern (e.g. 4.9)
                all_text = node.text()
                r_match = re.search(r"\b([1-5]\.\d)\b", all_text)
                if r_match:
                    try:
                        rating = float(r_match.group(1))
                    except ValueError:
                        pass

            # Review count
            review_count: int | None = None
            reviews_node = node.css_first("span.RDApEe, span[aria-label*='reviews']")
            if reviews_node and reviews_node.text():
                review_count = parse_review_count(reviews_node.text())
            if review_count is None:
                review_count = parse_review_count(node.text())

            # Address / submarket snippet
            snippet_node = node.css_first(".rllt__details, .wDYxhc, .dV7gna")
            snippet_text = " ".join(snippet_node.text().split()).strip() if snippet_node else ""

            local_pack.append(
                LocalPackItem(
                    position=pos,
                    name=name,
                    rating=rating,
                    review_count=review_count,
                    address_snippet=snippet_text,
                    categories=[],
                )
            )
            pos += 1
            if len(local_pack) >= 3:
                break

        # 2. Parse Organic Top 10 rankings
        # Google search results are in div.g, div.MjjYud, div.tF2Cxc
        organic_candidates = tree.css("div.g, div.tF2Cxc, div.MjjYud")
        seen_organic_urls: set[str] = set()
        org_pos = 1
        for node in organic_candidates:
            if node.css_first(".VkpGBb"):
                continue

            a_tag = node.css_first("a[href^='http']")
            if not a_tag:
                continue
            href = a_tag.attributes.get("href", "").split("?")[0]
            if "google.com" in href or href.startswith("/") or href in seen_organic_urls:
                continue

            h3 = node.css_first("h3")
            title = h3.text().strip() if h3 and h3.text() else ""
            if not title:
                continue

            snippet_node = node.css_first("div[data-sncf], div.VwiC3b, .IsZvec, div[style*='-webkit-line-clamp']")
            snippet = " ".join(snippet_node.text().split()).strip() if snippet_node else ""

            seen_organic_urls.add(href)
            organic.append(
                OrganicRankingItem(
                    position=org_pos,
                    url=href,
                    title=title,
                    snippet=snippet,
                )
            )
            org_pos += 1
            if len(organic) >= 10:
                break

        # 3. Parse "People Also Ask" (PAA)
        seen_questions: set[str] = set()
        paa_nodes = tree.css("div.related-question-pair, div[jsname='yEVEwb']")
        for pnode in paa_nodes:
            q_node = pnode.css_first("div[role='button'], span.CSkcPe")
            if not q_node or not q_node.text():
                continue
            question = q_node.text().strip()
            if question in seen_questions:
                continue
            seen_questions.add(question)

            answer_node = pnode.css_first("div.L39u4d, .hgKElc")
            answer = answer_node.text().strip() if answer_node and answer_node.text() else ""
            paa.append(PAAItem(question=question, snippet=answer))

        artifact = SerpCrawlArtifact(
            query=query,
            location=location,
            checked_at=timestamp,
            local_pack=local_pack,
            organic=organic,
            paa=paa,
        )

        evidence_snippet = f"Local Pack: {len(local_pack)} items | Organic: {len(organic)} items"
        evidence_list.append(
            EvidenceRecord(
                claim_type="local_serp_check",
                source_url=source_url or f"https://www.google.com/search?q={query}",
                timestamp=timestamp,
                http_status=http_status,
                dom_selector="div#search",
                verbatim_text=evidence_snippet,
                hash_sha256=compute_sha256(evidence_snippet),
            )
        )

        return artifact, evidence_list

"""Output normalization and deterministic JSON artifact serialization."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.models import (
    CitationItem,
    CitationsManifest,
    CitationSummary,
    EvidenceManifest,
    EvidenceRecord,
    SiteCrawlArtifact,
)


def write_json_file(file_path: Path | str, data: Any) -> str:
    """Write dictionary or pydantic data cleanly formatted to JSON."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return str(path.resolve())


class OutputManager:
    """Writes standardized output artifacts according to strict schemas."""

    def __init__(self, output_dir: str = "artifacts") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_citations(
        self,
        business_name: str,
        citations: list[dict[str, Any] | CitationItem],
        evidence_records: list[dict[str, Any] | EvidenceRecord] | None = None,
    ) -> dict[str, str]:
        """Export citations_manifest.json and evidence_manifest.json."""
        timestamp = datetime.now(timezone.utc).isoformat()
        manifest_id = f"evd_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        clean_citations: list[dict[str, Any]] = []
        total = 0
        live = 0
        blocked = 0
        mismatch = 0

        for item in citations:
            c_dict = item.model_dump() if isinstance(item, CitationItem) else item
            total += 1
            st = c_dict.get("status", "")
            match = c_dict.get("nap_match", "")

            if st == "live":
                live += 1
            elif st in ("blocked", "captcha"):
                blocked += 1

            if match == "mismatch":
                mismatch += 1

            clean_citations.append(c_dict)

        summary = CitationSummary(
            total=total,
            live=live,
            blocked=blocked,
            mismatch=mismatch,
        )

        manifest = CitationsManifest(
            business=business_name,
            checked_at=timestamp,
            summary=summary,
            citations=[CitationItem(**c) for c in clean_citations],
        )

        citations_path = self.output_dir / "citations_manifest.json"
        write_json_file(citations_path, manifest.model_dump())

        # Evidence manifest
        clean_evidence: list[dict[str, Any]] = []
        if evidence_records:
            for ev in evidence_records:
                clean_evidence.append(ev.model_dump() if isinstance(ev, EvidenceRecord) else ev)

        ev_manifest = EvidenceManifest(
            manifest_id=manifest_id,
            records=[EvidenceRecord(**e) for e in clean_evidence],
        )
        evidence_path = self.output_dir / "evidence_manifest.json"
        write_json_file(evidence_path, ev_manifest.model_dump())

        return {
            "citations_manifest": str(citations_path),
            "evidence_manifest": str(evidence_path),
        }

    def export_site_crawl(
        self,
        target_domain: str,
        pages: list[dict[str, Any]],
        crawl_depth: int = 1,
        evidence_records: list[dict[str, Any] | EvidenceRecord] | None = None,
    ) -> dict[str, str]:
        """Export site_crawl_artifact.json and evidence_manifest.json."""
        manifest_id = f"evd_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        site_artifact = SiteCrawlArtifact(
            target_domain=target_domain,
            crawl_depth=crawl_depth,
            pages=pages,  # type: ignore
        )

        site_path = self.output_dir / "site_crawl_artifact.json"
        write_json_file(site_path, site_artifact.model_dump())

        clean_evidence: list[dict[str, Any]] = []
        if evidence_records:
            for ev in evidence_records:
                clean_evidence.append(ev.model_dump() if isinstance(ev, EvidenceRecord) else ev)

        ev_manifest = EvidenceManifest(
            manifest_id=manifest_id,
            records=[EvidenceRecord(**e) for e in clean_evidence],
        )
        evidence_path = self.output_dir / "evidence_manifest.json"
        write_json_file(evidence_path, ev_manifest.model_dump())

        return {
            "site_crawl_artifact": str(site_path),
            "evidence_manifest": str(evidence_path),
        }

    def export_serp(
        self,
        serp_data: dict[str, Any],
        evidence_records: list[dict[str, Any] | EvidenceRecord] | None = None,
    ) -> dict[str, str]:
        """Export serp_artifact.json and evidence_manifest.json."""
        manifest_id = f"evd_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        serp_path = self.output_dir / "serp_artifact.json"
        write_json_file(serp_path, serp_data)

        clean_evidence: list[dict[str, Any]] = []
        if evidence_records:
            for ev in evidence_records:
                clean_evidence.append(ev.model_dump() if isinstance(ev, EvidenceRecord) else ev)

        ev_manifest = EvidenceManifest(
            manifest_id=manifest_id,
            records=[EvidenceRecord(**e) for e in clean_evidence],
        )
        evidence_path = self.output_dir / "evidence_manifest.json"
        write_json_file(evidence_path, ev_manifest.model_dump())

        return {
            "serp_artifact": str(serp_path),
            "evidence_manifest": str(evidence_path),
        }

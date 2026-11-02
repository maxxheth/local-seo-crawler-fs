"""Output normalization, SQLite export, multi-format serialization, and archiving."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tarfile
from typing import Any
import zipfile

import yaml
import zstandard as zstd

from src.models import (
    CitationItem,
    CitationsManifest,
    CitationSummary,
    EvidenceManifest,
    EvidenceRecord,
    SiteCrawlArtifact,
)
from src.storage import SQLiteStorage


def write_json_file(file_path: Path | str, data: Any) -> str:
    """Write dictionary or pydantic data cleanly formatted to JSON."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return str(path.resolve())


def write_yaml_file(file_path: Path | str, data: Any) -> str:
    """Write data formatted to YAML."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    return str(path.resolve())


def flatten_record_for_csv(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested structures into string representations for CSV export."""
    flat: dict[str, Any] = {}
    for k, v in record.items():
        if isinstance(v, (dict, list)):
            flat[k] = json.dumps(v, ensure_ascii=False)
        else:
            flat[k] = v
    return flat


def write_csv_file(file_path: Path | str, rows: list[dict[str, Any]]) -> str:
    """Write list of dictionaries to CSV."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.touch()
        return str(path.resolve())

    flattened = [flatten_record_for_csv(r) for r in rows]
    # Collect all unique fieldnames
    fieldnames: list[str] = []
    for r in flattened:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened)
    return str(path.resolve())


class OutputManager:
    """Writes standardized output artifacts according to strict schemas and formats."""

    def __init__(
        self,
        output_dir: str = "artifacts",
        export_format: str = "json",
        export_mode: str = "single",
        export_chunk_size: int = 250,
        archive_format: str = "none",
        tar_compression: str = "gz",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.export_format = export_format.lower()
        self.export_mode = export_mode.lower()
        self.export_chunk_size = export_chunk_size
        self.archive_format = archive_format.lower()
        self.tar_compression = tar_compression.lower()

    def _write_data(self, base_name: str, data: Any, is_list_records: bool = False) -> list[str]:
        """Export data in single or chunked files according to configured format."""
        written_files: list[str] = []
        ext = self.export_format

        # Chunked multi-file export for record lists
        if self.export_mode in ("multi", "chunked") and is_list_records and isinstance(data, list):
            chunk_size = max(1, self.export_chunk_size)
            num_chunks = (len(data) + chunk_size - 1) // chunk_size or 1
            for idx in range(num_chunks):
                batch = data[idx * chunk_size : (idx + 1) * chunk_size]
                chunk_filename = f"{base_name}_part_{idx + 1:04d}.{ext}"
                file_path = self.output_dir / chunk_filename

                if ext == "json":
                    written_files.append(write_json_file(file_path, batch))
                elif ext in ("yaml", "yml"):
                    written_files.append(write_yaml_file(file_path, batch))
                elif ext == "csv":
                    written_files.append(write_csv_file(file_path, batch))
                else:
                    written_files.append(write_json_file(file_path, batch))
        else:
            # Single file export
            file_path = self.output_dir / f"{base_name}.{ext}"
            if ext == "json":
                written_files.append(write_json_file(file_path, data))
            elif ext in ("yaml", "yml"):
                written_files.append(write_yaml_file(file_path, data))
            elif ext == "csv":
                rows = data if isinstance(data, list) else [data]
                written_files.append(write_csv_file(file_path, rows))
            else:
                written_files.append(write_json_file(file_path, data))

        return written_files

    def package_archive(self, file_paths: list[str], archive_name: str = "crawl_export") -> str | None:
        """Compress generated artifact files into zip or tarball."""
        if self.archive_format == "none" or not file_paths:
            return None

        # Filter to paths that exist
        valid_paths = [Path(p) for p in file_paths if Path(p).exists()]
        if not valid_paths:
            return None

        if self.archive_format == "zip":
            zip_dest = self.output_dir / f"{archive_name}.zip"
            with zipfile.ZipFile(zip_dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for vp in valid_paths:
                    zf.write(vp, arcname=vp.name)
            return str(zip_dest.resolve())

        elif self.archive_format == "tar":
            comp = self.tar_compression
            if comp == "zst":
                tar_dest = self.output_dir / f"{archive_name}.tar.zst"
                cctx = zstd.ZstdCompressor(level=3)
                tar_buffer = io.BytesIO()
                with tarfile.open(fileobj=tar_buffer, mode="w") as tf:
                    for vp in valid_paths:
                        tf.add(vp, arcname=vp.name)
                compressed = cctx.compress(tar_buffer.getvalue())
                with open(tar_dest, "wb") as f:
                    f.write(compressed)
                return str(tar_dest.resolve())
            else:
                mode_map = {
                    "gz": "w:gz",
                    "bz2": "w:bz2",
                    "xz": "w:xz",
                    "none": "w",
                }
                mode_str = mode_map.get(comp, "w:gz")
                suffix = f".tar.{comp}" if comp != "none" else ".tar"
                tar_dest = self.output_dir / f"{archive_name}{suffix}"
                with tarfile.open(tar_dest, mode=mode_str) as tf:
                    for vp in valid_paths:
                        tf.add(vp, arcname=vp.name)
                return str(tar_dest.resolve())

        return None

    def export_citations(
        self,
        business_name: str,
        citations: list[dict[str, Any] | CitationItem],
        evidence_records: list[dict[str, Any] | EvidenceRecord] | None = None,
    ) -> dict[str, str]:
        """Export citations_manifest and evidence_manifest."""
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

        # Standard canonical JSON
        json_path = self.output_dir / "citations_manifest.json"
        write_json_file(json_path, manifest.model_dump())

        # Configured multi-format / chunked output
        all_created_files = [str(json_path)]
        if self.export_format != "json" or self.export_mode in ("multi", "chunked"):
            exported_custom = self._write_data("citations_manifest", clean_citations, is_list_records=True)
            all_created_files.extend(exported_custom)

        # Evidence manifest
        clean_evidence: list[dict[str, Any]] = []
        if evidence_records:
            for ev in evidence_records:
                clean_evidence.append(ev.model_dump() if isinstance(ev, EvidenceRecord) else ev)

        ev_manifest = EvidenceManifest(
            manifest_id=manifest_id,
            records=[EvidenceRecord(**e) for e in clean_evidence],
        )
        ev_json_path = self.output_dir / "evidence_manifest.json"
        write_json_file(ev_json_path, ev_manifest.model_dump())
        all_created_files.append(str(ev_json_path))

        archive_path = self.package_archive(all_created_files, archive_name="citations_export")

        result = {
            "citations_manifest": str(json_path),
            "evidence_manifest": str(ev_json_path),
        }
        if archive_path:
            result["archive"] = archive_path
        return result

    def export_site_crawl(
        self,
        target_domain: str,
        pages: list[dict[str, Any]],
        crawl_depth: int = 1,
        evidence_records: list[dict[str, Any] | EvidenceRecord] | None = None,
    ) -> dict[str, str]:
        """Export site_crawl_artifact and evidence_manifest."""
        manifest_id = f"evd_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        site_artifact = SiteCrawlArtifact(
            target_domain=target_domain,
            crawl_depth=crawl_depth,
            pages=pages,  # type: ignore
        )

        json_path = self.output_dir / "site_crawl_artifact.json"
        write_json_file(json_path, site_artifact.model_dump())
        all_created_files = [str(json_path)]

        # Custom format or chunked multi-file export
        if self.export_format != "json" or self.export_mode in ("multi", "chunked"):
            exported_custom = self._write_data("site_crawl_artifact", pages, is_list_records=True)
            all_created_files.extend(exported_custom)

        # Evidence manifest
        clean_evidence: list[dict[str, Any]] = []
        if evidence_records:
            for ev in evidence_records:
                clean_evidence.append(ev.model_dump() if isinstance(ev, EvidenceRecord) else ev)

        ev_manifest = EvidenceManifest(
            manifest_id=manifest_id,
            records=[EvidenceRecord(**e) for e in clean_evidence],
        )
        ev_json_path = self.output_dir / "evidence_manifest.json"
        write_json_file(ev_json_path, ev_manifest.model_dump())
        all_created_files.append(str(ev_json_path))

        archive_path = self.package_archive(all_created_files, archive_name=f"site_crawl_{target_domain}")

        result = {
            "site_crawl_artifact": str(json_path),
            "evidence_manifest": str(ev_json_path),
        }
        if archive_path:
            result["archive"] = archive_path
        return result

    def export_serp(
        self,
        serp_data: dict[str, Any],
        evidence_records: list[dict[str, Any] | EvidenceRecord] | None = None,
    ) -> dict[str, str]:
        """Export serp_artifact and evidence_manifest."""
        manifest_id = f"evd_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        serp_path = self.output_dir / "serp_artifact.json"
        write_json_file(serp_path, serp_data)
        all_created_files = [str(serp_path)]

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
        all_created_files.append(str(evidence_path))

        archive_path = self.package_archive(all_created_files, archive_name="serp_export")

        result = {
            "serp_artifact": str(serp_path),
            "evidence_manifest": str(evidence_path),
        }
        if archive_path:
            result["archive"] = archive_path
        return result

    def export_from_storage(
        self,
        storage: SQLiteStorage,
        mode: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Export artifacts directly from SQLite storage, avoiding keeping all pages in memory."""
        meta = meta or {}
        evidence_all = storage.fetch_all_evidence()

        if mode == "competitor":
            pages = storage.fetch_all_pages()
            target_domain = meta.get("domain", "competitor.com")
            return self.export_site_crawl(
                target_domain=target_domain,
                pages=pages,
                crawl_depth=meta.get("max_depth", 5),
                evidence_records=evidence_all,
            )
        elif mode == "citations":
            citations = storage.fetch_all_citations()
            business_name = meta.get("business", "Target Business")
            return self.export_citations(
                business_name=business_name,
                citations=citations,
                evidence_records=evidence_all,
            )
        elif mode == "serp":
            serp_data = storage.fetch_all_serp() or {}
            return self.export_serp(serp_data=serp_data, evidence_records=evidence_all)

        return {}

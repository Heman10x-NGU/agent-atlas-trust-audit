"""Import-first market and regulator evidence ingestion for Phase 2.5."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .workspace import convert_document


SUPPORTED_IMPORT_EXTENSIONS = {".csv", ".xlsx", ".pdf", ".md", ".txt"}


def load_imported_evidence(import_paths: list[str | Path] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load external export files as search-result-shaped evidence rows.

    Returns `(search_results, source_manifest_rows)`.
    """
    if not import_paths:
        return [], []

    search_results: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []

    for raw_path in import_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Import path does not exist: {path}")
        files = list(_iter_import_files(path))
        for file_path in files:
            source_type = classify_import_source(file_path)
            provider = _provider_for_file(file_path)
            if file_path.suffix.lower() == ".csv":
                rows = _csv_import_results(file_path, source_type, provider)
            else:
                rows = _document_import_results(file_path, source_type, provider)
            search_results.extend(rows)
            manifest.append({
                "path": str(file_path),
                "name": file_path.name,
                "extension": file_path.suffix.lower(),
                "provider": provider,
                "evidence_class": source_type,
                "row_count": len(rows),
            })
    return search_results, manifest


def classify_import_source(file_path: str | Path) -> str:
    """Classify an import into the Phase 2.5 evidence classes."""
    name = Path(file_path).name.lower()
    if any(token in name for token in ("mca", "ministry_corporate", "roc", "charges")):
        return "regulator_ground_truth"
    if any(token in name for token in ("gst", "gstin")):
        return "regulator_ground_truth"
    if any(token in name for token in ("tracxn", "privatecircle", "askpc", "private_circle", "crunchbase", "dealroom")):
        return "imported_market_database"
    return "imported_market_database"


def _iter_import_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() in SUPPORTED_IMPORT_EXTENSIONS:
            yield path
        return
    for child in sorted(path.rglob("*")):
        if child.is_file() and child.suffix.lower() in SUPPORTED_IMPORT_EXTENSIONS:
            yield child


def _csv_import_results(file_path: Path, source_type: str, provider: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            handle.seek(0)
            plain_reader = csv.reader(handle, dialect=dialect)
            rows = list(plain_reader)
            if not rows:
                return []
            headers = [f"col_{i + 1}" for i in range(max(len(row) for row in rows))]
            dict_rows = [dict(zip(headers, row)) for row in rows]
        else:
            dict_rows = [dict(row) for row in reader]

    for index, row in enumerate(dict_rows, start=1):
        cleaned = {str(k or "").strip(): str(v or "").strip() for k, v in row.items() if str(k or "").strip()}
        text = _row_text(cleaned)
        if not text:
            continue
        regulator = _regulator_for(file_path, cleaned) if source_type == "regulator_ground_truth" else None
        results.append({
            "title": f"{provider} import {file_path.name} row {index}",
            "url": f"import://{file_path.name}#row-{index}",
            "snippet": text,
            "source": source_type,
            "evidence_class": source_type,
            "source_path": str(file_path),
            "chunk_id": f"{file_path.name}::row-{index}",
            "section": f"row {index}",
            "provider": provider,
            "import_source_type": provider,
            "regulator": regulator,
            "row": cleaned,
            "retrieval_reason": "imported market/regulator export",
            "confidence": 0.92 if source_type == "regulator_ground_truth" else 0.84,
        })
    return results


def _document_import_results(file_path: Path, source_type: str, provider: str) -> list[dict[str, Any]]:
    document = convert_document(file_path)
    text = document.get("text", "")
    chunks = _chunks(text)
    results: list[dict[str, Any]] = []
    regulator = _regulator_for(file_path, {}) if source_type == "regulator_ground_truth" else None
    for index, chunk in enumerate(chunks, start=1):
        results.append({
            "title": f"{provider} import {file_path.name} section {index}",
            "url": f"import://{file_path.name}#section-{index}",
            "snippet": chunk,
            "source": source_type,
            "evidence_class": source_type,
            "source_path": str(file_path),
            "chunk_id": f"{file_path.name}::section-{index}",
            "section": f"section {index}",
            "provider": provider,
            "import_source_type": provider,
            "regulator": regulator,
            "row": {},
            "retrieval_reason": "imported market/regulator export",
            "confidence": 0.90 if source_type == "regulator_ground_truth" else 0.80,
        })
    return results


def imported_market_sources_summary(
    import_manifest: list[dict[str, Any]],
    evidence_packets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize imported sources for bundle export."""
    packet_counts: dict[str, int] = {}
    for packet in evidence_packets:
        source_path = str(packet.get("locator", {}).get("source_path") or packet.get("source_metadata", {}).get("source_path") or "")
        if source_path:
            packet_counts[source_path] = packet_counts.get(source_path, 0) + 1
    summaries = []
    for row in import_manifest:
        path = str(row.get("path", ""))
        summaries.append({
            **row,
            "evidence_packet_count": packet_counts.get(path, row.get("row_count", 0)),
        })
    return summaries


def _provider_for_file(file_path: Path) -> str:
    name = file_path.name.lower()
    if "tracxn" in name:
        return "tracxn"
    if "askpc" in name:
        return "askpc"
    if "privatecircle" in name or "private_circle" in name:
        return "privatecircle"
    if "mca" in name or "roc" in name:
        return "mca"
    if "gst" in name or "gstin" in name:
        return "gst"
    if "crunchbase" in name:
        return "crunchbase"
    if "dealroom" in name:
        return "dealroom"
    return "generic_export"


def _regulator_for(file_path: Path, row: dict[str, Any]) -> str | None:
    combined = f"{file_path.name} {json.dumps(row, sort_keys=True)}".lower()
    if "gst" in combined or "gstin" in combined:
        return "gst"
    if "mca" in combined or "roc" in combined or "cin" in combined or "charges" in combined:
        return "mca"
    return None


def _row_text(row: dict[str, str]) -> str:
    parts = []
    for key, value in row.items():
        if value:
            parts.append(f"{key}: {value}")
    return " | ".join(parts)


def _chunks(text: str, chunk_chars: int = 1400) -> list[str]:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return []
    if len(compact) <= chunk_chars:
        return [compact]
    chunks = []
    words = compact.split()
    current: list[str] = []
    current_len = 0
    for word in words:
        next_len = current_len + len(word) + (1 if current else 0)
        if current and next_len > chunk_chars:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len = next_len
    if current:
        chunks.append(" ".join(current))
    return chunks

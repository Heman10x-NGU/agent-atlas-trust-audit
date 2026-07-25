"""Canonical trust records and replay manifests for Phase 3."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from .contracts import HARD_STOP_STATUSES
from .trust_layer import EVIDENCE_CLASS_PRIORITY, status_weight


SCHEMA_VERSION = "phase3.canonical_trust.v1"


def build_canonical_trust_records(
    claims: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    evidence_packets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build stable claim-level trust records for replay and benchmarking."""
    assessment_by_claim = {assessment.get("claim_id"): assessment for assessment in assessments}
    evidence_by_id = {packet.get("evidence_id"): packet for packet in evidence_packets}
    records: list[dict[str, Any]] = []

    for claim in sorted(claims, key=lambda item: str(item.get("claim_id", ""))):
        assessment = assessment_by_claim.get(claim.get("claim_id"), {})
        best_evidence = []
        for evidence_id in assessment.get("best_evidence_ids", []) or []:
            packet = evidence_by_id.get(evidence_id)
            if packet:
                best_evidence.append(_canonical_evidence(packet))

        status = str(assessment.get("status") or "unsupported")
        record = {
            "schema_version": SCHEMA_VERSION,
            "claim_id": str(claim.get("claim_id") or ""),
            "claim_fingerprint": _stable_hash({
                "text": _compact(claim.get("text")),
                "subject": _compact(claim.get("subject")),
                "claim_type": _compact(claim.get("claim_type")),
                "memo_section": _compact(claim.get("memo_section")),
            }),
            "claim_text": _compact(claim.get("text")),
            "subject": _compact(claim.get("subject")),
            "claim_type": _compact(claim.get("claim_type")),
            "memo_section": _compact(claim.get("memo_section")),
            "support_status": status,
            "support_weight": status_weight(status),
            "hard_stop": status in HARD_STOP_STATUSES,
            "reasoning": _compact(assessment.get("reasoning")),
            "missing_evidence": sorted(str(item) for item in assessment.get("missing_evidence", []) or []),
            "best_evidence": best_evidence,
        }
        record["record_fingerprint"] = _stable_hash(record)
        records.append(record)
    return records


def build_replay_manifest(
    result: dict[str, Any],
    canonical_trust_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build deterministic replay metadata for a trust audit result."""
    input_sources = _input_source_fingerprints(result.get("evidence_packets", []))
    support_distribution = {
        str(key): value
        for key, value in sorted((result.get("support_distribution") or {}).items())
    }
    hard_stops = [
        {
            "claim_id": record["claim_id"],
            "support_status": record["support_status"],
            "record_fingerprint": record["record_fingerprint"],
        }
        for record in canonical_trust_records
        if record.get("hard_stop")
    ]
    manifest_core = {
        "schema_version": SCHEMA_VERSION,
        "mode": "trust_audit_replay",
        "thesis": result.get("thesis"),
        "verdict": result.get("verdict"),
        "quality_score": result.get("quality_score"),
        "claim_count": len(canonical_trust_records),
        "evidence_packet_count": len(result.get("evidence_packets", [])),
        "support_distribution": support_distribution,
        "hard_stop_count": len(hard_stops),
        "hard_stops": hard_stops,
        "input_sources": input_sources,
        "trust_record_fingerprints": [
            record["record_fingerprint"] for record in canonical_trust_records
        ],
    }
    manifest = dict(manifest_core)
    manifest["replay_fingerprint"] = _stable_hash(manifest_core)
    return manifest


def trust_record_distribution(canonical_trust_records: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(record.get("support_status", "unsupported") for record in canonical_trust_records)
    return {str(key): counts[key] for key in sorted(counts)}


def _canonical_evidence(packet: dict[str, Any]) -> dict[str, Any]:
    evidence_class = str(
        packet.get("evidence_class")
        or packet.get("source_metadata", {}).get("evidence_class")
        or "public_web"
    )
    row = packet.get("source_metadata", {}).get("row") or {}
    source_path = str(packet.get("locator", {}).get("source_path") or packet.get("source_metadata", {}).get("source_path") or "")
    return {
        "evidence_id": str(packet.get("evidence_id") or ""),
        "evidence_fingerprint": _stable_hash({
            "source_uri": packet.get("source_uri"),
            "title": packet.get("title"),
            "excerpt_hash": _stable_hash(_compact(packet.get("excerpt"))),
            "evidence_class": evidence_class,
            "provider": packet.get("source_metadata", {}).get("provider"),
            "row_hash": _stable_hash(row) if row else "",
        }),
        "evidence_class": evidence_class,
        "trust_score": EVIDENCE_CLASS_PRIORITY.get(evidence_class, 0.5),
        "source_uri": str(packet.get("source_uri") or ""),
        "source_path": source_path,
        "title": _compact(packet.get("title")),
        "provider": str(packet.get("source_metadata", {}).get("provider") or ""),
        "regulator": str(packet.get("source_metadata", {}).get("regulator") or ""),
    }


def _input_source_fingerprints(evidence_packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}
    for packet in evidence_packets:
        source_uri = str(packet.get("source_uri") or "")
        locator = packet.get("locator", {})
        source_path = str(locator.get("source_path") or packet.get("source_metadata", {}).get("source_path") or "")
        source_key = source_uri or source_path or str(packet.get("evidence_id") or "")
        evidence_class = str(
            packet.get("evidence_class")
            or packet.get("source_metadata", {}).get("evidence_class")
            or "public_web"
        )
        entry = by_source.setdefault(source_key, {
            "source_key": source_key,
            "source_path": source_path,
            "evidence_class": evidence_class,
            "provider": str(packet.get("source_metadata", {}).get("provider") or ""),
            "evidence_ids": [],
            "content_hashes": [],
        })
        evidence_id = str(packet.get("evidence_id") or "")
        if evidence_id and evidence_id not in entry["evidence_ids"]:
            entry["evidence_ids"].append(evidence_id)
        entry["content_hashes"].append(_stable_hash(_compact(packet.get("excerpt"))))

    rows = []
    for entry in by_source.values():
        core = {
            "source_key": entry["source_key"],
            "source_path": entry["source_path"],
            "evidence_class": entry["evidence_class"],
            "provider": entry["provider"],
            "evidence_ids": sorted(entry["evidence_ids"]),
            "content_hashes": sorted(entry["content_hashes"]),
        }
        rows.append({
            **core,
            "source_fingerprint": _stable_hash(core),
        })
    rows.sort(key=lambda row: row["source_fingerprint"])
    return rows


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split())


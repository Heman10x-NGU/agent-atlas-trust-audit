"""Trust-layer helpers for Phase 2.5 memo audits."""
from __future__ import annotations

from typing import Any

from .contracts import HARD_STOP_STATUSES


EVIDENCE_CLASS_PRIORITY = {
    "regulator_ground_truth": 1.00,
    "private_document": 0.90,
    "imported_market_database": 0.86,
    "manual_fund_memory": 0.78,
    "public_web": 0.62,
    "external_ai_memo": 0.25,
}

STATUS_WEIGHTS = {
    "verified_by_regulator": 1.00,
    "verified": 0.92,
    "supported": 0.88,
    "weak_support": 0.55,
    "weak": 0.50,
    "needs_manual_check": 0.25,
    "data_stale": 0.20,
    "regulator_unavailable": 0.15,
    "unsupported": 0.0,
    "contradicted": 0.0,
    "contradicted_by_regulator": 0.0,
    "contradicted_by_imported_database": 0.0,
}


def status_weight(status: str) -> float:
    return STATUS_WEIGHTS.get(status, 0.0)


def hard_stop_assessments(assessments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return assessments that must block convergence."""
    return [assessment for assessment in assessments if assessment.get("status") in HARD_STOP_STATUSES]


def has_hard_stop(assessments: list[dict[str, Any]]) -> bool:
    return bool(hard_stop_assessments(assessments))


def rank_evidence_for_claims(
    claims: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    evidence_packets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build deterministic evidence rankings for trust bundles."""
    evidence_by_id = {packet.get("evidence_id"): packet for packet in evidence_packets}
    assessment_by_claim = {assessment.get("claim_id"): assessment for assessment in assessments}
    rankings: list[dict[str, Any]] = []

    for claim in claims:
        assessment = assessment_by_claim.get(claim.get("claim_id"), {})
        ordered_ids = []
        for evidence_id in assessment.get("best_evidence_ids", []) or []:
            if evidence_id not in ordered_ids:
                ordered_ids.append(evidence_id)
        for evidence_id in claim.get("cited_evidence_ids", []) or []:
            if evidence_id not in ordered_ids:
                ordered_ids.append(evidence_id)

        ranked_packets = []
        for evidence_id in ordered_ids:
            packet = evidence_by_id.get(evidence_id)
            if not packet:
                continue
            evidence_class = packet.get("evidence_class") or packet.get("source_metadata", {}).get("evidence_class") or "public_web"
            trust_score = EVIDENCE_CLASS_PRIORITY.get(evidence_class, 0.5)
            if evidence_id in (assessment.get("best_evidence_ids", []) or []):
                trust_score += 0.05
            ranked_packets.append({
                "evidence_id": evidence_id,
                "evidence_class": evidence_class,
                "source_uri": packet.get("source_uri"),
                "title": packet.get("title"),
                "trust_score": round(min(1.0, trust_score), 3),
                "reason": packet.get("retrieval_reason", ""),
            })
        ranked_packets.sort(key=lambda item: (-item["trust_score"], item["evidence_id"]))

        rankings.append({
            "claim_id": claim.get("claim_id"),
            "claim_text": claim.get("text"),
            "support_status": assessment.get("status", "unsupported"),
            "hard_stop": assessment.get("status") in HARD_STOP_STATUSES,
            "ranked_evidence": ranked_packets,
        })
    return rankings


def build_regulator_assessments(
    claims: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    evidence_packets: list[dict[str, Any]],
    regulators: list[str],
) -> list[dict[str, Any]]:
    """Report optional fixture/cache regulator coverage without live API dependency."""
    if not regulators:
        return []

    evidence_by_id = {packet.get("evidence_id"): packet for packet in evidence_packets}
    regulator_packets: dict[str, list[dict[str, Any]]] = {regulator: [] for regulator in regulators}
    for packet in evidence_packets:
        if packet.get("evidence_class") != "regulator_ground_truth":
            continue
        regulator = str(packet.get("source_metadata", {}).get("regulator") or "").lower()
        for requested in regulators:
            if regulator == requested or requested in str(packet.get("title", "")).lower():
                regulator_packets.setdefault(requested, []).append(packet)

    rows: list[dict[str, Any]] = []
    for regulator in regulators:
        packets = regulator_packets.get(regulator, [])
        if not packets:
            rows.append({
                "regulator": regulator,
                "claim_id": None,
                "status": "regulator_unavailable",
                "reasoning": "No local fixture/cache evidence was available; live API calls are not part of the default path.",
                "evidence_ids": [],
            })
            continue

        regulator_ids = {packet.get("evidence_id") for packet in packets}
        for claim in claims:
            assessment = next((item for item in assessments if item.get("claim_id") == claim.get("claim_id")), {})
            best_ids = [eid for eid in assessment.get("best_evidence_ids", []) or [] if eid in regulator_ids]
            if not best_ids:
                continue
            packet_classes = [
                evidence_by_id.get(eid, {}).get("evidence_class")
                for eid in best_ids
            ]
            status = assessment.get("status", "needs_manual_check")
            if status == "verified" and "regulator_ground_truth" in packet_classes:
                status = "verified_by_regulator"
            rows.append({
                "regulator": regulator,
                "claim_id": claim.get("claim_id"),
                "status": status,
                "reasoning": assessment.get("reasoning", ""),
                "evidence_ids": best_ids,
            })
    return rows

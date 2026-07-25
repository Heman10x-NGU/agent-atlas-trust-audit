"""Deterministic six-lens extraction from claims and evidence support."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .contracts import LENS_NAMES, LensBundle, LensRecord


_LENS_KEYWORDS = {
    "vc": {
        "funding", "stage", "seed", "series", "round", "raised", "valuation",
        "arr", "revenue", "investor", "venture",
    },
    "market": {
        "market", "tam", "demand", "adoption", "category", "landscape",
        "geography", "india", "global", "enterprise",
    },
    "competition": {
        "competition", "competitor", "crowded", "incumbent", "global",
        "leader", "alternative", "moat", "differentiation",
    },
    "product": {
        "product", "platform", "gateway", "observability", "monitoring",
        "evaluation", "routing", "fallback", "apm", "tool",
    },
    "traction": {
        "traction", "customer", "customers", "users", "stars", "github",
        "growth", "paying", "usage", "community",
    },
    "risk": {
        "risk", "weak", "manual", "unsupported", "contradict", "crowded",
        "uncertain", "generic", "stale",
    },
}

_CLAIM_TYPE_LENSES = {
    "funding_stage": {"vc"},
    "market_theme": {"market"},
    "thesis_fit": {"market", "product"},
    "traction": {"traction", "vc"},
    "risk": {"risk", "competition"},
}


def build_lens_bundle(
    claims: list[dict[str, Any]],
    evidence_packets: list[dict[str, Any]],
    support_assessments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build all six investor lenses from claim support metadata."""
    assessment_by_claim = {assessment.get("claim_id"): assessment for assessment in support_assessments}
    evidence_ids = {packet.get("evidence_id") for packet in evidence_packets}
    records = []

    for lens in LENS_NAMES:
        lens_claims = [
            claim for claim in claims
            if lens in _lenses_for_claim(claim, assessment_by_claim.get(claim.get("claim_id")))
        ]
        statuses = [
            assessment_by_claim.get(claim.get("claim_id"), {}).get("status", "unsupported")
            for claim in lens_claims
        ]
        status_counts = Counter(statuses)
        key_evidence_ids = _key_evidence_ids(lens_claims, assessment_by_claim, evidence_ids)
        record = LensRecord(
            lens=lens,
            support_status=_lens_status(statuses),
            rationale=_rationale(lens, lens_claims, status_counts, key_evidence_ids),
            key_evidence_ids=key_evidence_ids,
            representative_claim_ids=[str(claim.get("claim_id")) for claim in lens_claims[:5]],
            support_distribution=dict(status_counts),
        )
        records.append(record)

    return LensBundle(records).to_dict()


def compact_lens_summary(lens_bundle: dict[str, Any]) -> list[str]:
    """Return compact terminal/memo summary lines."""
    lines = []
    for lens in LENS_NAMES:
        record = lens_bundle.get(lens, {})
        support = record.get("support_status", "needs_manual_check")
        evidence = ", ".join(record.get("key_evidence_ids", [])[:2]) or "none"
        lines.append(f"{lens}: {support} ({evidence})")
    return lines


def _lenses_for_claim(
    claim: dict[str, Any],
    assessment: dict[str, Any] | None,
) -> set[str]:
    claim_type = str(claim.get("claim_type") or "")
    lenses = set(_CLAIM_TYPE_LENSES.get(claim_type, set()))
    text = f"{claim.get('subject', '')} {claim.get('text', '')}".lower()
    tokens = set(text.replace("/", " ").replace("-", " ").split())
    for lens, keywords in _LENS_KEYWORDS.items():
        if tokens & keywords:
            lenses.add(lens)
    status = (assessment or {}).get("status")
    if status in {
        "unsupported",
        "contradicted",
        "contradicted_by_imported_database",
        "contradicted_by_regulator",
        "needs_manual_check",
        "regulator_unavailable",
        "data_stale",
    }:
        lenses.add("risk")
    return lenses


def _lens_status(statuses: list[str]) -> str:
    if not statuses:
        return "needs_manual_check"
    counts = Counter(statuses)
    if counts.get("contradicted_by_regulator"):
        return "contradicted_by_regulator"
    if counts.get("contradicted_by_imported_database"):
        return "contradicted_by_imported_database"
    if counts.get("contradicted"):
        return "contradicted"
    if counts.get("unsupported") == len(statuses):
        return "unsupported"
    bad = counts.get("unsupported", 0) + counts.get("needs_manual_check", 0) + counts.get("regulator_unavailable", 0) + counts.get("data_stale", 0)
    verified = counts.get("verified", 0) + counts.get("verified_by_regulator", 0) + counts.get("supported", 0)
    if verified >= max(1, len(statuses) - bad) and bad / len(statuses) <= 0.25:
        if counts.get("verified_by_regulator"):
            return "verified_by_regulator"
        if counts.get("verified"):
            return "verified"
        return "supported"
    if verified or counts.get("weak", 0) or counts.get("weak_support", 0):
        return "weak_support" if counts.get("weak_support") else "weak"
    return "needs_manual_check"


def _key_evidence_ids(
    claims: list[dict[str, Any]],
    assessment_by_claim: dict[str, dict[str, Any]],
    known_evidence_ids: set[str],
) -> list[str]:
    ordered: list[str] = []
    for claim in claims:
        assessment = assessment_by_claim.get(claim.get("claim_id"), {})
        for evidence_id in assessment.get("best_evidence_ids", []) or []:
            if evidence_id in known_evidence_ids and evidence_id not in ordered:
                ordered.append(evidence_id)
        for evidence_id in claim.get("cited_evidence_ids", []) or []:
            if evidence_id in known_evidence_ids and evidence_id not in ordered:
                ordered.append(evidence_id)
    return ordered[:6]


def _rationale(
    lens: str,
    lens_claims: list[dict[str, Any]],
    status_counts: Counter,
    key_evidence_ids: list[str],
) -> str:
    if not lens_claims:
        return f"No extracted claims mapped to the {lens} lens; manual review is required."
    support_text = ", ".join(
        f"{status}={count}" for status, count in sorted(status_counts.items())
    )
    evidence_text = f"{len(key_evidence_ids)} evidence packet(s)" if key_evidence_ids else "no direct evidence packets"
    return f"{len(lens_claims)} claim(s) mapped to {lens}; support {support_text}; {evidence_text} attached."

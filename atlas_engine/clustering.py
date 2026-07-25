"""Deterministic claim clustering for repeated or near-duplicate claims."""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

from .contracts import ClaimCluster


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "based", "be", "by", "for", "from",
    "has", "have", "in", "into", "is", "it", "of", "on", "or", "stage", "the",
    "their", "this", "to", "with", "within",
}

_CLAIM_TYPE_PRIORITY = {
    "thesis_fit": 0,
    "funding_stage": 1,
    "traction": 2,
    "risk": 3,
    "market_theme": 4,
}


def cluster_claims(
    claims: list[dict[str, Any]],
    support_assessments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cluster repeated claims by subject, type, token overlap, and shared evidence."""
    if not claims:
        return []

    assessment_by_claim = {assessment.get("claim_id"): assessment for assessment in support_assessments}
    adjacency = defaultdict(set)

    for i, left in enumerate(claims):
        for right in claims[i + 1:]:
            if _should_cluster(left, right, assessment_by_claim):
                adjacency[left["claim_id"]].add(right["claim_id"])
                adjacency[right["claim_id"]].add(left["claim_id"])

    components = _connected_components([claim["claim_id"] for claim in claims], adjacency)
    claim_by_id = {claim["claim_id"]: claim for claim in claims}
    clusters = []

    for component in components:
        member_claims = [claim_by_id[claim_id] for claim_id in component]
        representative = _representative_claim(member_claims, assessment_by_claim)
        evidence_ids = _collect_evidence_ids(member_claims, assessment_by_claim)
        statuses = [
            assessment_by_claim.get(claim.get("claim_id"), {}).get("status", "unsupported")
            for claim in member_claims
        ]
        summary = Counter(statuses)
        cluster = ClaimCluster(
            cluster_id=_cluster_id(representative, component),
            normalized_subject=_normalize_subject(representative.get("subject", "")),
            claim_type=str(representative.get("claim_type") or ""),
            representative_claim_id=str(representative.get("claim_id") or ""),
            representative_claim=str(representative.get("text") or ""),
            member_claim_ids=[str(claim.get("claim_id")) for claim in member_claims],
            evidence_ids=evidence_ids,
            support_summary=dict(summary),
            cluster_support_status=_cluster_status(statuses),
            rationale=_cluster_rationale(member_claims, evidence_ids, summary),
        )
        clusters.append(cluster.to_dict())

    clusters.sort(key=lambda item: (
        item.get("normalized_subject", ""),
        _CLAIM_TYPE_PRIORITY.get(item.get("claim_type", ""), 99),
        item.get("representative_claim_id", ""),
    ))
    return clusters


def summarize_clusters(claim_clusters: list[dict[str, Any]], limit: int = 6) -> list[str]:
    """Return compact cluster summaries for terminal output."""
    summaries: list[str] = []
    for cluster in claim_clusters[:limit]:
        representative = str(cluster.get("representative_claim", ""))
        summaries.append(
            f"{cluster.get('cluster_id')}: {cluster.get('normalized_subject')} "
            f"[{cluster.get('claim_type')}] {cluster.get('cluster_support_status')} "
            f"({len(cluster.get('member_claim_ids', []))} claims) "
            f"{representative[:90]}"
        )
    return summaries


def _should_cluster(
    left: dict[str, Any],
    right: dict[str, Any],
    assessment_by_claim: dict[str, dict[str, Any]],
) -> bool:
    if _normalize_subject(left.get("subject", "")) != _normalize_subject(right.get("subject", "")):
        return False
    if str(left.get("claim_type") or "") != str(right.get("claim_type") or ""):
        return False
    left_tokens = _tokens(left.get("text", ""))
    right_tokens = _tokens(right.get("text", ""))
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    shared_evidence = bool(set(left.get("cited_evidence_ids", [])) & set(right.get("cited_evidence_ids", [])))
    same_signature = _signature(left) == _signature(right)

    if same_signature:
        return True
    if shared_evidence and overlap >= 0.15:
        return True
    if overlap >= 0.5:
        return True

    left_status = assessment_by_claim.get(left.get("claim_id"), {}).get("status")
    right_status = assessment_by_claim.get(right.get("claim_id"), {}).get("status")
    if left_status == right_status and overlap >= 0.35:
        return True
    return False


def _connected_components(nodes: list[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for node in nodes:
        if node in seen:
            continue
        stack = [node]
        component = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.append(current)
            stack.extend(sorted(adjacency.get(current, set()) - seen, reverse=True))
        components.append(component)
    return components


def _representative_claim(
    claims: list[dict[str, Any]],
    assessment_by_claim: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    def score(claim: dict[str, Any]) -> tuple[int, int, int, str]:
        assessment = assessment_by_claim.get(claim.get("claim_id"), {})
        status = assessment.get("status", "unsupported")
        status_rank = {
            "verified_by_regulator": 0,
            "verified": 0,
            "supported": 0,
            "weak_support": 1,
            "weak": 1,
            "needs_manual_check": 2,
            "regulator_unavailable": 3,
            "data_stale": 3,
            "unsupported": 3,
            "contradicted": 4,
            "contradicted_by_imported_database": 4,
            "contradicted_by_regulator": 4,
        }.get(status, 5)
        text_len = len(str(claim.get("text") or ""))
        cited = len(claim.get("cited_evidence_ids", []))
        return (status_rank, -cited, -text_len, str(claim.get("claim_id") or ""))

    return sorted(claims, key=score)[0]


def _collect_evidence_ids(
    claims: list[dict[str, Any]],
    assessment_by_claim: dict[str, dict[str, Any]],
) -> list[str]:
    ordered: list[str] = []
    for claim in claims:
        assessment = assessment_by_claim.get(claim.get("claim_id"), {})
        for evidence_id in assessment.get("best_evidence_ids", []) or []:
            if evidence_id not in ordered:
                ordered.append(evidence_id)
        for evidence_id in claim.get("cited_evidence_ids", []) or []:
            if evidence_id not in ordered:
                ordered.append(evidence_id)
    return ordered[:8]


def _cluster_status(statuses: list[str]) -> str:
    if not statuses:
        return "needs_manual_check"
    counts = Counter(statuses)
    if counts.get("contradicted_by_regulator"):
        return "contradicted_by_regulator"
    if counts.get("contradicted_by_imported_database"):
        return "contradicted_by_imported_database"
    if counts.get("contradicted"):
        return "contradicted"
    if counts.get("verified_by_regulator") and not counts.get("unsupported") and not counts.get("needs_manual_check"):
        return "verified_by_regulator"
    if counts.get("verified") and not counts.get("unsupported") and not counts.get("needs_manual_check"):
        return "verified"
    if counts.get("supported") and not counts.get("unsupported") and not counts.get("needs_manual_check"):
        return "supported"
    if counts.get("supported") or counts.get("verified") or counts.get("verified_by_regulator") or counts.get("weak") or counts.get("weak_support"):
        if counts.get("verified_by_regulator"):
            return "verified_by_regulator"
        if counts.get("verified"):
            return "verified"
        if counts.get("supported"):
            return "supported"
        return "weak_support" if counts.get("weak_support") else "weak"
    if counts.get("unsupported", 0) >= counts.get("needs_manual_check", 0):
        return "unsupported"
    return "needs_manual_check"


def _cluster_rationale(
    claims: list[dict[str, Any]],
    evidence_ids: list[str],
    summary: Counter,
) -> str:
    parts = [
        f"{len(claims)} claim(s) grouped on normalized subject/type",
        f"support {', '.join(f'{status}={count}' for status, count in sorted(summary.items()))}",
    ]
    if evidence_ids:
        parts.append(f"shared evidence: {', '.join(evidence_ids[:4])}")
    return "; ".join(parts) + "."


def _normalize_subject(subject: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(subject).lower()).strip()
    return re.sub(r"\s+", " ", text)


def _signature(claim: dict[str, Any]) -> str:
    text = " ".join(sorted(_tokens(claim.get("text", ""))))
    return f"{_normalize_subject(claim.get('subject', ''))}|{claim.get('claim_type', '')}|{text[:120]}"


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+.-]*", str(text).lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _cluster_id(representative: dict[str, Any], component: list[str]) -> str:
    seed = "|".join([
        _normalize_subject(representative.get("subject", "")),
        str(representative.get("claim_type") or ""),
        str(representative.get("text") or "")[:120],
        ",".join(sorted(component)),
    ])
    return "cluster_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]

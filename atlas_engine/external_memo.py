"""Imported memo audit mode using local workspace and import evidence."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit_bundle import write_memo_and_bundle
from .clustering import cluster_claims
from .evidence import normalize_evidence_packets
from .canonical_trust import build_canonical_trust_records, build_replay_manifest
from .entity_registry import apply_import_drift
from .imports import imported_market_sources_summary, load_imported_evidence
from .judgment import assess_claim_support, claim_quality_score
from .lenses import build_lens_bundle
from .trust_layer import build_regulator_assessments, hard_stop_assessments, rank_evidence_for_claims
from .workspace import search_workspace


def audit_external_memo(
    memo_path: str | Path,
    workspace_path: str | Path,
    import_paths: list[str | Path] | str | Path | None = None,
    regulators: list[str] | str | None = None,
    export_md: str | Path | None = None,
    export_bundle: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Extract external memo claims and judge them against workspace evidence."""
    memo_file = Path(memo_path).expanduser().resolve()
    workspace = Path(workspace_path).expanduser().resolve()
    if not memo_file.exists() or not memo_file.is_file():
        raise ValueError(f"External memo does not exist or is not a file: {memo_file}")
    if not workspace.exists() or not workspace.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {workspace}")

    text = memo_file.read_text(encoding="utf-8", errors="replace")
    run_id = run_id or f"audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    claims = extract_external_claims(text)
    import_paths_list = _import_paths(import_paths)
    regulators_list = _regulators(regulators)

    workspace_results = _collect_workspace_evidence(claims, workspace)
    imported_results, import_manifest = load_imported_evidence(import_paths_list)
    raw_evidence = workspace_results + imported_results
    evidence_packets = normalize_evidence_packets(
        raw_evidence,
        retrieval_query=f"external memo audit: {memo_file.name}",
        retrieval_reason="external memo claim audit",
    )
    trust_mode = bool(imported_results or regulators_list)
    assessments = assess_claim_support(claims, evidence_packets, trust_mode=trust_mode)
    quality = claim_quality_score(assessments)
    claim_clusters = cluster_claims(claims, assessments)
    lens_bundle = build_lens_bundle(claims, evidence_packets, assessments)
    evidence_rankings = rank_evidence_for_claims(claims, assessments, evidence_packets)
    hard_stops = hard_stop_assessments(assessments)
    regulator_assessments = build_regulator_assessments(claims, assessments, evidence_packets, regulators_list)
    imported_sources = imported_market_sources_summary(import_manifest, evidence_packets)
    drift_report = apply_import_drift(workspace, evidence_packets, run_id=run_id) if imported_results else None
    canonical_trust_records = build_canonical_trust_records(claims, assessments, evidence_packets)

    result = {
        "run_id": run_id,
        "mode": "audit_memo",
        "thesis": f"Audit external memo: {memo_file.name}",
        "external_memo_path": str(memo_file),
        "workspace_path": str(workspace),
        "import_paths": [str(path) for path in import_paths_list],
        "regulators": regulators_list,
        "verdict": _audit_verdict(assessments),
        "quality_score": quality["overall_score"],
        "support_distribution": quality["support_distribution"],
        "claim_ledger": claims,
        "support_assessments": assessments,
        "evidence_packets": evidence_packets,
        "evidence_rankings": evidence_rankings,
        "imported_market_sources": imported_sources,
        "hard_stop_assessments": hard_stops,
        "canonical_trust_records": canonical_trust_records,
        "lens_bundle": lens_bundle,
        "claim_clusters": claim_clusters,
        "companies": [],
        "executive_summary": _summary(assessments),
        "market_themes": [],
        "gaps_in_research": _gaps(assessments),
        "convergence_report": {
            "score": quality["overall_score"],
            "iteration_delta": quality["overall_score"],
            "contradiction_count": quality["contradiction_count"],
            "unsupported_ratio": quality["unsupported_ratio"],
            "hard_stop_count": quality.get("hard_stop_count", 0),
            "stop_reason": "hard_stop_contradiction" if hard_stops else "audit_complete",
            "support_distribution": quality["support_distribution"],
            "council_verdict": None,
            "next_research_actions": [],
        },
        "iterations": [{
            "iteration": 1,
            "quality_score": quality["overall_score"],
            "claim_count": len(claims),
            "evidence_packet_count": len(evidence_packets),
            "imported_evidence_count": len(imported_results),
            "support_distribution": quality["support_distribution"],
            "hard_stop_count": quality.get("hard_stop_count", 0),
        }],
        "removed_or_rejected_companies": [],
        "next_research_actions": [],
    }
    if regulators_list:
        result["regulator_assessments"] = regulator_assessments
    if drift_report is not None:
        result["drift_report"] = drift_report
        result["drift_report_path"] = drift_report.get("report_path")
    result["replay_manifest"] = build_replay_manifest(result, canonical_trust_records)
    memo_markdown = render_external_audit_memo(result)
    result["trust_report_markdown"] = memo_markdown
    write_memo_and_bundle(result, export_md=export_md, export_bundle=export_bundle, mode="audit_memo", memo_markdown=memo_markdown)
    if result.get("memo_path"):
        result["trust_report_path"] = result["memo_path"]
    return result


def extract_external_claims(text: str) -> list[dict[str, Any]]:
    """Deterministically extract auditable claims from external memo prose."""
    claims = []
    claim_index = 1
    current_section = "external_memo"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            current_section = heading.group(1).strip()
            continue
        line = re.sub(r"^\s*[-*]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        for sentence in _sentences(line):
            if not _looks_like_claim(sentence):
                continue
            subject = _subject(sentence)
            claim_type = _claim_type(sentence)
            claims.append({
                "claim_id": f"claim_{claim_index:03d}",
                "text": sentence,
                "subject": subject,
                "claim_type": claim_type,
                "memo_section": current_section,
                "cited_evidence_ids": [],
                "metadata": {
                    "external_claim_source": "external_memo",
                    "section": current_section,
                },
            })
            claim_index += 1
    return claims


def render_external_audit_memo(result: dict[str, Any]) -> str:
    """Render external memo audit output with failures visible up front."""
    claims = {claim["claim_id"]: claim for claim in result.get("claim_ledger", [])}
    hard_stops = result.get("hard_stop_assessments", [])
    lines = [
        "# Trust Report",
        "",
        "_Atlas audit of imported memo claims against workspace sources, imported evidence, and optional regulator evidence._",
        "",
        "## Source",
        "",
        f"- external_memo_path: {result.get('external_memo_path', '')}",
        f"- workspace_path: {result.get('workspace_path', '')}",
        f"- import_paths: {', '.join(result.get('import_paths', []) or ['none'])}",
        f"- regulators: {', '.join(result.get('regulators', []) or ['none'])}",
        f"- verdict: {result.get('verdict', '')}",
        f"- quality_score: {result.get('quality_score', 0):.3f}",
        "",
        "## Hard Stops",
        "",
    ]
    if hard_stops:
        lines.append(f"Hard-stop contradiction count: {len(hard_stops)}")
        for assessment in hard_stops:
            claim = claims.get(assessment.get("claim_id"), {})
            lines.append(
                f"- {assessment.get('status')}: {claim.get('text', assessment.get('claim_id'))} "
                f"(evidence: {', '.join(assessment.get('best_evidence_ids', []) or ['none'])})"
            )
    else:
        lines.append("No imported-database or regulator hard-stop contradictions were detected.")
    lines.extend([
        "",
        "## Support Summary",
        "",
    ])
    support = result.get("support_distribution", {})
    for status in (
        "verified_by_regulator",
        "verified",
        "supported",
        "weak_support",
        "weak",
        "needs_manual_check",
        "regulator_unavailable",
        "data_stale",
        "unsupported",
        "contradicted",
        "contradicted_by_imported_database",
        "contradicted_by_regulator",
    ):
        lines.append(f"- {status}: {support.get(status, 0)}")
    lines.extend(["", "## Claims Needing Review", ""])
    review = [
        assessment for assessment in result.get("support_assessments", [])
        if assessment.get("status") in {
            "weak",
            "weak_support",
            "needs_manual_check",
            "regulator_unavailable",
            "data_stale",
            "unsupported",
            "contradicted",
            "contradicted_by_imported_database",
            "contradicted_by_regulator",
        }
    ]
    if not review:
        lines.append("No weak, unsupported, manual-check, or contradicted claims were found.")
    for assessment in review:
        claim = claims.get(assessment.get("claim_id"), {})
        lines.append(
            f"- {assessment.get('status')}: {claim.get('text', assessment.get('claim_id'))} "
            f"(best evidence: {', '.join(assessment.get('best_evidence_ids', []) or ['none'])})"
        )
    lines.extend(["", "## Full Claim Ledger", ""])
    lines.extend(["| Claim | Section | Subject | Type | Status | Reasoning |", "|---|---|---|---|---|---|"])
    assessments = {a["claim_id"]: a for a in result.get("support_assessments", [])}
    for claim in result.get("claim_ledger", []):
        assessment = assessments.get(claim["claim_id"], {})
        lines.append(
            "| "
            f"{_cell(claim.get('claim_id'))} | "
            f"{_cell(claim.get('memo_section'))} | "
            f"{_cell(claim.get('subject'))} | "
            f"{_cell(claim.get('claim_type'))} | "
            f"{_cell(assessment.get('status'))} | "
            f"{_cell(assessment.get('reasoning'))} |"
        )
    if result.get("imported_market_sources"):
        lines.extend(["", "## Imported Sources", ""])
        lines.extend(["| Source | Provider | Evidence Class | Rows |", "|---|---|---|---|"])
        for source in result.get("imported_market_sources", []):
            lines.append(
                "| "
                f"{_cell(source.get('name') or source.get('path'))} | "
                f"{_cell(source.get('provider'))} | "
                f"{_cell(source.get('evidence_class'))} | "
                f"{_cell(source.get('row_count'))} |"
            )
    if result.get("drift_report"):
        drift = result["drift_report"]
        lines.extend(["", "## Drift", ""])
        lines.append(f"- material_change_count: {drift.get('material_change_count', 0)}")
        if result.get("drift_report_path"):
            lines.append(f"- drift_report_path: {result.get('drift_report_path')}")
    if result.get("replay_manifest"):
        replay = result["replay_manifest"]
        lines.extend(["", "## Replay", ""])
        lines.append(f"- schema_version: {replay.get('schema_version')}")
        lines.append(f"- replay_fingerprint: {replay.get('replay_fingerprint')}")
        lines.append(f"- canonical_trust_records: {len(result.get('canonical_trust_records', []))}")
    return "\n".join(lines).rstrip() + "\n"


def _collect_workspace_evidence(claims: list[dict[str, Any]], workspace: Path) -> list[dict[str, Any]]:
    seen = set()
    results = []
    for claim in claims:
        query = f"{claim.get('subject', '')} {claim.get('text', '')}"
        for result in search_workspace(query, workspace, limit=4):
            key = result.get("chunk_id") or result.get("url")
            if key in seen:
                continue
            seen.add(key)
            results.append(result)
    return results


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [" ".join(part.split()) for part in parts if len(part.strip()) >= 20]


def _looks_like_claim(sentence: str) -> bool:
    lower = sentence.lower()
    claim_markers = [
        " is ", " are ", " has ", " raised ", " reports ", " generated ",
        " will ", " can ", " market ", " revenue ", " customers", " funding",
        "series ", "seed", "risk", "competes", "growth", "employee", "employees",
        "headcount", "incorporated", "incorporation", "director", "signatory",
        "dpiit", "startup recognition", "charge", "charges", "arr",
    ]
    return any(marker in lower for marker in claim_markers)


def _subject(sentence: str) -> str:
    clean = re.sub(r"[*_`]", "", sentence).strip()
    patterns = [
        r"^([A-Z][A-Za-z0-9&.\- ]{1,50}?)\s+(?:is|are|has|raised|reports|generated|competes|will|can)\b",
        r"^The\s+([A-Za-z0-9&.\- ]{3,50}?)\s+(?:market|category|segment)\b",
    ]
    for pattern in patterns:
        match = re.match(pattern, clean)
        if match:
            return match.group(1).strip()
    words = clean.split()
    return " ".join(words[:3]).strip(" ,.:;") or "External memo"


def _claim_type(sentence: str) -> str:
    lower = sentence.lower()
    if any(term in lower for term in ("revenue", "arr", "annual recurring revenue")):
        return "revenue"
    if any(term in lower for term in ("employee", "employees", "headcount", "team size")):
        return "employee_count"
    if any(term in lower for term in ("incorporated", "incorporation", "founded in")):
        return "incorporation_year"
    if any(term in lower for term in ("director", "signatory", "authorised signatory", "authorized signatory")):
        return "signatory_director"
    if any(term in lower for term in ("dpiit", "startup recognition", "recognised startup", "recognized startup")):
        return "dpiit_startup_recognition"
    if any(term in lower for term in ("charge", "charges", "hypothecation", "secured loan")):
        return "charges"
    if any(term in lower for term in ("raised", "funding", "seed", "series ")):
        return "funding_stage"
    if any(term in lower for term in ("customer", "growth", "stars", "users", "traction")):
        return "traction"
    if any(term in lower for term in ("risk", "crowded", "competition", "competes", "incumbent")):
        return "risk"
    if "market" in lower or "category" in lower:
        return "market_theme"
    return "thesis_fit"


def _audit_verdict(assessments: list[dict[str, Any]]) -> str:
    statuses = [assessment.get("status") for assessment in assessments]
    if "contradicted_by_regulator" in statuses or "contradicted_by_imported_database" in statuses:
        return "HARD_STOP_CONTRADICTION"
    if "contradicted" in statuses:
        return "CONTRADICTIONS_FOUND"
    if any(status in {"unsupported", "needs_manual_check", "regulator_unavailable", "data_stale"} for status in statuses):
        return "NEEDS_MANUAL_REVIEW"
    if statuses and all(status in {"supported", "verified", "verified_by_regulator"} for status in statuses):
        return "SUPPORTED"
    return "WEAK_SUPPORT"


def _summary(assessments: list[dict[str, Any]]) -> str:
    total = len(assessments)
    bad = [
        a for a in assessments
        if a.get("status") in {
            "unsupported",
            "needs_manual_check",
            "regulator_unavailable",
            "data_stale",
            "contradicted",
            "contradicted_by_imported_database",
            "contradicted_by_regulator",
        }
    ]
    return f"Audited {total} external memo claim(s); {len(bad)} require manual review or contradict available evidence."


def _gaps(assessments: list[dict[str, Any]]) -> list[str]:
    gaps = []
    for assessment in assessments:
        for missing in assessment.get("missing_evidence", []) or []:
            if missing not in gaps:
                gaps.append(missing)
    return gaps


def _cell(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split()).replace("|", "\\|")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _import_paths(import_paths: list[str | Path] | str | Path | None) -> list[Path]:
    if not import_paths:
        return []
    if isinstance(import_paths, (str, Path)):
        return [Path(import_paths)]
    return [Path(path) for path in import_paths]


def _regulators(regulators: list[str] | str | None) -> list[str]:
    if not regulators:
        return []
    if isinstance(regulators, str):
        values = regulators.split(",")
    else:
        values = regulators
    normalized = []
    for regulator in values:
        value = str(regulator).strip().lower()
        if value and value not in normalized:
            normalized.append(value)
    return normalized

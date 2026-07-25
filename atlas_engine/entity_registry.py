"""Local entity registry for repeated evidence-audit runs."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REGISTRY_DIR = ".atlas"
REGISTRY_FILE = "entity_registry.json"


def registry_path(workspace_path: str | Path) -> Path:
    workspace = Path(workspace_path).expanduser().resolve()
    return workspace / REGISTRY_DIR / REGISTRY_FILE


def load_entity_registry(workspace_path: str | Path) -> dict[str, Any]:
    path = registry_path(workspace_path)
    if not path.exists():
        return {"entities": {}, "manual_decisions": [], "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"entities": {}, "manual_decisions": [], "updated_at": None}


def save_entity_registry(workspace_path: str | Path, registry: dict[str, Any]) -> str:
    path = registry_path(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["updated_at"] = _utc_now()
    path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def record_manual_decision(
    workspace_path: str | Path,
    company_name: str,
    decision: str,
    reason: str = "",
    run_id: str = "",
    evidence_ids: list[str] | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Persist a manual fund-memory decision for a company/entity."""
    registry = load_entity_registry(workspace_path)
    entities = registry.setdefault("entities", {})
    key = _key(company_name)
    entity = entities.setdefault(key, {
        "company_name": company_name,
        "aliases": [],
        "prior_support_status": None,
        "prior_rejected_reasons": [],
        "evidence_ids": [],
        "last_seen_run_id": None,
    })
    entity["company_name"] = entity.get("company_name") or company_name
    entity["last_seen_run_id"] = run_id or entity.get("last_seen_run_id")
    if decision:
        entity["prior_support_status"] = decision
    for alias in aliases or []:
        if alias and alias not in entity["aliases"]:
            entity["aliases"].append(alias)
    for evidence_id in evidence_ids or []:
        if evidence_id and evidence_id not in entity["evidence_ids"]:
            entity["evidence_ids"].append(evidence_id)
    if reason and reason not in entity["prior_rejected_reasons"]:
        entity["prior_rejected_reasons"].append(reason)

    registry.setdefault("manual_decisions", []).append({
        "company_name": company_name,
        "decision": decision,
        "reason": reason,
        "run_id": run_id,
        "evidence_ids": evidence_ids or [],
        "created_at": _utc_now(),
    })
    save_entity_registry(workspace_path, registry)
    return entity


def remember_run_entities(workspace_path: str | Path | None, result: dict[str, Any]) -> None:
    """Update entity registry with companies and council-rejected false positives."""
    if not workspace_path:
        return
    registry = load_entity_registry(workspace_path)
    entities = registry.setdefault("entities", {})
    run_id = str(result.get("run_id") or "")
    for company in result.get("companies", []) or []:
        name = str(company.get("name") or "").strip()
        if not name:
            continue
        key = _key(name)
        entity = entities.setdefault(key, {
            "company_name": name,
            "aliases": [],
            "prior_support_status": None,
            "prior_rejected_reasons": [],
            "evidence_ids": [],
            "last_seen_run_id": None,
        })
        entity["company_name"] = name
        entity["prior_support_status"] = company.get("support_status") or entity.get("prior_support_status")
        entity["last_seen_run_id"] = run_id
        for evidence_id in company.get("evidence_ids", []) or []:
            if evidence_id and evidence_id not in entity["evidence_ids"]:
                entity["evidence_ids"].append(evidence_id)
    for rejected in result.get("removed_or_rejected_companies", []) or []:
        name = str(rejected.get("company") or "").strip()
        if not name:
            continue
        key = _key(name)
        entity = entities.setdefault(key, {
            "company_name": name,
            "aliases": [],
            "prior_support_status": None,
            "prior_rejected_reasons": [],
            "evidence_ids": [],
            "last_seen_run_id": None,
        })
        entity["prior_support_status"] = "rejected_false_positive"
        entity["last_seen_run_id"] = run_id
        reason = str(rejected.get("reason") or "")
        if reason and reason not in entity["prior_rejected_reasons"]:
            entity["prior_rejected_reasons"].append(reason)
    save_entity_registry(workspace_path, registry)


def registry_warnings(workspace_path: str | Path | None, names: list[str]) -> list[dict[str, Any]]:
    """Warn when current run entities match prior rejected false positives."""
    if not workspace_path:
        return []
    registry = load_entity_registry(workspace_path)
    entities = registry.get("entities", {})
    warnings = []
    for name in names:
        key = _key(name)
        entity = entities.get(key)
        if not entity:
            entity = _find_alias(entities, name)
        if not entity:
            continue
        if entity.get("prior_support_status") == "rejected_false_positive":
            warnings.append({
                "company_name": name,
                "warning": "Matched a prior rejected false positive in the local registry.",
                "prior_rejected_reasons": entity.get("prior_rejected_reasons", []),
                "last_seen_run_id": entity.get("last_seen_run_id"),
            })
    return warnings


def apply_registry_warnings(result: dict[str, Any]) -> dict[str, Any]:
    warnings = registry_warnings(
        result.get("workspace_path"),
        [str(company.get("name") or "") for company in result.get("companies", [])],
    )
    if warnings:
        result["entity_registry_warnings"] = warnings
        warning_by_company = {warning["company_name"]: warning for warning in warnings}
        for company in result.get("companies", []) or []:
            name = str(company.get("name") or "")
            warning = warning_by_company.get(name)
            if not warning:
                continue
            company["manual_review_state"] = "prior_rejected_false_positive"
            company["registry_warning"] = warning.get("warning", "")
            company.setdefault("metadata", {})
        for claim in result.get("claim_ledger", []) or []:
            company = str(claim.get("metadata", {}).get("company") or "")
            warning = warning_by_company.get(company)
            if not warning:
                continue
            claim.setdefault("metadata", {})
            claim["metadata"]["entity_registry_flag"] = "prior_rejected_false_positive"
            if warning.get("prior_rejected_reasons"):
                claim["metadata"]["registry_rejected_reasons"] = list(warning["prior_rejected_reasons"])
    return result


def apply_import_drift(
    workspace_path: str | Path | None,
    evidence_packets: list[dict[str, Any]],
    run_id: str = "",
) -> dict[str, Any]:
    """Compare imported snapshots with prior entity registry state.

    First import of an entity seeds the snapshot. Subsequent material field
    changes mark the entity as needing manual review.
    """
    if not workspace_path:
        return {"changes": [], "new_snapshots": [], "report_path": None}

    registry = load_entity_registry(workspace_path)
    entities = registry.setdefault("entities", {})
    changes: list[dict[str, Any]] = []
    new_snapshots: list[dict[str, Any]] = []

    for packet in evidence_packets:
        evidence_class = packet.get("evidence_class") or packet.get("source_metadata", {}).get("evidence_class")
        if evidence_class not in {"imported_market_database", "regulator_ground_truth"}:
            continue
        row = packet.get("source_metadata", {}).get("row") or {}
        entity_name = _entity_from_import_row(row, packet)
        if not entity_name:
            continue
        snapshot = _snapshot_from_row(row, packet)
        if not snapshot:
            continue

        key = _key(entity_name)
        entity = entities.setdefault(key, {
            "company_name": entity_name,
            "aliases": [],
            "prior_support_status": None,
            "prior_rejected_reasons": [],
            "evidence_ids": [],
            "last_seen_run_id": None,
        })
        entity["company_name"] = entity.get("company_name") or entity_name
        previous = entity.get("latest_import_snapshot") or {}
        changed_fields = _changed_snapshot_fields(previous, snapshot)
        evidence_id = packet.get("evidence_id")
        if evidence_id and evidence_id not in entity.setdefault("evidence_ids", []):
            entity["evidence_ids"].append(evidence_id)
        entity["latest_import_snapshot"] = snapshot
        entity["last_import_run_id"] = run_id or entity.get("last_import_run_id")
        entity["last_seen_run_id"] = run_id or entity.get("last_seen_run_id")

        if previous and changed_fields:
            entity["prior_support_status"] = "needs_manual_check"
            entity["needs_manual_check"] = True
            entity.setdefault("drift_flags", []).append({
                "run_id": run_id,
                "fields": changed_fields,
                "created_at": _utc_now(),
            })
            changes.append({
                "company_name": entity_name,
                "registry_key": key,
                "evidence_id": evidence_id,
                "changed_fields": changed_fields,
            })
        elif not previous:
            new_snapshots.append({
                "company_name": entity_name,
                "registry_key": key,
                "evidence_id": evidence_id,
            })

    report = {
        "run_id": run_id,
        "generated_at": _utc_now(),
        "changes": changes,
        "new_snapshots": new_snapshots,
        "material_change_count": len(changes),
    }
    registry.setdefault("drift_history", []).append(report)
    save_entity_registry(workspace_path, registry)

    path = Path(workspace_path).expanduser().resolve() / REGISTRY_DIR / "drift_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(path)
    return report


def _find_alias(entities: dict[str, Any], name: str) -> dict[str, Any] | None:
    normalized = _key(name)
    for entity in entities.values():
        aliases = [_key(alias) for alias in entity.get("aliases", [])]
        if normalized in aliases:
            return entity
    return None


def _entity_from_import_row(row: dict[str, Any], packet: dict[str, Any]) -> str:
    preferred = (
        "company",
        "company_name",
        "name",
        "legal_name",
        "startup",
        "entity",
        "target",
    )
    normalized = {_key(k): str(v).strip() for k, v in (row or {}).items()}
    for key in preferred:
        value = normalized.get(_key(key))
        if value:
            return value
    title = str(packet.get("title") or "")
    match = re.search(r"\b([A-Z][A-Za-z0-9&.\- ]{2,50})\b", title)
    return match.group(1).strip() if match else ""


def _snapshot_from_row(row: dict[str, Any], packet: dict[str, Any]) -> dict[str, str]:
    values = {}
    for key, value in (row or {}).items():
        clean_key = _key(str(key))
        clean_value = re.sub(r"\s+", " ", str(value or "").strip())
        if clean_key and clean_value:
            values[clean_key] = clean_value
    if values:
        return values
    excerpt = str(packet.get("excerpt") or "")
    if excerpt:
        return {"excerpt": excerpt[:500]}
    return {}


def _changed_snapshot_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, str]]:
    changes = []
    keys = sorted(set(previous) | set(current))
    for key in keys:
        old = str(previous.get(key, "")).strip()
        new = str(current.get(key, "")).strip()
        if old != new:
            changes.append({"field": key, "old": old, "new": new})
    return changes


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

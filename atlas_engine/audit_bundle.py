"""Replayable audit bundle export for Atlas runs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import RunManifest
from .workspace import source_manifest, source_manifest_hash


REQUIRED_BUNDLE_FILES = (
    "memo.md",
    "claim_ledger.json",
    "support_assessments.json",
    "evidence_packets.json",
    "lens_bundle.json",
    "claim_clusters.json",
    "source_manifest.json",
    "run_manifest.json",
)

TRUST_BUNDLE_FILES = (
    "trust_report.md",
    "evidence_rankings.json",
    "imported_market_sources.json",
    "canonical_trust_records.json",
    "replay_manifest.json",
)


def validate_bundle_output_path(bundle_dir: str | Path) -> Path:
    """Validate and create an audit bundle directory."""
    path = Path(bundle_dir).expanduser()
    if path.exists() and not path.is_dir():
        raise ValueError(f"Audit bundle path is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def export_audit_bundle(
    result: dict[str, Any],
    bundle_dir: str | Path,
    mode: str = "research",
    memo_markdown: str | None = None,
) -> str:
    """Write replayable run artifacts into a bundle directory."""
    path = validate_bundle_output_path(bundle_dir)
    workspace_path = result.get("workspace_path")
    manifest = source_manifest(workspace_path) if workspace_path else {
        "workspace": "",
        "index_path": "",
        "generated_at": _utc_now(),
        "documents": [],
    }
    manifest_hash = source_manifest_hash(manifest)
    run_manifest = RunManifest(
        run_id=str(result.get("run_id") or "unknown"),
        mode=mode,
        created_at=_utc_now(),
        workspace_path=workspace_path,
        thesis=result.get("thesis"),
        memo_path=result.get("memo_path") or result.get("external_memo_path"),
        source_manifest_hash=manifest_hash,
        inputs={
            "workspace_path": workspace_path,
            "external_memo_path": result.get("external_memo_path"),
            "max_iterations": result.get("max_iterations"),
        },
        outputs={
            "quality_score": result.get("quality_score"),
            "verdict": result.get("verdict"),
            "claim_count": len(result.get("claim_ledger", [])),
            "evidence_packet_count": len(result.get("evidence_packets", [])),
        },
    ).to_dict()

    if memo_markdown is None:
        raise ValueError("memo_markdown is required for a trust-audit bundle")
    memo_text = memo_markdown
    (path / "memo.md").write_text(memo_text, encoding="utf-8")
    trust_report = result.get("trust_report_markdown")
    if trust_report is not None:
        (path / "trust_report.md").write_text(str(trust_report), encoding="utf-8")
    _write_json(path / "claim_ledger.json", result.get("claim_ledger", []))
    _write_json(path / "support_assessments.json", result.get("support_assessments", []))
    _write_json(path / "evidence_packets.json", result.get("evidence_packets", []))
    _write_json(path / "lens_bundle.json", result.get("lens_bundle", {}))
    _write_json(path / "claim_clusters.json", result.get("claim_clusters", []))
    if "evidence_rankings" in result:
        _write_json(path / "evidence_rankings.json", result.get("evidence_rankings", []))
    if "imported_market_sources" in result:
        _write_json(path / "imported_market_sources.json", result.get("imported_market_sources", []))
    if "canonical_trust_records" in result:
        _write_json(path / "canonical_trust_records.json", result.get("canonical_trust_records", []))
    if "replay_manifest" in result:
        _write_json(path / "replay_manifest.json", result.get("replay_manifest", {}))
    if result.get("regulator_assessments") is not None:
        _write_json(path / "regulator_assessments.json", result.get("regulator_assessments", []))
    _write_json(path / "source_manifest.json", manifest)
    _write_json(path / "run_manifest.json", run_manifest)
    return str(path)


def write_memo_and_bundle(
    result: dict[str, Any],
    export_md: str | Path | None = None,
    export_bundle: str | Path | None = None,
    mode: str = "research",
    memo_markdown: str | None = None,
) -> dict[str, Any]:
    """Write optional markdown and audit bundle outputs, mutating result paths."""
    if export_md:
        path = _validate_markdown_output(export_md)
        if memo_markdown is None:
            raise ValueError("memo_markdown is required for a trust-audit report")
        path.write_text(memo_markdown, encoding="utf-8")
        result["memo_path"] = str(path)
    if export_bundle:
        if "memo_path" not in result:
            bundle_path = Path(export_bundle).expanduser()
            bundle_path.mkdir(parents=True, exist_ok=True)
            memo_path = bundle_path / "memo.md"
            if memo_markdown is None:
                raise ValueError("memo_markdown is required for a trust-audit bundle")
            memo_path.write_text(memo_markdown, encoding="utf-8")
            result["memo_path"] = str(memo_path)
        result["audit_bundle_path"] = export_audit_bundle(result, export_bundle, mode=mode, memo_markdown=memo_markdown)
    return result


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_markdown_output(memo_out: str | Path) -> Path:
    path = Path(memo_out).expanduser()
    parent = path.parent if str(path.parent) else Path(".")
    if parent and not parent.exists():
        raise ValueError(f"Memo output directory does not exist: {parent}")
    if path.exists() and path.is_dir():
        raise ValueError(f"Memo output path is a directory: {path}")
    return path

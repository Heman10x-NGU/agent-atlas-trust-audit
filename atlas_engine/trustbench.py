"""Local deterministic TrustBench fixtures for Phase 3."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .contracts import HARD_STOP_STATUSES
from .external_memo import audit_external_memo
from .workspace import index_workspace


TRUSTBENCH_VERSION = "phase3.trustbench.v1"
TRUSTBENCH_CASES_PATH = Path(__file__).with_name("trustbench_cases.json")
TRUSTBENCH_THRESHOLDS = {
    "contradiction_recall": 0.8,
    "hard_stop_precision": 0.9,
    "unsupported_claim_recall": 0.8,
    "deterministic_replay_pass_rate": 1.0,
}


def run_trustbench(
    output_path: str | Path | None = None,
    fixtures_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run local fixture-based trust audit benchmark without network calls."""
    case_results = []
    specs = load_case_specs(fixtures_path)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for spec in specs:
            case_results.append(_run_case(root, spec))

    metrics = _score_cases(case_results)
    gates = {
        name: {
            "observed": metrics.get(name, 0.0),
            "threshold": threshold,
            "passed": metrics.get(name, 0.0) >= threshold,
        }
        for name, threshold in TRUSTBENCH_THRESHOLDS.items()
    }
    report = {
        "benchmark": "TrustBench",
        "version": TRUSTBENCH_VERSION,
        "passed": all(gate["passed"] for gate in gates.values()),
        "thresholds": TRUSTBENCH_THRESHOLDS,
        "metrics": metrics,
        "gates": gates,
        "cases": case_results,
    }
    if output_path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _run_case(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    case_root = root / spec["id"]
    workspace = case_root / "workspace"
    imports = case_root / "imports"
    memo = case_root / "memo.md"
    bundle = case_root / "bundle"
    workspace.mkdir(parents=True)
    imports.mkdir()

    for name, text in spec.get("workspace_files", {}).items():
        (workspace / name).write_text(text, encoding="utf-8")
    for name, text in spec.get("import_files", {}).items():
        (imports / name).write_text(text, encoding="utf-8")
    memo.write_text(spec["memo"], encoding="utf-8")
    index_workspace(workspace)

    seed_import_files = spec.get("seed_import_files") or {}
    if seed_import_files:
        seed_imports = case_root / "seed_imports"
        seed_imports.mkdir()
        for name, text in seed_import_files.items():
            (seed_imports / name).write_text(text, encoding="utf-8")
        audit_external_memo(
            memo,
            workspace,
            import_paths=[seed_imports],
            regulators=spec.get("regulators", []),
            run_id=f"{spec['id']}_seed",
        )

    first = audit_external_memo(
        memo,
        workspace,
        import_paths=[imports],
        regulators=spec.get("regulators", []),
        export_bundle=bundle,
        run_id=f"{spec['id']}_run_1",
    )
    second = audit_external_memo(
        memo,
        workspace,
        import_paths=[imports],
        regulators=spec.get("regulators", []),
        run_id=f"{spec['id']}_run_2",
    )

    detected = _detected_hard_stop_claims(first)
    unsupported = _detected_unsupported_claims(first)
    expected = set(spec.get("expected_contradictions", []))
    expected_unsupported = set(spec.get("expected_unsupported", []))
    false_hard_stops = sorted(detected - expected)
    missed = sorted(expected - detected)
    missed_unsupported = sorted(expected_unsupported - unsupported)
    drift = first.get("drift_report") or {}
    return {
        "id": spec["id"],
        "description": spec["description"],
        "verdict": first.get("verdict"),
        "expected_contradictions": sorted(expected),
        "detected_hard_stops": sorted(detected),
        "false_hard_stops": false_hard_stops,
        "missed_contradictions": missed,
        "expected_unsupported": sorted(expected_unsupported),
        "detected_unsupported": sorted(unsupported),
        "missed_unsupported": missed_unsupported,
        "hard_stop_count": len(detected),
        "deterministic_replay": (
            first.get("replay_manifest", {}).get("replay_fingerprint")
            == second.get("replay_manifest", {}).get("replay_fingerprint")
        ),
        "replay_fingerprint": first.get("replay_manifest", {}).get("replay_fingerprint"),
        "support_distribution": first.get("support_distribution", {}),
        "drift_material_change_count": drift.get("material_change_count", 0),
        "expected_drift": int(spec.get("expected_drift", 0)),
    }


def _score_cases(case_results: list[dict[str, Any]]) -> dict[str, float]:
    expected_total = sum(len(case.get("expected_contradictions", [])) for case in case_results)
    detected_expected = sum(
        len(set(case.get("expected_contradictions", [])) & set(case.get("detected_hard_stops", [])))
        for case in case_results
    )
    detected_total = sum(len(case.get("detected_hard_stops", [])) for case in case_results)
    false_total = sum(len(case.get("false_hard_stops", [])) for case in case_results)
    unsupported_expected_total = sum(len(case.get("expected_unsupported", [])) for case in case_results)
    unsupported_detected_expected = sum(
        len(set(case.get("expected_unsupported", [])) & set(case.get("detected_unsupported", [])))
        for case in case_results
    )
    deterministic_total = sum(1 for case in case_results if case.get("deterministic_replay"))
    drift_expected = [case for case in case_results if case.get("expected_drift")]
    drift_hits = sum(
        1
        for case in drift_expected
        if case.get("drift_material_change_count", 0) >= case.get("expected_drift", 0)
    )
    return {
        "case_count": float(len(case_results)),
        "contradiction_recall": round(detected_expected / expected_total, 3) if expected_total else 1.0,
        "hard_stop_precision": round((detected_total - false_total) / detected_total, 3) if detected_total else 1.0,
        "unsupported_claim_recall": (
            round(unsupported_detected_expected / unsupported_expected_total, 3)
            if unsupported_expected_total
            else 1.0
        ),
        "deterministic_replay_pass_rate": round(deterministic_total / len(case_results), 3) if case_results else 0.0,
        "drift_detection_rate": round(drift_hits / len(drift_expected), 3) if drift_expected else 1.0,
    }


def _detected_hard_stop_claims(result: dict[str, Any]) -> set[str]:
    return {
        str(assessment.get("claim_id"))
        for assessment in result.get("support_assessments", [])
        if assessment.get("status") in HARD_STOP_STATUSES
    }


def _detected_unsupported_claims(result: dict[str, Any]) -> set[str]:
    return {
        str(assessment.get("claim_id"))
        for assessment in result.get("support_assessments", [])
        if assessment.get("status") in {"unsupported", "needs_manual_check", "regulator_unavailable", "data_stale"}
    }


def load_case_specs(fixtures_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = Path(fixtures_path).expanduser() if fixtures_path else TRUSTBENCH_CASES_PATH
    if not path.exists():
        raise ValueError(f"TrustBench fixture file does not exist: {path}")
    specs = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(specs, list) or not specs:
        raise ValueError("TrustBench fixture file must contain a non-empty list of cases.")
    for spec in specs:
        if not isinstance(spec, dict) or not spec.get("id") or not spec.get("memo"):
            raise ValueError("Each TrustBench case must include at least id and memo.")
    return specs

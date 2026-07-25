import json
import tempfile
import unittest
from pathlib import Path

from atlas_engine.external_memo import audit_external_memo
from atlas_engine.trustbench import TRUSTBENCH_CASES_PATH, load_case_specs, run_trustbench
from atlas_engine.workspace import index_workspace


class CanonicalTrustTests(unittest.TestCase):
    def test_audit_exports_canonical_trust_records_and_replay_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            imports = root / "imports"
            bundle = root / "bundle"
            workspace.mkdir()
            imports.mkdir()
            memo = root / "memo.md"

            (workspace / "deal.md").write_text("# Acme\nAcme is a synthetic diligence record.\n", encoding="utf-8")
            (imports / "tracxn_export.csv").write_text(
                "company,funding_stage,employees\nAcme,Series B,42\n",
                encoding="utf-8",
            )
            memo.write_text("# Memo\n- Acme funding stage is Seed.\n", encoding="utf-8")
            index_workspace(workspace)

            first = audit_external_memo(
                memo,
                workspace,
                import_paths=[imports],
                export_bundle=bundle,
                run_id="phase3_test_1",
            )
            second = audit_external_memo(
                memo,
                workspace,
                import_paths=[imports],
                run_id="phase3_test_2",
            )

            records = first["canonical_trust_records"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["schema_version"], "phase3.canonical_trust.v1")
            self.assertEqual(records[0]["support_status"], "contradicted_by_imported_database")
            self.assertTrue(records[0]["hard_stop"])
            self.assertTrue(records[0]["record_fingerprint"].startswith("sha256:"))
            self.assertTrue(records[0]["best_evidence"][0]["evidence_fingerprint"].startswith("sha256:"))
            self.assertEqual(
                first["replay_manifest"]["replay_fingerprint"],
                second["replay_manifest"]["replay_fingerprint"],
            )
            self.assertTrue((bundle / "canonical_trust_records.json").exists())
            self.assertTrue((bundle / "replay_manifest.json").exists())
            bundle_manifest = json.loads((bundle / "replay_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(bundle_manifest["hard_stop_count"], 1)

            report = first["trust_report_markdown"]
            for section in (
                "# Trust Report",
                "## Source",
                "## Hard Stops",
                "## Support Summary",
                "## Claims Needing Review",
                "## Full Claim Ledger",
                "## Imported Sources",
                "## Drift",
                "## Replay",
            ):
                self.assertIn(section, report)
            self.assertIn("contradicted_by_imported_database", report)
            self.assertIn("replay_fingerprint: sha256:", report)

    def test_trustbench_passes_local_phase3_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "trustbench_report.json"
            report = run_trustbench(output_path=report_path)

            self.assertTrue(report["passed"])
            self.assertGreaterEqual(report["metrics"]["contradiction_recall"], 0.8)
            self.assertGreaterEqual(report["metrics"]["hard_stop_precision"], 0.9)
            self.assertGreaterEqual(report["metrics"]["unsupported_claim_recall"], 0.8)
            self.assertEqual(report["metrics"]["deterministic_replay_pass_rate"], 1.0)
            self.assertEqual(report["metrics"]["drift_detection_rate"], 1.0)
            self.assertTrue(report_path.exists())

    def test_trustbench_cases_are_file_backed(self):
        cases = load_case_specs()

        self.assertTrue(TRUSTBENCH_CASES_PATH.exists())
        self.assertGreaterEqual(len(cases), 5)
        self.assertTrue(any(case["id"] == "unsupported_claim" for case in cases))

if __name__ == "__main__":
    unittest.main()

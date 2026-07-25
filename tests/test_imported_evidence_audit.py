import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from atlas_engine.external_memo import audit_external_memo
from atlas_engine.imports import classify_import_source, load_imported_evidence
from atlas_engine.judgment import assess_claim_support
from atlas_engine.evidence import normalize_evidence_packets
from atlas_engine.workspace import index_workspace


class ImportedEvidenceTests(unittest.TestCase):
    def test_classifies_and_loads_csv_xlsx_pdf_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracxn = root / "tracxn_export.csv"
            askpc = root / "askpc_privatecircle_export.xlsx"
            mca = root / "mca_charges.pdf"
            tracxn.write_text("company,funding_stage,employees\nAcme,Seed,42\n", encoding="utf-8")
            _write_xlsx(askpc, "Company Acme revenue INR 5 crore")
            mca.write_bytes(b"%PDF-1.4\n1 0 obj <<>> stream\n(Acme charges amount INR 2 crore)\nendstream\n")

            self.assertEqual(classify_import_source(tracxn), "imported_market_database")
            self.assertEqual(classify_import_source(askpc), "imported_market_database")
            self.assertEqual(classify_import_source(mca), "regulator_ground_truth")

            evidence, manifest = load_imported_evidence([root])

            self.assertGreaterEqual(len(evidence), 3)
            self.assertEqual(len(manifest), 3)
            self.assertTrue(any(row["evidence_class"] == "regulator_ground_truth" for row in evidence))
            self.assertTrue(any(row["provider"] == "tracxn" for row in evidence))


class ImportedMemoAuditTests(unittest.TestCase):
    def test_audit_memo_hard_stops_on_imported_database_contradiction_and_exports_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            imports = root / "imports"
            workspace.mkdir()
            imports.mkdir()
            memo = root / "memo.md"
            audit_md = root / "trust_report.md"
            bundle = root / "bundle"

            (workspace / "deal.md").write_text("# Acme\nAcme is a diligence target.\n", encoding="utf-8")
            (imports / "tracxn_export.csv").write_text(
                "company,funding_stage,employees\nAcme,Series B,42\n",
                encoding="utf-8",
            )
            memo.write_text(
                "# Imported Memo\n"
                "- Acme funding stage is Seed.\n",
                encoding="utf-8",
            )
            index_workspace(workspace)

            result = audit_external_memo(
                memo,
                workspace,
                import_paths=[imports],
                export_md=audit_md,
                export_bundle=bundle,
            )

            statuses = {assessment["status"] for assessment in result["support_assessments"]}
            self.assertIn("contradicted_by_imported_database", statuses)
            self.assertEqual(result["convergence_report"]["stop_reason"], "hard_stop_contradiction")
            self.assertTrue(audit_md.exists())
            self.assertTrue((bundle / "trust_report.md").exists())
            self.assertTrue((bundle / "evidence_rankings.json").exists())
            self.assertTrue((bundle / "imported_market_sources.json").exists())
            report = audit_md.read_text(encoding="utf-8")
            self.assertIn("## Hard Stops", report)
            self.assertIn("contradicted_by_imported_database", report)

    def test_regulator_mode_reports_verified_and_regulator_assessments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            imports = root / "imports"
            workspace.mkdir()
            imports.mkdir()
            memo = root / "memo.md"
            bundle = root / "bundle"

            (workspace / "deal.md").write_text("# Acme\nAcme local notes.\n", encoding="utf-8")
            (imports / "mca_export.csv").write_text(
                "company,incorporation_year,director\nAcme,2021,Director: Priya Shah\n",
                encoding="utf-8",
            )
            memo.write_text(
                "# Imported Memo\n"
                "- Acme incorporation year is 2021.\n",
                encoding="utf-8",
            )
            index_workspace(workspace)

            result = audit_external_memo(
                memo,
                workspace,
                import_paths=[imports],
                regulators=["mca"],
                export_bundle=bundle,
            )

            statuses = {assessment["status"] for assessment in result["support_assessments"]}
            self.assertIn("verified_by_regulator", statuses)
            self.assertTrue((bundle / "regulator_assessments.json").exists())
            regulator_rows = json.loads((bundle / "regulator_assessments.json").read_text(encoding="utf-8"))
            self.assertTrue(any(row["regulator"] == "mca" for row in regulator_rows))

    def test_drift_report_updates_entity_registry_and_marks_manual_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            first_imports = root / "imports1"
            second_imports = root / "imports2"
            workspace.mkdir()
            first_imports.mkdir()
            second_imports.mkdir()
            memo = root / "memo.md"
            memo.write_text("# Imported Memo\n- Acme has 42 employees.\n", encoding="utf-8")
            (workspace / "deal.md").write_text("# Acme\nAcme local notes.\n", encoding="utf-8")
            (first_imports / "tracxn_export.csv").write_text("company,employees\nAcme,42\n", encoding="utf-8")
            (second_imports / "tracxn_export.csv").write_text("company,employees\nAcme,58\n", encoding="utf-8")
            index_workspace(workspace)

            audit_external_memo(memo, workspace, import_paths=[first_imports])
            result = audit_external_memo(memo, workspace, import_paths=[second_imports])

            drift = result["drift_report"]
            self.assertEqual(drift["material_change_count"], 1)
            registry = json.loads((workspace / ".atlas" / "entity_registry.json").read_text(encoding="utf-8"))
            self.assertTrue(registry["entities"]["acme"]["needs_manual_check"])
            self.assertEqual(registry["entities"]["acme"]["prior_support_status"], "needs_manual_check")


class ClaimJudgmentTests(unittest.TestCase):
    def test_deterministic_claim_types_are_supported_by_imported_rows(self):
        packets = normalize_evidence_packets(
            [
                {
                    "title": "AskPC row",
                    "url": "import://askpc#row-1",
                    "snippet": "company: Acme | revenue: INR 5 crore | employees: 42 | incorporation_year: 2021 | dpiit: not recognized | charges: INR 2 crore",
                    "source": "imported_market_database",
                    "evidence_class": "imported_market_database",
                }
            ]
        )
        claims = [
            _claim("c1", "Acme", "revenue", "Acme revenue is INR 5 crore."),
            _claim("c2", "Acme", "employee_count", "Acme has 42 employees."),
            _claim("c3", "Acme", "incorporation_year", "Acme incorporation year is 2021."),
            _claim("c4", "Acme", "dpiit_startup_recognition", "Acme has DPIIT startup recognition."),
            _claim("c5", "Acme", "charges", "Acme has no charges."),
        ]

        assessments = assess_claim_support(claims, packets, trust_mode=True)
        statuses = {assessment["claim_id"]: assessment["status"] for assessment in assessments}

        self.assertEqual(statuses["c1"], "verified")
        self.assertEqual(statuses["c2"], "verified")
        self.assertEqual(statuses["c3"], "verified")
        self.assertEqual(statuses["c4"], "contradicted_by_imported_database")
        self.assertEqual(statuses["c5"], "contradicted_by_imported_database")

def _claim(claim_id: str, subject: str, claim_type: str, text: str) -> dict:
    return {
        "claim_id": claim_id,
        "text": text,
        "subject": subject,
        "claim_type": claim_type,
        "memo_section": "external_memo",
        "cited_evidence_ids": [],
        "metadata": {"company": subject},
    }


def _write_xlsx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>{text}</t></si>
</sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>
</worksheet>""",
        )


if __name__ == "__main__":
    unittest.main()

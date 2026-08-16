from __future__ import annotations

import ast
import json
import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class RepositoryContractTest(unittest.TestCase):
    def test_skill_manifest_tracks_every_repository_file(self) -> None:
        manifest = load(ROOT / "skill-manifest.json")
        actual = set()
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith(".git/") or "__pycache__/" in relative or relative.endswith(".pyc"):
                continue
            if relative.startswith((".scratch/", "artifacts/", "dist/", ".venv/", ".pytest_cache/")):
                continue
            actual.add(relative)
        self.assertEqual(actual, set(manifest["tracked_files"]))

    def test_release_manifest_and_machine_contract_versions_agree(self) -> None:
        release = load(ROOT / "release.json")
        manifest = load(ROOT / "skill-manifest.json")
        rules = load(ROOT / "contracts" / "machine-rules.json")

        self.assertEqual(release["skillVersion"], manifest["version"])
        self.assertEqual(release["artifactSchemaVersion"], rules["schemaVersion"])
        self.assertNotIn(
            release["skillVersion"],
            (ROOT / "SKILL.md").read_text(encoding="utf-8"),
        )

    def test_formal_projection_and_schema_share_one_field_set(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        schema = load(ROOT / "contracts" / "gallery-template.schema.json")

        self.assertTrue(
            set(rules["formalProjection"]["topLevel"].values()).issubset(schema["properties"])
        )
        self.assertEqual(
            "1ebe5cb0790fa20e5968570c7b09d83d7c14b9347bcf5e60ca612384a3a81619",
            hashlib.sha256((ROOT / "contracts" / "gallery-template.schema.json").read_bytes()).hexdigest(),
        )

    def test_issue_2_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E01", "E04", "E05", "E07", "E10", "E11", "E19", "E21", "E27", "E35", "E36", "E38"}
            <= set(rules["historicalExperienceEvidence"])
        )

    def test_issue_3_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E05", "E06", "E07", "E10", "E25"}
            <= set(rules["historicalExperienceEvidence"])
        )

    def test_issue_4_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E06", "E10", "E11", "E12", "E13", "E29", "E34"}
            <= set(rules["historicalExperienceEvidence"])
        )

    def test_issue_5_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E18", "E19", "E20", "E21", "E22", "E24", "E25", "E26", "E27", "E28", "E30", "E31"}
            <= set(rules["historicalExperienceEvidence"])
        )

    def test_issue_6_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E30", "E31", "E35", "E36", "E38", "E39"}
            <= set(rules["historicalExperienceEvidence"])
        )

    def test_issue_7_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E06", "E07", "E08", "E09", "E19", "E20", "E26", "E28"}
            <= set(rules["historicalExperienceEvidence"])
        )

    def test_replacement_enums_expose_named_domain_roles(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")

        self.assertIsInstance(rules["sourceCategories"], dict)
        self.assertIsInstance(rules["strategySources"], dict)
        self.assertIn("unknownCategory", rules["sourceCategories"])
        self.assertIn("textContent", rules["sourceCategories"])
        self.assertIn("sceneAttribute", rules["sourceCategories"])
        self.assertEqual(
            {"perImageDecision", "batchDecision", "autonomousDecision"},
            set(rules["strategySources"]),
        )

    def test_workflow_consumes_machine_states_and_error_codes_without_copying_values(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        workflow_source = (ROOT / "scripts" / "produce_meme_template" / "workflow.py").read_text(encoding="utf-8")
        test_sources = [path.read_text(encoding="utf-8") for path in sorted((ROOT / "tests").glob("test_issue_*.py"))]
        sources = [workflow_source, *test_sources]
        machine_values = [rules["initialState"], *rules["resultStates"].values()]
        machine_values.extend(item["state"] for item in rules["productionPhases"])
        machine_values.extend(rules["errorCodes"].values())
        machine_values.extend(rules["sourceCategories"].values())
        machine_values.extend(rules["strategySources"].values())
        machine_values.extend(rules["invalidationReasons"].values())

        string_literals = [
            {node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            for source in sources
        ]
        for value in set(machine_values):
            for literals in string_literals:
                self.assertNotIn(value, literals)

        workflow_literals, *test_literal_sets = string_literals
        for phase in rules["productionPhases"]:
            self.assertNotIn(phase["phase"], workflow_literals)
            for test_literals in test_literal_sets:
                self.assertNotIn(phase["phase"], test_literals)

    def test_issue_5_test_consumes_slot_contract_values_without_copying_them(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        slot_contract = rules["slotCompilationContract"]
        machine_values = set()
        for field in (
            "subjectKinds",
            "semanticRoles",
            "slotTypes",
            "valueGateRoles",
            "personAttributeRoles",
            "assetUnitCountFields",
            "singleSlotReviewAxes",
        ):
            machine_values.update(slot_contract[field].values())

        source = (ROOT / "tests" / "test_issue_5_editable_prompt_compiler.py").read_text(encoding="utf-8")
        literals = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertTrue(machine_values.isdisjoint(literals))

    def test_issue_7_workflow_and_test_consume_identity_values_without_copying_them(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        identity_contract = rules["identityReplacementContract"]
        contract_role_names = set()

        def collect_role_names(value: object) -> None:
            if isinstance(value, dict):
                contract_role_names.update(value)
                for nested in value.values():
                    collect_role_names(nested)

        collect_role_names(identity_contract)
        machine_values = set()
        for field in (
            "planFields",
            "sourceFields",
            "candidateFields",
            "generationSectionRoles",
            "routeEvidenceFields",
            "candidateCardFields",
            "distinctIdentityEvidenceFields",
            "dependencyTypes",
            "topologyFields",
            "dependencyFields",
            "identityTextActions",
            "identityTextRelationshipTypes",
            "identityTextDecisionFields",
            "frozenConflictEvaluationFields",
            "neutralityAuditFields",
        ):
            machine_values.update(identity_contract[field].values())
        for route in identity_contract["routes"].values():
            machine_values.add(route["mode"])
        machine_values.update(identity_contract["identityEquivalenceModifiers"].values())
        # Equal-name mapping keys are stable role names used to look up the
        # current value. Generic adapter fields are shared by older contracts.
        machine_values -= contract_role_names | {"evidence", "type", "value"}

        sources = [
            (ROOT / "scripts" / "produce_meme_template" / "workflow.py").read_text(encoding="utf-8"),
            (ROOT / "tests" / "test_issue_7_identity_replacement.py").read_text(encoding="utf-8"),
        ]
        for source in sources:
            literals = {
                node.value
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertTrue(machine_values.isdisjoint(literals))

    def test_visual_review_fields_are_read_through_machine_roles(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        evidence_fields = set(rules["visualReviewContract"]["evidenceFieldRoles"].values())
        workflow = ast.parse(
            (ROOT / "scripts" / "produce_meme_template" / "workflow.py").read_text(encoding="utf-8")
        )
        direct_review_fields = set()
        for node in ast.walk(workflow):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "review"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                direct_review_fields.add(node.slice.value)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "review"
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                direct_review_fields.add(node.args[0].value)

        self.assertTrue(evidence_fields.isdisjoint(direct_review_fields))


if __name__ == "__main__":
    unittest.main()

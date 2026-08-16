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

    def test_formal_projection_and_schema_share_one_field_set(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        schema = load(ROOT / "contracts" / "gallery-template.schema.json")

        self.assertTrue(set(rules["formalProjection"]["topLevel"]).issubset(schema["properties"]))
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


if __name__ == "__main__":
    unittest.main()

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

    def test_issue_8_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E14", "E15", "E16", "E17", "E18", "E24", "E27", "E28"}
            <= set(rules["historicalExperienceEvidence"])
        )

    def test_issue_9_experience_ids_are_machine_traceable(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        self.assertTrue(
            {"E06", "E12", "E20", "E22", "E23", "E29"}
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

    def test_visible_text_contract_values_have_one_machine_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["visibleTextContract"]
        contract_role_names = set()

        def collect_role_names(value: object) -> None:
            if isinstance(value, dict):
                contract_role_names.update(value)
                for nested in value.values():
                    collect_role_names(nested)

        collect_role_names(contract)
        machine_values = {contract["slotBindingField"]}
        for field in (
            "analysisFields",
            "inventoryFields",
            "regionFields",
            "roles",
            "actions",
            "valueClasses",
            "exactEvidenceFields",
            "languageValues",
            "semanticAuditFields",
            "semanticDecisionFields",
            "slotOriginFields",
            "freeContentOriginFields",
        ):
            machine_values.update(contract[field].values())
        for values in contract["allowedActionsByRole"].values():
            machine_values.update(values)
        machine_values.update(contract["openSlotValueClasses"])
        machine_values.update(contract["freeEditableValueClasses"])
        machine_values.update(contract["nonSlotValueClasses"])
        machine_values -= contract_role_names | {
            "id",
            "evidence",
            "preserve",
            "remove",
            "review",
            "content",
            "attribution",
            "watermark",
            "brand",
            "ambiguous",
        }

        sources = [
            ROOT / "scripts" / "produce_meme_template" / "workflow.py",
            ROOT / "scripts" / "produce_meme_template" / "adapters.py",
            ROOT / "tests" / "test_issue_5_editable_prompt_compiler.py",
            ROOT / "tests" / "test_issue_7_identity_replacement.py",
            ROOT / "tests" / "test_issue_8_text_dense_templates.py",
        ]
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            literals = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertTrue(machine_values.isdisjoint(literals), path.as_posix())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                    continue
                collection = {
                    item.value
                    for item in node.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
                self.assertNotEqual(
                    set(contract["commonPunctuationCharacters"]),
                    collection,
                    path.as_posix(),
                )

    def test_multi_instance_contract_values_have_one_machine_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["multiInstanceContract"]
        self.assertNotIn("maximumUploadsPerControl", contract)
        self.assertNotIn("visualEvidenceField", contract)
        self.assertEqual(
            {
                "operationIdentity",
                "targetComponents",
                "stableAnchors",
                "controls",
                "explanation",
            },
            set(contract["approvedOperationBindingFields"]),
        )
        self.assertEqual(
            len(contract["approvedOperationBindingFields"]),
            len(set(contract["approvedOperationBindingFields"].values())),
        )
        self.assertEqual(set(contract["operations"]), set(contract["operationRequirements"]))
        for requirement in contract["operationRequirements"].values():
            self.assertEqual(
                {
                    "minimumTargets",
                    "requiredTargetRoleKeys",
                    "allowedTargetRoleKeys",
                    "requiredAnchorRoleKeys",
                    "requiredRelationTypeKeys",
                    "singleIdentityUnit",
                    "targetContainersMustBeAnchors",
                    "requiresCompleteOrderedChain",
                },
                set(requirement),
            )
            self.assertTrue(
                set(requirement["requiredTargetRoleKeys"])
                | set(requirement["allowedTargetRoleKeys"])
                | set(requirement["requiredAnchorRoleKeys"])
                <= set(contract["componentRoles"])
            )
            self.assertTrue(
                set(requirement["requiredRelationTypeKeys"])
                <= set(contract["relationTypes"])
            )
            self.assertIsInstance(requirement["minimumTargets"], int)
            self.assertNotIsInstance(requirement["minimumTargets"], bool)
            self.assertGreater(requirement["minimumTargets"], 0)
            for field in (
                "singleIdentityUnit",
                "targetContainersMustBeAnchors",
                "requiresCompleteOrderedChain",
            ):
                self.assertIsInstance(requirement[field], bool)
            for field in (
                "requiredTargetRoleKeys",
                "allowedTargetRoleKeys",
                "requiredAnchorRoleKeys",
                "requiredRelationTypeKeys",
            ):
                self.assertIsInstance(requirement[field], list)
                self.assertEqual(len(requirement[field]), len(set(requirement[field])))
                self.assertTrue(all(isinstance(value, str) and value for value in requirement[field]))
        self.assertTrue(
            set(contract["relationTypeKeysRequiringPreservation"])
            <= set(contract["relationTypes"])
        )
        identity_contract = rules["identityReplacementContract"]
        operation_dependency_types = contract["operationDependencyTypes"]
        self.assertEqual(
            set(contract["operations"]) - {"identityReplace"},
            set(operation_dependency_types),
        )
        self.assertEqual(
            len(operation_dependency_types.values()),
            len(set(operation_dependency_types.values())),
        )
        self.assertTrue(
            set(operation_dependency_types.values())
            <= set(identity_contract["dependencyTypes"])
        )
        self.assertEqual(
            set(identity_contract["dependencyComponentRoleKeys"]),
            set(identity_contract["dependencyRelationTypeKeys"]),
        )
        self.assertTrue(
            set(identity_contract["dependencyComponentRoleKeys"])
            <= set(identity_contract["dependencyTypes"])
        )
        for role_keys in identity_contract["dependencyComponentRoleKeys"].values():
            self.assertTrue(set(role_keys) <= set(contract["componentRoles"]))
        for relation_keys in identity_contract["dependencyRelationTypeKeys"].values():
            self.assertTrue(set(relation_keys) <= set(contract["relationTypes"]))
        slot_contract = rules["slotCompilationContract"]
        semantic_role_keys = {
            *slot_contract["semanticRoles"],
            *slot_contract["personAttributeRoles"],
        }
        self.assertEqual(
            set(contract["approvedControlBindings"]),
            set(contract["componentRoles"]),
        )
        for bindings in contract["approvedControlBindings"].values():
            self.assertIsInstance(bindings, list)
            self.assertEqual(
                len(bindings),
                len(
                    {
                        (binding["slotTypeRole"], binding["semanticRoleKey"])
                        for binding in bindings
                    }
                ),
            )
            for binding in bindings:
                self.assertEqual(
                    {"slotTypeRole", "semanticRoleKey"}, set(binding)
                )
                self.assertIn(binding["slotTypeRole"], slot_contract["slotTypes"])
                self.assertIn(binding["semanticRoleKey"], semantic_role_keys)
        self.assertEqual(
            set(contract["relationEndpointRoleKeyPairs"]),
            set(contract["relationTypes"]),
        )
        for role_pairs in contract["relationEndpointRoleKeyPairs"].values():
            self.assertIsInstance(role_pairs, list)
            self.assertTrue(role_pairs)
            self.assertEqual(
                len(role_pairs), len({tuple(pair) for pair in role_pairs})
            )
            self.assertTrue(
                all(
                    isinstance(pair, list)
                    and len(pair) == 2
                    and all(role in contract["componentRoles"] for role in pair)
                    for pair in role_pairs
                )
            )
        approved_identity_roles = contract["approvedIdentityDependencyRoleKeys"]
        identity_contract = rules["identityReplacementContract"]
        self.assertIsInstance(approved_identity_roles, list)
        self.assertTrue(approved_identity_roles)
        self.assertEqual(
            len(approved_identity_roles), len(set(approved_identity_roles))
        )
        self.assertTrue(
            set(approved_identity_roles)
            <= set(identity_contract["dependencyComponentRoleKeys"])
        )
        self.assertTrue(
            set(approved_identity_roles)
            <= set(identity_contract["dependencyRelationTypeKeys"])
        )
        role_names = set()

        def collect_role_names(value: object) -> None:
            if isinstance(value, dict):
                role_names.update(value)
                for nested in value.values():
                    collect_role_names(nested)

        collect_role_names(contract)
        machine_values = {contract["subjectImageMaxCountField"]}
        for field in (
            "sourceFields",
            "approvedFields",
            "planFields",
            "generationFields",
            "graphFields",
            "componentFields",
            "componentRoles",
            "relationFields",
            "relationTypes",
            "operationFields",
            "operations",
            "operationReviewFields",
            "approvedOperationBindingFields",
        ):
            machine_values.update(contract[field].values())
        machine_values.update(identity_contract["dependencyTypes"].values())
        machine_values.update(
            contract["relationTypes"][role]
            for role in contract["relationTypeKeysRequiringPreservation"]
        )
        machine_values -= role_names | {"id", "type", "role", "evidence"}

        sources = [
            ROOT / "scripts" / "produce_meme_template" / "workflow.py",
            ROOT / "tests" / "fixture_contracts.py",
            ROOT / "tests" / "test_issue_9_multi_instance_operations.py",
        ]
        for path in sources:
            literals = {
                node.value
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertTrue(machine_values.isdisjoint(literals), path.as_posix())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import json
import hashlib
import re
import subprocess
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class RepositoryContractTest(unittest.TestCase):
    def test_generated_readme_is_current(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/update_readme.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def test_skill_manifest_tracks_every_repository_file(self) -> None:
        manifest = load(ROOT / "skill-manifest.json")
        actual = set()
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith(".git/") or "__pycache__/" in relative or relative.endswith(".pyc"):
                continue
            if relative in {".DS_Store", ".env", ".env.local"}:
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

    def test_runtime_module_dependencies_are_acyclic(self) -> None:
        package = ROOT / "scripts" / "produce_meme_template"
        modules = {path.stem: path for path in package.glob("*.py")}
        graph = {name: set() for name in modules}
        for name, path in modules.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 1
                    and node.module in modules
                ):
                    graph[name].add(node.module)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str, path: tuple[str, ...]) -> None:
            if name in visiting:
                cycle_start = path.index(name)
                self.fail(
                    "runtime module dependency cycle: "
                    + " -> ".join((*path[cycle_start:], name))
                )
            if name in visited:
                return
            visiting.add(name)
            for dependency in sorted(graph[name]):
                visit(dependency, (*path, name))
            visiting.remove(name)
            visited.add(name)

        for name in sorted(graph):
            visit(name, ())

    def test_release_readiness_contract_has_one_typed_machine_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["releaseReadinessContract"]
        mapping_names = (
            "corpusFields",
            "corpusScenarioFields",
            "requestFields",
            "scenarioFields",
            "sourceProvenanceFields",
            "productionRequestFields",
            "scenarioRoles",
            "scenarioSourceCategoryRoleKeys",
            "fixtureDirectoryByScenarioRoleKey",
            "executionModes",
            "liveCredentialEnvironment",
            "liveReviewAdapterFields",
            "liveReviewEvidenceFields",
            "externalExecutionStatuses",
            "externalExecutionFields",
            "requestLedgerFields",
            "completionFields",
            "releaseGateFields",
            "releaseGateEvidenceFields",
            "reviewReceiptFields",
            "reviewAxes",
            "workspaceDirectories",
            "requiredLineageArtifacts",
            "outcomes",
            "errorCodes",
            "scenarioReportFields",
            "reportFields",
        )
        for mapping_name in mapping_names:
            mapping = contract[mapping_name]
            self.assertIsInstance(mapping, dict)
            self.assertEqual(len(mapping), len(set(mapping.values())))
            self.assertTrue(
                all(isinstance(value, str) and value for value in mapping.values())
            )
        self.assertEqual(
            set(contract["scenarioRoles"]),
            set(contract["scenarioSourceCategoryRoleKeys"]),
        )
        self.assertTrue(
            set(contract["scenarioSourceCategoryRoleKeys"].values())
            <= set(rules["sourceCategories"])
        )
        self.assertIn(
            contract["forwardSourceCategoryRoleKey"], rules["sourceCategories"]
        )
        self.assertEqual(
            {*contract["scenarioRoles"], "unseenForward"},
            set(contract["fixtureDirectoryByScenarioRoleKey"]),
        )
        self.assertEqual(
            set(contract["scenarioRoles"]),
            set(contract["recordedReplayScenarioRoleKeys"]),
        )
        for role_list_name in (
            "recordedReplayScenarioRoleKeys",
            "liveMandatoryScenarioRoleKeys",
            "liveSupplementScenarioRoleKeys",
        ):
            role_keys = contract[role_list_name]
            self.assertIsInstance(role_keys, list)
            self.assertEqual(len(role_keys), len(set(role_keys)))
            self.assertTrue(set(role_keys) <= set(contract["scenarioRoles"]))
        self.assertEqual(
            {"ordinaryPerson", "textDense", "complexMultiInstance"},
            set(contract["liveMandatoryScenarioRoleKeys"]),
        )
        self.assertEqual(
            {"knownCharacterIp", "genericAnimal"},
            set(contract["liveSupplementScenarioRoleKeys"]),
        )
        self.assertTrue(
            set(contract["liveMandatoryScenarioRoleKeys"]).isdisjoint(
                contract["liveSupplementScenarioRoleKeys"]
            )
        )
        supplement_count = contract["liveSupplementScenarioCount"]
        self.assertIsInstance(supplement_count, int)
        self.assertNotIsInstance(supplement_count, bool)
        self.assertEqual(1, supplement_count)
        self.assertLessEqual(
            supplement_count, len(contract["liveSupplementScenarioRoleKeys"])
        )
        self.assertTrue(
            set(contract["prefixLineageArtifactRoles"])
            <= set(contract["requiredLineageArtifacts"])
        )
        self.assertEqual(
            len(contract["prefixLineageArtifactRoles"]),
            len(set(contract["prefixLineageArtifactRoles"])),
        )
        sample_count = contract["templateTest"]["sampleCount"]
        self.assertIsInstance(sample_count, int)
        self.assertNotIsInstance(sample_count, bool)
        self.assertGreater(sample_count, 0)
        self.assertLessEqual(sample_count, len(contract["scenarioRoles"]))
        self.assertTrue(contract["reportFileName"].endswith(".json"))
        self.assertEqual(
            len(contract["liveReviewMethodIds"]),
            len(set(contract["liveReviewMethodIds"])),
        )
        self.assertTrue(
            all(
                isinstance(value, str) and value
                for value in contract["liveReviewMethodIds"]
            )
        )
        corpus = load(ROOT / contract["corpusRelativePath"])
        corpus_fields = contract["corpusFields"]
        scenario_fields = contract["corpusScenarioFields"]
        self.assertEqual(set(corpus), set(corpus_fields.values()))
        self.assertTrue(
            all(
                set(item) == set(scenario_fields.values())
                for item in corpus[corpus_fields["scenarios"]]
            )
        )

    def test_formal_projection_and_schema_share_one_field_set(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        release_contract = rules["releaseManagementContract"]
        schema_path = ROOT / release_contract["gallerySnapshotRelativePath"]
        metadata = load(
            ROOT / release_contract["gallerySnapshotMetadataRelativePath"]
        )
        schema = load(schema_path)

        self.assertTrue(
            set(rules["formalProjection"]["topLevel"].values()).issubset(schema["properties"])
        )
        self.assertEqual(
            metadata["sourceArtifactSha256"],
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )
        self.assertEqual("gallery-template", metadata["contractId"])
        self.assertEqual(
            load(ROOT / "release.json")["supportedContracts"]["galleryTemplate"],
            metadata["contractVersion"],
        )

    def test_gallery_consumers_resolve_the_schema_from_the_machine_contract(self) -> None:
        from scripts.produce_meme_template.template_test import (
            GALLERY_SCHEMA_PATH as TEMPLATE_TEST_SCHEMA_PATH,
        )
        from scripts.produce_meme_template.workflow_core import (
            GALLERY_SCHEMA_PATH as WORKFLOW_SCHEMA_PATH,
        )

        rules = load(ROOT / "contracts" / "machine-rules.json")
        expected = (
            ROOT
            / rules["releaseManagementContract"]["gallerySnapshotRelativePath"]
        )
        self.assertEqual(expected, WORKFLOW_SCHEMA_PATH)
        self.assertEqual(expected, TEMPLATE_TEST_SCHEMA_PATH)

    def test_runtime_semantics_field_names_have_one_machine_mapping(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        schema_path = (
            ROOT
            / rules["releaseManagementContract"]["gallerySnapshotRelativePath"]
        )
        schema = load(schema_path)
        runtime_fields = set(
            rules["runtimeSemanticsContract"]["fields"].values()
        )
        schema_fields = set(
            schema["$defs"]["runtimeSemantics"]["properties"]
        )

        self.assertEqual(schema_fields, runtime_fields)

    def test_historical_experience_contract_has_one_complete_typed_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["historicalExperienceContract"]
        matrix = load(ROOT / contract["matrixRelativePath"])
        mapping_names = (
            "requiredCorpusRoles",
            "corpusContentKinds",
            "corpusBindingFields",
            "migrationStatuses",
            "evidencePolarities",
            "evidenceExpectationFields",
            "implementationKinds",
            "evidenceOutcomes",
            "evidenceResultFields",
            "outcomes",
            "failureCategories",
            "matrixFields",
            "experienceFields",
            "authorityFields",
            "implementationFields",
            "evidenceFields",
            "corpusFields",
            "reportFields",
            "reportExperienceFields",
            "reportEvidenceFields",
            "reportCorpusFields",
            "summaryFields",
        )
        for mapping_name in mapping_names:
            mapping = contract[mapping_name]
            self.assertIsInstance(mapping, dict)
            self.assertEqual(len(mapping), len(set(mapping.values())))
            self.assertTrue(
                all(isinstance(value, str) and value for value in mapping.values())
            )
        self.assertEqual(
            [f"E{index:02d}" for index in range(1, 40)],
            contract["experienceIds"],
        )
        experience_fields = contract["experienceFields"]
        matrix_fields = contract["matrixFields"]
        self.assertEqual(
            contract["experienceIds"],
            [
                item[experience_fields["experienceId"]]
                for item in matrix[matrix_fields["experiences"]]
            ],
        )
        self.assertEqual(
            set(contract["requiredCorpusRoles"].values()),
            set(matrix[matrix_fields["corpus"]]),
        )
        self.assertEqual(
            set(contract["requiredCorpusRoles"]),
            set(contract["requiredCorpusBindings"]),
        )
        binding_fields = set(contract["corpusBindingFields"].values())
        self.assertTrue(
            all(
                set(binding) == binding_fields
                for binding in contract["requiredCorpusBindings"].values()
            )
        )
        self.assertTrue(contract["retiredRepositoryPrefixes"])
        self.assertEqual(
            set(contract["experienceIds"]),
            set(contract["requiredEvidenceContracts"]),
        )
        expectation_fields = set(
            contract["evidenceExpectationFields"].values()
        )
        self.assertTrue(
            all(
                items
                and all(set(item) == expectation_fields for item in items)
                for items in contract["requiredEvidenceContracts"].values()
            )
        )
        experience_digests = contract["requiredExperienceSha256ById"]
        self.assertEqual(
            set(contract["experienceIds"]), set(experience_digests)
        )
        experience_fields = contract["experienceFields"]
        for experience in matrix[matrix_fields["experiences"]]:
            experience_id = experience[experience_fields["experienceId"]]
            payload = json.dumps(
                experience,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                experience_digests[experience_id],
            )
        self.assertNotIn("historicalExperienceEvidence", rules)

    def test_template_test_contract_has_one_typed_machine_source(self) -> None:
        contract = load(ROOT / "contracts" / "machine-rules.json")[
            "templateTestContract"
        ]
        for mapping_name in (
            "requestFields",
            "caseFields",
            "modes",
            "states",
            "resultFields",
            "manifestFields",
            "reportFields",
            "caseReportFields",
            "generationRequestFields",
            "walBindingFields",
            "reviewFields",
            "artifactNames",
            "artifactTypes",
            "errorCodes",
        ):
            mapping = contract[mapping_name]
            self.assertIsInstance(mapping, dict)
            self.assertEqual(len(mapping), len(set(mapping.values())))
            self.assertTrue(
                all(isinstance(value, str) and value for value in mapping.values())
            )
        self.assertEqual(1, contract["defaultImageCount"])
        self.assertEqual(0, contract["defaultPrimaryOutputIndex"])
        self.assertGreater(contract["maximumCases"], 0)
        self.assertGreater(contract["maximumPromptLength"], 0)
        execution = load(ROOT / "contracts" / "machine-rules.json")[
            "generationExecutionContract"
        ]
        routes = contract["generationFailureRoutes"]
        self.assertEqual(set(execution["failureClasses"]), set(routes))
        for role, route in routes.items():
            self.assertEqual({"stateRole", "errorCodeRole"}, set(route))
            self.assertIn(route["stateRole"], contract["states"])
            self.assertIn(route["errorCodeRole"], contract["errorCodes"])
            self.assertIn(role, execution["failureRoutes"])

        protected = {
            *contract["modes"].values(),
            *contract["states"].values(),
            *contract["artifactNames"].values(),
            *contract["artifactTypes"].values(),
            *contract["errorCodes"].values(),
        }
        protected -= {
            *contract["modes"],
            *contract["states"],
            *contract["artifactNames"],
            *contract["artifactTypes"],
            *contract["errorCodes"],
        }
        for path in (
            ROOT / "scripts" / "produce.py",
            ROOT / "scripts" / "produce_meme_template" / "template_test.py",
            ROOT / "tests" / "test_issue_14_template_json_test.py",
        ):
            literals = {
                node.value
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            }
            self.assertTrue(protected.isdisjoint(literals), path.as_posix())

    def test_release_management_contract_has_one_typed_machine_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["releaseManagementContract"]
        schema = load(
            ROOT / "contracts" / "release-management-contract.schema.json"
        )
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(contract)))
        cross_mapped = json.loads(json.dumps(contract))
        foreign_role = next(iter(contract["errorCodes"]))
        cross_mapped["lockFields"][foreign_role] = "unexpectedLockField"
        self.assertTrue(
            list(Draft202012Validator(schema).iter_errors(cross_mapped))
        )
        for mapping in (value for value in contract.values() if isinstance(value, dict)):
            self.assertEqual(len(mapping), len(set(mapping.values())))
        phase_index = contract["migrationInvalidateFromPhaseIndex"]
        self.assertLess(phase_index, len(rules["productionPhases"]))
        legacy_roles = contract["legacyProductionPinRequiredFieldRoles"]
        self.assertTrue(
            set(legacy_roles) <= set(contract["productionPinFields"])
        )

        protected_values = {
            contract["lockFileName"],
            contract["lockArtifactType"],
            contract["lockSchemaVersion"],
            contract["productionPinArtifactType"],
            contract["replacementSpecVersion"],
            contract["buildValidationRunnerRelativePath"],
            contract["validatorRelativePath"],
            contract["replacementSpecRelativePath"],
            contract["gallerySnapshotRelativePath"],
            contract["gallerySnapshotMetadataRelativePath"],
            contract["migrationArtifactType"],
            contract["migrationFilePattern"],
            *contract["errorCodes"].values(),
            *contract["installLayout"].values(),
        }
        protected_values -= {
            *contract["errorCodes"],
            *contract["installLayout"],
        }
        for path in (
            ROOT / "scripts" / "release_tool.py",
            ROOT / "scripts" / "release_validation_runner.py",
            ROOT / "scripts" / "produce_meme_template" / "release_management.py",
            ROOT / "tests" / "test_issue_13_release_doctor_install.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            literals = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            }
            self.assertTrue(protected_values.isdisjoint(literals), path.as_posix())
            direct_lock_fields: set[str] = set()
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in {"lock", "entry"}
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                ):
                    direct_lock_fields.add(node.slice.value)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "lock"
                    and node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    direct_lock_fields.add(node.args[0].value)
            self.assertTrue(
                (
                    set(contract["lockFields"].values())
                    | set(contract["fileFields"].values())
                ).isdisjoint(direct_lock_fields),
                path.as_posix(),
            )

    def test_batch_production_contract_has_one_typed_machine_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["batchProductionContract"]
        strategy_contract = rules["replacementStrategyContract"]
        strategy_fields = strategy_contract["fieldRoles"]

        for mapping_name in (
            "requestFields",
            "resultFields",
            "sharedPolicyFields",
            "replacementPoolEntryFields",
            "resolutionFields",
        ):
            mapping = contract[mapping_name]
            self.assertIsInstance(mapping, dict)
            self.assertEqual(len(mapping), len(set(mapping.values())))
            self.assertTrue(
                all(
                    isinstance(value, str) and value
                    for value in mapping.values()
                )
            )

        pool_fields = contract["replacementPoolEntryFields"]
        policy_fields = contract["sharedPolicyFields"]
        self.assertEqual(
            strategy_fields["replacementValue"],
            pool_fields["replacementValue"],
        )
        self.assertEqual(
            strategy_fields["replacementCategory"],
            pool_fields["replacementCategory"],
        )
        for role in ("preserve", "forbidValues"):
            self.assertEqual(strategy_fields[role], policy_fields[role])
        self.assertEqual(
            set(contract["prioritySourceRoles"]),
            set(rules["strategySources"]),
        )
        self.assertEqual(
            len(contract["prioritySourceRoles"]),
            len(set(contract["prioritySourceRoles"])),
        )
        self.assertTrue(
            set(contract["mutableDependencyArtifactTypeRoles"])
            <= set(rules["generationExecutionContract"]["artifactTypes"])
        )
        self.assertTrue(contract["resolutionArtifactType"])
        self.assertIsInstance(contract["allocationAnalysisPoolField"], str)
        self.assertTrue(contract["allocationAnalysisPoolField"])
        self.assertIsInstance(contract["maximumReplacementPoolItems"], int)
        self.assertNotIsInstance(
            contract["maximumReplacementPoolItems"], bool
        )
        self.assertGreaterEqual(contract["maximumReplacementPoolItems"], 1)
        self.assertLessEqual(
            contract["maximumReplacementPoolItems"],
            contract["maximumItems"],
        )
        self.assertTrue(contract["resolutionArtifactName"].endswith(".json"))
        for bound_name in ("minimumItems", "maximumItems"):
            value = contract[bound_name]
            self.assertIsInstance(value, int)
            self.assertNotIsInstance(value, bool)
            self.assertGreater(value, 0)
        self.assertLessEqual(contract["minimumItems"], contract["maximumItems"])

        protected_values = {
            contract["requestFields"]["sharedPolicy"],
            contract["resultFields"]["sharedPolicyApplied"],
            contract["sharedPolicyFields"]["replacementPool"],
            contract["resolutionArtifactType"],
            contract["resolutionArtifactName"],
            contract["allocationAnalysisPoolField"],
            contract["artifactScopeDigestField"],
            contract["dependencyDigestField"],
            contract["resolutionFields"]["policyRevision"],
            contract["resolutionFields"]["policySha256"],
            contract["resolutionFields"]["effectiveStrategy"],
            contract["resolutionFields"]["fieldSources"],
            contract["resolutionFields"]["listValueSources"],
            contract["resolutionFields"]["sourceCategory"],
            contract["resolutionFields"]["allocationCandidateEvaluations"],
            contract["resolutionFields"][
                "allocationPreserveConflictEvaluations"
            ],
            contract["resolutionFields"]["allocationSeed"],
        }
        protected_values -= {
            role
            for mapping_name in (
                "requestFields",
                "resultFields",
                "sharedPolicyFields",
                "replacementPoolEntryFields",
                "resolutionFields",
            )
            for role in contract[mapping_name]
        }
        for path in (
            ROOT / "scripts" / "produce.py",
            ROOT / "scripts" / "produce_meme_template" / "batch_policy.py",
            ROOT / "tests" / "test_issue_12_batch_isolation.py",
        ):
            literals = {
                node.value
                for node in ast.walk(
                    ast.parse(path.read_text(encoding="utf-8"))
                )
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            }
            self.assertTrue(protected_values.isdisjoint(literals), path.as_posix())

    def test_generation_execution_contract_has_one_typed_machine_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["generationExecutionContract"]
        self.assertEqual(
            set(contract["failureClasses"]),
            set(contract["failureRoutes"]),
        )
        self.assertEqual(
            set(contract["failureClasses"]),
            set(contract["retryBudgets"]),
        )
        self.assertEqual(
            len(contract["walFields"]),
            len(set(contract["walFields"].values())),
        )
        self.assertEqual(
            set(contract["outputFormats"]),
            set(contract["outputFormatExtensions"]),
        )
        self.assertEqual(
            set(contract["outputFormats"]),
            set(contract["outputFormatSignatures"]),
        )
        self.assertEqual(
            set(contract["outputFormats"]),
            set(contract["outputFormatDecoderNames"]),
        )
        self.assertIsInstance(contract["defaultImageCount"], int)
        self.assertNotIsInstance(contract["defaultImageCount"], bool)
        self.assertIsInstance(contract["defaultPrimaryOutputIndex"], int)
        self.assertNotIsInstance(contract["defaultPrimaryOutputIndex"], bool)
        self.assertEqual(0, contract["defaultPrimaryOutputIndex"])
        self.assertGreaterEqual(
            contract["maximumImageCount"], contract["defaultImageCount"]
        )
        self.assertGreater(contract["maximumDecodedImageDimension"], 0)
        self.assertGreater(contract["maximumDecodedImagePixels"], 0)
        self.assertIsInstance(contract["fal"]["maximumDownloadRedirects"], int)
        self.assertNotIsInstance(
            contract["fal"]["maximumDownloadRedirects"], bool
        )
        self.assertGreaterEqual(contract["fal"]["maximumDownloadRedirects"], 0)
        for signature in contract["outputFormatSignatures"].values():
            self.assertRegex(signature, r"^(?:[0-9a-f]{2})+$")
        for pattern_name in (
            "providerIdentityPattern",
            "modelIdentityPattern",
            "opaqueExecutionIdentityPattern",
        ):
            re.compile(contract[pattern_name])
        sanitization = contract["persistedErrorSanitization"]
        self.assertTrue(sanitization["digestPrefix"])
        self.assertIsInstance(sanitization["digestLength"], int)
        self.assertNotIsInstance(sanitization["digestLength"], bool)
        self.assertGreater(sanitization["digestLength"], 0)
        for role, route in contract["failureRoutes"].items():
            self.assertIn(route["outcomeRole"], rules["resultStates"])
            self.assertIn(route["errorCodeRole"], rules["errorCodes"])
            phase_index = route["recoveryPhaseIndex"]
            self.assertTrue(
                phase_index is None
                or (
                    isinstance(phase_index, int)
                    and not isinstance(phase_index, bool)
                    and 0 <= phase_index < len(rules["productionPhases"])
                )
            )
            budget = contract["retryBudgets"][role]
            self.assertIsInstance(budget, int)
            self.assertNotIsInstance(budget, bool)
            self.assertGreaterEqual(budget, 0)

        machine_values = {
            *contract["walStatuses"].values(),
            *contract["submissionStatuses"].values(),
            *contract["pollStatuses"].values(),
            *contract["failureClasses"].values(),
            *contract["providerRoles"].values(),
            *contract["artifactTypes"].values(),
            contract["fal"]["model"],
        }
        machine_values -= {
            *contract["walStatuses"],
            *contract["submissionStatuses"],
            *contract["pollStatuses"],
            *contract["failureClasses"],
            *contract["providerRoles"],
            *contract["artifactTypes"],
            *rules["resultStates"],
        }
        sources = [
            ROOT / "scripts" / "produce_meme_template" / "generation_runtime.py",
            ROOT / "scripts" / "produce_meme_template" / "adapters.py",
            ROOT / "tests" / "test_issue_10_generation_wal.py",
        ]
        for path in sources:
            literals = {
                node.value
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertTrue(machine_values.isdisjoint(literals), path.as_posix())

    def test_object_storage_contract_has_one_typed_machine_source(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        contract = rules["objectStorageContract"]
        self.assertEqual(
            len(contract["adapterResultFields"]),
            len(set(contract["adapterResultFields"].values())),
        )
        self.assertEqual(
            len(contract["receiptFields"]),
            len(set(contract["receiptFields"].values())),
        )
        self.assertEqual(
            {"uploaded", "reused"}, set(contract["uploadStatuses"])
        )
        for pattern_name in (
            "providerIdentityPattern",
            "remoteIdentityPattern",
            "requestIdentityPattern",
            "idempotencyIdentityPattern",
        ):
            re.compile(contract[pattern_name])
        self.assertTrue(contract["objectKeyPrefix"])
        self.assertTrue(contract["idempotencyKeyPrefix"])
        self.assertIs(contract["assetUrlPolicy"]["allowQuery"], False)
        self.assertIs(contract["assetUrlPolicy"]["allowFragment"], False)
        self.assertTrue(contract["aliyun"]["sha256MetadataHeader"])
        self.assertTrue(contract["aliyun"]["forbidOverwriteHeader"])
        self.assertTrue(contract["aliyun"]["forbidOverwriteValue"])
        self.assertIn(
            contract["aliyun"]["objectIdentityAlgorithm"],
            hashlib.algorithms_available,
        )

        machine_values = {
            contract["artifactType"],
            *contract["uploadStatuses"].values(),
            *contract["providerRoles"].values(),
            contract["aliyun"]["objectIdentityAlgorithm"],
        }
        machine_values -= {
            *contract["uploadStatuses"],
            *contract["providerRoles"],
        }
        for path in (
            ROOT / "scripts" / "produce_meme_template" / "delivery_runtime.py",
            ROOT / "scripts" / "produce_meme_template" / "adapters.py",
            ROOT / "tests" / "test_issue_11_oss_finalization.py",
        ):
            literals = {
                node.value
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertTrue(machine_values.isdisjoint(literals), path.as_posix())

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
        runtime_sources = [
            (
                ROOT / "scripts" / "produce_meme_template" / name
            ).read_text(encoding="utf-8")
            for name in (
                "workflow_core.py",
                "replacement_planning.py",
                "batch_policy.py",
                "generation_runtime.py",
                "template_compiler.py",
                "delivery_runtime.py",
                "production_runtime.py",
                "workflow.py",
            )
        ]
        test_sources = [path.read_text(encoding="utf-8") for path in sorted((ROOT / "tests").glob("test_issue_*.py"))]
        sources = [*runtime_sources, *test_sources]
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

        runtime_literal_sets = string_literals[: len(runtime_sources)]
        test_literal_sets = string_literals[len(runtime_sources) :]
        for phase in rules["productionPhases"]:
            for runtime_literals in runtime_literal_sets:
                self.assertNotIn(phase["phase"], runtime_literals)
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
            (ROOT / "scripts" / "produce_meme_template" / "replacement_planning.py").read_text(encoding="utf-8"),
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
            (ROOT / "scripts" / "produce_meme_template" / "production_runtime.py").read_text(encoding="utf-8")
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
            ROOT / "scripts" / "produce_meme_template" / "template_compiler.py",
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
            ROOT / "scripts" / "produce_meme_template" / "replacement_planning.py",
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

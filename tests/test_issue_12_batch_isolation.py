from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
SECOND_SOURCE = (
    ROOT / "fixtures" / "e2e" / "multi-instance" / "repeated-pet" / "source.ppm"
)
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
FIXED_TIME = datetime.fromisoformat("2026-08-17T09:00:00+00:00")
BATCH_CONTRACT = RULES["batchProductionContract"]
BATCH_REQUEST_FIELDS = BATCH_CONTRACT["requestFields"]
BATCH_RESULT_FIELDS = BATCH_CONTRACT["resultFields"]
POLICY_FIELDS = BATCH_CONTRACT["sharedPolicyFields"]
POOL_FIELDS = BATCH_CONTRACT["replacementPoolEntryFields"]
RESOLUTION_FIELDS = BATCH_CONTRACT["resolutionFields"]
STRATEGY_FIELDS = RULES["replacementStrategyContract"]["fieldRoles"]
ALLOCATION_POOL_FIELD = BATCH_CONTRACT["allocationAnalysisPoolField"]


def batch_request(
    batch_id: str,
    items: list[dict],
    shared_policy: dict | None = None,
) -> dict:
    request = {
        BATCH_REQUEST_FIELDS["batchIdentity"]: batch_id,
        BATCH_REQUEST_FIELDS["items"]: items,
    }
    if shared_policy is not None:
        request[BATCH_REQUEST_FIELDS["sharedPolicy"]] = shared_policy
    return request


def shared_policy(
    policy_id: str,
    revision: str,
    scope: list[str],
    replacement_pool: list[dict],
    *,
    preserve: list[str] | None = None,
) -> dict:
    policy = {
        POLICY_FIELDS["policyIdentity"]: policy_id,
        POLICY_FIELDS["policyVersion"]: "1",
        POLICY_FIELDS["policyRevision"]: revision,
        POLICY_FIELDS["scope"]: scope,
        POLICY_FIELDS["replacementPool"]: replacement_pool,
    }
    if preserve is not None:
        policy[POLICY_FIELDS["preserve"]] = preserve
    return policy


def animal_pool(*values: str) -> list[dict]:
    return [
        {
            POOL_FIELDS["replacementValue"]: value,
            POOL_FIELDS["replacementCategory"]: RULES["sourceCategories"][
                "genericAnimal"
            ],
        }
        for value in values
    ]


def result_items(result) -> list[dict]:
    return result.as_dict()[BATCH_RESULT_FIELDS["items"]]


def synchronize_artifact_record(
    manifest: dict,
    name: str,
    payload: bytes,
) -> None:
    artifact_sha = hashlib.sha256(payload).hexdigest()
    artifact = manifest["artifacts"][name]
    artifact["sha256"] = artifact_sha
    artifact["bytes"] = len(payload)
    scope_payload = {
        "productionItemId": manifest["productionItemId"],
        "artifact": name,
        "sha256": artifact_sha,
    }
    artifact[BATCH_CONTRACT["artifactScopeDigestField"]] = hashlib.sha256(
        json.dumps(
            scope_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    dependency_field = BATCH_CONTRACT["dependencyDigestField"]
    for child in manifest["artifacts"].values():
        if (
            isinstance(child, dict)
            and name in child.get("dependsOn", [])
            and name in child.get(dependency_field, {})
        ):
            child[dependency_field][name] = artifact_sha


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Issue12BatchIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        base = load_json(FIXTURE / "request.json")
        base["sourceImage"] = str(FIXTURE / base["sourceImage"])
        self.first = {
            **copy.deepcopy(base),
            "productionItemId": "batch-item-a",
            "templateKey": "batch-template-a",
        }
        self.second = {
            **copy.deepcopy(base),
            "productionItemId": "batch-item-b",
            "templateKey": "batch-template-b",
            "sourceImage": str(SECOND_SOURCE),
        }
        self.adapters = DeterministicFixtureAdapters(FIXTURE)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_batch_creates_only_independent_production_items(self) -> None:
        result = run_production(
            batch_request(
                "independent-two-images", [self.first, self.second]
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        payload = result.as_dict()
        self.assertEqual(
            "independent-two-images",
            payload[BATCH_RESULT_FIELDS["batchIdentity"]],
        )
        self.assertIs(payload[BATCH_RESULT_FIELDS["sharedPolicyApplied"]], False)
        self.assertEqual(2, len(payload[BATCH_RESULT_FIELDS["items"]]))
        self.assertEqual(
            {RULES["resultStates"]["completed"]},
            {
                item["state"]
                for item in payload[BATCH_RESULT_FIELDS["items"]]
            },
        )
        self.assertEqual(
            {"batch-item-a", "batch-item-b"},
            {
                item["productionItemId"]
                for item in payload[BATCH_RESULT_FIELDS["items"]]
            },
        )

        first_manifest = load_json(self.output_root / "batch-item-a" / "production-manifest.json")
        second_manifest = load_json(self.output_root / "batch-item-b" / "production-manifest.json")
        self.assertEqual(
            hashlib.sha256(Path(self.first["sourceImage"]).read_bytes()).hexdigest(),
            first_manifest["sourceImageSha256"],
        )
        self.assertEqual(
            hashlib.sha256(Path(self.second["sourceImage"]).read_bytes()).hexdigest(),
            second_manifest["sourceImageSha256"],
        )
        self.assertNotEqual(
            first_manifest["sourceImageSha256"], second_manifest["sourceImageSha256"]
        )
        for item_id in ("batch-item-a", "batch-item-b"):
            item_dir = self.output_root / item_id
            self.assertTrue((item_dir / "production-pin.json").is_file())
            self.assertTrue((item_dir / "gallery-template.json").is_file())
            plan = load_json(item_dir / "replacement-plan.json")
            self.assertEqual("柯基犬", plan["primaryTargets"][0]["replacementValue"])
            self.assertEqual(
                RULES["strategySources"]["autonomousDecision"],
                plan["primaryTargets"][0]["decisionSource"],
            )
        self.assertEqual(
            {"batch-item-a", "batch-item-b"},
            {path.name for path in self.output_root.iterdir()},
        )

    def test_one_item_failure_does_not_stop_or_write_into_another_item(self) -> None:
        class FirstItemFails(DeterministicFixtureAdapters):
            def analyze_source(self, source_image: Path, replacement_strategy):
                if hashlib.sha256(source_image.read_bytes()).hexdigest() == hashlib.sha256(
                    Path(self.first_source).read_bytes()
                ).hexdigest():
                    return []
                return super().analyze_source(source_image, replacement_strategy)

        adapters = FirstItemFails(FIXTURE)
        adapters.first_source = self.first["sourceImage"]
        result = run_production(
            batch_request("failure-isolation", [self.first, self.second]),
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(result)
        }
        self.assertEqual(
            RULES["resultStates"]["failed"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["errorCodes"]["externalFailure"],
            items["batch-item-a"]["errorCode"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )
        self.assertFalse(
            (self.output_root / "batch-item-a" / "gallery-template.json").exists()
        )
        self.assertTrue(
            (self.output_root / "batch-item-b" / "gallery-template.json").is_file()
        )
        self.assertEqual(1, len(adapters.upload_calls))

    def test_explicit_shared_policy_is_resolved_with_per_image_priority(self) -> None:
        second = copy.deepcopy(self.second)
        second["replacementStrategy"] = {
            STRATEGY_FIELDS["policyIdentity"]: "single-item-exception",
            STRATEGY_FIELDS["policyVersion"]: "1",
            STRATEGY_FIELDS["replacementValue"]: "水豚",
            STRATEGY_FIELDS["replacementCategory"]: RULES["sourceCategories"][
                "genericAnimal"
            ],
        }
        result = run_production(
            batch_request(
                "explicit-shared-policy",
                [self.first, second],
                shared_policy(
                    "animal-variety",
                    "2026-08-17-r1",
                    ["batch-item-a", "batch-item-b"],
                    animal_pool("柯基犬", "水豚"),
                    preserve=["午后窗光"],
                ),
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertIs(
            result.as_dict()[BATCH_RESULT_FIELDS["sharedPolicyApplied"]], True
        )
        expected = {
            "batch-item-a": ("柯基犬", RULES["strategySources"]["batchDecision"]),
            "batch-item-b": ("水豚", RULES["strategySources"]["perImageDecision"]),
        }
        for item_id, (replacement_value, decision_source) in expected.items():
            item_dir = self.output_root / item_id
            plan = load_json(item_dir / "replacement-plan.json")
            self.assertEqual(
                replacement_value, plan["primaryTargets"][0]["replacementValue"]
            )
            self.assertEqual(
                decision_source, plan["primaryTargets"][0]["decisionSource"]
            )
            preserved = {
                entry["value"]: entry["decisionSource"]
                for entry in plan["frozenSetDecisions"]
            }
            self.assertEqual(
                RULES["strategySources"]["batchDecision"], preserved["午后窗光"]
            )
            resolution = load_json(
                item_dir / BATCH_CONTRACT["resolutionArtifactName"]
            )
            self.assertEqual(
                "explicit-shared-policy",
                resolution[RESOLUTION_FIELDS["batchIdentity"]],
            )
            self.assertEqual(
                "2026-08-17-r1",
                resolution[RESOLUTION_FIELDS["policyRevision"]],
            )
            self.assertEqual(
                ["batch-item-a", "batch-item-b"],
                resolution[RESOLUTION_FIELDS["scope"]],
            )
            self.assertEqual(
                [
                    RULES["strategySources"]["perImageDecision"],
                    RULES["strategySources"]["batchDecision"],
                    RULES["strategySources"]["autonomousDecision"],
                ],
                resolution[RESOLUTION_FIELDS["priority"]],
            )
            self.assertEqual(
                decision_source,
                resolution[RESOLUTION_FIELDS["fieldSources"]][
                    STRATEGY_FIELDS["replacementValue"]
                ],
            )
        self.assertEqual(
            {"batch-item-a", "batch-item-b"},
            {path.name for path in self.output_root.iterdir()},
        )

    def test_per_image_value_overrides_a_lower_priority_batch_forbid(self) -> None:
        explicit = copy.deepcopy(self.first)
        explicit["replacementStrategy"] = {
            STRATEGY_FIELDS["replacementValue"]: "水豚",
            STRATEGY_FIELDS["replacementCategory"]: RULES["sourceCategories"][
                "genericAnimal"
            ],
        }
        policy = shared_policy(
            "lower-priority-forbid",
            "priority-r1",
            ["batch-item-a"],
            animal_pool("柯基犬", "水豚"),
        )
        policy[POLICY_FIELDS["forbidValues"]] = ["水豚"]

        result = run_production(
            batch_request(
                "per-image-priority",
                [explicit],
                policy,
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        item = result_items(result)[0]
        self.assertEqual(RULES["resultStates"]["completed"], item["state"])
        plan = load_json(
            self.output_root / "batch-item-a" / "replacement-plan.json"
        )
        self.assertEqual("水豚", plan["primaryTargets"][0]["replacementValue"])
        self.assertNotIn("水豚", plan["strategy"].get("forbidValues", []))

    def test_shared_allocation_uses_compatible_candidates_before_diversity(self) -> None:
        class WaterCapybaraIsIncompatible(DeterministicFixtureAdapters):
            def analyze_source(self, source_image: Path, replacement_strategy):
                analysis = super().analyze_source(
                    source_image,
                    replacement_strategy,
                )
                for candidate in analysis["replacementPool"]:
                    if candidate["value"] == "水豚":
                        candidate["semanticCompatible"] = False
                explicit = analysis.get("explicitReplacementEvaluation")
                if isinstance(explicit, dict) and explicit.get("value") == "水豚":
                    explicit["semanticCompatible"] = False
                return analysis

        result = run_production(
            batch_request(
                "compatible-before-diversity",
                [self.first],
                shared_policy(
                    "compatibility-first",
                    "compatibility-r1",
                    ["batch-item-a"],
                    animal_pool("水豚", "柯基犬"),
                ),
            ),
            self.output_root,
            WaterCapybaraIsIncompatible(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(
            RULES["resultStates"]["completed"], result_items(result)[0]["state"]
        )
        plan = load_json(
            self.output_root / "batch-item-a" / "replacement-plan.json"
        )
        self.assertEqual("柯基犬", plan["primaryTargets"][0]["replacementValue"])

    def test_swapping_source_facts_between_items_fails_even_with_synced_digests(
        self,
    ) -> None:
        request = batch_request(
            "swapped-source-facts", [self.first, self.second]
        )
        first = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertTrue(
            all(
                item["state"] == RULES["resultStates"]["completed"]
                for item in result_items(first)
            )
        )
        item_dirs = [
            self.output_root / "batch-item-a",
            self.output_root / "batch-item-b",
        ]
        fact_paths = [item_dir / "source-analysis.json" for item_dir in item_dirs]
        payloads = [path.read_bytes() for path in fact_paths]
        for item_dir, fact_path, swapped in zip(
            item_dirs, fact_paths, reversed(payloads), strict=True
        ):
            fact_path.write_bytes(swapped)
            manifest_path = item_dir / "production-manifest.json"
            manifest = load_json(manifest_path)
            synchronize_artifact_record(
                manifest,
                "source-analysis.json",
                swapped,
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        resumed = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(
            {RULES["resultStates"]["blocked"]},
            {item["state"] for item in result_items(resumed)},
        )
        self.assertEqual(
            {RULES["errorCodes"]["productionItemIntegrityFailure"]},
            {item["errorCode"] for item in result_items(resumed)},
        )

    def test_shared_policy_rerun_reuses_each_item_and_its_stable_assignment(
        self,
    ) -> None:
        request = batch_request(
            "stable-shared-rerun",
            [self.first, self.second],
            shared_policy(
                "animal-variety",
                "stable-r1",
                ["batch-item-a", "batch-item-b"],
                animal_pool("柯基犬", "水豚"),
            ),
        )
        first = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )
        assignments = {
            item_id: load_json(
                self.output_root / item_id / "replacement-plan.json"
            )["primaryTargets"][0]["replacementValue"]
            for item_id in ("batch-item-a", "batch-item-b")
        }
        call_counts = (
            len(self.adapters.submission_calls),
            len(self.adapters.poll_calls),
            len(self.adapters.upload_calls),
        )

        resumed = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(
            {"柯基犬", "水豚"}, set(assignments.values())
        )
        self.assertTrue(
            all(item["resumed"] for item in result_items(resumed))
        )
        self.assertEqual(
            call_counts,
            (
                len(self.adapters.submission_calls),
                len(self.adapters.poll_calls),
                len(self.adapters.upload_calls),
            ),
        )
        self.assertEqual(
            [item["state"] for item in result_items(first)],
            [item["state"] for item in result_items(resumed)],
        )

    def test_rerun_reuses_bound_allocation_evidence_when_final_analysis_is_narrow(
        self,
    ) -> None:
        class NarrowFinalAnalysis(DeterministicFixtureAdapters):
            def __init__(self, fixture_dir: Path):
                super().__init__(fixture_dir)
                self.analysis_calls = 0

            def analyze_source(self, source_image: Path, replacement_strategy):
                self.analysis_calls += 1
                analysis = super().analyze_source(
                    source_image,
                    replacement_strategy,
                )
                if ALLOCATION_POOL_FIELD not in (replacement_strategy or {}):
                    analysis["replacementPool"] = []
                return analysis

        adapters = NarrowFinalAnalysis(FIXTURE)
        request = batch_request(
            "narrow-final-analysis",
            [self.first],
            shared_policy(
                "two-pass-analysis",
                "two-pass-r1",
                ["batch-item-a"],
                animal_pool("柯基犬", "水豚"),
            ),
        )

        first = run_production(
            request,
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )
        call_count = adapters.analysis_calls
        resumed = run_production(
            request,
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(
            RULES["resultStates"]["completed"], result_items(first)[0]["state"]
        )
        self.assertEqual(
            RULES["resultStates"]["completed"],
            result_items(resumed)[0]["state"],
        )
        self.assertTrue(result_items(resumed)[0]["resumed"])
        self.assertEqual(call_count, adapters.analysis_calls)

    def test_lower_priority_batch_preserve_yields_to_explicit_replacement(
        self,
    ) -> None:
        explicit = copy.deepcopy(self.first)
        explicit["replacementStrategy"] = {
            STRATEGY_FIELDS["replacementValue"]: "水豚",
            STRATEGY_FIELDS["replacementCategory"]: RULES["sourceCategories"][
                "genericAnimal"
            ],
        }
        policy = shared_policy(
            "semantic-preserve-priority",
            "semantic-preserve-r1",
            ["batch-item-a"],
            animal_pool("柯基犬", "水豚"),
            preserve=["橘猫"],
        )

        result = run_production(
            batch_request("semantic-preserve-priority", [explicit], policy),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(
            RULES["resultStates"]["completed"], result_items(result)[0]["state"]
        )
        plan = load_json(
            self.output_root / "batch-item-a" / "replacement-plan.json"
        )
        self.assertNotIn("橘猫", plan["strategy"].get("preserve", []))

    def test_failed_item_does_not_consume_a_shared_candidate(self) -> None:
        first_sha = hashlib.sha256(
            Path(self.first["sourceImage"]).read_bytes()
        ).hexdigest()

        class FirstExplicitValueIsIncompatible(DeterministicFixtureAdapters):
            def analyze_source(self, source_image: Path, replacement_strategy):
                analysis = super().analyze_source(
                    source_image,
                    replacement_strategy,
                )
                observed_sha = hashlib.sha256(source_image.read_bytes()).hexdigest()
                if observed_sha == first_sha:
                    for candidate in analysis["replacementPool"]:
                        if candidate["value"] == "水豚":
                            candidate["semanticCompatible"] = False
                    explicit = analysis.get("explicitReplacementEvaluation")
                    if isinstance(explicit, dict) and explicit.get("value") == "水豚":
                        explicit["semanticCompatible"] = False
                return analysis

        explicit = copy.deepcopy(self.first)
        explicit["replacementStrategy"] = {
            STRATEGY_FIELDS["replacementValue"]: "水豚",
            STRATEGY_FIELDS["replacementCategory"]: RULES["sourceCategories"][
                "genericAnimal"
            ],
        }
        baseline_root = self.output_root / "baseline"
        combined_root = self.output_root / "combined"
        baseline = run_production(
            batch_request(
                "failed-occupancy",
                [self.second],
                shared_policy(
                    "failed-occupancy-policy",
                    "failed-occupancy-r1",
                    ["batch-item-b"],
                    animal_pool("水豚", "柯基犬"),
                ),
            ),
            baseline_root,
            FirstExplicitValueIsIncompatible(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        combined = run_production(
            batch_request(
                "failed-occupancy",
                [explicit, self.second],
                shared_policy(
                    "failed-occupancy-policy",
                    "failed-occupancy-r1",
                    ["batch-item-a", "batch-item-b"],
                    animal_pool("水豚", "柯基犬"),
                ),
            ),
            combined_root,
            FirstExplicitValueIsIncompatible(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        baseline_value = load_json(
            baseline_root / "batch-item-b" / "replacement-plan.json"
        )["primaryTargets"][0]["replacementValue"]
        combined_value = load_json(
            combined_root / "batch-item-b" / "replacement-plan.json"
        )["primaryTargets"][0]["replacementValue"]
        combined_items = {
            item["productionItemId"]: item for item in result_items(combined)
        }
        self.assertEqual(
            RULES["resultStates"]["completed"],
            result_items(baseline)[0]["state"],
        )
        self.assertEqual(
            RULES["resultStates"]["blocked"],
            combined_items["batch-item-a"]["state"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"],
            combined_items["batch-item-b"]["state"],
        )
        self.assertEqual(baseline_value, combined_value)

    def test_shared_policy_analysis_failure_is_scoped_to_that_item(self) -> None:
        class FirstItemFails(DeterministicFixtureAdapters):
            def analyze_source(self, source_image: Path, replacement_strategy):
                if hashlib.sha256(source_image.read_bytes()).hexdigest() == self.failed_sha:
                    return []
                return super().analyze_source(source_image, replacement_strategy)

        adapters = FirstItemFails(FIXTURE)
        adapters.failed_sha = hashlib.sha256(
            Path(self.first["sourceImage"]).read_bytes()
        ).hexdigest()
        result = run_production(
            batch_request(
                "shared-failure-isolation",
                [self.first, self.second],
                shared_policy(
                    "animal-variety",
                    "failure-r1",
                    ["batch-item-a", "batch-item-b"],
                    animal_pool("柯基犬", "水豚"),
                ),
            ),
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(result)
        }
        self.assertEqual(
            RULES["resultStates"]["failed"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )
        self.assertEqual(1, len(adapters.upload_calls))

    def test_shared_policy_fact_drift_is_scoped_to_that_item(self) -> None:
        class FirstItemDriftsOnFinalAnalysis(DeterministicFixtureAdapters):
            def __init__(self, fixture_root: Path, failed_sha: str):
                super().__init__(fixture_root)
                self.failed_sha = failed_sha
                self.analysis_counts: dict[str, int] = {}

            def analyze_source(self, source_image: Path, replacement_strategy):
                source_sha = hashlib.sha256(source_image.read_bytes()).hexdigest()
                self.analysis_counts[source_sha] = (
                    self.analysis_counts.get(source_sha, 0) + 1
                )
                analysis = super().analyze_source(
                    source_image,
                    replacement_strategy,
                )
                if (
                    source_sha == self.failed_sha
                    and self.analysis_counts[source_sha] == 2
                ):
                    analysis["target"]["identity"] = "另一个来源身份"
                return analysis

        failed_sha = hashlib.sha256(
            Path(self.first["sourceImage"]).read_bytes()
        ).hexdigest()
        adapters = FirstItemDriftsOnFinalAnalysis(FIXTURE, failed_sha)

        result = run_production(
            batch_request(
                "shared-fact-drift",
                [self.first, self.second],
                shared_policy(
                    "animal-variety",
                    "drift-r1",
                    ["batch-item-a", "batch-item-b"],
                    animal_pool("柯基犬", "水豚"),
                ),
            ),
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(result)
        }
        self.assertEqual(
            RULES["resultStates"]["failed"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["errorCodes"]["externalFailure"],
            items["batch-item-a"]["errorCode"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )
        self.assertTrue(
            (
                self.output_root
                / "batch-item-a"
                / "production-manifest.json"
            ).is_file()
        )
        self.assertEqual(1, len(adapters.upload_calls))

    def test_shared_policy_only_applies_inside_its_explicit_scope(self) -> None:
        result = run_production(
            batch_request(
                "partial-policy-scope",
                [self.first, self.second],
                shared_policy(
                    "one-item-policy",
                    "scope-r1",
                    ["batch-item-a"],
                    animal_pool("水豚"),
                ),
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertTrue(
            all(
                item["state"] == RULES["resultStates"]["completed"]
                for item in result_items(result)
            )
        )
        scoped_plan = load_json(
            self.output_root / "batch-item-a" / "replacement-plan.json"
        )
        unscoped_plan = load_json(
            self.output_root / "batch-item-b" / "replacement-plan.json"
        )
        self.assertEqual("水豚", scoped_plan["primaryTargets"][0]["replacementValue"])
        self.assertEqual(
            RULES["strategySources"]["batchDecision"],
            scoped_plan["primaryTargets"][0]["decisionSource"],
        )
        self.assertEqual(
            RULES["strategySources"]["autonomousDecision"],
            unscoped_plan["primaryTargets"][0]["decisionSource"],
        )
        self.assertFalse(
            (
                self.output_root
                / "batch-item-b"
                / BATCH_CONTRACT["resolutionArtifactName"]
            ).exists()
        )

    def test_shared_assignment_is_stable_when_batch_item_order_changes(self) -> None:
        policy = shared_policy(
            "order-independent",
            "order-r1",
            ["batch-item-a", "batch-item-b"],
            animal_pool("柯基犬", "水豚"),
        )
        first_result = run_production(
            batch_request(
                "stable-order", [self.first, self.second], policy
            ),
            self.output_root / "forward",
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        reversed_result = run_production(
            batch_request(
                "stable-order", [self.second, self.first], policy
            ),
            self.output_root / "reversed",
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        def assignments(root: Path, result) -> dict[str, str]:
            self.assertTrue(
                all(
                    item["state"] == RULES["resultStates"]["completed"]
                    for item in result_items(result)
                )
            )
            return {
                item_id: load_json(root / item_id / "replacement-plan.json")[
                    "primaryTargets"
                ][0]["replacementValue"]
                for item_id in ("batch-item-a", "batch-item-b")
            }

        self.assertEqual(
            assignments(self.output_root / "forward", first_result),
            assignments(self.output_root / "reversed", reversed_result),
        )

    def test_colliding_allocation_seed_is_stable_across_hash_seeds(self) -> None:
        first = copy.deepcopy(self.first)
        second = copy.deepcopy(self.second)
        first["templateKey"] = "same-template"
        second["templateKey"] = "same-template"
        policy = shared_policy(
            "colliding-seed",
            "collision-r1",
            ["batch-item-a", "batch-item-b"],
            animal_pool("柯基犬", "水豚"),
        )
        request_path = self.output_root / "collision-request.json"
        request_path.write_text(
            json.dumps(
                batch_request(
                    "same-batch-and-template",
                    [first, second],
                    policy,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        observed: list[dict[str, str]] = []
        for hash_seed in ("1", "2"):
            output_path = self.output_root / f"hash-seed-{hash_seed}"
            environment = os.environ.copy()
            environment["PYTHONHASHSEED"] = hash_seed
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "produce.py"),
                    "--request",
                    str(request_path),
                    "--output",
                    str(output_path),
                    "--deterministic-fixture",
                    str(FIXTURE),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            observed.append(
                {
                    item_id: load_json(
                        output_path / item_id / "replacement-plan.json"
                    )["primaryTargets"][0]["replacementValue"]
                    for item_id in ("batch-item-a", "batch-item-b")
                }
            )

        self.assertEqual(observed[0], observed[1])

    def test_shared_pool_reuses_the_least_used_value_after_exhaustion(self) -> None:
        third = {
            **copy.deepcopy(self.first),
            "productionItemId": "batch-item-c",
            "templateKey": "batch-template-c",
        }
        result = run_production(
            batch_request(
                "balanced-exhaustion",
                [self.first, self.second, third],
                shared_policy(
                    "two-values-for-three-items",
                    "balanced-r1",
                    ["batch-item-a", "batch-item-b", "batch-item-c"],
                    animal_pool("柯基犬", "水豚"),
                ),
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertTrue(
            all(
                item["state"] == RULES["resultStates"]["completed"]
                for item in result_items(result)
            )
        )
        values = [
            load_json(
                self.output_root / item_id / "replacement-plan.json"
            )["primaryTargets"][0]["replacementValue"]
            for item_id in ("batch-item-a", "batch-item-b", "batch-item-c")
        ]
        self.assertEqual({1, 2}, {values.count(value) for value in set(values)})

    def test_changed_per_image_strategy_cannot_reuse_an_old_resolution(self) -> None:
        policy = shared_policy(
            "mutable-per-image-input",
            "input-r1",
            ["batch-item-a", "batch-item-b"],
            animal_pool("柯基犬", "水豚"),
        )
        request = batch_request(
            "changed-per-image-strategy",
            [self.first, self.second],
            policy,
        )
        first_result = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertTrue(
            all(
                item["state"] == RULES["resultStates"]["completed"]
                for item in result_items(first_result)
            )
        )
        changed_first = copy.deepcopy(self.first)
        changed_first["replacementStrategy"] = {
            STRATEGY_FIELDS["replacementValue"]: "水豚",
            STRATEGY_FIELDS["replacementCategory"]: RULES["sourceCategories"][
                "genericAnimal"
            ],
        }

        resumed = run_production(
            batch_request(
                "changed-per-image-strategy",
                [changed_first, self.second],
                policy,
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(resumed)
        }
        self.assertEqual(
            RULES["resultStates"]["blocked"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            items["batch-item-a"]["errorCode"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )

    def test_orphan_resolution_cannot_inject_a_pool_assignment(self) -> None:
        policy = shared_policy(
            "orphan-resolution",
            "orphan-r1",
            ["batch-item-a"],
            animal_pool("柯基犬", "水豚"),
        )
        request = batch_request(
            "orphan-resolution-batch",
            [self.first],
            policy,
        )
        seed_root = self.output_root / "seed"
        seed_result = run_production(
            request,
            seed_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(
            RULES["resultStates"]["completed"],
            result_items(seed_result)[0]["state"],
        )
        resolution_name = BATCH_CONTRACT["resolutionArtifactName"]
        orphan = load_json(seed_root / "batch-item-a" / resolution_name)
        effective = orphan[RESOLUTION_FIELDS["effectiveStrategy"]]
        effective[STRATEGY_FIELDS["replacementValue"]] = (
            "水豚"
            if effective[STRATEGY_FIELDS["replacementValue"]] == "柯基犬"
            else "柯基犬"
        )
        orphan_root = self.output_root / "orphan"
        orphan_dir = orphan_root / "batch-item-a"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / resolution_name).write_text(
            json.dumps(orphan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = run_production(
            request,
            orphan_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        item = result_items(result)[0]
        self.assertEqual(RULES["resultStates"]["blocked"], item["state"])
        self.assertEqual(
            RULES["errorCodes"]["immutableConflict"],
            item["errorCode"],
        )

    def test_invalid_existing_resolution_does_not_reassign_a_valid_sibling(
        self,
    ) -> None:
        second = {
            **copy.deepcopy(self.first),
            "productionItemId": "batch-item-b",
        }
        policy = shared_policy(
            "existing-resolution-isolation",
            "existing-resolution-r1",
            ["batch-item-a", "batch-item-b"],
            animal_pool("水豚", "柯基犬"),
        )
        request = batch_request(
            "existing-resolution-isolation",
            [self.first, second],
            policy,
        )
        first_result = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertTrue(
            all(
                item["state"] == RULES["resultStates"]["completed"]
                for item in result_items(first_result)
            )
        )
        resolution_name = BATCH_CONTRACT["resolutionArtifactName"]
        first_dir = self.output_root / "batch-item-a"
        resolution_path = first_dir / resolution_name
        resolution = load_json(resolution_path)
        effective = resolution[RESOLUTION_FIELDS["effectiveStrategy"]]
        current_value = effective[STRATEGY_FIELDS["replacementValue"]]
        effective[STRATEGY_FIELDS["replacementValue"]] = (
            "柯基犬" if current_value == "水豚" else "水豚"
        )
        resolution_bytes = (
            json.dumps(
                resolution,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        resolution_path.write_bytes(resolution_bytes)
        manifest_path = first_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        synchronize_artifact_record(
            manifest,
            resolution_name,
            resolution_bytes,
        )
        identity_payload = {
            "replacementStrategy": effective,
            "sharedPolicyResolution": resolution,
        }
        manifest["replacementStrategySha256"] = hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        resumed = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(resumed)
        }
        self.assertEqual(
            RULES["resultStates"]["blocked"],
            items["batch-item-a"]["state"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"],
            items["batch-item-b"]["state"],
        )
        self.assertTrue(items["batch-item-b"]["resumed"])

    def test_malformed_tracked_shared_evidence_is_item_scoped(self) -> None:
        policy = shared_policy(
            "malformed-tracked-evidence",
            "malformed-tracked-r1",
            ["batch-item-a", "batch-item-b"],
            animal_pool("水豚", "柯基犬"),
        )
        request = batch_request(
            "malformed-tracked-evidence",
            [self.first, self.second],
            policy,
        )
        for case in ("manifest", "source-analysis"):
            with self.subTest(case=case):
                output_root = self.output_root / case
                adapters = DeterministicFixtureAdapters(FIXTURE)
                first_result = run_production(
                    request,
                    output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )
                self.assertTrue(
                    all(
                        item["state"] == RULES["resultStates"]["completed"]
                        for item in result_items(first_result)
                    )
                )
                first_dir = output_root / "batch-item-a"
                manifest_path = first_dir / "production-manifest.json"
                if case == "manifest":
                    manifest_path.write_text("[]\n", encoding="utf-8")
                else:
                    source_path = first_dir / "source-analysis.json"
                    source_bytes = b"[]\n"
                    source_path.write_bytes(source_bytes)
                    manifest = load_json(manifest_path)
                    synchronize_artifact_record(
                        manifest,
                        "source-analysis.json",
                        source_bytes,
                    )
                    manifest_path.write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )

                resumed = run_production(
                    request,
                    output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                items = {
                    item["productionItemId"]: item
                    for item in result_items(resumed)
                }
                self.assertEqual(
                    RULES["resultStates"]["blocked"],
                    items["batch-item-a"]["state"],
                )
                self.assertEqual(
                    RULES["errorCodes"]["productionItemIntegrityFailure"],
                    items["batch-item-a"]["errorCode"],
                )
                self.assertEqual(
                    RULES["resultStates"]["completed"],
                    items["batch-item-b"]["state"],
                )
                self.assertTrue(items["batch-item-b"]["resumed"])

    def test_invalid_shared_policy_stops_before_starting_any_item(self) -> None:
        invalid_policy = shared_policy(
            "invalid-scope",
            "invalid-r1",
            ["unknown-item"],
            animal_pool("柯基犬"),
        )

        result = run_production(
            batch_request(
                "invalid-shared-policy",
                [self.first, self.second],
                invalid_policy,
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        payload = result.as_dict()
        self.assertEqual([], payload[BATCH_RESULT_FIELDS["items"]])
        self.assertEqual(
            RULES["errorCodes"]["invalidProductionRequest"],
            payload[BATCH_RESULT_FIELDS["errorCode"]],
        )
        self.assertEqual([], list(self.output_root.iterdir()))

    def test_no_compatible_shared_value_is_an_item_scoped_block(self) -> None:
        incompatible_pool = [
            {
                POOL_FIELDS["replacementValue"]: "雨后温室",
                POOL_FIELDS["replacementCategory"]: RULES["sourceCategories"][
                    "sceneAttribute"
                ],
            }
        ]

        result = run_production(
            batch_request(
                "no-compatible-shared-value",
                [self.first, self.second],
                shared_policy(
                    "scene-only",
                    "scene-r1",
                    ["batch-item-a"],
                    incompatible_pool,
                ),
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(result)
        }
        self.assertEqual(
            RULES["resultStates"]["blocked"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["errorCodes"]["noCompatibleReplacement"],
            items["batch-item-a"]["errorCode"],
        )
        self.assertTrue(
            (
                self.output_root
                / "batch-item-a"
                / "production-manifest.json"
            ).is_file()
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )

    def test_cli_accepts_the_same_batch_envelope(self) -> None:
        request_path = self.output_root / "batch-request.json"
        output_path = self.output_root / "cli-items"
        request_path.write_text(
            json.dumps(
                batch_request(
                    "cli-batch",
                    [self.first, self.second],
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "produce.py"),
                "--request",
                str(request_path),
                "--output",
                str(output_path),
                "--deterministic-fixture",
                str(FIXTURE),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            "cli-batch",
            payload[BATCH_RESULT_FIELDS["batchIdentity"]],
        )
        self.assertEqual(2, len(payload[BATCH_RESULT_FIELDS["items"]]))
        self.assertEqual(
            {RULES["resultStates"]["completed"]},
            {
                item["state"]
                for item in payload[BATCH_RESULT_FIELDS["items"]]
            },
        )

    def test_invalid_item_input_does_not_abort_the_remaining_batch(self) -> None:
        invalid = {
            **copy.deepcopy(self.first),
            "sourceImage": str(self.output_root / "missing-source.png"),
        }

        result = run_production(
            batch_request("invalid-item-isolation", [invalid, self.second]),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(result)
        }
        self.assertEqual(
            RULES["resultStates"]["needs_input"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["errorCodes"]["invalidProductionRequest"],
            items["batch-item-a"]["errorCode"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )

    def test_non_object_request_returns_a_stable_public_result(self) -> None:
        result = run_production(
            None,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(
            RULES["errorCodes"]["invalidProductionRequest"],
            result.error_code,
        )

    def test_malformed_scoped_items_do_not_abort_shared_siblings(self) -> None:
        malformed_items = []
        missing_key = copy.deepcopy(self.first)
        missing_key.pop("templateKey")
        malformed_items.append(missing_key)
        malformed_strategy = copy.deepcopy(self.first)
        malformed_strategy["replacementStrategy"] = [{}]
        malformed_items.append(malformed_strategy)

        for index, malformed in enumerate(malformed_items):
            with self.subTest(index=index):
                output_root = self.output_root / f"malformed-{index}"
                result = run_production(
                    batch_request(
                        f"malformed-shared-{index}",
                        [malformed, self.second],
                        shared_policy(
                            "malformed-item-isolation",
                            f"malformed-r{index}",
                            ["batch-item-a", "batch-item-b"],
                            animal_pool("柯基犬", "水豚"),
                        ),
                    ),
                    output_root,
                    DeterministicFixtureAdapters(FIXTURE),
                    clock=lambda: FIXED_TIME,
                )
                items = {
                    item["productionItemId"]: item
                    for item in result_items(result)
                }
                self.assertEqual(
                    RULES["resultStates"]["needs_input"],
                    items["batch-item-a"]["state"],
                )
                self.assertEqual(
                    RULES["resultStates"]["completed"],
                    items["batch-item-b"]["state"],
                )

    def test_sibling_symlink_cannot_alias_another_item_directory(self) -> None:
        aliased = self.output_root / "batch-item-a"
        sibling = self.output_root / "batch-item-b"
        aliased.symlink_to(sibling, target_is_directory=True)

        result = run_production(
            batch_request(
                "sibling-symlink",
                [self.first, self.second],
            ),
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(result)
        }
        self.assertEqual(
            RULES["resultStates"]["needs_input"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )
        sibling_manifest = load_json(sibling / "production-manifest.json")
        self.assertEqual("batch-item-b", sibling_manifest["productionItemId"])

    def test_foreign_visual_facts_and_defaults_cannot_replace_item_sidecars(
        self,
    ) -> None:
        request = batch_request(
            "foreign-sidecars", [self.first, self.second]
        )
        completed = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertTrue(
            all(
                item["state"] == RULES["resultStates"]["completed"]
                for item in result_items(completed)
            )
        )
        first_dir = self.output_root / "batch-item-a"
        second_dir = self.output_root / "batch-item-b"
        foreign_analysis = load_json(second_dir / "template-analysis.json")
        foreign_analysis["neutralTitle"] = "另一张图的视觉标题"
        foreign_defaults = load_json(second_dir / "editable-template-spec.json")
        foreign_defaults["title"] = "另一张图的编辑默认值"
        replacements = {
            "template-analysis.json": foreign_analysis,
            "editable-template-spec.json": foreign_defaults,
        }
        manifest_path = first_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        for name, value in replacements.items():
            payload = (
                json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            (first_dir / name).write_bytes(payload)
            synchronize_artifact_record(manifest, name, payload)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        resumed = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )
        items = {
            item["productionItemId"]: item for item in result_items(resumed)
        }
        self.assertEqual(
            RULES["resultStates"]["blocked"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            items["batch-item-a"]["errorCode"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )

    def test_p7_recovery_replays_item_facts_before_formal_projection(self) -> None:
        request = batch_request(
            "p7-cross-item-recovery",
            [self.first, self.second],
        )
        completed = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertTrue(
            all(
                item["state"] == RULES["resultStates"]["completed"]
                for item in result_items(completed)
            )
        )
        first_dir = self.output_root / "batch-item-a"
        second_dir = self.output_root / "batch-item-b"
        foreign_draft = (second_dir / "gallery-template.draft.json").read_bytes()
        (first_dir / "gallery-template.draft.json").write_bytes(foreign_draft)
        manifest_path = first_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        synchronize_artifact_record(
            manifest,
            "gallery-template.draft.json",
            foreign_draft,
        )
        for name in ("final-validation-report.json", "gallery-template.json"):
            (first_dir / name).unlink()
            manifest["artifacts"].pop(name)
        manifest["phase"] = RULES["productionPhases"][7]["phase"]
        manifest["state"] = RULES["productionPhases"][7]["state"]
        manifest["outcome"] = None
        manifest.pop("error", None)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        resumed = run_production(
            request,
            self.output_root,
            self.adapters,
            clock=lambda: FIXED_TIME,
        )

        items = {
            item["productionItemId"]: item for item in result_items(resumed)
        }
        self.assertEqual(
            RULES["resultStates"]["blocked"], items["batch-item-a"]["state"]
        )
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            items["batch-item-a"]["errorCode"],
        )
        self.assertEqual(
            RULES["resultStates"]["completed"], items["batch-item-b"]["state"]
        )
        self.assertFalse((first_dir / "gallery-template.json").exists())


if __name__ == "__main__":
    unittest.main()

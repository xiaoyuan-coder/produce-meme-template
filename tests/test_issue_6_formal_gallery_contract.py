from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Callable

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production
from scripts.produce_meme_template.workflow import WorkflowStop, _formal_projection, _validate_final


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
SAMPLE_FIXTURE = ROOT / "fixtures" / "contracts" / "latest-gallery-samples"
RULES = json.loads((ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8"))
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")
FORMAL_CONTRACT = RULES["formalProjection"]
FORMAL_TOP_LEVEL = set(FORMAL_CONTRACT["topLevel"].values())
FORMAL_METADATA = FORMAL_CONTRACT["metadata"]
FORMAL_DRAFT_STATUS = FORMAL_CONTRACT["statusValues"]["draft"]
TOP_LEVEL_FIELDS = FORMAL_CONTRACT["topLevel"]
METADATA_FIELD = TOP_LEVEL_FIELDS["formalMetadata"]
STATUS_FIELD = TOP_LEVEL_FIELDS["lifecycleStatus"]
COVER_FIELD = TOP_LEVEL_FIELDS["coverAsset"]
REFERENCE_FIELD = TOP_LEVEL_FIELDS["referenceAsset"]
TAGS_FIELD = FORMAL_METADATA["classificationTags"]
REVIEW_FIELD = FORMAL_METADATA["reviewReason"]
FORBIDDEN_FIELDS = FORMAL_CONTRACT["forbiddenKeys"]
SIDECAR_FIELDS = FORMAL_CONTRACT["recognizedMetadataSidecars"]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def migrate_legacy_terms(value):
    if isinstance(value, str):
        result = value
        migrations = RULES["formalProjection"]["legacyTermMigrations"].values()
        for migration in sorted(migrations, key=lambda item: len(item["from"]), reverse=True):
            result = result.replace(migration["from"], migration["to"])
        return result
    if isinstance(value, list):
        return [migrate_legacy_terms(item) for item in value]
    if isinstance(value, dict):
        return {key: migrate_legacy_terms(item) for key, item in value.items()}
    return value


def leaf_paths(value, prefix=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaf_paths(item, (*prefix, key))
    elif isinstance(value, list):
        for item in value:
            yield from leaf_paths(item, (*prefix, "[]"))
    else:
        yield prefix


def differing_leaf_paths(left, right, prefix=()):
    if isinstance(left, dict) and isinstance(right, dict):
        for key in left.keys() | right.keys():
            yield from differing_leaf_paths(left.get(key), right.get(key), (*prefix, key))
    elif isinstance(left, list) and isinstance(right, list):
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
            yield from differing_leaf_paths(left_item, right_item, (*prefix, index))
        if len(left) != len(right):
            yield prefix
    elif left != right:
        yield prefix


class ApprovedAnalysisAdapters(DeterministicFixtureAdapters):
    def __init__(self, transform: Callable[[dict], dict]):
        super().__init__(FIXTURE)
        self.transform = transform

    def analyze_approved(self, approved_image: Path) -> dict:
        return self.transform(super().analyze_approved(approved_image))

    def audit_semantics(self, content: dict) -> dict:
        audit = super().audit_semantics(content)
        digest = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        audit["contentSha256"] = digest
        audit["observedContentSha256"] = digest
        return audit


class AssetUrlAdapters(DeterministicFixtureAdapters):
    def __init__(self, url: str):
        super().__init__(FIXTURE)
        self.url = url

    def upload(self, approved_image: Path, object_key: str) -> dict:
        receipt = super().upload(approved_image, object_key)
        receipt["url"] = self.url.replace("{objectKey}", object_key)
        return receipt


class Issue6FormalGalleryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_case(self, item_id: str, adapters: DeterministicFixtureAdapters):
        return run_production(
            {**self.request, "productionItemId": item_id},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

    def test_public_workflow_delivers_only_the_machine_whitelist(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        result = self.run_case("issue-six-formal-whitelist", adapters)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        record = load_json(result.gallery_template)
        self.assertEqual(FORMAL_TOP_LEVEL, set(record))
        self.assertEqual({TAGS_FIELD}, set(record[METADATA_FIELD]))
        self.assertEqual(record[COVER_FIELD], record[REFERENCE_FIELD])
        self.assertTrue(record[COVER_FIELD].startswith("https://"))
        self.assertTrue(_validate_final(record, RULES)["pass"])

    def test_public_workflow_emits_the_v2_runtime_contract(self) -> None:
        result = self.run_case(
            "v2-runtime-contract",
            DeterministicFixtureAdapters(FIXTURE),
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        record = load_json(result.gallery_template)
        self.assertEqual(1, record["imageN"])
        self.assertEqual("PROMPT", record["kind"])
        self.assertEqual([], record["preprocessSteps"])
        self.assertNotIn("promptEnhancement", record)
        self.assertNotIn("extract", json.dumps(record, ensure_ascii=False))

        semantics = record["runtimeSemantics"]
        self.assertEqual(
            {"version", "targetInstances", "inputBindings", "visualContract"},
            set(semantics),
        )
        self.assertEqual(1, semantics["version"])
        target_by_id = {
            target["id"]: target for target in semantics["targetInstances"]
        }
        self.assertEqual(
            {item["id"] for item in record["inputSchema"]["slots"]},
            set(semantics["inputBindings"]),
        )
        self.assertEqual(2, record["inputSchema"]["version"])
        for item in record["inputSchema"]["slots"]:
            suggestions = item["text"]["suggestions"]
            self.assertEqual(3, len(suggestions))
            binding = semantics["inputBindings"][item["id"]]
            self.assertTrue(binding["targetIds"])
            self.assertTrue(
                all(target_id in target_by_id for target_id in binding["targetIds"])
            )

        visual = semantics["visualContract"]
        self.assertEqual(
            {"medium", "styleTraits", "composition", "relations", "colorAndLight"},
            set(visual),
        )
        self.assertTrue(visual["medium"])
        self.assertTrue(visual["styleTraits"])
        self.assertTrue(visual["composition"])
        self.assertTrue(visual["relations"])

    def test_public_workflow_uses_authored_target_roles_and_regions(self) -> None:
        authored_targets = [
            {
                "id": "approved-animal-main",
                "kind": "identity_subject",
                "role": "软垫上蜷卧的中央主体",
                "region": "画面中央偏下的头部、躯干与前爪区域",
            },
            {
                "id": "approved-cushion",
                "kind": "content_element",
                "role": "承托主体的软垫",
                "region": "画面下半部、主体胸腹与前爪下方",
            },
            {
                "id": "approved-room",
                "kind": "content_element",
                "role": "室内背景与侧面光线",
                "region": "主体后方及画面四周",
            },
        ]

        def author_runtime_targets(analysis: dict) -> dict:
            analysis["runtimeSemantics"]["targetInstances"] = copy.deepcopy(
                authored_targets
            )
            return analysis

        result = self.run_case(
            "authored-runtime-targets",
            ApprovedAnalysisAdapters(author_runtime_targets),
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        record = load_json(result.gallery_template)
        expected_targets = copy.deepcopy(authored_targets)
        for target in expected_targets:
            target["id"] = target["id"].replace("-", "_")
        self.assertEqual(expected_targets, record["runtimeSemantics"]["targetInstances"])

    def test_public_workflow_rejects_targets_without_unique_visual_locations(self) -> None:
        generic_targets = [
            {
                "id": "approved-animal-main",
                "kind": "identity_subject",
                "role": "主体",
                "region": "对应位置",
            },
            {
                "id": "approved-cushion",
                "kind": "content_element",
                "role": "画面元素",
                "region": "主体区域",
            },
            {
                "id": "approved-room",
                "kind": "content_element",
                "role": "背景",
                "region": "画面区域",
            },
        ]

        def generic_runtime_targets(analysis: dict) -> dict:
            analysis["runtimeSemantics"]["targetInstances"] = copy.deepcopy(
                generic_targets
            )
            return analysis

        result = self.run_case(
            "generic-runtime-targets",
            ApprovedAnalysisAdapters(generic_runtime_targets),
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_needs_review_is_conditional_and_preserves_draft_status(self) -> None:
        reason = "画内文字角色仍需人工复核"

        def add_review_reason(analysis: dict) -> dict:
            analysis[REVIEW_FIELD] = reason
            return analysis

        result = self.run_case(
            "conditional-needs-review",
            ApprovedAnalysisAdapters(add_review_reason),
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        record = load_json(result.gallery_template)
        self.assertEqual(reason, record[METADATA_FIELD][REVIEW_FIELD])
        self.assertEqual(FORMAL_DRAFT_STATUS, record[STATUS_FIELD])

    def test_production_sidecars_do_not_leak_into_the_formal_record(self) -> None:
        legacy_fields = {
            FORBIDDEN_FIELDS["candidatePlanning"]: {"values": ["内部候选"]},
            SIDECAR_FIELDS["runtimeCapability"]: {"capability": "internal"},
            SIDECAR_FIELDS["sourceEvidence"]: {"path": "/tmp/source.png"},
            SIDECAR_FIELDS["inputAnalysis"]: {
                "slot": {FORBIDDEN_FIELDS["suggestionEvidence"]: ["内部理由"]}
            },
            SIDECAR_FIELDS["optimizationEvidence"]: {
                FORBIDDEN_FIELDS["externalRequestIdentity"]: "generation-request-1"
            },
        }

        def add_sidecar_evidence(analysis: dict) -> dict:
            analysis.update(copy.deepcopy(legacy_fields))
            return analysis

        result = self.run_case(
            "sidecar-isolation",
            ApprovedAnalysisAdapters(add_sidecar_evidence),
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        analysis = load_json(result.output_dir / "template-analysis.json")
        record = load_json(result.gallery_template)
        self.assertTrue(set(legacy_fields) <= set(analysis))
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertTrue(all(field not in serialized for field in legacy_fields))
        self.assertNotIn("/tmp/", serialized)
        self.assertNotIn("generation-request-1", serialized)

    def test_two_latest_samples_have_explicit_comparable_projections(self) -> None:
        expected_hashes = {
            "heart": "026e69c00d28495c5bfcafdb4cccd2bab84a6f33b7bbf580d1eef380264fd03a",
            "wedding": "160f042c8a06cb776b8bad0177363226f72397626d852a29cd672a161177acf9",
        }
        recognized_sidecars = set(RULES["formalProjection"]["recognizedMetadataSidecars"].values())

        for name, expected_hash in expected_hashes.items():
            with self.subTest(sample=name):
                input_path = SAMPLE_FIXTURE / f"{name}.input.json"
                source = load_json(input_path)
                expected = load_json(SAMPLE_FIXTURE / f"{name}.expected.json")
                self.assertEqual(expected_hash, hashlib.sha256(input_path.read_bytes()).hexdigest())
                self.assertTrue(set(source[METADATA_FIELD]) - {TAGS_FIELD} <= recognized_sidecars)
                source_projection = _formal_projection(source, source[COVER_FIELD], RULES)
                self.assertEqual(expected, source_projection)
                projected_expected = _formal_projection(expected, expected[COVER_FIELD], RULES)
                self.assertEqual(expected, projected_expected)
                self.assertTrue(_validate_final(projected_expected, RULES)["pass"])
                self.assertFalse(_validate_final(source, RULES)["pass"])
                classifications = []
                for path in leaf_paths(source):
                    if path[0] != METADATA_FIELD or path[1] == TAGS_FIELD:
                        classifications.append("formal")
                    elif path[1] in recognized_sidecars:
                        classifications.append("sidecar")
                    else:
                        classifications.append("unclassified")
                self.assertNotIn("unclassified", classifications)
                self.assertIn("formal", classifications)
                self.assertIn("sidecar", classifications)

        for name in expected_hashes:
            record = load_json(SAMPLE_FIXTURE / f"{name}.expected.json")
            self.assertEqual(1, record["runtimeSemantics"]["version"])
            self.assertNotIn("promptEnhancement", record)
            self.assertNotIn("extract", json.dumps(record, ensure_ascii=False))

    def test_unknown_formal_fields_are_rejected_instead_of_silently_dropped(self) -> None:
        base = load_json(SAMPLE_FIXTURE / "heart.expected.json")
        mutations = {
            "unknownTopLevel": lambda record: record.update({"unexpectedField": True}),
            "legacyCover": lambda record: record.update(
                {FORBIDDEN_FIELDS["legacyCover"]: record[COVER_FIELD]}
            ),
            "unexpectedMetadata": lambda record: record[METADATA_FIELD].update(
                {"unexpectedMetadataField": "value"}
            ),
            "generationInfoMutation": lambda record: record[METADATA_FIELD].update(
                {
                    FORBIDDEN_FIELDS["generationRequest"]: {
                        FORBIDDEN_FIELDS["externalRequestIdentity"]: "request-1"
                    }
                }
            ),
        }

        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                record = copy.deepcopy(base)
                mutate(record)
                with self.assertRaises(WorkflowStop):
                    _formal_projection(record, record[COVER_FIELD], RULES)

    def test_v2_runtime_relationships_and_removed_fields_are_hard_gates(self) -> None:
        base = load_json(SAMPLE_FIXTURE / "heart.expected.json")

        def missing_binding(record: dict) -> None:
            first_input = record["inputSchema"]["slots"][0]["id"]
            record["runtimeSemantics"]["inputBindings"].pop(first_input)

        def unknown_target(record: dict) -> None:
            first_input = record["inputSchema"]["slots"][0]["id"]
            record["runtimeSemantics"]["inputBindings"][first_input]["targetIds"] = [
                "missing_target"
            ]

        def duplicate_identity_owner(record: dict) -> None:
            first, second = record["inputSchema"]["slots"][:2]
            target_id = record["runtimeSemantics"]["inputBindings"][first["id"]][
                "targetIds"
            ][0]
            record["runtimeSemantics"]["inputBindings"][second["id"]]["targetIds"] = [
                target_id
            ]

        def legacy_extract(record: dict) -> None:
            record["inputSchema"]["slots"][0]["image"]["extract"] = {
                "enabled": True
            }

        cases = {
            "missingBinding": missing_binding,
            "unknownTarget": unknown_target,
            "duplicateIdentityOwner": duplicate_identity_owner,
            "legacyExtract": legacy_extract,
            "legacyPromptEnhancement": lambda record: record.update(
                {"promptEnhancement": {"instruction": "legacy"}}
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                record = copy.deepcopy(base)
                mutate(record)
                self.assertFalse(_validate_final(record, RULES)["pass"])

    def test_temporary_paths_data_urls_and_audit_fields_fail_final_validation(self) -> None:
        base = load_json(SAMPLE_FIXTURE / "heart.expected.json")
        cases = {
            "temporaryValue": lambda record: record.update(
                {COVER_FIELD: "/tmp/template.png", REFERENCE_FIELD: "/tmp/template.png"}
            ),
            "dataUrl": lambda record: record.update(
                {
                    COVER_FIELD: "data:image/png;base64,AAAA",
                    REFERENCE_FIELD: "data:image/png;base64,AAAA",
                }
            ),
            "auditField": lambda record: record[METADATA_FIELD].update(
                {SIDECAR_FIELDS["optimizationEvidence"]: {"pass": True}}
            ),
        }

        for name, mutate in cases.items():
            with self.subTest(case=name):
                record = copy.deepcopy(base)
                mutate(record)
                self.assertFalse(_validate_final(record, RULES)["pass"])

    def test_invalid_https_asset_url_fails_through_the_public_workflow(self) -> None:
        invalid_urls = (
            "https://",
            "https://user:secret@assets.example/template.png",
            "https:// assets.example/template.png",
            "https://assets.example/template.png\nnext",
            "https://assets.example/template\u00a0image.png",
            "https://assets.example/template\u200bimage.png",
            "https://assets.example:abc/template.png",
            "https://assets.example:99999/template.png",
            "https://%20/template.png",
        )

        for index, url in enumerate(invalid_urls):
            with self.subTest(url=url):
                result = self.run_case(f"invalid-asset-url-{index}", AssetUrlAdapters(url))
                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
                self.assertIsNone(result.gallery_template)

    def test_valid_https_urls_are_not_mistaken_for_local_paths(self) -> None:
        asset_urls = (
            "https://assets.example/home/{objectKey}",
            "https://assets.example/Users/{objectKey}",
            "https://assets.example/var/folders/{objectKey}",
        )

        for index, url in enumerate(asset_urls):
            with self.subTest(url=url):
                result = self.run_case(f"valid-asset-url-{index}", AssetUrlAdapters(url))
                self.assertEqual(RULES["resultStates"]["completed"], result.state)
                record = load_json(result.gallery_template)
                self.assertEqual(record[COVER_FIELD], record[REFERENCE_FIELD])
                self.assertTrue(record[COVER_FIELD].startswith(url.split("{", 1)[0]))

    def test_embedded_local_paths_and_data_urls_stop_before_upload(self) -> None:
        embedded_values = (
            "调试文件 /tmp/generated.png",
            "用户文件 /home/alice/template.png",
            "内联图片 data:image/png;base64,AAAA",
            "调试路径:/tmp/generated.png",
            "调试路径：/tmp/generated.png",
            "调试路径:C:\\Users\\alice\\template.png",
            "调试路径=../tmp/generated.png",
            "https://example.test/image?source=/tmp/generated.png",
            "https://example.test/image#local=/Users/alice/input.png",
        )

        for index, embedded_value in enumerate(embedded_values):
            with self.subTest(value=embedded_value):
                def embed_local_value(analysis: dict, value: str = embedded_value) -> dict:
                    analysis["neutralDescription"] = value
                    return analysis

                adapters = ApprovedAnalysisAdapters(embed_local_value)
                result = self.run_case(f"embedded-local-value-{index}", adapters)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
                self.assertEqual([], adapters.upload_calls)


if __name__ == "__main__":
    unittest.main()

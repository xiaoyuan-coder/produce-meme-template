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
        receipt["url"] = self.url
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
            "heart": "e0970eda3bbc77399ff222bce0023c38e8917dbf817830942489b36d18c0ad34",
            "wedding": "5786703f093b64513e556a77ae0af9455e6af41d9bdd1d922444774174b5736e",
        }
        recognized_sidecars = set(RULES["formalProjection"]["recognizedMetadataSidecars"].values())
        semantic_corrections = {
            "heart": {(TOP_LEVEL_FIELDS["userTitle"],)},
            "wedding": {
                (TOP_LEVEL_FIELDS["userTitle"],),
                (TOP_LEVEL_FIELDS["userDescription"],),
                (TOP_LEVEL_FIELDS["userPromptTemplate"],),
            },
        }

        for name, expected_hash in expected_hashes.items():
            with self.subTest(sample=name):
                input_path = SAMPLE_FIXTURE / f"{name}.input.json"
                source = load_json(input_path)
                expected = load_json(SAMPLE_FIXTURE / f"{name}.expected.json")
                self.assertEqual(expected_hash, hashlib.sha256(input_path.read_bytes()).hexdigest())
                self.assertTrue(set(source[METADATA_FIELD]) - {TAGS_FIELD} <= recognized_sidecars)
                source_projection = _formal_projection(source, source[COVER_FIELD], RULES)
                migrated_projection = migrate_legacy_terms(source_projection)
                self.assertEqual(
                    semantic_corrections[name],
                    set(differing_leaf_paths(migrated_projection, expected)),
                )
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

        heart = load_json(SAMPLE_FIXTURE / "heart.expected.json")
        wedding = load_json(SAMPLE_FIXTURE / "wedding.expected.json")
        self.assertNotIn("他", heart[TOP_LEVEL_FIELDS["userTitle"]])
        self.assertNotIn("猫咪", wedding[TOP_LEVEL_FIELDS["userTitle"]])
        self.assertNotIn("三只宠物", wedding[TOP_LEVEL_FIELDS["userDescription"]])
        self.assertIn("根据数量", wedding[TOP_LEVEL_FIELDS["userPromptTemplate"]])

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
            "https://assets.example/home/template.png",
            "https://assets.example/Users/template.png",
            "https://assets.example/var/folders/template.png",
        )

        for index, url in enumerate(asset_urls):
            with self.subTest(url=url):
                result = self.run_case(f"valid-asset-url-{index}", AssetUrlAdapters(url))
                self.assertEqual(RULES["resultStates"]["completed"], result.state)
                record = load_json(result.gallery_template)
                self.assertEqual(url, record[COVER_FIELD])
                self.assertEqual(url, record[REFERENCE_FIELD])

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

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production
from scripts.produce_meme_template.gallery_contract_migration import (
    migrate_gallery_template_to_runtime_v2,
)
from scripts.produce_meme_template.template_compiler import _validate_final
from scripts.produce_meme_template.template_test import _formal_template_errors


ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "contracts/machine-rules.json").read_text(encoding="utf-8"))
SCHEMA = json.loads(
    (
        ROOT
        / RULES["releaseManagementContract"]["gallerySnapshotRelativePath"]
    ).read_text(encoding="utf-8")
)
UPSTREAM_EXAMPLES = (
    ROOT.parent
    / "memebuy数据/memebuy_monorepo/apps/memebuy-merchant-management/docs/gallery/template-import"
)
LEGACY_SAMPLE = ROOT / "fixtures/contracts/latest-gallery-samples/heart.expected.json"
E2E_FIXTURE = ROOT / "fixtures/e2e/simple-animal"


class DynamicGroupAdapters(DeterministicFixtureAdapters):
    def analyze_approved(self, approved_image: Path) -> dict:
        analysis = super().analyze_approved(approved_image)
        runtime = analysis["runtimeSemantics"]
        runtime["targetInstances"] = [
            {
                "id": "uploaded_pet_group",
                "kind": "identity_group",
                "role": "围绕软垫主体区域的全部上传宠物成员",
                "region": "画面中央偏下、随成员数量伸缩的群像区域",
                "memberKind": "pet",
                "minMembers": 1,
                "maxMembers": 8,
            },
            *[
                target
                for target in runtime["targetInstances"]
                if target["kind"] == "content_element"
            ],
        ]
        runtime["inputBindings"] = {
            "pet_subject": {
                "operation": "replace_identity",
                "targetIds": ["uploaded_pet_group"],
                "bindingPolicy": "preserve_group",
            }
        }
        return analysis

    def audit_semantics(self, content: dict) -> dict:
        audit = super().audit_semantics(content)
        digest = hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        audit["contentSha256"] = digest
        audit["observedContentSha256"] = digest
        return audit


class Issue32GalleryContractV2Test(unittest.TestCase):
    def test_public_production_compiles_an_authored_dynamic_group(self) -> None:
        request = json.loads((E2E_FIXTURE / "request.json").read_text(encoding="utf-8"))
        request["sourceImage"] = str(
            (E2E_FIXTURE / request["sourceImage"]).resolve()
        )
        request["productionItemId"] = "runtime-v2-dynamic-group"
        with tempfile.TemporaryDirectory() as temporary:
            result = run_production(
                request,
                Path(temporary),
                DynamicGroupAdapters(E2E_FIXTURE),
                clock=lambda: datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
            )
            self.assertEqual(RULES["resultStates"]["completed"], result.state)
            record = json.loads(result.gallery_template.read_text(encoding="utf-8"))
        target = next(
            target
            for target in record["runtimeSemantics"]["targetInstances"]
            if target["kind"] == "identity_group"
        )
        binding = record["runtimeSemantics"]["inputBindings"]["pet_subject"]
        self.assertEqual([target["id"]], binding["targetIds"])
        self.assertEqual("preserve_group", binding["bindingPolicy"])
        self.assertEqual(["group_photo"], binding["allowedSourceGrouping"])
        self.assertEqual("source", binding["clothingOwnership"])

    def test_frozen_snapshot_matches_both_official_v2_examples(self) -> None:
        validator = Draft202012Validator(SCHEMA)
        for name in ("agent-v2-example.json", "agent-v2-dynamic-group-example.json"):
            with self.subTest(name=name):
                record = json.loads((UPSTREAM_EXAMPLES / name).read_text(encoding="utf-8"))
                self.assertEqual([], list(validator.iter_errors(record)))

    def test_dynamic_group_is_current_and_invalid_member_range_is_blocked(self) -> None:
        record = json.loads(
            (UPSTREAM_EXAMPLES / "agent-v2-dynamic-group-example.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(_validate_final(record, RULES)["pass"])
        invalid = copy.deepcopy(record)
        target = invalid["runtimeSemantics"]["targetInstances"][0]
        target["minMembers"] = 13
        target["maxMembers"] = 2
        report = _validate_final(invalid, RULES)
        self.assertFalse(report["pass"])
        self.assertIn(
            f"{target['id']} 的动态群像成员范围无效。",
            report["runtimeSemanticsErrors"],
        )

    def test_v1_remains_readable_by_t1_but_is_not_a_new_production_record(self) -> None:
        record = json.loads(LEGACY_SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual([], _formal_template_errors(record))
        self.assertFalse(_validate_final(record, RULES)["pass"])
        self.assertTrue(_validate_final(record, RULES, require_current=False)["pass"])

    def test_v1_migration_requires_explicit_clothing_decisions_and_preserves_source(self) -> None:
        source = json.loads(LEGACY_SAMPLE.read_text(encoding="utf-8"))
        original = copy.deepcopy(source)
        audit = migrate_gallery_template_to_runtime_v2(source)
        self.assertEqual("needs_decision", audit.status)
        self.assertTrue(audit.required_decisions)
        self.assertEqual(original, source)

        decisions = {input_id: "source" for input_id in audit.required_decisions}
        result = migrate_gallery_template_to_runtime_v2(source, decisions)
        self.assertEqual("migrated", result.status)
        self.assertIsNotNone(result.migrated)
        migrated = result.migrated
        self.assertEqual(2, migrated["runtimeSemantics"]["version"])
        for input_id in audit.required_decisions:
            self.assertEqual(
                "source",
                migrated["runtimeSemantics"]["inputBindings"][input_id][
                    "clothingOwnership"
                ],
            )
        self.assertTrue(_validate_final(migrated, RULES)["pass"])
        self.assertEqual(original, source)


if __name__ == "__main__":
    unittest.main()

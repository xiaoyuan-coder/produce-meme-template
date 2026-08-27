from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
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
        subject_slot = next(
            slot for slot in analysis["slotCandidates"] if slot["id"] == "pet_subject"
        )
        subject_slot["inputModes"] = ["image"]
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
        analysis["groupStrategyDecisions"] = [
            {
                "inputId": "pet_subject",
                "targetId": "uploaded_pet_group",
                "route": "dynamic_group_photo",
                "identityFidelityRequired": True,
                "wholeGroupUploadNatural": True,
                "memberCountMayVary": True,
                "rolesIndependentlyAddressable": False,
                "homogeneousMemberKind": True,
                "sameIdentityRepeated": False,
                "coreGameplayEvidence": "核心玩法是让整组合照成员共同进入软垫群像，人数可随上传图变化",
                "highValueSlotEvidence": "一个纯图片群体槽比逐成员槽更符合用户上传整张合照的操作习惯",
            }
        ]
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
        group_input = next(
            slot
            for slot in record["inputSchema"]["slots"]
            if slot["id"] == "pet_subject"
        )
        self.assertIn("image", group_input)
        self.assertNotIn("text", group_input)
        self.assertNotIn("resolutionStrategy", group_input)

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

    def test_dynamic_group_rejects_fixed_member_count_and_compound_input(self) -> None:
        dynamic_contract = RULES["runtimeSemanticsContract"][
            "dynamicIdentityInputContract"
        ]
        record = json.loads(
            (UPSTREAM_EXAMPLES / "agent-v2-dynamic-group-example.json").read_text(
                encoding="utf-8"
            )
        )
        fixed_count = copy.deepcopy(record)
        target = fixed_count["runtimeSemantics"]["targetInstances"][0]
        target["maxMembers"] = target["minMembers"]
        self.assertFalse(_validate_final(fixed_count, RULES)["pass"])

        compound = copy.deepcopy(record)
        slot = compound["inputSchema"]["slots"][0]
        slot["resolutionStrategy"] = "image_over_text"
        slot["text"] = {
            "presentation": "suggestions",
            "allowCustom": True,
            "defaultValue": "一家人",
            "placeholder": "描述群体",
            "suggestions": ["一家人", "朋友们", "同事们"],
        }
        report = _validate_final(compound, RULES)
        self.assertFalse(report["pass"])
        self.assertIn(
            dynamic_contract["validationMessage"]
            + " @ runtimeSemantics.inputBindings.family_group",
            report["runtimeSemanticsErrors"],
        )

    def test_group_strategy_decision_cannot_reference_unbound_targets(self) -> None:
        class UnboundDecisionAdapters(DynamicGroupAdapters):
            def analyze_approved(self, approved_image: Path) -> dict:
                analysis = super().analyze_approved(approved_image)
                analysis["groupStrategyDecisions"].append(
                    {
                        "inputId": "invented_cat_group_text",
                        "targetId": "invented_cat_group",
                        "route": "descriptive_content_group",
                        "identityFidelityRequired": False,
                        "wholeGroupUploadNatural": False,
                        "memberCountMayVary": True,
                        "rolesIndependentlyAddressable": False,
                        "homogeneousMemberKind": True,
                        "sameIdentityRepeated": False,
                        "coreGameplayEvidence": "密集猫群只需要数量与群聚效果，不保留每只猫的身份",
                        "highValueSlotEvidence": "文字内容槽比逐只图片槽更符合用户的编辑动机",
                    }
                )
                return analysis

        request = json.loads((E2E_FIXTURE / "request.json").read_text(encoding="utf-8"))
        request["sourceImage"] = str(E2E_FIXTURE / request["sourceImage"])
        request["productionItemId"] = "unbound-group-strategy-decision"
        with tempfile.TemporaryDirectory() as temporary:
            result = run_production(
                request,
                Path(temporary),
                UnboundDecisionAdapters(E2E_FIXTURE),
                clock=lambda: datetime.fromisoformat("2026-08-27T08:00:00+00:00"),
            )
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)

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

    def test_migration_rejects_a_v2_record_with_broken_cross_references(self) -> None:
        record = json.loads(
            (UPSTREAM_EXAMPLES / "agent-v2-example.json").read_text(
                encoding="utf-8"
            )
        )
        binding = next(iter(record["runtimeSemantics"]["inputBindings"].values()))
        binding["targetIds"] = ["missing-target"]

        result = migrate_gallery_template_to_runtime_v2(record)

        self.assertEqual("invalid", result.status)
        self.assertIsNone(result.migrated)
        self.assertTrue(result.errors)

    def test_migration_audits_the_official_fixed_identity_v2_example(self) -> None:
        record = json.loads(
            (UPSTREAM_EXAMPLES / "agent-v2-example.json").read_text(
                encoding="utf-8"
            )
        )

        result = migrate_gallery_template_to_runtime_v2(record)

        self.assertEqual("needs_decision", result.status)
        self.assertTrue(result.required_decisions)
        self.assertIn("explicit clothing ownership", result.errors[0])

    def test_migration_cli_accepts_the_official_v2_bundle_shape(self) -> None:
        source = json.loads(LEGACY_SAMPLE.read_text(encoding="utf-8"))
        audit = migrate_gallery_template_to_runtime_v2(source)
        decisions = {
            source["key"]: {
                input_id: "source" for input_id in audit.required_decisions
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "bundle.json"
            decisions_path = root / "decisions.json"
            output_root = root / "output"
            input_path.write_text(
                json.dumps({"version": 2, "templates": [source]}),
                encoding="utf-8",
            )
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "migrate_gallery_contract_v2.py"),
                    "--input",
                    str(input_path),
                    "--decisions",
                    str(decisions_path),
                    "--output",
                    str(output_root),
                    "--apply",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stdout)
            self.assertTrue((output_root / f"{source['key']}.json").is_file())

    def test_migration_cli_accepts_the_readable_v1_bundle_shape(self) -> None:
        source = json.loads(LEGACY_SAMPLE.read_text(encoding="utf-8"))
        audit = migrate_gallery_template_to_runtime_v2(source)
        decisions = {
            source["key"]: {
                input_id: "source" for input_id in audit.required_decisions
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "v1-bundle.json"
            decisions_path = root / "decisions.json"
            output_root = root / "output"
            input_path.write_text(
                json.dumps({"version": 1, "templates": [source]}),
                encoding="utf-8",
            )
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "migrate_gallery_contract_v2.py"),
                    "--input",
                    str(input_path),
                    "--decisions",
                    str(decisions_path),
                    "--output",
                    str(output_root),
                    "--apply",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stdout)
            self.assertTrue((output_root / f"{source['key']}.json").is_file())

    def test_migration_cli_reports_an_unhashable_key_without_crashing_batch(self) -> None:
        source = json.loads(LEGACY_SAMPLE.read_text(encoding="utf-8"))
        source["key"] = ["invalid"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(source), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "migrate_gallery_contract_v2.py"),
                    "--input",
                    str(input_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, completed.returncode, completed.stdout)
            report = json.loads(completed.stdout)
            self.assertEqual("invalid", report["items"][0]["status"])

    def test_migration_cli_rejects_a_boolean_bundle_version(self) -> None:
        source = json.loads(LEGACY_SAMPLE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "invalid-bundle.json"
            input_path.write_text(
                json.dumps({"version": True, "templates": [source]}),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "migrate_gallery_contract_v2.py"),
                    "--input",
                    str(input_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, completed.returncode)
            self.assertNotIn("Traceback", completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual("invalid", report["items"][0]["status"])

    def test_migration_cli_rejects_a_template_key_that_escapes_output(self) -> None:
        source = json.loads(LEGACY_SAMPLE.read_text(encoding="utf-8"))
        source["key"] = "../escaped"
        audit = migrate_gallery_template_to_runtime_v2(source)
        decisions = {
            source["key"]: {
                input_id: "source" for input_id in audit.required_decisions
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            decisions_path = root / "decisions.json"
            output_root = root / "isolated-output"
            input_path.write_text(json.dumps(source), encoding="utf-8")
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "migrate_gallery_contract_v2.py"),
                    "--input",
                    str(input_path),
                    "--decisions",
                    str(decisions_path),
                    "--output",
                    str(output_root),
                    "--apply",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, completed.returncode, completed.stdout)
            self.assertFalse((root / "escaped.json").exists())


if __name__ == "__main__":
    unittest.main()

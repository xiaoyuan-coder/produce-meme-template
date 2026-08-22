from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.export_gallery_templates import ExportError, export_gallery_templates
from scripts.produce_meme_template.artifacts import pretty_json_bytes, sha256_bytes
from scripts.produce_meme_template.artifacts import canonical_json_bytes, sha256_file
from scripts.produce_meme_template import run_production
from tests.live_production_support import build_live_test_adapters


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
FIXED_TIME = datetime.fromisoformat("2026-08-22T08:00:00+00:00")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class GalleryTemplateExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        rules = load(ROOT / "contracts" / "machine-rules.json")
        diagnostic_fields = rules["releaseManagementContract"]["diagnosticFields"]
        def installed_runtime_preflight(_production_pin=None) -> dict:
            return {
                "pass": True,
                diagnostic_fields["installSource"]: "/verified-install/4.0.0",
                diagnostic_fields["errorCodes"]: [],
            }
        base_request = load(FIXTURE / "request.json")
        base_request["sourceImage"] = str(
            (FIXTURE / base_request["sourceImage"]).resolve()
        )
        self.records = []
        self.production_manifests = []
        for index in range(2):
            adapters, _client, _bucket = build_live_test_adapters(FIXTURE)
            result = run_production(
                {
                    **base_request,
                    "productionItemId": f"export-live-item-{index}",
                    "templateKey": f"sleepy-cat-office-meme-{index}",
                },
                self.root / "production",
                adapters,
                execution_mode=rules["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                clock=lambda: FIXED_TIME,
                runtime_preflight=installed_runtime_preflight,
            )
            if result.outcome != "completed":
                raise AssertionError(result.as_dict())
            self.records.append(load(result.output_dir / "gallery-template.json"))
            self.production_manifests.append(
                result.output_dir / "production-manifest.json"
            )
        self.source = self.root / "gallery-template.json"
        self.source.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exports_one_formal_object_per_key_named_file(self) -> None:
        output = self.root / "单模板JSON"
        manifest_path = self.root / "交付清单.json"

        manifest = export_gallery_templates(
            self.source,
            output,
            manifest_path=manifest_path,
            production_manifests=self.production_manifests,
        )

        expected_names = {f"{record['key']}.json" for record in self.records}
        self.assertEqual(expected_names, {path.name for path in output.iterdir()})
        self.assertEqual(2, manifest["recordCount"])
        self.assertEqual([record["key"] for record in self.records], manifest["keys"])
        self.assertTrue(manifest_path.is_file())
        for record in self.records:
            self.assertEqual(record, load(output / f"{record['key']}.json"))

        repeated = export_gallery_templates(
            self.source,
            output,
            manifest_path=manifest_path,
            production_manifests=self.production_manifests,
        )
        self.assertEqual(manifest, repeated)

    def test_rejects_synthetic_manifest_without_complete_p8_lineage(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        execution = rules["productionExecutionContract"]
        fields = execution["profileFields"]
        synthetic_manifests = []
        for index, record in enumerate(self.records):
            item_dir = self.root / f"synthetic-{index}"
            item_dir.mkdir()
            profile = {
                fields["artifactType"]: execution["artifactType"],
                fields["schemaVersion"]: rules["schemaVersion"],
                fields["executionMode"]: execution["executionModes"]["liveExternal"],
                fields["deliveryEligible"]: True,
                fields["adapterTopology"]: execution["adapterTopologies"]["liveExternal"],
                fields["generationProvider"]: rules["generationExecutionContract"]["providerRoles"]["fal"],
                fields["storageProvider"]: rules["objectStorageContract"]["providerRoles"]["aliyunOss"],
                fields["visualReviewMethodIdentity"]: execution["liveReviewMethodIds"][0],
                fields["authoringAnalysisMethodIdentity"]: "live-authoring-analysis",
                fields["authoringAuditMethodIdentity"]: "independent-authoring-audit",
                fields["runtimeInstallSource"]: "/verified-install/test",
            }
            profile_payload = pretty_json_bytes(profile)
            (item_dir / execution["artifactName"]).write_bytes(profile_payload)
            profile_sha = sha256_bytes(profile_payload)
            manifest_path = item_dir / "production-manifest.json"
            manifest_path.write_bytes(
                pretty_json_bytes(
                    {
                        "templateKey": record["key"],
                        "state": rules["resultStates"]["completed"],
                        "outcome": "completed",
                        "executionMode": execution["executionModes"]["liveExternal"],
                        "executionProfileSha256": profile_sha,
                        "artifacts": {
                            execution["artifactName"]: {"sha256": profile_sha},
                            "gallery-template.json": {
                                "sha256": sha256_bytes(pretty_json_bytes(record))
                            },
                        },
                    }
                )
            )
            synthetic_manifests.append(manifest_path)
        with self.assertRaisesRegex(ExportError, "谱系"):
            export_gallery_templates(
                self.source,
                self.root / "synthetic-output",
                manifest_path=self.root / "synthetic-manifest.json",
                production_manifests=synthetic_manifests,
            )

    def test_rejects_duplicate_keys_before_writing(self) -> None:
        self.source.write_text(
            json.dumps([self.records[0], self.records[0]], ensure_ascii=False),
            encoding="utf-8",
        )
        output = self.root / "单模板JSON"

        with self.assertRaisesRegex(ExportError, "重复 key"):
            export_gallery_templates(
                self.source,
                output,
                manifest_path=self.root / "duplicate-manifest.json",
            )

        self.assertFalse(output.exists())

    def test_rejects_invalid_record_and_conflicting_existing_file(self) -> None:
        invalid = dict(self.records[0])
        invalid["cover"] = "file:///tmp/cover.png"
        self.source.write_text(
            json.dumps(invalid, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ExportError, "未通过当前正式 Gallery 合同"):
            export_gallery_templates(
                self.source,
                self.root / "invalid",
                manifest_path=self.root / "invalid-manifest.json",
            )

        self.source.write_text(
            json.dumps(self.records[0], ensure_ascii=False),
            encoding="utf-8",
        )
        output = self.root / "单模板JSON"
        output.mkdir()
        target = output / f"{self.records[0]['key']}.json"
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "已有不同内容"):
            export_gallery_templates(
                self.source,
                output,
                manifest_path=self.root / "conflict-manifest.json",
                production_manifests=[self.production_manifests[0]],
            )

    def test_requires_a_manifest_outside_the_data_directory(self) -> None:
        with self.assertRaises(TypeError):
            export_gallery_templates(self.source, self.root / "missing-manifest")

    def test_rejects_missing_install_source_and_extra_profile_fields(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        execution = rules["productionExecutionContract"]
        fields = execution["profileFields"]
        source = self.root / "single-gallery-template.json"
        source.write_bytes(pretty_json_bytes(self.records[0]))
        production_manifest = self.production_manifests[0]
        profile_path = production_manifest.parent / execution["artifactName"]
        original_profile = profile_path.read_bytes()
        original_manifest = production_manifest.read_bytes()

        for invalid_profile_change in (
            lambda profile: {
                key: value
                for key, value in profile.items()
                if key != fields["runtimeInstallSource"]
            },
            lambda profile: {**profile, "uncontractedEvidence": True},
            lambda _profile: [],
        ):
            with self.subTest(change=invalid_profile_change):
                profile_path.write_bytes(original_profile)
                production_manifest.write_bytes(original_manifest)
                profile = invalid_profile_change(load(profile_path))
                profile_payload = pretty_json_bytes(profile)
                profile_path.write_bytes(profile_payload)
                profile_sha = sha256_bytes(profile_payload)
                manifest = load(production_manifest)
                manifest["executionProfileSha256"] = profile_sha
                manifest["artifacts"][execution["artifactName"]][
                    "sha256"
                ] = profile_sha
                production_manifest.write_bytes(pretty_json_bytes(manifest))

                with self.assertRaisesRegex(ExportError, "不可交付"):
                    export_gallery_templates(
                        source,
                        self.root / "invalid-profile",
                        manifest_path=self.root / "invalid-profile-manifest.json",
                        production_manifests=[production_manifest],
                    )

    def test_rejects_rehashed_visual_review_method_drift(self) -> None:
        rules = load(ROOT / "contracts" / "machine-rules.json")
        source = self.root / "single-review-drift.json"
        source.write_bytes(pretty_json_bytes(self.records[0]))
        manifest_path = self.production_manifests[0]
        manifest = load(manifest_path)
        revision = manifest["revision"]
        review_name = (
            "visual-review.json"
            if revision == 1
            else f"visual-review.r{revision}.json"
        )
        review_path = manifest_path.parent / review_name
        review = load(review_path)
        review["method"]["id"] = "rehashed-different-reviewer"
        review_path.write_bytes(pretty_json_bytes(review))

        artifact = manifest["artifacts"][review_name]
        artifact["sha256"] = sha256_file(review_path)
        artifact["bytes"] = review_path.stat().st_size
        batch = rules["batchProductionContract"]
        artifact[batch["artifactScopeDigestField"]] = sha256_bytes(
            canonical_json_bytes(
                {
                    "productionItemId": manifest["productionItemId"],
                    "artifact": review_name,
                    "sha256": artifact["sha256"],
                }
            )
        )
        dependency_field = batch["dependencyDigestField"]
        for record in manifest["artifacts"].values():
            dependency_digests = record.get(dependency_field)
            if isinstance(dependency_digests, dict):
                for dependency in dependency_digests:
                    dependency_digests[dependency] = manifest["artifacts"][
                        dependency
                    ]["sha256"]
        manifest_path.write_bytes(pretty_json_bytes(manifest))

        with self.assertRaisesRegex(ExportError, "不可交付"):
            export_gallery_templates(
                source,
                self.root / "review-drift-output",
                manifest_path=self.root / "review-drift-manifest.json",
                production_manifests=[manifest_path],
            )

    def test_data_directory_rejects_manifest_and_unexpected_files(self) -> None:
        output = self.root / "单模板JSON"
        output.mkdir()
        (output / "notes.txt").write_text("sidecar", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "交付范围外"):
            export_gallery_templates(
                self.source,
                output,
                manifest_path=self.root / "notes-manifest.json",
                production_manifests=self.production_manifests,
            )

        hidden_output = self.root / "hidden"
        hidden_output.mkdir()
        (hidden_output / ".DS_Store").write_text("sidecar", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "交付范围外"):
            export_gallery_templates(
                self.source,
                hidden_output,
                manifest_path=self.root / "hidden-manifest.json",
                production_manifests=self.production_manifests,
            )

        clean_output = self.root / "clean"
        with self.assertRaisesRegex(ExportError, "数据目录之外"):
            export_gallery_templates(
                self.source,
                clean_output,
                manifest_path=clean_output / "manifest.json",
                production_manifests=self.production_manifests,
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import scripts.produce_meme_template as production_api
from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
FIXED_TIME = datetime.fromisoformat("2026-08-17T08:00:00+00:00")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FakeOssBucket:
    def __init__(self, *, interrupt_after_put: bool = False) -> None:
        self.objects: dict[str, dict] = {}
        self.put_calls: list[str] = []
        self.head_calls: list[str] = []
        self.interrupt_after_put = interrupt_after_put

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def head_object(self, key: str) -> SimpleNamespace:
        self.head_calls.append(key)
        value = self.objects[key]
        return SimpleNamespace(
            headers={"x-oss-meta-sha256": value["sha256"]},
            request_id="oss-head-request-001",
            status=200,
            etag=value["etag"],
            content_length=len(value["body"]),
        )

    def put_object(
        self, key: str, body: bytes, *, headers: dict[str, str]
    ) -> SimpleNamespace:
        self.put_calls.append(key)
        etag = hashlib.new(
            RULES["objectStorageContract"]["aliyun"]["objectIdentityAlgorithm"],
            body,
        ).hexdigest()
        self.objects[key] = {
            "body": body,
            "sha256": headers["x-oss-meta-sha256"],
            "etag": etag,
        }
        if self.interrupt_after_put:
            self.interrupt_after_put = False
            raise SystemExit("process exited after OSS accepted the object")
        return SimpleNamespace(
            request_id="oss-put-request-001",
            status=200,
            etag=etag,
        )


class Issue11OssFinalizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_case(self, item_id: str, adapters) -> object:
        return run_production(
            {**self.request, "productionItemId": item_id},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

    def oss_adapters(self, bucket: FakeOssBucket):
        adapter_type = getattr(production_api, "AliyunOssWorkflowAdapters")
        return adapter_type(
            DeterministicFixtureAdapters(FIXTURE),
            bucket=bucket,
            public_base_url="https://93.184.216.34/templates",
        )

    def test_real_adapter_rejects_a_hostname_resolving_outside_public_internet(
        self,
    ) -> None:
        adapter_type = getattr(production_api, "AliyunOssWorkflowAdapters")

        def private_resolver(*_args, **_kwargs):
            return [(2, 1, 6, "", ("198.18.2.216", 0))]

        with self.assertRaises(ValueError):
            adapter_type(
                DeterministicFixtureAdapters(FIXTURE),
                bucket=FakeOssBucket(),
                public_base_url="https://127.0.0.1.sslip.io/templates",
                resolve_host=private_resolver,
            )

    def rewind_to_p7(self, output_dir: Path) -> dict:
        manifest_path = output_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        uploaded_phase = RULES["productionPhases"][7]
        manifest["phase"] = uploaded_phase["phase"]
        manifest["state"] = uploaded_phase["state"]
        manifest["outcome"] = None
        manifest["history"] = [
            entry
            for entry in manifest["history"]
            if entry["phase"] != RULES["productionPhases"][8]["phase"]
        ]
        for name in ("final-validation-report.json", "gallery-template.json"):
            manifest["artifacts"].pop(name)
            (output_dir / name).unlink()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    def test_controlled_real_adapter_binds_receipt_and_both_formal_urls(self) -> None:
        bucket = FakeOssBucket()

        result = self.run_case(
            "oss-controlled-success", self.oss_adapters(bucket)
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        self.assertEqual(1, len(bucket.put_calls))
        receipt = load_json(result.output_dir / "asset-receipt.json")
        contract = RULES["objectStorageContract"]
        fields = contract["receiptFields"]
        self.assertEqual(set(fields.values()), set(receipt))
        self.assertEqual(1, receipt[fields["formalRevision"]])
        self.assertEqual(
            "evidence/approved-template-image.png",
            receipt[fields["approvedArtifact"]],
        )
        self.assertEqual(
            contract["uploadStatuses"]["uploaded"],
            receipt[fields["uploadStatus"]],
        )
        self.assertEqual(
            "oss-put-request-001", receipt[fields["providerRequestIdentity"]]
        )
        self.assertEqual(
            next(iter(bucket.objects.values()))["etag"],
            receipt[fields["objectIdentity"]],
        )
        self.assertEqual(200, receipt[fields["providerStatusCode"]])
        record = load_json(result.gallery_template)
        self.assertEqual(record["cover"], record["referenceImage"])
        self.assertEqual(receipt[fields["url"]], record["cover"])
        self.assertNotIn("coverUrl", record)
        final_validation = load_json(
            result.output_dir / "final-validation-report.json"
        )
        self.assertTrue(final_validation["pass"])

    def test_remote_success_before_receipt_is_reconciled_without_duplicate_object(self) -> None:
        item_id = "oss-crash-after-remote-success"
        bucket = FakeOssBucket(interrupt_after_put=True)

        with self.assertRaises(SystemExit):
            self.run_case(item_id, self.oss_adapters(bucket))
        self.assertEqual(1, len(bucket.objects))
        self.assertFalse(
            (self.output_root / item_id / "asset-receipt.json").exists()
        )

        resumed = self.run_case(item_id, self.oss_adapters(bucket))

        self.assertEqual(RULES["resultStates"]["completed"], resumed.state)
        self.assertEqual(1, len(bucket.put_calls))
        self.assertEqual(1, len(bucket.objects))
        receipt = load_json(resumed.output_dir / "asset-receipt.json")
        contract = RULES["objectStorageContract"]
        self.assertEqual(
            contract["uploadStatuses"]["reused"],
            receipt[contract["receiptFields"]["uploadStatus"]],
        )

    def test_existing_remote_object_with_another_digest_is_never_overwritten(self) -> None:
        bucket = FakeOssBucket()
        item_id = "oss-existing-object-conflict"
        expected_key = None

        adapters = self.oss_adapters(bucket)
        original_upload = adapters.upload

        def conflicting_upload(image_path: Path, object_key: str) -> dict:
            nonlocal expected_key
            expected_key = object_key
            bucket.objects[object_key] = {
                "body": b"different",
                "sha256": "0" * 64,
                "etag": "conflicting-etag",
            }
            return original_upload(image_path, object_key)

        adapters.upload = conflicting_upload
        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertIsNotNone(expected_key)
        self.assertEqual([], bucket.put_calls)
        self.assertFalse((result.output_dir / "asset-receipt.json").exists())
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_existing_remote_object_requires_the_approved_content_identity(self) -> None:
        bucket = FakeOssBucket()
        item_id = "oss-existing-object-wrong-etag"
        adapters = self.oss_adapters(bucket)
        original_upload = adapters.upload

        def wrong_etag_upload(image_path: Path, object_key: str) -> dict:
            body = image_path.read_bytes()
            bucket.objects[object_key] = {
                "body": body,
                "sha256": hashlib.sha256(body).hexdigest(),
                "etag": "0" * 32,
            }
            return original_upload(image_path, object_key)

        adapters.upload = wrong_etag_upload
        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual([], bucket.put_calls)
        self.assertFalse((result.output_dir / "asset-receipt.json").exists())

    def test_upload_response_with_legacy_cover_field_is_rejected(self) -> None:
        item_id = "oss-response-legacy-cover"
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_upload = adapters.upload

        def legacy_upload(image_path: Path, object_key: str) -> dict:
            response = original_upload(image_path, object_key)
            response["coverUrl"] = response["url"]
            return response

        adapters.upload = legacy_upload
        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertFalse((result.output_dir / "asset-receipt.json").exists())
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_private_asset_url_is_rejected_before_receipt_and_final_projection(self) -> None:
        item_id = "oss-response-private-url"
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_upload = adapters.upload
        url_field = RULES["objectStorageContract"]["adapterResultFields"]["url"]

        def private_url_upload(image_path: Path, object_key: str) -> dict:
            response = original_upload(image_path, object_key)
            response[url_field] = "https://127.0.0.1/private-template.png"
            return response

        adapters.upload = private_url_upload
        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertFalse((result.output_dir / "asset-receipt.json").exists())
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_asset_url_path_must_identify_the_expected_object_key(self) -> None:
        item_id = "oss-response-wrong-object-url"
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_upload = adapters.upload
        fields = RULES["objectStorageContract"]["adapterResultFields"]

        def wrong_object_url(image_path: Path, object_key: str) -> dict:
            response = original_upload(image_path, object_key)
            response[fields["url"]] = (
                "https://assets.example.test/gallery/templates/another-template/"
                + Path(object_key).name
            )
            return response

        adapters.upload = wrong_object_url
        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertFalse((result.output_dir / "asset-receipt.json").exists())
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_temporary_signed_asset_url_is_not_persisted_as_a_formal_url(self) -> None:
        item_id = "oss-response-temporary-signed-url"
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_upload = adapters.upload
        url_field = RULES["objectStorageContract"]["adapterResultFields"]["url"]

        def signed_url_upload(image_path: Path, object_key: str) -> dict:
            response = original_upload(image_path, object_key)
            response[url_field] += "?signature=TEMPORARYSECRET"
            return response

        adapters.upload = signed_url_upload
        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertFalse((result.output_dir / "asset-receipt.json").exists())
        persisted = "\n".join(
            path.read_text(encoding="utf-8")
            for path in result.output_dir.rglob("*.json")
        )
        self.assertNotIn("TEMPORARYSECRET", persisted)

    def test_malformed_receipt_during_p7_resume_returns_a_stable_integrity_result(self) -> None:
        item_id = "oss-malformed-receipt-resume"
        first = self.run_case(item_id, DeterministicFixtureAdapters(FIXTURE))
        output_dir = first.output_dir
        manifest_path = output_dir / "production-manifest.json"
        manifest = self.rewind_to_p7(output_dir)
        receipt_path = output_dir / "asset-receipt.json"
        receipt_path.write_text("{", encoding="utf-8")
        manifest["artifacts"]["asset-receipt.json"]["sha256"] = hashlib.sha256(
            receipt_path.read_bytes()
        ).hexdigest()
        manifest["artifacts"]["asset-receipt.json"]["bytes"] = receipt_path.stat().st_size
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        resumed = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            resumed.error_code,
        )
        self.assertEqual([], adapters.upload_calls)

    def test_non_object_draft_during_p7_resume_is_stably_blocked(self) -> None:
        item_id = "oss-non-object-draft-resume"
        first = self.run_case(item_id, DeterministicFixtureAdapters(FIXTURE))
        output_dir = first.output_dir
        manifest_path = output_dir / "production-manifest.json"
        manifest = self.rewind_to_p7(output_dir)
        draft_path = output_dir / "gallery-template.draft.json"
        draft_path.write_text("[]\n", encoding="utf-8")
        manifest["artifacts"]["gallery-template.draft.json"]["sha256"] = hashlib.sha256(
            draft_path.read_bytes()
        ).hexdigest()
        manifest["artifacts"]["gallery-template.draft.json"]["bytes"] = draft_path.stat().st_size
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        resumed = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            resumed.error_code,
        )
        self.assertEqual([], adapters.upload_calls)

    def test_completed_item_rechecks_receipt_revision_semantics(self) -> None:
        item_id = "oss-completed-receipt-revision"
        first = self.run_case(item_id, DeterministicFixtureAdapters(FIXTURE))
        receipt_path = first.output_dir / "asset-receipt.json"
        receipt = load_json(receipt_path)
        revision_field = RULES["objectStorageContract"]["receiptFields"][
            "formalRevision"
        ]
        receipt[revision_field] += 1
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path = first.output_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        manifest["artifacts"]["asset-receipt.json"]["sha256"] = hashlib.sha256(
            receipt_path.read_bytes()
        ).hexdigest()
        manifest["artifacts"]["asset-receipt.json"]["bytes"] = receipt_path.stat().st_size
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        resumed = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)
        self.assertEqual([], adapters.upload_calls)


if __name__ == "__main__":
    unittest.main()

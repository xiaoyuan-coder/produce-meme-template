from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.produce_meme_template import build_live_production_adapters
from scripts.produce_meme_template.adapters import DeterministicFixtureAdapters


class _FixtureRoleState:
    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir.resolve()
        self.approved_image_path_override = None
        self.authoring_handoffs: list[dict[str, Any]] = []


class LiveSourceRole:
    live_template_identity_method_id = "template-registry-semantic-review"

    def __init__(self, fixture_dir: Path) -> None:
        self.state = _FixtureRoleState(fixture_dir)

    def resolve_template_identity(self, source_image: Path, request: dict) -> dict:
        return DeterministicFixtureAdapters.resolve_template_identity(
            self.state, source_image, request
        )

    def analyze_source(self, source_image: Path, replacement_strategy: dict | None) -> dict:
        return DeterministicFixtureAdapters.analyze_source(
            self.state, source_image, replacement_strategy
        )


class LiveVisualReviewRole:
    live_review_method_id = "independent-live-vision-review"

    def __init__(
        self,
        fixture_dir: Path,
        *,
        emitted_method_id: str | None = None,
    ) -> None:
        self.state = _FixtureRoleState(fixture_dir)
        self.emitted_method_id = emitted_method_id or self.live_review_method_id

    def inspect_generated(self, generated_image: Path, review_request: dict) -> dict:
        review = DeterministicFixtureAdapters.inspect_generated(
            self.state, generated_image, review_request
        )
        review["method"] = {
            "id": self.emitted_method_id,
            "version": "test-live-1",
        }
        return review


class LiveAuthoringAnalysisRole:
    live_authoring_analysis_method_id = "live-authoring-analysis"

    def __init__(self, fixture_dir: Path) -> None:
        self.state = _FixtureRoleState(fixture_dir)

    def analyze_approved_with_handoff(
        self, approved_image: Path, authoring_handoff: dict
    ) -> dict:
        self.state.authoring_handoffs.append(authoring_handoff)
        return DeterministicFixtureAdapters.analyze_approved(
            self.state, approved_image
        )


class LiveAuthoringAuditRole:
    live_authoring_audit_method_id = "independent-authoring-audit"

    def __init__(self, fixture_dir: Path) -> None:
        self.state = _FixtureRoleState(fixture_dir)

    def audit_authoring_contract(
        self, approved_image: Path, review_request: dict
    ) -> dict:
        return DeterministicFixtureAdapters.audit_authoring_contract(
            self.state, approved_image, review_request
        )


class LiveSemanticAuditRole:
    def __init__(self, fixture_dir: Path) -> None:
        self.state = _FixtureRoleState(fixture_dir)

    def audit_semantics(self, content: dict) -> dict:
        return DeterministicFixtureAdapters.audit_semantics(self.state, content)


class LiveVisualContractAuditRole:
    def __init__(self, fixture_dir: Path) -> None:
        self.state = _FixtureRoleState(fixture_dir)

    def audit_visual_contract(
        self, approved_image: Path, review_request: dict
    ) -> dict:
        return DeterministicFixtureAdapters.audit_visual_contract(
            self.state, approved_image, review_request
        )


class FakeFalClient:
    class Completed:
        pass

    class Handle:
        request_id = "fal-live-test-request-001"

    def __init__(self) -> None:
        self.submit_calls: list[tuple[str, dict]] = []
        self.status_calls: list[tuple[str, str]] = []
        self.result_calls: list[tuple[str, str]] = []

    def submit(self, model: str, *, arguments: dict) -> Handle:
        self.submit_calls.append((model, arguments))
        return self.Handle()

    def status(self, model: str, request_id: str) -> Completed:
        self.status_calls.append((model, request_id))
        return self.Completed()

    def result(self, model: str, request_id: str) -> dict:
        self.result_calls.append((model, request_id))
        return {"images": [{"url": "https://fal.example/live-output.png"}]}


class FakeOssBucket:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.put_calls: list[str] = []
        self.head_calls: list[str] = []

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def head_object(self, key: str) -> SimpleNamespace:
        self.head_calls.append(key)
        value = self.objects[key]
        return SimpleNamespace(
            headers={"x-oss-meta-sha256": value["sha256"]},
            request_id="oss-live-head-001",
            status=200,
            etag=value["etag"],
            content_length=len(value["body"]),
        )

    def put_object(
        self, key: str, body: bytes, *, headers: dict[str, str]
    ) -> SimpleNamespace:
        self.put_calls.append(key)
        etag = hashlib.md5(body).hexdigest()
        self.objects[key] = {
            "body": body,
            "sha256": headers["x-oss-meta-sha256"],
            "etag": etag,
        }
        return SimpleNamespace(
            request_id="oss-live-put-001",
            status=200,
            etag=etag,
        )


def build_live_test_adapters(
    fixture_dir: Path,
    *,
    emitted_review_method_id: str | None = None,
):
    client = FakeFalClient()
    bucket = FakeOssBucket()
    approved = next(fixture_dir.glob("approved-template-image.*"))
    approved_bytes = DeterministicFixtureAdapters._fixture_image_result(approved)[
        "imageBytes"
    ]
    adapters = build_live_production_adapters(
        source_adapter=LiveSourceRole(fixture_dir),
        visual_review_adapter=LiveVisualReviewRole(
            fixture_dir,
            emitted_method_id=emitted_review_method_id,
        ),
        authoring_analysis_adapter=LiveAuthoringAnalysisRole(fixture_dir),
        authoring_audit_adapter=LiveAuthoringAuditRole(fixture_dir),
        semantic_audit_adapter=LiveSemanticAuditRole(fixture_dir),
        visual_contract_audit_adapter=LiveVisualContractAuditRole(fixture_dir),
        fal_options={
            "client": client,
            "download_bytes": lambda _url: approved_bytes,
            "sleep": lambda _seconds: None,
        },
        oss_options={
            "public_base_url": "https://cdn.example.com/templates",
            "bucket": bucket,
            "resolve_host": lambda *_args, **_kwargs: [
                (None, None, None, None, ("93.184.216.34", 443))
            ],
        },
    )
    return adapters, client, bucket

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class DeterministicFixtureAdapters:
    """Repeatable analysis, generation, semantic-audit, and upload adapters.

    Production integrations can implement the same protocol methods. The workflow owns
    all planning, gates, state, projection, and persistence decisions.
    """

    def __init__(self, fixture_dir: str | Path):
        self.fixture_dir = Path(fixture_dir).resolve()
        self.generate_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []

    def analyze_source(self, source_image: Path) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "source-analysis.json")
        result["sourceImageSha256"] = hashlib.sha256(source_image.read_bytes()).hexdigest()
        return result

    def generate(self, source_image: Path, generation_package: dict[str, Any]) -> dict[str, Any]:
        self.generate_calls.append(
            {
                "sourceImage": str(source_image),
                "requestId": generation_package["requestId"],
                "imageCount": generation_package["output"]["imageCount"],
            }
        )
        image_path = self.fixture_dir / "approved-template-image.ppm"
        return {
            "requestId": generation_package["requestId"],
            "extension": image_path.suffix,
            "imageBytes": image_path.read_bytes(),
        }

    def inspect_generated(self, generated_image: Path) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "visual-review.json")
        result["generatedImageSha256"] = hashlib.sha256(generated_image.read_bytes()).hexdigest()
        return result

    def analyze_approved(self, approved_image: Path) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "approved-analysis.json")
        result["visualFactSourceSha256"] = hashlib.sha256(approved_image.read_bytes()).hexdigest()
        return result

    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "semantic-audit.json")
        result["observedContentSha256"] = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return result

    def upload(self, approved_image: Path, object_key: str) -> dict[str, Any]:
        image_sha = hashlib.sha256(approved_image.read_bytes()).hexdigest()
        call = {"approvedImage": str(approved_image), "objectKey": object_key, "imageSha256": image_sha}
        self.upload_calls.append(call)
        return {
            "provider": "deterministic-fixture-oss",
            "objectKey": object_key,
            "imageSha256": image_sha,
            "url": f"https://fixtures.memebuy.test/{object_key}",
            "idempotencyKey": image_sha,
        }

    def with_visual_review(self, overrides: dict[str, Any]) -> "DeterministicFixtureAdapters":
        clone = copy.copy(self)
        original = clone.inspect_generated

        def inspect(generated_image: Path) -> dict[str, Any]:
            result = original(generated_image)
            for key, value in overrides.items():
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key].update(value)
                else:
                    result[key] = value
            return result

        clone.inspect_generated = inspect  # type: ignore[method-assign]
        clone.generate_calls = []
        clone.upload_calls = []
        return clone

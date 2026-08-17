from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


RULES_PATH = Path(__file__).resolve().parents[2] / "contracts" / "machine-rules.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _visual_evidence_sha(review: dict[str, Any], contract: dict[str, Any]) -> str:
    evidence_payload = {
        field: review[field]
        for field in contract["evidenceFieldRoles"].values()
    }
    return hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _semantic_overlap(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        normalized = re.sub(r"[\s，。！？；、,.!?;:]", "", value)
        for prefix in ("保持", "保留", "维持", "固定"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
        return normalized

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    return bool(
        normalized_left
        and normalized_right
        and (normalized_left in normalized_right or normalized_right in normalized_left)
    )


def _fixture_observed_language(
    source_text: str, declared_language: str, contract: dict[str, Any]
) -> str:
    languages = contract["languageValues"]
    has_latin = bool(re.search(r"[A-Za-z]", source_text))
    has_cjk = bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", source_text))
    has_kana = bool(re.search(contract["japaneseKanaPattern"], source_text))
    has_hangul = bool(re.search(contract["koreanHangulPattern"], source_text))
    if has_latin and (has_cjk or has_kana or has_hangul):
        return languages["mixed"]
    if has_kana:
        return languages["japanese"]
    if has_hangul:
        return languages["korean"]
    if has_latin:
        return languages["english"]
    if has_cjk:
        # The deterministic fixture adapter recognizes its regression corpus.
        # Production adapters perform the same decision from the Approved Image.
        traditional_regression_characters = set("歡迎光臨藝術設計")
        if any(character in traditional_regression_characters for character in source_text):
            return languages["traditionalChinese"]
    return declared_language


def _fixture_identity_text_is_neutral(values: list[str]) -> bool:
    neutral_meaning = re.compile(
        r"^(?:portrait|profile|player|your\s+name|hero|人物|角色|主角|档案|简介|代号|昵称)$",
        re.IGNORECASE,
    )
    return all(bool(neutral_meaning.fullmatch(value.strip())) for value in values)


def _fixture_normalized_visible_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "Z"))
    )


class DeterministicFixtureAdapters:
    """Repeatable analysis, generation, semantic-audit, and upload adapters.

    Production integrations can implement the same protocol methods. The workflow owns
    all planning, gates, state, projection, and persistence decisions.
    """

    def __init__(self, fixture_dir: str | Path):
        self.fixture_dir = Path(fixture_dir).resolve()
        self.generate_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []

    def analyze_source(
        self, source_image: Path, replacement_strategy: dict[str, Any] | None
    ) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "source-analysis.json")
        result["sourceImageSha256"] = hashlib.sha256(source_image.read_bytes()).hexdigest()
        if replacement_strategy and replacement_strategy.get("replacementValue") is not None:
            requested_value = replacement_strategy["replacementValue"]
            requested_category = replacement_strategy["replacementCategory"]
            matching = next(
                (
                    candidate
                    for candidate in result.get("replacementPool", [])
                    if candidate.get("value") == requested_value
                    and candidate.get("category") == requested_category
                ),
                None,
            )
            if matching is not None:
                result["explicitReplacementEvaluation"] = copy.deepcopy(matching)
        if replacement_strategy and replacement_strategy.get("preserve"):
            changed_components = [
                {"componentId": "primary-role", "value": result["target"]["role"]},
                {"componentId": "primary-identity", "value": result["target"]["identity"]},
                *[
                    {
                        "componentId": f"dependency-{index}-{item['type']}",
                        "value": item["value"],
                    }
                    for index, item in enumerate(result.get("dependencyClosure", []))
                ],
            ]
            result["preserveConflictEvaluations"] = [
                {
                    "preserveValue": preserve_value,
                    "conflictsWithChangedSet": any(
                        _semantic_overlap(preserve_value, component["value"])
                        for component in changed_components
                    ),
                    "changedComponentIds": [
                        component["componentId"]
                        for component in changed_components
                        if _semantic_overlap(preserve_value, component["value"])
                    ],
                }
                for preserve_value in replacement_strategy["preserve"]
            ]
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

    def inspect_generated(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "visual-review.json")
        contract = _read_json(RULES_PATH)["visualReviewContract"]
        result["bindings"] = {
            **review_request["bindings"],
            "generatedImageSha256": hashlib.sha256(generated_image.read_bytes()).hexdigest(),
            "evidenceSha256": _visual_evidence_sha(result, contract),
        }
        return result

    def analyze_approved(self, approved_image: Path) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "approved-analysis.json")
        result["visualFactSourceSha256"] = hashlib.sha256(approved_image.read_bytes()).hexdigest()
        return result

    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "semantic-audit.json")
        rules = _read_json(RULES_PATH)
        roles = rules["semanticAuditChecks"]
        slots = content["slots"]
        result["evidence"][roles["resolvedPrompts"]["evidence"]] = [
            "defaults",
            *[
                f"{slot['id']}={suggestion}"
                for slot in slots
                for suggestion in slot["suggestions"]
            ],
        ]
        result["evidence"][roles["openAxes"]["evidence"]] = [
            slot["semanticRole"] for slot in slots
        ]
        result["evidence"][roles["maximumDifference"]["evidence"]] = [
            slot["suggestions"][0] for slot in slots
        ]
        result["evidence"][roles["slotSuggestions"]["evidence"]] = [slot["id"] for slot in slots]
        text_contract = rules["visibleTextContract"]
        region_fields = text_contract["regionFields"]
        audit_fields = text_contract["semanticAuditFields"]
        decision_fields = text_contract["semanticDecisionFields"]
        regions = content[text_contract["analysisFields"]["regions"]]
        identity_value_class = text_contract["valueClasses"]["identityRelated"]
        subject_type = rules["slotCompilationContract"]["slotTypes"]["primarySubjectUpload"]
        subject_open = any(slot["type"] == subject_type for slot in slots)

        def region_identity_is_neutral(region: dict[str, Any]) -> bool:
            if region[region_fields["valueClass"]] != identity_value_class:
                return True
            values = [region[region_fields["selectedText"]]]
            slot_id = region.get(region_fields["slotIdentity"])
            if slot_id:
                values.extend(
                    suggestion
                    for slot in slots
                    if slot["id"] == slot_id
                    for suggestion in slot["suggestions"]
                )
            return _fixture_identity_text_is_neutral(values)

        slot_origin_fields = text_contract["slotOriginFields"]
        free_origin_fields = text_contract["freeContentOriginFields"]
        actions = text_contract["actions"]

        def region_matches_values(region: dict[str, Any], values: list[str]) -> bool:
            normalized_values = [
                _fixture_normalized_visible_text(value) for value in values
            ]
            source = _fixture_normalized_visible_text(
                region[region_fields["sourceText"]]
            )
            if source and any(source in value for value in normalized_values):
                return True
            lexemes = re.findall(
                r"[A-Za-z]+|\d+|[\u3400-\u4dbf\u4e00-\u9fff]{2,}",
                region[region_fields["sourceText"]],
            )
            for lexeme in lexemes:
                normalized = _fixture_normalized_visible_text(lexeme)
                if len(normalized) < 2:
                    continue
                if normalized.isascii():
                    derived = any(normalized in value for value in normalized_values)
                else:
                    derived = any(normalized == value for value in normalized_values)
                if derived:
                    return True
            return False

        def inferred_text_region(values: list[str]) -> str | None:
            for region in regions:
                if region_matches_values(region, values):
                    return region[region_fields["identity"]]
            return None

        def slot_origin_region(slot: dict[str, Any]) -> str | None:
            explicit = next(
                (
                    region[region_fields["identity"]]
                    for region in regions
                    if region.get(region_fields["slotIdentity"]) == slot["id"]
                ),
                None,
            )
            if explicit is not None:
                return explicit
            return inferred_text_region(
                [slot["defaultValue"], *slot["suggestions"]]
            )

        def free_content_origin(value: str) -> str | None:
            explicit = next(
                (
                    region[region_fields["identity"]]
                    for region in regions
                    if region[region_fields["action"]] == actions["freeEditable"]
                    and region[region_fields["selectedText"]] == value
                ),
                None,
            )
            return explicit if explicit is not None else inferred_text_region([value])

        user_visible_texts = [
            content[rules["formalProjection"]["topLevel"]["userPromptTemplate"]],
            *content["freeEditableContent"],
            *[
                value
                for slot in slots
                for value in [slot["defaultValue"], *slot["suggestions"]]
            ],
        ]
        fixed_region_leaks = [
            region[region_fields["identity"]]
            for region in regions
            if region[region_fields["action"]]
            not in {actions["openSlot"], actions["freeEditable"]}
            and not (
                region[region_fields["valueClass"]] == identity_value_class
                and not subject_open
            )
            and region_matches_values(region, user_visible_texts)
        ]

        result["evidence"][roles["visibleTextClassification"]["evidence"]] = {
            audit_fields["reviewedRegionIdentities"]: [
                region[region_fields["identity"]] for region in regions
            ],
            audit_fields["decisions"]: [
                {
                    region_fields["identity"]: region[region_fields["identity"]],
                    region_fields["role"]: region[region_fields["role"]],
                    region_fields["action"]: region[region_fields["action"]],
                    region_fields["valueClass"]: region[region_fields["valueClass"]],
                    decision_fields["observedLanguage"]: _fixture_observed_language(
                        region[region_fields["sourceText"]],
                        region[region_fields["exactTextEvidence"]][
                            text_contract["exactEvidenceFields"]["language"]
                        ],
                        text_contract,
                    ),
                    decision_fields["observedTokens"]: copy.deepcopy(
                        region[region_fields["exactTextEvidence"]][
                            text_contract["exactEvidenceFields"]["tokens"]
                        ]
                    ),
                    decision_fields["identityNeutral"]: region_identity_is_neutral(region),
                    audit_fields["explanation"]: "逐区复核文字角色、编辑价值和处理操作",
                }
                for region in regions
            ],
            audit_fields["slotOrigins"]: [
                {
                    slot_origin_fields["slotIdentity"]: slot["id"],
                    slot_origin_fields["originRegionIdentity"]: slot_origin_region(slot),
                    slot_origin_fields["explanation"]: "独立判断槽值是否取自某个可见文字区域",
                }
                for slot in slots
            ],
            audit_fields["freeContentOrigins"]: [
                {
                    free_origin_fields["content"]: value,
                    free_origin_fields["originRegionIdentity"]: free_content_origin(value),
                    free_origin_fields["explanation"]: "独立判断自由编辑内容是否取自某个可见文字区域",
                }
                for value in content["freeEditableContent"]
            ],
            audit_fields["fixedRegionLeaks"]: fixed_region_leaks,
            audit_fields["complete"]: True,
            audit_fields["explanation"]: "独立复核全部可见文字区域的角色、价值类别与处理决定",
        }
        result["observedContentSha256"] = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        identity_audit = roles["identityNeutrality"]
        identity_fields = rules["identityReplacementContract"]["neutralityAuditFields"]
        identity_regions = [
            region
            for region in regions
            if region[region_fields["valueClass"]] == identity_value_class
        ]
        neutrality_applicable = bool(subject_open and identity_regions)
        identity_specific = bool(
            neutrality_applicable
            and any(
                not region_identity_is_neutral(region)
                for region in identity_regions
            )
        )
        result["checks"][identity_audit["check"]] = not identity_specific
        result["evidence"][identity_audit["evidence"]] = {
            identity_fields["applicability"]: neutrality_applicable,
            identity_fields["specificIdentityDetected"]: identity_specific,
            identity_fields["explanation"]: "逐项复核开放主体旁的身份相关文字是否保持中性",
        }
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

        def inspect(generated_image: Path, review_request: dict[str, Any]) -> dict[str, Any]:
            result = original(generated_image, review_request)
            for key, value in overrides.items():
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key].update(value)
                else:
                    result[key] = value
            contract = _read_json(RULES_PATH)["visualReviewContract"]
            result["bindings"]["evidenceSha256"] = _visual_evidence_sha(result, contract)
            return result

        clone.inspect_generated = inspect  # type: ignore[method-assign]
        clone.generate_calls = []
        clone.upload_calls = []
        return clone

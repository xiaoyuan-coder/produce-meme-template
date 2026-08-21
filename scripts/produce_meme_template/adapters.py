from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import struct
import time
import unicodedata
import zlib
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    load_json_object as _read_json,
)
from .validation import is_public_ip_address, is_safe_public_https_url


RULES_PATH = Path(__file__).resolve().parents[2] / "contracts" / "machine-rules.json"


class _NoAutomaticRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


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


def _ppm_fixture_as_png(payload: bytes) -> bytes:
    lines = [line.split(b"#", 1)[0] for line in payload.splitlines()]
    tokens = b" ".join(lines).split()
    if len(tokens) < 4 or tokens[0] != b"P3":
        raise ValueError("deterministic fixture must be an ASCII PPM image")
    width, height, maximum = (int(value) for value in tokens[1:4])
    samples = [int(value) for value in tokens[4:]]
    if width < 1 or height < 1 or maximum < 1 or len(samples) != width * height * 3:
        raise ValueError("deterministic PPM fixture dimensions are invalid")
    pixels = bytes(round(sample * 255 / maximum) for sample in samples)
    scanlines = b"".join(
        b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3]
        for row in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


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
        self.approved_image_path_override: Path | None = None
        self.generate_calls: list[dict[str, Any]] = []
        self.submission_calls: list[dict[str, Any]] = []
        self.poll_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self.authoring_handoffs: list[dict[str, Any]] = []

    def analyze_source(
        self, source_image: Path, replacement_strategy: dict[str, Any] | None
    ) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "source-analysis.json")
        result["schemaVersion"] = _read_json(RULES_PATH)["schemaVersion"]
        result["sourceImageSha256"] = hashlib.sha256(source_image.read_bytes()).hexdigest()
        rules = _read_json(RULES_PATH)
        context_contract = rules["sourceAuthoringContextContract"]
        cultural_field = context_contract["culturalReferenceField"]
        continuity_field = context_contract["subjectContinuityField"]
        target = result["target"]
        known_ip = target["category"] == rules["sourceCategories"][
            "knownCharacterIp"
        ]
        if cultural_field not in result:
            result[cultural_field] = {
                "assessed": True,
                "status": "identified" if known_ip else "not_detected",
                "checkedSignals": [
                    "角色造型与配色",
                    "服装、道具与身份符号",
                    "画面中可辨识的系列元素",
                ],
                "references": (
                    [
                        {
                            "name": target["identity"],
                            "type": "known_character_ip",
                            "role": target["role"],
                            "evidence": "fixture 已将主要目标确认为具名角色 IP",
                        }
                    ]
                    if known_ip
                    else []
                ),
                "candidates": [],
                "evidence": (
                    "已核对主体设计、标志性服装道具与系列符号"
                ),
            }
        if continuity_field not in result:
            mechanism = result["mechanism"]
            result[continuity_field] = {
                "subjectCount": 1,
                "speciesOrType": target["category"],
                "genderPresentation": "当前画面未显示可靠性别线索",
                "apparentAge": "保持当前主体的年龄阶段",
                "outfitRole": "保持服装在当前玩法中的角色",
                "contrastMechanism": mechanism["payoff"],
                "preserveTraits": [
                    mechanism["setup"],
                    mechanism["turn"],
                    mechanism["payoff"],
                ],
                "evidence": "fixture 从来源机制中冻结主体连续性下界",
            }
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
        image_path = self._fixture_approved_image_path()
        return {
            "requestId": generation_package["requestId"],
            **self._fixture_image_result(image_path),
        }

    def _fixture_approved_image_path(self) -> Path:
        if self.approved_image_path_override is not None:
            return self.approved_image_path_override
        candidates = [
            path
            for path in self.fixture_dir.glob("approved-template-image.*")
            if path.is_file() and not path.is_symlink()
        ]
        if len(candidates) != 1:
            raise ValueError("fixture must contain exactly one approved template image")
        return candidates[0]

    @staticmethod
    def _fixture_image_result(image_path: Path) -> dict[str, Any]:
        payload = image_path.read_bytes()
        if image_path.suffix.lower() == ".ppm":
            return {"extension": ".png", "imageBytes": _ppm_fixture_as_png(payload)}
        return {"extension": image_path.suffix.lower(), "imageBytes": payload}

    def submit_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
    ) -> dict[str, Any]:
        contract = _read_json(RULES_PATH)["generationExecutionContract"]
        task_fields = contract["taskFields"]
        fields = contract["submissionFields"]
        task_id = generation_task[task_fields["taskIdentity"]]
        self.submission_calls.append(
            {
                "sourceImage": str(source_image),
                "taskId": task_id,
                "requestId": generation_package["requestId"],
            }
        )
        return {
            fields["status"]: contract["submissionStatuses"]["submitted"],
            fields["provider"]: contract["providerRoles"]["deterministicFixture"],
            fields["model"]: "fixture-image-model",
            fields["providerRequestIdentity"]: f"fixture-request-{task_id}",
            fields["failureClass"]: None,
            fields["failureReason"]: None,
        }

    def poll_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
        submission: dict[str, Any],
    ) -> dict[str, Any]:
        rules = _read_json(RULES_PATH)
        contract = rules["generationExecutionContract"]
        task_fields = contract["taskFields"]
        intent_fields = contract["requestIntentFields"]
        submission_fields = contract["submissionFields"]
        result_fields = contract["pollResultFields"]
        asset_fields = contract["outputAssetFields"]
        self.poll_calls.append(
            {
                "taskId": generation_task[task_fields["taskIdentity"]],
                "providerRequestId": submission[
                    submission_fields["providerRequestIdentity"]
                ],
            }
        )
        generated = self.generate(source_image, generation_package)
        image_sha = hashlib.sha256(generated["imageBytes"]).hexdigest()
        image_count = generation_task[task_fields["requestIntent"]][
            intent_fields["imageCount"]
        ]
        output_assets = [
            {
                asset_fields["providerOutputIdentity"]: f"fixture-output-{index}",
                asset_fields["sha256"]: image_sha,
            }
            for index in range(image_count)
        ]
        primary_index = generation_task[task_fields["requestIntent"]][
            intent_fields["primaryOutputIndex"]
        ]
        return {
            result_fields["status"]: contract["pollStatuses"]["succeeded"],
            result_fields["failureClass"]: None,
            result_fields["failureReason"]: None,
            result_fields["extension"]: generated["extension"],
            result_fields["imageBytes"]: generated["imageBytes"],
            result_fields["outputAssets"]: output_assets,
            result_fields["providerOutputIdentity"]: output_assets[primary_index][
                asset_fields["providerOutputIdentity"]
            ],
        }

    def fetch_template_image(self, _url: str) -> bytes:
        image_path = self._fixture_approved_image_path()
        return self._fixture_image_result(image_path)["imageBytes"]

    def inspect_template_test(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        fields = _read_json(RULES_PATH)["templateTestContract"][
            "reviewFields"
        ]
        return {
            fields["templateJsonSha256"]: review_request[
                fields["templateJsonSha256"]
            ],
            fields["testCaseSha256"]: review_request[
                fields["testCaseSha256"]
            ],
            fields["generatedImageSha256"]: hashlib.sha256(
                generated_image.read_bytes()
            ).hexdigest(),
            fields["pass"]: True,
            fields["visibleDeviations"]: [],
            fields["explanation"]: "fixture review found no visible deviation",
        }

    def inspect_generated(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "visual-review.json")
        result["schemaVersion"] = _read_json(RULES_PATH)["schemaVersion"]
        contract = _read_json(RULES_PATH)["visualReviewContract"]
        result["bindings"] = {
            **review_request["bindings"],
            "generatedImageSha256": hashlib.sha256(generated_image.read_bytes()).hexdigest(),
            "evidenceSha256": _visual_evidence_sha(result, contract),
        }
        return result

    def analyze_approved(self, approved_image: Path) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "approved-analysis.json")
        result["schemaVersion"] = _read_json(RULES_PATH)["schemaVersion"]
        omission = result.get("subjectSlotOmissionEvidence")
        if isinstance(omission, dict) and "reason" in omission:
            evidence = omission.pop("reason")
            omission.update(
                {
                    "uploadReplacementFeasible": False,
                    "blockerCode": "inseparable_multi_identity_unit",
                    "evidence": evidence,
                }
            )
        image_sha = hashlib.sha256(approved_image.read_bytes()).hexdigest()
        result["visualFactSourceSha256"] = image_sha
        decision_contract = _read_json(RULES_PATH).get(
            "renderingCoherenceDecisionContract", {}
        )
        decision = result.get(decision_contract.get("authoringField"))
        if isinstance(decision, dict):
            approved_image_field = decision_contract.get("fields", {}).get(
                "approvedImageSha256"
            )
            if approved_image_field:
                decision[approved_image_field] = image_sha
        return result

    def analyze_approved_with_handoff(
        self, approved_image: Path, authoring_handoff: dict[str, Any]
    ) -> dict[str, Any]:
        self.authoring_handoffs.append(copy.deepcopy(authoring_handoff))
        return self.analyze_approved(approved_image)

    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]:
        result = _read_json(self.fixture_dir / "semantic-audit.json")
        result["schemaVersion"] = _read_json(RULES_PATH)["schemaVersion"]
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
        suggestion_contract = rules["slotSuggestionReviewContract"]
        slot_review_fields = suggestion_contract["slotReviewFields"]
        suggestion_review_fields = suggestion_contract["suggestionReviewFields"]
        authored_reviews = result["evidence"].get(
            roles["slotSuggestions"]["evidence"], []
        )
        authored_review_by_slot = {
            review.get(slot_review_fields["slotIdentity"]): review
            for review in authored_reviews
            if isinstance(review, dict)
            and isinstance(review.get(slot_review_fields["slotIdentity"]), str)
        }

        def authored_nonempty_text(
            authored: dict[str, Any], role: str, fallback: str
        ) -> str:
            value = authored.get(slot_review_fields[role])
            return value if isinstance(value, str) and value.strip() else fallback

        def suggestion_review(
            slot: dict[str, Any],
            suggestion: str,
            authored_slot_review: dict[str, Any],
        ) -> dict[str, Any]:
            authored_items = authored_slot_review.get(
                slot_review_fields["suggestionReviews"], []
            )
            authored_item = next(
                (
                    item
                    for item in authored_items
                    if isinstance(item, dict)
                    and item.get(suggestion_review_fields["value"])
                    == suggestion
                ),
                {},
            )
            fallback_evidence = (
                f"{suggestion} 与 {slot['defaultValue']} 属于同一"
                f"{slot['label']}编辑轴并保持当前模板机制"
            )
            return {
                suggestion_review_fields["value"]: suggestion,
                suggestion_review_fields["sameAxis"]: authored_item.get(
                    suggestion_review_fields["sameAxis"], True
                ),
                suggestion_review_fields["sameGranularity"]: authored_item.get(
                    suggestion_review_fields["sameGranularity"], True
                ),
                suggestion_review_fields["mechanismCompatible"]: authored_item.get(
                    suggestion_review_fields["mechanismCompatible"], True
                ),
                suggestion_review_fields["evidence"]: (
                    authored_item.get(suggestion_review_fields["evidence"])
                    if isinstance(
                        authored_item.get(suggestion_review_fields["evidence"]),
                        str,
                    )
                    and authored_item[suggestion_review_fields["evidence"]].strip()
                    else fallback_evidence
                ),
            }

        result["evidence"][roles["slotSuggestions"]["evidence"]] = [
            {
                slot_review_fields["slotIdentity"]: slot["id"],
                slot_review_fields["defaultValue"]: slot["defaultValue"],
                slot_review_fields["axis"]: authored_nonempty_text(
                    authored_review_by_slot.get(slot["id"], {}),
                    "axis",
                    slot["label"],
                ),
                slot_review_fields["granularity"]: authored_nonempty_text(
                    authored_review_by_slot.get(slot["id"], {}),
                    "granularity",
                    f"单个{slot['label']}替换值",
                ),
                slot_review_fields["suggestionReviews"]: [
                    suggestion_review(
                        slot,
                        suggestion,
                        authored_review_by_slot.get(slot["id"], {}),
                    )
                    for suggestion in slot["suggestions"]
                ],
                slot_review_fields["evidence"]: authored_nonempty_text(
                    authored_review_by_slot.get(slot["id"], {}),
                    "evidence",
                    f"逐项比较 {slot['id']} 的默认值与全部推荐值",
                ),
            }
            for slot in slots
        ]
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
        content_sha = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result["observedContentSha256"] = content_sha
        identity_audit = roles["identityNeutrality"]
        identity_fields = rules["identityReplacementContract"]["neutralityAuditFields"]
        identity_regions = [
            region
            for region in regions
            if region[region_fields["valueClass"]] == identity_value_class
        ]
        saved_identity_review = result["evidence"].get(
            identity_audit["evidence"]
        )
        if not identity_regions and isinstance(saved_identity_review, dict):
            neutrality_applicable = saved_identity_review.get(
                identity_fields["applicability"]
            )
            identity_specific = saved_identity_review.get(
                identity_fields["specificIdentityDetected"]
            )
        else:
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

    def audit_visual_contract(
        self, approved_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        rules = _read_json(RULES_PATH)
        contract = rules["visualContractGroundingReviewContract"]
        fields = contract["reviewFields"]
        unit_review_fields = contract["renderingUnitReviewFields"]
        transfer_review_fields = contract["subjectTransferReviewFields"]
        decision_contract = rules["renderingCoherenceDecisionContract"]
        decision_fields = decision_contract["fields"]
        unit_fields = decision_contract["renderingUnitFields"]
        transfer_fields = decision_contract["subjectTransferFields"]
        decision = review_request["renderingCoherenceDecision"]
        return {
            fields["approvedImageSha256"]: hashlib.sha256(
                approved_image.read_bytes()
            ).hexdigest(),
            fields["visualContractSha256"]: hashlib.sha256(
                _canonical_bytes(review_request["visualContract"])
            ).hexdigest(),
            fields["renderingCoherenceDecisionSha256"]: hashlib.sha256(
                _canonical_bytes(decision)
            ).hexdigest(),
            fields["mediumMatchesApprovedImage"]: True,
            fields["compositionMatchesApprovedImage"]: True,
            fields["relationsMatchApprovedImage"]: True,
            fields["renderingUnitReviews"]: [
                {
                    unit_review_fields["identity"]: unit[unit_fields["identity"]],
                    unit_review_fields["componentIdentities"]: unit[
                        unit_fields["componentIdentities"]
                    ],
                    unit_review_fields["matchesApprovedImage"]: True,
                    unit_review_fields["evidence"]: (
                        f"逐组件核对 {unit[unit_fields['identity']]} 的媒介与画风"
                    ),
                }
                for unit in decision[decision_fields["renderingUnits"]]
            ],
            fields["subjectTransferReviews"]: [
                {
                    transfer_review_fields["inputIdentity"]: transfer[
                        transfer_fields["inputIdentity"]
                    ],
                    transfer_review_fields["targetIdentities"]: transfer[
                        transfer_fields["targetIdentities"]
                    ],
                    transfer_review_fields["completeRedraw"]: True,
                    transfer_review_fields["authorityMatches"]: True,
                    transfer_review_fields["evidence"]: (
                        "逐项核对上传继承范围、模板保留范围与完整重绘结果"
                    ),
                }
                for transfer in decision[decision_fields["subjectTransfers"]]
            ],
            fields["evidence"]: (
                "逐区域核对确认图媒介、固定组件绘制语言与 subject 完整转绘范围"
            ),
        }

    def upload(self, approved_image: Path, object_key: str) -> dict[str, Any]:
        image_sha = hashlib.sha256(approved_image.read_bytes()).hexdigest()
        call = {"approvedImage": str(approved_image), "objectKey": object_key, "imageSha256": image_sha}
        self.upload_calls.append(call)
        contract = _read_json(RULES_PATH)["objectStorageContract"]
        fields = contract["adapterResultFields"]
        return {
            fields["provider"]: contract["providerRoles"]["deterministicFixture"],
            fields["objectKey"]: object_key,
            fields["objectIdentity"]: "fixture-object-" + image_sha,
            fields["imageSha256"]: image_sha,
            fields["url"]: f"https://fixtures.memebuy.test/{object_key}",
            fields["idempotencyKey"]: contract["idempotencyKeyPrefix"] + image_sha,
            fields["uploadStatus"]: contract["uploadStatuses"]["uploaded"],
            fields["providerRequestIdentity"]: "fixture-upload-" + image_sha,
            fields["providerStatusCode"]: 200,
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
        clone.submission_calls = []
        clone.poll_calls = []
        clone.upload_calls = []
        return clone


class FalQueueWorkflowAdapters:
    """Use fal's durable queue for generation and delegate all other workflow adapters."""

    def __init__(
        self,
        delegate: Any,
        *,
        client: Any | None = None,
        download_bytes: Any | None = None,
        open_url: Any | None = None,
        resolve_host: Any | None = None,
        peer_address: Any | None = None,
        sleep: Any = time.sleep,
        poll_interval_seconds: float = 1.0,
        maximum_status_polls: int = 900,
    ) -> None:
        self.delegate = delegate
        self._client = client
        self._download_bytes = download_bytes or self._download
        self._open_url = open_url or build_opener(_NoAutomaticRedirect()).open
        self._resolve_host = resolve_host
        self._peer_address = peer_address or self._response_peer_address
        self._sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds
        self.maximum_status_polls = maximum_status_polls

    @property
    def generate_calls(self) -> list[dict[str, Any]]:
        return self.delegate.generate_calls

    @property
    def upload_calls(self) -> list[dict[str, Any]]:
        return self.delegate.upload_calls

    def _fal_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import fal_client
        except ImportError as exc:
            raise RuntimeError(
                "fal-client is required for real generation; install requirements.txt"
            ) from exc
        self._client = fal_client
        return self._client

    @staticmethod
    def _source_data_uri(source_image: Path) -> str:
        extension = source_image.suffix.lower()
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".ppm": "image/x-portable-pixmap",
        }
        mime_type = mime_types.get(extension)
        if mime_type is None:
            raise ValueError(f"unsupported fal source image extension: {extension}")
        encoded = base64.b64encode(source_image.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _image_size(value: str) -> str | dict[str, int]:
        if value == "auto":
            return value
        match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
        if match is None:
            raise ValueError("generation image size must be WIDTHxHEIGHT or auto")
        return {"width": int(match.group(1)), "height": int(match.group(2))}

    def _download(self, url: str) -> bytes:
        maximum_redirects = _read_json(RULES_PATH)["generationExecutionContract"][
            "fal"
        ]["maximumDownloadRedirects"]
        current_url = url
        for redirect_count in range(maximum_redirects + 1):
            validation_options = {"resolve_dns": True}
            if self._resolve_host is not None:
                validation_options["resolver"] = self._resolve_host
            if not is_safe_public_https_url(current_url, **validation_options):
                raise ValueError("fal image fetch target is not public HTTPS")
            try:
                response = self._open_url(Request(current_url, method="GET"), timeout=120)
            except HTTPError as exc:
                if not 300 <= exc.code < 400:
                    raise
                response = exc
            with response:
                if not is_public_ip_address(self._peer_address(response)):
                    raise ValueError("fal image connection peer is not public")
                status = getattr(response, "status", getattr(response, "code", 200))
                if 300 <= status < 400:
                    location = response.headers.get("Location")
                    if not location or redirect_count >= maximum_redirects:
                        raise ValueError("fal image redirect is missing or exceeds the limit")
                    current_url = urljoin(current_url, location)
                    continue
                final_url = response.geturl()
                if not is_safe_public_https_url(final_url, **validation_options):
                    raise ValueError("fal final image fetch target is not public HTTPS")
                payload = response.read()
            if not payload:
                raise ValueError("fal returned an empty image")
            return payload
        raise ValueError("fal image redirect exceeds the limit")

    @staticmethod
    def _response_peer_address(response: Any) -> str:
        current = response
        for path in (
            ("fp", "raw", "_sock"),
            ("fp", "fp", "raw", "_sock"),
            ("fp", "raw", "_connection", "sock"),
        ):
            current = response
            for field in path:
                current = getattr(current, field, None)
                if current is None:
                    break
            if current is not None and callable(getattr(current, "getpeername", None)):
                peer = current.getpeername()
                if isinstance(peer, tuple) and peer and isinstance(peer[0], str):
                    return peer[0]
        return ""

    @staticmethod
    def _exception_status(error: BaseException) -> int | None:
        for current in (error, getattr(error, "cause", None), getattr(error, "__cause__", None)):
            if current is None:
                continue
            for field in ("status", "status_code"):
                value = getattr(current, field, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
        return None

    @staticmethod
    def _failure_role(error: BaseException) -> str:
        status = FalQueueWorkflowAdapters._exception_status(error)
        detail = str(error).casefold()
        if any(term in detail for term in ("safety", "moderation", "content policy")):
            return "humanReview"
        if status == 429 or (isinstance(status, int) and status >= 500):
            return "retryable"
        if isinstance(error, (TimeoutError, ConnectionError)):
            return "retryable"
        if status in {400, 409, 422}:
            return "replanRequired"
        return "permanent"

    @staticmethod
    def _safe_error_detail(error: BaseException) -> str:
        detail = str(error)
        credential = os.environ.get("FAL_KEY", "")
        return detail.replace(credential, "[REDACTED]") if credential else detail

    @staticmethod
    def _status_completed(status: Any) -> bool:
        if type(status).__name__.casefold() == "completed":
            return True
        value = getattr(status, "status", None)
        if value is None and isinstance(status, dict):
            value = status.get("status")
        return isinstance(value, str) and value.upper() == "COMPLETED"

    def analyze_source(
        self, source_image: Path, replacement_strategy: dict[str, Any] | None
    ) -> dict[str, Any]:
        return self.delegate.analyze_source(source_image, replacement_strategy)

    def inspect_generated(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.delegate.inspect_generated(generated_image, review_request)

    def analyze_approved(self, approved_image: Path) -> dict[str, Any]:
        return self.delegate.analyze_approved(approved_image)

    def analyze_approved_with_handoff(
        self, approved_image: Path, authoring_handoff: dict[str, Any]
    ) -> dict[str, Any]:
        method = getattr(self.delegate, "analyze_approved_with_handoff", None)
        if callable(method):
            return method(approved_image, authoring_handoff)
        return self.delegate.analyze_approved(approved_image)

    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]:
        return self.delegate.audit_semantics(content)

    def audit_visual_contract(
        self, approved_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.delegate.audit_visual_contract(approved_image, review_request)

    def fetch_template_image(self, url: str) -> bytes:
        return self._download_bytes(url)

    def inspect_template_test(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.delegate.inspect_template_test(
            generated_image, review_request
        )

    def upload(self, approved_image: Path, object_key: str) -> dict[str, Any]:
        return self.delegate.upload(approved_image, object_key)

    def submit_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
    ) -> dict[str, Any]:
        rules = _read_json(RULES_PATH)
        contract = rules["generationExecutionContract"]
        task_fields = contract["taskFields"]
        intent_fields = contract["requestIntentFields"]
        submission_fields = contract["submissionFields"]
        intent = generation_task[task_fields["requestIntent"]]
        model = contract["fal"]["model"]
        arguments = {
            "prompt": intent[intent_fields["prompt"]],
            "image_urls": [self._source_data_uri(source_image)],
            "image_size": self._image_size(intent[intent_fields["imageSize"]]),
            "quality": contract["fal"]["quality"],
            "num_images": intent[intent_fields["imageCount"]],
            "output_format": intent[intent_fields["outputFormat"]],
        }
        try:
            handle = self._fal_client().submit(model, arguments=arguments)
            provider_request_id = str(getattr(handle, "request_id", "")).strip()
            if not provider_request_id:
                raise ValueError("fal submit did not return request_id")
        except Exception as exc:
            failure_role = self._failure_role(exc)
            if failure_role == "retryable":
                failure_role = "submissionUnknown"
            return {
                submission_fields["status"]: contract["submissionStatuses"]["failed"],
                submission_fields["provider"]: contract["providerRoles"]["fal"],
                submission_fields["model"]: model,
                submission_fields["providerRequestIdentity"]: None,
                submission_fields["failureClass"]: contract["failureClasses"][
                    failure_role
                ],
                submission_fields["failureReason"]: self._safe_error_detail(exc),
            }
        return {
            submission_fields["status"]: contract["submissionStatuses"]["submitted"],
            submission_fields["provider"]: contract["providerRoles"]["fal"],
            submission_fields["model"]: model,
            submission_fields["providerRequestIdentity"]: provider_request_id,
            submission_fields["failureClass"]: None,
            submission_fields["failureReason"]: None,
        }

    def poll_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
        submission: dict[str, Any],
    ) -> dict[str, Any]:
        rules = _read_json(RULES_PATH)
        contract = rules["generationExecutionContract"]
        submission_fields = contract["submissionFields"]
        result_fields = contract["pollResultFields"]
        task_fields = contract["taskFields"]
        intent_fields = contract["requestIntentFields"]
        asset_fields = contract["outputAssetFields"]
        model = submission[submission_fields["model"]]
        provider_request_id = submission[submission_fields["providerRequestIdentity"]]
        try:
            for attempt in range(self.maximum_status_polls):
                status = self._fal_client().status(model, provider_request_id)
                if self._status_completed(status):
                    break
                if attempt == self.maximum_status_polls - 1:
                    raise TimeoutError("fal request did not complete within the polling budget")
                self._sleep(self.poll_interval_seconds)
            response = self._fal_client().result(model, provider_request_id)
            payload = response.get("data", response) if isinstance(response, dict) else None
            images = payload.get("images") if isinstance(payload, dict) else None
            expected_count = generation_task[task_fields["requestIntent"]][
                intent_fields["imageCount"]
            ]
            if not isinstance(images, list) or len(images) != expected_count:
                raise ValueError("fal result image count does not match the frozen task")
            downloaded: list[tuple[str, bytes]] = []
            for image in images:
                url = image.get("url") if isinstance(image, dict) else None
                if not is_safe_public_https_url(url):
                    raise ValueError("fal result image URL is not a public HTTPS target")
                downloaded.append((url, self._download_bytes(url)))
            primary_index = generation_task[task_fields["requestIntent"]][
                intent_fields["primaryOutputIndex"]
            ]
            _primary_url, primary_bytes = downloaded[primary_index]
            output_identities = [
                "fal-output-" + hashlib.sha256(url.encode("utf-8")).hexdigest()
                for url, _payload in downloaded
            ]
            output_assets = [
                {
                    asset_fields["providerOutputIdentity"]: output_identity,
                    asset_fields["sha256"]: hashlib.sha256(payload).hexdigest(),
                }
                for output_identity, (_url, payload) in zip(
                    output_identities, downloaded, strict=True
                )
            ]
            output_format = generation_task[task_fields["requestIntent"]][
                intent_fields["outputFormat"]
            ]
            output_format_role = next(
                role
                for role, value in contract["outputFormats"].items()
                if value == output_format
            )
            extension = contract["outputFormatExtensions"][output_format_role]
            return {
                result_fields["status"]: contract["pollStatuses"]["succeeded"],
                result_fields["failureClass"]: None,
                result_fields["failureReason"]: None,
                result_fields["extension"]: extension,
                result_fields["imageBytes"]: primary_bytes,
                result_fields["outputAssets"]: output_assets,
                result_fields["providerOutputIdentity"]: output_identities[primary_index],
            }
        except Exception as exc:
            failure_role = self._failure_role(exc)
            return {
                result_fields["status"]: contract["pollStatuses"]["failed"],
                result_fields["failureClass"]: contract["failureClasses"][failure_role],
                result_fields["failureReason"]: self._safe_error_detail(exc),
                result_fields["extension"]: None,
                result_fields["imageBytes"]: None,
                result_fields["outputAssets"]: [],
                result_fields["providerOutputIdentity"]: None,
            }


class AliyunOssWorkflowAdapters:
    """Store the Approved Template Image in Aliyun OSS and delegate other seams."""

    def __init__(
        self,
        delegate: Any,
        *,
        public_base_url: str,
        bucket: Any | None = None,
        endpoint: str | None = None,
        bucket_name: str | None = None,
        resolve_host: Any | None = None,
    ) -> None:
        validation_options = {"resolve_dns": True}
        if resolve_host is not None:
            validation_options["resolver"] = resolve_host
        if not is_safe_public_https_url(public_base_url, **validation_options):
            raise ValueError("OSS public base URL must be public HTTPS")
        self.delegate = delegate
        self.public_base_url = public_base_url.rstrip("/")
        self._bucket = bucket or self._create_bucket(endpoint, bucket_name)
        self.upload_calls: list[dict[str, Any]] = []

    @staticmethod
    def _create_bucket(endpoint: str | None, bucket_name: str | None) -> Any:
        endpoint = endpoint or os.environ.get("OSS_ENDPOINT")
        bucket_name = bucket_name or os.environ.get("OSS_BUCKET_NAME")
        access_key_id = os.environ.get("OSS_ACCESS_KEY_ID")
        access_key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET")
        if not all((endpoint, bucket_name, access_key_id, access_key_secret)):
            raise RuntimeError(
                "Aliyun OSS requires endpoint, bucket name and access-key environment variables"
            )
        try:
            import oss2
        except ImportError as exc:
            raise RuntimeError(
                "oss2 is required for real OSS upload; install requirements.txt"
            ) from exc
        return oss2.Bucket(
            oss2.Auth(access_key_id, access_key_secret), endpoint, bucket_name
        )

    @property
    def generate_calls(self) -> list[dict[str, Any]]:
        return self.delegate.generate_calls

    def analyze_source(
        self, source_image: Path, replacement_strategy: dict[str, Any] | None
    ) -> dict[str, Any]:
        return self.delegate.analyze_source(source_image, replacement_strategy)

    def submit_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
    ) -> dict[str, Any]:
        return self.delegate.submit_generation(
            source_image, generation_package, generation_task
        )

    def poll_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
        submission: dict[str, Any],
    ) -> dict[str, Any]:
        return self.delegate.poll_generation(
            source_image, generation_package, generation_task, submission
        )

    def inspect_generated(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.delegate.inspect_generated(generated_image, review_request)

    def fetch_template_image(self, url: str) -> bytes:
        return self.delegate.fetch_template_image(url)

    def inspect_template_test(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.delegate.inspect_template_test(
            generated_image, review_request
        )

    def analyze_approved(self, approved_image: Path) -> dict[str, Any]:
        return self.delegate.analyze_approved(approved_image)

    def analyze_approved_with_handoff(
        self, approved_image: Path, authoring_handoff: dict[str, Any]
    ) -> dict[str, Any]:
        method = getattr(self.delegate, "analyze_approved_with_handoff", None)
        if callable(method):
            return method(approved_image, authoring_handoff)
        return self.delegate.analyze_approved(approved_image)

    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]:
        return self.delegate.audit_semantics(content)

    def audit_visual_contract(
        self, approved_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.delegate.audit_visual_contract(approved_image, review_request)

    @staticmethod
    def _header_value(headers: Any, name: str) -> str | None:
        if not hasattr(headers, "items"):
            return None
        expected = name.casefold()
        for key, value in headers.items():
            if isinstance(key, str) and key.casefold() == expected:
                return str(value)
        return None

    def upload(self, approved_image: Path, object_key: str) -> dict[str, Any]:
        rules = _read_json(RULES_PATH)
        contract = rules["objectStorageContract"]
        fields = contract["adapterResultFields"]
        sha_header = contract["aliyun"]["sha256MetadataHeader"]
        body = approved_image.read_bytes()
        image_sha = hashlib.sha256(body).hexdigest()
        expected_object_identity = hashlib.new(
            contract["aliyun"]["objectIdentityAlgorithm"], body
        ).hexdigest()
        self.upload_calls.append(
            {
                "approvedImage": str(approved_image),
                "objectKey": object_key,
                "imageSha256": image_sha,
            }
        )
        if self._bucket.object_exists(object_key):
            metadata_result = self._bucket.head_object(object_key)
            request_result = metadata_result
            upload_status = contract["uploadStatuses"]["reused"]
        else:
            request_result = self._bucket.put_object(
                object_key,
                body,
                headers={
                    sha_header: image_sha,
                    contract["aliyun"]["forbidOverwriteHeader"]: contract[
                        "aliyun"
                    ]["forbidOverwriteValue"],
                },
            )
            metadata_result = self._bucket.head_object(object_key)
            upload_status = contract["uploadStatuses"]["uploaded"]
        remote_sha = self._header_value(
            getattr(metadata_result, "headers", None), sha_header
        )
        if (
            remote_sha != image_sha
            or getattr(metadata_result, "content_length", None) != len(body)
        ):
            raise ValueError("OSS object metadata does not match the approved image")
        object_identity = str(getattr(metadata_result, "etag", "")).strip().strip('"')
        request_identity = str(getattr(request_result, "request_id", "")).strip()
        status_code = getattr(request_result, "status", None)
        if (
            not object_identity
            or object_identity.casefold() != expected_object_identity.casefold()
            or not request_identity
            or not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not 200 <= status_code < 300
        ):
            raise ValueError("OSS response is missing stable object or request evidence")
        return {
            fields["provider"]: contract["providerRoles"]["aliyunOss"],
            fields["objectKey"]: object_key,
            fields["objectIdentity"]: object_identity,
            fields["imageSha256"]: image_sha,
            fields["url"]: self.public_base_url + "/" + quote(object_key, safe="/"),
            fields["idempotencyKey"]: contract["idempotencyKeyPrefix"] + image_sha,
            fields["uploadStatus"]: upload_status,
            fields["providerRequestIdentity"]: request_identity,
            fields["providerStatusCode"]: status_code,
        }

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


ROOT = Path(__file__).resolve().parents[1]
BASE_FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
TEXT_FIXTURE = ROOT / "fixtures" / "e2e" / "text-dense"
RULES = json.loads((ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8"))
SCENARIOS = json.loads((TEXT_FIXTURE / "scenarios.json").read_text(encoding="utf-8"))
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")
TEXT_CONTRACT = RULES["visibleTextContract"]
ANALYSIS_FIELDS = TEXT_CONTRACT["analysisFields"]
INVENTORY_FIELDS = TEXT_CONTRACT["inventoryFields"]
REGION_FIELDS = TEXT_CONTRACT["regionFields"]
EVIDENCE_FIELDS = TEXT_CONTRACT["exactEvidenceFields"]
TEXT_ROLES = TEXT_CONTRACT["roles"]
TEXT_ACTIONS = TEXT_CONTRACT["actions"]
VALUE_CLASSES = TEXT_CONTRACT["valueClasses"]
LANGUAGES = TEXT_CONTRACT["languageValues"]
TEXT_SLOT_TYPE = RULES["slotCompilationContract"]["slotTypes"]["visibleTextPrompt"]
IDENTITY_TEXT_ROLE = RULES["slotCompilationContract"]["semanticRoles"]["identityText"]


def canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class TextScenarioAdapters(DeterministicFixtureAdapters):
    def __init__(self, transform: Callable[[dict], dict]):
        super().__init__(BASE_FIXTURE)
        self.transform = transform

    def analyze_approved(self, approved_image: Path) -> dict:
        return self.transform(super().analyze_approved(approved_image))

    def audit_semantics(self, content: dict) -> dict:
        result = super().audit_semantics(content)
        digest = canonical_sha(content)
        result["contentSha256"] = digest
        result["observedContentSha256"] = digest
        return result


def text_evidence(scenario: dict) -> dict:
    return {
        EVIDENCE_FIELDS["language"]: scenario["language"],
        EVIDENCE_FIELDS["tokens"]: scenario["tokens"],
        EVIDENCE_FIELDS["lines"]: scenario["lines"],
        EVIDENCE_FIELDS["caseSensitiveTokens"]: scenario.get("caseSensitiveTokens", []),
        EVIDENCE_FIELDS["rareSymbols"]: scenario.get("rareSymbols", []),
        EVIDENCE_FIELDS["symbolTopology"]: scenario.get("symbolTopology", "保持原文字形与顺序"),
        EVIDENCE_FIELDS["explanation"]: "逐区域核对 Approved Template Image 的原语种和精确排版事实",
    }


class Issue8TextDenseTemplatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = json.loads((BASE_FIXTURE / "request.json").read_text(encoding="utf-8"))
        self.request["sourceImage"] = str(BASE_FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_case(self, item_id: str, transform: Callable[[dict], dict]):
        adapters = TextScenarioAdapters(transform)
        result = run_production(
            {**self.request, "productionItemId": item_id},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )
        return result, adapters

    @staticmethod
    def text_card(analysis: dict) -> dict:
        scenario = SCENARIOS["textCard"]
        slot = analysis["slotCandidates"][1]
        slot.update(
            {
                "id": "headline_text",
                "type": TEXT_SLOT_TYPE,
                "semanticRole": "primary_visual_text",
                "label": "画面主文字",
                "placeholder": "输入主文字",
                "defaultValue": scenario["sourceText"],
                "suggestions": ["今天先放空\n(－_－) zzZ", "周末慢一点\n(－_－) zzZ"],
                "hiddenConflictTokens": ["画面主文字", "文字内容", "文字排版"],
                "titleForbiddenTokens": [
                    scenario["sourceText"], "今天先放空", "周末慢一点"
                ],
                TEXT_CONTRACT["slotBindingField"]: scenario["regionId"],
                "exactVisibleText": True,
                "exactVisibleTextEvidence": {
                    "approvedImageSha256": analysis["visualFactSourceSha256"],
                    "visibleText": scenario["sourceText"],
                    "evidence": "逐字核对主要文字卡",
                },
            }
        )
        analysis["promptTemplate"] = (
            f'一只{{{{ pet_subject | "柯基犬" }}}}蜷卧在软垫上，前爪搭住垫边，'
            f'画面上方文字卡写着{{{{ headline_text | "{scenario["sourceText"]}" }}}}，'
            '{{ room_mood | "午后窗光" }}从侧面照入安静的客厅，背景带轻微景深。'
        )
        analysis["defaultValuePreferenceExceptionEvidence"] = {
            "headline_text": {"reviewed": True, "reason": "精确画内文字需要保留换行与颜文字"}
        }
        analysis[ANALYSIS_FIELDS["regions"]] = [
            {
                REGION_FIELDS["identity"]: scenario["regionId"],
                REGION_FIELDS["sourceText"]: scenario["sourceText"],
                REGION_FIELDS["role"]: TEXT_ROLES["content"],
                REGION_FIELDS["valueClass"]: VALUE_CLASSES["primaryVisual"],
                REGION_FIELDS["action"]: TEXT_ACTIONS["openSlot"],
                REGION_FIELDS["slotIdentity"]: "headline_text",
                REGION_FIELDS["selectedText"]: scenario["sourceText"],
                REGION_FIELDS["exactTextEvidence"]: text_evidence(scenario),
            }
        ]
        analysis[ANALYSIS_FIELDS["inventory"]] = {
            INVENTORY_FIELDS["complete"]: True,
            INVENTORY_FIELDS["regionIdentities"]: [scenario["regionId"]],
            INVENTORY_FIELDS["explanation"]: "全画布逐区清点后仅有一组主要视觉文字",
        }
        return analysis

    @staticmethod
    def rewrite_text_card(
        analysis: dict,
        *,
        source_text: str,
        language: str,
        tokens: list[str],
        case_sensitive_tokens: list[str],
        rare_symbols: list[str],
    ) -> dict:
        analysis = Issue8TextDenseTemplatesTest.text_card(analysis)
        original_text = SCENARIOS["textCard"]["sourceText"]
        slot = analysis["slotCandidates"][1]
        slot["defaultValue"] = source_text
        slot["exactVisibleTextEvidence"]["visibleText"] = source_text
        analysis["promptTemplate"] = analysis["promptTemplate"].replace(
            original_text, source_text
        )
        region = analysis[ANALYSIS_FIELDS["regions"]][0]
        region[REGION_FIELDS["sourceText"]] = source_text
        region[REGION_FIELDS["selectedText"]] = source_text
        evidence = region[REGION_FIELDS["exactTextEvidence"]]
        evidence[EVIDENCE_FIELDS["language"]] = language
        evidence[EVIDENCE_FIELDS["tokens"]] = tokens
        evidence[EVIDENCE_FIELDS["lines"]] = source_text.splitlines()
        evidence[EVIDENCE_FIELDS["caseSensitiveTokens"]] = case_sensitive_tokens
        evidence[EVIDENCE_FIELDS["rareSymbols"]] = rare_symbols
        return analysis

    @staticmethod
    def add_watermark_to_approved_analysis(analysis: dict) -> dict:
        region = {
            REGION_FIELDS["identity"]: "poster-watermark-region",
            REGION_FIELDS["sourceText"]: "@sample_account",
            REGION_FIELDS["role"]: TEXT_ROLES["watermark"],
            REGION_FIELDS["valueClass"]: VALUE_CLASSES["lowValueInformation"],
            REGION_FIELDS["action"]: TEXT_ACTIONS["remove"],
            REGION_FIELDS["selectedText"]: "",
            REGION_FIELDS["exactTextEvidence"]: {
                EVIDENCE_FIELDS["language"]: LANGUAGES["english"],
                EVIDENCE_FIELDS["tokens"]: ["@sample_account"],
                EVIDENCE_FIELDS["lines"]: ["@sample_account"],
                EVIDENCE_FIELDS["caseSensitiveTokens"]: ["@sample_account"],
                EVIDENCE_FIELDS["rareSymbols"]: ["@", "_"],
                EVIDENCE_FIELDS["symbolTopology"]: "账号水印",
                EVIDENCE_FIELDS["explanation"]: "确认模板图仍可见账号水印",
            },
        }
        analysis[ANALYSIS_FIELDS["regions"]].append(region)
        analysis[ANALYSIS_FIELDS["inventory"]][INVENTORY_FIELDS["regionIdentities"]].append(
            region[REGION_FIELDS["identity"]]
        )
        return analysis

    @staticmethod
    def long_poster(analysis: dict) -> dict:
        scenario = SCENARIOS["longPoster"]
        slot = analysis["slotCandidates"][1]
        slot.update(
            {
                "id": "poster_keyword",
                "type": TEXT_SLOT_TYPE,
                "semanticRole": "high_value_text_span",
                "label": "海报关键词",
                "placeholder": "输入关键词或短语",
                "defaultValue": scenario["selectedSpan"],
                "suggestions": ["立刻行动", "先做重点", "拒绝拖延"],
                "hiddenConflictTokens": ["海报关键词", "海报文字内容", "关键词排版"],
                "titleForbiddenTokens": [
                    scenario["selectedSpan"], "立刻行动", "先做重点", "拒绝拖延"
                ],
                TEXT_CONTRACT["slotBindingField"]: scenario["longRegionId"],
                "exactVisibleText": True,
                "exactVisibleTextEvidence": {
                    "approvedImageSha256": analysis["visualFactSourceSha256"],
                    "visibleText": scenario["selectedSpan"],
                    "evidence": "高价值短语逐字出现在海报长文区域",
                },
            }
        )
        analysis["promptTemplate"] = (
            f'一只{{{{ pet_subject | "柯基犬" }}}}蜷卧在海报前景软垫上，前爪搭住垫边，'
            f'海报正文突出短语{{{{ poster_keyword | "{scenario["selectedSpan"]}" }}}}，'
            f'次要文字写着“{scenario["secondaryText"]}”，'
            '{{ room_mood | "午后窗光" }}照入安静的客厅，背景带轻微景深。'
        )
        analysis["freeEditableContent"].append(scenario["secondaryText"])
        analysis[ANALYSIS_FIELDS["regions"]] = [
            {
                REGION_FIELDS["identity"]: scenario["longRegionId"],
                REGION_FIELDS["sourceText"]: scenario["longText"],
                REGION_FIELDS["role"]: TEXT_ROLES["content"],
                REGION_FIELDS["valueClass"]: VALUE_CLASSES["highValueSpan"],
                REGION_FIELDS["action"]: TEXT_ACTIONS["openSlot"],
                REGION_FIELDS["slotIdentity"]: "poster_keyword",
                REGION_FIELDS["selectedText"]: scenario["selectedSpan"],
                REGION_FIELDS["exactTextEvidence"]: {
                    EVIDENCE_FIELDS["language"]: LANGUAGES["simplifiedChinese"],
                    EVIDENCE_FIELDS["tokens"]: [
                        "周五之前交付终稿", "逾期需要重新排期", "并在群内同步所有修改记录",
                        "暂停内耗", "从现在开始完成最重要的一件事"
                    ],
                    EVIDENCE_FIELDS["lines"]: [scenario["longText"]],
                    EVIDENCE_FIELDS["caseSensitiveTokens"]: [],
                    EVIDENCE_FIELDS["rareSymbols"]: [],
                    EVIDENCE_FIELDS["symbolTopology"]: "中文段落中的关键词保持原位置",
                    EVIDENCE_FIELDS["explanation"]: "逐段核对完整海报文字并抽取独立高价值短语",
                },
            },
            {
                REGION_FIELDS["identity"]: scenario["secondaryRegionId"],
                REGION_FIELDS["sourceText"]: scenario["secondaryText"],
                REGION_FIELDS["role"]: TEXT_ROLES["content"],
                REGION_FIELDS["valueClass"]: VALUE_CLASSES["secondaryReadable"],
                REGION_FIELDS["action"]: TEXT_ACTIONS["freeEditable"],
                REGION_FIELDS["selectedText"]: scenario["secondaryText"],
                REGION_FIELDS["exactTextEvidence"]: {
                    EVIDENCE_FIELDS["language"]: LANGUAGES["simplifiedChinese"], EVIDENCE_FIELDS["tokens"]: [scenario["secondaryText"]],
                    EVIDENCE_FIELDS["lines"]: [scenario["secondaryText"]], EVIDENCE_FIELDS["caseSensitiveTokens"]: [],
                    EVIDENCE_FIELDS["rareSymbols"]: [], EVIDENCE_FIELDS["symbolTopology"]: "单行次要说明",
                    EVIDENCE_FIELDS["explanation"]: "次要可读文字具有全文编辑价值"
                },
            },
            {
                REGION_FIELDS["identity"]: "poster-attribution-region",
                REGION_FIELDS["sourceText"]: scenario["attributionText"],
                REGION_FIELDS["role"]: TEXT_ROLES["attribution"],
                REGION_FIELDS["valueClass"]: VALUE_CLASSES["lowValueInformation"],
                REGION_FIELDS["action"]: TEXT_ACTIONS["preserve"],
                REGION_FIELDS["selectedText"]: scenario["attributionText"],
                REGION_FIELDS["exactTextEvidence"]: {
                    EVIDENCE_FIELDS["language"]: LANGUAGES["mixed"], EVIDENCE_FIELDS["tokens"]: ["摄影", "MOMO", "STUDIO"],
                    EVIDENCE_FIELDS["lines"]: [scenario["attributionText"]], EVIDENCE_FIELDS["caseSensitiveTokens"]: ["MOMO", "STUDIO"],
                    EVIDENCE_FIELDS["rareSymbols"]: [], EVIDENCE_FIELDS["symbolTopology"]: "中文归因前缀连接英文署名",
                    EVIDENCE_FIELDS["explanation"]: "出处信息可读但没有独立编辑价值"
                },
            },
            {
                REGION_FIELDS["identity"]: "poster-decoration-region",
                REGION_FIELDS["sourceText"]: scenario["decorativeText"],
                REGION_FIELDS["role"]: TEXT_ROLES["content"],
                REGION_FIELDS["valueClass"]: VALUE_CLASSES["decorativeMicrotext"],
                REGION_FIELDS["action"]: TEXT_ACTIONS["preserve"],
                REGION_FIELDS["selectedText"]: scenario["decorativeText"],
                REGION_FIELDS["exactTextEvidence"]: {
                    EVIDENCE_FIELDS["language"]: LANGUAGES["english"], EVIDENCE_FIELDS["tokens"]: ["EST.", "2026"],
                    EVIDENCE_FIELDS["lines"]: [scenario["decorativeText"]], EVIDENCE_FIELDS["caseSensitiveTokens"]: ["EST."],
                    EVIDENCE_FIELDS["rareSymbols"]: [], EVIDENCE_FIELDS["symbolTopology"]: "装饰性年份微字",
                    EVIDENCE_FIELDS["explanation"]: "装饰微字只维持版面完整性"
                },
            },
            {
                REGION_FIELDS["identity"]: "poster-brand-region",
                REGION_FIELDS["sourceText"]: scenario["brandText"],
                REGION_FIELDS["role"]: TEXT_ROLES["brand"],
                REGION_FIELDS["valueClass"]: VALUE_CLASSES["lowValueInformation"],
                REGION_FIELDS["action"]: TEXT_ACTIONS["preserve"],
                REGION_FIELDS["selectedText"]: scenario["brandText"],
                REGION_FIELDS["exactTextEvidence"]: {
                    EVIDENCE_FIELDS["language"]: LANGUAGES["english"], EVIDENCE_FIELDS["tokens"]: [scenario["brandText"]],
                    EVIDENCE_FIELDS["lines"]: [scenario["brandText"]], EVIDENCE_FIELDS["caseSensitiveTokens"]: [scenario["brandText"]],
                    EVIDENCE_FIELDS["rareSymbols"]: [], EVIDENCE_FIELDS["symbolTopology"]: "单行品牌文字",
                    EVIDENCE_FIELDS["explanation"]: "品牌文字已分类并固定，不占用户槽位"
                },
            },
        ]
        analysis[ANALYSIS_FIELDS["inventory"]] = {
            INVENTORY_FIELDS["complete"]: True,
            INVENTORY_FIELDS["regionIdentities"]: [region[REGION_FIELDS["identity"]] for region in analysis[ANALYSIS_FIELDS["regions"]]],
            INVENTORY_FIELDS["explanation"]: "全画布清点正文、次要说明、归因、装饰微字和品牌文字",
        }
        return analysis

    @staticmethod
    def identity_interface(analysis: dict) -> dict:
        scenario = SCENARIOS["identityInterface"]
        slot = analysis["slotCandidates"][1]
        slot.update(
            {
                "id": "identity_label",
                "type": TEXT_SLOT_TYPE,
                "semanticRole": IDENTITY_TEXT_ROLE,
                "label": "身份标题栏",
                "placeholder": "输入中性短标题",
                "defaultValue": scenario["sourceText"],
                "suggestions": ["PROFILE", "PLAYER", "YOUR NAME"],
                "hiddenConflictTokens": ["身份文字", "标题栏文字", "界面标签"],
                "titleForbiddenTokens": ["PORTRAIT", "PROFILE", "PLAYER", "YOUR NAME"],
                TEXT_CONTRACT["slotBindingField"]: scenario["regionId"],
                "exactVisibleText": True,
                "exactVisibleTextEvidence": {
                    "approvedImageSha256": analysis["visualFactSourceSha256"],
                    "visibleText": scenario["sourceText"],
                    "evidence": "界面标题栏逐字可见",
                },
            }
        )
        analysis["promptTemplate"] = (
            f'一只{{{{ pet_subject | "柯基犬" }}}}蜷卧在复古界面中央，前爪搭住垫边，'
            f'标题栏写着{{{{ identity_label | "{scenario["sourceText"]}" }}}}，'
            '{{ room_mood | "午后窗光" }}勾勒界面层次，背景保留安静的客厅和轻微景深。'
        )
        analysis["defaultValuePreferenceExceptionEvidence"] = {
            "identity_label": {"reviewed": True, "reason": "模板图使用中性英文界面标题"}
        }
        analysis[ANALYSIS_FIELDS["regions"]] = [
            {
                REGION_FIELDS["identity"]: scenario["regionId"], REGION_FIELDS["sourceText"]: scenario["sourceText"],
                REGION_FIELDS["role"]: TEXT_ROLES["content"], REGION_FIELDS["valueClass"]: VALUE_CLASSES["identityRelated"], REGION_FIELDS["action"]: TEXT_ACTIONS["openSlot"],
                REGION_FIELDS["slotIdentity"]: "identity_label", REGION_FIELDS["selectedText"]: scenario["sourceText"],
                REGION_FIELDS["exactTextEvidence"]: text_evidence(scenario),
            }
        ]
        analysis[ANALYSIS_FIELDS["inventory"]] = {
            INVENTORY_FIELDS["complete"]: True, INVENTORY_FIELDS["regionIdentities"]: [scenario["regionId"]],
            INVENTORY_FIELDS["explanation"]: "界面内唯一可见身份相关文字已分类",
        }
        return analysis

    def test_three_text_dense_scenarios_compile_through_the_public_workflow(self) -> None:
        for name, transform in {
            "text-card": self.text_card,
            "long-poster": self.long_poster,
            "identity-interface": self.identity_interface,
        }.items():
            with self.subTest(name=name):
                result, adapters = self.run_case(name, transform)
                self.assertEqual(RULES["resultStates"]["completed"], result.state)
                editable = json.loads((result.output_dir / "editable-template-spec.json").read_text())
                self.assertIn(ANALYSIS_FIELDS["regions"], editable)
                self.assertEqual(1, len(adapters.upload_calls))

    def test_unclassified_visible_text_is_blocked_before_upload(self) -> None:
        def missing_region(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            analysis[ANALYSIS_FIELDS["inventory"]][INVENTORY_FIELDS["regionIdentities"]].append("unclassified-region")
            return analysis

        result, adapters = self.run_case("unclassified-visible-text", missing_region)
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)

    def test_attribution_brand_and_decorative_microtext_cannot_consume_slots(self) -> None:
        for region_id in ("poster-attribution-region", "poster-brand-region", "poster-decoration-region"):
            with self.subTest(region_id=region_id):
                def low_value_slot(analysis: dict, region_id: str = region_id) -> dict:
                    analysis = self.long_poster(analysis)
                    region = next(item for item in analysis[ANALYSIS_FIELDS["regions"]] if item[REGION_FIELDS["identity"]] == region_id)
                    region.update({REGION_FIELDS["action"]: TEXT_ACTIONS["openSlot"], REGION_FIELDS["slotIdentity"]: "poster_keyword"})
                    analysis["slotCandidates"][1][TEXT_CONTRACT["slotBindingField"]] = region_id
                    return analysis

                result, adapters = self.run_case(f"low-value-slot-{region_id}", low_value_slot)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual([], adapters.upload_calls)

        def brand_as_free_editable(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            brand = next(
                region
                for region in analysis[ANALYSIS_FIELDS["regions"]]
                if region[REGION_FIELDS["identity"]] == "poster-brand-region"
            )
            brand[REGION_FIELDS["action"]] = TEXT_ACTIONS["freeEditable"]
            return analysis

        brand_result, brand_adapters = self.run_case(
            "brand-as-free-editable", brand_as_free_editable
        )
        self.assertEqual(RULES["resultStates"]["blocked"], brand_result.state)
        self.assertEqual([], brand_adapters.upload_calls)

    def test_brand_preserve_remove_and_review_routes_have_stable_outcomes(self) -> None:
        preserved, preserved_adapters = self.run_case("brand-preserved", self.long_poster)
        self.assertEqual(RULES["resultStates"]["completed"], preserved.state)
        self.assertEqual(1, len(preserved_adapters.upload_calls))

        def route_brand(analysis: dict, action: str) -> dict:
            analysis = self.long_poster(analysis)
            brand = next(
                region
                for region in analysis[ANALYSIS_FIELDS["regions"]]
                if region[REGION_FIELDS["identity"]] == "poster-brand-region"
            )
            brand[REGION_FIELDS["action"]] = action
            if action == TEXT_ACTIONS["remove"]:
                brand[REGION_FIELDS["selectedText"]] = ""
            return analysis

        removed, removed_adapters = self.run_case(
            "brand-remove-requires-image-revision",
            lambda analysis: route_brand(analysis, TEXT_ACTIONS["remove"]),
        )
        self.assertEqual(RULES["resultStates"]["blocked"], removed.state)
        self.assertEqual(RULES["errorCodes"]["visualHardFailure"], removed.error_code)
        self.assertEqual([], removed_adapters.upload_calls)

        reviewed, reviewed_adapters = self.run_case(
            "brand-needs-review",
            lambda analysis: route_brand(analysis, TEXT_ACTIONS["review"]),
        )
        self.assertEqual(RULES["resultStates"]["needs_input"], reviewed.state)
        self.assertEqual([], reviewed_adapters.upload_calls)

    def test_long_paragraph_cannot_become_one_slot_even_with_exact_text_exception(self) -> None:
        def paragraph_slot(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            scenario = SCENARIOS["longPoster"]
            slot = analysis["slotCandidates"][1]
            slot["defaultValue"] = scenario["longText"]
            slot["exactVisibleTextEvidence"]["visibleText"] = scenario["longText"]
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                scenario["selectedSpan"], scenario["longText"]
            )
            analysis["defaultValuePreferenceExceptionEvidence"] = {
                slot["id"]: {"reviewed": True, "reason": "尝试保留完整海报段落"}
            }
            region = analysis[ANALYSIS_FIELDS["regions"]][0]
            region[REGION_FIELDS["valueClass"]] = VALUE_CLASSES["primaryVisual"]
            region[REGION_FIELDS["selectedText"]] = scenario["longText"]
            return analysis

        result, adapters = self.run_case("whole-paragraph-slot", paragraph_slot)
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)

        def paragraph_suggestion(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            analysis["slotCandidates"][1]["suggestions"][0] = SCENARIOS["longPoster"]["longText"]
            return analysis

        suggestion_result, suggestion_adapters = self.run_case(
            "whole-paragraph-suggestion", paragraph_suggestion
        )
        self.assertEqual(RULES["resultStates"]["blocked"], suggestion_result.state)
        self.assertEqual([], suggestion_adapters.upload_calls)

        def paragraph_in_generic_suggestion(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            analysis["slotCandidates"][2]["suggestions"][0] = SCENARIOS["longPoster"][
                "longText"
            ]
            return analysis

        generic_result, generic_adapters = self.run_case(
            "whole-paragraph-generic-suggestion", paragraph_in_generic_suggestion
        )
        self.assertEqual(RULES["resultStates"]["blocked"], generic_result.state)
        self.assertEqual([], generic_adapters.upload_calls)

    def test_exact_text_fidelity_rejects_language_token_line_case_and_symbol_drift(self) -> None:
        mutations = {
            "language": lambda region: region[REGION_FIELDS["exactTextEvidence"]].update({EVIDENCE_FIELDS["language"]: LANGUAGES["english"]}),
            "tokens": lambda region: region[REGION_FIELDS["exactTextEvidence"]].update({EVIDENCE_FIELDS["tokens"]: ["今日宜躺平"]}),
            "lines": lambda region: region[REGION_FIELDS["exactTextEvidence"]].update({EVIDENCE_FIELDS["lines"]: [region[REGION_FIELDS["sourceText"]].replace("\n", " ")]}),
            "case": lambda region: region[REGION_FIELDS["exactTextEvidence"]].update({EVIDENCE_FIELDS["caseSensitiveTokens"]: []}),
            "symbols": lambda region: region[REGION_FIELDS["exactTextEvidence"]].update({EVIDENCE_FIELDS["rareSymbols"]: ["-"]}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                def drift(analysis: dict, mutate: Callable[[dict], None] = mutate) -> dict:
                    analysis = self.text_card(analysis)
                    mutate(analysis[ANALYSIS_FIELDS["regions"]][0])
                    return analysis

                result, adapters = self.run_case(f"exact-text-drift-{name}", drift)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual([], adapters.upload_calls)

    def test_language_and_punctuation_are_driven_by_the_machine_contract(self) -> None:
        def bracketed_english(analysis: dict) -> dict:
            return self.rewrite_text_card(
                analysis,
                source_text="【SALE】",
                language=LANGUAGES["english"],
                tokens=["SALE"],
                case_sensitive_tokens=["SALE"],
                rare_symbols=[],
            )

        punctuation_result, _ = self.run_case("machine-punctuation", bracketed_english)
        self.assertEqual(RULES["resultStates"]["completed"], punctuation_result.state)

        language_cases = (
            ("mixed-script-as-chinese", "今日 SALE", LANGUAGES["simplifiedChinese"], ["今日", "SALE"], ["SALE"]),
            ("traditional-as-simplified", "歡迎光臨", LANGUAGES["simplifiedChinese"], ["歡迎光臨"], []),
            ("traditional-regression-as-simplified", "藝術設計", LANGUAGES["simplifiedChinese"], ["藝術設計"], []),
        )
        for item_id, source_text, wrong_language, tokens, case_tokens in language_cases:
            with self.subTest(item_id=item_id):
                def wrong_language_analysis(
                    analysis: dict,
                    source_text: str = source_text,
                    wrong_language: str = wrong_language,
                    tokens: list[str] = tokens,
                    case_tokens: list[str] = case_tokens,
                ) -> dict:
                    return self.rewrite_text_card(
                        analysis,
                        source_text=source_text,
                        language=wrong_language,
                        tokens=tokens,
                        case_sensitive_tokens=case_tokens,
                        rare_symbols=[],
                    )

                result, adapters = self.run_case(item_id, wrong_language_analysis)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual([], adapters.upload_calls)

        def valid_traditional(analysis: dict) -> dict:
            return self.rewrite_text_card(
                analysis,
                source_text="歡迎光臨",
                language=LANGUAGES["traditionalChinese"],
                tokens=["歡迎光臨"],
                case_sensitive_tokens=[],
                rare_symbols=[],
            )

        traditional_result, _ = self.run_case("traditional-language-valid", valid_traditional)
        self.assertEqual(RULES["resultStates"]["completed"], traditional_result.state)

    def test_secondary_readable_text_must_stay_in_prompt_and_free_editable_content(self) -> None:
        scenario = SCENARIOS["longPoster"]
        for target in ("prompt", "free"):
            with self.subTest(target=target):
                def missing_secondary(analysis: dict, target: str = target) -> dict:
                    analysis = self.long_poster(analysis)
                    if target == "prompt":
                        analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                            f"，次要文字写着“{scenario['secondaryText']}”", ""
                        )
                    else:
                        analysis["freeEditableContent"].remove(scenario["secondaryText"])
                    return analysis

                result, adapters = self.run_case(f"secondary-missing-{target}", missing_secondary)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual([], adapters.upload_calls)

        def locked_secondary(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            secondary = next(
                region
                for region in analysis[ANALYSIS_FIELDS["regions"]]
                if region[REGION_FIELDS["identity"]] == scenario["secondaryRegionId"]
            )
            secondary[REGION_FIELDS["action"]] = TEXT_ACTIONS["preserve"]
            return analysis

        locked, locked_adapters = self.run_case("secondary-incorrectly-locked", locked_secondary)
        self.assertEqual(RULES["resultStates"]["blocked"], locked.state)
        self.assertEqual([], locked_adapters.upload_calls)

    def test_removed_or_fixed_text_cannot_reenter_the_user_editable_prompt(self) -> None:
        scenario = SCENARIOS["longPoster"]
        forbidden_by_region = {
            "poster-attribution-region": scenario["attributionText"],
            "poster-decoration-region": scenario["decorativeText"],
            "poster-brand-region": scenario["brandText"],
        }
        for region_id, forbidden_text in forbidden_by_region.items():
            with self.subTest(region_id=region_id):
                def leak(analysis: dict, forbidden_text: str = forbidden_text) -> dict:
                    analysis = self.long_poster(analysis)
                    analysis["freeEditableContent"].append(forbidden_text)
                    analysis["promptTemplate"] = analysis["promptTemplate"].removesuffix("。") + (
                        f"，并显示“{forbidden_text}”。"
                    )
                    return analysis

                result, adapters = self.run_case(f"fixed-text-leak-{region_id}", leak)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual([], adapters.upload_calls)

        def attribution_with_equivalent_spacing(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            formatted_attribution = "摄影：MOMO  STUDIO"
            analysis["freeEditableContent"].append(formatted_attribution)
            analysis["promptTemplate"] += f" 右下角还写着{formatted_attribution}。"
            return analysis

        spaced_result, spaced_adapters = self.run_case(
            "fixed-attribution-spacing-bypass", attribution_with_equivalent_spacing
        )
        self.assertEqual(RULES["resultStates"]["blocked"], spaced_result.state)
        self.assertEqual([], spaced_adapters.upload_calls)

        def wrapped_decoration_in_free_content(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            analysis["freeEditableContent"].append("年份 2026")
            analysis["promptTemplate"] += " 装饰年份为 2026。"
            return analysis

        free_result, free_adapters = self.run_case(
            "wrapped-fixed-token-in-free-content", wrapped_decoration_in_free_content
        )
        self.assertEqual(RULES["resultStates"]["blocked"], free_result.state)
        self.assertEqual([], free_adapters.upload_calls)

        def wrapped_decoration_in_prompt_only(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            analysis["promptTemplate"] += " 装饰年份为 2026。"
            return analysis

        prompt_result, prompt_adapters = self.run_case(
            "wrapped-fixed-token-in-prompt", wrapped_decoration_in_prompt_only
        )
        self.assertEqual(RULES["resultStates"]["blocked"], prompt_result.state)
        self.assertEqual([], prompt_adapters.upload_calls)

    def test_approved_image_with_remove_action_is_a_visual_hard_failure(self) -> None:
        def watermark_present(analysis: dict) -> dict:
            return self.add_watermark_to_approved_analysis(self.long_poster(analysis))

        result, adapters = self.run_case("approved-watermark-removal", watermark_present)
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["visualHardFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_fixed_visible_text_cannot_hide_in_a_generic_prompt_slot(self) -> None:
        scenario = SCENARIOS["longPoster"]

        def decoration_as_generic_slot(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            slot = analysis["slotCandidates"][2]
            old_id = slot["id"]
            old_default = slot["defaultValue"]
            slot.update(
                {
                    "id": "decorative_caption",
                    "defaultValue": scenario["decorativeText"],
                    "suggestions": ["EST. 2025", "EST. 2027"],
                }
            )
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                old_id, slot["id"]
            ).replace(old_default, slot["defaultValue"])
            analysis["defaultValuePreferenceExceptionEvidence"] = {
                slot["id"]: {"reviewed": True, "reason": "尝试把装饰年份作为普通 prompt 槽"}
            }
            return analysis

        result, adapters = self.run_case("decoration-generic-prompt-slot", decoration_as_generic_slot)
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)

        def decoration_token_as_generic_slot(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            slot = analysis["slotCandidates"][2]
            old_id = slot["id"]
            old_default = slot["defaultValue"]
            slot.update(
                {
                    "id": "decorative_year",
                    "semanticRole": "decorative_caption",
                    "defaultValue": "2026",
                    "suggestions": ["2025", "2024", "2023"],
                }
            )
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                old_id, slot["id"]
            ).replace(old_default, slot["defaultValue"])
            analysis["defaultValuePreferenceExceptionEvidence"] = {
                slot["id"]: {"reviewed": True, "reason": "尝试把装饰微字中的年份作为普通 prompt 槽"}
            }
            return analysis

        token_result, token_adapters = self.run_case(
            "decoration-token-generic-prompt-slot", decoration_token_as_generic_slot
        )
        self.assertEqual(RULES["resultStates"]["blocked"], token_result.state)
        self.assertEqual([], token_adapters.upload_calls)

        def merged_decoration_token(analysis: dict) -> dict:
            analysis = decoration_token_as_generic_slot(analysis)
            decoration = next(
                region
                for region in analysis[ANALYSIS_FIELDS["regions"]]
                if region[REGION_FIELDS["identity"]] == "poster-decoration-region"
            )
            evidence = decoration[REGION_FIELDS["exactTextEvidence"]]
            evidence[EVIDENCE_FIELDS["tokens"]] = [scenario["decorativeText"]]
            evidence[EVIDENCE_FIELDS["caseSensitiveTokens"]] = [
                scenario["decorativeText"]
            ]
            return analysis

        merged_result, merged_adapters = self.run_case(
            "merged-decoration-token-cannot-hide-year", merged_decoration_token
        )
        self.assertEqual(RULES["resultStates"]["blocked"], merged_result.state)
        self.assertEqual([], merged_adapters.upload_calls)

        def prefixed_decoration_token(analysis: dict) -> dict:
            analysis = decoration_token_as_generic_slot(analysis)
            slot = analysis["slotCandidates"][2]
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                slot["defaultValue"], "年份 2026"
            )
            slot["defaultValue"] = "年份 2026"
            slot["suggestions"] = ["年份 2025", "年份 2024", "年份 2023"]
            return analysis

        prefixed_result, prefixed_adapters = self.run_case(
            "prefixed-decoration-token-has-non-slot-origin",
            prefixed_decoration_token,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], prefixed_result.state)
        self.assertEqual([], prefixed_adapters.upload_calls)

        def unrelated_scene_with_shared_word(analysis: dict) -> dict:
            analysis = self.long_poster(analysis)
            slot = analysis["slotCandidates"][2]
            old_default = slot["defaultValue"]
            slot["defaultValue"] = "摄影棚"
            slot["suggestions"] = ["工作室", "展览馆", "排练厅"]
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                old_default, slot["defaultValue"]
            )
            return analysis

        scene_result, scene_adapters = self.run_case(
            "attribution-token-does-not-lock-scene", unrelated_scene_with_shared_word
        )
        self.assertEqual(RULES["resultStates"]["completed"], scene_result.state)
        self.assertEqual(1, len(scene_adapters.upload_calls))

    def test_malformed_text_shapes_return_stable_results_and_extra_fields_are_rejected(self) -> None:
        mutations = {
            "unhashable-role": lambda analysis: analysis[ANALYSIS_FIELDS["regions"]][0].update(
                {REGION_FIELDS["role"]: {}}
            ),
            "extra-region-field": lambda analysis: analysis[ANALYSIS_FIELDS["regions"]][0].update(
                {"unexpectedField": True}
            ),
        }
        for item_id, mutate in mutations.items():
            with self.subTest(item_id=item_id):
                def malformed(analysis: dict, mutate: Callable[[dict], None] = mutate) -> dict:
                    analysis = self.text_card(analysis)
                    mutate(analysis)
                    return analysis

                result, adapters = self.run_case(item_id, malformed)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
                self.assertEqual([], adapters.upload_calls)

        adapters = TextScenarioAdapters(self.text_card)
        original_audit = adapters.audit_semantics

        def malformed_audit(content: dict) -> dict:
            audit = original_audit(content)
            field = RULES["semanticAuditChecks"]["visibleTextClassification"]["evidence"]
            decisions = TEXT_CONTRACT["semanticAuditFields"]["decisions"]
            audit["evidence"][field][decisions] = 1
            return audit

        adapters.audit_semantics = malformed_audit
        result = run_production(
            {**self.request, "productionItemId": "malformed-text-semantic-evidence"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

        reviewed_adapters = TextScenarioAdapters(self.text_card)
        original_reviewed_audit = reviewed_adapters.audit_semantics

        def malformed_reviewed_regions(content: dict) -> dict:
            audit = original_reviewed_audit(content)
            field = RULES["semanticAuditChecks"]["visibleTextClassification"]["evidence"]
            reviewed = TEXT_CONTRACT["semanticAuditFields"]["reviewedRegionIdentities"]
            audit["evidence"][field][reviewed] = [{}]
            return audit

        reviewed_adapters.audit_semantics = malformed_reviewed_regions
        reviewed_result = run_production(
            {**self.request, "productionItemId": "malformed-reviewed-region-ids"},
            self.output_root,
            reviewed_adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], reviewed_result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], reviewed_result.error_code)
        self.assertEqual([], reviewed_adapters.upload_calls)

    def test_semantic_provenance_cannot_omit_known_slot_or_free_content_origins(self) -> None:
        audit_fields = TEXT_CONTRACT["semanticAuditFields"]
        slot_origin_fields = TEXT_CONTRACT["slotOriginFields"]
        free_origin_fields = TEXT_CONTRACT["freeContentOriginFields"]
        visible_review_field = RULES["semanticAuditChecks"]["visibleTextClassification"][
            "evidence"
        ]

        slot_adapters = TextScenarioAdapters(self.text_card)
        original_slot_audit = slot_adapters.audit_semantics

        def missing_slot_origin(content: dict) -> dict:
            audit = original_slot_audit(content)
            decisions = audit["evidence"][visible_review_field][
                audit_fields["slotOrigins"]
            ]
            headline = next(
                decision
                for decision in decisions
                if decision[slot_origin_fields["slotIdentity"]] == "headline_text"
            )
            headline[slot_origin_fields["originRegionIdentity"]] = None
            return audit

        slot_adapters.audit_semantics = missing_slot_origin
        slot_result = run_production(
            {**self.request, "productionItemId": "missing-required-slot-origin"},
            self.output_root,
            slot_adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], slot_result.state)
        self.assertEqual([], slot_adapters.upload_calls)

        free_adapters = TextScenarioAdapters(self.long_poster)
        original_free_audit = free_adapters.audit_semantics
        secondary_text = SCENARIOS["longPoster"]["secondaryText"]

        def missing_free_origin(content: dict) -> dict:
            audit = original_free_audit(content)
            decisions = audit["evidence"][visible_review_field][
                audit_fields["freeContentOrigins"]
            ]
            secondary = next(
                decision
                for decision in decisions
                if decision[free_origin_fields["content"]] == secondary_text
            )
            secondary[free_origin_fields["originRegionIdentity"]] = None
            return audit

        free_adapters.audit_semantics = missing_free_origin
        free_result = run_production(
            {**self.request, "productionItemId": "missing-required-free-origin"},
            self.output_root,
            free_adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], free_result.state)
        self.assertEqual([], free_adapters.upload_calls)

    def test_identity_related_text_is_neutral_when_the_subject_is_open(self) -> None:
        def specific_identity(analysis: dict) -> dict:
            analysis = self.rewrite_text_card(
                analysis,
                source_text="皮卡丘",
                language=LANGUAGES["simplifiedChinese"],
                tokens=["皮卡丘"],
                case_sensitive_tokens=[],
                rare_symbols=[],
            )
            slot = analysis["slotCandidates"][1]
            slot["semanticRole"] = IDENTITY_TEXT_ROLE
            slot["suggestions"] = ["哆啦A梦", "伊布", "索尼克"]
            region = analysis[ANALYSIS_FIELDS["regions"]][0]
            region[REGION_FIELDS["valueClass"]] = VALUE_CLASSES["identityRelated"]
            return analysis

        result, adapters = self.run_case("specific-identity-text", specific_identity)
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)

        def valid_neutral_chinese(analysis: dict) -> dict:
            analysis = self.identity_interface(analysis)
            scenario = SCENARIOS["identityInterface"]
            slot = analysis["slotCandidates"][1]
            slot["defaultValue"] = "档案"
            slot["suggestions"] = ["简介", "代号", "昵称"]
            slot["exactVisibleTextEvidence"]["visibleText"] = "档案"
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                scenario["sourceText"], "档案"
            )
            region = analysis[ANALYSIS_FIELDS["regions"]][0]
            region[REGION_FIELDS["sourceText"]] = "档案"
            region[REGION_FIELDS["selectedText"]] = "档案"
            evidence = region[REGION_FIELDS["exactTextEvidence"]]
            evidence[EVIDENCE_FIELDS["language"]] = LANGUAGES["simplifiedChinese"]
            evidence[EVIDENCE_FIELDS["tokens"]] = ["档案"]
            evidence[EVIDENCE_FIELDS["lines"]] = ["档案"]
            evidence[EVIDENCE_FIELDS["caseSensitiveTokens"]] = []
            return analysis

        neutral_result, neutral_adapters = self.run_case(
            "neutral-chinese-identity-copy", valid_neutral_chinese
        )
        self.assertEqual(RULES["resultStates"]["completed"], neutral_result.state)
        self.assertEqual(1, len(neutral_adapters.upload_calls))

    def test_ambiguous_text_requires_review_and_has_no_generation_delivery_side_effect(self) -> None:
        def ambiguous(analysis: dict) -> dict:
            analysis = self.text_card(analysis)
            region = analysis[ANALYSIS_FIELDS["regions"]][0]
            region.update({REGION_FIELDS["role"]: TEXT_ROLES["ambiguous"], REGION_FIELDS["action"]: TEXT_ACTIONS["review"]})
            region.pop(REGION_FIELDS["slotIdentity"])
            return analysis

        result, adapters = self.run_case("ambiguous-text-review", ambiguous)
        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse(result.gallery_template)


if __name__ == "__main__":
    unittest.main()

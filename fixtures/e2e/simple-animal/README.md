# Issue #2–#6 tracer fixture

这个 fixture 通过同一个公共工作流运行一张普通动物网图的 P0–P8。适配器读取固定的来源分析、确认模板图审核、模板图分析和独立语义审计，并用图片或被审计内容 SHA 绑定每段证据。

| 经验 | 可定位证据 |
| --- | --- |
| E01 | `test_issue_2_vertical_slice.py::test_e01_single_call_keeps_independent_state_lineage_and_pin` |
| E04 | `test_issue_2_vertical_slice.py::test_e04_source_identity_leak_blocks_before_upload` |
| E05、E07 | `replacement-plan.json` 断言单一主要目标与同类替换 |
| E06 | `dependencyClosure` 逐项记录自动补齐来源；显式策略不跳过闭包 |
| E10、E11 | 六维 `visual-review.json` 与硬失败测试 |
| E12 | 非目标保持与可见文字证据；未授权文字漂移在 P2 阻断 |
| E13 | 全画布幽灵字、伪签名和平台水印 finding 均属于硬失败 |
| E19、E21 | 三槽 fixture 含主体槽及四道价值门禁 |
| E27 | Prompt Template 槽位绑定、`freeEditableContent`、默认值/推荐值代入自然度、隐藏语义冲突与最大差异标题断言 |
| E25 | Replacement Pool 与 Slot Suggestion Pool 分离；推荐项拒绝默认值和重复值 |
| E29 | 六维视觉事实逐项携带 `pass` 与图像证据，并绑定当前生成事实 |
| E34 | 视觉硬失败从 P2 创建新 request ID 与不可变 revision，复用已验证 P0/P1 |
| E35、E36 | 正式白名单、同 URL 回填与 sidecar 隔离断言 |
| E38 | 测试只允许 fixture generation/OSS 调用，不调用数据库、管理台或发布系统 |

运行：

```bash
python3 scripts/produce.py \
  --request fixtures/e2e/simple-animal/request.json \
  --deterministic-fixture fixtures/e2e/simple-animal \
  --output /tmp/produce-meme-template-smoke
```

Issue #3 的显式策略、unknown、普通真人、知名 IP、动物、物体、文字、场景属性、稳定性与池隔离验收位于 `tests/test_issue_3_replacement_strategy.py`。

Issue #4 的完整硬门禁、自主确认、风险升级、审核新鲜度、失效传播与 P2 重做验收位于 `tests/test_issue_4_template_image_gate.py`。

Issue #5 的必填主体判别、主体槽省略证据、单槽例外、唯一槽位 ID、人物派生属性、默认值语言/长度及图像绑定例外、Prompt 内联默认值、非空推荐池、自由编辑文字、资产单元计数、隐藏层职责和逐检查语义证据验收位于 `tests/test_issue_5_editable_prompt_compiler.py`。

Issue #6 的严格正式白名单、条件 `needsReview`、sidecar 隔离、模板图 URL 同一性、未知字段与临时值拒绝位于 `tests/test_issue_6_formal_gallery_contract.py`；两份权威样例的输入、显式 expected 投影和摘要证据位于 `fixtures/contracts/latest-gallery-samples/`。

# Issue #2 tracer fixture

这个 fixture 通过同一个公共工作流运行一张普通动物网图的 P0–P8。适配器读取固定的来源分析、确认模板图审核、模板图分析和独立语义审计，并用图片或被审计内容 SHA 绑定每段证据。

| 经验 | 可定位证据 |
| --- | --- |
| E01 | `test_issue_2_vertical_slice.py::test_e01_single_call_keeps_independent_state_lineage_and_pin` |
| E04 | `test_issue_2_vertical_slice.py::test_e04_source_identity_leak_blocks_before_upload` |
| E05、E07 | `replacement-plan.json` 断言单一主要目标与同类替换 |
| E10、E11 | 六维 `visual-review.json` 与硬失败测试 |
| E19、E21 | 三槽 fixture 含主体槽及四道价值门禁 |
| E27 | Prompt Template 槽位绑定、`freeEditableContent`、默认值/推荐值代入自然度、隐藏语义冲突与最大差异标题断言 |
| E35、E36 | 正式白名单、同 URL 回填与 sidecar 隔离断言 |
| E38 | 测试只允许 fixture generation/OSS 调用，不调用数据库、管理台或发布系统 |

运行：

```bash
python3 scripts/produce.py \
  --request fixtures/e2e/simple-animal/request.json \
  --deterministic-fixture fixtures/e2e/simple-animal \
  --output /tmp/produce-meme-template-smoke
```

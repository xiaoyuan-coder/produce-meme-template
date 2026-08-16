# 最新 Gallery 样例投影 fixture

`heart.input.json` 与 `wedding.input.json` 是 2026-08-14 两份最新正式样例的逐字节只读副本：

| fixture | SHA-256 |
| --- | --- |
| `heart.input.json` | `e0970eda3bbc77399ff222bce0023c38e8917dbf817830942489b36d18c0ad34` |
| `wedding.input.json` | `5786703f093b64513e556a77ae0af9455e6af41d9bdd1d922444774174b5736e` |

`*.expected.json` 是显式可比投影：只保留机器白名单中的正式字段和 `metadata.tags`，并按机器迁移规则把正式运行字段中的旧“候选图”称呼改为“模板参考图”。它们还落实了合同审计明确要求的语义修正：爱心标题使用中性双人表达；婚礼标题不写死猫咪，描述不写死三只，Prompt 根据宠物数量自适应位置。输入样例里的 `semanticContext`、`runtimeRequirements`、`templateSource`、`inputSemantics` 与 `optimizationAudit` 保留在原始 input fixture，作为生产 sidecar / 迁移证据，不进入 expected 正式记录。

`tests/test_issue_6_formal_gallery_contract.py` 遍历两份输入的全部标量叶子，要求每条路径都明确归入正式投影或已识别 sidecar，未分类数保持为 0；同时精确比较迁移投影与 expected 的语义纠偏路径，验证输入摘要、冻结 Schema 和最终业务门禁。

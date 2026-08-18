# Gallery Template runtimeSemantics v2 样例投影 fixture

`heart.input.json` 与 `wedding.input.json` 是 2026-08-18 两份已按研发 v2 运行合同迁移的实际生产样例：

| fixture | SHA-256 |
| --- | --- |
| `heart.input.json` | `157acce3f5efe4d0beb768eed86e5085151cb10b1159a0e89162e7f5c6e71fde` |
| `wedding.input.json` | `ce34c952aca1771a28fdd818970cbf638816a4dbb6966b7ef7e79ac40f8c612f` |

`*.expected.json` 是显式可比投影：保留 v2 正式字段与 `metadata.tags`；输入样例中的 `semanticContext` 和 `contractAudit` 作为生产 sidecar / 迁移证据，不进入 expected 正式记录。

`tests/test_issue_6_formal_gallery_contract.py` 遍历两份输入的全部标量叶子，要求每条路径都归入正式投影或已识别 sidecar，并校验投影、冻结 Schema 与 runtimeSemantics 正式门禁。

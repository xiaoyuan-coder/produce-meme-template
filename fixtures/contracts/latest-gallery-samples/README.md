# Gallery Template inputSchema v2 / runtimeSemantics v1 样例投影 fixture

`heart.input.json` 与 `wedding.input.json` 是 2026-08-18 两份已按研发 v2 运行合同迁移的实际生产样例：

| fixture | SHA-256 |
| --- | --- |
| `heart.input.json` | `026e69c00d28495c5bfcafdb4cccd2bab84a6f33b7bbf580d1eef380264fd03a` |
| `wedding.input.json` | `160f042c8a06cb776b8bad0177363226f72397626d852a29cd672a161177acf9` |

`*.expected.json` 是显式可比投影：保留 v2 正式字段与 `metadata.tags`；输入样例中的 `semanticContext` 和 `contractAudit` 作为生产 sidecar / 迁移证据，不进入 expected 正式记录。

`tests/test_issue_6_formal_gallery_contract.py` 遍历两份输入的全部标量叶子，要求每条路径都归入正式投影或已识别 sidecar，并校验投影、冻结 Schema 与 runtimeSemantics 正式门禁。

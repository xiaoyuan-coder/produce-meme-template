# Issue #7 身份路由 fixture

`scenarios.json` 用机器规则中的具名 role 描述三个端到端场景：

- 普通真人通过 prompt-only 创建新身份，删除旧身份文字；
- 公众人物按同类候选卡替换，同步固定玩法中的身份文字，不开放主体槽；
- 知名 IP 按同类候选卡替换，将身份文字改为中性高价值文字槽。

`tests/test_issue_7_identity_replacement.py` 将 role 解析为当前机器值，复用 `simple-animal` 的确定性图片 adapter 运行 P0–P8，同时覆盖候选卡缺失、依赖闭包漏项、身份文字未同步和非身份默认内容泄漏反例。

# 全量规范注册表

## 1. 权威范围

注册表覆盖 `SKILL.md`、`CONTEXT.md`、实施规格、`SKILL.md` 可达的全部本地 Markdown reference，以及除已明确取代 ADR 外的全部当前 ADR。扫描器索引每个权威文档中的全部非代码语义单元，不依赖“必须”“禁止”等关键词，因此改写措辞不能绕开登记。

权威文档集合由 `contracts/machine-rules.json#/normativeRuleRegistryContract` 发现。新增 Skill 指针或 ADR 会自动扩大集合；注册表未同步时仓库测试失败。

## 2. 执行所有权

`contracts/normative-rule-registry.json` 为每个权威文档绑定一个执行家族，并冻结文件摘要和全部语义单元的唯一 `ruleId` 与摘要。每个执行家族必须同时绑定：

- 一个真实 Python symbol 或机器合同 JSON Pointer；
- 至少一个 Good Case；
- 至少一个 Bad Case；
- 至少一个历史经验 ID；
- 适用时绑定最终关键资格角色。

文档本身不能充当执行所有者。所有者文件、symbol、测试方法、历史经验或关键资格引用失效时，验证器失败。

## 3. 变更流程

修改 Skill、一级 reference、实施规格或 ADR 时，先修改唯一权威内容及其代码门禁和红绿证据，再运行：

```bash
python3 scripts/validate_normative_rule_registry.py --root . --render
```

`--render` 只输出候选注册表，不写文件。维护者审查新增、删除和改写的 rule unit 及其执行家族后，再更新注册表。`--check` 精确对账当前权威集合、文档摘要、语义单元、所有者和证据。

审查通过后使用 `--write` 原子更新机器注册表，随后必须再运行 `--check`。

禁止只刷新摘要来放行知识层规则。新语义超出现有执行家族时，先新增代码门禁、Bad Case、历史经验和新的执行家族；完成这些条件后才接纳注册表变化。

## 4. 完成条件

规范注册表 PASS 只证明所有可达规范都已进入一个带代码所有者和红绿证据的执行家族。P8 的关键资格账本继续证明当前生产项的业务结果；运行、恢复、发布和 T1 等独立合同由各自执行家族证明。两层必须同时通过。

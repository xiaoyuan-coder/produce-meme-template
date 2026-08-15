# ADR 0008：当前正式封面字段采用 cover

- 状态：已接受
- 日期：2026-08-15

## 背景

当前正式数据、2026-07-30 Schema 和 2026-08-13 的 843 条最终数据都使用 `cover`。Unified V1.7.1、旧 UAT 与管理台专题导出代码仍使用 `coverUrl`，形成真实版本冲突。用户明确确认当前正式数据采用 `cover`。

## 决定

新 Skill 正式模板 JSON 使用 `cover` 与 `referenceImage`，两者写入同一个已验证模板图 HTTPS URL。正式输出不生成 `coverUrl`，也不为旧链路执行双写。

## 影响

- finalizer、Schema、fixtures、版本合同和最终验收共同使用 `cover`。
- Unified 与管理台中的 `coverUrl` 作为迁移检查项和版本冲突 fixture。
- 未来字段变化通过外部合同升级和显式迁移处理。

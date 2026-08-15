# ADR 0007：曾误判当前正式封面字段为 coverUrl

- 状态：已被 ADR 0008 取代
- 日期：2026-08-15

## 背景

历史数据同时存在 `cover` 与 `coverUrl`。2026-07-30 的本地 Schema 和 2026-08-13 的 843 条修订数据使用 `cover`；Unified V1.7.1、已确认 UAT 扩展和当前管理台专题导出代码使用 `coverUrl`。专题导出会主动拒绝缺少有效 `coverUrl` 的记录。

## 决定

本 ADR 当时依据 Unified V1.7.1、旧 UAT 和管理台专题导出代码，误将 `coverUrl` 判断为当前正式字段。用户随后确认当前正式数据使用 `cover`。

## 影响

- 本决定停止执行，后续以 ADR 0008 为准。
- Unified 与管理台中的 `coverUrl` 记录为版本不一致证据。

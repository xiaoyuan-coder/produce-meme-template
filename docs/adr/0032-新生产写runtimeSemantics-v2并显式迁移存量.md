# ADR 0032：新生产写 runtimeSemantics v2 并显式迁移存量

- 状态：已接受
- 日期：2026-08-27

## 背景

研发作者合同于 2026-08-26 将当前写版本提升为 `runtimeSemantics.version=2`，读取端继续接受 v1/v2。新合同增加动态身份群组 `identity_group`、`preserve_group + group_photo` 和身份绑定的 `clothingOwnership`。研发文档 Schema 暂时漏写 `clothingOwnership`，真实 TypeScript Schema、真实校验器和官方样例已经支持该字段。

## 决定

新 Production Item 只写 `runtimeSemantics.version=2`。每个 `replace_identity` binding 必须显式写 `clothingOwnership=source|template`；固定目标继续使用 `one_to_one` 或 `same_source_repeated`，动态合照使用一个 `identity_group` 目标和 `preserve_group + group_photo`。当前关闭 `preserve_pixels`、`distinct_subjects`、合照选单人、人宠混合自动拆组、`match_declared_order` 和 `reference_only`。

T1 作为兼容读取端接受 v1/v2。P8 和正式新数据验证只接受当前写版本。存量 v1 通过独立迁移工具升级：结构可确定性转换，服装归属逐身份槽显式给出；缺少裁决时停在审计状态，不从散文静默推断，也不覆盖原文件。迁移前后都必须执行冻结 Schema 和跨字段验证；无效 v2 不能标记为 current，无效迁移结果不能写盘。v1 缺少动态群组事实时不能自动推导 `identity_group`，需要重新作者分析。

冻结快照以真实运行行为为准，并在元数据中记录上游文档 Schema 的遗漏。正式快照路径、合同版本、Skill 版本和 Artifact Schema 同步升级。

## 影响

- 新数据稳定输出 v2，服装来源进入可执行字段。
- 固定 CP/多实例继续逐目标同步替换；人数随合照变化的群像具备独立结构。
- 存量迁移可以先全量审计，再按明确决策批量写入隔离目录；CLI 同时接受单模板、裸数组和正式 v2 bundle。模板 key 必须符合上游正典格式，输出路径需要留在解析后的隔离根内，并以原子 create-once 方式写入。
- 上游文档与运行 Schema 再次一致前，快照导入器保留来源 SHA 和差异说明。

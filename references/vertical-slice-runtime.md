# 单图纵向切片运行合同

## 1. 公共工作流

Issue #2–#5 只通过 `run_production(request, output_root, adapters)` 暴露正式生产接缝。请求包含一个 `templateKey`、一个 `sourceImage` 和可选的单图 `replacementStrategy`，输出属于一个独立 Production Item。来源/模板分析、生成、视觉证据、独立语义审计和 OSS 由注入式 adapter 提供；阶段推进、门禁、状态、谱系、正式投影和外部副作用授权由工作流核心控制。

机器阶段、外部结果、错误码、类别和视觉维度统一读取 `contracts/machine-rules.json`。类别与策略来源使用“具名领域角色 → 机器值”映射，代码不依赖 JSON 成员顺序。代码、测试和 fixture 不再维护第二份枚举。

## 2. P0–P8 产物

| 阶段 | 状态 | 主要产物 |
| --- | --- | --- |
| P0 | `INGESTED` | source evidence、`source-analysis.json`、`production-pin.json` |
| P1 | `REPLACEMENT_PLANNED` | `replacement-plan.json` |
| P2 | `TEMPLATE_IMAGE_APPROVED` | `generation-package.json`、Approved Template Image、`visual-review.json` |
| P3 | `TEMPLATE_ANALYZED` | `template-analysis.json` |
| P4 | `EDITABLE_SPEC_COMPILED` | `editable-template-spec.json` |
| P5 | `TEMPLATE_COMPILED` | `hidden-template-spec.json`、`gallery-template.draft.json` |
| P6 | `STATIC_VALIDATED` | `semantic-audit.json`、`validation-report.json` |
| P7 | `ASSET_UPLOADED` | `asset-receipt.json` |
| P8 | `FINALIZED` | `final-validation-report.json`、`gallery-template.json` |

`production-manifest.json` 记录每个阶段、产物 SHA、依赖、revision 和外部结果，并把规范化单图策略 SHA 纳入 Production Item 身份。除 manifest 外，revision 产物使用原子排他创建；相同 Production Item 内容不一致时返回稳定的不可变冲突。请求标识符与策略结构在落盘前通过检查，解析后的 Production Item 真实路径还必须是 `output_root` 的直接子目录；预置符号链接不能把写入引向根外。相同来源图、key 和策略已完成后再次调用，需要先验证请求身份、pin 和全部产物摘要，再复用最终产物。

## 3. 适配器门禁

- 来源分析证据必须绑定 Source Web Image SHA。
- 来源分析 adapter 同时接收规范化后单图策略的隔离副本，不能修改工作流用于身份摘要与规划的原始快照。指定值通过独立 `explicitReplacementEvaluation` 证据执行同类、语义、视觉、权利与安全硬过滤，无需进入自主替换池。
- 文字与自主场景目标必须提供 `targetEligibility` 前置条件证据；显式场景值依据策略优先级直接进入硬过滤。冻结项通过绑定策略值和变更组件 ID 的 `preserveConflictEvaluations` 做语义冲突判定，与主要目标或依赖闭包重叠时在 P1 前阻断。
- 权利或安全证据仍为 `review` 的显式值，以及没有 pass 候选但存在 review 候选的自主路由，统一返回 `needs_input / NEEDS_REVIEW`，不与确定不兼容混为同一阻断结果。
- `dependencyClosure` 结构损坏时返回稳定外部分析失败；闭包为空、替换范围无法可靠判定时返回 `needs_input / NEEDS_REVIEW`。两条路径都不调用生成或上传 adapter。
- 视觉审核必须绑定当前 Generation Package SHA、生成图 SHA、完整证据 SHA、检查方法版本和时间；模板分析必须绑定当前 Approved Template Image SHA。详细规则读取 `template-image-gate.md`。
- 语义审计必须绑定标题、Prompt、隐藏层、自由内容和全部槽位值的规范摘要，并通过机器规则声明的完整审计项；推荐项的同轴、同颗粒度和可生成性属于该独立语义审计。adapter 只接收隔离的只读快照；返回后请求摘要或核心编译摘要发生变化时返回稳定外部失败，P4/P5 产物不能与 P8 投影分叉。
- Generation Package 与审核绑定由工作流核心保留不可变快照；生成和视觉审核 adapter 只接收隔离副本，返回结果必须重新绑定核心持有的 request ID 与摘要。
- 生成结果先以 `generated-candidate-image` 保存；只有六维视觉合同和全部硬门禁通过后，才能登记为 Approved Template Image。
- 硬失败停止在 P2，人工意见不能绕过门禁。
- 清晰通过由工作流自主确认；身份歧义、审美风险或证据不足返回人工复核且不创建确认图。
- P2 视觉硬失败可创建新的不可变 revision，只重做生成与视觉审核；新 generation package 使用新 request ID，旧 P2 证据和精确失效事件保留在 manifest。
- P7 adapter 只能接收当前 Approved Template Image；Asset Receipt 的图片 SHA 必须一致，URL 必须为 HTTPS。
- P7 恢复先验证已有产物谱系和 Asset Receipt，再直接执行 P8 正式投影；图片 SHA、对象键或 URL 不一致时阻断，恢复路径不调用生成或上传 adapter。
- 完整生产调用授权门禁后的 P7 上传，不授权数据库导入、管理台写入、发布、Tag 或生产上线。

## 4. 版本 pin

每个 Production Item 单独保存 Skill 行为版本、Artifact Schema 版本、完整 tracked-file release digest、机器规则 SHA、Gallery Contract 快照 SHA 和上游来源 SHA。运行中的 pin 不读取未来安装版本。Issue #4 从视觉硬失败创建后续 revision，先验证并复用 P0/P1，再记录 generation facts 的下游失效范围；真实外部任务 WAL 与更广阶段恢复由后续 ticket 扩展。

## 5. 迁移证据

确定性 tracer fixture 位于 `fixtures/e2e/simple-animal/`。Issue #2 测试覆盖 E01、E04、E05、E07、E10、E11、E19、E21、E27、E35、E36 和 E38；Issue #3 覆盖 E05、E06、E07、E10 和 E25；Issue #4 覆盖 E06、E10、E11、E12、E13、E29 和 E34；Issue #5 的高价值槽位、文案、资产单元与统一 Prompt 编译覆盖 E18、E19、E20、E21、E22、E24、E25、E26、E27、E28、E30 和 E31。

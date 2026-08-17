# 生产项纵向切片运行合同

## 1. 公共工作流

Issue #2–#12 只通过 `run_production(request, output_root, adapters)` 暴露正式生产接缝。单项请求包含一个 `templateKey`、一个 `sourceImage`、可选的单图 `replacementStrategy` 和 `generationOptions`，输出属于一个独立 Production Item。批量信封也进入该入口，由核心拆成相同的单项生命周期；默认不共享业务事实，显式共享策略的分辨读取 `shared-batch-policy.md`。来源/模板分析、队列生成、视觉证据、独立语义审计和 OSS 由注入式 adapter 提供；阶段推进、门禁、状态、谱系、正式投影和外部副作用授权由工作流核心控制。

机器阶段、外部结果、错误码、类别和视觉维度统一读取 `contracts/machine-rules.json`。类别与策略来源使用“具名领域角色 → 机器值”映射，代码不依赖 JSON 成员顺序。代码、测试和 fixture 不再维护第二份枚举。

## 2. P0–P8 产物

| 阶段 | 状态 | 主要产物 |
| --- | --- | --- |
| P0 | `INGESTED` | source evidence、`source-analysis.json`、`production-pin.json` |
| P1 | `REPLACEMENT_PLANNED` | `replacement-plan.json` |
| P2 | `TEMPLATE_IMAGE_APPROVED` | `generation-package.json`、`generation-task.json`、`generation-wal.json`、Approved Template Image、`visual-review.json` |
| P3 | `TEMPLATE_ANALYZED` | `template-analysis.json` |
| P4 | `EDITABLE_SPEC_COMPILED` | `editable-template-spec.json` |
| P5 | `TEMPLATE_COMPILED` | `hidden-template-spec.json`、`gallery-template.draft.json` |
| P6 | `STATIC_VALIDATED` | `semantic-audit.json`、`validation-report.json` |
| P7 | `ASSET_UPLOADED` | `asset-receipt.json` |
| P8 | `FINALIZED` | `final-validation-report.json`、`gallery-template.json` |

`production-manifest.json` 记录每个阶段、产物 SHA、依赖、revision 和外部结果，并把规范化单图策略 SHA 纳入 Production Item 身份。显式共享策略还将当前 item 的分辨摘要纳入身份并保存 `shared-policy-resolution.json`。每个不可变产物记录 Production Item scope digest 与不可变依赖摘要，防止跨 item 事实交换。除 manifest 外，revision 产物使用原子排他创建；相同 Production Item 内容不一致时返回稳定的不可变冲突。请求标识符与策略结构在落盘前通过检查，解析后的 Production Item 真实路径还必须是 `output_root` 的直接子目录；预置符号链接不能把写入引向根外。相同来源图、key 和策略已完成后再次调用，需要先验证请求身份、pin 和全部产物摘要，再复用最终产物。

## 3. 适配器门禁

- 来源分析证据必须绑定 Source Web Image SHA。
- 来源分析 adapter 同时接收规范化后单图策略的隔离副本，不能修改工作流用于身份摘要与规划的原始快照。指定值通过独立 `explicitReplacementEvaluation` 证据执行同类、语义、视觉、权利与安全硬过滤，无需进入自主替换池。
- 文字与自主场景目标必须提供 `targetEligibility` 前置条件证据；显式场景值依据策略优先级直接进入硬过滤。冻结项通过绑定策略值和变更组件 ID 的 `preserveConflictEvaluations` 做语义冲突判定，与主要目标或依赖闭包重叠时在 P1 前阻断。
- 权利或安全证据仍为 `review` 的显式值，以及没有 pass 候选但存在 review 候选的自主路由，统一返回 `needs_input / NEEDS_REVIEW`，不与确定不兼容混为同一阻断结果。
- `dependencyClosure` 结构损坏时返回稳定外部分析失败；闭包为空、替换范围无法可靠判定时返回 `needs_input / NEEDS_REVIEW`。两条路径都不调用生成或上传 adapter。
- 来源组件图和图片操作必须使用机器合同中的完整字段形状。具名依赖闭包与操作目标精确一致；接触和遮挡关系显式进入保持列表。P2 对每个操作检查目标清除、稳定锚点、关系保持和非目标漂移，任一失败都不创建确认图。详细规则读取 `multi-instance-image-operations.md`。
- 普通真人、公众人物和知名 IP 额外提供具名身份路由、完整重绘依据、双向绑定的 distinctIdentity 证据、带组件 ID 的身份拓扑、身份文字决策和自主冻结项冲突证据。公众人物与知名 IP 的同类候选必须带身份锚点、反锚点和玩法融合要求；空身份、同值/同义身份、缺少 full-body、拓扑/闭包不完全相等、文字组件类型错绑或互斥冻结命令都在生图前停止。同步文字通过具名关系类型绑定新身份，可使用英文名、团名、称谓、号码、识别色或徽标。
- P2 的 `identityTextEvidence` 必须与当前身份路由适用性一致；旧身份残留或新身份文字不同步都派生为视觉硬失败。subject 上传 type 与 primary-subject role 双向配对，并作为身份是否开放的判定事实。主体开放时，具体新身份只能出现在主体槽默认态；标题、描述、非主体槽完整文案、自由内容、固定 Prompt 片段和隐藏层均保持中性，并接受内容摘要绑定的独立语义审计。主体固定且身份文字同步时，描述和固定 Prompt 可以使用新身份。
- 视觉审核必须绑定当前 Generation Package SHA、生成图 SHA、完整证据 SHA、检查方法版本和时间；模板分析必须绑定当前 Approved Template Image SHA。详细规则读取 `template-image-gate.md`。
- 语义审计必须绑定标题、Prompt、隐藏层、自由内容和全部槽位值的规范摘要，并通过机器规则声明的完整审计项；推荐项的同轴、同颗粒度和可生成性属于该独立语义审计。adapter 只接收隔离的只读快照；返回后请求摘要或核心编译摘要发生变化时返回稳定外部失败，P4/P5 产物不能与 P8 投影分叉。
- Generation Package 与审核绑定由工作流核心保留不可变快照；生成 adapter 接收隔离的 Generation Package，视觉审核 adapter 接收当前摘要与 `imageOperations` 隔离副本，按动态 operation ID 逐项返回证据。返回结果必须重新绑定核心持有的 request ID 与摘要；adapter 改写审核请求会使证据失效。
- 生成数量默认为 1；显式选项与主输出索引经预检后进入 Production Item 身份。核心在 submit 前冻结 generation task 并落盘 prepared WAL，submit 后先持久化 provider request ID 再轮询。恢复、失败分类和完成态语义对账读取 `generation-execution-and-recovery.md`。
- 来源分析、生成、视觉审核、模板分析、语义审计和上传 adapter 都必须返回对象；非对象结果统一转为稳定外部失败。所有图片型 adapter 只接收字节一致的只读临时快照，核心 source、candidate 和 approved 路径不暴露。快照改写、删除、类型替换或调用前后核心摘要变化都会使当前证据绑定失效并停止后续副作用。
- 生成结果先以 `generated-candidate-image` 保存；只有六维视觉合同和全部硬门禁通过后，才能登记为 Approved Template Image。
- 硬失败停止在 P2，人工意见不能绕过门禁。
- 清晰通过由工作流自主确认；身份歧义、审美风险或证据不足返回人工复核且不创建确认图。
- P2 视觉硬失败可创建新的不可变 revision，只重做生成与视觉审核；新 generation package 使用新 request ID，旧 P2 证据和精确失效事件保留在 manifest。
- P7 adapter 只能接收当前 Approved Template Image；核心同时对账同 revision 的候选图摘要。Asset Receipt 必须绑定 Production Item、正式 revision、候选/确认图路径与 SHA、远端对象身份、对象键、请求状态、provider request ID 和公网 HTTPS URL，详细规则读取 `oss-finalization.md`。
- P7 恢复先验证已有产物谱系和完整 Asset Receipt，再直接执行 P8 正式投影；图片 SHA、revision、对象身份、对象键或 URL 不一致时阻断，恢复路径不调用生成或上传 adapter。远端 PUT 成功而 receipt 尚未落盘时，真实 adapter 通过同一对象键与 SHA metadata 对账复用对象。
- 完整生产调用授权门禁后的 P7 上传，不授权数据库导入、管理台写入、发布、Tag 或生产上线。

## 4. 版本 pin

每个 Production Item 单独保存 Skill 行为版本、Artifact Schema 版本、完整 tracked-file release digest、机器规则 SHA、Gallery Contract 快照 SHA 和上游来源 SHA。运行中的 pin 不读取未来安装版本。视觉硬失败创建后续 revision 时，先验证并复用 P0/P1，再为新 Generation Package 创建新 task 和 WAL。轮询中断恢复复用冻结 task 与 provider request ID，不重复 submit。

## 5. 迁移证据

确定性 tracer fixture 位于 `fixtures/e2e/simple-animal/`，身份路由场景位于 `fixtures/e2e/identity-routes/`，文字卡、长海报和身份界面场景位于 `fixtures/e2e/text-dense/`，多实例与图片操作场景位于 `fixtures/e2e/multi-instance/`。Issue #2 测试覆盖 E01、E04、E05、E07、E10、E11、E19、E21、E27、E35、E36 和 E38；Issue #3 覆盖 E05、E06、E07、E10 和 E25；Issue #4 覆盖 E06、E10、E11、E12、E13、E29 和 E34；Issue #5 覆盖 E18、E19、E20、E21、E22、E24、E25、E26、E27、E28、E30 和 E31；Issue #6 覆盖 E30、E31、E35、E36、E38 和 E39；Issue #7 的三类身份路由、候选卡、依赖闭包、身份文字硬失败与中性默认态覆盖 E06、E07、E08、E09、E19、E20、E26 和 E28；Issue #8 的文字区域清单、角色/操作分类、长文 span、精确文字保真和次要文字全文编辑覆盖 E14、E15、E16、E17、E18、E24、E27 和 E28；Issue #9 的多实例组件图、五类图片操作、接触遮挡保持与四类独立计数覆盖 E06、E12、E20、E22、E23 和 E29；Issue #10 的默认单图、冻结任务、request ID WAL、失败分类、断点恢复、真实 FAL adapter 和视觉硬失败重做覆盖 E10、E12、E13、E29 和 E34；Issue #11 的唯一 Approved Image 上传、完整 receipt、远端对象恢复和 `cover === referenceImage` 覆盖 E35 与 E36；Issue #12 的默认批量隔离、显式共享策略、稳定分配、逐项失败/恢复和跨图事实串线阻断覆盖 E01、E02、E03 和 E32。

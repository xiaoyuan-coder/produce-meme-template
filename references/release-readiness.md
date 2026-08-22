# 真实影子批次与 live_external 发布准备合同

本合同只适用于候选锁声明 `live_external` 的发布。`compatible_minor` 的确定性资格证据、全新安装和 doctor 规则读取 `release-doctor-install.md`。

## 1. 两层执行证据

发布准备使用同一个 `run_release_readiness` 公共 seam，区分两种执行模式：

模式枚举与 live 审核方法集合统一读取 `productionExecutionContract`；本合同只声明影子场景覆盖、报告和候选晋升规则。发布 readiness 的 live 结果属于候选资格证据，不直接赋予其中 Production Item 业务交付资格；正式业务交付仍执行 `production-execution-authority.md` 的已安装 runtime 与导出门禁。

- `recorded_replay`：对真实来源图、已复核 Approved Template Image 和固定分析证据进行公共工作流回放。它验证代码、谱系、正式投影和 T1，不代表外部供应商或 OSS 已执行。
- `live_external`：使用 Fal 队列生成与阿里云 OSS 上传执行同一批请求，并为每个新生成图注入独立 live 审核 adapter。来源分析可以复用同一来源图的录制证据；视觉硬门禁、Approved 分析、语义审计和 T1 复核必须针对本次 live 图片重新执行。仅该模式全部完成时，报告的 `releaseEligible` 才能为 `true`。

任何 adapter 都必须声明自己的执行模式。请求中的模式与 adapter 不一致时，在创建输出目录之前拒绝。缺少凭证时记录缺失的机器角色，禁止把录制回放换名为真实执行。live 能力只能由 `LiveShadowReadinessAdapters` 完整构造器在核心登记；核心直接调用已登记的官方 Fal→Aliyun 组合，忽略调用方覆盖的场景 factory。live 谱系还必须同时出现 Fal WAL、阿里云 OSS Receipt 和机器允许的独立审核方法；录制审核方法即使重绑图片 SHA 也不能满足 live 门禁。

## 2. 固定 corpus 与风险采样

固定 corpus 必须同时包含普通真人、知名 IP、通用动物、通用物件、文字密集画面和复杂多实例构图。每个角色拥有唯一 Production Item ID、Template Key、来源图 SHA 和 Approved Image SHA。`recorded_replay` 每次回放全部六类，用于证明低成本全量回归、谱系和正式投影仍成立。

`live_external` 使用风险采样组合执行四个场景：普通真人、文字密集画面和复杂多实例构图是必测项，再从知名 IP 与通用动物中选择一项补充。通用物件和未被选中的补充项继续由固定回放与历史经验 fixture 覆盖。旧 Unified 产物只作为已迁移规则和历史方法证据，不能代替本次新生成图片的 live 审核。

文字密集场景按被选中的主视觉文字判定替换完整性。与身份无关、不承担主视觉的可读长文字可以保留；具有编辑价值时，它以 `free_editable` 进入 Prompt Template 和 `freeEditableContent`，不新增 `inputSchema` 槽位。验收同时核对两种并列交互：槽位编辑默认保留这些文字，整段编辑接受用户对其直接改写。

另外保留一张不在六类固定 corpus 中出现的来源图，用于安装后的最小上下文前向测试。所有 corpus 路径、SHA、来源页面、许可证和替换值由 `releaseReadinessContract` 声明的字段形状解析；实际生成 Prompt 只读取工作流冻结的 Generation Package，避免录制图片创作说明成为第二份执行事实。

## 3. 逐项谱系与正式投影

每个场景必须通过公共 `run_production` seam，并对账 Production Pin、Source Image、Source Analysis、Replacement Plan、Generation Package、Generation Task、Generation WAL、Generated Candidate、Visual Review、Approved Template Image、Approved Analysis、Editable Spec、Validation Report、Formal Draft、Asset Receipt、Final Validation Report 和正式 Gallery Template。

对账要求：

- manifest 记录的 SHA 与当前普通文件一致；
- 来源图和 Approved Image 与 corpus SHA 一致；
- 来源类别与场景角色一致；
- 正式 JSON 顶层字段精确等于 `formalProjection.topLevel`；
- `cover === referenceImage`，且 Final Validation Report 对同一事实给出 PASS。

## 4. T1 与恢复

机器合同使用固定盐值对本次执行所得的正式模板做可重放抽样。录制回放从六份模板中抽样，live 风险采样从四份模板中抽样。每份抽中模板同时执行一个槽位编辑 case 和一个自由编辑 case。槽位值从当前 `inputSchema` 取得非空建议或选项字面值；自由 Prompt 使用带场景角色的全新字面输入。

T1 输出与生产项目录互为同级隔离目录。重跑必须复用已绑定的生产、生成、上传与 T1 产物；禁止因重跑增加外部提交或上传副作用。

## 5. 报告和外部风险候选冻结

`release-readiness-report.json` 写在 runtime 之外，通过同目录临时文件、`fsync` 和排他硬链接原子发布。请求 ledger 在任何外部调用前冻结；完成 sidecar 绑定 request SHA、corpus SHA 和 report SHA。重跑遇到完成报告时先重放 ledger、完成 sidecar、逐场景 Production Manifest/谱系、正式 JSON 与 T1 报告，全部一致才直接复用，任一缺失或漂移均在 adapter 调用前返回不可变冲突。同一内容可幂等复用，已有不同内容视为不可变冲突。输出路径不得包含 `..`、符号链接或 runtime 内部路径。稳定候选的 `stage` 还要把 code review 比较点写入 release lock，并用 Git 确认它是被审 HEAD 的不同祖先。

冻结 `1.0.0`、SemVer major 变化或主动声明外部风险的候选，要求六类录制回放谱系、正式投影、T1、未见图前向测试、四场景 `live_external` 风险采样、历史经验回归、全量测试、候选包构建、安装 smoke 和 doctor 全部 PASS，同时没有未结案的 P1/P2 review finding。稳定版候选通过 `stage_release` 生成；公共 `promote_release` 只接受经 `verify_release_readiness_completion` 深度重放为有效的完成工作区，并把候选逐字节晋升到公开 dist。

最终 live 请求通过 `releaseGateEvidence` 绑定已验证发布包、期望 release digest、全新安装 runtime、由该安装副本产出的未见图 Production Item，以及 Standards/Spec 两轴 clean review receipt。请求同时冻结前向 Manifest 与两份 receipt 的 SHA。每份 receipt 分别记录 code review 的比较基线 `comparisonBaseGitCommit` 和实际被审提交 `reviewedGitCommit`；核心要求后者精确绑定安装 pin 中的 Git commit，并同时绑定同一安装 pin。核心在外部批次前后重新执行 package 与 installed runtime 的 doctor，对账两者 release digest、安装记录、完整 Production Manifest 谱系、安装 pin 和未见图来源 SHA。live readiness 直接复用并深度校验该安装副本的前向 Production Item，禁止再次提交同一未见图。该安装 pin 的 SHA 还必须与本次四个 live Production Items 及未见图项各自的 `production-pin.json` 摘要完全一致。布尔值或 adapter 自报的发布结论不参与 eligibility。

Runtime pin SHA 统一指向生产 seam 实际持久化的 `production-pin.json` 字节摘要。review receipt、release gate 与 Production Item 谱系共用该文件级事实，不使用另一种 JSON 排序方式重算逻辑内容摘要。

Promotion 由冻结 candidate 副本重放 completion，但未见图的来源路径和 corpus 必须继续锚定 release gate 中经 doctor 验证的 installed runtime。candidate 只负责重放规则，不把已安装谱系的来源路径改写为 candidate 内路径。

任一条不满足时，可以产出开发版 readiness 证据，但不得把 `releaseEligible` 写为 `true`，也不得执行 Tag、push 或正式发布。

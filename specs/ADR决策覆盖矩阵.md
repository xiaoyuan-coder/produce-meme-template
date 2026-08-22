# ADR 决策覆盖矩阵

## 1. 审计结论

本矩阵覆盖《新模板生产 Skill 实施规格》与当前流程优化中的 49 条 Implementation Decisions。审计区分三类落点：

- **ADR**：难以逆转、脱离上下文会令人意外、存在真实取舍的决定。
- **可变规则**：由唯一 reference、机器 Schema、compiler 或 fixture 管理，可以版本化演进。
- **发布配置**：由 release 配置和发布门禁管理，不另设 ADR。

ADR 0007 已被 ADR 0008 取代；当前 ADR 0001–0027 中共有 26 份生效决定。49 条实施决策全部获得唯一权威落点，未发现需要重新询问的高风险分支。

“已覆盖”只表示决定和规则所有者已经明确。尚未存在的 Schema、compiler、reference 和 fixture 仍需后续 ticket 实现。

## 2. 全量映射

| ID | 实施决定摘要 | 决定性质 | 唯一权威落点 | 覆盖状态 |
| --- | --- | --- | --- | --- |
| D01 | 独立仓库中的单一用户结果 Skill | 架构 | ADR 0011 | ADR 已覆盖 |
| D02 | 单图、批量和 T1 共用顶层入口，批量拆成独立生产项 | 架构与边界 | ADR 0001、0002、0012 | ADR 已覆盖 |
| D03 | 公共工作流是最高测试 seam，外部动作使用 adapter | 架构 | ADR 0012 | ADR 已覆盖 |
| D04 | P0–P8 是完整生产，P7 上传包含在调用授权内 | 外部副作用 | ADR 0013 | ADR 已覆盖 |
| D05 | T1 使用独立生命周期，只读现成正式 JSON | 生命周期边界 | ADR 0001、0012 | ADR 已覆盖 |
| D06 | P0–P8 领域阶段及复核、阻断和失败语义 | 公共状态合同 | ADR 0014；生命周期状态 Schema | 复合覆盖 |
| D07 | 外部结果沿用四态并携带稳定错误证据 | 公共结果合同 | ADR 0014；结果合同 Schema | 复合覆盖 |
| D08 | 每个生产项独立 pin、manifest、revision 和依赖图 | 版本与恢复架构 | ADR 0002、0015、0020 | ADR 已覆盖 |
| D09 | 单图策略、共享策略、自主策略的优先级和逐决定来源 | 可变业务规则 | `references/replacement-spec.md`；Replacement Plan Schema 与 fixtures | 规则落点已指定 |
| D10 | 来源组件操作、单一主要目标与依赖闭包 | 核心领域边界 | ADR 0004；`references/replacement-spec.md` | 复合覆盖 |
| D11 | Replacement Pool 与 Slot Suggestion Pool 分离 | 领域模型边界 | ADR 0016 | ADR 已覆盖 |
| D12 | 自主替换类别路由与 unknown | 可变业务规则 | `references/replacement-spec.md`；类别枚举与 fixtures | 规则落点已指定 |
| D13 | 真人、公众人物和 IP 的同类替换与身份文字闭包 | 可变业务规则 | `references/replacement-spec.md`；身份替换 fixtures | 规则落点已指定 |
| D14 | Generation Package 从 Replacement Plan 确定性编译 | 机器合同 | `references/replacement-spec.md`；Generation Package Schema 与 compiler | 规则落点已指定 |
| D15 | 默认一次一张、request ID WAL 与分类重试 | 恢复规则 | ADR 0015；生命周期与重试合同、恢复测试 | 复合覆盖 |
| D16 | 六维视觉合同和四类专项审核 | 可变质量规则 | `references/replacement-spec.md`；模板图分析 reference、Schema 与 fixtures | 规则落点已指定 |
| D17 | Skill 自主确认、风险升级和画面硬失败重做 | 审核边界 | ADR 0003、0005 | ADR 已覆盖 |
| D18 | P3 以后只采用确认模板图事实 | 事实权威 | ADR 0017 | ADR 已覆盖 |
| D19 | Template Analysis 的事实、组件、数量与合同内容 | 可变分析合同 | 模板图分析 reference、Template Analysis Schema 与 fixtures | 规则落点已指定 |
| D20 | 2–5 槽、常态约 3、真实单槽例外 | 可变产品规则 | 槽位设计 reference、Slot Spec Schema 与 fixtures | 规则落点已指定 |
| D21 | 主体优先及人物派生槽逐图评估 | 可变产品规则 | 槽位设计 reference 与人物槽 fixtures | 规则落点已指定 |
| D22 | 文字角色分类和三类文字槽准入 | 可变产品规则 | 文字角色 reference、文字区域 Schema 与 fixtures | 规则落点已指定 |
| D23 | 简洁默认值和文字容量上限 | 可变文案规则 | 用户可见文案 reference 与容量 fixtures | 规则落点已指定 |
| D24 | 开放主体时处理具体身份文字，不承诺自动联动 | 数据语义边界 | ADR 0018；文字 reference 与冲突审计 | 复合覆盖 |
| D25 | 中性标题与最大差异输入测试 | 数据语义边界 | ADR 0018；用户文案 reference 与标题 fixtures | 复合覆盖 |
| D26 | Prompt Template 包含结构化槽位和自由编辑内容 | 用户内容合同 | ADR 0006；Prompt compiler 与 fixtures | 复合覆盖 |
| D27 | Prompt Template 拥有最终用户内容权限 | 用户内容合同 | ADR 0006；hidden conflict auditor | 复合覆盖 |
| D28 | inputSchema 和 runtimeSemantics 从中间模型编译 | 机器合同 | ADR 0022；模板 JSON 编译 reference、v2 Schema 与 compiler | 复合覆盖 |
| D29 | targetInstances、inputBindings 与 visualContract 分工并执行开放内容冲突审计 | 可变编译规则 | ADR 0022；模板 JSON 编译 reference 与 conflict fixtures | 复合覆盖 |
| D30 | 正式 JSON 使用字段白名单 | 数据合同 | ADR 0010；正式投影 compiler | 复合覆盖 |
| D31 | v2 删除 `image.extract` 和手写 `promptEnhancement` | 数据合同适配 | ADR 0022；v2 Schema 与 fixture | 复合覆盖 |
| D32 | 旧生产 metadata 进入 sidecar | 数据边界 | ADR 0010；正式投影 compiler 与 sidecar Schema | 复合覆盖 |
| D33 | 使用 `cover`，拒绝 `coverUrl` 双写 | 上游合同 | ADR 0008；ADR 0007 已取代 | ADR 已覆盖 |
| D34 | 只上传确认模板图并将同一 URL 双字段回填 | 外部副作用与合同 | ADR 0008、0013；OSS finalizer contract | 复合覆盖 |
| D35 | Asset Receipt 支持上传幂等和 URL 复用 | 恢复架构 | ADR 0015；OSS reference 与恢复测试 | 复合覆盖 |
| D36 | 正式 JSON 与生产旁证分离，下游只读正式文件 | 数据与产物边界 | ADR 0010、0015 | ADR 已覆盖 |
| D37 | 正式记录默认 DRAFT，数据库与发布状态在范围外 | 上游合同与职责边界 | ADR 0009；正式合同快照与 fixtures | 复合覆盖 |
| D38 | 机器枚举、状态、白名单和 Schema 条件单一来源 | 维护架构 | ADR 0019 | ADR 已覆盖 |
| D39 | SKILL 主流程与一级 references 渐进披露 | Skill 架构 | ADR 0019 | ADR 已覆盖 |
| D40 | 确定性 scripts 与推理性 reference 分工 | Skill 架构 | ADR 0019 | ADR 已覆盖 |
| D41 | Skill、Artifact Schema 和 Gallery Contract 三类版本 | 版本架构 | ADR 0020 | ADR 已覆盖 |
| D42 | 不可变发布包和受控安装副本 | 发布架构 | ADR 0020 | ADR 已覆盖 |
| D43 | 生产前 doctor 校验安装与合同 | 发布门禁 | ADR 0020；doctor script 与 version-drift fixtures | 复合覆盖 |
| D44 | 上游正式合同使用只读快照和显式升级 | 合同治理 | ADR 0020 | ADR 已覆盖 |
| D45 | `0.1.0` 开发，影子运行通过后冻结 `1.0.0` | 发布配置 | `release.json`、发布检查和影子运行验收 | 配置门禁已指定 |
| D46 | 旧 Unified 与拆分版只作只读迁移事实源 | 仓库与迁移边界 | ADR 0011；历史经验迁移矩阵 | 复合覆盖 |
| D47 | 四个可恢复用户大阶段聚合 P0–P8，第二阶段调用 Fal API | 公共工作流与外部副作用 | ADR 0023；`references/vertical-slice-runtime.md` | 复合覆盖 |
| D48 | P1 语义/结构经验通过 Authoring Handoff 注入 P3，P3 以 Approved Image 做增量分析；默认批次五路并发、大阶段屏障与单项一次新请求 | 阶段事实边界与性能架构 | ADR 0025；`references/template-image-gate.md`；`references/shared-batch-policy.md` | 复合覆盖 |
| D49 | 正式生产与回放使用执行画像分权；正式模式要求已安装 runtime、Fal、独立审核和 Aliyun OSS，拆分导出重放交付资格 | 执行授权与交付门禁 | ADR 0027；`references/production-execution-authority.md` | 复合覆盖 |

## 3. ADR 边界检查

本轮没有将下列内容提升为 ADR：类别枚举、槽位数量、文字长度、instruction 字数、错误预算、字段必填条件和 fixture 期望。这些内容会随产品合同和模型能力演进，放入版本化 reference、Schema、compiler 或 fixture 更易维护。

当前生效 ADR 已覆盖仓库与公共工作流边界、阶段状态与谱系恢复、模板图事实权威、正式 JSON 合同、Authoring Handoff、生产前身份与作者审计，以及受信执行画像与交付资格。

## 4. 当前决策前沿

基于现有实施规格、用户确认口径和迁移证据，当前没有发现仍需用户选择的高风险分支。用户已于 2026-08-16 确认共享理解，本轮决策前沿为空。

生产价值的两个最高优先级枢纽已经对齐：

1. P1–P2 换出并确认正确的 Approved Template Image，建立下游唯一视觉事实。
2. P3–P6 将模板图事实编译为正确的用户编辑合同和正式模板 JSON。

OSS、版本、恢复和发布能力为这两个业务枢纽提供稳定交付与复现保障。

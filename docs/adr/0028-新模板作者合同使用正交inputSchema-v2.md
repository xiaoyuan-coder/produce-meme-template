# ADR-0028：新模板作者合同使用正交 inputSchema v2

- 状态：Accepted
- 日期：2026-08-24

## Context

研发将《Agent 模板 JSON 与运行时上下文合同》确立为新模板作者合同的 CURRENT 正本。新合同使用 `inputSchema: {version: 2, slots: [...]}`，每个槽位独立声明文字、图片或文图复合模式；它同时将固定重复身份的可执行绑定扩展为 `same_source_repeated`。研发导入器继续兼容旧数组形状，兼容读取不构成新产物的作者资格。

## Decision

新 Production Item 编译正交 `inputSchema` v2 对象。槽位稳定意图继续使用同一 `id`，文字与图片是该意图的两种输入模式。人物、环境和道具等运行职责只由 `runtimeSemantics.inputBindings` 与目标类型决定。

身份输入继续只接受一张已确认的单主体图。一个身份对应一个可见位置时使用 `one_to_one`；同一身份在模板中固定重复出现时，每个可见位置分别建模并使用 `same_source_repeated`。合照成员选择、多主体来源、动态人数和像素保留继续关闭。

内容图片只在上传图拥有明确且稳定的接管目标时开放。同一模板同时包含身份图和内容图时，作者分析必须绑定来源隔离裁决；正式编译将结果写入原子化的 `visualContract.relations`。编辑后容器依赖同样使用结构化作者旁证生成正向关系，不增加正式 Gallery 字段。

身份特征继承继续沿用现有裁决：上传图默认提供清晰可见的服装与配饰特征，只有构成模板核心玩法的具体特征可以最小范围进入 `keepFromTemplate`。

新合同中的约束账本、图片动态编号、默认值动作投影和 Planning LLM 属于研发运行时实现，本 Skill 不生成对应的推测字段。三组真实试跑继续属于发布前验证；T1 保持独立生命周期，不进入 P0–P8 完成门禁。

## Consequences

- 新增不可变上游快照，保留 2026-08-18 旧快照作为迁移证据。
- 正式 JSON 形状发生不兼容变化，Skill 行为使用 SemVer major 升级，内部 Artifact Schema 同步升级。
- P3 槽位候选与 P4 独立审计需要对输入模式达成一致；编译器只消费独立审计确认的模式。
- 正式投影仍保持 metadata 白名单、`cover === referenceImage`、`DRAFT`、`imageN = 1` 和 `kind = PROMPT`。

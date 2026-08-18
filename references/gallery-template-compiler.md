# 正式模板编译合同

## 1. 视觉事实来源

P3 以后只读取 Approved Template Image 的 SHA 绑定分析。Source Web Image 的身份、物种、文字和颜色作为 `forbiddenLegacyClaims` 参与泄漏检查，不参与标题、描述、槽位默认值、Prompt Template 或 `runtimeSemantics` 编译。

## 2. 高价值槽位与 Prompt Template

槽位必须使用唯一、非空且符合冻结 Gallery Schema `inputId.pattern` 的 ID；placeholder 解析直接派生同一 Schema 模式。槽位同时通过用户动机、画面可见、模型可控和机制保持四道具名布尔门禁。常态预算读取 `contracts/machine-rules.json`，为 2–5 个且通常约 3 个。分析必须明确提供 `hasPrimarySubject` 布尔值和机器合同内的 `subjectKind`；人物判别必须与主体存在性一致。有明显主体时默认包含主体槽；省略时保存四道门禁逐项结果，至少一项失败并附理由。仅有一个高价值槽位时，必须穷尽复核主体、内容物、颜色、文字、服装、道具、场景和嵌套内容，保存完整且无重复的复核轴与例外理由。

人物的服装、造型、发型、姿势和颜色分别记录四道门禁、是否入槽和图像证据；入槽决定必须与门禁结论一致。每次 Approved Template Image 分析都必须提供 `componentGraph` 与 `assetUnitAnalysis`，分别计数可见主体、身份单元、上传素材和控件，不从其中一个数量推导其他数量；控件数最终必须与通过门禁的槽位数一致。组件图、容器和关系的详细规则读取 [多实例组件图与图片操作合同](multi-instance-image-operations.md)。

默认值必须是非空用户文案，优先使用中文且原则上为 2–8 个字符；偏离语言或长度偏好时必须按槽位保存已复核结论与理由。硬上限为 12 个字符，只有 `exactVisibleTextEvidence` 同时绑定当前 Approved Template Image SHA、逐字默认值和非空图像证据的文字槽可以超过。每个推荐池必须非空；推荐项按 trim 后的用户可见值判断重复与默认值冲突，同轴、同颗粒度和可生成性继续由绑定完整内容摘要的独立语义审计判断。

Prompt Template 是完整自然语言描述：

- 引用全部结构化槽位；
- 保留没有入槽但仍有编辑价值的动作、背景和氛围；
- 允许用户全文改写；
- 不写生产过程术语；
- `runtimeSemantics.visualContract` 不恢复用户改写后的主体、文字、颜色、服装、道具或场景。

`editable-template-spec.json` 保存 `resolvedPromptContract`：`promptTemplate` 是槽位编辑与全文编辑的唯一用户可见文字源；每个 placeholder 必须携带与侧车 `defaultValue` 完全一致的内联默认值，默认槽位值代入后形成无残留 placeholder 的 `defaultResolvedPrompt`。次要可读文字具有编辑价值时留在 `freeEditableContent` 和 Prompt Template，不自动暴露为控件。

可见文字先依据 [可见文字区域与文字槽合同](visible-text-contract.md) 完整清点和分类。只有身份相关文字、主要视觉文字或长文高价值 span 可以成为文字槽；归因、水印、品牌、装饰微字和低价值海报信息不能占用槽位预算。token、换行、大小写、罕见符号和必要符号拓扑接受确定性门禁，实际语种与身份中性由内容摘要绑定的独立逐区域语义审计确认。

P6 必须确认全部 `freeEditableContent` 原样进入 Prompt Template，并分别代入槽位默认值与每个推荐值。所有代入结果都要解除 placeholder、保留完整句式与自然标点；只有占位符拼接的片段不属于完整 Prompt。完整句式的自然度属于推理性判断，由独立语义审计 adapter 复核并输出结构化证据。adapter 只接收深拷贝审计快照；调用前后摘要必须一致，原地变异按外部 adapter 失败处理。

Replacement Pool 保存于 `replacement-plan.json`，Slot Suggestion Pool 保存于 `editable-template-spec.json`。两个集合独立编译，正式 JSON 只包含产品合同支持的槽位建议。

## 3. runtimeSemantics

`runtimeSemantics.version` 固定为 `1`。Approved Template Image 分析必须逐个写出 `targetInstances` 的稳定 ID、可观察角色和明确空间区域；编译器保留这些作者事实，并与 Approved 组件图双向核对。subject 输入对应唯一 `identity_subject`，prompt 内容输入对应一个或一组 `content_element`；关键固定内容可以作为无输入绑定的 `content_element`。每个 input id 必须在 `inputBindings` 中出现且只能绑定类型匹配的目标。

subject binding 固定使用 `replace_identity + one_to_one + illustration_redraw + single_subject + reject`，并只接受一张单主体图片。内容 binding 使用 `replace_content`；单目标采用 `replace_as_unit`，需要保持空间组结构的多目标采用 `preserve_target_group`。当前正式合同不声明合照成员选择、像素保留或未经客户端确认的多主体能力。

`visualContract` 精确包含：

- `medium`：一句正向、可观察的绘制或摄影媒介；
- `styleTraits`：替换区与固定区共享的造型比例、线条、细节密度和材质语言；
- `composition`：画幅、裁切、位置、比例、留白和阅读顺序；
- `relations`：身份边界、接触、遮挡、承托、容器关系和逐模板服装裁决；
- `colorAndLight`：只有色光属于模板卖点时填写，允许空数组。

每个高价值槽位继续提供 `hiddenConflictTokens` 和 `titleForbiddenTokens`。确定性门禁拒绝 visual contract 锁回槽位默认值、建议值或自由编辑内容；独立语义审计复核目标—绑定职责、开放内容权限、身份中性和最大差异标题。审计 SHA 同时绑定标题、Prompt、runtimeSemantics、自由内容和全部槽位默认值/推荐值；任何被审计内容变化都会让旧结论失效。

## 4. 四层验收与正式投影

P6 分别记录 Schema、语义、视觉合同和 Gallery Contract 证据，`pass` 由四层结果共同推导。语义层要求 `semantic-audit.json` 合同有效、内容摘要双向一致，并通过机器规则列出的全部审计项；每个具名检查必须提供机器映射指定的结构化证据。Prompt 代入覆盖默认值和全部推荐场景，开放轴与推荐审查覆盖全部槽位，最大差异输入覆盖每个推荐池；runtimeSemantics 范围和目标—绑定职责对象严格匹配机器角色。空容器、标量占位或覆盖不完整都不能形成通过结论。P8 使用 `contracts/machine-rules.json` 的白名单投影，并以 `contracts/upstream/gallery-template/runtime-semantics-v2-contract/gallery-template.schema.json` 再次校验最终记录。该 Schema 是当前作者合同的只读快照，来源、取得时间、兼容范围和摘要记录在同目录 `snapshot-metadata.json`；投影白名单继续独立存在于机器规则中。

本 Skill 的 P8 生产投影固定输出：

`key`、`status`、`title`、`description`、`imageSize`、`imageN`、`kind`、`promptTemplate`、`inputSchema`、`preprocessSteps`、`runtimeSemantics`、`metadata.tags`、条件性的 `metadata.needsReview`、`cover`、`referenceImage`。

作者 Schema 中 `description`、`imageN`、`kind`、`preprocessSteps` 和 `metadata` 保持可选，并允许调用方分发使用的 `communityKey` 与 `featureKeys`；T1 可以测试这些合法形状。P8 继续输出上述稳定子集，不为生产项自行添加社区或专题归属。subject 的 `text` 仅保留 `allowCustom`、`defaultValue` 和三条 `suggestions`；上传提示只位于 `image.hint`。

`cover` 与 `referenceImage` 写入 Asset Receipt 中同一个 HTTPS URL。投影源若含未知顶层字段、未知 metadata、空 `needsReview` 或非 HTTPS URL，直接阻断；冻结 Schema 允许但业务白名单未开放的 metadata 同样不能静默进入正式记录。最终验证还拒绝 Data URL、文件 URL、临时/用户绝对路径、生成 request 字段和审计字段。

`coverUrl`、`promptEnhancement`、`inputSchema[].image.extract`、Replacement Pool、六维分析、推荐理由、版本 pin、候选策划和审计证据只保留在存量迁移输入或 sidecar。`fixtures/contracts/latest-gallery-samples/` 冻结研发已验证的 v2 正式样例及显式 expected 投影；全部标量叶子必须归入正式字段或机器声明的 metadata sidecar，未分类数保持为 0。expected 执行白名单投影和中性文案纠偏，并继续通过冻结 Gallery Schema 与最终业务门禁。

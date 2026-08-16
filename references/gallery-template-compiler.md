# 正式模板编译合同

## 1. 视觉事实来源

P3 以后只读取 Approved Template Image 的 SHA 绑定分析。Source Web Image 的身份、物种、文字和颜色作为 `forbiddenLegacyClaims` 参与泄漏检查，不参与标题、描述、槽位默认值、Prompt Template 或隐藏约束编译。

## 2. 高价值槽位与 Prompt Template

槽位必须使用唯一、非空且符合冻结 Gallery Schema `inputId.pattern` 的 ID；placeholder 解析直接派生同一 Schema 模式。槽位同时通过用户动机、画面可见、模型可控和机制保持四道具名布尔门禁。常态预算读取 `contracts/machine-rules.json`，为 2–5 个且通常约 3 个。分析必须明确提供 `hasPrimarySubject` 布尔值和机器合同内的 `subjectKind`；人物判别必须与主体存在性一致。有明显主体时默认包含主体槽；省略时保存四道门禁逐项结果，至少一项失败并附理由。仅有一个高价值槽位时，必须穷尽复核主体、内容物、颜色、文字、服装、道具、场景和嵌套内容，保存完整且无重复的复核轴与例外理由。

人物的服装、造型、发型、姿势和颜色分别记录四道门禁、是否入槽和图像证据；入槽决定必须与门禁结论一致。每次 Approved Template Image 分析都必须提供 `assetUnitAnalysis`，分别计数可见主体、身份单元、上传素材和控件，不从其中一个数量推导其他数量；控件数最终必须与通过门禁的槽位数一致。

默认值必须是非空用户文案，优先使用中文且原则上为 2–8 个字符；偏离语言或长度偏好时必须按槽位保存已复核结论与理由。硬上限为 12 个字符，只有 `exactVisibleTextEvidence` 同时绑定当前 Approved Template Image SHA、逐字默认值和非空图像证据的文字槽可以超过。每个推荐池必须非空；推荐项按 trim 后的用户可见值判断重复与默认值冲突，同轴、同颗粒度和可生成性继续由绑定完整内容摘要的独立语义审计判断。

Prompt Template 是完整自然语言描述：

- 引用全部结构化槽位；
- 保留没有入槽但仍有编辑价值的动作、背景和氛围；
- 允许用户全文改写；
- 不写生产过程术语；
- 隐藏层不恢复用户改写后的主体、文字、颜色、服装、道具或场景。

`editable-template-spec.json` 保存 `resolvedPromptContract`：`promptTemplate` 是槽位编辑与全文编辑的唯一用户可见文字源；每个 placeholder 必须携带与侧车 `defaultValue` 完全一致的内联默认值，默认槽位值代入后形成无残留 placeholder 的 `defaultResolvedPrompt`。次要可读文字具有编辑价值时留在 `freeEditableContent` 和 Prompt Template，不自动暴露为控件。

P6 必须确认全部 `freeEditableContent` 原样进入 Prompt Template，并分别代入槽位默认值与每个推荐值。所有代入结果都要解除 placeholder、保留完整句式与自然标点；只有占位符拼接的片段不属于完整 Prompt。完整句式的自然度属于推理性判断，由独立语义审计 adapter 复核并输出结构化证据。adapter 只接收深拷贝审计快照；调用前后摘要必须一致，原地变异按外部 adapter 失败处理。

Replacement Pool 保存于 `replacement-plan.json`，Slot Suggestion Pool 保存于 `editable-template-spec.json`。两个集合独立编译，正式 JSON 只包含产品合同支持的槽位建议。

## 3. 隐藏约束

`instruction` 只写媒介、模板卖点和可选色彩，字符上限、生产禁词与署名/品牌/水印等越界词读取机器规则。`lockedConstraints` 锁定画幅、媒介、造型、材质和接触等呈现维度；`preserve` 保存编辑后仍需成立的语义关系。两层都必须是非空、无重复的字符串列表，精确内容不得交叉；同义职责重叠由独立语义审计判断。每个高价值槽位必须提供 `hiddenConflictTokens` 和 `titleForbiddenTokens`，为确定性字面检查提供第一层证据。P6 还调用独立语义审计 adapter，判断 instruction 范围、隐藏层职责、同义锁回和最大差异标题。审计 SHA 同时绑定标题、Prompt、隐藏层、自由内容和全部槽位默认值/推荐值；任何被审计内容变化都会让旧结论失效。

## 4. 四层验收与正式投影

P6 分别记录 Schema、语义、视觉合同和 Gallery Contract 证据，`pass` 由四层结果共同推导。语义层要求 `semantic-audit.json` 合同有效、内容摘要双向一致，并通过机器规则列出的全部审计项；每个具名检查必须提供机器映射指定的结构化证据。Prompt 代入覆盖默认值和全部推荐场景，开放轴与推荐审查覆盖全部槽位，最大差异输入覆盖每个推荐池；instruction 范围和隐藏层职责对象严格匹配机器角色。空容器、标量占位或覆盖不完整都不能形成通过结论。P8 使用 `contracts/machine-rules.json` 的白名单投影，并以 `contracts/gallery-template.schema.json` 再次校验最终记录。该 Schema 是当前上游合同的逐字节只读快照，SHA-256 为 `1ebe5cb0790fa20e5968570c7b09d83d7c14b9347bcf5e60ca612384a3a81619`；投影白名单继续独立存在于机器规则中。

正式记录只允许：

`key`、`status`、`title`、`description`、`imageSize`、`promptTemplate`、`inputSchema`、`promptEnhancement`、`metadata.tags`、条件性的 `metadata.needsReview`、`cover`、`referenceImage`。

`cover` 与 `referenceImage` 写入 Asset Receipt 中同一个 HTTPS URL。`coverUrl`、Replacement Pool、六维分析、推荐理由、版本 pin、候选策划和审计证据只保留在 sidecar。

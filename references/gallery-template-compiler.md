# 正式模板编译合同

## 1. 视觉事实来源

P3 以后只读取 Approved Template Image 的 SHA 绑定分析。Source Web Image 的身份、物种、文字和颜色作为 `forbiddenLegacyClaims` 参与泄漏检查，不参与标题、描述、槽位默认值、Prompt Template 或隐藏约束编译。

## 2. 高价值槽位与 Prompt Template

槽位必须同时通过用户动机、画面可见、模型可控和机制保持四道价值门禁。常态预算读取 `contracts/machine-rules.json`，Issue #2 fixture 产出三个槽位并包含主要主体槽。

Prompt Template 是完整自然语言描述：

- 引用全部结构化槽位；
- 保留没有入槽但仍有编辑价值的动作、背景和氛围；
- 允许用户全文改写；
- 不写生产过程术语；
- 隐藏层不恢复用户改写后的主体、文字、颜色、服装、道具或场景。

P6 必须确认全部 `freeEditableContent` 原样进入 Prompt Template，并分别代入槽位默认值与每个推荐值。所有代入结果都要解除 placeholder、保留完整句式与自然标点；只有占位符拼接的片段不属于完整 Prompt。完整句式的自然度属于推理性判断，由独立语义审计 adapter 复核并输出结构化证据。

Replacement Pool 保存于 `replacement-plan.json`，Slot Suggestion Pool 保存于 `editable-template-spec.json`。两个集合独立编译，正式 JSON 只包含产品合同支持的槽位建议。

## 3. 隐藏约束

`instruction` 只写媒介、模板卖点和可选色彩，字符上限和禁用词读取机器规则。`lockedConstraints` 锁定画幅、媒介、造型、材质和接触等呈现维度；`preserve` 保存编辑后仍需成立的语义关系。每个高价值槽位必须提供 `hiddenConflictTokens` 和 `titleForbiddenTokens`，为确定性字面检查提供第一层证据。P6 还调用独立语义审计 adapter，判断同义改写后的隐藏层是否锁回开放轴，并以最大差异输入复核标题。审计 SHA 同时绑定标题、Prompt、隐藏层、自由内容和全部槽位默认值/推荐值；任何被审计内容变化都会让旧结论失效。

## 4. 四层验收与正式投影

P6 分别记录 Schema、语义、视觉合同和 Gallery Contract 证据，`pass` 由四层结果共同推导。语义层要求 `semantic-audit.json` 合同有效、内容摘要双向一致，并通过机器规则列出的全部审计项。P8 使用 `contracts/machine-rules.json` 的白名单投影，并以 `contracts/gallery-template.schema.json` 再次校验最终记录。该 Schema 是当前上游合同的逐字节只读快照，SHA-256 为 `1ebe5cb0790fa20e5968570c7b09d83d7c14b9347bcf5e60ca612384a3a81619`；投影白名单继续独立存在于机器规则中。

正式记录只允许：

`key`、`status`、`title`、`description`、`imageSize`、`promptTemplate`、`inputSchema`、`promptEnhancement`、`metadata.tags`、条件性的 `metadata.needsReview`、`cover`、`referenceImage`。

`cover` 与 `referenceImage` 写入 Asset Receipt 中同一个 HTTPS URL。`coverUrl`、Replacement Pool、六维分析、推荐理由、版本 pin、候选策划和审计证据只保留在 sidecar。

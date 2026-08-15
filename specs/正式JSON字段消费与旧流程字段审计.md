# 正式 JSON 字段消费与旧流程字段审计

## 1. 结论

两份最新样例混合了正式运行字段、平台展示字段和旧生产审计字段。新 Skill 应在 P8 执行正式投影，只把正式合同与产品展示真正需要的字段写入 `gallery-template.json`；候选分析、规则解释、版本能力和视觉审计移入 sidecar。

对两份样例执行最小 metadata 投影后仍通过当前 Schema：

| 样例 | 原 JSON 字符数 | 投影后字符数 | 减少 |
| --- | ---: | ---: | ---: |
| 他被画进一颗大爱心 | 4266 | 2219 | 48.0% |
| 带着猫咪一起拍婚纱照 | 5905 | 2338 | 60.4% |

测试投影只保留 `metadata.tags`，同时把正式字段中的“候选图”改成“参考图/模板参考图”；两个结果均通过 `/Users/xiaoyuan/Downloads/schema.json`。

### 1.1 全字段覆盖验证

为排除只检查顶层字段造成的遗漏，审计脚本遍历了两份样例的全部标量叶子路径，并对数组下标与动态槽位名进行了归一化。结果如下：

| 归类 | 路径数 |
| --- | ---: |
| 用户可见产品字段 | 8 |
| 明确运行字段 | 8 |
| 运行或界面共同消费字段 | 19 |
| Schema 要求的留档字段 | 1 |
| 移入生产 sidecar 的字段 | 74 |
| **合计** | **110** |
| **未归类** | **0** |

唯一的“Schema 要求留档字段”是 `inputSchema[].image.extract`。`metadata.needsReview` 没有出现在这两份样例中，但当前 Schema 与导入语义支持它，因此白名单将其作为条件字段保留。当前合同范围内不存在未处理字段。

## 2. 明确参与生成或运行的字段

| 字段 | 作用 | 依据 |
| --- | --- | --- |
| `promptTemplate` | 渲染用户最终自然语言提示词 | Schema 标记“可执行字段” |
| `inputSchema` | 定义用户输入、槽位控件和图片绑定 | Schema 标记“可执行字段” |
| `inputSchema.subject.text` | 文字默认值、placeholder 和推荐项 | subject 合同 |
| `inputSchema.subject.image.promptValue` | 图片模式下注入 Prompt 的中性主体描述 | Schema 明确说明 |
| `inputSchema.subject.image.hint` | 用户上传提示 | 前端字段 |
| `maxCount/minWidth/minHeight/private/sourceOptions` | 上传数量、尺寸、隐私和来源能力 | subject 合同 |
| `promptEnhancement.stageKey` | 选择改写 stage | Schema 要求 |
| `instruction` | 模板独有媒介、卖点和可选色彩 | promptEnhancement 合同 |
| `referenceField` | 指向 `referenceImage` | promptEnhancement 合同 |
| `lockedConstraints` | 结构和风格硬约束 | Schema 明确说明 |
| `preserve` | 语义锚点 | Schema 明确说明 |
| `output` | finalPrompt 输出契约 | Schema 必填 |
| `referenceImage` | 模板固定参考图 | Schema 明确说明 |

## 3. 平台记录与展示字段

| 字段 | 作用 | 新 Skill 处理 |
| --- | --- | --- |
| `key/status/title/description/imageSize` | 标识、状态、用户可见文案和尺寸 | 保留 |
| `cover` | 正式封面资源 | 保留，并与 `referenceImage` 使用同一 URL |
| `metadata.tags` | 分类、审核页展示和未来筛选 | 保留 |
| `metadata.needsReview` | 导入时强制 DRAFT 的人工复核原因 | 仅在确有复核原因时保留 |

## 4. 合同要求留档、但不参与生成的字段

### `inputSchema[].image.extract`

Schema 将该字段定义为“主体身份抽取语义留档”，同时列为 `subjectImageConfig` 必填字段。因此：

- 正式 JSON 继续保留。
- 文本保持简短，只说明要读取的身份信息。
- 使用“模板参考图”称呼最终参考资产，不使用“候选图、来源图、生产阶段”等旧流程词。
- 不把关键生成效果只写在 extract 中。

建议形态：

```text
提取该主体可辨识的身份特征，并在模板参考图的媒介与造型体系中重绘。
```

两份最新样例的 `image.hint`、`image.extract` 和 `instruction` 各自仍写“候选图”。正式投影统一替换为“模板参考图/参考图”；其中 `hint` 是用户可见字段，必须通过生产术语泄漏门禁。

## 5. 应移出正式 JSON 的旧流程字段

以下字段位于开放 metadata 中，Schema 允许保存，但当前生成合同没有消费证据：

| 字段 | 原用途 | 新落点 |
| --- | --- | --- |
| `metadata.semanticContext` | 旧分析语义、visualHook、受众与用途 | `template-analysis.json` / `editable-template-spec.json` |
| `metadata.semanticContext.candidateScope` | 旧候选策划范围 | `replacement-plan.json` 或 fixture |
| `metadata.runtimeRequirements` | 旧运行能力声明 | `production-pin.json` / release capability contract |
| `metadata.templateSource` | 来源图权限留档 | `template-analysis.json` |
| `metadata.inputSemantics` | 旧前端校验、推荐项解释和 fallback 副本 | `editable-template-spec.json` |
| `metadata.inputSemantics.*.candidateScope` | 候选与推荐范围 | `editable-template-spec.json` |
| `suggestionAxis`、`suggestionRationales` | 推荐项生成与审核理由 | `editable-template-spec.json` / audit sidecar |
| `semanticType/slotRole/componentId/property/operation/renderingMode` | 旧中间槽位模型 | 内部 SlotSpec，不进入正式投影 |
| `metadata.optimizationAudit` | 843 条修订时的六维视觉与图片操作审计 | `template-analysis.json` / `production-manifest.json` |

本地管理台 `template-review.ts` 会读取 `metadata.inputSemantics` 作为 uploadLabel/defaultValue 的兜底；当前两份样例已经在 `inputSchema` 和 Prompt Template 中提供正式值，因此删除该 metadata 不影响审核页主要展示。生成后端消费证据仍以 Schema 和正式链路为准。

## 6. 正式投影白名单

新 Skill 的 `gallery-template.json` 默认只输出：

```text
key
status
title
description
imageSize
promptTemplate
inputSchema
promptEnhancement
metadata.tags
metadata.needsReview（仅在存在时）
cover
referenceImage
```

所有生成前分析、替换方案、候选选择理由、六维视觉事实、推荐项理由、版本能力和审核证据继续落盘，但只存在于生产 sidecar。

## 7. 验收门禁

正式投影需要同时满足：

1. 当前 Schema PASS。
2. `cover === referenceImage`。
3. 所有 Prompt Template placeholder 都能由 `inputSchema` 或字面 fallback 解析。
4. `promptEnhancement` 完整且无开放内容冲突。
5. 正式 JSON 不含键名 `candidateScope`、`optimizationAudit`、`runtimeRequirements`、`suggestionRationales`。
6. 正式 JSON 的运行字段不出现“候选图、来源图、作者源、revision、审核”等生产过程词；必须表达参考资产时使用“参考图”或“模板参考图”。
7. sidecar 的删除不会改变正式 JSON 的生成语义和用户展示。

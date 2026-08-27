# 正式模板编译合同

模板身份、编辑权限、标题、槽位全部属性、Prompt Template 与逐图 `runtimeSemantics` 的作者裁决统一读取 [模板身份、编辑权限与正式字段编写规范](template-authoring.md)。本文只定义这些作者事实进入确定性编译、四层验收和正式投影时的机器边界。

## 1. 视觉事实来源

P3 以后只读取 Approved Template Image 的 SHA 绑定分析。Source Web Image 的身份、物种、文字和颜色作为 `forbiddenLegacyClaims` 参与泄漏检查，不参与标题、描述、槽位默认值、Prompt Template 或 `runtimeSemantics` 编译。

## 2. 高价值槽位与 Prompt Template

槽位必须使用唯一、非空且符合冻结 Gallery Schema `inputId.pattern` 的 ID；placeholder 解析直接派生同一 Schema 模式。作者侧四道价值门禁、槽位属性语义和单槽穷尽复核读取 `template-authoring.md`。Artifact Schema `0.25.0` 起，`slotCandidates[].valueGates` 不再直接决定槽位入选；编译器只消费 `authoring-contract-audit.json` 中绑定 Approved Image SHA、当前组件 ID 和四道独立结论的复核。subject 候选可选提供 `imagePromptValue/imageHint` 短文本，缺失时由机器合同回填通用单主体说明；`promptValue` 仅作为图片模式的中性 LLM 说明，`hint` 仅用于上传 UI，两者都不代替 target/binding。每个 subject 候选还必须提供 `identityInheritanceDecision`：默认继承上传图清晰可见的身份特征，只将参与模板核心玩法的特征列为模板固定例外。常态预算读取 `contracts/machine-rules.json`。分析必须明确提供 `hasPrimarySubject` 与机器合同内的 `subjectKind`，人物判别与主体存在性保持一致。

人物的服装、造型、发型、姿势和颜色分别记录四道门禁、是否入槽和图像证据；入槽决定必须与门禁结论一致。每次 Approved Template Image 分析都必须提供 `componentGraph` 与 `assetUnitAnalysis`，分别计数可见主体、身份单元、上传素材和控件，不从其中一个数量推导其他数量；控件数最终必须与通过门禁的槽位数一致。组件图、容器和关系的详细规则读取 [多实例组件图与图片操作合同](multi-instance-image-operations.md)。

Artifact Schema `0.24.0` 起，Authoring Handoff 增加 `subjectEditIntent`，显式传递来源主体身份单元、主体组件、`repeated_identity` 关系与建议绑定模式。Artifact Schema `0.28.0` 起，P4 审计请求绑定主体存在性上下文：`hasPrimarySubject`、Approved 可见身份组件与身份单元、Handoff 主体数量与绑定模式、subject 候选和主体省略证据必须同时进入独立审核；编译和交付重放使用同一上下文再次 fail-closed。主体省略 blocker 除了字段完整，还必须通过代码对应的可计算前提。`inseparable_multi_identity_unit` 至少需要两个身份单元，且将成员分别开放为 subject 后必须超过槽位硬上限；条件不成立时直接阻断。旧 `reason` 字段保持为无效迁移输入，适配器不再将它静默升级为合格类型证据。

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

`slotSuggestionReviews` 保存逐槽对象列表。每个对象精确绑定 `slotId`、`defaultValue`、自由文本 `axis`、自由文本 `granularity`、槽级 `evidence` 和三条 `suggestionReviews`；每条推荐精确绑定当前 `value`，并要求 `sameAxis`、`sameGranularity`、`mechanismCompatible` 全部为 `true`，同时提供非空 `evidence`。验证器拒绝旧式 slot ID 列表、槽位缺失或重复、未知字段、未知槽位、当前值不一致、空证据和否定结论。确定性 adapter 从逐图 fixture 读取轴、颗粒度与逐值结论，外部语义审计 adapter 通过同一字段合同提交证据；正式 Gallery 记录形状保持不变。Artifact Schema `0.21.0` 起使用该证据结构，旧 pin 继续由对应已安装旧 runtime 解释。

Replacement Pool 保存于 `replacement-plan.json`，Slot Suggestion Pool 保存于 `editable-template-spec.json`。两个集合独立编译，正式 JSON 只包含产品合同支持的槽位建议。

`metadata.tags` 在 P4 执行逐图独立审核，详细编写规则读取 `template-authoring.md`。审核请求同时绑定 Approved Image SHA、当前图标题、描述和标签；审核结果必须与当前标签精确一对一，每项都确认图像事实根据和分类价值。标签固定为 5–8 项，至少一项来自机器合同列出的十一大类；泛标签、重复标签、批次复用且无单图证据的标签不能进入 draft。`description` 固定为 20 个字符以内，并在作者审计和最终 JSON 验证两处执行。

## 3. runtimeSemantics

`runtimeSemantics.version` 的新生产写版本固定为 `2`，兼容读取版本为 v1/v2。Approved Template Image 分析必须逐个写出 `targetInstances` 的稳定 ID、可观察角色和明确空间区域；编译器保留这些作者事实，并与 Approved 组件图双向核对。固定 subject 输入对应 `identity_subject`，动态合照输入对应一个 `identity_group`，prompt 内容输入对应一个或一组 `content_element`；关键固定内容可以作为无输入绑定的 `content_element`。每个 input id 必须在 `inputBindings` 中出现且只能绑定类型匹配的目标。

Artifact Schema `0.22.0` 起，P3 还必须提交 `renderingCoherenceDecision`。它把当前组件图完整划分为统一或刻意混合的渲染单元，并为每个 subject 精确绑定 target、身份继承范围、模板保留范围和完整重绘结论。编译器只从这份决策生成正式 `visualContract.medium/styleTraits`；组件遗漏、单元重叠、混合媒介缺少边界依据、subject 未完整重绘或权限与槽位不一致都会在 draft 前阻断。该 sidecar 不改变正式 Gallery Schema。

固定 subject binding 使用 `replace_identity + one_to_one|same_source_repeated + illustration_redraw + single_subject + reject`。`groupStrategyDecisions` 必须精确覆盖实际 binding：两个以上 `one_to_one` 走 `independent_subjects`，一个输入绑定多个同源身份目标走 `same_source_repeated`，文字输入绑定多个内容目标走 `descriptive_content_group`，动态合照走 `dynamic_group_photo`。只有真实身份整组上传、人数可变、成员同类且无需逐角色寻址时，才使用一个 `identity_group` 和 `preserve_group + group_photo + reject`。成员类别只允许 person/pet，人数范围满足 `1 <= minMembers < maxMembers <= 20`。对应 `inputSchema` 必须是纯图片槽；编译器会清除 `text` 与 `resolutionStrategy`，最终验证器也会拒绝外部写回的复合槽。每个身份 binding 都由 `identityInheritanceDecision` 确定性投影 `clothingOwnership=source|template`；同时保留更细的结构例外关系。内容 binding 使用 `replace_content`；单目标采用 `replace_as_unit`，需要保持空间组结构的多目标采用 `preserve_target_group`。合照选单人、像素保留、人宠混合自动拆组和未经客户端确认的多图分组保持关闭。

`visualContract` 的作者语义与逐图画风标准读取 `template-authoring.md`，正式结构精确包含：

- `medium`：一句正向、可观察的绘制或摄影媒介；
- `styleTraits`：替换区与固定区共享的造型比例、线条、细节密度和材质语言；
- `composition`：画幅、裁切、位置、比例、留白和阅读顺序；
- `relations`：身份边界、接触、遮挡、承托、容器关系和逐模板服装裁决；
- `colorAndLight`：只有色光属于模板卖点时填写，允许空数组。

编译器拒绝只用“沿用确认模板图”“高质量插画”“精美细节”“合理构图”或“自然关系”等泛化约束；每个字段必须达到机器合同的最小信息长度，并写出当前图可观察的媒介、造型、构图或关系事实。旧 v1 `promptEnhancement` 不参与新生产项的 v2 编译。

每个高价值槽位继续提供 `hiddenConflictTokens` 和 `titleForbiddenTokens`。确定性门禁拒绝 visual contract 锁回槽位默认值、建议值或自由编辑内容；独立语义审计复核目标—绑定职责、开放内容权限、身份中性和最大差异标题。审计 SHA 同时绑定标题、Prompt、runtimeSemantics、渲染决策、自由内容和全部槽位默认值/推荐值；任何被审计内容变化都会让旧结论失效。P6 视觉合同审计直接读取 Approved Template Image，并逐项返回媒介、构图、动作关系、渲染单元和 subject 转绘权限结论；验证器按决策中的 unit/component 与 input/target 原样对账，拒绝总括式“已检查”证据。

## 4. 四层验收与正式投影

P6 分别记录 Schema、语义、视觉合同和 Gallery Contract 证据，`pass` 由四层结果共同推导。语义层要求 `semantic-audit.json` 合同有效、内容摘要双向一致，并通过机器规则列出的全部审计项；每个具名检查必须提供机器映射指定的结构化证据。Prompt 代入覆盖默认值和全部推荐场景，开放轴与推荐审查覆盖全部槽位，最大差异输入覆盖每个推荐池；runtimeSemantics 范围和目标—绑定职责对象严格匹配机器角色。空容器、标量占位或覆盖不完整都不能形成通过结论。

四层验证后还必须编译 `critical-outcome-qualification.json`。该资格账本从已持久化事实重算机器合同声明的 27 项关键结果，覆盖换图依赖闭包、画风、身份与多主体、来源目标画布、来源标记策略、冻结 Prompt、交互肢体、Approved Image、用户文案、主体与槽位策略、身份继承、默认值、Prompt 代入、用户权限、标题与推荐项、runtimeSemantics、身份和可见文字、渲染、标签及正式记录合同。27 项 ID 必须精确覆盖且全部为真。P8 不相信历史通过状态；它重新编译账本，并与已持久化账本精确对账，缺失、删改或任一项变为假都停止上传和正式投影。

P8 使用 `contracts/machine-rules.json` 的白名单投影，并以 `contracts/upstream/gallery-template/agent-template-json-runtime-contract-2026-08-26/gallery-template.schema.json` 再次校验最终记录。该 Schema 是当前作者合同的只读快照，来源、取得时间、兼容范围、上游文档/运行 Schema 差异和摘要记录在同目录 `snapshot-metadata.json`；投影白名单继续独立存在于机器规则中。

存量 v1 先运行 `python3 scripts/migrate_gallery_contract_v2.py --input <file-or-dir>` 生成审计清单。每个身份槽补齐 `source|template` 决策后，传入 `--decisions <json> --apply --output <new-dir>` 写出 v2；工具拒绝覆盖原文件或已存在的输出。动态群组不能从 v1 自动推导，需重新执行双层群体策略分析。存量重新编译时，调用方将旧正式 `title` 作为 `preservedTitle` 传入同一 seam；编译器仅在它与 `neutralTitle` 逐字相同时继续。

决策文件按模板 key 和 input id 两级映射，例如：

```json
{
  "mutual-cheek-hold-couple-portrait": {
    "boy_subject": "source",
    "girl_subject": "source"
  }
}
```

本 Skill 的 P8 生产投影固定输出：

`key`、`status`、`title`、`description`、`imageSize`、`imageN`、`kind`、`promptTemplate`、`inputSchema`、`preprocessSteps`、`runtimeSemantics`、`metadata.tags`、条件性的 `metadata.needsReview`、`cover`、`referenceImage`。

作者 Schema 中 `inputSchema.version` 固定为 `2`，`slots[]` 通过 `text`、`image` 或两者组合表达输入能力；同时具备两种模式时使用 `resolutionStrategy=image_over_text`。Skill 的文字模式固定输出 `presentation=suggestions`、作者默认值、placeholder 和三条推荐项；图片模式不输出 `enabled`。

`cover` 与 `referenceImage` 写入 Asset Receipt 中同一个 HTTPS URL。投影源若含未知顶层字段、未知 metadata、空 `needsReview` 或非 HTTPS URL，直接阻断；冻结 Schema 允许但业务白名单未开放的 metadata 同样不能静默进入正式记录。最终验证还拒绝 Data URL、文件 URL、临时/用户绝对路径、生成 request 字段和审计字段。

`coverUrl`、`promptEnhancement`、`inputSchema.slots[].image.extract`、Replacement Pool、六维分析、推荐理由、版本 pin、候选策划和审计证据只保留在存量迁移输入或 sidecar。`fixtures/contracts/latest-gallery-samples/` 冻结研发已验证的 v2 正式样例及显式 expected 投影；全部标量叶子必须归入正式字段或机器声明的 metadata sidecar，未分类数保持为 0。expected 执行白名单投影和中性文案纠偏，并继续通过冻结 Gallery Schema 与最终业务门禁。存量迁移器读取同一 `runtimeSemanticsContract` 字段和枚举，迁移前后执行该验证链；CLI 接受单模板、裸数组和正式 v2 bundle，只在安全的隔离根内原子 create-once 写入正典 key 对应文件。

## 5. 正式记录拆分交付

P8 的 Production workspace 继续保存每个生产项的正式 `gallery-template.json` 和生产 sidecar。用户指定长期正式数据目录，或要求单模板独立文件时，P8 完成后调用：

```bash
python3 scripts/export_gallery_templates.py \
  --source <单条对象或正式记录数组.json> \
  --production-manifest <逐条正式生产Manifest.json> \
  --output-dir <正式目录/单模板JSON> \
  --manifest <正式目录/交付清单.json>
```

导出器要求显式提供目录外的 `--manifest`，对每条记录重新执行当前 Gallery Schema、最终业务合同和执行资格重放，要求 key 唯一且合法，并把每条对象单独写为 `<key>.json`。单工作区来源自动发现相邻 Production Manifest；正式记录数组逐条重复提供 `--production-manifest`。单模板目录不接受汇总数组、交付清单、生产 sidecar、点文件或其他范围外内容。相同内容可幂等重跑；同名文件内容不同默认阻断，只有人工确认后显式使用 `--overwrite`。

拆分导出只改变本地交付布局，不修改记录字段、模板图、OSS 对象或 URL。交付清单位于单模板目录之外，记录来源摘要、key 集合和每个文件摘要。Artifact Schema `0.27.0` 起，P8 首次进入完成态前与导出前共用全链路资格重放；导出还要求已安装 runtime 产生的 `live_external`、Fal、独立审核和 Aliyun OSS 证据，回放 JSON 保留给 T1 与回归。Artifact Schema `0.25.0` 起，P0 必须先写入 `template-key-resolution.json`，完成存量来源查询、语义 key 审核和冲突审核。Schema 正则继续只负责字符形状；缺失注册表证据或使用素材追踪号作为新 key 时，公共生产 seam 在 P1 前停止。

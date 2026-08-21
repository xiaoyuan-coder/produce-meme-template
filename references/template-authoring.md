# 模板身份、编辑权限与正式字段编写规范

## 1. 适用范围与完成标准

P3–P6 使用当前 Production Item 的 Approved Template Image 和与其 SHA 绑定的只读 Authoring Handoff。Approved Image 决定最终可见事实；Handoff 提供 P1 已冻结的玩法、IP/文化身份、主体连续性、组件拓扑和替换边界。P3 围绕两者差量编写标题、描述、槽位、Prompt Template 和 `runtimeSemantics`，不重复从零发现上述语义事实。

完成一次编写前，逐项确认：每个用户可见字面都能追溯到当前确认图或用户对该图的显式要求；每个槽位都通过四道价值门禁并绑定当前组件；Prompt 完整表达开放内容；`runtimeSemantics` 精确定位目标并使用可观察的画风事实；当前 Production Item 没有读取兄弟项的标题、默认值、槽位、Prompt 或视觉合同。

## 2. 单图事实域

每张确认图建立独立事实域，包含其图片 SHA、`visibleFacts`、`componentGraph`、`textRegions`、关系、槽位候选与用户单图要求。标题、默认值、推荐项、上传提示、Prompt 和运行语义只从这个事实域编写。

批量入口只负责逐项调度。默认批量不提供跨图事实；共享策略只在用户显式授权的 scope 内影响换图分配。共享主题、其他图片的构图、上一项的文案和批次中常见的视觉模式都不能补写当前图片没有的事实。需要标题多样性时，先完成每张图的独立标题，再在显式共享策略允许时做不改变事实的措辞复核。

以下情况属于批次幻觉并阻断当前项：

- 目标角色、空间位置、容器或动作来自另一张图；
- 标题、Prompt 或推荐项为了批次整齐而加入当前图不可核验的物体、身份、节日或场景；
- `visualContract` 复制兄弟项的媒介、构图、色光或关系；
- 使用“同上、保持原样、沿用参考风格、与本批一致”等回指语句代替当前图的观察结果。

## 3. 模板身份与编辑权限

模板身份由用户替换默认内容后仍需成立的视觉机制构成：核心动作或关系、容器与空间骨架、画面阅读顺序、媒介与造型语言、必要的接触和遮挡，以及承担玩法的文字结构。具体默认人物、动物、物件、颜色、服装或文案在开放后不再属于模板身份。

对当前确认图的每个显著内容按顺序裁决：

1. 判断它是否参与模板身份；记录动作、关系、容器、构图或视觉钩子。
2. 判断用户是否有独立修改动机，结果是否一眼可见，模型能否稳定控制，修改后模板机制是否成立。
3. 四项全部成立时进入结构化槽位。
4. 具有编辑价值但不值得占用独立控件时，进入 Prompt Template 的全文自由编辑内容。
5. 缺少独立编辑价值且承担版式或语境职责时，作为固定视觉内容保留。
6. 水印、来源标记和失效身份依赖执行清理；事实歧义进入人工复核。

主体槽采用强制优先级：当前图存在明显主体，且用户上传图能在保持机制的前提下接管该主体时，必须开放 `subject`。“当前 v2 不支持”、“为了简化控件”或自由文本理由不能省略主体。省略时必须提供 `uploadReplacementFeasible=false` 和机器合同允许的类型化 blocker；明显猫、狗、人物、单个道具等可分离主体通常不满足省略条件。

结构化槽位的四道门禁固定为：

- `userMotivation`：用户有清晰、常见且可表达的修改目的；
- `visuallyVisible`：修改结果在成图中直接可见；
- `modelControllable`：正式合同和图片模型能稳定控制该变化；
- `mechanismPreserved`：替换为最大差异合法值后，模板核心玩法仍成立。

“画面里能检测到”不构成槽位理由。低价值装饰、轻微渲染参数和与其他槽高度耦合的细节退出控件预算。常态保留 2–5 个槽位且通常约 3 个；单槽例外必须穷尽复核机器合同声明的全部轴。

纯色或近纯色背景默认属于低价值版式事实：它通常只承担铺底、留白和色彩对比，保留在 Prompt Template 或 `visualContract.colorAndLight` 中即可。只有用户对该背景存在独立、常见且明确的修改动机，修改结果显著，并且颜色变化不会破坏主体辨识、文字对比和模板机制时，才允许开放为槽位。不能为了达到槽位数量预算，把“白色背景、蓝色背景、纯色底”等自动提升为背景槽。

## 4. 槽位公共属性

每个槽位先在 `template-analysis.json.slotCandidates` 中保存逐图作者事实，再由编译器投影为正式 `inputSchema`。

| 属性 | 编写规则 |
| --- | --- |
| `id` | 使用稳定英文语义 ID，字母开头；表达角色或位置，不写当前默认身份。多实例用稳定空间角色区分，例如 `person_left`。同一模板重跑保持不变。 |
| `type` | 图片优先接管一个身份或完整可见目标时使用 `subject`；文字、颜色、内容物、场景和氛围使用内部 prompt 类槽并投影为正式 `prompt`。 |
| `semanticRole` | 从机器合同枚举中选择当前组件实际承担的职责，并与 `componentGraph.controlId`、目标类型和输入绑定一致。 |
| `label` | 用户可见的短名词短语，明确当前图中的对象或区域；保持身份中性，不复用其他模板标签。正式上限读取 Gallery Schema。 |
| `required` | 当前正式投影固定为 `false`，因为 Prompt Template 内联默认值提供可执行 fallback。 |
| 默认值 | 只读取当前确认图；使用用户语言和合适颗粒度，不写分析术语。开放身份使用中性或当前模板默认态。 |
| `suggestions` | 恰好三项；先以默认值确定编辑轴和语义颗粒度，再逐项给出同轴、同颗粒度、互不重复的替代值，并检查姿态、容器、接触、媒介和机制兼容性。具体角色名、泛化人物类型、发色服装描述和画风标签属于不同颗粒度，未经当前默认值证据不能混用。 |

槽位 ID、label、默认值和推荐项分别承担机器绑定、界面识别、默认成图和快捷替换职责，不能互相复制充数。

推荐项审计必须逐槽回答四件事：默认值对应什么编辑轴、默认值处于什么颗粒度、三条推荐是否逐项保持同轴同颗粒度、它们是否适配当前动作与视觉机制。P6 对每个槽位绑定当前默认值与三条推荐值，逐条记录 `sameAxis`、`sameGranularity`、`mechanismCompatible` 和非空证据；仅列出“已检查的 slot ID”不能形成通过结论。P3 保留作者事实与推荐池，独立审计证据统一由 P6 产生，避免作者自证。

## 5. Subject 槽属性

`subject` 表示图片输入优先、文字输入兜底的单个目标。当前 v2 正式形状如下：

```json
{
  "id": "pet_subject",
  "type": "subject",
  "label": "猫咪主体",
  "required": false,
  "resolutionStrategy": "image_over_text",
  "text": {
    "allowCustom": true,
    "defaultValue": "三花猫",
    "suggestions": ["橘白猫", "银渐层猫", "黑白奶牛猫"]
  },
  "image": {
    "enabled": true,
    "promptValue": "用户上传图中的主体",
    "hint": "上传1张主体清晰的单主体图片",
    "maxCount": 1,
    "minWidth": 256,
    "minHeight": 256,
    "private": true,
    "sourceOptions": ["upload", "recent_upload", "asset_library"]
  }
}
```

逐字段规则：

- `resolutionStrategy` 固定为 `image_over_text`：有合法图片时由图片接管目标，没有图片时使用文字值。
- `text.allowCustom` 固定为 `true`；`text.defaultValue` 和三条 `suggestions` 服从公共默认值与推荐项规则。
- 当前 v2 的 `subject.text` 不包含 `placeholder`。subject 的界面说明由 `label`、`image.hint` 和文字默认值共同承担；旧 `text.placeholder` 会被 Schema 拒绝。
- `image.enabled` 固定为 `true`。
- `image.promptValue` 是图片模式提供给运行时/LLM 的中性主体说明；`image.hint` 是上传 UI 文案。它们都不承担目标定位、身份绑定或画风约束。目标位置和接管关系只由 `runtimeSemantics.targetInstances + inputBindings` 表达。
- 作者可省略这两项，编译器分别回填“用户上传图中的主体”和“上传1张主体清晰的单主体图片”。需要让界面更清楚时可提供非空短文本覆盖，例如“用户上传图中的人物”；无需逐图复述容器、动作和空间位置。
- `maxCount` 当前固定为 `1`。多个独立身份或素材使用独立 subject 槽；当前合同不把多人合照静默压成单主体。
- `minWidth/minHeight`、`private` 和 `sourceOptions` 由机器合同统一投影，作者分析不得按批次随意改写。

### 5.1 身份特征继承裁决

每个 subject 候选在第三阶段只做一次范围裁决：默认从用户上传图读取当前主体清晰可见的身份特征；某项特征构成模板核心动作、关系或视觉钩子时，才列为模板固定例外。不建立更大的特征权限系统。

`template-analysis.json.slotCandidates[].identityInheritanceDecision` 精确包含：

```json
{
  "inheritFromUpload": ["可辨认身份特征", "肤色", "发型", "服装", "表情"],
  "keepFromTemplate": ["双手托腮动作"],
  "reason": "托腮动作构成模板核心玩法"
}
```

- `inheritFromUpload` 必须包含机器合同声明的“可辨认身份特征”，并至少按当前图补充一项肤色、毛色与花纹、发型、服装、配饰、表情或动作等清晰可辨范围。
- `keepFromTemplate` 只列参与核心玩法的例外，可以为空；存在固定例外时，`reason` 必须用一句当前图事实说明其玩法、动作、关系、结构或视觉钩子依据。两个列表不得重叠。
- 模板媒介、画风、造型比例和构图骨架继续属于 `visualContract`，不写入身份特征列表。
- 裁决保留在生产 sidecar；编译器将其生成为图片模式的 `runtimeSemantics.visualContract.relations`，正式 `inputSchema` 不新增字段。`image.promptValue` 继续只提供中性主体说明。
- 若某项特征已开放为槽位，用户本次槽位值拥有最高权限，不再将同一默认值列为模板固定例外。
- “至少一项具体范围”是确定性 fail-fast 下界。当前图事实和固定例外是否真正参与模板玩法，由直接读取 Approved Template Image 的 P3 作者分析负责裁决；P6 独立语义审计只复核编译后权限、目标、绑定与约束之间的一致性。

### 5.2 混合服装权限

服装可以采用混合裁决。用户上传图提供人物身份、服装颜色、可辨认材质和局部细节；模板保留承担核心视觉机制的服装轮廓、体积、受力方式或动作所需结构。此时 `inheritFromUpload` 写入可继承的服装事实，`keepFromTemplate` 只写轮廓或结构例外，并在 `reason` 中说明它与动作、裁切或视觉钩子的关系。

例如动态半身人物模板可以继承上传人物的服装配色和领口细节，同时固定“贴合肩胸、轻量、不过度膨胀”的外轮廓，以免厚重外套改变头肩占幅和冲出画面的动作线。固定例外不能偷渡具体制服、角色专属饰品、编号或默认颜色；这些内容应随身份继承、开放编辑或清理。

## 6. Prompt 槽属性

正式 prompt 槽只包含 `id/type/label/required/placeholder/suggestions`。`placeholder` 使用面向用户的动作短句说明该轴，例如“输入软垫颜色或材质”；它不描述模型内部约束。默认值位于 Prompt Template 的内联 placeholder，并在生产 sidecar 中保存，正式 `inputSchema` 不复制第二份默认值。

文字槽继续服从 `visible-text-contract.md`：身份文字、主视觉文字和高价值短 span 才能成为控件；次要可读长文字进入全文自由编辑；固定、清理或复核区域不能泄漏回普通 Prompt 槽。

## 7. 标题规范

标题只读取当前确认图的可见动作、关系、容器、视觉钩子、场景机制和已确认的主要文字。来源图标题、来源身份、默认槽位值、兄弟项标题和专题运营文案不参与编写。

每个标题通过四个单图门禁：

1. `templateGrounded`：每个实义词都能在当前确认图或当前机制中核验；
2. `usageMotivation`：用户能理解这个模板适合表达或制作什么；
3. `spokenNaturalness`：朗读自然，没有分析术语和对象清单；
4. `slotPortability`：把所有开放槽替换为最大差异合法值后仍成立。

四项结论写入当前 `template-analysis.json.titleEvidence`，字段精确为 `templateGrounded/usageMotivation/spokenNaturalness/slotPortability/evidence`。四项必须均为 `true`，`evidence` 需说明当前图的标题骨架及最大差异替换后仍成立的原因；编译器在生成 `editable-template-spec.json` 前执行该门禁。

具体人物、IP、物种、年龄、性别、服装、颜色和默认文字只要属于开放轴，就退出标题骨架。标题优先使用动作或关系，其次使用容器或视觉钩子，再使用中性情绪和场景。批次差异化只在显式共享策略授权时作为第五项措辞检查，并且不能改变单图事实。

## 8. Prompt Template 编译规范

Prompt Template 是用户可见且可全文替换的完整自然语言画面描述。按当前确认图的阅读顺序编写：主要目标与动作、容器和关系、其他开放内容、具有编辑价值的自由内容。每个结构化槽恰好以 `{{ id | "默认值" }}` 出现，所有 `freeEditableContent` 原样出现。

Prompt Template 的每个字面都应是用户可编辑的内容或它们之间的自然关系。“保留宠物照片剪贴与扁平应援图形的混合媒介”这类媒介、画风、渲染、固定构图、材质纪律、水印清理或内部审核指令统一进入 `runtimeSemantics.visualContract` 或生产 sidecar；机器合同在 `editable-template-spec.json` 写入前拦截泄漏片段。

默认值和每条推荐项逐项代入后都必须得到无残留 placeholder、语法完整、关系清楚的自然句。Prompt Template 只表达用户有权修改的画面内容及其自然关系；媒介、画风、固定构图、材质纪律、清洁要求和内部冲突说明进入 `runtimeSemantics.visualContract` 或生产 sidecar。

槽位编辑与全文编辑拥有同等用户字面权限。用户改写主体、文字、颜色、服装、道具或场景后，隐藏层不得恢复旧默认值。单图 Prompt 不引用批次主题、兄弟图内容或“与上一张一致”等上下文。

## 9. runtimeSemantics 编译规范

### 9.1 targetInstances

每个开放目标和关键身份边界使用稳定 ID，并绑定当前 `componentGraph` 中的实际组件。`role` 描述当前图中可观察的职责，`region` 描述可定位的空间范围；两者分别达到机器合同的最小信息长度，且“主体、背景、画面元素、主体区域、画面区域、对应位置”这类通用词不能单独形成定位。重复实例、镜像、阴影、容器和分格继续按多实例合同建立关系。

### 9.2 inputBindings

每个正式 input ID 恰好出现一次。subject 绑定唯一 `identity_subject` 并执行一对一身份重绘；prompt 输入绑定一个内容目标，或绑定需要保持组结构的一组内容目标。绑定只决定输入接管哪个目标，不承载风格文案。

### 9.3 renderingCoherenceDecision

P3 必须先依据当前 Approved Template Image 建立 `renderingCoherenceDecision`，再编译正式 `visualContract.medium/styleTraits`。这份 sidecar 是媒介与画风的作者权威，字段由 `renderingCoherenceDecisionContract` 唯一定义：

- `mode=unified` 表示整张图只有一个绘制体系，只允许一个 `renderingUnit`，且 `boundaryEvidence` 必须为空；
- `mode=intentional_mixed` 表示确认图本身有刻意的混合媒介，至少声明两个 `renderingUnit`，并逐条写明可观察的分界依据；
- 每个 `renderingUnit` 使用自由文本记录当前图的媒介特征，列出所覆盖的 `componentIds`；全部组件必须恰好覆盖一次，不能遗漏手部、道具、容器、阴影、文字或背景；
- 每个 subject 都必须提交 `subjectTransfer`，其 target、上传继承范围和模板保留范围与组件绑定及 `identityInheritanceDecision` 完全一致，并明确 `completeRedraw=true`；
- `medium` 与逐单元 `styleTraits` 由编译器投影进正式 `visualContract`。旧 visualContract 中同名作者文本不能覆盖这份决策。

统一渲染强调的是确认图中的实际共同绘制语言。若人物、手部、物件和背景都采用二维闭合线稿与平涂色块，应放在同一单元；若确认图确实使用二维角色叠加摄影背景，则分别建单元并说明边界。不能为了兼容生成结果，事后把意外的摄影手部、三维公仔或写实皮肤登记成“刻意混合”。

### 9.4 visualContract

`visualContract` 保存用户替换内容后仍需成立的严格视觉事实：

- `medium`：明确摄影、插画、拼贴、版画、像素或其他媒介，并写出当前图可见的材料表现；
- `styleTraits`：逐图描述造型比例、线条、边缘、细节密度、纹理和材质；
- `composition`：描述画幅、裁切、目标位置、比例、留白和阅读顺序；
- `relations`：描述接触、遮挡、承托、容器、重复实例、分格顺序和身份边界；
- `colorAndLight`：只有色光本身属于模板身份时填写；色光是开放槽时保持空数组。

每条约束使用正向、可观察、可由当前图片核验的句子，并达到机器合同的最小信息长度。“沿用确认模板图”“保持原风格”“维持原构图”“高质量插画”“精美细节”“合理构图”“自然关系”只回指图片或表达质量愿望，不能成为正式约束。画风描述需要落到当前图的具体媒介、轮廓、颗粒、材质、体块、纸张、镜头或光影事实。旧 v1 `promptEnhancement` 只保留为迁移证据，不能回退生成 v2 visual contract。

动态、无容器或人物冲出画面的模板需要额外固定动作事实：`composition` 至少写明画幅、头部中心位置、头肩占幅、上下左右裁切边界、躯干轴向和装饰分布；`relations` 至少写明肩胸朝向、手臂进入或退出画面的范围、头发或飘带的运动方向，以及装饰元素与人物的前后层级。只写“高能、动感、冲出画面”会给模型过大的构图自由，容易发生人物缩小、服装体积膨胀、姿态旋转和装饰密度失控。

最小信息长度和禁用泛词只负责阻断明显空值。直接读取 Approved Template Image 的 P3 作者分析负责当前图事实、目标可定位性和 visual contract 具体性的视觉裁决。P6 另行把当前 Approved Template Image、编译后的 visual contract、渲染决策和组件图交给视觉审计 adapter：逐项核对媒介、构图、动作关系、每个渲染单元和每个 subject 权限。审计对象、图片和内容摘要必须一致；任一否定结论都在 OSS 前阻断。

视觉合同保护模板身份，同时服从用户开放权限。它不能写入任一开放槽的默认值、推荐值、自由编辑字面或对应的同义锁定词。标题、Prompt、targets、bindings 和 visual contract 由独立语义审计绑定同一内容摘要；冲突、缺项或跨图事实在 OSS 上传前停止。

## 10. 复核示例

- 海报中的主要四行口号承担玩法且用户有独立修改动机：开放短文字槽。
- `EXPOSITION`、`Peinture—Sculpture` 等次要长文字可读且允许用户整段修改：进入 `freeEditableContent` 与 Prompt Template，不占 `inputSchema`。
- 版权、出处和装饰微字缺少独立编辑价值：固定保留或清理，不进入用户 Prompt。
- 树根旁动物图的 target role 必须写树根、林地和坐立关系；软垫、客厅和蜷卧属于另一张图，出现即为批次幻觉。
- 大面积纯色铺底只承担干净背景时，保留在 Prompt 或色光事实中；缺少独立编辑动机时不建立颜色槽。
- 人物默认值为具体角色名时，推荐项需保持角色身份颗粒度；若产品目标是开放普通人物，应先把默认值中性化，再提供同颗粒度人物建议，不能直接拼接发色、物种和画风标签。

# 新模板生产 Skill 总体设计

## 1. 结论

建议新建一个独立、干净的模板生产 Skill，暂用工作名 `produce-meme-template`。旧 Unified 和拆分版保留为迁移依据，不继续充当新流程的实现底座。

新 Skill 只暴露一条端到端生产入口：

> 网图 → 制定替换方案 → 生成并确认模板图 → 分析模板图 → 编译模板数据 → 上传模板图 → 回填并落盘

新 Skill 的正式生产交付物只有一份完成 URL 回填、通过正式 Schema、业务质量检查与执行资格重放的模板 JSON。P0 的 `production-execution-profile.json` 区分不可交付回放和 `live_external` 正式执行；阶段 sidecar、证据和清单只服务生产与审计，不进入下游业务 JSON。主流程固定为 P0–P8。真实用户编辑与生图验证使用 `T1` 独立调用入口，单独读取已落盘 JSON；前端交互、后端实现和数据库导入在职责范围外。

用户可以一次执行完整生产，也可以按四个可恢复大阶段推进同一个生产项：第一阶段输出换图执行 JSON；第二阶段调用真实图片 API 生成候选图并确认 Approved Template Image；第三阶段输出通过 P6 的待 OSS 模板数据包；第四阶段上传 OSS、回填 URL 并输出最终正式模板数据。四个大阶段是 P0–P8 的操作聚合层，共用同一份不可变生产谱系。

## 2. 设计目标

1. 完整继承 Unified 已经验证有效的能力。
2. 以生成后的模板图为最终视觉事实源，消除旧网图对模板数据的隐性污染。
3. 替换策略支持“显式策略优先、无策略时自主策划”。自主策划受严格替换规范约束。
4. 用户可见内容与 `runtimeSemantics` 运行语义分层编译，最终统一写入正式模板 JSON。
5. 各阶段产物可追踪、可恢复、可复核，禁止通过后续阶段静默改写上游事实。
6. 机器规则只有一个定义位置，文档负责解释，脚本负责执行。
7. Skill 源码、安装副本、生产批次和外部契约都能明确回答“当前用的是哪个版本”。

## 3. 三类事实源

| 事实源 | 可以决定 | 不得决定 |
| --- | --- | --- |
| 网图 | 原始笑点机制、结构关系、可替换目标、风险线索、重构方向 | 最终标题、最终描述、最终默认主体、最终画面细节 |
| 已确认模板图 | 最终可见主体、媒介、形态、构图、默认内容、reference/cover 视觉事实 | 产品不支持的字段和运行时行为 |
| 正式模板 JSON 合同 | 字段结构、槽位类型、目标实例、输入绑定、视觉合同和取值限制 | 模板图中不存在的视觉事实、合同未声明的运行行为 |

这三个事实源必须在产物中分别记录来源。模板图确认以后，标题、描述、槽位默认值、promptTemplate 和 `runtimeSemantics` 全部重新以模板图为依据编译。

## 4. 核心领域对象

| 对象 | 含义 |
| --- | --- |
| `SourceImage` | 输入网图，只用于重构分析与风险识别 |
| `BatchPolicy` | 用户提供的批次策略，可为空 |
| `ReplacementSpec` | Skill 内置、版本化的严格替换规范 |
| `ReplacementPlan` | 单图替换决定，记录目标、替换值、依赖闭包、冻结项、理由和来源 |
| `GenerationPackage` | 提交给生图 API 的参考图、prompt、参数和追踪信息 |
| `GeneratedTemplateImage` | 换图后的候选模板图 |
| `ApprovedTemplateImage` | 通过画面验收的唯一模板图，也是 cover/reference 的上传源 |
| `TemplateAnalysis` | 对已确认模板图的结构化视觉与语义分析 |
| `EditableTemplateSpec` | 用户可编辑内容、槽位、推荐项和 promptTemplate 的中间模型 |
| `RuntimeTemplateSpec` | inputSchema、targetInstances、inputBindings 和 visualContract 的中间模型 |
| `GalleryTemplateRecord` | 编译后的正式业务 JSON |
| `AssetReceipt` | OSS 上传结果、摘要和路径证明 |
| `ProductionManifest` | 全流程版本、输入、产物、状态、摘要和审计记录 |
| `TemplateJsonTestRun` | 用户明确触发的单次模板 JSON 生图测试，绑定被测 JSON 摘要、revision、测试输入和结果证据 |

## 5. 完整生产工作流

### P0：接收与预检

输入：网图、可选单图策略、可选批次策略、批次标识。

执行：

- 读取图像尺寸、格式和可访问性。
- 识别水印、签名、平台标、品牌、人物/IP、敏感内容和低清风险。
- 为当前生产项建立 `ProductionManifest`，锁定该生产项的运行版本。批量入口默认不共享版本 pin。
- 只做重构所需的最小分析，暂不编写模板 title、description 和 promptTemplate。

退出条件：图像可处理；策略来源明确；风险达到阻断级时进入人工复核。

### P1：制定替换方案

策略优先级固定为：

1. 单图显式策略。
2. 批次显式策略。
3. Skill 自主策略。

显式策略仍需经过安全性、可生成性、画面兼容性和依赖完整性检查。自主策略严格执行《第一阶段替换与生图规范》。

策略可以只覆盖部分决策。被用户明确指定的范围保持权威；未覆盖的目标选择、依赖闭包、批次分配和生图约束由 Skill 按默认规范补齐。每个决定分别记录来源，避免把“部分指定”误记成整张图完全自主或完全人工指定。

产物：`replacement-plan.json`，至少包含：

- 策略来源与版本。
- 原始目标类别与拟替换类别。
- 目标替换值、选择理由和置信度。
- 必须联动替换的依赖闭包。
- 必须冻结的结构、文字、关系和构图。
- 语言策略、媒介策略、去水印要求和权利风险。
- 低置信度或未知类别的人工复核标记。

### P2：编译生图任务并生成模板图

根据替换方案编译 `GenerationPackage`。prompt 由稳定结构生成，禁止自由拼接遗漏关键约束。

P2 属于用户第二阶段，并通过图片生成 adapter 调用 API。真实生产使用 Fal 队列完成 submit、status、result 和图片下载；API 返回结果先登记为候选图，视觉门禁通过后才形成第二阶段产物 Approved Template Image。

生成后执行画面验收：

- 替换目标是否一眼可辨。
- 旧身份、旧颜色、旧轮廓、旧文字或旧水印是否残留。
- 依赖闭包是否完成替换。
- 冻结项是否保持。
- 媒介、形态、边缘、色彩明暗、表面、构图是否一致。
- 接触、遮挡、穿戴、容器、反射、影子等空间关系是否成立。
- 笑点机制和因果方向是否仍然成立。

失败时回到 P1 或重新生成；通过后将唯一图像登记为 `ApprovedTemplateImage`。同一模板只能有一个当前有效的已确认模板图。

### P3：分析已确认模板图

本阶段从零分析模板图，不沿用 P0 对网图的实例性描述。

分析至少覆盖：

- 字面画面事实、推断语义和经人工确认的含义。
- setup、turn、payoff、tone、时间或因果方向。
- 六维视觉合同：媒介、形态、边缘、色彩明暗、表面、构图。
- 主体实例、重复实例、派生内容、接触和遮挡关系。
- 固定结构、可编辑内容和编辑后可能破坏机制的边界。
- 默认值、推荐值和图片输入的适用性。

产物：`template-analysis.json`。该文件只描述已确认模板图。

### P4：设计高价值槽位与用户可见内容

高价值槽位需要同时满足：

1. 用户有明确编辑动机。
2. 编辑结果能在画面中被辨认。
3. 大模型能稳定控制该内容。
4. 修改后仍保留模板核心机制。

正式模板常态开放 2–5 个高价值槽位，通常约 3 个。分析结果只有一个候选槽时，必须继续复核主体、主要内容物、主文字、颜色、服装、道具、场景和嵌套内容；穷尽分析后确实只有一个高价值变量时，允许交付 1 槽模板并记录例外证据。第五个槽位需要有独立用户价值和稳定生成能力，不能用低价值装饰补数。每个槽位需要定义简洁默认值、推荐项、图片输入能力、解析策略、画面操作和与其他槽位的依赖。默认值优先 2–8 个中文字符，原则上不超过 12 个字符；精确画内文字按实际内容处理。

文字进入槽位前增加价值门禁，只有三类文字可以开放：

1. 与主体身份直接绑定的姓名、角色名、称谓、团名、号码或身份标签。
2. 占据主要视觉、承担主要表达或玩法的文字，例如文字卡、主标题和核心口号。
3. 长文本中能独立改变含义、适合单独替换且不会破坏排版的关键词或短语。

海报微字、版权信息、装饰性外文、氛围填充文字和缺少独立编辑价值的说明文字进入固定文字合同。长文本默认保留；需要编辑时优先抽取高价值词或收尾短句，并为不同文字角色配置长度和版面安全上限。

用户可见内容包括：

- `title`
- `description`
- 槽位名称、提示、默认值和推荐项
- `promptTemplate`

title 使用中性标题，描述画面机制、视觉钩子、动作、关系、容器或使用场景。具体 IP、人物姓名、年龄、性别、物种、发型和服装不进入 title。模板图默认主体发生变化后，中性标题仍保持准确。

主体与姓名、英文名、团名、称谓、号码或身份徽标存在指代关系时，先记录身份文字依赖。只要主体开放，模板图和 JSON 默认态中的依赖文字就必须中性化、删除，或作为用户可独立修改的短文字槽使用中性默认值。生产侧不能保证自动联动时，旧身份专名不能进入默认值、promptTemplate、instruction、lockedConstraints 或 preserve。

`promptTemplate` 由用户可编辑画面模型编译，要求：

- 是一段完整、自然、可直接提交的图片描述。
- 包含全部开放槽位，并包含没有入槽但仍具有编辑价值的画面内容，供自由编辑模式修改。
- 不要求把所有可见细节都开放或写入；无编辑价值的装饰细节继续由模板图和隐藏约束控制。
- 不出现“原图、参考图、候选图、分析、内部约束”等内部词。
- 不把锁定的媒介、构图和防漂移约束伪装成用户槽位。
- Prompt Template 的最终字面拥有用户内容权限；隐藏字段不得恢复被用户改掉的主体、文字、颜色、配饰、服装、道具或场景。
- 结构化槽位编辑和自由编辑最终都产出同一种 `resolvedPrompt`。

实现上保存“槽位中间模型 + 编译后的 promptTemplate”，禁止分别手工维护两份含义相同的文本。

### P5：编译 runtimeSemantics 运行合同

运行合同层包括：

- `inputSchema`：槽位、输入形态、三条同轴推荐项和 subject 单图入口。
- `targetInstances`：每个可接管身份位或内容位的稳定 ID、角色与区域。
- `inputBindings`：将 subject 一对一绑定到 `identity_subject`，将 prompt 绑定到单个或一组 `content_element`。
- `visualContract`：仅保留媒介、风格、构图、关系和色光，不锁回用户开放内容。

只编译正式模板合同已经支持且运行或产品展示需要的字段。正式 metadata 默认白名单为 `tags`，确有人工复核原因时增加 `needsReview`。`candidateScope`、`runtimeRequirements`、`templateSource`、`inputSemantics`、`suggestionRationales` 和 `optimizationAudit` 留在生产 sidecar，不进入正式 JSON。新正式 JSON 不输出 `inputSchema[].image.extract` 或手写 `promptEnhancement`。

身份文字自动识别、别名推导、计算默认值或联动字段如果未进入正式 JSON 合同，就不写入正式数据。此时使用中性模板图、中性标题和中性文字默认值消除身份冲突。

产物：`editable-template-spec.json`、`hidden-template-spec.json` 和编译后的 `gallery-template.draft.json`。

### P6：静态验收

必须同时验证四层：

1. Schema：字段、类型、枚举和引用合法。
2. 语义：标题、描述、槽位、默认值和模板图一致。
3. 视觉合同：媒介、形态、实例绑定、操作闭包和接触关系完整。
4. JSON 合同：promptTemplate、inputSchema 和隐藏字段满足正式模板合同，未添加自创字段或运行行为承诺。

布尔型 `pass` 只能由检查证据推导，验证报告必须记录证据定位。

### P7：上传 OSS 与回填

只上传 `ApprovedTemplateImage`。上传动作负责存储，不代表权利、语义或质量审核通过。

成功后：

- 生成 `asset-receipt.json`，记录本地文件摘要、对象路径和返回 URL。
- 按当前正式数据合同将 URL 回填到 `cover` 和 `referenceImage`。Unified、旧 UAT 与管理台导出中残留的 `coverUrl` 不进入新 Skill 正式输出。
- 不把本地路径、临时 URL、上传响应或旁路字段写进正式业务 JSON。
- `cover` 和 `referenceImage` 指向同一个已确认模板图 URL。

### P8：最终落盘

最终目录至少包含：

```text
template-key/
├── gallery-template.json
├── production-manifest.json
├── replacement-plan.json
├── template-analysis.json
├── asset-receipt.json
└── evidence/
    ├── source-image.*
    └── approved-template-image.*
```

正式业务 JSON 与审计产物分离。下游导入只读取 `gallery-template.json`。P8 运行《正式 JSON 字段消费与旧流程字段审计》定义的白名单投影，生产旁证继续留在同目录 sidecar。

## 6. 状态机与回退规则

```text
INGESTED
  → REPLACEMENT_PLANNED
  → GENERATION_READY
  → TEMPLATE_IMAGE_GENERATED
  → TEMPLATE_IMAGE_APPROVED
  → TEMPLATE_ANALYZED
  → TEMPLATE_COMPILED
  → STATIC_VALIDATED
  → ASSET_UPLOADED
  → FINALIZED

任一生产阶段 ──不确定或风险──→ NEEDS_REVIEW
任一生产阶段 ──硬性失败──→ BLOCKED
NEEDS_REVIEW ──获得有效决定──→ 返回原阶段继续
```

规则：

- 每个阶段只能读取允许的上游产物并生成自己的产物。
- `NEEDS_REVIEW` 保存阻断阶段、问题、证据和所需决定；人工结论写入正式决策记录后才能恢复。
- `BLOCKED` 保存稳定错误码和不可继续的原因，不能只写一段自由文本。
- 上游内容变化后，所有依赖阶段自动失效，禁止保留旧 `pass`。
- 模板图变化时，P3–P8 全部重新执行。
- 只有 URL 回填失败时，可以从 P7 恢复，前提是本地图像摘要与 P6 记录一致。

## 7. Skill 组织方式

`SKILL.md` 控制在 500 行以内，只负责触发判断、输入确认、主流程、路由和可检查的完成条件。每条分支用明确指针直达一层 reference；复杂知识按阶段渐进加载，同一含义只保留一个权威位置。

```text
produce-meme-template/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── workflow.md
│   ├── replacement-spec.md
│   ├── template-image-analysis.md
│   ├── slot-and-copy-authoring.md
│   ├── template-json-compilation.md
│   ├── oss-finalization.md
│   └── optional-generation-test.md
├── contracts/
│   ├── upstream/
│   ├── internal/
│   └── controlled-vocabulary.json
├── scripts/
│   ├── init_batch.*
│   ├── compile_replacement_plan.*
│   ├── compile_generation_package.*
│   ├── compile_template_record.*
│   ├── validate_phase.*
│   ├── finalize_asset_urls.*
│   ├── test_template_record.*
│   └── release_skill.*
├── fixtures/
│   ├── good/
│   └── bad/
├── tests/
├── release.json
└── release-lock.json
```

这里采用“单一生产 Skill + 内部深模块”。对用户只有一个入口，内部模块拥有清晰的产物合同和失败边界。只有将来某个能力确实拥有独立调用者、独立版本周期和独立验收标准时，才拆成新的顶层 Skill。

## 8. 明确排除的职责

- 不启动、探活或依赖管理台。
- 不调用管理台任务 API，不写管理台任务状态。
- 不负责图库专题策划。
- 不负责下游数据库导入。
- 不实现前端交互或后端运行逻辑；T1 只读取已落盘 JSON，并调用现有生图能力验证模板效果。
- 不直接修改已落盘的正式模板；修改通过 revision 进行。

## 9. 新旧流程的关键差异

| 维度 | 旧 Unified 主要路径 | 新设计 |
| --- | --- | --- |
| 模板图时机 | 模板草稿后生成候选图 | 第一阶段先完成换图并确认模板图 |
| 最终视觉事实 | 原图分析与候选图容易混用 | 已确认模板图唯一负责最终视觉事实 |
| 替换决策 | 分散在分析、候选和对齐阶段 | 独立 ReplacementPlan，显式记录策略来源 |
| 用户内容与隐藏字段 | 同一阶段内交织 | 中间模型分层，最后统一编译成正式 JSON |
| 正式交付 | JSON、sidecar 和测试状态容易混用 | 只交付完成 URL 回填的正式模板 JSON |
| JSON 效果测试 | 容易成为批次生产门禁 | 通过 T1 独立调用现成 JSON，使用独立状态和测试产物 |
| 版本 | 多文件和安装副本分别维护 | 单一发布源、不可变 release、逐生产项 pin |

## 10. 实施顺序

1. 确认新 Skill 的正式名称和源码目录。
2. 冻结当前正式 Schema、当前正式数据样例和用户确认口径；研发 authoring/import 文档不作为生产 Skill 前置条件。
3. 建立领域对象、阶段 Schema、controlled vocabulary 和状态机验证器。
4. 先实现 P0–P2，用真实批次验证替换规范和模板图验收。
5. 实现 P3–P6，迁移 Unified 的有效分析、槽位和正式 JSON 编译能力。
6. 实现 P7–P8 与版本发布链路。
7. 使用历史 GoodCase、BadCase 和当前业务数据做回归，达到验收门槛后再切换生产入口。

T1 采用单独的实现与验收计划，在主生产入口稳定后建设，只消费现成正式 JSON。

## 独立测试入口 T1：按需测试模板 JSON

T1 拥有独立的调用、任务状态和产物目录。用户明确指定一份现成的 `gallery-template.json` 时才创建测试任务。生产主流程、生产状态机和批次完成条件均只包含 P0–P8。

执行：

- 锁定被测 JSON 的文件摘要、`templateRevision`、模板图 URL 和测试器版本。
- 优先使用用户给定的编辑值；未给定时，根据 `inputSchema` 和槽位推荐项编译一组具有明显差异、语义合理的模拟用户输入。
- 根据用户要求选择“槽位修改”或“全文自由编辑”，必要时分别验证两种模式。
- 调用现有生图能力生成真实结果，检查开放内容是否生效、固定内容是否保持、身份文字是否冲突、长文字是否失控，以及媒介、构图和接触关系是否稳定。
- 输出测试图片、调用证据和 `template-json-test-report.json`。测试产物属于测试记录，不写入正式模板 JSON。

失败处理：T1 在自己的测试报告中记录失败字段、图像证据和修订建议。用户决定修复时，另行创建新的生产 revision，并从受影响阶段重新执行；测试任务不改变原 JSON 及其生产状态。

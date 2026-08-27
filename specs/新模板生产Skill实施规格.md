# 重建可维护的端到端 Meme 模板生产 Skill

## Problem Statement

当前模板生产能力同时存在两类失败。上一轮拆分把统一能力拆成多个业务 Skill 和专业 Skill，但没有完整继承 Unified 已经验证有效的换图、视觉判断、文案、槽位和恢复能力；阶段切得过细以后，规则散落、流程需要多次显式唤起，真实生产难以稳定完成。为了业务上新重新启用的 Unified 又把来源分析、候选策划、模板数据编译、生成、审核、上传和历史兼容规则堆在同一处，继续增加补丁会进一步放大重复定义和版本漂移。

旧流程还把模板数据分析放在模板图生成之前，导致标题、描述、槽位默认值、Prompt Template 和隐藏约束容易继承来源网图中已经被替换的身份、物种、数量或文字。历史批次已经证明 Schema PASS 只能说明结构合法，无法证明语义、视觉合同和用户编辑权限正确；843 条数据静态全量通过后，抽样仍暴露出大量 bad case。

用户需要一套单一、完整、可恢复的模板生产工作流：从来源网图开始，先根据显式策略或严格自主规范完成换图并确认模板图，再以模板图为唯一视觉事实源分析高价值槽位、用户可见 Prompt Template 和隐藏运行约束，最终上传同一张模板图并回填 `cover` 与 `referenceImage`，落盘一份符合当前正式合同的模板 JSON。批量入口只提供多图提交便利，每张图默认保持独立。真实用户编辑和生图验证继续可用，但只在用户明确指定一份已落盘 JSON 时独立执行。

该工作流还需要从第一天解决版本不统一：仓库源码、发布包、安装副本、生产项、内部 Schema 和上游正式合同分别可追溯；机器规则拥有单一来源；历史 GoodCase、BadCase 和正式样例转成可重复执行的 fixture，防止再次出现“问答已经完整、实现仍不可用”的迁移结果。

## Solution

建立一个新的用户可见结果 Skill `produce-meme-template`。它通过一条端到端入口编排 P0–P8 主生产流程：接收与预检、制定替换方案、编译生图任务并生成模板图、分析确认模板图、设计高价值槽位和用户可见内容、编译隐藏约束、静态验收、上传 OSS 与回填、最终正式投影与落盘。

同一入口接受 1–4 大阶段选择：第一阶段完成 P0–P1 并输出换图执行包；第二阶段通过 Fal API 完成 P2 并输出 Approved Template Image；第三阶段完成 P3–P6 并输出待 OSS 模板数据包；第四阶段完成 P7–P8 并输出最终正式 JSON。省略阶段选择时串联执行全部四阶段。每次调用都从同一 Production Item 的不可变谱系恢复。

Skill 对外保持一个公共生产工作流接缝，支持单图和批量提交；批量提交内部拆成彼此独立的 `Production Item`。生图和 OSS 作为注入式执行适配器接入该工作流，业务规则、状态、产物和验收全部由工作流核心控制。`T1` 作为同一 Skill 下的独立测试命令，只读取用户明确指定的现成正式 JSON，编译后调用 Codex 内置生图工具；它不进入 P0–P8 状态机，不调用 Fal/OSS，也不改变正式 JSON。

Skill 内部按深模块组织。`SKILL.md` 只保留触发范围、主流程、完成条件、风险升级和 reference 路由；替换、文字、视觉合同、槽位、文案、正式 JSON 编译、生命周期、OSS 和 T1 规则分别拥有唯一 reference 与机器合同；确定性编译、验证、版本、恢复和上传准备由 scripts 承担；历史经验以匿名化红绿 fixture 和端到端验收样本存在。正式交付只包含完成 URL 回填的 `gallery-template.json`，替换理由、六维分析、版本 pin、推荐项理由和审核证据保留在生产 sidecar 中。

## User Stories

1. As a 模板生产者, I want 提供一张来源网图即可启动完整生产, so that 我无需记忆多个拆分 Skill 的调用顺序。
2. As a 模板生产者, I want 一次提交多张来源网图, so that 我可以批量上新数据。
3. As a 模板生产者, I want 批量中的每张图独立分析和产出, so that 一张图的身份、策略或失败不会污染其他图片。
4. As a 模板生产者, I want 批量默认不共享替换组合、版本 pin 和汇总报告, so that 上传行为不会被误解为共享业务策略。
5. As a 模板生产者, I want 显式提供共享批次策略, so that 需要时可以统一 IP 池、比例、去重或专题锚点分配。
6. As a 模板生产者, I want 单图策略优先于共享批次策略, so that 个别图片可以保留明确例外。
7. As a 模板生产者, I want 没有指定策略时由 Skill 自主决定替换内容, so that 日常生产无需为每张图手工策划。
8. As a 模板生产者, I want 自主决定受严格版本化替换规范约束, so that 同类替换和画面保真具有一致标准。
9. As a 模板生产者, I want 每个替换决定记录来源, so that 单图、批次和自主决策可以混合且可追溯。
10. As a 模板生产者, I want 来源网图先完成最小机制分析, so that 早期分析只服务换图，不提前固化最终模板文案。
11. As a 模板生产者, I want 未知主体或高风险冲突进入复核, so that Skill 不会在识别依据不足时随意选择替换值。
12. As a 模板生产者, I want 自主换图默认只主动替换一个主要组件, so that 画面核心机制和原有构图保持稳定。
13. As a 模板生产者, I want 关联文字、影子、倒影、重复实例和身份标记同步替换, so that 单一主要目标变化后不会残留旧身份。
14. As a 模板生产者, I want 真人通常替换为同类真人, so that 新身份与来源人物的年龄阶段、人数、关系和角色功能兼容。
15. As a 模板生产者, I want 普通真人可以由图片模型直接生成新身份, so that 自主换脸无需局限在固定本地资产池。
16. As a 模板生产者, I want 身份资产仅在稳定性确有收益时辅助生成, so that 资产池不会再次成为唯一替换来源。
17. As a 模板生产者, I want 二次元角色可以自主替换为同类知名 IP, so that 经过历史批次验证的角色多样性能力得到保留。
18. As a 模板生产者, I want 猫、狗、食物、物体和文字按同类连续性替换, so that 新画面仍然承载原来的笑点角色。
19. As a 模板生产者, I want 身份相关姓名、团名、识别色和徽标进入依赖闭包, so that 视觉身份和文字身份始终一致。
20. As a 模板生产者, I want 原图画风和核心构图默认保留, so that 换图形成新模板，同时延续原图最有价值的视觉机制。
21. As a 模板生产者, I want 生图 Prompt 从结构化 Replacement Plan 编译, so that 替换目标、依赖闭包、冻结项和媒介合同不会在自由拼接时遗漏。
22. As a 模板生产者, I want 每次生成默认只请求一张图, so that 成本、恢复和结果选择保持清楚。
23. As a 模板生产者, I want 网络重试复用 request ID, so that 轮询失败不会重复提交并产生额外费用。
24. As a 模板生产者, I want 不同失败类型拥有不同重试策略, so that 身份、文字、水印、接触和网络问题回到正确的修正阶段。
25. As a 模板生产者, I want Skill 自动检查生成图的替换完整性, so that 旧脸、旧轮廓、旧文字和漏换实例会在确认前被发现。
26. As a 模板生产者, I want Skill 自动检查六维视觉合同, so that 媒介、形态、边缘、色彩明暗、表面和构图不会发生无意漂移。
27. As a 模板生产者, I want Skill 检查穿戴、持握、遮挡和容器关系, so that 换图后的接触几何真实成立。
28. As a 模板生产者, I want Skill 全画布检查水印、签名和平台标, so that 半透明和嵌入内容的残留也能被拦截。
29. As a 模板生产者, I want 画面硬失败直接返回修正或重生成, so that 人工决定不会放行旧身份残留和结构破坏。
30. As a 模板生产者, I want 只有真实歧义和多个有效方案接近时请求人工决定, so that 常规批量生产可以自主推进。
31. As a 模板生产者, I want 通过硬门禁的生成图由 Skill 自主确认为模板图, so that 主流程无需等待逐张人工批准。
32. As a 模板生产者, I want 确认模板图成为后续唯一视觉事实源, so that 来源网图身份不会再次进入正式数据。
33. As a 模板生产者, I want 模板图变化时自动失效所有下游产物, so that 标题、槽位和 JSON 不会继续引用过期画面事实。
34. As a 模板生产者, I want 模板图分析区分可见事实、推断和人工确认含义, so that 不确定识别不会伪装成画面事实。
35. As a 模板生产者, I want 模板图分析记录组件、实例和依赖关系, so that 主体、嵌套图片、容器和重复实例都能正确进入槽位判断。
36. As a 模板生产者, I want 身份数、视觉实例数、上传素材数和输入控件数分别计算, so that 多人和重复人物不会被错误合并。
37. As a 模板生产者, I want 有主体的图片优先评估主体槽, so that 最有用户价值的修改入口不会被省略。
38. As a 模板生产者, I want 服装、造型、发型、姿势和颜色逐图评估, so that 人物模板不会机械套用同一组派生槽位。
39. As a 模板生产者, I want 常态获得约三个高价值槽位, so that 前端编辑既有明显自由度又保持易用。
40. As a 模板生产者, I want 模板可以承载二至五个高价值槽位, so that 复杂图片的独立可编辑内容不会被强行合并。
41. As a 模板生产者, I want 穷尽分析后确实只有一个高价值变量时允许单槽交付, so that 稀有真实单槽模板不会被低价值装饰补数。
42. As a 模板生产者, I want 每个槽位同时通过用户动机、视觉可见、模型可控和机制保持门禁, so that 可检测元素不会自动变成编辑控件。
43. As a 模板生产者, I want 文字槽只开放身份文字、主要视觉文字或长文中的高价值词组, so that 海报微字和装饰文字不会形成超长低价值槽。
44. As a 模板生产者, I want 长文本优先抽取关键词或短语, so that 用户输入和生成排版保持稳定。
45. As a 模板生产者, I want 文字默认值和建议值拥有版面长度上限, so that 过长文字不会破坏模板布局。
46. As a 模板生产者, I want 主体开放时具体身份文字被移除、中性化或独立成短槽, so that 用户替换主体后不会留下旧姓名。
47. As a 模板生产者, I want 正式数据不承诺自动推导主体英文名或别名, so that JSON 只表达当前合同真正支持的能力。
48. As a 模板生产者, I want 标题使用中性的动作、关系、容器、钩子或场景, so that 主体完全替换后标题仍然成立。
49. As a 模板生产者, I want 标题通过最大差异输入测试, so that IP、年龄、性别、物种、发型和服装不会被默认值锁死。
50. As a 模板生产者, I want 标题和描述只陈述模板图可验证事实, so that 来源图节日、人物或物种不会泄漏。
51. As a 模板生产者, I want 默认值保持简洁易读, so that 用户可以快速理解槽位含义。
52. As a 模板生产者, I want 推荐项与默认值同轴、同颗粒度且不重复, so that 用户看到的替换建议稳定而有价值。
53. As a 模板生产者, I want Replacement Pool 与 Slot Suggestion Pool 分离, so that 第一阶段换图目标不会被错误当成用户推荐项。
54. As a 模板生产者, I want Prompt Template 是完整自然语言画面描述, so that 用户可以直接理解和编辑最终生图提示词。
55. As a 模板生产者, I want Prompt Template 包含全部结构化槽位, so that 槽位替换会真实反映在用户提示词中。
56. As a 模板生产者, I want Prompt Template 保留未入槽但仍有编辑价值的内容, so that 全文编辑模式可以修改这些画面部分。
57. As a 模板生产者, I want 槽位编辑和自由编辑归一为同一种 resolved prompt, so that 两种并列交互模式拥有一致语义。
58. As a 模板生产者, I want 用户字面对主体、文字、颜色、服装、道具和场景拥有最终权限, so that隐藏约束不会恢复用户已经改掉的默认内容。
59. As a 模板生产者, I want 固定媒介和防漂移要求保留在隐藏层, so that Prompt Template 保持用户可读和可自由编辑。
60. As a 模板生产者, I want visualContract 使用正向、可观察的视觉事实, so that 运行合同不再重复生产清除和自检脚手架。
61. As a 模板生产者, I want visualContract 分开表达媒介、画风、构图、关系和条件性色光, so that 数据简洁且可以被运行时逐项验证。
62. As a 模板生产者, I want targetInstances、inputBindings 和 visualContract 分工明确, so that 输入接管位置与跨输入保持的视觉事实可以稳定运行。
63. As a 模板生产者, I want 编译器审计 Prompt Template 与 runtimeSemantics 冲突, so that 开放内容不会被视觉合同中的默认值锁回。
64. As a 模板生产者, I want 正式 JSON 只输出当前白名单字段, so that旧生产审计信息不会继续混入业务数据。
65. As a 模板生产者, I want metadata 默认只包含 tags, so that正式记录保持小而清晰。
66. As a 模板生产者, I want 确有人工复核原因时记录 needsReview, so that DRAFT 状态拥有可理解的复核依据。
67. As a 模板生产者, I want subject 图片槽只保存上传模式和素材约束, so that 身份提取、位置绑定和画风规则统一由 runtimeSemantics 表达。
68. As a 模板生产者, I want 正式数据使用 `cover` 和 `referenceImage`, so that输出与当前正式样例和 Schema 一致。
69. As a 模板生产者, I want `cover` 与 `referenceImage` 指向同一张确认模板图, so that封面和运行参考没有 revision 偏差。
70. As a 模板生产者, I want 正式输出拒绝 `coverUrl`, so that Unified 和旧管理台的版本残留不会形成双写合同。
71. As a 模板生产者, I want OSS 上传只处理确认模板图, so that来源图和失败生成图不会进入正式数据。
72. As a 模板生产者, I want OSS 成功凭证与正式 JSON 分开保存, so that重试落盘时可以复用 URL 且不污染业务字段。
73. As a 模板生产者, I want 最终交付是一份已回填 URL 且验证通过的 JSON, so that我可以直接把它交给后续人工导入流程。
74. As a 模板生产者, I want sidecar 保留替换、分析、版本和审核证据, so that正式 JSON 精简后仍然可以追溯生产过程。
75. As a 模板生产者, I want 任一上游产物变化后下游证据自动过期, so that旧审核结论不会错误复用。
76. As a 模板生产者, I want revision 保持不可变, so that失败恢复和重新生产不会覆盖历史事实。
77. As a 模板生产者, I want 中断后从最近有效阶段恢复, so that长流程无需每次从网图重新开始。
78. As a 模板生产者, I want 状态和错误码稳定, so that我可以判断缺输入、可复核风险、硬阻断和执行失败。
79. As a 模板测试者, I want 明确指定一份现成模板 JSON 启动 T1, so that我可以单独验证模板编辑和真实生图效果。
80. As a 模板测试者, I want T1 默认一次生成一张图, so that测试成本可控且结果容易归因。
81. As a 模板测试者, I want T1 覆盖槽位替换、真实用户上传图和全文编辑, so that实际用户输入与两种编辑方式都能验证。
82. As a 模板测试者, I want T1 执行包绑定 JSON SHA、模板 revision、模板参考图和用户上传图 SHA, so that测试结果不会错配到其他数据版本。
83. As a 模板测试者, I want T1 结果不修改正式 JSON 和生产状态, so that测试失败只触发新的修订建议。
84. As a Skill 维护者, I want 所有机器枚举和状态只定义一次, so that文档、Python、JavaScript 和测试不会各自漂移。
85. As a Skill 维护者, I want SKILL.md 保持精简并通过明确指针加载细分规则, so that每次调用只读取当前分支需要的知识。
86. As a Skill 维护者, I want 每个内部模块拥有清晰输入输出, so that替换、分析、槽位和 JSON 编译可以独立修复。
87. As a 模板生产者, I want 身份换图自动覆盖同图全部身份单元, so that CP、搭档和多主体不会只换一个。
88. As a 模板生产者, I want 接触与遮挡逐关系审核肢体拓扑, so that 错指、多肢、融合和凭空肢体不会成为确认模板图。
89. As a 模板生产者, I want 每张模板独立编写和审核分类标签, so that 批量模板不会复用同一组泛标签。
90. As a 模板生产者, I want P8 从全部证据重算关键结果资格, so that 任一门禁结果被绕过、删除或篡改时都无法交付。
91. As a 模板生产者, I want T 恤印花和截图来源在 P1 先确定目标画布, so that 衣服、模特、黑色截屏框和设备界面不会进入第二阶段成图。
92. As a 模板生产者, I want 贴纸、装饰图标和商标逐项决定保留、同步或删除, so that P2 不会把合法视觉内容误判为污染。
87. As a Skill 维护者, I want 一个最高层端到端测试接缝, so that重构内部模块时仍能证明用户结果保持正确。
88. As a Skill 维护者, I want 历史 GoodCase 和 BadCase 变成固定 fixture, so that过往批次经验不会继续停留在复盘文字中。
89. As a Skill 维护者, I want 两份最新正式 JSON 同时作为合同样例和语义 bad case, so that字段形状得到保留，旧标题和数量冲突也不会被复制。
90. As a Skill 维护者, I want Skill 行为版本、内部产物 Schema 和正式合同版本分别管理, so that一次升级不会错误宣称所有合同同时变化。
91. As a Skill 维护者, I want 发布包拥有完整文件摘要, so that仓库源码与安装副本可以机器比对。
92. As a Skill 维护者, I want 安装副本只能从验证后的发布包产生, so that本地临时补丁不会悄悄进入生产。
93. As a Skill 维护者, I want 每个 Production Item 独立写入 production pin, so that批量运行中每张图都能回答自己使用了哪个版本。
94. As a Skill 维护者, I want 运行中的 Production Item 不静默升级, so that中途安装新版本不会改变后续阶段语义。
95. As a Skill 维护者, I want 合同升级产生差异报告和显式迁移记录, so that需要失效和重跑的阶段可以确定计算。
96. As a Skill 维护者, I want 新 Skill 完成真实影子批次后再冻结 1.0.0, so that发布版本建立在可用证据上。

## Implementation Decisions

- 新建并完成一个用户可见结果 Skill `produce-meme-template`。沿用已经初始化的 Skill 骨架，替换全部占位内容；不继续扩展上一轮 4 个业务 Skill、1 个路由 Skill和 11 个专业 Skill 的用户调用图。
- 对外只提供一个公共工作流接口，支持单图输入、批量输入和 `T1` 独立测试命令。批量输入返回逐个 Production Item 的结果集合，不自动形成共享业务实体。
- 公共生产工作流是最高测试 seam。生图、任务轮询、图像读取、视觉证据和 OSS 上传通过注入式 adapter 提供，确定性测试使用 fake adapter，真实集成测试使用实际 adapter。
- P0–P8 是一次完整生产调用的固定生命周期。P7 OSS 上传属于生产主流程；调用完整生产即授权在前置门禁通过后上传确认模板图，无需沿用旧流程的第二次固定命令。
- `T1` 与 P0–P8 使用独立生命周期、状态、目录和版本记录。T1 只接受已落盘且通过静态验证的正式 JSON，执行后端固定为 Codex 内置生图工具，不承担生产完成门禁。供应商队列/WAL 回放使用独立命名，不占用 T1 语义。
- 主流程状态依次表达接收、替换计划完成、生图准备完成、模板图生成、模板图确认、模板图分析、模板数据编译、静态验证、资源上传和最终化；不确定项进入 `NEEDS_REVIEW`，硬合同或画面失败进入 `BLOCKED`，外部执行异常进入稳定失败结果。
- 状态结果沿用现有 `completed`、`needs_input`、`blocked`、`failed` 外部结果词汇，并为每个未完成结果提供稳定错误码、证据和可恢复阶段。
- 每个 Production Item 创建独立 `Production Manifest`、`Production Pin`、revision 索引和产物依赖图。批量提交默认不共享版本 pin；恰好使用同一 release 的项目仍各自记录。
- 用户策略优先级固定为单图显式策略、共享批次显式策略、Skill 自主策略。策略可以只覆盖部分决定，其余部分由当前替换规范补齐，每个关键决定记录自己的 decision source。
- 自主替换使用来源组件操作，不再从未来槽位中选候选目标。主要目标默认一个，依赖闭包可以包含多个必须同步变化的区域和身份线索。
- Replacement Pool 只用于形成确认模板图；Slot Suggestion Pool 只用于用户编辑。两个集合分别建模、验证和记录。
- 自主类别路由至少区分普通真人、现实公众人物或偶像、历史人物、经典艺术主体、明确角色/IP、通用人物、动物、物体、食物、场景、视觉属性、文字和 unknown。
- 普通真人允许模型生成新身份；公众人物和知名 IP 允许同类替换；任何替换都需要保持角色功能、视觉可实现性、画风和笑点连续性，并完成身份文字闭包。
- Generation Package 从 Replacement Plan 确定性编译，包含任务声明、替换目标、依赖闭包、冻结项、六维媒介合同、残留清除、空间关系和输出要求。生成 adapter 无权扩大 changed set。
- 每次生成请求默认 `n=1`。提交成功后先持久化 request ID 和 WAL，再进入轮询；网络重试复用同一任务。重试预算按 failure class 配置。
- 模板图审核同时验证六维视觉合同和身份拓扑、文字拓扑、接触几何、清洁资产图四类专项合同。审核证据绑定 Generation Package、图片、证据图和方法版本的摘要。
- 满足全部硬门禁时由 Skill 自主产生 `Approved Template Image`。身份或语义不确定、多个有效方案价值接近和策略冲突可以请求人工决定；旧身份残留、水印残留、身份文字冲突、实例漏换、结构破坏和媒介明显漂移必须修正后重生成。
- P3 从确认模板图重新分析，来源网图实例描述不能作为默认值、标题、描述或 Prompt Template 的事实输入。来源身份只保留为 forbidden legacy claims 和残留检查依据。
- Template Analysis 保存事实置信度、机制、组件图、四个数量、文字区域、身份拓扑、六维视觉合同、专项合同、固定结构和编辑边界。
- 高价值槽位常态 2–5 个且通常约 3 个。首次只发现一个候选时继续复核主体、内容物、颜色、文字、服装、道具、场景和嵌套内容；穷尽后确实只有一个时允许单槽并保存例外证据。
- 主体通常属于高价值槽位。服装、造型、发型、姿势、颜色和配饰分别通过独立价值与可生成性检查，禁止按人物类型批量套用。
- 每个身份图片输入在 P3 裁决特征继承范围：默认继承用户上传图中清晰可见的特征，只将构成模板核心玩法的特征列为最小模板固定例外。服装默认跟随上传图；特定服装特征承担核心玩法时才进入 `keepFromTemplate`。细粒度裁决保留于 sidecar 并编译为 `runtimeSemantics.relations`，二态归属确定性写入每个身份 binding 的 `clothingOwnership`。
- 文字区域先分类角色和操作，再决定是否开放。只有身份绑定文字、主要视觉文字、承担笑点的关键句和长文中的高价值词组可以进入文字槽；装饰微字、版权出处、氛围填充和低价值说明保持固定或清理。
- 默认值优先使用 2–8 个中文字符，原则上不超过 12 个字符；精确画内文字按实际内容处理。文字槽另外受角色和版面容量约束。
- 主体开放时，确认模板图和正式 JSON 默认态中的具体身份文字必须删除、中性化或转为具有中性默认值的独立短文字槽。正式合同没有声明自动联动时，不生成身份解析、别名推导和计算默认值字段。
- title 统一使用中性的画面机制、动作、关系、容器、视觉钩子或场景，通过最大差异输入测试。具体 IP、姓名、年龄、性别、物种、发型、服装和身份配色不进入标题。
- Prompt Template 是用户可见、可全文编辑的完整自然语言画面描述。它包含全部结构化槽位和未入槽但仍有编辑价值的内容；结构化编辑和全文编辑最终编译为同一种 resolved prompt。
- Prompt Template 拥有用户内容权限。隐藏字段不能恢复用户修改后的主体、文字、颜色、配饰、服装、道具和场景。
- `inputSchema` 固定为 `{version: 2, slots: [...]}`，每个槽的 `text` 与 `image` 模式正交组合。`targetInstances`、`inputBindings` 和 `visualContract` 从同一中间模型确定性编译。身份输入绑定一个目标时使用 `one_to_one`，同一来源身份覆盖多个固定实例时使用 `same_source_repeated`。
- 内容图片输入绑定 `content_element`；身份图与内容图并存时必须提交来源隔离裁决，每个内容图目标必须提交 `post_edit/template_fixed/independent` 容器分类。
- `visualContract` 使用正向、可观察的媒介、画风、构图、关系和条件性色光事实；编译器必须检查它与开放槽位和自由编辑内容的冲突。
- `metadata.tags` 每图独立编写 2–5 项，逐项绑定 Approved Image 可见事实与分类价值；泛标签、重复标签和无单图证据的批量共用标签在 draft 前阻断。
- P6 产出 27 项关键结果资格账本，P8 从冻结生图包、视觉审核、作者审核和验证报告重算并精确对账后才可上传。
- 正式模板记录使用白名单投影，只保留 `key`、`status`、`title`、`description`、`imageSize`、`imageN`、`kind`、`promptTemplate`、`inputSchema`、`preprocessSteps`、`runtimeSemantics`、`metadata.tags`、条件性的 `metadata.needsReview`、`cover` 和 `referenceImage`。
- 新模板的 `inputSchema.slots[].image` 不输出 `enabled` 或 `extract`；`runtimeSemantics` 是输入目标、绑定和视觉约束的唯一正式运行权威，正式 JSON 不输出 `promptEnhancement`。新生产只写 runtimeSemantics v2；T1 可读 v1/v2；存量 v1 以显式服装裁决迁移到隔离输出，不覆盖原数据。
- `candidateScope`、`runtimeRequirements`、`templateSource`、`inputSemantics`、`suggestionRationales` 和 `optimizationAudit` 以及其他生产审计字段全部保留在 sidecar，不进入正式 JSON。
- 当前正式封面字段固定为 `cover`。`coverUrl` 不输出、不双写，并作为版本冲突 fixture 持续验证。
- P7 只上传当前 Approved Template Image。Asset Receipt 保存图片摘要、对象键、URL 和幂等信息；P8 将同一 HTTPS URL 写入 `cover` 与 `referenceImage`。
- OSS 已成功而最终落盘失败时，恢复流程复用 Asset Receipt，不重复上传。图片 SHA 不变的 metadata 修订可以显式复用已有 URL。
- 生产目录保留正式 JSON、manifest、pin、replacement plan、template analysis、editable spec、hidden spec、validation report、asset receipt 和 evidence。下游只读取正式 JSON。
- 正式 JSON 默认状态采用当前合同支持的 DRAFT；确有复核原因时通过 `needsReview` 表达。Skill 不负责数据库导入和发布状态推进。
- 机器规则单一来源至少覆盖类别枚举、图片操作、文字角色、状态迁移、失败类型、runtimeSemantics 结构、视觉维度、字段白名单和 Schema 条件。文档解释语义，编译器和验证器读取同一份机器定义。
- Skill 使用渐进式披露。主文件保留端到端步骤与完成条件；替换、文字、模板图分析、槽位、用户文案、JSON 编译、生命周期、OSS 和 T1 分别通过一级 reference 指针按分支加载。
- 确定性 scripts 负责合同编译、Schema 验证、正式投影、placeholder 解析、冲突审计、版本摘要、依赖失效、恢复和发布校验。推理性判断由 Skill 按 reference 执行并产出结构化证据。
- Skill、内部 artifact Schema 和 gallery contract 使用三个独立版本。唯一 release 描述文件是人工修改版本的事实源，其他版本声明由脚本生成或核验。
- 发布包不可变并包含全部发布文件、内部 Schema、上游合同快照、编译器、验证器和 fixture 集的 SHA-256。安装副本只从通过验证的发布包产生。
- 每次生产开始执行 doctor，核对安装版本、release digest、额外文件和合同兼容范围。`live_external` 还要求运行目录来自已验证安装包，并在 P0 前核验 Aliyun OSS → Fal → 独立审核 delegate 的执行画像；检查失败时停止生产且不调用 Fal/OSS。fixture 与 source worktree 固定为不可交付回放。
- 上游正式合同以只读快照管理，新版本通过新增快照、差异检查和显式适配进入；合同变更不会静默覆盖历史快照。
- 首个开发版本从 `0.1.0` 开始。确定性测试、前向测试和真实批次影子运行全部通过后才冻结 `1.0.0`。
- 上一轮拆分 Skill 和 Unified 保持只读迁移事实源，直至迁移矩阵全部有新规则落点和回归证据。新实现不复制旧目录结构，不在旧 Skill 上继续堆补丁。

## Testing Decisions

- 好测试只断言用户可观察行为：输入、状态、正式产物、sidecar 合同、稳定错误、允许发生的生图与 OSS 副作用。测试不依赖内部函数调用顺序、reference 加载顺序或模块数量。
- 最高测试 seam 是公共生产工作流。使用来源图片 fixture、可选策略和 fake generation/OSS adapter 驱动完整 P0–P8，断言逐阶段状态、不可变产物和最终正式 JSON。
- `T1` 使用同一命令入口下的独立子命令测试，断言它只消费指定 JSON 与真实用户上传图、生成 `codex-imagegen-request.json`、不调用 Fal/OSS 且不改变 Production Item。
- 优先复用现有工作流的注入式生成器与上传器、不可变 revision、Artifact Manifest、合同版本错误、上传幂等和最终 URL 一致性测试经验；现有三阶段目录和候选槽位语义不作为新行为断言。
- 为公共生产 seam 建立最小 tracer fixture：单张简单网图在自主策略下完成主要目标替换、模板图确认、三槽设计、正式投影、OSS 回填和最终落盘。
- 建立批量隔离测试：两张图同批提交时分别拥有分析、替换、pin、状态和输出；交换任一图片的 sidecar 或模板图必须失败。
- 建立显式共享策略测试：只有带共享策略的输入才执行跨图 IP 池、比例、去重和稳定分配；删除策略后所有项目恢复独立行为。
- 建立策略优先级测试：单图显式决定覆盖共享策略，共享策略覆盖自主选择，未覆盖决定继续由默认规范补齐。
- 建立 Replacement Plan 稳定性测试：输入、策略、seed 和版本一致时得到同一计划；unknown 和冲突策略返回稳定复核或阻断结果。
- 建立同类自主替换 fixture，覆盖普通真人、公众人物、二次元 IP、猫、狗、食物、物体、文字和场景属性。
- 建立普通真人生成身份 fixture，证明无需固定本地身份资产；旧脸、旧身体、多人串脸和身份文字残留分别为红例。
- 建立知名 IP 同类替换 fixture，验证角色锚点、反锚点、姿态兼容、媒介兼容和身份文字闭包。
- 建立单一主要目标测试，证明关联文字、影子、倒影和重复实例属于 dependency closure；未授权的第二主要目标变化构成漂移。
- 建立 Generation Package 编译快照，覆盖 changed set、frozen set、来源目标画布、来源标记逐项策略、六维合同、接触关系、清洁要求和一张图默认值。
- 建立 WAL 与恢复测试，证明 request ID 先于轮询持久化、网络恢复不重复 submit、不同 failure class 回到正确阶段。
- 建立模板图视觉审核 fixture，覆盖媒介漂移、结构破坏、旧身份残留、文字错误、水印残留、载体/截屏框残留、贴纸与商标误删、容器漏换、穿戴漂浮、反射漏换和分格漏换。
- 建立硬失败测试，证明人工决定不能把视觉硬失败直接推进为 Approved Template Image。
- 建立审核新鲜度测试，证明 Generation Package 或模板图 SHA 变化后旧审核证据失效。
- 建立模板图事实源测试，主动让来源图身份、物种、节日、数量和文字进入 title、description、默认值、Prompt 或 hidden fields，验证全部被拦截。
- 建立组件与四个数量测试，覆盖单主体、重复身份、多身份、同一身份多照片、嵌套完整照片、相框、分格和有序集合。
- 建立槽位预算测试，覆盖常态三槽、合法二槽、合法五槽和有证据单槽；低价值装饰补数必须失败。
- 建立人物派生槽测试，证明服装、造型、发型、姿势和颜色分别依据图片价值决定，不按人物模板统一开放。
- 建立 subject 身份特征继承测试，证明继承范围与模板固定例外必须完整、不重叠，合法裁决进入 `runtimeSemantics.relations` 且不泄漏为正式 `inputSchema` 字段。
- 建立文字槽测试，覆盖身份文字、文字卡、主标题、长文关键词、装饰微字、版权信息、双语依赖、标点和符号拓扑。
- 建立文字容量测试，验证默认值、推荐项和用户输入的角色化上限；整段长海报文字不能成为一个槽。
- 建立身份文字去冲突测试，使用“主体替换为另一角色、文字未手动编辑”的情形，证明正式默认态不会保留旧专名。
- 建立中性标题测试，对每个主体开放模板执行最大差异输入替换；标题仍需准确、自然且可由模板图核验。
- 建立推荐项测试，覆盖同轴、同颗粒度、默认值去重、生成可行性和纯图片槽无需文本建议。
- 建立 Prompt Template 测试，证明全部结构化槽位都有绑定，非槽位自由编辑内容允许存在，所有默认值和推荐项代入后语法自然。
- 建立多主体换图红例，证明遗漏 CP 成员、搭档成员、独立第二主体或任一专属组件时 P1 阻断且不调用生图 API。
- 建立交互肢体红例，对每条接触和遮挡关系否定部位可溯源性、拓扑、接触、遮挡或无融合结论，确认 P2 不产生 Approved Image。
- 建立逐图标签红例，覆盖泛标签组、重复标签、数量越界和独立审核否定，确认均在正式编译前停止。
- 建立关键结果资格重放测试，删除或篡改账本后重试 P8，确认不上传且不产生正式 JSON。
- 建立两种编辑模式等价测试，证明槽位编辑与全文编辑最后都得到完整 resolved prompt。
- 建立用户权限冲突测试，证明 `visualContract` 不会恢复用户修改过的主体、文字、颜色、服装、道具和场景。
- 建立 runtimeSemantics 测试，覆盖目标唯一性、输入—目标类型匹配、`one_to_one`、`same_source_repeated`、`identity_group + preserve_group + group_photo`、内容组分配、来源隔离、容器分类、媒介、画风、构图、关系、`clothingOwnership` 和 v1→v2 显式迁移。
- 建立正式投影测试，对两份最新正式样例的全部 110 类归一化叶子路径执行分类；未分类数必须保持为 0。
- 建立字段白名单测试，正式 JSON 只允许当前合同字段；旧流程 metadata、临时路径、Data URL、生产术语和 `coverUrl` 必须失败。
- 建立 Schema 测试，正式 JSON 通过冻结的 runtimeSemantics v2 Schema；`image.extract` 与 `promptEnhancement` 均被拒绝，placeholder 全部可解析。
- 建立四层验证测试，分别产出 schema、semantic、visual contract 和 gallery contract 证据；任何一层失败都不能汇总为 PASS。
- 建立 OSS 测试，证明只上传 Approved Template Image，`cover === referenceImage`，失败恢复复用 receipt，危险对象键在调用存储前被拒绝。
- 建立 sidecar 隔离测试，删除 sidecar 不改变正式 JSON 的运行语义，正式 JSON 也不包含 artifact metadata。
- 建立不可变 revision 和 artifact graph 测试，证明上游变更准确失效对应下游，重试不会覆盖历史 revision。
- 建立版本漂移测试，覆盖源码与安装副本差异、release 额外文件、合同 major 不兼容、运行中升级和批量混合 pin。
- 建立 Skill 结构测试，覆盖 frontmatter 仅含允许字段、description 触发范围、reference 指针、脚本入口、UI metadata 和 manifest 跟踪文件一致性。
- 建立规则唯一来源测试，扫描重复枚举、状态和字段白名单，防止验证器、脚本和文档再次各自维护副本。
- 将机器合同声明的全部历史经验逐项绑定至少一个绿 fixture 或红 fixture；迁移状态和验收证据缺失时禁止宣称能力迁移完成。
- 两份最新正式 JSON 原样作为输入 fixture，并建立修正后的 expected fixture，覆盖中性标题、宠物数量一致性、Prompt 自由编辑内容和旧 metadata 投影。
- 确定性测试不调用真实图片 API、OSS、管理台或数据库。真实 adapter 通过单独集成测试验证。
- Skill 内容和脚本完成后使用结构验证器、全量单元测试和端到端 smoke test；随后通过最小上下文的独立前向测试检查触发、流程遵循和产物质量。
- 发布 `1.0.0` 前先以固定录制回放覆盖普通真人、知名 IP、动物、物体、文字梗和复杂多实例，再执行四图 live 风险采样：普通真人、文字密集、复杂多实例为必测项，知名 IP 与动物任选一项补充；逐张新生成图片独立审核模板图与最终 JSON。容器、穿戴、背景和混合媒介继续由对应场景与历史经验 fixture 交叉覆盖。

## Out of Scope

- 前端槽位编辑器、自由编辑器和交互实现。
- 后端 Worker、Prompt 拼接链路、身份自动识别、别名推导和运行时联动实现。
- 数据库、UAT 或 production 的自动导入、发布与上线状态推进。
- 管理台任务中心、任务 ID、状态轮询、索引刷新和管理台 API。
- 默认批量共享策略、跨图组合分配、统一版本 pin、标题去重和汇总报告。
- 把 T1 设为 P0–P8 的必要阶段、上线门禁或批次完成条件。
- 一次生成请求默认产出多张正式模板图。
- 通过修改 JSON 掩盖模板图的旧身份残留、结构破坏和媒介漂移。
- 在正式 JSON 中增加当前 Schema 未声明的自动联动、审核、版本或生产过程字段。
- 为旧消费者双写 `coverUrl`，或承诺研发侧缺失文档中的运行行为。
- 按新规范批量清洗历史模板 key。
- 既有模板的通用维护与后台导出更新流程；该能力继续由独立维护工作流负责。
- 管理台部署、服务暴露、插件打包、代码提交、Tag、推送和生产发布。

## Further Notes

- 本规格取代旧仓库 `xiaoyuan-coder/memebuy-image-analyser#1`“拆分槽位模板生产为三阶段 Skill 家族”的总体方向。旧规格及其子票据保留为失败经验和迁移事实源，不自动关闭或删除。
- 当前仓库已经存在一个只含初始化占位内容的 `produce-meme-template` Skill 草案。实现从该骨架继续，无需再次初始化。
- 当前工作树含有大量其他进行中修改。实现必须按 ticket 隔离改动，避免把管理台、风格模板和其他用户工作混入本重构。
- 当前正式合同依据是用户确认的数据口径、冻结 Schema、两份最新正式样例及其 SHA，以及已接受 ADR。研发编译链路文档和后端实现不构成阻断条件。
- 两份最新样例用于冻结字段形状和回归问题，不能直接复制其中写死主体的标题、数量冲突描述和旧流程 metadata。
- ADR 完整性审计已覆盖全部 52 条 Implementation Decisions；ADR 0001–0031 中 30 份生效，ADR 0007 已由 ADR 0008 取代。全量规范注册表还必须对所有可达权威文档的每个语义单元重算覆盖，缺少可执行所有者、Good Case、Bad Case 或历史经验时禁止完成。完整映射见《ADR 决策覆盖矩阵》；用户已确认当前决策前沿为空。
- 生产价值的两个最高优先级枢纽是：P1–P2 形成正确的 Approved Template Image，以及 P3–P6 将模板图事实编译为正确的用户编辑合同和正式模板 JSON。
- 第一轮实现应先打通一个真实纵向切片：单图自主替换、生成一张模板图、自动确认、三槽分析、正式投影、fake OSS 回填和最终落盘；随后再扩展复杂身份、文字、批量策略、恢复和版本发布。
- 下一阶段使用 `/to-tickets` 将本规格拆成 blockers-first 的 tracer-bullet tickets。每张票据需要声明阻塞关系、外部验收行为和它所迁移的历史经验 ID。

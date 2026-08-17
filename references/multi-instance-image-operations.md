# 多实例组件图与图片操作合同

## 1. 四类数量独立计算

来源分析与 Approved Template Image 分析都要提供结构化 `componentGraph`。组件图分别记录：

- `visualInstance`：画面中可辨认的视觉实例；
- `identityUnitId`：多个实例是否属于同一身份；
- `uploadAssetId`：用户需要提供的独立素材；
- `controlId`：前端实际暴露的编辑控件。

P3 以 Approved Template Image 为事实源，从组件图重新计算四类数量，并与 `assetUnitAnalysis` 精确对账。一个身份可以拥有多个画面实例和一份上传素材；一个控件也可以驱动多个实例。多个独立上传素材映射到同一个主体控件时，该控件的 `image.maxCount` 必须等于素材单元数且不超过冻结 Gallery Contract 上限；每份素材只能归属一个主体上传控件。任何数量都不能从另一类数量推导。

每个 `controlId` 还必须符合机器合同的“组件角色 → 槽位类型 + 语义角色”绑定。例如背景组件只能承载场景内容/氛围 prompt，文字组件承载可见文字槽，主体、道具、反射或容器内图片才能按合同承载主要上传槽。Approved analysis 还必须用 `approvedOperationBindings` 为每个 operation 显式列出目标组件、稳定锚点和控件；目标和锚点列表按位置逐项对应来源 operation 的同类列表。P3 按 operation ID 精确对账，不从同角色组件或全图控件反推目标。所有 binding 的 Approved target 组件在全局只归一个 operation 所有。一个 binding 列出的控件不得同时驱动目标集外组件；`identity_replace` 选定身份的全部组件必须精确落入该 binding。这一对账同时防止数量正确但控件指向错误区域，以及用无关组件伪造目标完整性。

## 2. 组件、容器与关系

组件使用唯一 `componentId`，并分别声明角色、身份、视觉实例、上传素材、控件和可选容器。容器必须指向同一组件图中的另一个组件，不能形成自引用。

关系使用唯一 `relationId`，连接两个已有组件。机器合同支持重复身份、镜像、阴影、反射、嵌套、接触、遮挡和有序排列。重复身份、镜像、阴影和反射关系的两端必须具有同一个非空身份单元。重复实例、镜像、阴影、反射、相框内副本和其他身份派生内容必须与主体进入同一身份闭包；依赖闭包的每一项都必须具备唯一组件 ID，组件集合与图片操作目标集合精确一致。P3 对 Approved 组件图重放同样的派生身份约束，并要求 `ordered_set` 的 Approved 面板仍形成完整单链。来源操作目标的组件角色与列表位置必须由同一个实际目标组逐项覆盖；每一条 `preservedRelationIds` 都必须在 Approved 图中有独立的同类型对应，其源端和目标端必须分别精确指向 binding 中与来源组件同位置的 Approved 组件。需要容器锚点的操作还必须让目标的实际 `containerId` 落入该 binding 锚点。全图其他身份、道具或容器上的同类角色与关系不能代替当前 operation 证据；删除或伪造反射、阴影、嵌套、接触和遮挡关系会在 P3 阻断。

`dependencyClosure.type` 只使用 `identityReplacementContract.dependencyTypes` 的唯一机器枚举。身份类依赖必须与组件角色及镜像/阴影/反射/嵌套等关系拓扑对应；场景、局部遮罩、容器内容和有序成员由操作到该枚举角色的映射解引。任意字符串或与拓扑不符的类型都在生成前阻断。

## 3. 五类图片操作

每个操作都必须声明唯一 ID、操作类型、目标区域、清除要求、稳定锚点、需要保持的关系和可定位证据。

| 操作 | 用途 | 核心约束 |
| --- | --- | --- |
| `identity_replace` | 主体身份及其派生实例统一替换 | 同一身份的主体、重复实例、镜像、反射和阴影全部入目标集 |
| `scene_replace` | 保留主体并替换场景 | 背景进入目标集，主体与前景作为稳定锚点 |
| `mask_fill` | 局部对象或区域重绘 | 先清除旧内容，再保持与非目标区域的接触和遮挡 |
| `content_replace` | 替换相框、屏幕或容器内部内容 | 内部内容进入目标集，外层容器作为稳定锚点 |
| `ordered_set` | 多人分格、多面板或有序实例组 | 分别记录每个目标区域，容器必须由不分叉、不断裂的 `ordered_before` 完整链覆盖 |

目标区域和稳定锚点必须存在且互不重叠。与目标相连的接触和遮挡关系必须显式列入 `preservedRelationIds`。来源证据结构损坏返回稳定外部失败；证据结构完整但漏掉闭包或关系时在生成前阻断。

## 4. 生成与视觉硬门禁

P1 将完整图片操作写入 Replacement Plan，P2 Generation Package 逐字继承该列表。核心把当前绑定摘要和只读 `imageOperations` 一起传给视觉审核 adapter；adapter 不依赖构造期带外状态。视觉审核按操作 ID 逐项回答：

- 旧目标是否清除干净；
- 稳定锚点是否保持；
- 接触和遮挡关系是否保持；
- 非目标服装、背景、道具和文字是否无漂移。

任一回答为假都属于视觉硬失败，不能创建 Approved Template Image，也不能上传。证据列表缺项、增项、重复、字段形状错误或与当前操作 ID 不一致，按外部审核证据失败停止。

## 5. Fixture 与经验追踪

`fixtures/e2e/multi-instance/` 提供相框整图、多人分格、重复宠物、人物接触物体和场景替换五类场景。公共 seam 测试覆盖 E06、E12、E20、E22、E23 和 E29，观察 Replacement Plan、Generation Package、Approved 组件图、视觉硬失败、正式交付和上传副作用。

字段名、角色、关系、操作和视觉证据字段只读取 `contracts/machine-rules.json` 的 `multiInstanceContract` 与 `visualReviewContract`。

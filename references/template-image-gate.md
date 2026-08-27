# 模板图确认与 P2 恢复合同

## 1. 确认输入与证据绑定

生成结果先登记为 Generated Candidate Image。视觉审核必须同时绑定当前 `generation-package` 摘要、候选图片摘要、机器规则声明的完整证据摘要、检查方法与版本、检查时间。任一绑定缺失、过期或与当前事实不一致，都属于外部证据合同失败，不能产生 Approved Template Image。

## 2. 硬门禁

P2 必须逐项验证主要目标替换、依赖闭包、非目标保持、画面机制、接触几何、可见文字、全画布清洁、水印清除和旧身份清除。medium、form、edge、color and shading、surface、composition 六维分别保存布尔结果与图像证据。全画布清洁额外记录幽灵字、伪签名和平台水印发现项；其他来源标记全部读取 P1 的逐项策略。

P2 审核请求还携带 P1 冻结的 `sourceCanvasDecision` 与 `sourceMarkPolicy`。审核必须证明目标画布动作完成、排除的衣服/模特/黑框/设备界面已经消失、设计内部结构仍被保留，并对每个来源标记返回精确匹配的动作结果。普通贴纸、装饰图标和已批准保留的商标不属于画面污染；它们仍然存在时不能触发清洁失败，意外消失或漂移时触发非目标保持失败。水印、平台标、账户标、伪签名、无关 Logo 和其他 required-remove 项仍然存在时触发全画布清洁失败，水印类同时触发水印清除失败。

`fullCanvasClean` 的有效结论由结构化污染发现项和来源标记动作证据派生。视觉审核 adapter 的泛化全画布布尔值只保留为原始观察，不能单独否决已批准保留的贴纸或商标。`watermarkFree=false` 继续直接阻断，并与逐项水印证据共同形成交叉验证。目标画布证据、逐项标记证据或与 P1 决策的精确绑定缺失时，按外部证据合同失败停止。

对组件图中每一条 `contact` 或 `occlusion` 关系，视觉审核还必须提交一条精确对应的交互完整性证据，同时确认手指、手臂、腿脚等主体部位可溯源到对应躯干，拓扑数量合理，接触位置成立，遮挡顺序成立，且没有融合、多肢、少肢或凭空出现的部位。关系证据缺失、端点不一致或任一结论为否，统一触发接触几何硬失败。

视觉证据还包含机器规则声明的 `identityTextEvidence`。非身份路由明确标记不适用；普通真人、公众人物和知名 IP 路由明确标记适用，并分别证明来源身份文字已消失、结果文字与新身份一致。适用性、布尔结论、证据说明或证据摘要缺失时，工作流以外部证据合同失败停止；旧身份残留或结果不同步时派生为视觉硬失败。

P3 模板分析只接收与 Approved Template Image 字节一致的只读临时快照，核心产物路径不暴露给 adapter。工作流在调用前保存 P2 已审核摘要，调用后验证快照与正式 Approved Image 未变、分析证据仍绑定调用前摘要；快照被改写、删除或替换类型也统一返回稳定外部失败并停止上传。P7 上传使用同一快照边界，上传凭证继续绑定调用前摘要。

P2 通过后还要编译 `authoring-handoff.json`：它绑定 P1 `authoring-intent.json`、当前 Generation Package、Visual Review 和 Approved Image SHA，并保留机制、IP/文化身份、主体连续性、替换拓扑与审批增量。P3 adapter 同时接收只读图片快照和只读 Handoff；任一对象在调用中变化都使证据失效。

任一硬门禁或六维事实失败时，工作流将审核决定派生为 `rejected`，返回机器规则中的视觉硬失败并停止在 P2。请求中的人工决定和 adapter 自报决定都不能覆盖硬失败。

## 3. 自主确认与风险升级

完整证据清晰通过时，工作流自主派生 `approved` 并复制候选图为 Approved Template Image。身份歧义、审美风险、证据不足或多个有效方案接近时派生 `needs_review`，不创建确认图，也不执行模板分析或上传。

## 4. 不可变重做

视觉硬失败后的同一 Production Item 默认保持阻断且不增加供应商请求。用户明确授权 `authorizeVisualRedo=true` 后才从 P2 重做，复用摘要校验通过的 P0 来源分析和 P1 替换计划。每次已授权重做递增 manifest revision，生成新的 request ID，并使用 `-rN` 保存 generation package、候选图、视觉审核和确认图；既有 revision 不覆盖。

manifest 的失效事件记录被替代的 generation package、新 package、两者摘要、旧依赖后代以及 P2–P8 失效阶段。新模板分析只绑定当前 revision 的 Approved Template Image 和视觉审核。歧义或证据不足保持人工复核状态，不自动重做。

## 5. 副作用边界

视觉审核未通过或证据合同失败时，不调用模板分析、语义审计、上传或正式投影。P2 重做只再次调用生成与视觉审核；已验证有效的 P0/P1 不重复。真实服务提交、request ID WAL、任务轮询和跨进程恢复读取 `generation-execution-and-recovery.md`。

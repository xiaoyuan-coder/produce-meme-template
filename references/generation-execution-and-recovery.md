# 生成执行与 WAL 恢复合同

## 1. 请求与任务冻结

每次 P2 默认请求一张图。调用方可在 `generationOptions` 中显式设置生成数量和主输出索引，数量上限、字段名与默认值只读取 `contracts/machine-rules.json` 的 `generationExecutionContract`。规范化生成选项的摘要进入 Production Item 身份，同一生产项不能在恢复时静默改变输出数量或主输出。

工作流在调用供应商前创建不可变 `generation-task[-rN].json`。任务同时绑定：

- Source Web Image SHA、Generation Package SHA 和 Production Pin SHA。
- revision、生成 request ID、完整 Prompt、尺寸、格式、输出数量和主输出索引。
- request intent SHA、输入总摘要和稳定 task ID。

任何冻结输入变化都会产生新任务身份。已完成项和 P7 恢复还会重算 task、WAL、主输出与候选图的语义对账；单独改写 manifest 摘要无法绕过该门禁。

## 2. 提交日志

工作流先持久化 `generation-wal[-rN].json` 的 `prepared` 状态，再调用 `submit_generation`。供应商返回后，核心验证结果形状和不可变请求，立即把 provider、model 和 request ID 写入 `submitted` WAL，然后才允许 `poll_generation`。

WAL 只保存恢复必需的机器事实：task ID 与 task SHA、revision、供应商任务身份、轮询次数、失败分类、输出身份与 SHA。供应商输出 URL 仅在内存中用于下载，WAL 保存不可逆的安全输出 ID。失败详情只保存摘要；Data URI、API key、临时图片字节和未脱敏例外不进入持久化产物。

如果 submit 超时或连接在响应前断开，提交结果记为 `submission_unknown`。工作流保留 prepared WAL 并要求调用方先确认供应商状态，同一任务不会自动再次 submit。

## 3. 轮询、输出与失败分类

每次轮询前先增加并持久化 `pollAttemptCount`。达到预算的 submitted WAL 不会再次轮询。成功结果必须与冻结输出数量一致，每个输出保存安全 provider output ID 和 SHA，主输出的 ID/SHA 还必须与实际 Generated Candidate Image 一致。候选图片必须由受信任解码器完成格式、单帧、尺寸和像素上限验证，扩展名与冻结输出格式精确一致。

图片 URL 的初始地址、每一跳 redirect 和最终地址均执行 HTTPS、主机、端口、凭据与公网地址校验。loopback、private、link-local、reserved 和 localhost 目标在读取响应内容前拒绝。

失败分类与路由由机器合同唯一定义：

- `retryable`：保留 provider request ID，在预算内继续轮询同一任务。
- `replan_required`：停止在重新规划路由，不继续视觉审核。
- `human_review`：进入风险人工复核。
- `permanent`：以稳定失败结果结束，不重试。
- `submission_unknown`：暂停自动执行，禁止重复提交。

`retryable` 超过机器合同中的预算后升级为永久失败。失败 WAL 保留分类、原因和已消耗次数，不伪造输出证据。

## 4. 恢复与失效

跨进程重跑先验证 Production Item 身份、pin、Generation Package、冻结 task 和 WAL。提交前进程退出时，核心可重算并登记完全一致的 package/task/prepared WAL，然后执行首次 submit。WAL 先于 manifest 落盘的合法一步前滚由 `previousWalSha256` 证明并修复 manifest 摘要；任何非 prepared WAL 都必须保留合法前驱摘要。

已有合法 request ID 时，核心重建提交快照并只调用 poll；请求提交次数保持不变。succeeded WAL 与本地候选图、task、package、pin 和输出摘要全部对账时，恢复直接进入视觉审核，不依赖供应商结果继续可用。视觉硬失败重做使用新 revision、新 Generation Package、新 task 和新 WAL，同时复用校验通过的 P0/P1。

候选图仍要通过模板图确认合同的六维视觉、改动集、稳定锚点、非目标漂移、文字与水印门禁。供应商返回成功只证明生成任务完成，Approved Template Image 仍由工作流硬门禁派生。

## 5. 真实 FAL 适配器

`FalQueueWorkflowAdapters` 使用 `fal-client` 的持久队列接口执行 `openai/gpt-image-2/edit`，并将分析、视觉审核、语义审计和上传转发给注入的 delegate。运行环境通过 `FAL_KEY` 提供凭据，调用方负责组合真实下游 adapters 并承担 API 成本。

默认测试不访问付费服务。公共工作流集成测试使用真实 FAL adapter 实现和可控传输，验证 submit/status/result/download、任务形状、WAL 持久化与敏感内容不落盘。付费影子运行需由用户显式授权。

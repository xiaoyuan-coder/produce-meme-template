# Fal 生图质量配置回归事故说明

- 事故日期：2026-08-27
- 影响阶段：第二阶段 P2 模板图生成
- 影响模型：`openai/gpt-image-2/edit`；少量内容重生请求使用 `openai/gpt-image-2`
- 直接配置：`contracts/machine-rules.json#/generationExecutionContract/fal/quality`
- 引入提交：`dc04904`（2026-08-17）
- 修复版本：`6.0.1`

## 结论

Skill 迁移时将历史默认质量 `low` 写成了 `high`。现有需求与 Issue #10 均没有授权这项质量升级，配置变更也缺少请求级成本回归断言。该问题属于迁移引入的配置回归，用户没有手动把质量改为 `high`。

## 影响范围

对 2026-08-17 之后留存的 Generation WAL 进行只读去重审计，确认 282 个具有唯一 Fal `providerRequestId` 的任务：264 个成功、17 个失败、1 个仍为 submitted。另有 13 次没有 provider request ID 的提交失败，未计入确认任务数。

按 Fal 官方公开价格、实际输出尺寸和 2026-08-26 人民币汇率中间价估算，成功图片费用约为人民币 359 元；把所有确认任务都按扣费处理约为 385 元。相同任务采用 `low` 预计约为 29 元，因此可归因于本次质量配置回归的增量费用约为 330 元。自定义尺寸和失败任务的精确账单以 Fal 后台为准；现有 WAL 没有保存 `X-Fal-Billable-Units`，所以无法精确到单次请求。

计价依据：

- https://fal.ai/models/openai/gpt-image-2/edit
- https://fal.ai/models/openai/gpt-image-2
- https://fal.ai/docs/documentation/model-apis/faq
- https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do?COLLCC=1275801109

## 根因

1. 新 Skill 实现首次接入真实 Fal 队列时直接写入 `quality: high`，迁移过程没有对照旧流程的 `low` 默认值。
2. 需求只覆盖真实提交、WAL 和恢复，没有把质量档位与成本控制作为验收项。
3. 原测试仅验证模型、数量、输入图和队列恢复，没有断言实际提交的 `quality`。
4. README 没有公开当前模型和质量，日常检查难以及时发现配置漂移。

## 纠正与防复发

1. 唯一机器事实源中的 P2 Fal 默认质量恢复为 `low`。
2. 公共 Fal 工作流测试同时断言合同值和实际提交参数均为 `low`；未来改成 `medium` 或 `high` 会直接导致仓库测试失败。
3. README 的当前状态表公开 P2 Fal 模型和默认质量，维护者无需进入适配器源码即可核对。
4. Skill 行为版本升级为 `6.0.1`，旧 `6.0.0` 生产 pin 保持可追溯，禁止静默继承修复。
5. 旧 Production Item 如需继续调用 Fal，应先执行显式版本迁移或创建新生产项；继续使用已安装的 `6.0.0` 会保留旧 `high` 配置。

## 当前状态

源代码修复、回归测试和事故说明属于本次 `6.0.1` 变更。按照仓库发布治理，安装副本只能来自验证后的不可变发布包；提交、构建、安装与切换需单独执行发布流程。

# ADR 0022：正式 JSON 采用 runtimeSemantics v2 合同

- 状态：已接受
- 日期：2026-08-18

## 背景

研发于 2026-08-17 更新 Gallery Template 作者合同，并以五条模板完成后端写入和独立生成验证。新合同将输入目标、绑定和视觉约束统一提升为 `runtimeSemantics`，由 Worker 一次性编译运行提示词。旧合同要求的 `inputSchema[].image.extract` 与作者手写 `promptEnhancement` 已退出新模板正式字段。

## 决定

新生产项使用 `runtime-semantics-v2-contract` 快照。正式 JSON 输出 `imageN: 1`、`kind: "PROMPT"`、`preprocessSteps` 和 `runtimeSemantics`；subject 图片槽删除 `image.extract`，正式投影删除 `promptEnhancement`。`runtimeSemantics.version` 固定为 `1`，每个开放输入通过 `inputBindings` 指向具名 `targetInstances`，媒介、风格、构图、关系和色光进入 `visualContract`。

正式投影继续保持白名单；本 Skill 默认只输出 `metadata.tags` 与条件性的 `metadata.needsReview`。作者 Schema 保留研发合同声明的可选字段和 `communityKey`/`featureKeys`，P8 仅生成仓库职责内的稳定子集。subject `text` 不再额外输出 placeholder。`cover` 与 `referenceImage` 分别保存并继续指向同一张 Approved Template Image。存量 v1 样例与旧合同快照保留为迁移证据，不参与新生产项编译。

## 影响

- Gallery Contract 切换为独立的新快照，旧 `current-cover-contract` 保持只读。
- Artifact Schema 升级，因为 P5 sidecar 从 `promptEnhancement` 形状迁移到 `runtimeSemantics`。
- subject 输入只接受一张单主体图片并一对一绑定唯一 `identity_subject`；内容输入绑定 `content_element`。
- 每个槽固定提供三条同轴建议，正式校验交叉检查输入、目标和绑定。
- T1 将 `promptTemplate + runtimeSemantics` 编译为逐 case 实际 Prompt，冻结后交给真实生图 seam。

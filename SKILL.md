---
name: produce-meme-template
description: 从来源网图生产可交付的 Meme 模板 JSON。用于先按指定或自主策略完成换图，再分析确认模板图、设计高价值槽位和 Prompt Template、编译隐藏约束、校验正式合同、上传 OSS 并回填 cover/referenceImage；也用于批量独立生产，以及用户明确指定现成 JSON 后单独执行模板生图测试。
---

# Meme 模板生产

## 当前状态

本仓库处于 `0.1.0` 规格阶段。生产脚本、合同和 fixtures 完成前，只使用本 Skill 进行设计与实现，不宣称已经具备 P0–P8 正式生产能力。

## 开始工作

1. 读取 [CONTEXT.md](CONTEXT.md)，使用其中的领域词汇。
2. 读取 [实施规格](specs/新模板生产Skill实施规格.md) 和与当前改动相关的 [ADR](docs/adr/)。
3. 涉及来源网图替换、生图 Prompt 或模板图确认时，完整读取 [第一阶段替换规范](references/replacement-spec.md)。
4. 将旧 Unified 和上一轮拆分版视为迁移证据；新规则只写入本仓库的唯一所有者。

## 调用边界

- **正式生产**：从来源网图进入 P0–P8，以完成 OSS URL 回填并通过正式投影的 `gallery-template.json` 结束。
- **批量提交**：把多张来源网图拆成相互独立的 Production Item；仅在用户显式提供共享批次策略时建立跨图约束。
- **T1 测试**：只在用户明确指定现成正式 JSON 时执行，使用独立状态与产物，不改变 P0–P8 或正式 JSON。

## 事实源

- 来源网图只决定换图机制、组件关系和来源风险。
- Approved Template Image 决定最终标题、描述、槽位默认值、Prompt Template 和视觉约束。
- 正式合同决定允许写入 `gallery-template.json` 的字段与类型。

## 完成条件

实现阶段的每项变更必须同时具备外部行为测试、对应历史经验 ID、机器规则唯一落点和版本影响说明。完整生产只有在 P0–P8 全部完成、四层验证通过、`cover === referenceImage` 且正式 JSON 不含生产 sidecar 字段时才算完成。

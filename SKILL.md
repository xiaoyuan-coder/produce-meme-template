---
name: produce-meme-template
description: 从来源网图生产可交付的 Meme 模板 JSON。用于先按指定或自主策略完成换图，再分析确认模板图、设计高价值槽位和 Prompt Template、编译隐藏约束、校验正式合同、上传 OSS 并回填 cover/referenceImage；也用于批量独立生产，以及用户明确指定现成 JSON 后单独执行模板生图测试。
---

# Meme 模板生产

## 当前状态

`0.5.0` 已提供 Issue #2–#5 的单图纵向切片：普通真人、知名 IP、动物、物体、文字或场景来源图可通过同一公共工作流完成 P0–P8；P2 执行完整视觉门禁与不可变重做，P3–P4 从确认模板图编译 2–5 个高价值槽位、可审计主体/单槽例外、简洁默认值、独立推荐池和统一 resolved Prompt。当前仓库内置确定性生成、视觉证据、独立语义审计和 OSS fixture adapter；真实服务 adapter 仍需由调用环境注入。

## 开始工作

1. 读取 [CONTEXT.md](CONTEXT.md)，使用其中的领域词汇。
2. 读取 [实施规格](specs/新模板生产Skill实施规格.md) 和与当前改动相关的 [ADR](docs/adr/)。
3. 涉及来源网图替换、生图 Prompt 或模板图确认时，完整读取 [第一阶段替换规范](references/replacement-spec.md)。
4. 将旧 Unified 和上一轮拆分版视为迁移证据；新规则只写入本仓库的唯一所有者。

## 公共入口

- Python seam：`scripts.produce_meme_template.run_production(request, output_root, adapters)`。
- 确定性演示：`python3 scripts/produce.py --request <request.json> --deterministic-fixture <fixture-dir> --output <output-dir>`。
- 一次请求只包含一个 `templateKey` 和一张 `sourceImage`；每个 Production Item 独立保存 manifest、pin、不可变 revision、产物摘要和依赖。

## 调用边界

- **正式生产**：从来源网图进入 P0–P8，以完成 OSS URL 回填并通过正式投影的 `gallery-template.json` 结束。
- **批量提交**：把多张来源网图拆成相互独立的 Production Item；仅在用户显式提供共享批次策略时建立跨图约束。
- **T1 测试**：只在用户明确指定现成正式 JSON 时执行，使用独立状态与产物，不改变 P0–P8 或正式 JSON。

## 规则路由

- P0–P2 的自主替换、依赖闭包和生图包读取 [第一阶段替换规范](references/replacement-spec.md)。
- P2 的视觉硬门禁、证据绑定、自主确认和不可变重做读取 [模板图确认与恢复合同](references/template-image-gate.md)。
- P0–P8 状态、谱系、适配器与恢复边界读取 [纵向切片运行合同](references/vertical-slice-runtime.md)。
- P3–P8 的槽位、Prompt Template、隐藏约束和正式投影读取 [正式模板编译合同](references/gallery-template-compiler.md)。
- 机器枚举、阶段、硬门禁和字段白名单只读取 `contracts/machine-rules.json`；正式结构读取 `contracts/gallery-template.schema.json`。

## 事实源

- 来源网图只决定换图机制、组件关系和来源风险。
- Approved Template Image 决定最终标题、描述、槽位默认值、Prompt Template 和视觉约束。
- 正式合同决定允许写入 `gallery-template.json` 的字段与类型。

## 完成条件

实现阶段的每项变更必须同时具备外部行为测试、对应历史经验 ID、机器规则唯一落点和版本影响说明。完整生产只有在 P0–P8 全部完成、四层验证通过、上传凭证绑定当前 Approved Template Image、`cover === referenceImage` 且正式 JSON 不含生产 sidecar 字段时才算完成。

# 单图纵向切片运行合同

## 1. 公共工作流

Issue #2 只通过 `run_production(request, output_root, adapters)` 暴露正式生产接缝。请求包含一个 `templateKey` 和一个 `sourceImage`，输出属于一个独立 Production Item。来源/模板分析、生成、视觉证据、独立语义审计和 OSS 由注入式 adapter 提供；阶段推进、门禁、状态、谱系、正式投影和外部副作用授权由工作流核心控制。

机器阶段、外部结果、错误码、类别和视觉维度统一读取 `contracts/machine-rules.json`。代码、测试和 fixture 不再维护第二份枚举。

## 2. P0–P8 产物

| 阶段 | 状态 | 主要产物 |
| --- | --- | --- |
| P0 | `INGESTED` | source evidence、`source-analysis.json`、`production-pin.json` |
| P1 | `REPLACEMENT_PLANNED` | `replacement-plan.json` |
| P2 | `TEMPLATE_IMAGE_APPROVED` | `generation-package.json`、Approved Template Image、`visual-review.json` |
| P3 | `TEMPLATE_ANALYZED` | `template-analysis.json` |
| P4 | `EDITABLE_SPEC_COMPILED` | `editable-template-spec.json` |
| P5 | `TEMPLATE_COMPILED` | `hidden-template-spec.json`、`gallery-template.draft.json` |
| P6 | `STATIC_VALIDATED` | `semantic-audit.json`、`validation-report.json` |
| P7 | `ASSET_UPLOADED` | `asset-receipt.json` |
| P8 | `FINALIZED` | `final-validation-report.json`、`gallery-template.json` |

`production-manifest.json` 记录每个阶段、产物 SHA、依赖、revision 和外部结果。除 manifest 外，revision 产物使用原子排他创建；相同 Production Item 内容不一致时返回稳定的不可变冲突。请求标识符在落盘前通过格式检查，解析后的 Production Item 真实路径还必须是 `output_root` 的直接子目录；预置符号链接不能把写入引向根外。相同来源图和 key 已完成后再次调用，需要先验证请求身份、pin 和全部产物摘要，再复用最终产物。

## 3. 适配器门禁

- 来源分析证据必须绑定 Source Web Image SHA。
- 视觉审核与模板分析必须分别绑定当前生成图 SHA。
- 语义审计必须绑定标题、Prompt、隐藏层、自由内容和全部槽位值的规范摘要，并通过机器规则声明的完整审计项。
- 生成结果先以 `generated-candidate-image` 保存；只有六维视觉合同和全部硬门禁通过后，才能登记为 Approved Template Image。
- 硬失败停止在 P2，人工意见不能绕过门禁。
- P7 adapter 只能接收当前 Approved Template Image；Asset Receipt 的图片 SHA 必须一致，URL 必须为 HTTPS。
- P7 恢复先验证已有产物谱系和 Asset Receipt，再直接执行 P8 正式投影；图片 SHA、对象键或 URL 不一致时阻断，恢复路径不调用生成或上传 adapter。
- 完整生产调用授权门禁后的 P7 上传，不授权数据库导入、管理台写入、发布、Tag 或生产上线。

## 4. 版本 pin

每个 Production Item 单独保存 Skill 行为版本、Artifact Schema 版本、完整 tracked-file release digest、机器规则 SHA、Gallery Contract 快照 SHA 和上游来源 SHA。运行中的 pin 不读取未来安装版本。Issue #2 只创建 revision 1；后续 ticket 将扩展跨 revision 的恢复与精确失效。

## 5. 迁移证据

确定性 tracer fixture 位于 `fixtures/e2e/simple-animal/`。测试方法按 E01、E04、E05、E07、E10、E11、E19、E21、E27、E35、E36 和 E38 命名或分组，保证 Issue #2 指定经验都有规则与外部验收落点。

---
name: produce-meme-template
description: 从来源网图生产可交付的 Meme 模板 JSON。用于先按指定或自主策略完成换图，再分析确认模板图、设计高价值槽位和 Prompt Template、编译隐藏约束、校验正式合同、上传 OSS 并回填 cover/referenceImage；也用于批量独立生产，以及用户明确指定现成 JSON 后单独执行模板生图测试。
---

# Meme 模板生产

## 当前状态

当前已提供 Issue #2–#16 的纵向切片：普通真人、公众人物与知名 IP 使用具名身份路由和完整依赖闭包；文字密集模板先清点并分类全部可见文字；多实例、镜像、容器、分格和接触关系通过组件图与五类图片操作进入同一 Replacement Plan。批量信封默认拆成独立 Production Item；显式共享策略使用稳定分配、逐字段来源和单项分辨 sidecar 进入同一单项生命周期。发布包、安装副本、doctor 和 production pin 通过三条独立版本线与完整文件摘要对账，E01–E39 追踪矩阵在发布前编译成绑定同一 pin 的回归报告。P2 在供应商提交前冻结生成任务和 WAL，提交后立即绑定 request ID，再执行全部硬门禁。P3–P6 编译高价值槽位、精确文字、中性默认内容和统一 resolved Prompt；P7 使用 Approved Template Image SHA 确定对象键并保存完整 Asset Receipt，P8 依据冻结 Gallery Contract 执行严格白名单投影与同一模板图 URL 双字段回填。用户明确指定现成正式 JSON 时，T1 以独立 task、WAL、图片和报告验证槽位编辑与全文编辑。

## 开始工作

1. 读取 [CONTEXT.md](CONTEXT.md)，使用其中的领域词汇。
2. 读取 [实施规格](specs/新模板生产Skill实施规格.md) 和与当前改动相关的 [ADR](docs/adr/)。
3. 涉及来源网图替换、生图 Prompt 或模板图确认时，完整读取 [第一阶段替换规范](references/replacement-spec.md)。
4. 将旧 Unified 和上一轮拆分版视为迁移证据；新规则只写入本仓库的唯一所有者。

## 公共入口

- Python seam：`scripts.produce_meme_template.run_production(request, output_root, adapters)`。
- 确定性演示：`python3 scripts/produce.py --request <request.json> --deterministic-fixture <fixture-dir> --output <output-dir>`。
- 发布、安装、诊断与显式 pin 迁移：`python3 scripts/release_tool.py <build|install|doctor|migrate-pin> ...`。
- 历史经验回归：`python3 scripts/experience_regression.py --runtime <runtime> --output <outside-runtime.json>`。
- 影子批次与 1.0 准备：`scripts.produce_meme_template.run_release_readiness(request, output_root, adapters)`。
- 单项请求包含一个 `templateKey` 和一张 `sourceImage`；批量请求使用机器合同声明的信封字段包含多个同形单项请求。每个 Production Item 独立保存 manifest、pin、不可变 revision、产物摘要和依赖。

## 调用边界

- **正式生产**：从来源网图进入 P0–P8，以完成 OSS URL 回填并通过正式投影的 `gallery-template.json` 结束。
- **批量提交**：把多张来源网图拆成相互独立的 Production Item；仅在用户显式提供共享批次策略时建立跨图约束。
- **T1 测试**：只在用户明确指定现成正式 JSON 时执行，使用独立状态与产物，不改变 P0–P8 或正式 JSON。

## 规则路由

- P0–P2 的自主替换、依赖闭包和生图包读取 [第一阶段替换规范](references/replacement-spec.md)。
- 批量信封的默认隔离、显式共享策略、稳定分配、分辨 sidecar 和逐项恢复读取 [批量隔离与共享策略合同](references/shared-batch-policy.md)。
- P0–P3 遇到重复实例、镜像、阴影、容器、分格或接触遮挡时读取 [多实例组件图与图片操作合同](references/multi-instance-image-operations.md)。
- P2 的视觉硬门禁、证据绑定、自主确认和不可变重做读取 [模板图确认与恢复合同](references/template-image-gate.md)。
- P2 的生成数量、冻结任务、request ID WAL、失败分类和队列恢复读取 [生成执行与 WAL 恢复合同](references/generation-execution-and-recovery.md)。
- P7–P8 的 Approved Image 上传、远端对象对账、Asset Receipt 恢复和双 URL 回填读取 [OSS 幂等终结合同](references/oss-finalization.md)。
- P0–P8 状态、谱系、适配器与恢复边界读取 [纵向切片运行合同](references/vertical-slice-runtime.md)。
- 三线版本、不可变发布包、安装验证、doctor 和显式 pin 迁移读取 [Release、安装、doctor 与版本 pin 合同](references/release-doctor-install.md)。
- T1 的现成 JSON 门禁、编辑归一、真实生成恢复和偏差报告读取 [T1 独立模板 JSON 生图测试合同](references/template-json-test.md)。
- E01–E39 的唯一落点、代表 corpus、失败分类和发布门禁读取 [历史经验回归门禁](references/historical-experience-regression.md)。
- 六类真实影子样本、未见图前向测试、发布 readiness 报告和 `1.0.0` 冻结读取 [真实影子批次与 1.0 发布准备合同](references/release-readiness.md)。
- P3–P8 的槽位、Prompt Template、隐藏约束和正式投影读取 [正式模板编译合同](references/gallery-template-compiler.md)。
- P3–P6 遇到可见文字、文字密集海报或文字槽时读取 [可见文字区域与文字槽合同](references/visible-text-contract.md)。
- 机器枚举、阶段、硬门禁和字段白名单只读取 `contracts/machine-rules.json`；正式结构读取 `contracts/gallery-template.schema.json`。

## 事实源

- 来源网图只决定换图机制、组件关系和来源风险。
- Approved Template Image 决定最终标题、描述、槽位默认值、Prompt Template 和视觉约束。
- 正式合同决定允许写入 `gallery-template.json` 的字段与类型。

## 完成条件

实现阶段的每项变更必须同时具备外部行为测试、对应历史经验 ID、机器规则唯一落点和版本影响说明。发布前的 E01–E39 回归报告必须绑定当前 production pin、清单、机器规则、追踪矩阵和全部代表 corpus，并且结果为 PASS。完整生产只有在 P0–P8 全部完成、四层验证通过、上传凭证绑定当前 Approved Template Image、`cover === referenceImage` 且正式 JSON 不含生产 sidecar 字段时才算完成。`1.0.0` 另外要求 readiness 报告的真实外部执行完成且 `releaseEligible=true`。

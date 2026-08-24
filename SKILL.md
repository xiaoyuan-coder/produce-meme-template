---
name: produce-meme-template
description: 从来源网图分阶段或端到端生产可交付的 Meme 模板 JSON。用于输出换图执行 JSON、调用 Fal API 生成并确认模板图、编译待 OSS 的模板数据包、上传 OSS 并回填最终模板数据；也用于批量独立生产，以及用户明确指定现成 JSON 后单独执行模板生图测试。
---

# Meme 模板生产

## 开始工作

1. 读取 [CONTEXT.md](CONTEXT.md)，使用其中的领域词汇。
2. 读取 [实施规格](specs/新模板生产Skill实施规格.md) 和与当前改动相关的 [ADR](docs/adr/)。
3. 涉及来源网图替换、生图 Prompt 或模板图确认时，完整读取 [第一阶段替换规范](references/replacement-spec.md)。
4. 将旧 Unified 和上一轮拆分版视为迁移证据；新规则只写入本仓库的唯一所有者。

## 公共入口

- Python seam：`scripts.produce_meme_template.run_production(request, output_root, adapters, stage=<1|2|3|4>, execution_mode=<recorded_replay|live_external>)`；省略 `stage` 时执行完整第四阶段，省略执行模式时只获得回放资格。
- 正式 adapter 工厂：`scripts.produce_meme_template.build_live_production_adapters(...)`；按职责注入来源、视觉审核、作者分析、作者审计、语义审计和视觉合同审计 adapter，并由核心登记 Fal/OSS 拓扑。
- 确定性演示：`python3 scripts/produce.py --request <request.json> --deterministic-fixture <fixture-dir> --output <output-dir> --stage <1|2|3|4>`。
- 发布候选、readiness 晋升、安装、诊断与显式 pin 迁移：`python3 scripts/release_tool.py <build|stage|promote|install|doctor|migrate-pin> ...`。
- 历史经验回归：`python3 scripts/experience_regression.py --runtime <runtime> --output <outside-runtime.json>`。
- 正式记录拆分交付：`python3 scripts/export_gallery_templates.py --source <gallery-template.json> --production-manifest <production-manifest.json> --output-dir <单模板JSON目录> --manifest <目录外交付清单.json>`；单工作区来源可自动发现 Manifest，批量数组逐条重复传入，输出目录内只保留按 `key` 命名的正式 JSON。
- 影子批次与 1.0 准备：`scripts.produce_meme_template.run_release_readiness(request, output_root, adapters)`。
- 单项请求包含一个 `templateKey` 和一张 `sourceImage`；批量请求使用机器合同声明的信封字段包含多个同形单项请求。每个 Production Item 独立保存 manifest、pin、不可变 revision、产物摘要和依赖。

## 调用边界

- **第一阶段**：执行 P0–P1，输出 `replacement-package.json`；其中绑定来源分析、替换计划、生图包与 `authoring-intent.json`。本阶段必须完成 IP/文化身份发现、主体连续性和玩法机制冻结，不提交生图 API。
- **第二阶段**：执行 P2，必须通过生成 adapter 调用图片 API。真实生产使用 `FalQueueWorkflowAdapters` 提交、轮询并下载候选图；候选图通过视觉门禁后输出 Approved Template Image 和绑定当前图片摘要的 `authoring-handoff.json`。
- **第三阶段**：执行 P3–P6，P3 使用 Approved Image 与只读 Authoring Handoff 做增量分析，输出状态为 `awaiting_oss_finalization` 的 `template-data-package.json`；其中绑定正式 draft、runtimeSemantics、语义审计和四层验证。
- **第四阶段**：执行 P7–P8，上传当前 Approved Template Image，回填同一 OSS URL，输出最终 `gallery-template.json`。
- **完整生产**：省略阶段参数或指定第四阶段，依次执行四个大阶段。四次分段调用和一次完整调用使用同一个 Production Item、revision、pin 与产物谱系。
- **批量提交**：把多张来源网图拆成相互独立的 Production Item；并发、阶段屏障与显式共享策略读取批量合同。
- **T1 测试**：只在用户明确指定现成正式 JSON 时执行，使用独立状态与产物，不改变 P0–P8 或正式 JSON。
- **正式数据归档**：用户指定长期数据目录或要求一条模板一个文件时，在 P8 后显式执行拆分导出；生产 sidecar、交付清单和 OSS 幂等规则读取正式编译合同。
- **正式执行资格**：正式/回放模式、安装来源与交付资格读取正式执行画像合同。

## 规则路由

- P0–P2 的自主替换、依赖闭包和生图包读取 [第一阶段替换规范](references/replacement-spec.md)。
- 批量信封的默认隔离、显式共享策略、稳定分配、分辨 sidecar 和逐项恢复读取 [批量隔离与共享策略合同](references/shared-batch-policy.md)。
- P0–P3 遇到重复实例、镜像、阴影、容器、分格或接触遮挡时读取 [多实例组件图与图片操作合同](references/multi-instance-image-operations.md)。
- P2 的视觉硬门禁、证据绑定、自主确认和不可变重做读取 [模板图确认与恢复合同](references/template-image-gate.md)。
- P2 的生成数量、冻结任务、request ID WAL、失败分类和队列恢复读取 [生成执行与 WAL 恢复合同](references/generation-execution-and-recovery.md)。
- P7–P8 的 Approved Image 上传、远端对象对账、Asset Receipt 恢复和双 URL 回填读取 [OSS 幂等终结合同](references/oss-finalization.md)。
- 在杨媛个人工作区执行正式业务生产或拆分交付时读取 [个人工作区落盘与交付合同](references/personal-workspace-delivery.md)。
- 四个用户大阶段、P0–P8 状态、谱系、适配器与恢复边界读取 [纵向切片运行合同](references/vertical-slice-runtime.md)。
- 三线版本、发布风险分级、不可变发布包、安装验证、doctor 和显式 pin 迁移读取 [Release、安装、doctor 与版本 pin 合同](references/release-doctor-install.md)。
- 正式/回放模式、adapter 拓扑、执行画像、阶段 provider 对账、交付资格和零重复外部调用读取 [正式执行画像与交付资格合同](references/production-execution-authority.md)。
- T1 的现成 JSON 门禁、编辑归一、真实生成恢复和偏差报告读取 [T1 独立模板 JSON 生图测试合同](references/template-json-test.md)。
- E01–E44 的唯一落点、代表 corpus、失败分类和发布门禁读取 [历史经验回归门禁](references/historical-experience-regression.md)。
- 首稳、major 或显式外部风险候选的六类影子样本、未见图前向、四场景 live 和 readiness 报告读取 [真实影子批次与稳定版发布准备合同](references/release-readiness.md)。
- 创建、迁移、审查或独立测试 Gallery Template JSON，以及 P3–P8 的槽位、Prompt Template、runtimeSemantics 和正式投影，完整读取 [正式模板编译合同](references/gallery-template-compiler.md)。
- P3–P6 判断模板身份、编辑权限、subject 身份特征继承范围，或编写标题、槽位全部属性、Prompt Template、target/binding 与逐图画风约束时，完整读取 [模板身份、编辑权限与正式字段编写规范](references/template-authoring.md)。
- P3–P6 遇到可见文字、文字密集海报或文字槽时读取 [可见文字区域与文字槽合同](references/visible-text-contract.md)。
- 机器枚举、阶段、硬门禁和字段白名单只读取 `contracts/machine-rules.json`；正式结构读取 `contracts/upstream/gallery-template/runtime-semantics-v2-contract/gallery-template.schema.json`。

## 事实源

- 来源网图只决定换图机制、组件关系和来源风险。
- `authoring-intent.json` 传递 P1 已冻结的玩法、IP/文化身份、主体连续性和组件结构；`authoring-handoff.json` 将它们与 P2 确认结果绑定。
- Approved Template Image 决定最终标题、描述、槽位默认值、Prompt Template 和视觉约束。
- 正式合同决定允许写入 `gallery-template.json` 的字段与类型。

## 完成条件

实现阶段的每项变更必须同时具备外部行为测试、对应历史经验 ID、机器规则唯一落点和版本影响说明。发布前的 E01–E44 回归报告必须绑定当前 production pin、清单、机器规则、追踪矩阵和全部代表 corpus，并且结果为 PASS。完整生产只有在 P0–P8 全部完成、执行画像具备交付资格、四层验证通过、上传凭证绑定当前 Approved Template Image、`cover === referenceImage` 且正式 JSON 不含生产 sidecar 字段时才算完成。发布按候选锁中的 profile 验收；首稳、major 和显式外部风险要求真实外部 readiness，兼容 minor/patch 要求全量验证、双轴 clean review、全新安装和 doctor。

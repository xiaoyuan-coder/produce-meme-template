# T1 独立模板 JSON 生图测试合同

## 唯一含义

T1 只验证一份用户明确指定的现成正式 `gallery-template.json`。输入是一份正式 JSON、一个编辑用例和可选的真实用户上传图；输出是一份供 Codex 内置生图工具执行的 `codex-imagegen-request.json` 以及一张测试结果图。T1 不进入 P0–P8，不改变正式 JSON，也不调用 Fal、OSS 或发布门禁。

旧的供应商队列、WAL、失败路由与确定性 fixture 回放保留为 `run_recorded_template_test`，只服务发布验证。它不再使用 T1 的公共名称。

## 固定流程

1. 运行 `prepare_template_test` 或 `python3 scripts/produce.py t1 ...`，读取正式 JSON，校验 Gallery Schema、`inputSchema`、`promptTemplate` 与 `runtimeSemantics`。兼容读取明确接受 runtimeSemantics v1/v2；该兼容性不授权新生产继续写 v1。
2. 槽位编辑把真实输入代入 Prompt Template；图片输入必须绑定一个带 `image` 合同的槽，并复制为不可变测试资产。全文编辑直接使用用户新 Prompt。
3. 编译后的 Prompt 必须同时包含用户字面、目标实例、输入绑定、媒介、画风、构图、关系和条件性色光。多张参考图逐张标明职责：模板图提供视觉结构，用户图只提供绑定槽的内容或身份特征。
4. 执行包的 `backend` 固定为 `codex_builtin_imagegen`，`imageCount=1`，并明确禁止 `fal_generation`、`oss_upload` 和 `production_state_mutation`。
5. Agent 读取执行包后调用 Codex 内置生图工具一次，并按执行包中的四项 checklist 对照模板参考图与用户上传图进行视觉验收。
6. 只有出现清楚、局部、可描述的偏差时允许一次纠正生成；纠正 Prompt 只补充失败项，同时重复全部关键保持约束。第二次结果仍失败时报告偏差，停止继续消耗生成次数。

## 完成条件

- 用户上传图真实进入 reference image 列表并绑定正确 slot；
- 实际送入内置生图工具的 Prompt 与执行包 `prompt` 字面一致；
- 结果图体现用户输入，且媒介、画风、构图、关系与模板参考图一致；
- 一次初始生成，最多一次纠正；
- 整个 T1 输出目录中没有 Production Manifest、Fal request ID、OSS receipt 或正式 JSON 改写。

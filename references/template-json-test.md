# T1 独立模板 JSON 生图测试合同

## 1. 入口与边界

T1 只由用户通过 `scripts/produce.py t1` 或 `run_template_test` 明确唤起。请求必须指定一份已落盘且通过当前 Gallery v2 Schema、`runtimeSemantics` 跨字段关系与 `cover === referenceImage` 门禁的正式 `gallery-template.json`、正整数 `templateRevision`、独立 invocation ID 和测试用例。T1 输出根目录与正式 JSON 所在的 Production workspace 必须完全分离。P0–P8 不调用 T1；T1 不读取来源网图、不上传 OSS，也不写 Production Manifest 或正式 JSON。

## 2. 冻结测试事实

首次执行 create-once 保存规范化请求、当前 runtime production pin、正式 JSON SHA、模板 key、revision、同一模板图 URL 和模板图字节 SHA。每个 case 独立保存 generation task、request-ID WAL、单张 candidate 与视觉复核证据。恢复时必须逐项重放请求、pin、模板、参考图、task、WAL、candidate 和最终报告摘要；任一同步失败均进入 T1 自己的稳定完整性错误。

## 3. 两种用户编辑

- `slot_edit` 只接受正式 `inputSchema` 中的槽 ID。`prompt` 直接代入；`subject` 的文字模式代入 `text` 语义值，图片模式由运行时按 `inputBindings` 接管唯一身份目标。v2 正式合同不接受旧版顶层数组形态的纯 `image` 槽或 select 扩展字段。未显式编辑的占位符继续使用 Prompt Template 的字面兜底；v2 模板全部槽位均为纯图片模式时，T1 接受空 `slotValues` 并使用这些字面兜底准备结构化用例，真实图片稳定性仍由发布就绪的独立图片试验覆盖。
- `free_edit` 直接使用用户给出的完整 Prompt 文本。

两种模式先得到用户基础 Prompt，再与 `runtimeSemantics.targetInstances`、`inputBindings` 和 `visualContract` 一次性编译为实际 Prompt。逐 case 报告的 `resolvedPrompt`、generation request 和真实 Fal 请求中的 prompt 必须逐字一致。测试任务同时冻结整份模板 JSON 摘要；结构化编辑和全文编辑的用户内容权限继续最高，`visualContract` 不得恢复旧主体、文字、颜色、配饰、服装、道具或场景。

## 4. 真实生成与恢复

每个 case 默认 `imageCount=1`、主输出索引为 0，并复用 `generationExecutionContract` 的 provider/model/request/output ID、输出格式、图片解码、轮询预算和 failure class。provider request ID 写入 WAL 后才轮询；进程退出或 retryable poll 后，重跑只轮询同一 request ID。终端生成失败冻结失败报告；重试需要新的 invocation ID。

## 5. 报告与偏差

`template-json-test-report.json` 绑定模板 JSON 路径与 SHA、template revision、模板图 URL、tester Skill 版本、完整 production pin、规范化请求 SHA 和逐 case 结果。每个 case 保存用户输入、resolved prompt、生成请求、输出图片路径与 SHA、可见偏差、视觉结论和稳定错误。视觉偏差表示模板修订建议，不改变 T1 执行完成，也不修改旧模板 revision。

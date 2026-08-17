# T1 独立模板 JSON 生图测试合同

## 1. 入口与边界

T1 只由用户通过 `scripts/produce.py t1` 或 `run_template_test` 明确唤起。请求必须指定一份已落盘且通过当前 Gallery Schema 与 `cover === referenceImage` 门禁的正式 `gallery-template.json`、正整数 `templateRevision`、独立 invocation ID 和测试用例。T1 输出根目录与正式 JSON 所在的 Production workspace 必须完全分离。P0–P8 不调用 T1；T1 不读取来源网图、不上传 OSS，也不写 Production Manifest 或正式 JSON。

## 2. 冻结测试事实

首次执行 create-once 保存规范化请求、当前 runtime production pin、正式 JSON SHA、模板 key、revision、同一模板图 URL 和模板图字节 SHA。每个 case 独立保存 generation task、request-ID WAL、单张 candidate 与视觉复核证据。恢复时必须逐项重放请求、pin、模板、参考图、task、WAL、candidate 和最终报告摘要；任一同步失败均进入 T1 自己的稳定完整性错误。

## 3. 两种用户编辑

- `slot_edit` 只接受正式 `inputSchema` 中的槽 ID。普通文字和主体的文字模式直接代入；select 先按 option value 选择，再允许解析该 option 的 payload 字段。纯 `image` 槽需要二进制测试素材，当前的字符串 `slotValues` 请求会在生成前稳定拒绝，不会把资源 ID 当成 Prompt 文字。未显式编辑的占位符继续使用 Prompt Template 的字面兜底。
- `free_edit` 直接使用用户给出的完整 Prompt 文本。

两种模式都输出同一个 `resolvedPrompt`，并要求冻结 generation request 中的 prompt 与之逐字一致。结构化编辑和全文编辑不得由隐藏字段恢复旧主体、文字、颜色、配饰、服装、道具或场景。

## 4. 真实生成与恢复

每个 case 默认 `imageCount=1`、主输出索引为 0，并复用 `generationExecutionContract` 的 provider/model/request/output ID、输出格式、图片解码、轮询预算和 failure class。provider request ID 写入 WAL 后才轮询；进程退出或 retryable poll 后，重跑只轮询同一 request ID。终端生成失败冻结失败报告；重试需要新的 invocation ID。

## 5. 报告与偏差

`template-json-test-report.json` 绑定模板 JSON 路径与 SHA、template revision、模板图 URL、tester Skill 版本、完整 production pin、规范化请求 SHA 和逐 case 结果。每个 case 保存用户输入、resolved prompt、生成请求、输出图片路径与 SHA、可见偏差、视觉结论和稳定错误。视觉偏差表示模板修订建议，不改变 T1 执行完成，也不修改旧模板 revision。

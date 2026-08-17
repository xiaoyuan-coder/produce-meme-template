# OSS 幂等终结与正式 URL 回填合同

## 1. 上传事实源

P7 只接受当前 revision 中唯一的 Approved Template Image。工作流同时对账其对应 Generated Candidate Image；两者必须存在、摘要一致且都属于当前 Production Item 谱系。Source Web Image、普通候选图、视觉失败图和旧 revision 图都不能成为上传输入。

对象键由机器合同的固定前缀、Schema 合法的模板 key、Approved Image SHA 和生图合同的冻结图片扩展名确定性组成。工作流在调用 adapter 前验证前缀安全性、扩展名枚举和路径边界，adapter 无权改写对象键或图片 SHA。

## 2. 远端对象与幂等恢复

上传 adapter 返回的对象必须包含机器合同声明的完整字段：provider、对象键、远端对象身份、图片 SHA、公开 HTTPS URL、幂等键、请求状态和 provider request ID。真实 OSS 公网基址在 adapter 创建时解析 DNS，全部解析结果都必须是公网地址。未知字段、缺字段、旧 `coverUrl`、非公网 HTTPS URL或任一身份不一致都作为外部合同失败，不创建 Asset Receipt。

Aliyun OSS adapter 使用图片 SHA 自定义 metadata，并对首次 PUT 设置禁止覆盖条件。对象键已存在时先读取 metadata、对象长度和 ETag：三者与当前 Approved Image 一致才返回 `reused`；任一不一致都停止且不覆盖远端对象。进程在远端 PUT 成功后、Asset Receipt 落盘前退出时，重跑通过同一对象键与 metadata 对账恢复，不产生第二个对象。

## 3. Asset Receipt

`asset-receipt.json` 是生产 sidecar，不进入正式模板 JSON。它精确绑定：

- Production Item ID、template key 和正式 revision。
- 当前 candidate/approved artifact 路径及两者 SHA。
- provider、对象键、远端对象身份、URL 和幂等键。
- `uploaded | reused` 请求状态与 provider request ID。

已有 receipt 只能在字段集合、schema version、revision、候选/确认图摘要、对象键和全部远端身份重新对账通过时复用。P7 已落盘而 P8 中断时，恢复直接复用 receipt，不再次调用上传 adapter。

## 4. 正式投影与最终验证

P8 只从已验证 receipt 读取同一个 URL，并同时写入 `cover` 与 `referenceImage`。正式记录拒绝 `coverUrl` 和任何上传响应 sidecar 字段。URL 回填后重新运行冻结 Gallery Schema、顶层/metadata 白名单、禁止字段、禁止值、正式术语、状态和双 URL 一致性检查；全部通过后才以 create-once 方式落盘 `gallery-template.json`。

正式生产完成只授权 OSS 对象写入和本地 JSON 终结，不包含数据库导入、管理台写入、发布、Tag 或上线。

## 5. 验收

确定性 OSS fixture 验证 receipt 复用、危险结果拒绝和双字段回填。`AliyunOssWorkflowAdapters` 通过受控 bucket transport 验证真实 SDK 的 object-exists、metadata、conditional PUT 和 request evidence 语义；默认测试不访问真实 bucket。连接真实测试 bucket 需要用户显式提供凭据并承担外部写入。

# 正式执行画像与交付资格合同

## 1. 两种执行模式

`contracts/machine-rules.json` 的 `productionExecutionContract` 是执行模式、画像字段、正式 adapter 拓扑、审核方法集合和交付资格的唯一机器事实源。

- `recorded_replay`：用于 fixture、录制证据、单元测试和发布回放。允许现有注入式 adapter，画像固定为 `deliveryEligible: false`。
- `live_readiness`：仅用于发布资格的 Fal/OSS 风险采样。它保留真实 provider 凭证，画像固定为 `deliveryEligible: false`，且拆分导出始终拒绝。
- `live_external`：用于正式业务生产。调用方通过 `build_live_production_adapters` 按职责提供六个相互独立的 adapter；工厂检查角色对象及其可见 delegate 链的根对象，禁止共用 fixture 或共用代理根。核心登记 Aliyun OSS → Fal → 独立审核 delegate 拓扑，并要求 doctor 的 `installSource` 来自已验证发布包。直接实例化相同外观的 Fal/OSS 包装器不会进入核心登记表。

未显式指定模式时兼容为 `recorded_replay`，因此旧脚本无法因省略参数获得交付资格。执行画像写入 `production-execution-profile.json`，其摘要和执行模式写入 Production Manifest。

## 2. fail-fast 门禁

正式模式预检在创建 Production Item 目录、来源分析、Fal submit 和 OSS upload 之前完成。下列任一情况返回 `UNTRUSTED_PRODUCTION_EXECUTION`：

- 使用 deterministic fixture 或自定义直连 adapter 冒充正式拓扑；
- 缺少 Fal 或 Aliyun OSS 层；
- 模板 key 注册表与语义审核方法不在批准集合；
- 视觉审核方法不在批准集合；
- 作者分析与作者合同审计使用相同方法身份；
- doctor 表明运行目录是 source worktree；
- 恢复时执行模式或画像摘要发生变化。

P2 再核 generation WAL/provider 和视觉审核 method ID；P7/P8 再核 Asset Receipt provider。预检负责节省外部费用，阶段后置复核负责防止 adapter 返回值或持久化证据漂移。

## 3. 交付门禁

`gallery-template.json` 不加入生产字段。`scripts/export_gallery_templates.py` 从同目录自动读取单条 Production Manifest；批量数组使用重复的 `--production-manifest` 显式提供逐条谱系。导出前重放完整 P0–P8 产物谱系、生图执行凭证、当前生产事实、语义与视觉验证报告、Asset Receipt 和最终投影。所有事实与完成态、`live_external`、Fal、Aliyun OSS、已安装 runtime 及正式记录 SHA 全部一致后，才允许写入交付目录。

回放 JSON 可用于 T1、回归和人工检查；导出会稳定阻断。旧 Production Item 没有执行画像时同样阻断，不能由批处理脚本补一个布尔值绕过。pin 迁移仍可读取旧谱系并生成迁移报告，迁移本身不会补写执行画像或授予交付资格；下一次公共生产调用会要求按当前模式重新建立画像。

## 4. 效率与恢复

执行画像是内容寻址身份的一部分。未变化的 item 重放完成态时直接返回，新增 Fal submit 和 OSS upload 都为零。P2、Approved Image 或画像事实变化时只失效对应 item 的后继产物；批量仍按机器合同中的并发上限并行，并在大阶段之间设置屏障。

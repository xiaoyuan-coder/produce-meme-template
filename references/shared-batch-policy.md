# 批量隔离与共享策略合同

## 1. 入口与边界

`run_production(request, output_root, adapters)` 同时接受单个 Production Item 和批量信封。批量信封只负责拆分和归集结果；每个 item 继续运行同一个 P0–P8 生命周期，拥有独立的来源事实、Replacement Plan、Production Pin、Manifest、不可变 revision、输出目录和正式 JSON。

批量信封字段、数量边界、共享策略字段和分辨产物字段统一读取 `contracts/machine-rules.json` 的 `batchProductionContract`。信封本身不生成跨图业务产物，下游仍只读取每个 item 的 `gallery-template.json`。

## 2. 默认隔离

未提供 `sharedPolicy` 时，工作流按输入顺序逐项调用单项 seam。批量不建立跨图候选池、不去重替换值、不共享版本或状态，也不使用其他 item 的视觉事实。单项失败只影响该 item；其他 item 继续生产。恢复、重做、上传和完成态对账也以 item 为边界。

## 3. 显式共享策略

只有请求显式携带 `sharedPolicy` 时，工作流才在其 `scope` 内分配替换值并继承共享的保留项和禁止值。策略必须带稳定 policy identity、version、revision、唯一 scope 和类型化 replacement pool。候选池上限从 `batchProductionContract.maximumReplacementPoolItems` 读取。不在 scope 内的 item 继续使用默认隔离路径。

优先级固定为 `per_image > batch > autonomous`。单图显式替换值先占用候选；其余 item 使用 `batchId + templateKey + sourceIdentity` 构造稳定种子，优先分配当前使用次数最少的兼容值。候选充足时保持唯一；候选耗尽后按最少使用次数稳定复用。输入顺序变化不得改变已决议的 item 分配。

## 4. 分辨与谱系

共享策略在 P0 前分两次使用来源分析。第一次请求在机器字段 `allocationAnalysisPoolField` 中携带整个有界候选池，一次返回来源类别、身份和每个候选的硬过滤证据；核心先过滤出兼容值，再做稳定、尽量唯一的分配。第二次只携带最终有效策略，验证显式替换、保留项冲突和最终候选可用性。两次的来源类别和身份必须一致。

每个 scoped item 在自己的目录中保存 `shared-policy-resolution.json`。该 sidecar 绑定 batch、item、policy version/revision/SHA、Source Image SHA、来源身份与类别、scope、优先级、最终单图策略、逐字段来源、列表值来源、第一次分析的逐候选评估和分配种子。Replacement Plan 依据该分辨结果写入 `per_image` 或 `batch` 决策来源。分辨 sidecar 进入 Replacement Plan 依赖和 Production Item 身份摘要。恢复时使用这份已绑定证据重放兼容过滤与稳定分配，不会将第二次最终策略分析误作批量候选分析。

所有不可变产物记录 Production Item scope digest 和不可变依赖摘要。恢复时同时重放单项事实语义；交换 source analysis、Approved analysis、默认值或视觉事实即使同步 manifest 文件摘要，也会在继续生产前阻断。

## 5. 失败与恢复

信封或共享策略的顶层形状无效时，返回批量级输入错误且不启动 item。已进入某个 item 的输入、adapter、候选或门禁失败都返回该 item 的稳定结果，并保留其他 item 的外部副作用授权。

重跑已有有效分辨 sidecar 时，工作流复用已绑定的最终策略和分配，不重复提交、轮询或上传。策略、scope、来源图或关键身份变化后，旧分辨不可继续作为当前事实。

# 历史经验回归门禁

## 1. 唯一追踪矩阵

`fixtures/regression/historical-experience-matrix.json` 是机器合同所列全部历史经验的追踪矩阵。每条经验只声明一个当前权威规则、一个实现定位、迁移状态、旧口径裁决和至少一条具体 unittest 证据。经验 ID 集合及顺序、字段名、迁移状态、证据极性、corpus 角色、报告字段与失败分类只读取 `contracts/machine-rules.json` 的 `historicalExperienceContract`。

条件化、重写或废止的经验必须写明 `legacyDisposition`。这份裁决与当前实现证据一起验证，旧入口或旧字段不能仅凭历史文档继续生效。E31 的正式隐藏层职责已经由当前冻结 Gallery Contract、编译器和冲突审计共同验收，迁移状态从待冻结收口为确认迁移。

## 2. 可执行证据与代表性 corpus

每条 experience 的 topic、权威规则、实现定位、迁移裁决与全部 evidence 以完整规范化行摘要按 E ID 冻结在机器合同；其中 evidence 精确绑定 unittest ID、Good Case / Bad Case / human-review 极性和所需 fixture 文件。Bad Case 与 human-review 还必须绑定由机器合同冻结的门禁 locator 与当前值；测试方法必须显式引用对应机器角色，runner 回传实际 observed locator/value，核心与期望逐项对账。改权威来源、实现、标签、fixture 或测试 ID 都会作为规则缺失或证据不可用失败。发布门禁先执行完整测试集，再把每个测试的真实 success、failure、error、skip、expected-failure 结果交给回归编译器；skip 与 expected-failure 统一归入证据不可用。缺测试、缺 fixture、执行失败或证据执行器不可用均不能生成 PASS。

固定 corpus 同时包含两份最新正式 JSON 的原始输入和修正 expected，以及普通真人、知名 IP、动物、可见文字和复杂构图五类代表场景。每个角色的允许路径、内容类型、精确 JSON Pointer 与预期语义值由 `requiredCorpusBindings` 固定，矩阵只提供与这些绑定一致的文件摘要；无关 JSON 内偶然出现同名字符串不能冒充代表场景。每个 corpus 文件绑定 SHA-256；expected 正式 JSON 还必须通过当前冻结 Gallery Schema、正式白名单、旧字段和业务语义门禁。

## 3. 报告与失败分类

`scripts/experience_regression.py --runtime <runtime> --output <outside-runtime.json>` 生成原子 create-once 报告。核心重算并写入当前 runtime 的完整 production pin，adapter 自报 pin 只参与漂移对账；执行证据前后都会重放 revision 身份，运行中修改 tracked 文件也会失败。报告同时绑定 `skill-manifest.json`、机器规则、追踪矩阵和全部 corpus 摘要，并逐条列出权威规则、实现定位、证据结果、机器门禁、迁移状态和旧口径裁决。报告目录必须位于运行副本之外，路径各层不得通过调用方创建的符号链接重定向；验证子进程禁用 bytecode 写入，保证被审计 runtime 保持只读。

失败分类固定区分：规则或实现缺失、fixture 缺失、证据执行器不可用、正式合同不兼容、版本漂移和可执行证据失败。矩阵缺失或形状损坏仍会生成结构完整的 FAIL 报告并归入 fixture 缺失；额外、重复或错序经验不会产生负数汇总。无法自动裁决的案例使用与主流程一致的 human-review 证据，不得伪装为通过或普通执行失败。

## 4. 发布门禁

`scripts/release_validation_runner.py` 在完整单元、fixture、Schema 和 E2E 测试通过后编译历史经验报告；机器合同声明的任一历史经验、corpus 角色或版本绑定不完整时返回失败，release build 不会继续。安装副本运行同一报告入口时，production pin 来自该只读安装副本，因此源码 Git revision、发布清单、安装文件、pin 和报告可以逐摘要对账。

回归门禁只读取本仓库当前规则与匿名化 fixture，不修改旧 Unified 或拆分版，也不调用真实图片 API、OSS、数据库、管理台和自动发布入口。

# produce-meme-template 协作规则

本仓库只维护 `produce-meme-template`。它从来源网图生成 Approved Template Image，再编译、校验和交付正式模板 JSON。

## 开始任务

- 先读取根目录 `CONTEXT.md`、适用 ADR 和 `specs/新模板生产Skill实施规格.md`。
- 修改替换、生图或模板图验收规则时读取 `references/replacement-spec.md`。
- 旧 `memebuy-skills`、Unified 和上一轮拆分版只提供迁移证据；保留其文件，不在旧仓库继续实现本 Skill。
- 修改前运行 `git status --short`，保留用户已有变化。

## 工程边界

- 对外保持一个端到端生产工作流 seam；T1 是同一入口下的独立命令。
- 机器枚举、状态、字段白名单和 Schema 各自只有一个定义位置。
- `SKILL.md` 只保留流程与路由，详细规则放入一级 `references/`，确定性行为放入 `scripts/`。
- 正式业务 JSON 与生产 sidecar 分离；下游只读取 `gallery-template.json`。
- 前端、后端 Worker、数据库导入、管理台和自动发布均在仓库职责外。

## 版本与验证

- `release.json` 是 Skill 行为版本的唯一人工事实源。
- 行为、合同、脚本、输出或用户可见规则变化时同步更新版本、manifest 和测试。
- `README.md` 面向人类维护者，由 `scripts/update_readme.py` 从仓库事实源生成；相关事实变化后重新生成并运行 `python3 scripts/update_readme.py --check`。
- 测试优先通过公共工作流观察状态、产物、错误和外部副作用；内部调用顺序不作为行为合同。
- 未经用户明确要求，不执行 commit、Tag、push 或发布。

## Agent skills

### Issue tracker

规格和实现票据使用 GitHub Issues。见 `docs/agents/issue-tracker.md`。

### Triage labels

可由 Agent 实施的规格和票据使用 `ready-for-agent`。见 `docs/agents/triage-labels.md`。

### Domain docs

仓库采用 single-context：根目录 `CONTEXT.md` 与 `docs/adr/`。见 `docs/agents/domain.md`。

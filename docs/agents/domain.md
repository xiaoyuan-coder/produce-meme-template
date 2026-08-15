# Domain docs

本仓库采用 single-context 领域文档布局：

- `CONTEXT.md` 定义整个模板生产生命周期的统一语言。
- `docs/adr/` 记录难以逆转的设计决定。
- `specs/` 保存实施规格、历史经验迁移和合同审计。
- `references/` 保存 Skill 运行时按分支加载的领域规则。

开始任务时先读 `CONTEXT.md`；再读取当前改动涉及的 ADR、spec 和 reference。发生冲突时采用已接受的最新 ADR，其中 ADR 0008 取代 ADR 0007。

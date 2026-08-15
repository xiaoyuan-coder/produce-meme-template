# 独立仓库承载单一用户结果 Skill

上一轮多 Skill 家族没有完整继承 Unified 能力，Unified 自身又持续积累耦合规则。新能力使用独立仓库，只暴露一个端到端 `produce-meme-template` 用户结果 Skill；专业判断通过仓库内部深模块、reference、contract 和 script 组织。旧 Unified 与拆分版保持只读迁移事实源，避免新实现继续继承旧调用图和版本漂移。

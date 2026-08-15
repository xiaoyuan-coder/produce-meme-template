# ADR 0009：研发编译链路文档不阻断数据生产 Skill

- 状态：已接受
- 日期：2026-08-15

## 背景

`2026-08-15-当前对话完整Handoff-模板身份媒介与后端编译链路.md` 主要诊断 Worker 的通用身份句、`gallery.template_rewrite` 和 `imageRoleContext`。其中引用的 `gallery-template-authoring-spec.md` 与 `gallery-template-import/README.md` 当前未在工作区找到。用户确认本次职责只覆盖模板 JSON 数据质量，不负责后端实现。

## 决定

新 Skill 以当前正式数据、当前 Schema 与用户确认口径作为数据合同依据。研发编译链路、Worker 系统规则、import 实现和缺失的研发文档不构成设计或实现阻断项。

数据侧可以吸收经真实案例验证的字段撰写经验，同时不承诺字段在后端如何消费，不新增推测性后端能力。

## 影响

- 撤销 Q19。
- 新 Skill 可以继续完成 Schema、编译器、fixtures 和版本合同设计。
- 后端规则变化只在正式数据合同变化时触发本 Skill 的合同升级。

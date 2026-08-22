#!/usr/bin/env python3
"""Render the human-facing README from repository facts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def load_json(relative_path: str) -> dict[str, Any]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def stage_rows(rules: dict[str, Any]) -> str:
    contract = rules["majorStageContract"]
    artifact_names = contract["artifactNames"]
    descriptions = {
        "replacement": "冻结换图计划、生图任务和 Authoring Intent；不调用图片 API",
        "template_image": "调用 Fal API 生成候选图，确认模板图并编译 Authoring Handoff",
        "template_data": "通过 Approved Image + Handoff 增量编译并校验待 OSS 数据包",
        "final": "上传确认模板图到 OSS，回填并交付正式模板 JSON",
    }
    display_names = {
        "replacement": "换图执行",
        "template_image": "模板图生成",
        "template_data": "模板数据编译",
        "final": "OSS 最终化",
    }
    fallback_artifacts = {"approvedTemplateImage": "Approved Template Image"}

    rows = []
    for stage in contract["stages"]:
        selector = stage["selector"]
        role = stage["primaryArtifactRole"]
        artifact = artifact_names.get(role, fallback_artifacts.get(role, role))
        rows.append(
            f"| {stage['number']} | `{selector}` | {display_names[selector]} | "
            f"{descriptions[selector]} | `{artifact}` |"
        )
    return "\n".join(rows)


def describe_key_pattern(pattern: str) -> str:
    if pattern == "^[a-z][a-z0-9-]{1,59}$":
        return "小写字母开头，只允许小写字母、数字和连字符，总长 2–60 位"
    return "必须匹配当前 Gallery Template Schema 声明的正则"


def render() -> str:
    release = load_json("release.json")
    manifest = load_json("skill-manifest.json")
    rules = load_json("contracts/machine-rules.json")
    stage_contract = rules["majorStageContract"]
    supported_contract = release["supportedContracts"]["galleryTemplate"]
    gallery_schema = load_json(
        "contracts/upstream/gallery-template/"
        f"{supported_contract}/gallery-template.schema.json"
    )
    key_pattern = gallery_schema["properties"]["key"]["pattern"]
    object_key_prefix = rules["objectStorageContract"]["objectKeyPrefix"]
    execution_contract = rules["productionExecutionContract"]

    return f"""# produce-meme-template

> 本文档由 `python3 scripts/update_readme.py` 根据仓库事实源生成。请勿直接修改；更新合同或入口后重新运行生成器。

从来源网图分阶段或端到端生产可交付的 Meme 模板 JSON。整个流程保持同一个 Production Item、revision、pin 和产物谱系，并支持从最近有效的大阶段恢复。

## 当前状态

| 项目 | 当前值 | 事实源 |
| --- | --- | --- |
| Skill 版本 | `{release['skillVersion']}` | `release.json` |
| 发布验收 Profile | `{release['releaseReadinessProfile']}` | `release.json` |
| Artifact Schema | `{release['artifactSchemaVersion']}` | `release.json` |
| Gallery Template 合同 | `{supported_contract}` | `release.json` |
| 默认生产阶段 | `{stage_contract['defaultSelector']}`（完整生产） | `contracts/machine-rules.json` |
| 正式执行模式 | `{execution_contract['executionModes']['liveExternal']}` | `contracts/machine-rules.json` |
| Manifest 更新时间 | `{manifest['updated_at']}` | `skill-manifest.json` |
| Manifest 跟踪文件 | `{len(manifest['tracked_files'])}` 个 | `skill-manifest.json` |

## 四阶段生产 SOP

| 阶段 | selector | 名称 | 工作与边界 | 主要产物 |
| --- | --- | --- | --- | --- |
{stage_rows(rules)}

省略 `stage` 或指定第四阶段时，工作流依次完成全部四阶段。批量调用在大阶段之间设置屏障：先完成全部 P1 与 Prompt 检查，再并发进入 P2。第二阶段必须通过图片生成 adapter 调用 API；正式生产显式使用 `{execution_contract['executionModes']['liveExternal']}`，从 doctor 验证的安装包运行，经 Fal 队列和独立审核后进入 Aliyun OSS。省略执行模式时固定为不可交付回放。第三阶段的产物状态为 `{stage_contract['templateDataStatus']}`，只有第四阶段完成 OSS 上传与 URL 回填后才会生成正式 `gallery-template.json`。

## 核心合同

- Approved Template Image 是标题、描述、槽位默认值、`referenceImage` 和 `cover` 的视觉事实源。
- `production-execution-profile.json` 绑定执行模式、adapter 拓扑、审核方法、provider 和 runtime 安装来源；回放结果不能进入拆分交付。
- P1 必须完成 IP/文化身份发现和主体连续性冻结；`subjectEditIntent` 将身份单元、主体组件和重复实例关系注入 P3。
- 默认批次使用 `{rules['batchProductionContract']['executionPolicy']['defaultMaxConcurrency']}` 路并发，结果仍按输入顺序归集。
- 单个 Production Item 默认只创建 `{rules['generationExecutionContract']['defaultMaximumNewProviderRequestsPerItem']}` 个新供应商请求；视觉重做需要显式设置 `authorizeVisualRedo=true`。
- 高价值内容进入 `inputSchema`；可全文编辑且无需主动开槽的内容保留在 `promptTemplate`。
- 明显主体只要能通过用户上传图实现替换，就必须开放 subject 槽；多个独立身份在槽位预算内分拆为多个 subject，重复实例继续共享同一输入。
- `promptTemplate` 只承载用户可编辑内容；媒介、画风、固定构图和清理指令进入隐藏视觉合同。
- subject 图片默认继承用户上传图的清晰可见身份特征；只有核心玩法特征沿用模板值。
- 纯色铺底默认留在 Prompt 或色光事实中；缺少独立高价值编辑动机时不建立槽位。
- subject 可采用混合服装权限：上传图提供身份、配色和局部细节，模板只固定承担动作与构图机制的轮廓或体积。
- P6 推荐项审计逐槽绑定当前默认值与三条推荐值，并逐条证明同轴、同颗粒度和机制兼容；旧式 slot ID 列表在当前 Artifact Schema 中直接阻断。
- `runtimeSemantics` 负责目标定位、输入绑定和跨编辑保持的视觉事实。
- 正式业务 JSON 与生产 sidecar 分离；下游只读取 `gallery-template.json`。
- T1 是现成正式 JSON 的独立生图测试入口，不属于四个生产阶段。

## Key 与模板数据重跑

`templateKey` 是生产请求必须显式提供的模板稳定标识，P5 draft 和 P8 正式 JSON 都将它原样投影为 `key`。Skill 不会根据标题、来源图片或新一轮分析重新命名模板。

- 当前格式：`{key_pattern}`（{describe_key_pattern(key_pattern)}）。
- 同一个 Production Item 恢复执行时，继续使用原 `templateKey`；请求改 key 会触发身份完整性阻断。
- 只修正标题、描述、槽位、Prompt 或语义审计时，继续使用原 `productionItemId` 并运行第三阶段；已确认图片与 request ID 会直接复用。
- 只有模板图确实需要重做时才增加供应商请求，并在人工确认后显式传入 `authorizeVisualRedo=true`。
- 模板图未变时，OSS 对象可以幂等复用；模板图变化时，对象路径中的图片 SHA 和最终 URL 会更新，正式模板 `key` 保持不变。
- OSS 对象路径以 `{object_key_prefix}/<templateKey>/<approved-image-sha>.<ext>` 生成。Skill 交付稳定的 `key`，数据库按 key 执行 upsert 或替换由下游导入系统负责。

重跑请求示例：

```json
{{
  "productionItemId": "existing-production-item-id",
  "templateKey": "previous-template-key",
  "sourceImage": "path/to/source-image.jpg"
}}
```

## 使用入口

Python 公共 seam：

```python
from scripts.produce_meme_template import build_live_production_adapters, run_production

adapters = build_live_production_adapters(
    source_adapter=source_adapter,
    visual_review_adapter=visual_review_adapter,
    authoring_analysis_adapter=authoring_analysis_adapter,
    authoring_audit_adapter=authoring_audit_adapter,
    semantic_audit_adapter=semantic_audit_adapter,
    visual_contract_audit_adapter=visual_contract_audit_adapter,
    oss_options=oss_options,
)

result = run_production(
    request=request,
    output_root=output_root,
    adapters=adapters,
    stage=2,  # 1、2、3、4；省略时执行完整生产
    execution_mode="live_external",  # 正式生产必须显式声明
)
```

确定性 fixture 演示：

```bash
python3 scripts/produce.py \\
  --request path/to/request.json \\
  --deterministic-fixture path/to/fixture \\
  --output path/to/output \\
  --stage 1
```

正式记录拆分交付：

```bash
python3 scripts/export_gallery_templates.py \\
  --source path/to/gallery-template.json \\
  --production-manifest path/to/production-manifest.json \\
  --output-dir path/to/单模板JSON \\
  --manifest path/to/交付清单.json
```

导出前会重新执行当前正式合同与执行资格重放；单工作区来源自动发现相邻 Production Manifest，批量数组逐条重复传入 `--production-manifest`。`--manifest` 必须显式指向单模板目录外；单模板目录只保存 `<key>.json`，点文件和 sidecar 同样会被阻断。每个文件只包含一条正式记录。相同内容可幂等重跑，已有同名文件内容不同则默认阻断。

发布、安装与诊断入口：

```bash
python3 scripts/release_tool.py --help
```

稳定版按候选锁中的发布验收 profile 推进。`compatible_minor` 完成 stage 全量验证、双轴 clean review、全新临时安装和 doctor 后晋升，不调用 Fal/OSS；首稳、major 和显式外部风险使用 `live_external`，继续执行未见图前向与四场景 live。

完整的 Agent 路由读取 [`SKILL.md`](SKILL.md)。生产边界读取 [`references/vertical-slice-runtime.md`](references/vertical-slice-runtime.md)，执行授权与交付资格读取 [`references/production-execution-authority.md`](references/production-execution-authority.md)，换图与模板图验收读取 [`references/replacement-spec.md`](references/replacement-spec.md)，模板身份、槽位属性、标题、Prompt 和 runtimeSemantics 读取 [`references/template-authoring.md`](references/template-authoring.md)，数据编译读取 [`references/gallery-template-compiler.md`](references/gallery-template-compiler.md)。

## 环境与凭证

真实第二、第四阶段分别需要 Fal 和 OSS 凭证。凭证只保存在本地 `.env` 或运行环境中，`.env` 与 `.env.local` 不进入 Git、manifest 或 README。请只记录环境变量名称，禁止把实际值写入文档、测试和提交历史。

## 开发校验

更新 README：

```bash
python3 scripts/update_readme.py
```

检查 README 是否与事实源同步：

```bash
python3 scripts/update_readme.py --check
```

运行仓库合同测试：

```bash
python3 -m unittest tests.test_repository_contract
```

版本、阶段、合同、公共入口或 manifest 发生变化后，重新生成 README 并执行 `--check`。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when README.md differs from the generated content",
    )
    args = parser.parse_args()
    expected = render()

    if args.check:
        if not README.exists() or README.read_text(encoding="utf-8") != expected:
            print("README.md is stale; run: python3 scripts/update_readme.py", file=sys.stderr)
            return 1
        print("README.md is current")
        return 0

    README.write_text(expected, encoding="utf-8")
    print(f"updated {README.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

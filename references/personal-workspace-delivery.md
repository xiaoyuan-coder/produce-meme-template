# 杨媛个人工作区落盘与交付合同

## 1. 批次根目录

在 `/Users/xiaoyuan/Desktop/杨媛工作总库/01-模板数据生产与评测` 内执行正式业务生产时，先建立唯一批次根目录：

```text
04-图库模板生产/01-本月生产批次/YYYY-MM-DD-任务名称/
├── 批次说明.md
├── requests/                         # 本批输入信封或逐项请求
├── production/                       # run_production 的 output_root
│   └── <production-item>/
│       ├── production-manifest.json
│       ├── gallery-template.json     # 仅 P8 完成后存在
│       └── ...                       # pin、revision、证据和回执
└── work/                             # 不具备正式数据资格的临时编排文件
```

`run_production(..., output_root=...)` 的 `output_root` 固定为该批次的 `production/`。禁止把 Production Item 写进 Skill 仓库、桌面临时目录、`.meme-admin/` 或 `05-模板JSON`。

## 2. 正式资格与投影

- 数据台只把通过 Manifest 谱系校验且已完成相应阶段的 Production Item 计入真实生产状态；目录名、自然语言完成声明和散落 JSON 不提供正式资格。
- 需要一条模板一个文件的业务交付时，使用 `export_gallery_templates.py` 投影到 `04-图库模板生产/05-模板JSON/YYYY-MM-DD-任务名称/单模板JSON/`，并把交付清单写在 `单模板JSON/` 同级。
- `单模板JSON/` 只保存正式投影；生产证据继续留在批次 `production/`。生产 sidecar、交付清单和 OSS 回执不进入正式业务 JSON。
- `06-生产批次记录` 保留历史 Skill 直跑数据，不再作为本 Skill 新批次默认工作根。旧数据不移动；数据台继续按证据读取。

## 3. 批次说明

每个新批次必须填写 `批次说明.md`，记录输入数、成功数、失败数、待确认数、Skill 版本、验收状态、Production Item 根和正式投影路径。

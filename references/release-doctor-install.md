# Release、安装、doctor 与版本 pin 合同

## 1. 三条版本线

`release.json` 是唯一人工版本事实源。`skillVersion` 描述 Skill 行为与知识，`artifactSchemaVersion` 描述内部 P0–P8 产物形状，`supportedContracts.galleryTemplate` 描述当前支持的 Gallery Contract。三者独立演进；行为版本升级不会自动改写内部 Schema 或上游合同版本。

## 2. 不可变发布包

`scripts/release_tool.py build` 只接受仓库根目录、真实当前 Git HEAD、与 `git ls-files` 精确一致的 manifest 和干净工作区。构建前通过独立 runner 执行完整测试集和最小纵向 smoke；门禁结束后再次核对 HEAD、状态与 Git blob，再从 Git blob 构建。结果写入 `dist/produce-meme-template/<skillVersion>/`，同版本目录 create-once 并转为只读。`release-lock.json` 绑定 Git commit、构建时间、三条版本线、Gallery Schema SHA、完整 tracked file 集合、逐文件字节数与 SHA、文件集合 content digest 和 lock digest。

## 3. 安装与诊断

`scripts/release_tool.py install` 要求调用方提供受信的 expected release digest；内部自洽但不匹配该摘要的包不得安装。安装先验证完整文件与元数据，在 staging 运行最小 smoke 并复核摘要，再写入只读版本目录、create-once 安装记录并原子切换 `current`。目标目录已完成但指针尚未切换时，重跑会验证并续接。安装目录缺文件、多文件、内容漂移、mixed-version、损坏 lock 或安装记录不一致均由 `doctor` 阻断。doctor 报告运行根、实际安装来源、三条版本线、content digest、release lock digest、完整 production pin 和可执行修复提示。

源码工作区运行时，doctor 拒绝越界或符号链接 tracked file，并从 `skill-manifest.json` 的完整集合实时计算 content digest；安装副本运行时以 release lock 与安装记录为冻结依据。生产开始前必须通过 doctor。已有 Production Item 恢复时，三条版本线、Git commit、文件集合、机器规则、验证器、Replacement Spec 版本与摘要、Gallery 快照必须与 `production-pin.json` 全对象精确一致。

## 4. 显式迁移

运行中 Production Item 不读取新安装版本。确需迁移时，`scripts/release_tool.py migrate-pin` 先对账旧 pin 与 Production Manifest 中的 pin artifact ledger，再读取已通过 doctor 的新运行副本，create-once 写入 `production-pin-migration-rN.json`。报告绑定 Production Item ID、旧 template revision、manifest SHA、逐版本线差异、新旧 pin SHA 和最早失效阶段；兼容读取旧版 pin 形状。旧 pin、旧 manifest 与旧 revision 保持不变，迁移报告只提供显式重建依据。

## 5. 最小发布入口

全新会话按 build、install、doctor、produce 顺序进入纵向切片。发布构建和真实安装不调用图片供应商或 OSS；最小生产仍通过同一 `scripts/produce.py` 公共工作流和所选 adapter 完成。

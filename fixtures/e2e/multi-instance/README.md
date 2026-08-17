# 多实例、容器与接触关系 tracer

`scenarios.json` 保存相框整图、多人分格、重复宠物、人物接触物体和场景替换五类单图机制。每类都有独立的 `source.ppm` 与 `approved.ppm`，并分别声明来源语义、组件/关系、图片操作、确认模板图组件图、Approved operation 目标/锚点绑定，以及画面实例、身份、上传素材和前端控件四个期望计数。

专项测试只通过公共 `run_production` seam 观察 Replacement Plan、Generation Package、模板分析、视觉硬失败、正式交付和上传副作用；同时验证五组来源/确认图像的内容摘要互不复用。

追踪经验 E06、E12、E20、E22、E23 与 E29；字段、关系和操作枚举只读取 `contracts/machine-rules.json`。

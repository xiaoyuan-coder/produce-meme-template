# 用 Authoring Handoff 连接替换分析与模板编译

P1 同时服务于一次生图成功和 P3 高效编译。它需要冻结画面玩法、IP/文化身份发现、主体连续性、组件关系、文字语言策略和槽位机会，产出不可变的 `authoring-intent.json`。P2 在候选图通过视觉门禁后，将该意图、当前 Generation Package、Visual Review 和 Approved Image SHA 编译为 `authoring-handoff.json`。

P3 以 Approved Template Image 作为最终视觉事实权威，并通过只读 Authoring Handoff 继承已确认的语义与结构事实。它仅需分析 Approved Image 与 P1 意图之间的可见增量：最终默认值、空间定位、可见文字、渲染事实和意外偏移。P1 不得覆盖 Approved Image 的像素事实，P3 也不再从零发现玩法、IP 或主体连续性。

批量默认使用五条独立 Production Item 通道，并在用户大阶段之间设置屏障。全部可执行项先完成 P1 语义分析与 Prompt 冻结，再并发提交 P2；默认每项只创建一个新供应商请求，视觉重做需要显式授权。跨项 adapter 调用可以重叠，每项依然拥有独立目录、manifest、revision 和失败结果，归集顺序与输入顺序一致。

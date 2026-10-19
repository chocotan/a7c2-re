# 范围

- 日期：2026-10-19。
- auth：设备所有者本人的 ILCE-7CM2，授权学术研究。
- in_scope：`analysis/` 下合法取得的离线固件与反编译产物；对 `im.elf`、`libIMDB.so`、`appFw.so`、`libNetService.so` 的只读静态分析。
- network_profile：离线；未连接设备、未运行固件。
- 本轮不包含：在真机上故意损坏、截断或替换任何 `.so`；装载/卸载模块；修改启动项；任何刷写操作。
- 验收：区分"`appFw.so` 干净加载失败"与"`DT_INIT` 崩溃"两种情形，给出可复核的地址与反编译依据，并标明未确证项。

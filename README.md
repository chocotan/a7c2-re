# A7C2（Sony ILCE-7CM2 / A7C II）逆向研究

对 **Sony ILCE-7CM2（固件 2.01）** 的逆向成果集合。

> 授权范围：仅限设备所有者本人的相机及其合法取得的固件，用于学术研究。真机工具不修改只读分区、不刷写固件；离线QEMU实验不连接真机或物理SD。

## ⚠️ 风险提示（先读这一节）

本节针对下方的**真机工具**，不适用于隔离QEMU实验。真机脚本会在**你的相机上以 root 身份执行代码**，并**修改可写分区上的文件**。
它们**不刷写固件、不修改只读分区**（`/` 与 `/usr` 全程只读），但风险是真实的：

| 风险 | 机制 | 缓解 |
|---|---|---|
| **相机可能无法正常开机**（仅限 2026-10-16 的 **preload 入口**） | 该入口用 `/etc/ld.so.preload` 让**每一个动态链接的程序**加载我们的库；该库一旦出现符号版本、重定位或构造函数错误，**所有动态程序都会启动失败** | 用完必须中和（512 字节空白 + **读回校验**）；工具报 `PreloadRecoveryRequired` 时必须当真处理，不得绕过 |
| **带崩服务进程**（2026-10-18 的 **libJit 入口**） | 载荷在 USB 服务守护进程内被 `dlopen`，`_init` 若崩溃会带崩处理 USB 请求的那条线程 | 载荷刻意只做裸 syscall、**第一件事就是 `unlink` 自己**、启动子进程后立刻返回；不写 `/etc`、不发 DEBUG。后果是**需要断电重启**，不是砖 |
| **分区写满引发连锁失败** | 相机的 `/tmp`、`/etc`、`/var` 是**同一块 12 MiB vfat 分区**；写满后**所有**写入都失败——包括"中和 preload"的清理写入（本仓库作者已实际踩到） | 批量数据一律写 SD 卡；写前判断可用空间（判不出=不写） |
| **SD 卡被清空** | `--format-card` 会重建整张卡的分区表 | 用专用卡；脚本会要求显式确认 |
| **可写分区被改动** | 会在 `/setting`、`/log`、`/etc` 等位置写入测试文件（**libJit 入口只写 `/tmp/jiritsu`，即独立分区 nflasha6**） | 会话结束自动 `rm`；无删除能力的会话会记录 `left_behind`，不谎报干净 |
| **设备状态残留** | 每次会话结束后相机停在 `054c:096f`。恢复代价**取决于主机操作系统**：**Windows 必须断电重启**（关机再开机）；**Linux 重新插拔 USB 即可**，不用断电 | 详见 [`RECOVERY-AND-LESSONS.md`](report/2026-10-16-system-dump/RECOVERY-AND-LESSONS.md) |
| **脚本可能存在未知BUG**| 每个脚本请在AI的辅助下确认后再执行，最好先上虚拟机验证后上机，在第一次上机时使用影响最小的测试代码 |

**只在你自己的设备上使用**，并自行承担风险：作者不对设备损坏、数据丢失或法律后果负责（见 `LICENSE`）。

## 成果

| 日期 | 成果 | 一句话 |
|---|---|---|
| 2026-10-19 | [CMOS 输入能否从某个进程拿到](report/2026-10-19-cmos-input-path/) | 无 V4L2；帧经 `stream2` 的物理页 `mmap` + DMM/EE 缓冲进入 `im.elf` 进程，已证实的通路只给 **JPEG/NV12**，未见 RAW/Bayer；传感器归属未确证 |
| 2026-10-19 | [某个 `.so` 损坏后 WiFi 还能不能起来](report/2026-10-19-so-corruption-wifi/) | `im.elf` 的装载器不把模块加载失败当致命（`dlopen`/`dlsym` 失败只返回 -1/-2），且网络阶段先于应用阶段，所以 `appFw.so` 干净加载失败时 WiFi 仍能初始化；但 `DT_INIT` 崩溃会连带杀死同进程的网络线程 |
| 2026-10-19 | [SSH 可行性调研与实测：交互式 root shell](report/2026-10-19-ssh-feasibility/) | 原厂 22 端口只做受限转发（`MaxSessions 0`）；用 libJit 入口在 **2222** 起独立 sshd（仅密钥）并实测登录 `Uid: 0 0 0 0`。根因是 `root` 的登录 shell 为 `/bin/false`；唯一持久改动是改成 `/bin/sh`（已备份、有还原步骤） |
| 2026-10-18 | [跨机型验证：Senser/Jiritsu 链](report/2026-10-18-fx30-service-probe/) | 只读侦察证明这条链是**协议级**而非机型级的：大量存储 PID 随机型变（`0e8a`/`0e0e`），Senser 服务身份恒为 `0336`；已在 ILME-FX30 上验证 |
| 2026-10-18 | [libJit 执行入口（两台真机已验证）](report/2026-10-18-libjit-exec/) | 改用固件自身对 `/tmp/jiritsu/libJit.so` 的 `dlopen` 取得 root 命令执行：**不写 `/etc/ld.so.preload`、不发 DEBUG、会话后无残留**；已在 **ILCE-7CM2 2.01** 与 **ILME-FX30** 上验证 `Uid: 0 0 0 0`，可从该 shell 挂卡并写读 |
| 2026-10-17 | [原生 QEMU 相机实验（进行中）](report/2026-10-17-qemu-native-camera/) | 原固件菜单、原生窗口、9项操作、日期初始化修复及原Fsys虚拟写盘已验收；完整主界面/成像/拍照录像未完成。见[阶段进展](report/2026-10-17-qemu-native-camera/STATUS.md) |
| 2026-10-16 | [整机固件转储](report/2026-10-16-system-dump/) | 19 个闪存分区 / 1.80 GiB 一次会话转储完成，三重独立验证通过；关键机制是分区的**密文/明文双视图**与"预加载注入 + DEBUG 触发点"取得 root |

## 目录约定

**每个成果一个自包含目录**：该成果用到的脚本、载荷、依赖清单都放在成果目录内，便于单独阅读与长期保存；不设全局共享脚本目录。

```
report/
└── <YYYY-MM-DD>-<主题>/
    ├── README.md          入口：本成果的结论、依赖、用法与产物
    ├── <报告>.md          报告正文（思路 → 脚本 → 结论）
    ├── scripts/           本成果使用的脚本（含各自依赖说明）
    ├── payload/           本成果使用的载荷：源码 + 已构建二进制（含 SHA-256）
    └── requirements.txt   本成果的 Python 依赖
```

当某个模块确实被多个成果复用、且在各自目录中重复出现时，再抽到顶层 `shared/`（**当前未预置**，避免过早抽象）。

## 参考项目与致谢

本项目建立在以下公开工作上。**上游作者没有为本仓库的任何结论、脚本或工具背书**，也不对其他机身、
其他固件版本的兼容性负责；本仓库对它们的引用不等于转述它们的结论。

| 项目 | 本仓库用到了什么 | 没用到什么 |
|---|---|---|
| [ma1co/Sony-PMCA-RE](https://github.com/ma1co/Sony-PMCA-RE)（MIT） | USB 枚举、Senser 双阶段认证、Backup 操作与备份解析。通过 `analysis/vendor/pmca-adamwuqwq` 使用提交 `3824c398c0fa`（该提交的相关源码与上游 master 逐字节一致，见 `analysis/BOOT-AND-USB-DUMP.md`） | 其 GUI 与官方更新包流程 |
| [ma1co/fwtool.py](https://github.com/ma1co/fwtool.py) | 官方更新包的离线解包，产出整机转储用的参照文件树 | — |
| [YuuLuo/a7c2-service-preload](https://github.com/YuuLuo/a7c2-service-preload) | A7C II 2.01 **服务认证 + 机内执行链**的公开研究，是本仓库 2026-10-16 preload 入口的出发点（见 `analysis/2026-10-15_逆向-A7C2服务预加载-report.md`） | 其载荷实现与命令通道；本仓库的 worker、邮箱与终端模式是自己重写的 |
| [QAQLeonard/a7c2-fl23-research](https://github.com/QAQLeonard/a7c2-fl23-research) | 2026-10-18 的 **libJit 入口**：其真机结果表明"写 `/tmp/jiritsu/libJit.so` 会让固件自己 `dlopen`"值得验证。本仓库读了其提交 `dd01c07` 的源码，然后**用本项目自己的反编译证据独立复核了触发条件**，并据此实现入口（[成果](report/2026-10-18-libjit-exec/)） | 其补丁字节、色彩单元合并、安装/还原流程；与本仓库无代码派生关系，我们也没做 FL2/FL3 移植 |
| [lzh2016zzz/a7cr-fl23-research](https://github.com/lzh2016zzz/a7cr-fl23-research) | 仅记录来源：上一条的 FL2/FL3 方法源自这里 | 本仓库未使用 |

本仓库自有代码以 MIT 发布（见 [`LICENSE`](LICENSE)）。上述项目的许可证各自独立（PMCA-RE 为 MIT），
不因本仓库的许可而被覆盖。

## 按成果查找

- 想了解**方法**：直接读 `report/<成果>/README.md` 与报告正文；
- 想**复现**：进入成果目录，按其 `README.md` 的"依赖"一节准备 `scripts/vendor/`、Python 包与系统工具；
- 想**核对结论**：报告中的"判读纪律"一节列出了三条必须同时满足的验证要求。

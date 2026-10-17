# AGENTS.md — 本仓库的工作约定

适用于任何在此仓库（`a7c2-re`）中工作的 agent 或人。**动手前先读这一节。**

## 0. 这是什么

对 **Sony ILCE-7CM2（A7C II，固件 2.01）** 的授权逆向成果集合，每个成果一个自包含目录
（`report/<日期>-<主题>/`：报告 + 脚本 + 载荷 + 依赖清单）。许可见 `LICENSE`（MIT）。

## 1. ⚠️ 风险（必须让读者知道）

这些脚本会在**相机上以 root 身份执行代码**，并**修改可写分区上的文件**。不刷固件、不动只读分区，
但风险真实存在：

| 风险 | 机制 | 缓解 |
|---|---|---|
| 相机可能无法正常开机 | 代码执行路线①用 `/etc/ld.so.preload` 让**每个动态链接程序**加载我们的库；该库一旦有符号/重定位/构造函数错误，**所有动态程序都会启动失败** | 用完中和（512 字节空白 + **读回校验**）；工具报 `PreloadRecoveryRequired` 时必须当真处理，不得绕过 |
| 分区被写满导致连锁失败 | 相机 `/tmp`、`/etc`、`/var` 是**同一块 12 MiB vfat 分区**；写满后**所有**写入失败（连中和 preload 的清理写入也失败，已实际发生） | 批量数据一律写 SD 卡；写前用 `jiritsu_space.py` 判可用空间（判不出=不写） |
| SD 卡被清空 | `--format-card` 会重建分区表 | 用专用卡；脚本要求显式确认 |
| 可写分区被改动 | 会在 `/setting`、`/log`、`/etc` 等写测试文件 | 会话结束自动 `rm`；纯服务会话无删除能力时记录 `left_behind`，不假装干净 |
| 设备状态残留 | 会话结束后相机停在 `054c:096f`，需**断电重启**才能回到海量存储身份 | 见 `RECOVERY-AND-LESSONS.md` |

**只在你自己的设备上使用。** 作者不对设备损坏、数据丢失或法律后果负责。

## 2. 使用前提

- 需要 vendored 第三方 **Sony-PMCA-RE** 放在 `scripts/vendor/pmca-adamwuqwq/`（本仓库不含，
  见成果 README 的 clone 命令与 revision）；它需要 `pyusb` + `pycryptodomex`。
- 载荷 `.so` 已在 `payload/`（脚本会按 `scripts/` → `scripts/payload/` → `../payload/` 查找，
  并校验 SHA-256 ✓）。
- 相机侧 shell 很精简：**没有** `df head tail md5sum sha256sum mkfifo printf find stat id`；
  **有** `cat dd grep sed mknod kill mount ps sleep tar cp gzip cpio vmstat`、`/sbin/insmod`。
  因此 FIFO 用 `mknod <path> p`；命令用**引号 heredoc**；输出增量读用 `dd bs=1 skip=<偏移>`。

## 3. 判据纪律（内容型证据）

- **权限**只看 `/proc/$$/status` 的 `Uid`/`Gid`（`0 0 0 0`），**不看提示符**。
- **写入**必须读回比对摘要；"命令返回 0"不算证据。
- **镜像**要 `e2fsck` 结构 + 跨通道摘要 + 参照树全树比对，三者齐备。
- **失败也要留证据**（返回码、原始字节、原因）；**否定结论同样入档**，未验证项明确标注。
- 同一返回码可能有多种原因（如 `0x10000009` 出现在"分区已满"与"写 0 字节文件"两种情形）
  —— 用**对照实验**区分（同意尺寸写另一个分区），不要挑一个解释就下结论。

## 4. 代码与文档约定

- 不给相机执行的脚本写非 ASCII（邮箱信封曾因 ASCII 编码失败，读起来像"载荷没跑"）。
- 不提交二进制与产物：`.gitignore` 已排除 `vendor/`、`evidence/`、`output/`、`*.img`、`*.zip`。
- 文档里不写机器专属绝对路径；命令、路径、revision 都要是别人能照做的。
- 改动 `scripts/` 里的脚本后，跑一遍 `test_*.py`（若随仓库提供）或至少做 `--dry-run` 与导入自检，
  **测试通过再提交**。

## 5. 设备速查（实测）

| 项 | 值 |
|---|---|
| USB 身份 | 海量存储 `054c:0e8a` → 服务 `054c:0336` → 结束后 `054c:096f` |
| 内核 | `4.9.337-scel+`（有 ext2/vfat，**无 exfat/ext4**） |
| 分区双视图 | `pnflasha*` = 密文物理视图；`cnflasha*` = 明文视图（内核按 ext2 挂 `/` `/usr`）；`/dev/nflashaN` 是指向 `cnflashaN` 的符号链接 |
| DRAM | 基址 `0x400000000`（`/proc/iomem` 的 System RAM），`MemTotal ≈ 229 MB` |
| `/dev/mem` | 读 DRAM ✓；非 DRAM（BootROM `0xffff0000`、SCU）→ `Bad address` ✗；`/proc/kcore` 不存在 ✗ |
| 代码执行 | ① 预加载 + 固件 DEBUG 触发点（会话内）② 开机 `/etc/profile` 被 `ash --login` 以 root source（持久，已验证） |
| 固件写入语义 | 覆写已存在文件**会截断**（可回收空间）、可创建新文件、0 字节文件可写；分区满时一切失败 |
| 存储 | 卡 ext2 分区 37.5 GB（推荐批量目标）· `/log` 296 MB 空闲 · `/tmp`+`/etc`+`/var` 仅 ~4 MB |

# 恢复流程与设备约束（实测记录）

这份文档记录的是**踩过的坑和爬出来的步骤**，全部来自本机真机观测；每条都给出证据，未验证的一律标注。

## 1. 一条硬规则：批量数据写 SD 卡，不写相机内部分区

相机的 `/tmp`、`/etc`、`/var` **是同一块 12 MiB vfat 分区**（`nflasha10`）。往里写大文件会把它填满，而后果**不止于写不进去**：

```
一次失败的 RAM 转储回退写到 /tmp
→ dd 报 "No space left on device"（半成品 2,998,272 B 留在 /tmp）
→ 之后**任何**对该分区的写入都失败：0x10000009
→ 连"把 /etc/ld.so.preload 置为中性"的清理写入也失败
→ 预加载停在不可确认状态，工具 fail-closed 报 PreloadRecoveryRequired
```

**所以**：镜像、内存转储、dd 输出等批量数据一律写到卡的 ext2 分区；相机侧只保留千字节量级的文件（载荷 70,912 B、邮箱几百字节）。
工具侧已有护栏：`jiritsu_memread.TMP_BUDGET = 1 MiB`，超过预算且卡未挂载时**在写之前**直接拒绝。

> **唯一的例外**：预加载载荷 `/tmp/jiritsu/a7c2-payload.so`（70,912 B）与 worker 邮箱
> （`a7c2-cmd.txt` / `a7c2-out.txt`，几百字节）必须留在内部——`/etc/ld.so.preload` 的条目在任意
> 程序 `exec` 时被读取，若指向未挂载的卡就会失效。代价固定约 70 KB。
> 内存读取的小请求回退到 **`/tmp/jiritsu`（nflasha6，25 MiB，约 20 MiB 空闲）**，不再落到
> `/tmp`（nflasha10，12 MiB，最紧的那块）；上限 256 KiB，且取回后立即 `rm`。
> 挂载点本身在 `/tmp/sd`：只读 rootfs 里没有其它可写目录能创建挂载点；那只是一个目录项，写进去的文件落在卡上。

### 会话的命令顺序（命令/终端会话）

```
1. mkdir -p /tmp/sd; [ -b /dev/mmca2 ] && mount -t ext2 /dev/mmca2 /tmp/sd; echo CARD_MOUNT_RC=$?
2. 调用者给的命令
3. rm -f <我们的检查文件>
```

**为什么第一步必须是挂卡**：卡上的东西（工具、邮箱、数据）只有在卡挂载后才存在，而 worker
是我们在相机里唯一的 shell —— 所以这是物理上最早能挂的时刻。`--no-mount-card` 可关闭。

**为什么载荷不能放卡上**（哪怕第一步就挂卡）：`/etc/ld.so.preload` 的条目是在**任意程序 exec 时**
被动态链接器读取的，那一刻根本还没有 shell 去执行 mount。所以载荷（70,912 B）必须留在常驻挂载的
内部分区；其余一切都可以放卡上。

### 每个检查写到哪里（避免再踩同一个坑）

| 开关 | 体积 | 落在 |
|---|---|---|
| `--write-check` | 1 KiB | `/tmp/jiritsu/a7c2-write-check.bin` → **nflasha6（25 MiB）** |
| `--traversal-write-check` | 256 B | `/etc/a7c2-write-check.bin` → **nflasha10（12 MiB，最紧的那块）** —— 已冗余（arming 自己会读回校验），跳过即可 |
| `--sd-write-check` / `--dump-to-card` | 1 KiB / 批量 | SD 卡 ✓ |
| `--mem-read` | 区域大小 | SD 卡 ✓；小请求回退 nflasha6，≤256 KiB 且取回后 `rm` ✓ |
| `--deploy-*` | 70 KB + ≤256 KiB | nflasha6 ✓（载荷与邮箱属机制必需） |
| `--write-file <路径>` | 任意 | 由调用者指定；默认不执行 |
| `--dump-partition`（LEGACY） | 1 MiB/块 | 相机自身闪存 ✗ —— 默认拒绝，需 `--allow-internal-chunking` 显式覆盖 |

## 2. 固件的文件写入语义（实测，不是推断）

| 情形 | 结果 | 证据 |
|---|---|---|
| 覆写**已存在**文件（长度 ≤ 现有） | ✓ 成功 | 29 B 写进 512 B 文件（arming）、256 B 写进 256 B 文件（traversal 检查） |
| 覆写**会截断**被写文件 | ✓ 确认 | 2,998,272 B 的文件被 2 B 覆写后读回 2 B |
| **创建**新文件 | ✓ 成功 | `/etc/a7c2-write-check.bin`（256 B，从无到有） |
| 写**0 字节**文件 | ✓ 成功 | `/etc/ld.so.preload`(0 B) 被写成 2 B，读回一致 |
| 分区已满时的任何写入 | ✗ 失败 `0x10000009` | 见 §1 的因果链 |

**早先的"不能扩展文件"结论是错的**：那两次失败（0→72 B、0→512 B）发生在分区已被写满之后，与长度无关。判别方法是**同尺寸写到另一个分区**：`/setting`（另一分区）成功、`/etc` 失败 → 只能是分区问题。

**空间回收手段**：没有删除 API，但"覆写即截断"可用 —— 把一个占空间的文件覆写成几字节即可回收（本机用它对 2.86 MiB 与 4 MiB 的两个遗留文件各回收了一次）。

## 3. 恢复流程（从"预加载状态不明"回到可用）

1. **先确认不是空间问题**：对该分区写一个**同尺寸**的小文件到**另一个分区**做对照；两边结果不同即分区满。
2. **回收空间**：用"覆写即截断"把**我们自己的**大遗留文件压到几字节（不要动相机自己的数据，例如 `/etc/kikilog.dat`(4.2 MB)、`/tmp/jiritsu/mediainfo.txt`）。
3. **中和 `/etc/ld.so.preload`**：写入纯空白。注意 arming 护栏的语义是"不覆盖会加载库的内容" —— 如果该文件里是来路不明的非空白内容，arming 会**正确拒绝**，必须先中和。
4. **arming 并验证**：`--deploy-shell --approve-preload arm-preload-once` + 一条打印 `/proc/$$/status` 的 `Uid`/`Gid` 的命令。
   **判据只看内容**：`Uid: 0 0 0 0` + `Gid: 0 0 0 0`，从不看提示符或命令返回码。
5. **收尾**：确认 `disarmed: true` 且 `disarm_method=content-verified`；用 root 执行 `rm` 清掉我们的遗留（现在能真正删除）；`/etc/ld.so.preload` 留在中性状态。

本机最后一次成功恢复的判据（可直接照抄比对）：

```
armed=True payload_ran=True mailbox_ok=True disarmed=True worker_stopped=True
disarm_method=content-verified
Uid: 0 0 0 0     Gid: 0 0 0 0
```

## 3.5 第二条代码执行入口：`/etc/profile`（已验证）

`/sbin/init` 与 `/sbin/init2` 的结尾都是 `while true; do ash --login; done`，而 `/etc` 在一个**可写**分区上。
把一个脚本写进 `/etc/profile`，登录 shell 就会 source 它 —— **以 root 身份**。

实测（2026-10-16，复刻"非交互"这一更严条件）：

```sh
rm -f /etc/profile-ran.txt
/bin/ash --login < /dev/null > /tmp/ash-out.txt 2>&1
ls -l /etc/profile-ran.txt; cat /etc/profile-ran.txt
→ -rw-r--r-- 1 root root 12 /etc/profile-ran.txt
→ profile-ran
```

要点：

- 判据是**内容**（标记文件与内容 ✓），不是返回码，也不是"看起来成功了"；
- 该文件（256 B）**跨断电重启持久** ✓，实测读回摘要一致 ✓；
- 这提供了**不依赖 arming** 的持久 root 执行入口：把要做的事写进 `/etc/profile` 即可（开机自动执行，无需 USB 会话）；
- 写入用 `--write-file /etc/profile=<本地文件>`（MSC 层 ✓，见 §2 的写入语义）；
- **可逆**：把内容覆写成 2 字节（如 `##`）即失效；不需要时直接 `rm` ✓。

## 3.6 预装救砖网（必须在设备健康时装）

会话通道要靠设备能认证才用得起来；真变砖时它自己就进不去了。所以救砖网只能**提前装**。

```bash
python usb_service_probe.py --install-rescue \
  --deploy-shell --approve-preload arm-preload-once --deadline 300
```

安装后的布局：

| 位置 | 作用 |
|---|---|
| `/etc/profile` | 钩子：挂卡 → 若卡上有 `/tmp/sd/a7c2-rescue.sh` 就执行（每次登录 shell、root） |
| 卡上 `a7c2-rescue.sh` | 先把 `/etc/ld.so.preload` **截断为 0 字节**（不加载任何库 = 解除卡死），再按 `a7c2-backup/MANIFEST` 把备份复制回系统 |
| 卡上 `a7c2-backup/` | 备份：载荷（已知可用）、`/etc/profile`；`ld.so.preload.saved` **仅作参考、永不还原**（还原武装条目会再次弄坏相机） |
| 卡上 `a7c2-rescue.log` | 每次执行追加日志，相机存储异常时仍可读 |

**覆盖范围（实测依据）**：条目指向缺失的库 ✓、条目是垃圾文本 ✓、分区写满 ✓、遗留文件 ✓ —— 可救。

**救不了**：库本身致命损坏（未定义符号 / 重定位 / 构造函数错误）✗。
实测依据：`/bin/busybox` 是**动态链接**（`interpreter /lib/ld-linux-aarch64.so.1`）、`/bin/sh` 是它的符号链接、
`/sbin/init` 与 `/sbin/init2` 的 shebang 都是 `#!/bin/busybox sh` —— 坏库会让**每个动态程序**失败，
于是 **init 都起不来** → 钩子永不执行 ✗。

→ 该情形只能**预防**：永不把未校验的库留在武装状态（工具强制校验 SHA-256 并在会话结束中和）；
需要持久化时优先用 `/etc/profile` 脚本（语法错误只让登录 shell 报错并继续，不会像坏库那样连 busybox 都起不来）。

## 4. 会话之后怎么恢复（取决于主机操作系统）

每次服务会话结束后相机停在 `054c:096f`（或短暂 `0336`）。

**断电重启的要求取决于主机操作系统**（用户实测，2026-10-18）：
- **Windows**：会话后停在 `096f`，必须**断电重启**（关机再开机）才能回到 `0e8a`；
- **Linux**：不需要断电，**重新插拔 USB 即可**回到 `0e8a`。

本项目所有实测都在 Linux 上做，因此上面描述的"停在 `096f`"只说明会话末态，
不说明恢复代价。Linux 上偶尔会先回到 `0336`（该身份拒绝重新认证），再插一次或重新上电即可。

补充：拔插后偶尔先回到 `0336`，而 `0336` **拒绝重新认证**（实测 `GenericUsbException`，认证包读取即失败）
→ 该身份只能由 `0e8a` 经两阶段认证进入，所以此时再插一次（或上电）即可。

## 5. 本机的能力边界（实测）

| 目标 | 路径 | 状态 |
|---|---|---|
| 任意命令执行 / 文件读写（服务层） | MSC 层 `0x0f/0x10/0x11` | ✓ 可用 |
| 代码执行（会话内） | 预加载 + DEBUG 触发点 | ✓ 可用（需先中和/武装，见 §3） |
| 代码执行（开机持久） | `/etc/profile` 被 `ash --login` source | ✓ 可用（已验证，见 §3.5） |
| **读物理内存** | `/dev/mem` @ DRAM | ✓ **可用**（DRAM 基址 `0x400000000`，见 `/proc/iomem`） |
| 读非 DRAM 区域（BootROM `0xffff0000`、SCU） | `/dev/mem` | ✗ `Bad address`（不在资源树里） |
| 整机 RAM 转储规模 | `MemTotal: 234240 kB`（≈229 MB） | 写卡完成即可（见规则 §1） |
| `/proc/kcore` | — | ✗ 不存在 |
| 厂商 senser `FileControl`（`0xFF01`） | 任意路径（含 HASP 读取成功后） | ✗ 一律返回 `128`，本机不可用 |
| 厂商 senser 终端（TE） | `0x83/0x04` 端点对 | ⏳ 未验证（首次尝试用错了端点，见报告 §3.4） |

## 6. 相机侧工具集的坑

- **没有** `id`、`head`、`tail`、`md5sum`、`mkfifo`、`printf`、`find`、`stat`、`df`；
- **有** `cat dd grep sed mknod kill mount ps sleep tar cp gzip cpio vmstat`、`/sbin/insmod`、`/usr/bin/openssl`、`/usr/bin/gpioctrl.elf`；
- 因此：FIFO 用 `mknod <path> p` 建；命令用**引号 heredoc** 喂（免转义、不依赖 applet）；读输出用 `dd bs=1 skip=<偏移>` 增量取（`tail` 不存在）；
- 判 root 用 `busybox grep -E '^(Uid|Gid):' /proc/$$/status`；
- **给相机执行的脚本避免非 ASCII**：邮箱信封曾用 ASCII 编码，含中文的命令会 `UnicodeEncodeError`（已在 `jiritsu_preload_deploy.submit_command` 改为 UTF-8，并加了行为级回归测试）；
- `/tmp` 是 vfat，**不能建 FIFO**（`mknod` → `Operation not supported`）→ FIFO 放 tmpfs（`/tmp_wbshd`）或 devtmpfs（`/dev`）。

## 7. 未验证项（如实标注）

- 厂商 senser 终端端点对 `0x83/0x04`（第一次尝试用错了端点而得出否定结论，需重测）；
- 服务通道的 `MemoryDump`（`0xFF03`）是否能读非 DRAM 物理区域（BootROM/SCU 的正路）。

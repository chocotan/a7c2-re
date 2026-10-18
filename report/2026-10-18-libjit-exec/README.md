# libJit 执行入口：不写 `/etc/ld.so.preload` 的 root 代码执行（2026-10-18）

把"在相机上执行任意命令"的入口，从本项目的旧方案（写 `/etc/ld.so.preload` +
发 DEBUG 命令让某个进程 `exec` 时把载荷拉进来）换成**固件自己的 JIT 加载路径**：
向 `/tmp/jiritsu/libJit.so` 写入一个共享对象，固件写完 `fsync` 之后自己 `dlopen` 它。

换的理由只有一个：旧方案会在 `/etc` 上留下一个已武装的加载决定，而 `/etc` 所在的
12 MiB 分区一旦写满，连"中和 preload"这一步都会失败 —— 那正是
[`../2026-10-16-system-dump/RECOVERY-AND-LESSONS.md`](../2026-10-16-system-dump/RECOVERY-AND-LESSONS.md)
记录的唯一不可确认状态。JIT 路径不写 `/etc`、不发 DEBUG（因此不改时钟）、也没有后续清理动作。

> 当前状态：**已在一台 ILCE-7CM2 2.01 真机上跑通**（2026-10-18，见第 6、9 节）：载荷在服务
> 进程内以 `euid=0` 执行、命令输出回传、模块自删、`/etc` 未被触碰。

## 1. 机制（本项目反编译 + 反汇编证据）

证据摘录已固化在 [`evidence/libjit-loader-excerpt.txt`](evidence/libjit-loader-excerpt.txt)
与 [`evidence/set_file2-dlopen.txt`](evidence/set_file2-dlopen.txt)。原始出处是
`analysis/evidence/jiritsu-static/ghidra-file-commands.txt`（服务库反编译）与
`analysis/evidence/jiritsu-static/ghidra-decompiled.txt`（分派层反编译）。

| 事实 | 证据 |
|---|---|
| 写入命令 `adj_cmd_set_file2`（cmd `0x10`）**写完之后自己 `dlopen`**，紧接着 `dlclose` | `open(path,0x241)` → `write` → `fsync` → `close` → `strcmp(path,"/tmp/jiritsu/libJit.so")` → `dlopen("/tmp/jiritsu/libJit.so",1)` → `dlclose()` |
| dlopen flags = **`1`（RTLD_LAZY）**；**没有 `dlsym`**、没有符号名要求 | 同上，`dlopen` 之后直接 `dlclose`，中间无 `dlsym` |
| 路径是 `strcmp` **全串相等**，字面量 `/tmp/jiritsu/libJit.so` | `strcmp((char *)local_a40,"/tmp/jiritsu/libJit.so")`；`..` 之类的写法会被写入但**不会**加载 |
| 目标路径来自前一条 cmd `0x0f` 选定的路径（`get_access_filename(0xff, "/tmp/jiritsu", …)`，前缀匹配后原样复制） | `adj_cmd_set_file2` 开头；与旧工具用的是同一条路径机制 |
| 写入前先挂 `/dev/nflasha6` 到 `/tmp/jiritsu`（25 MiB，`EBUSY` 视为成功） | `sys_mount("/dev/nflasha6","/tmp/jiritsu")` |
| **`/tmp/jbusy` 存在时直接拒绝**（返回 `0x11`，一个字节都不写） | `sys_file_chk_exist("/tmp/jbusy") == 0 → uVar9 = 0x11` |
| `/tmp/jiritsu` 全目录占用 + 载荷不得超过 **25 MiB**（`0x1900000`） | `glob("/tmp/jiritsu/*")` 求和后比对 `0x1900000` |
| 打开模式 `0x241` = `O_WRONLY\|O_CREAT\|O_TRUNC`（覆写即截断） | `CFileAccess::open(path,0x241)` |
| **`dlopen` 失败时文件已写并 fsync，但返回码变成 `0x10000000`**；成功才是 `0` | `if (lVar17 == 0) { … uVar9 = 0x10000000; }` |
| 响应在 `_init` **之后**才发出（内联，同线程） | `dlopen` 在请求处理函数体内；`packet_set_process_end` 在 handler 返回后 |
| 密码学意义上不降权：整库 127 个 UND 符号里没有 `setuid/setgid/seteuid` | `dynsyms.txt` 全表 |

关键推论：`dlopen()` 在任何 `dlsym` 之前运行对象的 `DT_INIT`（即 `_init`）。
**所以只要文件能写成、能被 dlopen，我们的代码就一定先跑**，不需要满足任何导出符号要求
—— 而这正是本目录载荷的唯一依赖。

（同一台相机上还有**另一条** JIT 加载路径：`cmd_start` → `CImJiritsu::run()`，它走
`ramdisk_mount("size=5M")` + `copy` + `dlopen` 并要求 `CreateScriptI`/`LoadScriptI`/
`RunScriptI`/`DestroyScriptI` 四个符号。本工具**不走**那条路，也不去构造 `cmd_start` 的帧。）

## 2. 真机已证实的触发条件

2026-10-18 的实机运行（第 9 节）把 §1 的静态推断全部落成事实：写入该路径后，固件内联
`dlopen` 成功、`_init` 运行、命令输出回传。运行前 `/tmp/jmode` **不存在**，运行后也不存在
—— 说明本条路径**不需要** `jmode`（`jmode` 是 §1 末尾那条 `cmd_start` 路径的前置）。

`arm_write_returned` 与 `module_ran` 相隔 0.28 s，与"dlopen 在请求处理函数内联、响应在
`_init` 之后才发出"一致。

## 2.1 载荷为什么这样写

`_init` 的顺序是刻意的，每一步都对应上面的一条约束：

1. **先 `unlink("/tmp/jiritsu/libJit.so")`** —— 之后无论这个函数怎么失败，磁盘上都没有
   可被再次加载的对象。这一步在权限检查之前，因为它不依赖任何前提。
2. 检查 `geteuid()==0`，把 `pid/euid/status` 写进 `a7c2-exec.start`（带版本标签
   `A7JITE01`）。这就是"模块到底有没有跑、以什么身份跑"的唯一可信证据。
3. 读 `/tmp/jiritsu/a7c2-exec.request`（≤2047 B 的 shell 脚本），
   把 `/bin/sh` 作为**子进程**启动（`clone(SIGCHLD)`，不带 `CLONE_VM`；aarch64 没有 `fork`），
   子进程 stdout/stderr 重定向到 `a7c2-exec.out`。
4. 父进程立刻返回 `_init`。这不只是性能问题：`dlclose` 紧随 `dlopen`，
   若 `_init` 里做长活，整条 USB 请求会被命令的整个运行时间堵住。
   子进程是独立进程，父侧 `dlclose` 把模块 unmap 掉不影响它。
5. 命令的退出码由**脚本最后一行**写入 `a7c2-exec.status`，并把每次运行的 nonce 带进去，
   所以上一次运行留下的行不可能冒充这一次。

载荷是裸 `svc 0` syscall、`-nostdlib`、**无 NEEDED**：它跑在一个我们无法控制的进程里，
不能假设任何库符号可解析。

## 3. 与旧 preload 方案的差异

| 维度 | 旧方案（preload） | 本方案（libJit） |
|---|---|---|
| 执行入口 | 写 `/etc/ld.so.preload` → 发 DEBUG → 某进程 exec 时加载 | 写固定路径 → 固件写完自己 `dlopen` |
| 触碰的分区 | **`/etc`（12 MiB，与 `/tmp`、`/var` 同一块）** | 只碰 `/tmp/jiritsu`（nflasha6，25 MiB，独立分区） |
| 副作用 | DEBUG 会执行 `/bin/date -s …`（**改系统时间**） | 无 DEBUG、无设时 |
| 会话后残留 | 中和写入失败就留下无法确认的武装状态 | `_init` 第一条就是 `unlink` 自己；`/etc` 从未被碰 |
| 命令通道 | 常驻 `sh` + FIFO 邮箱，可交互、多命令、增量取输出 | 一次一条命令，结果走 3 个小文件 |
| 失败可见性 | 邮箱无输出 = 没跑 | 同样的判据：`a7c2-exec.start` 不出现 = 没跑（写入返回码不可信，见第 1 节） |

代价：一次会话只能出一次命令（这个入口只能被 `dlopen` 一次）。需要多命令时把多条写进
同一个 `--command` 脚本即可。

## 4. 目录内容

| 路径 | 作用 |
|---|---|
| `scripts/libjit_requests.py` | 帧构造与守卫：`select`/`write`/`read`、脚本包装、结果解析。纯函数，无 USB 依赖 |
| `scripts/libjit_exec.py` | 主机端：双阶段认证、预检（`libJit.so`/`jbusy`/`jmode`）、装入口、轮询、取输出、收尾、事件记录；`--unmount-camera-volumes` 处理桌面自动挂载 |
| `scripts/payload/libjit_exec.c` | 机内载荷（见第 2 节） |
| `scripts/payload/build.sh` / `BUILD.txt` | 构建脚本与工具链记录 |
| `scripts/payload/libjit_exec.so` | 编译产物，5,328 B，SHA-256 `a71fbf4f0f6afdd44b07bce0d42750b5cbad8d4c95a8b70215bd039dbfd143f9`（含清理模式，见 §5.1） |
| `tests/` | 17 项离线测试（帧字节、路径守卫、解析器、载荷 ELF 结构、与已复核 allowlist 的一致性） |
| `evidence/` | 第 1 节引用的反编译/反汇编原文片段；真机运行的原始记录（`start.bin`/`status.bin`/`output.bin`/`run.json`/`events.jsonl`）。**`evidence/` 被本仓库 `.gitignore` 排除**（原始证据不出库），所以下文指向它的链接在 GitHub 上不存在；关键数据已逐条内联在正文里 |

主机端**不复制** Senser 帧实现：它复用同仓库
[`../2026-10-16-system-dump/scripts/jiritsu_version_read.py`](../2026-10-16-system-dump/scripts/jiritsu_version_read.py)
里已复核过的 `exchange()`/`authorize()`，那条字节布局只保留一份。用 `--scripts` /
`A7C2_SCRIPTS` 指向别处；PMCA 用 `--pmca` / `A7C2_PMCA`。

## 5. 用法

前置条件与旧工具相同：相机 `ILCE-7CM2` 2.01、USB 设为"大量存储"、相机卷**未挂载**、
`054c:0e8a` 与 `054c:0336` 两个身份的驱动就绪（Windows 需 Zadig/libusbK）、需要 `sudo`。

系统自带的 `python3` 没有 `pyusb`/`Cryptodome`，用本项目已有的 venv
（`analysis/.venv`，本机实测可用）；下面的 `PY` 代指它。

```sh
cd final/a7c2-re/report/2026-10-18-libjit-exec/scripts
PY=../../../../../analysis/.venv/bin/python
sh payload/build.sh                                                    # 需要 aarch64 交叉编译器

$PY libjit_exec.py run --command 'uname -a' --dry-run                  # 不接相机，只打印帧
sudo $PY libjit_exec.py run --command 'cat /proc/$$/status | grep -E "^(Uid|Gid):"' \
     --keep ../evidence/$(date +%F)-run
sudo $PY libjit_exec.py cleanup                                        # 真删本工具留在相机上的文件
```

会话前相机两个卷会被桌面自动挂载，必须先卸载（工具用 sysfs 祖先匹配，只会拦、不会动别的盘）：

```sh
lsblk -o NAME,LABEL,MOUNTPOINT | grep -E "A7C2CAM|PMHOME"
udisksctl unmount -b /dev/sdX1
```

`--keep <dir>` 只决定**本地**证据写到哪里（事件流、相机侧文件副本、命令输出）；相机侧的
`a7c2-exec.*` 默认在收尾时被截到 1 字节，不会给你留垃圾。只有需要现场时才加
`--leave-on-camera`。

退出码：`0` 成功，`2` 模块根本没运行（看事件流里的 `dlopen_failed`），
`3` 模块运行了但命令未完成，`4` 命令返回非零。标准输出是命令的原始输出，事件流（JSON）走标准错误。

这个相机**没有 `id`**（busybox 没编进去），第一次真机跑 `id` 返回 `exit 127`。判身份用
`cat /proc/$$/status | grep -E '^(Uid|Gid):'`，或者直接看结果 JSON 里的 `euid`。

批量数据不要从这条通道回传：`--max-output` 默认 256 KiB。大文件请让命令自己挂卡写
`/tmp/sd`（相机 `/dev/mmca2`），这与本项目"批量数据一律写 SD 卡"的硬规则一致。

## 5.1 收尾：真删，不是截断

载荷以 root 跑，所以清理就是普通 `unlink` —— 不需要固件的按路径删除命令（`0x12`，
其载荷布局仍不在本项目证据内）。两条规则：

- **每次运行开工前**，载荷先把上一轮留下的 `start` / `status` / `out` 删掉，残留不会跨会话累积。
- **`cleanup` 子命令**用一个保留请求 `#A7JIT-CLEANUP` 让载荷把 5 个路径（`libJit.so`、
  `request`、`start`、`status`、`out`）全部删掉，并且**不启动 shell、不写任何记录**。
  成功的证明就是这些路径**事后都不存在** —— 所以它不写"我做完了"这类文件（写了就等于
  没清干净）。工具会先记录"删除前各文件字节数"，再逐个确认消失。

普通用户命令无法触发这条分支：`build_script()` 拒绝以该标记开头的命令（有测试）。

## 6. 验收矩阵

| 项 | 结果 | 说明 |
|---|---|---|
| 载荷可复现构建 | ✅ | `payload/BUILD.txt` 记录编译器版本、完整命令行、SHA-256、体积 |
| 载荷是 AArch64 `ET_DYN`、带 `DT_INIT`、**无 NEEDED** | ✅ | `tests/test_payload_artifact.py` 直接解析 ELF |
| 帧字节与守卫 | ✅ | `tests/test_libjit_requests.py`（路径越界、NUL、非 ASCII、长度、nonce） |
| 与旧 transport 的 allowlist 兼容 | ✅ | 用 `jiritsu_version_read.authorize()` 实测接受本工具的帧 |
| `--dry-run` 帧输出 | ✅ | 见第 5 节 |
| 离线测试 | ✅ **17 passed** | `python3 -m pytest tests/ -q` |
| 写入即 `dlopen` | ✅ 静态证据 | 第 1 节；`adj_cmd_set_file2` 内联 `dlopen`/`dlclose` |
| **真机：`vfat` 上 `dlopen` 成功** | ✅ | 写返回结果 `0`（非 `0x10000000`），`a7c2-exec.start` 随即出现 |
| **真机：载荷运行、`euid` 实际值** | ✅ **`euid=0`** | `A7JITE01 v1 pid=532 euid=0 status=0` |
| **真机：命令输出与退出码回传** | ✅ | `uname -a` 输出完整；`id` 不存在 → `exit 127` 被正确回传 |
| **真机：模块自删、无遗留武装状态** | ✅ | 运行后读 `/tmp/jiritsu/libJit.so` 返回 `0x10000006`（不存在） |
| **真机：不需要 `/tmp/jmode`** | ✅ | 运行前后 `jmode` 都不存在 |
| **真机：进程凭据按项目判据** | ✅ | `Uid: 0 0 0 0` / `Gid: 0 0 0 0`（`/proc/$$/status`，非提示符、非自报） |
| **真机：`vfat` 挂载未带 `noexec`** | ✅ | `/dev/nflasha6 /tmp/jiritsu vfat rw,sync,dirsync,noatime,…` |
| 证据流完整（含线路级 trace） | ✅ | `events.jsonl` 93 行 / 28 条 `jiritsu_request` |
| **真机：从 root shell 挂 SD 卡并写读** | ✅ | `mount -t ext2 /dev/mmca2 /tmp/sd` rc=0，写入 19 B 后 `cat` 读回一致，`umount` rc=0 |

## 7. 风险与边界

- **在服务进程里 `fork` 一个子进程**，我们从不 `wait`：若守护进程用 `waitpid(-1)` 收尸，
  可能收走我们的子进程；这不影响我们，本工具也不去改它的信号处理。
- **`_init` 若崩溃会带崩处理 USB 请求的那个线程**。载荷刻意只做 syscall、先自删、立刻返回，
  但仍不能保证。会话结束后相机停在 `096f`，**断电重启**才能再连（与本项目其他流程一致）。
- **`/tmp/jbusy` 存在时不要动手**。工具会先读它；存在就停止并提示，而不是强行清掉一个
  来源不明的标志文件（那是另一条加载路径的"忙碌"标记）。
- `libJit.so` 若已存在，工具**拒绝写入**（来源不明，可能是相机自己的）。
- **不写 `/etc`、不发 DEBUG、不 remount 只读分区、不刷固件、不动原始闪存区块。**
- 写入量：请求脚本 ≤1.8 KiB + 载荷 5.3 KiB，落在 25 MiB 的 `/tmp/jiritsu` 里；
  但注意该分区的占用是**累积**的，本工具不删除文件（见下一条），长期使用要留意。
- **不用固件的按路径删除命令**：分派表里确实有 `case 0x12`（`cmd_file_delete2`），但它是
  外部导入符号、函数体不在本项目证据内，所以本工具不发送自己无法佐证的帧。清理改由载荷自己
  `unlink`（它已经是 root），见 §5.1。

## 7.1 为什么不用普通 exFAT 分区做交换通道

真机实测（2026-10-16，root shell 里抓的）：

```
/proc/filesystems:  ext2  squashfs  vfat  fuseblk  /  nodev fuse, fusectl  ← 无 exfat、无 ext4
mount -t exfat -o ro /dev/mmca1 /tmp/sdtest   → EXFAT_FAIL
mount -t vfat  -o ro /dev/mmca1 /tmp/sdtest   → VFAT_FAIL
```

`sys_mount` 也是同一个 `mount(2)`，内核按 fstype 查注册表分派；表里没有 exfat handler，
busybox `mount`、`sys_mount`、厂商的 `Fsys_Mount` 都一样失败。相机能读写 exFAT 卡是因为
**PrFILE2 在用户态直接对块设备做原始 I/O，不经过 mount**（`/sbin/initrc/fstab` 里也没有
任何 SD 卡条目）。内核虽有 `fuse`/`fuseblk`，但相机上没有 exfat-fuse 那个用户态程序。

所以：shell 路线要读写卡，只能走 ext2 分区（`/dev/mmca2`）；想写 exFAT 分区得回到主机端的
固件 SD 文件 API（`cmd 0x0f`/`0x10`，mode `0x01`），那条
[`../2026-10-16-system-dump/`](../2026-10-16-system-dump/) 已经验证过三方 SHA-256 一致。

## 8. 参考

- 本项目：`analysis/2026-10-15_逆向-A7C2服务预加载-report.md`（当初已注意到这条 `dlopen`，
  但判定为"不适合作为无副作用写入测试"而搁置）、
  [`../2026-10-16-system-dump/`](../2026-10-16-system-dump/)（转储、`/tmp` 分区空间教训）。
- 交叉核对：各项断言的第二条独立来源是只读的
  `analysis/qemu-runtime/analysis/menu-root/usr/lib/libJiritsu.so`（其 SHA-256 与
  `analysis/evidence/jiritsu-static/sha256.txt` 一致）的反汇编，以及 `dynsyms.txt`
  全表（用于"不降权"这一项：库内没有任何 `setuid/setgid/seteuid`）。
- 外部（入口这条线的来源，见仓库顶层 [`参考项目与致谢`](../../README.md#参考项目与致谢)）：
  - [QAQLeonard/a7c2-fl23-research](https://github.com/QAQLeonard/a7c2-fl23-research)
    提交 `dd01c07`：其真机结果表明"写 `/tmp/jiritsu/libJit.so` 会让固件自己 `dlopen`"值得验证。
    本目录**只**借用"入口"这一件事；其补丁字节、色彩单元与安装流程与本成果无关，也没有代码派生关系。
  - [YuuLuo/a7c2-service-preload](https://github.com/YuuLuo/a7c2-service-preload)：A7C II 2.01 的
    服务认证链（`0e8a` → Senser → `0336` → 第二次认证）与机内执行思路。本目录的认证流程与该成果同源，
    但载荷、请求构造与结果回传是本项目自己的。
  - [ma1co/Sony-PMCA-RE](https://github.com/ma1co/Sony-PMCA-RE)（MIT，经 `adamwuqwq` 提交 `3824c398c0fa`）：
    主机侧 USB/Senser 传输与 `SonySenserCamera` 的 Backup 接口。本目录的帧实现复用同仓库
    `../2026-10-16-system-dump/scripts/jiritsu_version_read.py`，其底层即这份 vendor。

## 9. 真机运行记录

> 原始记录在本地 `evidence/`（被 `.gitignore` 排除，故这些路径在 GitHub 上不存在）。
> 下面每次运行都把关键判据逐条抄进正文，读者不需要那些文件也能复核结论。

### 2026-10-18 · run1 · 一台 ILCE-7CM2 2.01

| 项 | 值 |
|---|---|
| 载荷 | `libjit_exec.so` 5,328 B，SHA-256 `395569edb00cb91d2d060064fcde9f6645e0dcf6804bbdd270e57299d66295c8` |
| 命令 | `uname -a; id`（只读，不改相机任何文件） |
| 内核 | `Linux (none) 4.9.337-scel+ #1 SMP PREEMPT Sat Oct 11 04:09:59 JST 2025 aarch64 GNU/Linux` |
| `_init` 身份 | `pid=532 euid=0 status=0` |
| 完成行 | `A7JIT001 8b8ee600 127`（nonce 匹配；`id` 不存在 → 127） |
| 输出 | `uname -a` 一行 + `sh: id: not found` |
| 写入→模块运行延迟 | 0.28 s（内联 dlopen） |
| 运行后 `libJit.so` | 不存在（载荷自删） |
| `/tmp/jmode` | 运行前后都不存在 |
| 会话结束 | 相机停在 `054c:096f`，**断电重启**后才能再连 |
| 证据 | [`evidence/2026-10-18-run1/`](evidence/2026-10-18-run1/)（`start.bin` / `status.bin` / `output.bin` / `run.json`） |

本次用 `--keep`，所以相机侧 4 个临时文件没有截断（`request` 73 B、`start` 36 B、`status` 22 B、
`out` 107 B，合计 238 B，落在 nflasha6）。下一次运行会先把它们覆写掉。

过程中的两个坑，都已修在工具里：
- 桌面会自动把相机两个卷挂载起来，服务会话必须先卸载（工具的 sysfs 祖先匹配会拦住，
  不会去动别的磁盘）。按 `mountinfo` 里的字符串找相机是错的，那里没有 USB VID/PID。
- `DiagnosingBackend` 里引用了未定义的 `timeout`；`__enter__` 只在真机上才会执行到，
  所以离线测试和 `--dry-run` 都没暴露它。

### 2026-10-18 · run2 · 同一台机身（同一 payload）

命令：`cat /proc/$$/status | grep -E "^(Uid|Gid):"; grep jiritsu /proc/mounts; ls -l /tmp/jiritsu`
（`exit_status = 0`，输出 620 B，完整见 [`evidence/2026-10-18-run2/output.bin`](evidence/2026-10-18-run2/output.bin)）

```
Uid:	0	0	0	0
Gid:	0	0	0	0
/dev/nflasha6 /tmp/jiritsu vfat rw,sync,dirsync,noatime,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,avoid_dlink,errors=remount-ro 0 0
-rwxr-xr-x  1 root 0  183 ... a7c2-exec.out
-rwxr-xr-x  1 root 0  151 ... a7c2-exec.request
-rwxr-xr-x  1 root 0   36 ... a7c2-exec.start
-rwxr-xr-x  1 root 0   17 ... a7c2-exec.status
-rwxr-xr-x  1 root 0  70912 ... a7c2-payload.so      ← 旧 preload 方案留下的载荷
-rwxr-xr-x  1 root 0     25 ... mediainfo.txt        ← 相机自己的文件
```

三点结论：

1. **`Uid: 0 0 0 0`** —— 按本项目判据（只看 `/proc/$$/status`，不看提示符、不信自报）确认是 root，
   不只是"未降权"。
2. **`vfat` 挂载实际是 `rw,sync,dirsync,noatime`，没有 `noexec`** —— 这就是 `dlopen` 能成功的原因，
   与静态分析里 `mount` flags 低 32 位 `0x490`（`MS_SYNCHRONOUS|MS_DIRSYNC|MS_NOATIME`）一致。
3. 工作区里还躺着旧 preload 方案留下的 **`a7c2-payload.so`（70,912 B）**。它是我们自己的文件，
   按本项目"用覆写即截断回收空间"的做法，下次会话可以把它压到 1 字节收回 71 KB ——
   固件对这个分区的预算检查是 25 MiB 全目录求和，值得清掉。`mediainfo.txt` 是相机自己的，不要动。

### 2026-10-18 · run3 · 从 root shell 挂 SD 卡

命令（`exit_status = 1` 是最后那条 `grep mmca /proc/mounts` 没匹配到内容，即卸载成功 —— 不是失败）：

```
ls -l /dev/mmc*; mkdir -p /tmp/sd; mount -t ext2 /dev/mmca2 /tmp/sd; echo MOUNT_RC=$?
grep mmca /proc/mounts
echo A7C2-LIBJIT-SDTEST > /tmp/sd/libjit-sdtest.txt; echo WRITE_RC=$?
cat /tmp/sd/libjit-sdtest.txt; ls -l /tmp/sd/libjit-sdtest.txt
umount /tmp/sd; echo UMOUNT_RC=$?; grep mmca /proc/mounts
```

```
brw-------  1 root 0  250, 12 ... /dev/mmca2          ← 设备节点在（第二分区）
MOUNT_RC=0
/dev/mmca2 /tmp/sd ext2 rw,relatime,errors=continue 0 0
WRITE_RC=0
A7C2-LIBJIT-SDTEST
-rw-r--r--  1 root 0   19 ... /tmp/sd/libjit-sdtest.txt
UMOUNT_RC=0
```

结论：**这条入口可以直接挂卡、写卡、读回、卸载**，不需要 preload 那条链路，也不需要
固件自己的 SD 挂载命令（`cmd 0x02`）。挂载参数是内核 ext2 默认的 `rw,relatime,errors=continue`。

待补：跨通道确认（把相机切回大量存储后，在宿主机挂 `/dev/sde2` 读同一个文件），
这是本项目对"写入"的完整判据（读回一致 + 跨通道一致）。相机侧读回已一致，跨通道待做。

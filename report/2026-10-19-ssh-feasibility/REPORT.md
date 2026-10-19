# A7C II 2.01 开启 SSH：可行性调研

日期：2026-10-19。范围见 [scope.md](scope.md)。本报告采用普通技术调研结构，`flavor = null`。

本成果的文档导航见 [README.md](README.md)。共享库损坏时的网络容错证据已合并为
[SO-CORRUPTION-WIFI.md](SO-CORRUPTION-WIFI.md)；该附篇仅分析故障边界，不能据此保证 SSH 可用于救援。

## 1. 结论

**原厂固件已经包含 SSH 服务；在已授权的本机研究环境中，交互式 SSH 终端已经实测打通（root）。**
索尼同机型官方帮助明确说明，访问认证使用 SSH，用于遥控拍摄及图像传输的加密通信。
原始整机 dump 中存在 `/usr/sbin/sshd`、`ssh-keygen`、PAM 和 Sony 自定义 NSS 组件。
本项目已有的 libJit 入口提供 root 命令执行，本轮据此在**端口 2222** 上启动了第二个 sshd 实例并完成密钥登录。

需要区分三件事：原厂 22 端口服务**仍然**只做受限转发（`MaxSessions 0`、`PermitRootLogin no`、
无公钥/密码认证），本轮的登录能力来自我们自己的实例，而不是原厂配置被放宽。
另外，`root` 账户的登录 shell 原为 `/bin/false`，本轮把它改成 `/bin/sh` 才能得到交互 shell ——
这是本次持久化账户修改，部署产生的其他文件与目录见 §8 及 [README.md](README.md)。

关键限制是原厂配置的 **`MaxSessions 0`**：按 OpenSSH 语义，它禁止 shell、命令执行和 SFTP 等子系统会话，同时允许转发。
配置还将转发目标限定为两个本机端口，禁止 root 登录和公钥认证。
因此，“有 SSH 服务”“能通过原厂访问认证”“能取得交互终端”是三个需要分别核实的结果。

离线调研后，用户提供相机 Wi-Fi 地址并授权测试。本轮追加的单次检查结果是：**ping 成功，TCP 22 返回 `Connection refused`，未取得 SSH banner**。
说明该地址当前可达，但 22 端口未接受连接；无法仅凭这一结果区分没有监听与主动拒绝规则。
上述初期检查没有登录、启动服务或修改相机；后续授权部署与登录实测另见 §8。

## 2. 已核实的基础

| 项目 | 结果 | 证据等级 |
|---|---|---|
| 设备及软件 | ILCE-7CM2，固件 2.01；历史实测内核为 AArch64 Linux `4.9.337-scel+` | 既有真机记录 E1 |
| 管理权限前提 | 历史记录有 `/proc/$$/status` 的 `Uid/Gid: 0 0 0 0` | 既有真机记录 E1；本轮未重测 |
| SSH 服务程序 | `/usr/sbin/sshd`，767,408 B，AArch64 PIE，带调试信息；含版本字符串 `OpenSSH_7.9p1` | 本轮原镜像/ELF 检查 E2 |
| 程序来源一致性 | 从原始 `/usr` 镜像读出的 sshd 与已有离线参照文件 SHA-256 一致 | 本轮跨来源检查 E2 |
| 配置来源一致性 | `sshd_config`、`pam.d/ssh`、`nsswitch.conf` 在原始 A10 镜像、其离线提取目录及官方包三者一致 | 本轮只读 FAT 检查 E3 |
| 原厂访问认证 | 官方帮助提供访问认证开关及用户名、密码管理，明确提到 SSH | 官方文档 E6 |

版本字符串不证明 Sony 未修改 OpenSSH，也不证明二进制具有上游版本的全部行为。

## 3. 原厂 SSH 的用途与依赖

### 3.1 默认配置体现的限制

下列行号以官方包内 `0800_appli/tmp/ssh/sshd_config` 为准；已确认与转储镜像内配置一致。

| 配置 | 行 | 可支持的判断 |
|---|---|---|
| `Port 22`、`AddressFamily inet` | 5、26 | 默认配置指定 IPv4 TCP 22；不能据此认定真机正在监听 |
| `HostKey /tmp_network/ssh/ssh_host_ecdsa_key` | 14 | 依赖运行期主机密钥路径，静态分区中没有密钥不能证明服务不可用 |
| `PermitRootLogin no` | 29 | 原厂配置不允许 root 远程登录 |
| `PubkeyAuthentication no`、`PasswordAuthentication no` | 33、53 | 不能把它当作普通公钥/密码登录配置 |
| `UsePAM yes` | 88 | 认证还涉及 PAM 栈 |
| `PermitOpen localhost:15740 localhost:60152` | 97 | 转发目标受到限制，符合原厂应用通信用途 |
| `AllowTcpForwarding yes`、`MaxSessions 0` | 100、104 | 允许转发，但按上游语义禁止 shell、exec 和子系统会话 |
| SFTP `Subsystem` 行被注释 | 78 | 当前配置没有启用该外部 SFTP 子系统 |

配置同时出现 `ChallengeResponseAuthentication yes`（50 行）和 `KbdInteractiveAuthentication no`（106 行）。
没有运行该厂商版本的配置解析器，不能仅凭这两行宣布“全部认证方式关闭”或断言原厂认证必然失败。
OpenSSH 对不同版本的选项关系及厂商改动须另行核实。

### 3.2 ELF 与账号依赖

ELF 的解释器为 `/lib/ld-linux-aarch64.so.1`；直接依赖：

```text
libpam.so.0  libcrypto.so.3  libdl.so.2  libutil.so.1
libz.so.1    libcrypt.so.1   libresolv.so.2  libc.so.6
```

原始镜像目录中可以找到这些库对应文件/链接；这尚不等于完成递归加载与运行自检。
导入表包含 `socket/bind/listen/accept`、`pam_start/pam_authenticate/pam_acct_mgmt`、
`getpwnam/getspnam`、`setuid/setgid/initgroups`、`openpty`、`execve` 等锚点。
它们说明网络、认证、降权和终端功能确实位于程序中；是否被当前运行配置允许，仍由配置与运行行为决定。

账号集成也不是纯粹的通用 Linux 配置：

- `nsswitch.conf:6` 为 `passwd: files ssh`，账号查询会涉及厂商 `libnss_ssh.so.2`。
- 该库存在 NSS 导出符号及运行期账号文件路径字符串；本轮未读取或公开账号文件、密码、哈希或密钥。
- `pam.d/ssh:2–10` 使用 `pam_unix`、`pam_tally2`，并调用 `ssh_account_lock_recorder`、`ssh_session_monitoring`。
- PAM 配置含失败计数与临时锁定条件。因此不得用反复猜密码来验证认证是否生效。
- 原始 `/usr` 镜像的 `/lib/openssh` 目录查询返回不存在；仅能说明那个目录缺失，不能证明其他位置不存在 SFTP 实现。

索尼官方提供的是应用访问认证凭据管理。没有证据表明菜单账号就是 Linux root，或默认允许交互 shell。

### 3.3 网络与原厂服务生命周期

并行调查返回后，主会话复核了以下反编译上下文（E9）：

- SSH Manager 的装配先查询功能能力 `0x305`，再装载模块、注册回调；销毁时注销回调并卸载。当前真机能力查询值未知。
- `sequence_start_pc_remote_wifi_direct::run()` 与 `sequence_start_pc_remote_usb_tethering::run()` 都在参数条件成立时，经包装层调用 `sshd_start`。
- `sequence_stop_smartphone_pc_remote::run()` 在条件成立时调用 `sshd_stop`。这证明存在受遥控功能生命周期控制的启停路径，不证明所有模式都使用相同条件。
- 启停包装层在能力标志未开启时直接返回成功值。因此，单独看到启停返回 0 也不能证明已经监听；仍需实际 TCP/SSH 握手。
- 原厂 `run_supplicant.sh:10–12,40` 明确使用 `wlan0/p2p0`、`nl80211`，分别加载 BSS 与 P2P 配置。这是无线网络角色的静态证据，不证明两个接口同时可用。

据此可以将“Wi-Fi 已连通”与“原厂 SSH 服务已启动”明确分开。具体参数开关的来源、
当前接入点模式下的分支、服务模式与正常拍摄模式共存，以及休眠/切换时监听是否消失，仍需实际记录。
USB tethering 遥控也不能等同于电脑当前看到的大量存储、PTP 或服务 USB 接口。

## 4. 候选方向

| 方向 | 可行性判断 | 当前缺口 |
|---|---|---|
| 使用官方访问认证与遥控通信 | 有明确官方支持，优先用于确认原厂 SSH 的正常生命周期 | 本轮未做本机握手与应用连接验证 |
| 在已取得、经授权的本机管理环境中评估原厂 sshd 的独立实验实例 | 有二进制和匹配用户态基础；值得在隔离仿真副本中先验证 | 配置有效性、合法认证、账号/PTY、进程管理及网络共存尚未验收 |
| 独立构建受维护的轻量 SSH 服务用于隔离实验 | 技术上可作后备比较方向，目前无必要先增加一个二进制 | ABI、内核兼容、熵源、认证、体积与生命周期均未验证 |
| 开机常驻远程终端 | 本轮不推进 | 一次可用不代表能安全持久运行，增加启动与恢复风险 |

**建议先按官方流程核实 PC 遥控与访问认证状态，再在隔离副本验证普通、明确授权的认证会话。**
同机型官方流程（E7）将 Wi-Fi 连接与 PC Remote 开关分为两步：同一接入点连通后，还需进入
`MENU → 网络 → 连接/PC遥控 → PC遥控功能 → PC遥控 → 开`，并保留访问认证为开启。
官方还要求先断开与智能手机的连接，再由 Imaging Edge Desktop (Remote) 使用相机显示的认证信息连接。
这是正常产品功能操作，不承诺打开后必然获得 22 端口或交互终端；本轮没有代替用户改变这些设置。

当前拒绝连接与“仅开 Wi-Fi 尚未启动相关应用服务”的解释相容，但还不能据此锁定原因。

## 5. 验证与恢复约束

| 验收项 | 当前状态 | 通过判据 |
|---|---|---|
| 二进制及基础依赖存在 | 静态已核实 | 原始镜像内文件、ELF 依赖及摘要 |
| 配置可以被目标版本接受 | 已核实 | `sshd -t` 返回 0（§8） |
| 当前用户指定 Wi-Fi 地址 | 单次 ICMP 成功，TCP 22 拒绝连接 | 后续在明确产品模式下记录 TCP/SSH 握手；当前没有 banner 或指纹 |
| 认证合法且成功 | **已核实** | 仅密钥认证通过，无密码 |
| 交互终端 | **已核实** | PTY 会话 `PWD=/root`，`Uid/Gid: 0 0 0 0` |
| 文件传输 | 未测 | 单独验证子系统/协议，传输后跨端摘要一致 |
| 正常拍摄与网络共存 | 未测 | 正常模式下连通、按键及拍摄不受影响，记录退出和休眠行为 |
| 停止与恢复 | 未测 | 实验进程退出、监听消失、原配置摘要不变或已还原、临时文件明确清点 |

历史 USB 服务会话与正常拍摄模式不是同一状态。已有报告中的 `054c:0e8a/0336/096f` 说明 USB 身份切换，
不证明存在 USB IP 链路；历史启动的子进程是否能跨模式切换存活也未被 SSH 实验确认。
恢复需要断电时，内存中的实验进程会消失，不能把“入口执行过”当作“服务在正常模式仍运行”。

存储继续遵守项目规则：批量二进制、日志与实验数据放 SD 的 ext2 分区；
`/tmp`、`/etc`、`/var` 共用的 12 MiB 分区不能作为日志盘。
写前空间无法判定则不写，所有配置变更先备份、后读回核对摘要。
本轮没有执行任何上述设备写入。

## 6. Evidence → Finding → Path

### 6.1 Evidence

本地路径相对项目根目录；原始镜像与完整固件不随公开报告分发。

| ID | source_ref | 复核方式 | 内容摘要 / SHA-256 |
|---|---|---|---|
| E1 | [libJit 成果 §6/§9](../2026-10-18-libjit-exec/README.md) | 阅读验收矩阵与既有真机输出 | 历史实测；本轮未重跑，hash=n/a |
| E2 | `analysis/output/system-dump/a7c2-usr.img:/sbin/sshd` | 只读 `debugfs` 读出并比对离线参照文件；`readelf` 检查 | `a6b871ef0da3990ea0d25d7996ee79c3bfc29385d4eb1670892814c0e1472534` |
| E3a | `nflasha10.img:/ssh/sshd_config` | fwtool `readFat` 只读内容，并与官方包/既有提取结果比较 | 3,094 B；`fdccb272a96cc8b100116c8142b60b2bfd200390895770d2f263d07921db195d` |
| E3b | `nflasha10.img:/pam.d/ssh` | 同上 | 453 B；`ae2247cfced38a73782e6e1c863cdd235a3e1064e8035dc2181369bae9cbfcdd` |
| E3c | `nflasha10.img:/nsswitch.conf` | 同上 | 280 B；`3cb6f73826acc6786500cbac5f7a6a6ce11c807621eaa67d3f0dcebbe556f929` |
| E4 | `a7c2-usr.img:/lib/security`、`/lib/libnss_ssh.so.2`、`/bin/ssh-*` | 只读镜像目录检查；NSS 文件 strings/readelf | PAM/NSS/辅助程序存在；未运行，hash=n/a |
| E5 | [OpenBSD 6.5 sshd_config 手册](https://man.openbsd.org/OpenBSD-6.5/sshd_config.5) | 查阅 `MaxSessions` | 上游参数语义；厂商行为仍须验证，hash=n/a |
| E6 | [Sony ILCE-7CM2：Access Authen. Settings](https://helpguide.sony.net/ilc/2360/v1/en/contents/221h_access_authentication_settings.html) | 查阅用途、SSH 说明、菜单凭据管理 | 官方同机型文档；在线手册可能随版本更新，hash=n/a |
| E7 | [Sony ILCE-7CM2：PC Remote Function](https://helpguide.sony.net/ilc/2360/v1/en/contents/0903_pc_remote_function.html) | 查阅接入点连接流程第 1–4 步、手机连接限制及 SSH 说明 | Wi-Fi 与 PC Remote 分别开启；hash=n/a |
| E8 | 本地 `evidence/network-check.json`（被 Git 忽略） | 对用户明确指定的地址做 1 次 TCP22 连接与 1 次 ICMP 检查；不应对未授权地址复跑 | TCP errno 111；ICMP 1/1，RTT 84.558 ms；hash=n/a |
| E9 | `analysis/appfw-decompile/corpus/parts/7/0x003480000.c:7329,7869`；`0x0034d0000.c:16224,16242`；`0x003590000.c:7243,7384,7454`；`0x0034b0000.c:4693`；`parts/6/0x002d40000.c:6353` | 只读核对 PC Remote 启停序列、包装层、能力检查及其调用上下文；无线接口来自原厂 `run_supplicant.sh:10–12,40` | 并行研究结果已由主会话复核关键调用；反编译签名可能有误，hash=n/a |

### 6.2 Findings

| ID | 严重度 | evidence_ids | 置信度 | location / status |
|---|---|---|---|---|
| F1 | n/a_re | E2、E3、E4、E6 | 高 | 原厂用户态中已有 SSH 服务；静态核实 |
| F2 | n/a_re | E3a、E5 | 高（配置及上游语义） | 默认配置不开放交互 session；尚无目标版本动态验收 |
| F3 | n/a_re | E3b、E3c、E4、E6 | 高（组件存在） | 认证涉及 Sony NSS/PAM 和应用凭据；端到端行为待测 |
| F4 | n/a_re | E1、E2 | 中 | 具备进一步做终端实验的基础；服务启动、网络与登录尚未证明 |
| F5 | n/a_re | E8 | 高（当次观察） | 当前地址可达，TCP22 拒绝连接；不等于设备没有 SSH 能力 |
| F6 | n/a_re | E6、E7 | 高（官方流程） | Wi-Fi 开启不能替代 PC Remote 及访问认证设置；实际监听生命周期仍待测 |
| F7 | n/a_re | E9 | 高（静态调用关系） | 存在 PC Remote 条件启停 SSH 的调用链；当前模式条件和实际服务状态未闭合 |

P1，`path_type=solve`：核对历史权限前提（E1）→ 确认原始服务程序（E2）→
核对默认限制与认证组件（E3/E4）→ 与官方说明交叉验证（E5/E6）→
分开验收配置、网络、认证、session 和恢复。此路径是调查与验收顺序，不是利用执行链。

### 6.3 本轮时间线

1. 2026-10-19：读取最终仓库约定与既有真机成果，保留仓库已有未提交修改。
2. 同日：发现原厂 sshd 与配置；从原始 dump 核对服务程序摘要。
3. 同日：只读解析 A10 镜像，确认三个 SSH 相关配置与官方包、既有提取结果一致。
4. 同日：核实 ELF 解释器、依赖与导入锚点，交叉查阅 Sony 与 OpenSSH 官方说明。
5. 同日：用户提供 Wi-Fi 地址并授权测试；单次 ICMP 成功，单次 TCP22 拒绝连接，未继续握手或登录。
6. 同日：并行网络调查完成；主会话复核条件启停、能力检查及包装层无操作成功分支，补入 §3.3。

## 7. 离线复核入口

以下命令在项目根目录执行，不连接设备、不启动固件、不写原镜像：

```sh
debugfs -R 'stat /sbin/sshd' analysis/output/system-dump/a7c2-usr.img
debugfs -R 'cat /sbin/sshd' analysis/output/system-dump/a7c2-usr.img 2>/dev/null | sha256sum
readelf -l analysis/qemu-runtime/analysis/menu-root/usr/sbin/sshd
readelf -d analysis/qemu-runtime/analysis/menu-root/usr/sbin/sshd
readelf --dyn-syms -W analysis/qemu-runtime/analysis/menu-root/usr/sbin/sshd
sha256sum analysis/extracted/firmware/0800_appli/tmp/ssh/sshd_config
```

原始 FAT 配置比对使用项目已有的 fwtool 读取器，打开方式为 `rb`，没有安装新依赖或挂载文件系统。

## 8. 实测结果与持久写入记录

### 8.1 已完成

在设备所有者授权下，用 `final` 中已验证的 libJit 入口在相机上启动了一个**独立**的 sshd 实例：

- 配置位于 tmpfs `/tmp_network/a7c2ssh`（`Port 2222`、`ListenAddress 0.0.0.0`、`PubkeyAuthentication yes`、
  `PasswordAuthentication no`、`UsePAM no`、`StrictModes no`），**没有修改** `/etc/ssh/sshd_config`，
  **没有**触碰原厂 22 端口服务；实测原厂 22 端口仍无监听。
- `sshd -t` 返回 0；`/proc/net/tcp` 出现 `00000000:08AE`（0.0.0.0:2222）LISTEN。
- 主机侧用本项目生成的密钥完成登录，回读判据：

```
SHELL_OK
Uid:	0	0	0	0
Gid:	0	0	0	0
Linux (none) 4.9.337-scel+ #1 SMP PREEMPT Sat Oct 11 04:09:59 JST 2025 aarch64 GNU/Linux
32014_05.2025101101
```

PTY 会话另测：`PWD=/root`、`ls /tmp_network/a7c2ssh` 列出 `authorized_keys host_ecdsa host_ecdsa.pub sshd_config sshd.pid`。

### 8.2 为什么需要改 `/etc/passwd`

原厂把非登录账户写成了不可登录的 shell：

```
root:*:0:0:root:/root:/bin/false
sshd:*:77:77:sshd:/var/run/sshd:/bin/false
```

只启动 sshd 是不够的：认证成功、`exec request accepted` 都会出现，但会话立刻以退出码 1 结束且无输出，
因为 sshd 要用该账户的 shell 执行命令。这就是原厂"认证成功但没有 shell"的真实机制，
与 `MaxSessions 0` 是同一套设计。

### 8.3 持久化改动与还原

账户修改是 `/etc/passwd` 中 `root` 行的 shell 字段
（`/etc` 位于 12 MiB 的 nflasha10）。此外部署曾写入 `/tmp/jiritsu` 下的载荷、邮箱、
备份和首次密钥尝试残留，并创建 `/var/empty`；不能把这些统称为 RAM 内容或无持久写入。
以下记录的是账户文件改动前后的 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `/etc/passwd`（改后，相机上实测） | `a4137642bd3a2d83f4b7666341aaa2cfcf453cd3ef255e6d7f13cba5e4d779aa` |
| 备份 `/tmp/jiritsu/passwd.orig`（nflasha6，124 B） | `4a691edcd492167bb3d538e4e15c24dd5fbc45785afcf2beddb3fbd39e097398` |
| 主机副本 `evidence/passwd.orig` | 同上（已核对一致） |

除 `root` 行的最后一个字段外，两份文件内容相同。

**是否需要还原**：保留不影响相机正常拍摄与启动（root 的登录 shell 只在 `login`/`su`/sshd 登录时使用），
且 `root` 口令为 `*`（锁定）、原厂 22 端口仍拒绝会话，因此不构成密码登录入口。
但这是**对原厂系统的持久修改**，按项目纪律应显式记录：

- 需要它：只要还想要 SSH 交互 shell，就必须保留。
- 想回到原厂状态：

```sh
# 在相机上（root shell 或经 libJit 入口）
cp /tmp/jiritsu/passwd.orig /etc/passwd
/usr/bin/openssl dgst -sha256 /etc/passwd   # 应等于 4a691edc...
```

注意：还原后 SSH 交互 shell 立即失效（认证仍会通过，但会话以退出码 1 结束）。
建议在刷写固件或交付前还原，并保留主机侧 `evidence/passwd.orig`。

### 8.4 复现

`ssh-camera.sh start` / `ssh-camera.sh stop` 封装了完整流程（卸载相机卷 → libJit 入口 →
启动/停止 2222 实例 → 登录探测）。每次调用消耗一次 USB 会话，**调用前需拔插 USB**
（会话结束后相机停在 `054c:096f`）。

脚本不内置任何主机路径、相机地址或密钥，全部由调用方提供：

| 变量 | 作用 | 默认 |
|---|---|---|
| `A7C2_HOST` | 相机在局域网里的地址（必填） | 无 |
| `A7C2_SSH_KEY` | 客户端私钥路径 | `~/.ssh/a7c2-camera` |
| `A7C2_PYTHON` | 带 `pyusb` + `Cryptodome` 的解释器 | 仓库内 `analysis/.venv/bin/python` |

仓库根目录、libJit 工具路径、相机卷的卸载目标都由脚本自己推导或按卷标匹配，
不写死具体机器。

```sh
A7C2_HOST=<相机地址> final/a7c2-re/report/2026-10-19-ssh-feasibility/ssh-camera.sh start
```

密钥对在首次运行时由脚本用 `ssh-keygen` 现场生成（ed25519），**默认落在 `~/.ssh/`，不进仓库**；
只有公钥会被写进相机的 `authorized_keys`。也可以事先自备密钥并用 `A7C2_SSH_KEY` 指定。
登录命令：

```sh
ssh -i ~/.ssh/a7c2-camera -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -p 2222 root@<相机地址>
```

sshd 与它的主机密钥都在 tmpfs，**重启即消失**；`/etc/passwd` 的改动则保留。
交互 shell 需要 `root` 的登录 shell 已经是 `/bin/sh`（§8.2/§8.3）；脚本本身不改 `passwd`。

### 8.5 仍未验收

拍摄模式与网络共存、休眠/模式切换时监听是否消失、SFTP 文件传输、
以及把这些改动纳入自动化前的最小权限设计，都还没有验收。

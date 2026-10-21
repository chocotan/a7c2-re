# A7C II 2.01：固件开发者/工程模式体系调查报告

日期：2026-10-21。范围与方法见 [README.md](README.md)。
证据强度分级：**代码证实**（反编译/脚本/分派表直接可见）、**字符串证实**（功能字符串存在但未闭合调用链）、**推断**（合理但未证实）。

## 1. 总结论

零售固件内置一整套分层的开发者/工程模式体系，缺省全部关闭：

| 层 | 机制 | 零售机状态 |
|---|---|---|
| 启动调试模式 | `/setting/mode/dmode` + `bootin.elf` | 未设置（默认 mode 3 正常启动） |
| 任意库注入 | `/setting/mode/preload` → `LD_PRELOAD` | 未设置 |
| 开发者标记 | `/setting/development.txt` 含 `DEV` → 解锁串口 console | 文件存在但**为空** |
| updater 工程模式 | `/setting/updater/mode*` 标志 + `up.sh` | 未设置 |
| senser 服务模式 | USB `054c:0336`，`sen.elf`+`libsencore.so` | 可用（本项目 dump 即经此取得），但高权限命令被分派表拒绝 |
| 串口 console | `kemco.txt` 内核参数 | `console=ttynull0`（关闭） |

**启用路径评估**：USB 服务模式（senser）**不能**用于开启开发者模式——读写 dmode 的
`0x601/0x8013 DBG_MODE` 命令虽有完整实现（终点确为 `change_mode.sh`），但 USB 分派器对带
`0x8000` 标记的命令一律拒绝（代码证实，§5）。可行路径是本项目已有的 root 入口
（[libJit](../2026-10-18-libjit-exec/)，已验证 `Uid: 0 0 0 0` 可写文件）直接写
`/setting`（nflasha2，vfat 可读写）下的标志文件，重启经 dmode 生效，不碰只读分区。

## 2. 启动调试模式（dmode）

机制链（全部代码证实）：`/sbin/init2` 读 `/setting/mode/dmode`（缺省 3）→
`/usr/bin/bootin.elf &`；`/usr/bin/change_mode.sh` 是唯一官方写入入口
（由 `libsencore.so` 内部调用，但 USB 不可达，见 §5）。

模式全集（`change_mode.sh` usage 文本 + bootin.elf 字符串）：

| 模式 | dmode 值 | 语义 | 零售机可用性 |
|---|---|---|---|
| 0 | `0` | 不装第二批驱动、不启动 bootin；仍有 insmod_drv1（devno/dma330/crypt-emmc/emmc）+ console getty shell | ✓（交互需 console，§3） |
| 1/2/3 | `1/2/3` | 阶段累进：Infra(`s g r u`)→ +LIRO(`l`，`liro.ko`)→ +并行 infra+appFw(`i a`)；3=默认 | ✓ |
| 4/5/6 | `1n/2n/3n` | ~~nounified driver~~ **`n` 无解析分支，与 1/2/3 完全相同**（bootin `0x402334` 只认 `P/L/W/G/S/D`，代码证实） | 等同无效 |
| 7 | `3L` | LeakTracer 内存泄漏追踪 | ✓ 库随固件发布；bootin 建 `/tmp/lt` tmpfs+fifo，设 `LT_CHECK_PROCESS=1` 等环境 |
| 8 | `3w` | 仅设 `MALLOC_OPTIONS=2W`，未找到读取者 | **存疑**（代码证实） |
| 9 | `3k` | ~~KMC ICE~~ **`k` 无解析分支，同 mode 3**（代码证实，死代码） | 无效 |
| a | `3g` | ~~target gdb~~ **被拒**：bootin `0x401ae0` 要求命令以 `/` 开头，`gdb --args …` 是相对命令，fork 前即 return -1；调用方不查返回值继续进消息循环 | ✗ 死路径 |
| b | `3s` | ~~gdbserver~~ **同样被拒**：拼出的 `gdbserver 0.0.0.0:<port>` 无绝对路径前缀，同一检查处拒绝；端口默认 **10000**，可由 Backup 项 `0x940002` 覆盖。**补上 gdbserver 二进制也不能修复此路径** | ✗ 死路径 |
| d | `3d` | ~~DUMA~~ 小写 `d` 无解析分支，同 mode 3 | 无效 |
| e/g | `3p` | efence / ASan | ✗ 库未发布（白名单中有路径但文件缺失） |
| f | `3p` | jemalloc | ✓（白名单路径，附 `MALLOC_CONF=narenas:1024,tcache:false`） |
| p | `3p` | ~~任意路径 LD_PRELOAD~~ **白名单制**：bootin `0x403f08` 仅认 4 条固定路径（LeakTracer/efence/jemalloc/asan），`strncmp` 匹配才 `setenv(LD_PRELOAD, 固定路径)`；**任意路径不会生效**（代码证实） | 仅限白名单 |
| 11/12 | `1D/3D` | ~~LIQUID wait/attach~~ `D` 标志位（`0x80`）置位后**无任何消费者**（代码证实，死代码）；且会把 console 改为 ttynull | 无效且有害 |

**对本项目价值排序**（按 bootin 反编译修正后）：
1. **mode 1/2**——分层截停启动（Infra / Infra+LIRO），appFw 不起的"活 Linux+全驱动"环境，
   直接服务 QEMU 原生运行项目的分层排障。这是 dmode 体系里唯一确认有效且高价值的部分。
2. **mode 7/f**——LeakTracer / jemalloc 经白名单 preload 生效，appFw 内存诊断
  （QEMU 里 appFw 稳定性调试的对口工具）。
3. mode 0——最小系统+console shell，内核层实验（低-中）。

**不再有"官方任意注入通道"**：preload 白名单只认 4 条固定路径，任意 .so 需放到白名单路径
（/usr 只读，不可行）或继续用已有的 libJit 入口——libJit 仍是注入首选。

tmode 语义（代码证实）：`/setting/mode/tmode` 开头两字节为 `TT` 且无更高优先级选择时
追加 `target=test`；target 自动选择顺序 Backup(`0x92001b`)=resub → qemu 内核 → TT=test → 默认；
test 目标需 `libTestDriver.so`（未随固件发布）。`killall` 只向 bootin 自身队列发退出消息，
**不杀 im/appFw**。

## 3. 开发者标记与串口 console

- `/setting/development.txt` 在本机**存在但为空**。`change_mode.sh` 检查其含 `DEV` 时，
  把 `/setting/kemco.txt` 的 `console=ttynull` 替换为 `console=ttyAMA0,460800n8`（代码证实）。
- 本机 `kemco.txt` 确认为 `console=ttynull0`（零售关闭串口输出）。
- **固件内没有发现任何创建/写入 `development.txt` 的接口**（代码证实：全树仅
  `change_mode.sh` 引用它，且只读）。写入只能靠 root 入口直接写文件。
- `up.sh` 末尾：`/setting/updater/debug` 标志存在时也会执行 console on
  （改 nflasha1 的 `boot/kemco.txt`，`console=ttynull`→`ttyAMA0,460800n8 amba2.console=1`）。
- 真机上 mode 0/1/2 的交互依赖 console 解锁；QEMU 中 console 可自由接，不受此限。

## 4. updater 工程能力（up.sh / sauu）

本机身份码（ud_datcnv.elf 编码已逆向：逆序逐字节、每半字节 +0x30 转字符，无密码学成分；
Python 离线复现并往返验证）：

| 文件 | 位置 | 明文 | 含义 |
|---|---|---|---|
| dat2 | nflasha1 根目录 | `20030014` | 机型码 |
| dat3 | nflasha1 根目录 | `00000000` | 区域码 |
| dat4 | nflasha2 `/updater/dat4` | `0201` | 版本 2.01 |

`up.sh` 各模式的真实语义（执行体为 updater 环境的 `sauu`/`execute_ufp.sh`，代码证实）：

| up.sh 参数 | 显示名 | 实际行为 |
|---|---|---|
| `user` | UFP mode | 标准 USB 更新（`sauu`），检查机型/区域/版本 |
| `serv` | Service mode | 实为 **Minor**：`sauu -m`，允许同版本重刷；仍校验机型/区域；要求 M 类包 |
| `ver` | Version Skip | `sauu -s`，跳过版本高低比较 → 可重装/降级；仍校验机型/区域；要求 O 类包 |
| `ms` / `file` | MS Crypt / File Crypt | 从 SD 卡加密包 / 内部 nflasha22 `firm.dat` 更新 |
| `modver` | — | 只改 updater 认定的版本号，不改运行代码 |
| `modmod`/`modreg` | — | 改**更新包匹配用**的机型/区域码（dat2/dat3） |

**否定结论（代码证实，补强 2026-10-11 签名链报告的本机证据）**：
三种模式共用写入路径 `UFPDecryptorModule::exec_write_firm@0x12f50`，解密前先
`VerifySignature`，`RSA_verify()==1` 失败抛 `0x30002`。**没有任何 updater 模式接受自制包。**

**区域码边界**：dat3 已闭合的消费路径只有更新包匹配比较（`libupdaterufp.so@0x1f34`）。
appFw 的语言可用性读 Backup 区（`backup_get_language_dst@0x35aa6bc`），NTSC/PAL 选择器也是
Backup 项；**改 dat3 解锁语言/制式没有调用链证据**，语言解锁应走 Backup 路线。

## 5. senser 服务模式命令面

架构（代码证实）：顶层 opcode 经 `Parse`/`CmdDecode` 分派；认证结果 3/2/1 对应三张权限表
（A 表 `10,20,40,FF00,FF02`；B 表 `40,10,20`；C 表仅 `40`）。二级目录在
`libsencore.so` VA `0x177F8`，共 1808 条记录、156 个 block（固件共用诊断目录，
不代表 A7C2 全部可用）。

关键命令实证：

| 命令 | 状态 |
|---|---|
| ProductInfo `0x10` | 可用；HASP 历史读取/重置；`0x00F1` 终端切换是**成功空操作**（`mov w0,#1; ret`） |
| FirmwareUpdate `0x20` | **空实现**（直接 return 0） |
| MiconAccess `0x30` | **空实现**（固定返回 `0x80`） |
| AdjustControl `0x40` | 主通道：调整数据/日志/GPIO/启动标记/子系统转发，受权限与拒绝标记约束 |
| `0x601/0x11,12,14` | 服务/工厂/测试**启动标记**读写（可用；不等于开发者模式） |
| `0x601/0x8013` DBG_MODE | 读写 dmode → `change_mode.sh` 的完整调用链存在，**但 USB 分派器拒绝**（`0xFED8`：`catalog_code & 0xE000) == 0x8000 → reject`） |
| `0x601/0x801F` TERM_MODE、`0x4E0/0x8002,8003` S_MEM_READ/WRITE、`0x4F0/0x8001` MEM_READ | 同批 274 条带拒绝标记，USB 不可达 |
| `0x601/0xFE,FF` | 内部 256 字节暂存区读写（offset+size ≤ 0x100），**不是**任意内存 API |
| `0x603/*` | 备份/调整数据 READ/WRITE/SAVE/PDT 等转发（PMCA 已在用） |
| `0x1001/*` | JIRITSU 子系统的模式/挂载/格式化/文件收发/删除/列表/摘要转发（路径约束与权限未闭合） |
| TestMode `0xFF00` | 转发到本机 FIFO（`/var/fifo/sen_host_req|res`，30s 超时）；接收端命令集未知；**无直接 shell 入口** |
| Sonar `0xFF02` | 加载 `libSnr.so:Sonar_Packet`；该插件**不在固件中**，缺失返回 `0x80` |
| FileControl `0xFF01` / MemoryDump `0xFF03` | PMCA 支持但 **A7C2 所有权限级分派表均无此项**（历史真机请求已认证仍返回 `0x80`，吻合） |

权限门控（字符串+符号证实）：HASP 授权文件 `/setting/.hasp`（`Read_Hasp`/`Regist_HaspId`/
`Reset_Hasp`，调整类命令校验，`adjcmd_HASP invalid file`）；RSA 挑战-应答
（`SEN_SSL_DecryptData_Using_RSA_PublicKey`+随机数/摘要）。本机 `/setting` 已有
`.hasp`、`.etools` 文件。插件机制 `libpro%02X.so` 按编号扩展命令（本机仅 `libpro00.so` =
HASP 读写/复位两条）。

**修正的历史认知**：PMCA"终端开启"在 A7C2 无等效效果；历史 rootterm 尝试收到 0 字节，
从未有过 shell。本项目 dump 的成功不依赖这些缺失命令。

辅助客户端（均为本机 OSAL/testcmd 客户端，非独立 USB 入口）：`rcvcmd.elf`（收消息）、
`sndcmd.elf`/`testcmd.elf`（发消息）、`adjstctl.elf`（按 block/code 调诊断目录）。

## 5.5 SSH 服务（原厂路径）

sshd 随固件发布且有多个业务启动入口（代码证实）：PC Remote 的 Wi-Fi Direct（`appFw.so@0x34896bc`）、
USB tethering（`0x3488d54`）、手机远控（`0x3492c24/0x3492f5c`）、通用 `sequence_start_sshd::run@0x34938e0`，
最终经 `libInfraSSHManager.so::startSshd@0x908c` 执行**无附加参数**的 `/usr/sbin/sshd`，
读 `/etc/ssh/sshd_config`。

但原厂配置把 SSH 限定为**远控转发隧道**（nflasha10 镜像代码证实，与
[2026-10-19-ssh-feasibility](../2026-10-19-ssh-feasibility/) 的真机实测完全一致）：

- `MaxSessions 0`：禁 shell/命令/SFTP，仅允许转发；`PermitOpen localhost:15740 localhost:60152` 限定转发目标；
- `PermitRootLogin no`、`PubkeyAuthentication no`、`PasswordAuthentication no`；
  challenge-response 开时二进制会强制 keyboard-interactive=1，走 PAM（`pam_tally2 deny=20 unlock_time=60`）；
- 动态账号由 `libnss_ssh.so.2` 从 `/tmp_network/ssh/ssh_name_password` 读取，
  **固定 UID 501 / GID 65534 / shell `/bin/false`**；密码为 SHA-256 crypt（`$5$rounds=5000$`），无固定默认密码；
- `/etc/passwd`：root 密码字段锁定（`*`）、shell `/bin/false`；
- `/tmp_network` 是 1M tmpfs（运行时凭据不持久）；host key 为 ECDSA-256，经 Backup 保存/恢复（非每次换钥）；
- `usbmuxd` 是 **Apple 设备 tethering** 组件（USB host 场景），不是电脑 SSH-over-USB 通道；
- `ssh_account_lock_recorder`（失败计数/20 次事件）与 `ssh_session_monitoring`（首会话开 KikiLog）为审计组件；
- parentchild 日志里的 `ssh_enable` 属于**出站 SSH 客户端**流程（`start_ssh_client_session`），不是入站开关。

**结论**：不存在“服务模式永久开启 SSH”的文件开关；即便完成访问认证，拿到的也只是受限转发账号。
交互式 root shell 的可行路径仍是已有实测方案：libJit root → 自建第二个 sshd 实例（端口 2222，
改 root shell），见 [2026-10-19-ssh-feasibility](../2026-10-19-ssh-feasibility/) §1–§8。

## 6. 随固件发布的工程工具

| 工具 | 用途 | 价值 |
|---|---|---|
| `/usr/scenario/` 26 个插件 + `scenario.elf` | 可编排的原生测试链：音频/麦克风/HDMI 输入输出/面板/EVF/录像启停/变焦/人脸/视频灯/工厂模式/Wi-Fi 测试。`CAMERA_SCN_MOVIE_REC_START.so::scenario_run` 证实能向相机队列发消息驱动固件；注意部分插件是 `return -1` 空壳 | **高**：QEMU 项目逐项验收显示/成像/媒体链的现成工具 |
| `adjstctl.elf` | 调整命令客户端（block/code+负载 → `testcmd_sndmsg`）；目录含 `AFADJ_FORMAT`、`ADJSTART/ADJEND`、快门检查、`EXEC_SYS_SET_ADJUSTMENT_VALUE` | **高**：调整数据读写入口；数据格式与写入落点待逐命令追踪 |
| `app_monio.elf` | 应用内部命令与观测（DMM 缓冲+OSAL 消息） | **高** |
| `mwf_monio.elf` | 中间件状态 dump/历史/轮询，支持 scenario 文件 | **高** |
| `/usr/sbin/` sshd、tcpdump、dhcpcd；`/usr/bin/` iperf、wpa_cli、wl | 网络诊断集 | 中（sshd 实测见 [2026-10-19-ssh-feasibility](../2026-10-19-ssh-feasibility/)） |
| `myftm`、`btRFTest.elf`、`Wltestcmd` | WLAN/BT 射频工厂测试 | 低（与拍摄仿真无关） |
| `NsBkSet.elf`、`GPIO2SDIO.elf`、`SDIO2GPIO.elf`、`SetPort.elf` | 板级预置/引脚切换（有覆盖副作用） | 低 |

## 7. 冷门模式与调整入口

- **KMC ICE(9)/LIQUID(11/12)**：KMC=京都微电脑（PARTNER JTAG、LIQUID IDE，厂商官方资料证实
  归属），但零售固件中无解析分支/无消费者——**死代码，不要投入**。
- **Wfree(8)**：仅设 `MALLOC_OPTIONS=2W`，无读取者，有效性存疑。
- **amode=adj**：bootin 把它转换为 `app_argv=adj`，im 在 `0x40515c` 将其值写入 `0x419668` 后
  **全固件无任何读取者**（代码证实，死参数）；**不是**校准界面入口。
  真正的调整检查 UI 在 `AdjChkConfig.so`（独立 `main_loop`，初始化输入/触摸/显示/sensor 通信，
  含 AF 调整命令与 `Backup_reset_subsystem(0x2cf,2)` 持久数据重置能力），由 IMDB 的 `0x4000`
  标志（im.elf 的 `cho` 参数）选择加载——这是进工厂调整界面的候选路径，未闭合。
- **usbj**：USB 充电启动；独立 `usbj` 参数（非 amode）选择 IMDB 的 USBJ 目标。

## 8. 其他发现

- `/setting/sen/smode` 内容为 `UU` 时 init2 启动 `sen.elf`（独立服务启动模式，
  与正常模式同一接口框架不同端口），随后仍启动 bootin.elf（代码证实）。
- 固件字符串遗留索尼开发基础设施 URL：
  `https://kanga.ps-depot-developer.com/FSErFHP8Je0xINMFVv9P/api/firmwarelist_api.php`。
- `up.sh modmod/modreg` 改写的是 nflasha1 的 dat2/dat3（经 `ud_datcnv.elf` 编码），
  配套调试入口 `0xdeb0` 存在于 USB 更新命令分发器，但非任意命令 shell。

## 9. 对本项目路线图的直接价值

1. **QEMU 原生运行**：mode 1/2 分层截停排障；scenario 插件做显示/成像/媒体链逐项验收；
   mode 7/f 诊断 appFw 内存问题；console 在 QEMU 自由可用。
2. **真机研究**：libJit root → 写 `/setting/mode/dmode` 可截停启动层；preload 注入仍走 libJit
  （bootin 的 preload 是 4 路径白名单，不是任意注入通道）；
   `development.txt` 写 `DEV` + change_mode.sh 恢复串口 console（仅当真机需要串口时）。
3. **语言/区域解锁**：放弃 dat3 路线，走 Backup 区（PMCA LanguageTweak 同路线）。
4. **固件修改**：updater 各模式均不绕签名，自制包路线仍是签名链问题，与工程模式无关。

## 10. 未决问题

- ~~bootin 逐模式进程序列、gdbserver 路径端口~~（已闭合：阶段 `s→g r u→+l→+i a`；
  gdb/gdbserver 因相对路径在 `0x401ae0` 被拒，端口默认 10000/Backup `0x940002`）；
- ~~sshd 原厂触发链~~（已闭合，§5.5：四个业务入口 + 转发限定配置；无服务模式常开开关）；
- `2W` 是否被当前分配器实际消费、LeakTracer FIFO 真机可用性；
- TestMode FIFO（`/var/fifo/sen_host_req`）接收端的子命令集；
- JIRITSU `0x1001` 转发的路径约束与有效权限；
- HASP 文件格式与 RSA 认证细节（高权限 senser 命令的前提）；
- `AdjChkConfig.so` 的完整进入条件（独立参数 `cho` → im 标志 `0x4000` → IMDB adjust 位 `0x80`
  → `Application_System_Init("AdjChkConfig.so")`，链已定位、真机可达性未验证）与校准写入落点；
- 区域码 dat3 的枚举表与销售区域映射。

## 附：主要证据锚点

- `scratch/devmode/rootfs/sbin/init2`：dmode/smode 读取与 bootin 启动（rootfs 全树提取自
  `analysis/output/system-dump/a7c2-rootfs.img`，原始镜像只读未动）；
- `scratch/devmode/usr/bin/change_mode.sh`、`up.sh`：模式定义原文；
- `scratch/devmode/usr/bin/bootin.elf`：模式解析 `0x402334`（仅 `P/L/W/G/S/D` 有分支）、
  阶段展开 `0x402494`（`s→+g r u→+l→+i a`）、启动命令拼接 `0x402b90`、
  相对路径拒绝 `0x401ae0`（gdb/gdbserver 死路径）、gdbserver 端口 `0x40205c`
  （默认 10000，Backup `0x940002` 可覆盖）、preload 白名单 `0x403f08`（4 条固定路径）、
  tmode 检查 `0x402b24`（开头 `TT`）、killall `0x4029e0`（仅退 bootin 自身）；
- `scratch/devmode/usr/lib/libsencore.so`（SHA-256
  `3362d4b6e03dc517b04f1eb04d4199ff16d33f6e8f1bfddce657a375778d79e7`）：
  顶层分派表 `0x6D848/0x6D8B8/0x6D8F8`、拒绝检查 `0xFED8`、二级目录 `0x177F8`；
- `libupdaterufp.so`/`sauu`/`libupdaterapi.so`：模式选择、签名验证
  `exec_write_firm@0x12f50`/`0x1361c`；与 nflasha1 `boot/initrd.img` 内 updater 比对一致；
- nflasha1/nflasha2 以 `ro,loop,nosuid,nodev,noexec` 挂载读取后卸载，前后 SHA-256 一致：
  nflasha1 `0937e8cc…948d7c`，nflasha2 `5f73837a…739a7254`；
- dat 编解码：ud_datcnv.elf `0x400cc4–0x400d70`，Python 离线复现并往返验证。

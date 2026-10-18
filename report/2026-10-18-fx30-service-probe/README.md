# 跨机型验证：Senser / Jiritsu 链能不能用在第二台机器上（2026-10-18）

本项目的全部真机工具（[整机转储](../2026-10-16-system-dump/)、
[libJit 执行入口](../2026-10-18-libjit-exec/)）都建筑在一条链上：

```
大量存储 054c:0e8a → Senser 认证 → 服务身份 054c:0336 → 第二次认证 → Jiritsu 命令
```

换一台机器还能不能走，**不能靠推断** —— 型号不同、固件不同，路径和命令集都可能不一样。
这个目录只回答一个问题：**这条链是机型专属的，还是协议级的？**

结论：**是协议级的**。已在 **ILME-FX30**（固件 `31182_15.2025122501`）上完整验证，
连同一个 libJit 载荷都直接可用。

| 项 | ILCE-7CM2（基线） | ILME-FX30（本次） |
|---|---|---|
| 大量存储 PID | `054c:0e8a` | **`054c:0e0e`** ← 机型级，不同 |
| 接口 | class 08/06/50（usb-storage） | 同 |
| Senser 认证 | ✅ | ✅ |
| 认证后身份 | `054c:0336` | **`054c:0336`** ← 协议级，相同 |
| 第二次认证 / `readHasp()` | ✅ / 88 B | ✅ / 88 B |
| Jiritsu 帧 + `/tmp/jiritsu` work 区 | ✅ | ✅ 完全相同 |
| `/version.txt` | `2.01` | **`31182_15.2025122501`** |
| 内核 | `4.9.337-scel+`（2025-10-11 构建） | `4.9.337-scel+`（**2025-12-25** 构建） |
| libJit 入口 `euid` | `0` | **`0`** |
| 会话结束状态 | 停在 `054c:096f`，**必须断电重启** | **自己回到 `054c:0e0e`**，无需重启 |

两个可复用的结论：

1. **PID 分两层**：大量存储身份是机型级的（`0e8a` / `0e0e` …），Senser 服务身份是协议级的
   （`0336`、`02a9`，即 PMCA 已知的那两个）。所以工具要把起始 PID 做成参数，
   而不是写死 —— [libJit 工具](../2026-10-18-libjit-exec/)已改成 `--vendor-pid`。
2. **FX30 迭代成本低得多**：会话结束自己回大量存储，不用拔电池。A7C2 每次都要断电重启。

## 这个结论**不**证明什么

- 不证明 FX30 的固件偏移、补丁窗口、色彩单元与 A7C2 有任何关系 —— 那些是
  `appFw.so` / `cpapp-b.bin` 里按版本定位的，与本目录无关。
- 不证明其它机型也行。**只测了两台**，同属 `4.9.337-scel+` 平台。
- 不证明 FX30 上任何写入安全性。本目录只做了**只读**侦察 + 以只读命令验证执行入口。

## 用法

前置：相机 `USB 连接方式` 设为**大量存储**（PTP/MTP 模式下它是 `054c:0e10`，块设备都不出，
走不通）；相机卷**未挂载**；需要 `sudo`。

```sh
cd final/a7c2-re/report/2026-10-18-fx30-service-probe/scripts
PY=../../../../../analysis/.venv/bin/python

# 只读侦察：认证 → 观察变成什么身份 → 读 ProductInfo 与 /version.txt
sudo $PY fx30_probe.py --vendor-pid 0x0e0e --unmount-volumes --save ../evidence/fx30
```

`--vendor-pid` 用相机在大量存储模式下 `lsusb -d 054c:` 看到的 PID。脚本**不写任何文件、
不执行任何代码**；唯一读的是固定路径 `/version.txt`。

侦察通过后，再换 [libJit 工具](../2026-10-18-libjit-exec/)跑命令：

```sh
cd ../2026-10-18-libjit-exec/scripts
sudo $PY libjit_exec.py --vendor-pid 0x0e0e run --unmount-camera-volumes --command 'cat /proc/$$/status'
sudo $PY libjit_exec.py --vendor-pid 0x0e0e cleanup --unmount-camera-volumes
```

退出码：`0` 走到服务模式并读完；`2` 认证后没有出现已知 Senser 身份（该机型可能走别的协议）；
`3` 认证失败或读取失败。

依赖：同仓库 [`../2026-10-16-system-dump/scripts/`](../2026-10-16-system-dump/scripts/) 里的
`jiritsu_version_read.py`（Senser 帧）与其 `vendor/pmca-adamwuqwq`。可用 `A7C2_SCRIPTS` /
`A7C2_PMCA` 覆盖。

## 验收

| 项 | 结果 |
|---|---|
| 只读侦察（`fx30_probe.py`） | ✅ 真机通过，输出见 [`evidence/fx30-20261018/`](evidence/fx30-20261018/)（本地，`.gitignore` 排除 `evidence/`） |
| `/version.txt` 读出 | ✅ 19 B：`31182_15.2025122501` |
| `readHasp()` | ✅ 88 B，SHA-256 `1bf77665…` |
| 载荷在 FX30 上以 root 运行 | ✅ `A7JITE01 v1 pid=534 euid=0 status=0` + `Uid: 0 0 0 0`；记录见 [libJit 报告](../2026-10-18-libjit-exec/) 第 9 节 |
| 单元测试 | ❌ 无。脚本是纯 I/O 编排，验收就是上面两次真机运行；它不写不执行，风险面很小 |
| 第三台机型 | ❌ 未测 |

## 风险

- 认证会把相机切到服务身份。**A7C2 之后必须断电重启**；FX30 会自己回来。
- 在未知机型上，`readHasp` / `/version.txt` 之外的任何请求都不应该发。这个脚本刻意只发
  这两类，就是为了让它在陌生机器上是**可撤销的只读操作**。
- 不要把这个脚本当成"任意 Sony 机型都能开 shell"的工具。它只是说：链在 FX30 上通了。

## 参考

- [libJit 执行入口](../2026-10-18-libjit-exec/)、[整机固件转储](../2026-10-16-system-dump/)
- [ma1co/Sony-PMCA-RE](https://github.com/ma1co/Sony-PMCA-RE)（MIT，经 `adamwuqwq` 提交
  `3824c398c0fa`）：Senser 认证与 USB 传输
- 仓库顶层 [`参考项目与致谢`](../../README.md#参考项目与致谢)

# 成果：整机固件转储（2026-10-16）

Sony **ILCE-7CM2（A7C II，固件 2.01）** 全部闪存分区的完整转储方法与结论。本目录自包含：脚本、载荷、依赖清单与报告都在这里。

| 项 | 值 |
|---|---|
| 报告正文 | [`2026-10-16_逆向-A7C2-ILCE-7CM2-整机转储-report.md`](2026-10-16_逆向-A7C2-ILCE-7CM2-整机转储-report.md) |
| 结论 | 19 个分区 / **1.80 GiB** 一次会话转储完成；`e2fsck` 结构干净、跨通道 52 个文件摘要一致、与厂商参照树全树比对 **1078/1078** 逐字节一致 |
| 关键机制 | 每个闪存分区有**密文（`pnflasha*`）与明文（`cnflasha*`）两个视图**，内核以 ext2 挂载明文视图；经"预加载注入 + 固件 DEBUG 触发点"取得 root 后，在相机内 `dd` 到 SD 卡 |
| 前提 | 自有设备 + 明确授权；**未刷写固件、未修改只读分区** |

> **硬规则：批量数据写 SD 卡，不写相机内部分区。**
> 相机的 `/tmp`、`/etc`、`/var` 是**同一块 12 MiB vfat 分区**；写满它会让**所有**写入失败
> （包括中和 `/etc/ld.so.preload` 的清理写入，会留下不可确认的武装状态）。
> 细节、证据与恢复步骤见 [`RECOVERY-AND-LESSONS.md`](RECOVERY-AND-LESSONS.md)。

## 依赖（运行前必读）

这些脚本**不是开箱即用的**：它们来自作者本机工具链。路径与版本均为脚本中实际要求的值。

| 类别 | 需要什么 | 说明 |
|---|---|---|
| 设备 | Sony ILCE-7CM2，固件 2.01 | 其他固件版本的协议行为可能不同 |
| 权限 | `sudo` / root | USB 设备节点 `/dev/bus/usb/*/*` 默认 root 专属；loop 挂载与卡格式化同样需要 |
| Python | ≥ 3.8 | 仅用标准库 + 下列两个包 |
| Python 包 | `pyusb==1.3.1`、`pycryptodomex==3.24.0` | 见 [`requirements.txt`](requirements.txt)；PMCA 的 SHA-256 计算使用 `Cryptodome` 命名空间（**不是** `Crypto`） |
| vendored 第三方 | [Sony-PMCA-RE](https://github.com/adamwuqwq/Sony-PMCA-RE) 放在 **`scripts/vendor/pmca-adamwuqwq/`** | 该路径在 `scripts/usb_service_probe.py:36` 硬编码；推荐作者所用 revision `3824c398c0fa` |
| 系统工具 | `e2fsprogs`（`e2fsck`/`debugfs`/`dumpe2fs`/`mkfs.ext2`）、`exfatprogs`（`mkfs.exfat`）、`parted`、`util-linux`（`lsblk`/`blkid`/`mount`） | 卡准备与验证阶段使用；转储本身在相机内完成 |
| 载荷 | `payload/*.arm64.so`（**已包含**） | 校验值见下；源码同目录，**构建环境本项目未记录** |
| 参照树（可选） | 与相机同版本的厂商文件树 | 仅 `verify_system_dump.py` 与 `identify_partition_images.py` 需要；可用官方更新包 + [fwtool.py](https://github.com/ma1co/fwtool.py) 解包得到。**注意**：`dump_system.py` 的 `--reference` 默认值指向作者的工程路径，运行时需显式传入你自己的参照树（如 `--reference /path/to/menu-root`） |

获取 vendored 依赖（路径必须一致，相对于**本目录**）：

```bash
cd scripts
git clone https://github.com/adamwuqwq/Sony-PMCA-RE vendor/pmca-adamwuqwq
git -C vendor/pmca-adamwuqwq checkout 3824c398c0fa
python3 - <<'PY'
import sys; sys.path.insert(0, 'vendor/pmca-adamwuqwq')
import pmca.usb.sony        # 依赖 pyusb + pycryptodomex
print('pmca OK')
PY
```

载荷校验值：

| 文件 | 大小 | SHA-256 |
|---|---:|---|
| `payload/a7c2_shell.arm64.so` | 70,912 B | `d261855eb1ea461a77c7847fefef7e6960d4495433d245a9eeb458eef0b35e6e` |
| `payload/system_dump_probe.arm64.so` | 71,152 B | `5a95d422dff2134b055e159f786184b2929553c3416f0a0b8d847aee816bc5f1` |

## 用法

四个阶段，中途只需两个物理动作（卡入相机、卡入电脑）：

```bash
cd scripts
sudo python3 dump_system.py --card /dev/sda                 # 全流程：check → dump → fetch → verify
sudo python3 dump_system.py --card /dev/sda --format-card   # 首次使用：先重建卡的 exFAT + ext2 双分区布局
sudo python3 dump_system.py --card /dev/sda --stage dump    # 单阶段续跑：check | dump | fetch | verify
```

在相机里执行任意命令（**一次会话跑多条**，避免每条命令都重连一次相机）：

```bash
cd scripts
sudo ./.venv/bin/python run_command.py 'uname -a; cat /proc/version'   # 一次会话多条命令
sudo ./.venv/bin/python run_command.py --file commands.txt --json r.json
sudo ./.venv/bin/python run_command.py --interactive                   # 逐条，a7c2[1]> 提示符
sudo ./.venv/bin/python run_command.py --shell                         # 终端模式：cd/export 保留
sudo ./.venv/bin/python run_command.py --shell-script cmds.txt         # 批量喂给常驻 shell
```

读相机内存（DRAM 基址来自 `/proc/iomem`；批量数据按硬规则写卡）：

```bash
cd scripts
sudo ./.venv/bin/python memread.py --phys 0x400000000 --size 0x1000 --save dram-head.bin
# 整机 RAM（约 229 MiB）写卡、不回传：
#   usb_service_probe.py --mem-read-card-only --mem-read phys:0:0x400000000:0x4000000:ram.img
```

统一入口 `cam.sh`：`./cam.sh shell | cmd '…' | mem … | dump | fetch | verify`

**终端模式与逐条模式的区别**：逐条模式每条命令新起一个 `/bin/sh -c`，`cd`/`export` 不保留；
终端模式在相机内**常驻一个 `sh`**，用 FIFO 喂命令，因此状态保留、输出可增量取回。
实测（真机）：`cd /tmp/jiritsu` 后 `pwd` 返回 `/tmp/jiritsu`，`X=42; export X` 后 `echo $X` 返回 `42`。

操作细节：工具会**自动卸载桌面挂载的相机卷**（服务会话要求卷未挂载）；会话结束后相机会停在
`054c:096f`。恢复代价取决于主机操作系统：**Windows 需断电重启**，**Linux 重新插拔即可**——
工具会检测并给出这句提示。
命令输出与每条命令的退出码打印在 **stderr**（服务 CLI 的 JSON 事件流占用 stdout）。
实测：非零退出码（相机无 `id` applet 时的 `EXIT 127`）被正确报告。

设备端约束（决定了实现方式）：`/tmp` 是 vfat 而 **FIFO 无法创建**，所以 FIFO 放在
`/tmp_wbshd`（tmpfs）→ `/dev`（devtmpfs）→ 卡的 ext2 分区，哪个可用用哪个；
相机没有 `mkfifo`/`printf`/`tail`，因此用 `mknod` + 引号 heredoc + `dd bs=1 skip=`。

产物写入 `output/system-dump/`（脚本运行时的工作目录下）：每个分区一个 `.img`，另有
`manifest.json`（每镜像 SHA-256）、`verification.json`（全树比对）、`partition-inventory.json`（文件系统类型与内容归属）。

> 报告中的 `analysis/<脚本>` 路径即本目录的 `scripts/<脚本>`：报告是作者工程目录下的原始文档，此处保持原样并提供本条对照。

## 脚本清单

| 脚本 | 作用 | 额外依赖 |
|---|---|---|
| `dump_system.py` | 编排四阶段、提示物理动作、产出清单 | 下列脚本 |
| `usb_service_probe.py` | 服务会话、载荷部署、`--dump-to-card` 驱动 | vendored PMCA |
| `jiritsu_card_dump.py` | 相机内挂载卡分区、**自动发现**明文分区、逐个 `dd`、写完成标记 | — |
| `jiritsu_version_read.py` / `jiritsu_crypto.py` | 参考串行协议的帧与掩码实现 | — |
| `jiritsu_preload_deploy.py` | 预加载写入、DEBUG 触发、邮箱命令通道、清理与中和 | — |
| `jiritsu_read_file.py` / `jiritsu_write_check.py` / `jiritsu_sd_write.py` / `jiritsu_traversal_write.py` / `jiritsu_preload_clear.py` | 读写能力验证与恢复 | — |
| `copy_card_images.py` | 从卡取回镜像并双向 SHA-256 校验 | — |
| `verify_system_dump.py` | 镜像与参照树逐文件比对（loop 只读挂载） | 参照树 |
| `identify_partition_images.py` | 识别每个镜像的文件系统类型与内容归属 | 参照树（可选） |
| `payload/*.c` | 载荷源码：只读探针、命令通道 shell | aarch64 交叉编译工具链（**未记录**） |
| `jiritsu_memread.py` / `memread.py` | 读物理内存到本机：`/dev/mem` 的 DRAM 区间（`0x400000000` 起），分块回传或写卡（`--mem-read-card-only`） | `/dev/mem` 可读 DRAM（实测）；非 DRAM 区域返回 `Bad address` |
| `jiritsu_write_file.py` | 通用 MSC 层写入（任意相机绝对路径 + 任意长度），带读回验证；覆写会截断，可用于回收空间 | — |
| `senser_file.py` | 厂商 senser 文件 API（读/写/删）—— **本机返回 128，不可用**；保留以复现否定结论 | vendored PMCA |
| `cam.sh` | 统一入口：`shell` / `cmd` / `mem` / `dump` / `install-card` / `fetch` / `verify` | — |
| `run_command.py` | 命令执行入口：一次会话内多命令 / 批量 / 交互（`--shell`）/ 终端模式（`--shell-script`） | 同 `usb_service_probe.py` |
| `jiritsu_run_command.py` | 逐条命令驱动：回复从末尾剥离 `EXIT`/`A7C2-END`，命令自身打印类似内容不会误伤 | — |
| `jiritsu_shell.py` | 终端模式驱动：相机内常驻 `sh` + FIFO，保留 `cd`/`export`，按字节偏移增量取回输出 | — |

## 未包含的内容及原因

| 未包含 | 原因 |
|---|---|
| 作者私有的文件摘要清单与其比对工具 | 与本机证据目录耦合，外部读者无从获得输入；其作用可由"参照树比对"覆盖 |
| 厂商终端（Senser TE）验证工具 | 依赖 PMCA 的 Senser 类；其结论（未改固件无法取得 root 终端）已写入报告 |
| 19 个分区镜像（1.80 GiB） | 属本机转储产物，且单文件最大 314 MB，超出 GitHub 单文件限制 |

## 判读纪律

- **不要用提示符判断权限**：root 判定只看 `/proc/$$/status` 的 `Uid`/`Gid`；
- **不要用"命令返回 0"判断有效**：`dd` 成功不证明内容有意义，必须做文件系统级校验（`e2fsck`、魔数、熵）；
- **不要用单一证据判断完整**：结构校验 + 跨通道摘要比对 + 参照树全树比对，三者缺一不可。

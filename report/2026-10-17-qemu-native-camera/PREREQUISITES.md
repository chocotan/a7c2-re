# 启动前准备：镜像来源、下载与系统资源

**当前菜单实验可以仅以官方2.01升级包作为Sony原始内容来源，不要求先转储整机19个分区。** 如果你把升级包内的固件数据称为BODYDATA，它是解包输入，不是可以直接交给QEMU启动的虚拟磁盘。

当前启动器先从已解包的分区镜像提取原内核、initrd、`/usr`、CP文件和默认配置，再构造实验cpio根，通过修改过的QEMU直接装载Linux内核。它没有执行完整相机BootROM/eMMC冷启动，也没有把BODYDATA或19个分区自动拼成一台完整相机。这正是“菜单已恢复”和“完整拍摄启动尚未完成”的区别。

## 1. 三种资产获取路线

| 来源 | 适用场景 | 本仓库目前验证到哪里 |
|---|---|---|
| 官方ILCE-7CM2 **2.01**升级包/BODYDATA/FDAT解包 | 复现当前干净菜单实验 | **已验证**。当前导入器固定验证官方解包内容及ALLLANG默认配置 |
| 自有相机的SD卡转储 | 获取实际分区、设备特有数据，或对照升级包 | **已验证转储路线**：19分区、1.80GiB及结构/内容校验；但不能据此认定其镜像直接符合本导入器的固定hash |
| 已适配本机型的fastboot环境 | 已有具备分区读出能力的bootloader/payload时获取镜像 | 可作为获取来源；**本成果未提供已验收的A7C II fastboot完整转储/一键导入流程**，须核对实际命令、视图与结果 |

### 官方包：当前可复现入口

自行合法取得对应的2.01包，用支持该格式的完整解包工具取得下节的目录及FDAT。可参考 [fwtool.py](https://github.com/ma1co/fwtool.py)；整机报告记录过使用revision `72fbdd15ddcc` 的参照树。此成果附带的 `runtime/analysis/vendor/fwtool` **只是FAT读取/写入所需的MIT子集，不是完整升级包解密/解包工具**。

当前 `import-assets.sh` 接受的是**已解包目录**，不接受原始EXE、DMG、BODYDATA或真机整盘作为自动解包输入。外部工具是否支持你持有的包格式应单独验证；不要把本导入器宣传为下载官方安装包后一步直启。

Sony官网可能只展示更新版本。本轮没有联网重新确认2.01旧版下载可用性；新版不能替代这里固定校验的2.01，更不能仅改文件名绕过版本检查。

### SD卡：本仓库已有实测方法

完整步骤、依赖和验收见 [整机固件转储](../2026-10-16-system-dump/README.md) 和 [恢复与经验](../2026-10-16-system-dump/RECOVERY-AND-LESSONS.md)。该路线在相机内取得root后，将数据写到专用SD卡的ext2分区，再在电脑只读取回与校验。

- 使用 **`cnflasha*` 明文视图**；`pnflasha*`是密文物理视图。读出成功不代表取得了可分析的明文文件系统。
- 批量镜像写SD卡，不写相机的 `/tmp`、`/etc`、`/var`：它们共用仅12MiB分区，写满会影响恢复操作。
- `--format-card`会重建卡的分区表，必须使用已备份、可清空的专用卡。这里不指定任何读者机器上的磁盘设备名。
- 保留分区大小、SHA-256、文件系统结构校验与参照树比对结果。`a7c2-rootfs.img`、`a7c2-usr.img`等转储命名不等同于官方解包目录命名。

**真实转储并非当前导入器的自动兼容输入。** 分区长度、尾部填充、可写状态和个人设置都可能与升级包不同。应先只读识别和逐文件对照，再单独实现/验证导入配置；不要重命名、裁剪或改hash清单来假装匹配。设备私有Backup、序列号、配对/网络信息不应混入公开包。

### fastboot：先确认设备与实际读出命令

如果你已有适配A7C II的fastboot环境，可以从该环境取得所需分区。需要主机fastboot客户端（如官方 [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)）及**明确支持本机型/固件的进入方法和payload**。

仅能被 `fastboot devices` 列出不代表能读分区。分区枚举、读出/暂存/取回可能是payload自定义OEM命令，并非所有bootloader支持统一的 `fetch` 或 `dump`。以实际实现/帮助信息为准，记录完整返回、分区长度和SHA，并检查所得数据是否明文。

工作区曾参考ILCE-7M4的自定义fastboot payload文档，其中有 `partition dump` 等OEM扩展；**另一个型号的参考不是A7C II的验证结果**。本公开成果没有包含该payload，也不提供未经验证的进入/读出命令。取镜像不需要执行刷写、擦除或解锁命令；不要把这些写入动作当作默认准备步骤。

## 2. 当前导入器实际需要的文件

目录名和相对路径必须保留：

```text
FIRMWARE_ROOT/
├── 0700_part_image/dev/
│   ├── nflasha3
│   ├── nflasha8
│   └── nflasha15
├── 0800_appli/setting/
│   ├── DmmCfg.bin
│   ├── cxd90057.dtb
│   ├── development.txt
│   ├── facedmy.bin
│   ├── kemco.txt
│   └── ulogio.bin
└── 0110_backup/SYSIPSX-DSLR/PP/CX89701_ALLLANG.bin
```

另需 `firmware.fdat` 或其**前512字节**作为型号、版本和身份校验输入。

| 文件 | 用途 | 固定长度 |
|---|---|---:|
| `nflasha3` | 原内核、initrd及启动相关内容 | 32,752,640B |
| `nflasha8` | CP相关文件 | 25,102,336B |
| `nflasha15` | `/usr`、原应用、库、字体/图片等 | 770,030,592B |
| `CX89701_ALLLANG.bin` | 干净默认配置，不使用研究者私有Backup | 2,390,264B |
| 六个setting文件 | 内存/驱动等配置；空的development.txt也须保留 | 逐项见导入器固定清单 |
| FDAT头 | 固定型号/2.01版本及身份 | 至少512B |

各文件SHA以 `runtime/analysis/prepare-assets.py::INPUTS` 与 `HEADER_SHA` 为准。大小一致不足以通过。完整19分区、真机私有setting、全部DRAM转储、BootROM不是当前菜单导入器的必需输入；它们可能用于后续整机研究，不能保证取得后就自动恢复成像。

## 3. 需要下载或安装什么

| 类别 | 内容 | 说明 |
|---|---|---|
| 本仓库 | 当前源码 | 不含Sony固件、私有配置、镜像和编译程序 |
| QEMU基底 | `qemu-11.1.2.tar.xz` | SHA及官方发行站点见[BUILD](BUILD.md)；不能用系统普通QEMU替代Sony修改版 |
| DTC/fwtool | 无需另下载本实验使用的子集 | DTC源码、MIT FAT代码和许可已随仓库提供；完整固件解包工具另行准备 |
| Python | 3.12+及Pillow | `python3 -m pip install -r requirements.txt`；资产导入自身可用3.11，但统一构建入口要求更高版本 |
| 构建工具 | C/C++编译器、Clang、LLD、Make、Ninja、Meson、Flex、Bison、pkg-config、patch、Git | QEMU当前Meson接受版本至少1.5；源码包中的Python依赖/离线wheel还应按configure提示核对 |
| 图形开发库 | GTK3、SDL2、pixman、glib2、libseccomp开发文件 | 构建参数由build-qemu.sh固定；不要求OpenGL |
| Linux运行工具 | bubblewrap、util-linux/flock、cpio | 需要可用的用户/挂载/网络命名空间隔离 |
| 资产提取 | e2fsprogs/debugfs | 导入官方镜像时需要；不挂载真实设备 |
| 可选验证 | dosfstools/fsck.fat；Xvfb、xdotool、ImageMagick | 分别用于存储结构检查及GUI自动验收 |
| 仅SD转储路线 | Sony-PMCA-RE、pyusb、pycryptodomex及卡工具 | 版本/路径以[原转储README](../2026-10-16-system-dump/README.md)为准，QEMU菜单运行本身不依赖USB相机 |

当前验证环境为 **Linux x86_64 / Arch Linux**。其他发行版请安装对应包名，并核对版本；没有将某条未经执行的apt命令当作完整环境保证。Windows/macOS原生运行、WSL及容器中运行尚未验收。

## 4. 宿主资源与图形环境

以下“建议”是准备余量，**不是经过最低配置测试的门槛**。

| 项目 | 建议/约束 |
|---|---|
| CPU | 运行建议至少4个逻辑核，构建建议8个；构建最多8 job，当前客体单vCPU、AArch64 TCG模拟 |
| RAM | 建议32GiB或以上。QEMU参数使用24GiB稀疏地址覆盖以容纳Sony高物理地址；实际驻留量较小，但低内存或严格内存提交策略可能分配失败，未验证8/16GiB最低配置 |
| 磁盘 | 单次准备建议至少10GiB空闲，保留原包、解包树、多个构建/实验建议20–30GiB以上；构建器保留每次独立build目录 |
| 文件系统 | 支持Unix执行位及符号链接；不要把runtime直接放到不支持这些语义的FAT目录 |
| 桌面 | 本地X11或带Xwayland的桌面，`DISPLAY`及认证文件可用；无桌面可用`--display none`做自动化，但不会出现窗口 |
| 权限 | QEMU运行/构建/官方资产导入不要求以root运行；bubblewrap隔离必须可用。真机SD准备/转储的权限要求见其独立文档 |

供容量估算的本轮本地占用约为：生成`menu-root` 684MiB、保留解包启动内容48MiB、cpio 395MiB、**每个**独立QEMU构建目录约1.4GiB；另加原升级包、原分区输入、生成程序和日志。未做完整宿主RAM峰值/最低配置基准；`-m 24576`不是说真实相机拥有24GiB，也不能据稀疏映射就保证小内存主机可用。

## 5. 从准备到启动

在本成果目录执行，变量换成自己已准备并校验的路径：

```bash
python3 -m pip install -r requirements.txt
./build-qemu.sh /path/to/qemu-11.1.2.tar.xz
./import-assets.sh --firmware-root "$FIRMWARE_ROOT" --fdat "$FDAT_OR_HEADER"
python3 runtime/analysis/prepare-assets.py --runtime runtime --check-runtime
./start.sh
```

`import-assets.sh`的参数及目录不可覆盖规则见[导入说明](runtime/analysis/ASSET-IMPORT.md)。启动时会构建辅助程序与cpio，之后出现原生QEMU窗口。若已有窗口持有运行锁，新启动会等待，不能据此认定驱动卡死。

成功判据以原画面、可核验提交和场景测试为准。完整拍摄主界面、CMOS、镜头/AF、照片和录像仍在实现；当前菜单启动成功不等于这些能力已经恢复。

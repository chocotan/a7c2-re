# 离线构建与许可证

`build-qemu.sh` 已在最终目录完整跑通。它从固定 QEMU 源码包、安全解包、应用完整 Sony 补丁、验证改动文件身份、离线编译，再通过真实 QMP/input socket 测试后安装。它与相机运行共用锁，避免替换正在使用的程序。

## 重建

在本成果目录执行：

```bash
./build-qemu.sh
# 或使用自己保存的、相同身份的原始源码包：
./build-qemu.sh /path/to/qemu-11.1.2.tar.xz
sha256sum --check source/build-programs.sha256
```

默认源码包是 `source/vendor/qemu-11.1.2.tar.xz`；SHA-256 必须为：

```text
731b5681e4bb18be313231579b8efd0296c5b015fa36dc533874b639ba838016
```

可在 QEMU 官方发行站点 `https://download.qemu.org/` 查找同名源码包；本轮未联网重新核验下载可用性，离线输入始终以上述 hash 为准。构建脚本不联网，不会自动安装依赖。补丁及公开的 `source/dtc/` 源码树由 `source/manifest.json` 的 hash 绑定；应用后会再次核对全部修改文件。DTC压缩归档仅留作本地生成记录，公开重建不依赖它。

需要 Linux 原生构建环境：C/C++ 编译器、Python 3.12或更新版本、Ninja、Meson/其QEMU构建依赖、Make、Flex、Bison、pkg-config、patch、bubblewrap、flock，以及 GTK3、SDL2、pixman、glib2、libseccomp 开发文件。宿主动态库仍由发行版提供；产物不是通用于所有 Linux 发行版的静态程序。可用 `ldd` 检查本机缺失依赖。

构建最多使用八个 job。配置与编译在无网络命名空间、只读宿主根、仅本次 build 目录可写的 bubblewrap 中完成。上游 EDK2 中一个未使用的 `/opt/X11/include` 绝对链接被明确跳过；其余归档条目仍走 Python 的安全 data filter。

构建保留 `source/build/`，失败时可检查而不会误删其他工作。成功后安装：

| 文件 | 用途 |
|---|---|
| `runtime/sources/qemu-11.1.2/build-sony-ui/qemu-system-aarch64` | 带 Sony timer/bus/输入设备及 GTK/SDL 的整机 QEMU |
| `runtime/sources/qemu-11.1.2/build-user/qemu-aarch64` | 使用原 AArch64 glibc 的辅助程序检查 |
| `runtime/sources/qemu-tools/usr/lib/libfdt.so.1` | 本机 DTB 构建工具依赖 |
| `runtime/sources/qemu-tools/usr/bin/dtc` | 从随附DTC源码生成的设备树编译器 |
| `runtime/analysis/qemu-virt-board/virt-packed.dtb` | 从公开的同名 `.dts` 源码生成的板级模板 |
| `source/build-programs.sha256` | 本次生成程序的相对路径清单及 hash |

该入口证明完整源码链可重建；**未声称不同发行版、编译器和路径下二进制逐字节相同**。

## 运行时辅助程序

Python运行依赖只有Pillow，其余使用标准库：

```bash
python3 -m pip install -r requirements.txt
```

导入 Sony 资产后，`runtime/analysis/build-observers.sh` 编译项目的 observer、软件光栅/LZ77、OSD、ramfb、输入及诊断程序。它沿用原固件 glibc ABI，使用本机 Clang/LLD；不是把 Sony 原库重新编译。`start.sh` 会执行所需预检、构建和私有运行环境准备。

资产导入还使用宿主 `debugfs`（e2fsprogs）；FAT解析器以MIT源码随包提供。独立存储验收使用 `fsck.fat`（dosfstools），GUI自动验收使用Xvfb、xdotool和ImageMagick。这些工具不是运行原生窗口本身都需要的依赖。

## 随附的许可与原始资产边界

- QEMU 适用 GPL-2.0，另有按源文件标明的组件许可。完整基底源包保留原始许可文件；摘要及 GPL 文本在 `source/licenses/QEMU-LICENSE.txt`、`QEMU-GPL-2.0.txt`。
- `source/dtc/` 保留DTC源码及原许可；libfdt 的 BSD-2-Clause 文本同时保存在 `source/licenses/libfdt-BSD-2-Clause.txt`。生成的编译器、库、DTB和本地压缩包不提交。
- 最小 fwtool FAT 代码采用 MIT，许可在相邻 `runtime/analysis/vendor/fwtool/` 及 `source/licenses/fwtool-MIT.txt`。
- Sony 固件和由它导入的原库、图像资源、配置不属于上述开源许可，也不放进公开源码补丁。使用者须自行取得对应固件，再通过 `import-assets.sh` 导入。

本地运行日志、资产、构建树和生成镜像被 `.gitignore` 排除。分享源码时不要把整个 `runtime/` 或研究目录误作公开压缩包。

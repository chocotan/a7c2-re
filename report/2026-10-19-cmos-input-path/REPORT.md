# CMOS 输入能否从某个进程拿到

日期：2026-10-19。范围见 [scope.md](scope.md)。`flavor = null`。

## 1. 结论

**没有"打开就能读传感器"的设备。图像数据通过 DMA 物理页映射进入用户态，而且已证实的通道给的是处理/编码后的数据（JPEG、NV12），不是传感器原始 Bayer。**

| 问题 | 回答 | 置信度 |
|---|---|---|
| 有 V4L2 / `/dev/video*` 这种标准采集节点吗 | **没有** | 已确证 |
| 帧经什么进入用户态 | `stream2` 的 `mmap`（物理页映射）+ DMM/EE 图像缓冲 | 已确证 |
| 谁能拿到 | `im.elf` 里的库（PID 532 持有 `/dev/stream2`） | 已确证（进程归属） |
| 拿到的是什么 | 已证实的通路是 **JPEG / NV12** | 已确证 |
| 能不能拿到未经处理的 Bayer | **未确证**（不排除存在 RAW 缓冲，但没有证据） | 未确证 |
| 传感器是否挂在 Linux 侧 | 未确证；CP 域管理成像是合理推测 | 未确证 |

一句话：**root 权限不是关键**。要拿帧，需要的是有效的缓冲地址/句柄、尺寸，以及赶上缓冲生命周期；
而没有证据表明原始 CMOS 数据曾经出现在这些缓冲里。

## 2. 证据

### 2.1 没有标准采集接口

真机：无 `/dev/video*`，`/sys/class` 无 video 类；`/proc/devices` 里与成像相关的是
`249 stream`、`249 stream2`、`251 ldec`、`240 pcidma`、`254 uio`（uio 未建节点）。

### 2.2 `stream2` 只映射物理页，不定义"帧"

`stream2.ko` 的 ioctl 只校验命令字：

```c
if ((v5 >> 8 & 0xff) != 0x78) { printk("<1>Stream Driver : ioctl error !!\n"); return -EINVAL; }
if (v5 != 0xc0047805) { ... }
```

- ioctl `0xc0047805` 从用户态复制一张地址表，最多 16 项，每项 `{u64 phys_addr, u64 size}`，
  4 KiB 对齐，`{0,0}` 终止。
- `stream_mmap()` 校验"映射长度 == 各项 size 之和"，逐页 `vm_insert_page()`。

因此**缓冲区内容由用户态决定**；驱动里没有宽高、像素格式或帧边界字段。
单凭驱动不能认定它承载的是传感器帧，只能说它能把 DMA 页映射进进程。

复核：

```sh
R=analysis/qemu-runtime/analysis/menu-root/usr
kuna decompile "$R/kmod/stream2.ko" .text+0x208
kuna decompile "$R/kmod/stream2.ko" .text+0x0
```

### 2.3 EE 缓冲 + 已证实的 JPEG/NV12 消费链

`libInfraPtpControl.so`：

- `DmmBufferManager::OpenEeImageBuffer@0xfbe0` 向 DMM 端点 `0x61003c` 发操作 `0x24`，保存回复地址；本函数不采集。
- `PtptIpEe::MmapEEJpeg@0x12100 → pubMmap@0x12954`：从 DMM 回复取物理地址与长度，
  执行 `ioctl(..., 0xc0047805, 表)` 再 `mmap(...)`。

`libInfraStreamingUsb.so`（路径更完整、更能定性）：

- `rcvVdtEvtNotify@0x10610 → comMmap@0xf4a8 → stream2`，通知里带地址与长度；
- `getStrVdtFormat@0xf948` 的格式枚举只有：

```text
VDT_FORMAT_JPEG_FHD / _HD720 / _4K
VDT_FORMAT_NV12_FHD / _HD720 / _4K
V4L2_PIX_FMT_MJPEG
```

**没有 RAW / Bayer 项**。这就是"已证实通道给的是处理后的数据"的直接依据。

`libmpr.so` 承担录制、回放与编解码协调（`XavcsMovieWriter`、`MoviePlayer`、
`Codec::SendMsgToAvbbc@0x27c058` → `osal_snd_msg(0x8055001f, …)`），
`stillrecord::_shrare@0xe274c` 处理 `DefStruct::RawInfo` 并调用 `app_dmm::share_and_open`。
这说明存在与 RAW 相关的**缓冲管理**，但**不能**据此认定它接收连续 CMOS Bayer。

### 2.4 传感器不在 Linux 侧（推测）

检查了 120 个 kmod、`usr/lib/`、以及两份 dtb
（`analysis/extracted/firmware/0800_appli/setting/cxd90057.dtb`、
`analysis/qemu-boot-research/real-dtb-config/extracted/nflasha1/boot/cxd90057.dtb`）：
**未见 imx/imager 传感器驱动或节点**，两处 I²C 节点下也没有传感器子节点。

结合既有报告里 `run_procam()` 向 CP 域发消息（[cmos-init-boot](../2026-10-17-qemu-full-system-ds/report/2026-10-18-cmos-init-boot/REPORT.md)），
"CP 域管理成像"是合理推测；**硬件归属未确证**。

## 3. 对模拟工作的含义

要"喂"进一帧，模拟点应落在 **`stream2` 的 DMA 缓冲 + DMM/EE 句柄**这一层，
而不是伪造一个 `/dev/video` 节点——固件根本不走 V4L2。

但缓冲区里要放什么，取决于消费者：`libInfraStreamingUsb` 那条链期望 **JPEG 或 NV12**；
静态路径（`libmpr` + `DefStruct::RawInfo`）涉及的是另一套 RAW 缓冲管理。
这两条要分开模拟，不能假设一份数据两边都能用。

## 4. 边界与未确证项

- 未做动态取帧验证；本节全部是离线静态分析 + 真机只读观察（`ps`、`/proc/*/maps`、`/proc/*/fd`、`/proc/devices`）。
- **未确证**：`Ee` 的全称；`mpr` 的全称；是否存在暴露原始 Bayer 的缓冲；传感器硬件的域归属。
- 真机观察只在当前状态下取得（未在拍摄状态下复测），缓冲区是否活跃、内容是否有效均未验证。
- 没有在相机上写入、装载模块、或读取任何缓冲内容。

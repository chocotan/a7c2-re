# 原固件文件系统写入与主机导出验证

**已证明原 Fsys 能实际写入虚拟 FAT 磁盘并导出到主机；尚未证明拍照或录像。** 本测试没有运行相机应用、调用编码器、连接相机或物理 SD。

## 真实调用路径

原 `libInfraMediaCommon.so` 自带文件型 dummy 驱动。测试 preload 只在显式 `A7C2_FILE_MEDIA_TEST=1` 时包装原 `mcmn_param_init`：先执行原函数，再将原配置项 `EXISTMEDIADRIVER (0x69)` 从实际读取的1改为0，让原 `mcmn_init` 继续选择已经存在的文件后端。它没有替换 Fsys、模拟成功返回或跳过实际写入。

`DBG_DRV_DUMMY_OMIT_WRITE (0xf9)` 必须为0。原 root `/dev/mmca` 因而访问 guest 普通文件 `/image_mmca`。初始镜像由项目生成，只有 `SEED.TXT`，不包含待验证文件。

第一个独立进程执行原 API：

```text
mcmn_init → Fsys_Init → Mount → Mkdir → Open(0x0f)
→ Write(4096) → Flush → Close → Open(1) → Read/比对
→ Close → Unmount
```

第二个进程重新初始化、挂载、打开并读回，再卸载。所有状态调用返回0，Write/Read返回实际4096字节。之后仅为传输，将 Linux 文件中真实的磁盘内容复制到专用预留区 `0x41e600000`；QMP 把这4MiB保存到主机。该区域与原 kemco/CP/native-display 预留不重叠。

## 验证结果

最终运行：`runtime/analysis/menu-runs/run-k4pzww2c`。

- 两个原 Fsys 进程均完成，第二个进程读回内容一致。
- 主机磁盘镜像的完整 FNV 与 guest 复制时计算值一致。
- `fsck.fat -n` 独立检查通过，没有修改镜像。
- 从 FAT 目录和 cluster 解析得到 `/SEED.TXT`（32B）和 `/QTEST/PROBE.BIN`（4096B）。
- 文件内容为确定的测试模式，逐字节与输入相同。

| 内容 | SHA-256 |
|---|---|
| 完整磁盘镜像 | `d2490baadea79eabbfda9e23bea5f70c2f821e41915d172d8603ed96fd5a7acf` |
| 原 Fsys 写入的4096B文件 | `c5d2097fa1cd94e31b5aeb3e30ddad3b49194826436c11fdf91f27090e8f9e9a` |

早期运行 `run-vxhbaums` 已通过内容读写，但测试模板的空白卷标不符合 fsck 判据。后来修正的是**初始模板**，重新由固件写入，再得到上述最终结果；没有修改旧输出镜像来伪造通过。

## 复现入口

依赖正常运行所需的 Sony 资产、编译环境，以及用于独立结构检查的 `fsck.fat`。

```bash
env -u MENU_NATIVE_WINDOW -u MENU_HEADLESS_OSD -u MENU_CONTROL_FD \
    -u QEMU_NATIVE_DISPLAY -u MENU_KEY_INJECT \
    MENU_MEDIA_SELFTEST=1 bash runtime/analysis/run-menu-probe.sh
python3 runtime/analysis/verify-media-image.py /本次输出的run目录
```

该模式独占 guest，不启动 camera app，不适用于正在运行的 GUI 会话。普通 GUI 启动会清除继承的实验变量。

## 仍未完成的层

- MediaDetect/RM 的有卡/无卡通知与拍摄主界面启动门槛，不等价于这次直接 root 挂载测试。
- 原 MPR、libstill 或 MP4 writer 对真实 capture/encoder 数据的消费。
- 虚拟 CMOS/镜头/AF 与快门、REC 生命周期。
- 原 dummy 的底层 flush 不是 host fsync。本次证明的是完整写出、重挂读回和保存的主机文件；不声称相机断电持久化语义已实现。

因此本文件不能被引用为“已经拍出照片/录出视频”。

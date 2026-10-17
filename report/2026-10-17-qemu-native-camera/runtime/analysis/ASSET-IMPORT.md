# Sony 2.01 offline native assets importer

从合法取得、已经解包的 **ILCE-7CM2 2.01** 固件生成干净 QEMU runtime 资产。
这是固定输入版本的离线安装器；不下载、不启动 VM、不访问设备、不生成 cpio/ext2。
公开源码包括本目录 Python、固定 `sony-assets.json` 清单、文档和 MIT fwtool 子集，**不得把 `check/` 或生成的 runtime 当公开源码一起分发**。

## 安装

要求 Linux、Python 3.11+、e2fsprogs 的 `debugfs`，至少约 1 GB 空闲空间；不需要 root。
将本目录整体移动即可使用，依赖只从相邻 `vendor/fwtool` 加载。
先由启动链安装程序准备 `RUNTIME/analysis/guest-overlay/simulation/diag.sh`。
缺失时导入立即失败；不会从旧 menu-root 取默认文件。

```bash
python3 prepare-assets.py \
  --firmware-root "$FIRMWARE_ROOT" \
  --fdat "$FDAT_OR_512_BYTE_HEADER" \
  --runtime "$RUNTIME"
python3 prepare-assets.py --runtime "$RUNTIME" --check-runtime
```

`FIRMWARE_ROOT` 下只读取代码 `INPUTS` 列出的 nflasha3/8/15、6 个 setting 文件及
`0110_backup/SYSIPSX-DSLR/PP/CX89701_ALLLANG.bin`。FDAT 只读前 512 B。
所有原始输入只读；整个原镜像 hash、大小和 FDAT 身份必须匹配。
资产路径不能包含符号链接；普通目录名可包含空格。输入期间不应由其他进程改写文件。

runtime 可以先包含 main 安装的脚本、overlay 和 `sources/`，但以下输出必须不存在，
包含悬空符号链接也拒绝：`analysis/{menu-root,unpacked,libIMDB-menu.so,firmware-identity.json,native-assets.json}`。
不支持覆盖升级或清理用户原目录；同一 runtime 只允许一个安装进程。

## 生成内容与保证

- debugfs 从原始 initrd 展开根，再从 nflasha15 展开 `/usr`；保留原始 guest 链接及文件模式。
  非 root 用户无法保留 guest UID/GID；只容忍精确的 chown 权限提示，其他 debugfs 错误失败。
- nflasha3/8 由已核验镜像提取到 `analysis/unpacked/firmware.tar_unpacked/0700_part_image/dev/`，
  `/cp`、`/system`、6 个 setting 和 ALLLANG 按现有启动布局安装。不会复制 770 MB 的 nflasha15 原镜像。
- `seed-backup.bin` 与 `seed-setting/Backup.bin` 均为同一个公开固件 ALLLANG 内容，seed 区只允许 7 个指定文件。
  不读取 real-setting，不保留研究根 rootshd 或旧 helper。main 随后执行既有 build/pack。
- IMDB 通过标准库 ELF64 PT_LOAD 映射，在 VA `0x1db20..0x21d80`、步长 `0x48` 上定位恰好 10 项，
  执行 `mask &= ~7`（include-default）；原始和输出 SHA 固定校验，3 个输出副本必须一致。
- 身份 FAT 为 4 MiB，fwtool DAT2/DAT3 roundtrip 校验；目录排序固定、时区固定 UTC+8、mtime 为 315532800，
  复现历史 hash `535d8f0570a4e56e04e71c400863ed39bbd735c85a9aa8e32748a136968d495c`，DAT4 为 `01 02`。
- schema 2 的 `native-assets.json` 最后安装，记录型号、版本、输入 hash、`source_class`、关键参数及每个安装文件的大小/hash/权限或链接目标。
  不写输入宿主绝对路径，不将派生 IMDB/身份卷标记为原厂。
- `--check-runtime` 不需要原始固件，独立校验关键固定锚点、manifest 记录、seed 一致性和所有 guest 链接。
  绝对 guest 链接按 guest 根解析；哈希读取从不跟随宿主符号链接。Sony 资产集合及预期摘要由源码中固定摘要的 `sony-assets.json` 约束，不能靠改写 manifest 取消校验。
  host 生成根中的 `setting/` 必须为空，只在 guest 中从已验证 seed 初始化。仅允许列明的编译 helper 和项目 overlay 新增到根中；执行位也参与校验。
  manifest 不是数字签名；不抵御攻击者同时修改受信任的工具源码。schema 1 的旧测试记录不再接受，应在新目录重新导入。

失败保留 `analysis/.native-assets-staging-*` 和 debugfs 日志，错误输出给出位置；不声明安装成功。
成功后也保留本次的小型 debugfs 日志目录。中途安装失败可能留下部分输出，必须人工移到新的 runtime 后重试；
工具不删除旧文件。提取的文件字节、IMDB、身份 FAT 与 manifest 可复现；后续 cpio 的顺序、UID/GID、mtime 不在本工具保证范围内。

## 检查

```bash
python3 test-assets.py -v
SONY_TEST_USR_IMAGE="$FIRMWARE_ROOT/0700_part_image/dev/nflasha15" \
  python3 test-assets.py --runtime "$RUNTIME" -v
```

第一条是无 Sony 文件的小测试；第二条验证真实导入、精确 IMDB SHA/10 字节差异，
并在独占测试 runtime 上短暂篡改库、Backup 和链接，确认检测后恢复原内容。
**第二条只用于测试 runtime，不能在别人正在运行的 runtime 上执行。**

本工具只交付资产导入。原生窗口、QEMU、observer 编译、基础 DTB、启动链与 GUI 验收由主集成负责。
fwtool 修改和许可证见 [vendor/fwtool/README.md](vendor/fwtool/README.md)。

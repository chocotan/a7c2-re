# A7C II 在 QEMU 中启动：问题、修复与验收边界

本记录面向希望复现、修改或继续研究 ILCE-7CM2 2.01 的同好。它按故障层次整理从原厂内核启动到原生窗口、输入和虚拟存储的主要问题，保留失败实验和被后续证据推翻的判断，而非只列最终启动参数。

镜像获取、BODYDATA与运行输入的关系、fastboot/SD卡路线及系统资源要求，先看[启动前准备](PREREQUISITES.md)。

**当前能启动并操作原固件菜单，但不是完整相机模拟器。** 九个原生操作通过了指定场景验证，不等于所有菜单项均可用。用户报告的“设置 → 地区/日期 → 地区/日期/时间设置”确定键断言，已通过原日期驱动与启动时钟初始化修复，并完成123步重放回归；异常后的输入队列积压也已修复。退出菜单后的拍摄主界面、CMOS、镜头/AF、固件拍照和录像仍未验收。

本文由文档代理核验已有证据后整理，主会话随后补入原日期驱动、时钟初始化与123步整机回归的实际结果。保留“仅加载 udate 仍失败”的阴性对照，避免把模块加载等同于状态初始化。后续修复应继续追加新证据，不能把排队中的实验写成成功。

## 1. 状态与阅读入口

状态用语：**已解决**指明确的软件错误已修；**范围内解决**指通过列出的场景；**实验替代**指软件端承担了限定硬件契约；**部分完成**指已有链路但未到终点；**未解决**指阻塞仍在。

| 层次/现象 | 当前状态 | 已取得的证据及范围 | 去哪里看 |
|---|---|---|---|
| 原厂内核/initrd 不启动 | 范围内解决 | 修改 DTB、固定装载地址后到 BusyBox；不是完整相机启动 | §3，E01 |
| 内核/IPC 模块缺符号或访问空设备 | 部分完成 | 依赖顺序已修；未建模 USB/MMC/PCIe 显式跳过 | §4，E02 |
| 应用是否加载、是否建屏被误判 | 已解决一批测量错误 | 完整日志、正确 ELF VA、ABI 保留与返回时序 | §5，E03 |
| 菜单生命周期没有走到 | 范围内解决 | 原管理器流程构造菜单、调用 init、创建 `0x2ca` 的44项 | §6，E04 |
| 位图复制/字体/背景不能画 | 实验替代 | 真实 GPE 请求、受限复制、显式像素模型、31张原资源 CPU 解码 | §7，E05 |
| 第二帧等待输出完成 | 实验替代 | 有锁的像素消费、原子提交后通知；原生帧返回和交换 | §8，E06 |
| QEMU 没有相机图形窗口 | 范围内解决 | GTK/SDL 构建、标准 ramfb；窗口与 AP 像素一致 | §9，E07 |
| 原生按钮/键盘无有效响应 | 范围内解决 | 七键及右后拨盘双向；九步图像对照，不覆盖全部菜单 | §10，E08 |
| 日期设置确定后卡死、后续“队列已满” | 已修复报告病例 | 原时钟初始化后123步重放通过；异常持续监控与清队列通过 | §11，E09 |
| 路径、锁、快照导出失败 | 已解决列出的错误 | 长 Unix 路径、构建锁继承、逐命令标记、严格导出校验 | §12，E10 |
| 研究 rootfs 不适合分享 | 范围内解决 | 干净 ALLLANG 导入、schema2、固定集合及权限检查 | §13，E11 |
| 源码不足以重建程序 | 范围内解决 | 完整11文件差分、离线源码构建和生成程序清单 | §14，E12 |
| 固件能否真实写主机虚拟磁盘 | 范围内解决 | 原 Fsys 双进程重挂读回、镜像校验、fsck 全通过 | §15，E13 |
| 退出菜单后的主界面、模式切换 | 未解决 | 媒体首次状态和序列初始化链尚不完整 | §16，E14 |
| CP、CMOS、镜头、AF、拍照录像 | 部分调查/未解决 | CP 原字节已执行到首个缺失地址；未到 RTOS/镜头接收端 | §17，E15 |

本成果只运行隔离 VM，不接真机，不直通物理磁盘或 SD；只创建主机实验目录中的虚拟文件。仓库中的真机脚本风险与本实验不同，不能把这里的 preload、地址或启动命令直接用于实体相机。范围约定见仓库 [AGENTS.md](../../AGENTS.md) 及本成果 [README](README.md)。

## 2. 先分清“启动成功”在哪一层

当前可复现调用路径是：

```text
原厂 AP 内核 + DTB 适配 + 实验根文件系统
  → 原 bootin / im.elf / appFw 与部分虚拟服务
  → 原管理器生命周期进入菜单
  → 原控件、资源、GPE 请求
  → 显式软件 GPE + Sony LZ77 CPU 解码
  → AP 输出消费者有锁复制、提交 HOSDAP02、发送完成通知
  → 标准 fw_cfg / ramfb → QEMU GTK 窗口

GTK 鼠标/键盘 → 私有输入通道 → 原输入队列 → 固件处理 → 新提交画面
```

这是 **P01 菜单显示调用路径**，证据为 E01、E03–E08。未恢复的正常拍摄启动不能从这条受控菜单路径反推为已完成。

每一层要有自己的判据：shell 提示符不证明 app 已启动；`init` 返回不证明资源已解码；`create_screen` 不证明像素已绘制；send 返回0不证明接收者存在；完成回调不证明硬件真的传输了图像；F12 截图不等于固件拍照。

从本成果目录启动当前菜单版本：

```bash
./start.sh
```

需先按 [BUILD.md](BUILD.md) 准备程序、按 [资产导入说明](runtime/analysis/ASSET-IMPORT.md) 导入自己取得的2.01固件。入口清除继承的实验变量，执行资产检查、构建和独占启动；不要复制历史十几项环境变量来覆盖默认配置。实验需要共享锁，**已有原生窗口运行时，新实验可能只是在等锁**，不能据等待时间认定 guest 驱动死锁。

## 专题：从最初黑屏到真实菜单

最初看不到菜单，**不是最后加一个显示开关就解决的**。过程中有几种外观相同、原因完全不同的“黑屏”：内核/应用没起来、应用只清空了位图、菜单像素生成了却没有提交，以及 QEMU 没有接入这份像素。最终恢复的是“原固件菜单 → 实际绘图 → 有所有权保证的提交 → QEMU原生窗口”这条链。

### 黑屏先按证据分类

| 看到什么 | 必须先查什么 | 不能据此得出的结论 |
|---|---|---|
| 无画面，串口停在早期故障 | Image/DTB/initrd地址、实际bootargs、timer/IRQ和驱动异常 | 不能先说是UI资源缺失 |
| 原appFw已运行，28个启动控件存在，只收到清屏 | 原生命周期、控件内容、完整notifyDraw/drawFramebuffer调用 | 不能说“内核没启动”，也不能说“屏已建所以菜单已画好” |
| off-buffer已有菜单，第二帧却不返回 | 输出接收者、帧gate、缓冲租期与消费通知 | 不能只发空ACK宣称显示恢复 |
| AP已提交菜单，QEMU窗口仍不显示 | 构建是否有GTK/SDL、是否实际配置ramfb、显示设备是否接到了该帧 | 不能把原生窗口与Sony显示协议视为同一件事 |
| 最外层MENU退出后新帧变黑 | shooting/boot生命周期和新提交的像素内容 | 不能冻结上一张菜单掩盖普通启动未完成 |

### 实际解决顺序、为什么有效、怎样验证

| 步骤 | 当时的阻塞与采取的动作 | 为什么有效与验证证据 | 尚存的实验措施 |
|---|---|---|---|
| 1. 先让原内核可靠启动 | 固定Image/DTB装载，修改DTB的 `/chosen/bootargs` 和initrd描述，保留内存预留，补GIC/定时器适配；未建模驱动按证据跳过 | 原镜像在此路径不采用普通 `-append`；正确地址和映射让原内核真实到shell，客体uname与标记求值证明执行。[E01] | DTB/板级模型是仿真适配，不能当完整Sony硬件；见§3–4 |
| 2. 让原应用及其依赖进入可观测状态 | 构建实验rootfs，按原依赖初始化IPC、Backup、DMM等；对缺失的CAPID/SA/HDMI等使用限定服务；修正加载顺序和日志盲区 | 完整guest日志证明appFw、Application_System_Init、uxengine_init2运行；修好哨点后能看到真实 `0x308/count28`。[E02、E03] | 部分服务仍为软件替代；完整普通boot未完成 |
| 3. 用原框架建立菜单，而不是手工画菜单 | 在受控时机调用原九参数入口 **`0x1e78870`**，把308启动屏一次切为346菜单controller，保留原析构、构造、init和消息泵 | 运行日志出现真实菜单ctor/init，原请求泵自然创建 **`0x2ca/count44`**，之后发出菜单绘图请求。[E04] | **这是实验菜单入口，不是正常冷启动恢复**；不重复apply来压住后续状态 |
| 4. 真正执行菜单像素操作 | 实现已核准的Fill/精确Copy，再显式启用Sprite、A8字体、混合、tile/伸缩；按真实参数和callback布局执行 | 原先只有清屏，随后真实请求产生“My Menu Setting”等文字；复制/模型像素测试及源字形互证。[E05] | 软件RGBA4444模型不声称与硬件逐像素相同；不支持模式仍拒绝 |
| 5. 把原资源解成可画的图片 | 发现31次原LZ77调用都因缺 `/dev/ldec` 失败；用同格式CPU解码读取原UXC，通过原位图锁写入完整结果 | 九样本逐字节匹配参考实现，真实菜单31/31解码成功；原背景、图标、橙色选中条和按钮出现。[E05] | 替代的是指定LZ77硬件路径，不是伪造设备成功或人工补图 |
| 6. 恢复帧消费与完成，而不只保存一张离屏图 | 对缺席的LIRO接收任务提供显式AP-only headless端；按1002→1003/1005配对，保护Bitmap引用、生命周期和像素读锁；复制后提交HOSDAP02，再通知完成 | 第二帧正常返回，原双缓冲交换，AP收到完成；1005引用已提交的私有保留帧，以 `pixel_source_seq`区分新像素；完整容器校验通过。[E06] | CP视频/widget合成和实体面板仍未模拟；仅消费AP表面 |
| 7. 把已提交像素交给QEMU真正显示 | 构建GTK/SDL，`ramfb-viewer`校验提交容器后转换为XRGB8888，通过标准 **fw_cfg DMA / `etc/ramfb`** 配置独立scanout | QMP显示设备截帧与AP像素转换逐点一致；GTK实际窗口也匹配，所以不只是host另开图片查看器。[E07] | 这是仿真显示桥，不是Sony实体面板驱动 |
| 8. 再验收真实交互和完整启动入口 | 原生按钮/键盘经私有通道进入原输入队列，修正原键码和1/0短按协议；干净ALLLANG、离线重建及一键启动合并验证 | 九步实际窗口与各次AP快照逐像素一致，正常退出；日期故障另以123步重放回归。[E08、E09、E11、E12] | 其他未验收菜单、控件和拍摄链仍不宣称可用 |

### 三张菜单图代表不同的证据层

以下是**工作区历史图像**，位于 `analysis/qemu-boot-research/screenshots/`，不假定源码包包含这些生成文件：

| 文件 | 当时真实做到的事 | 仍不能声称什么 |
|---|---|---|
| `05-menu-offscreen-software.png` | 原控件/字体和绘图请求形成可读菜单文字 | 图片缺失、文字有重叠，离屏可读不等于帧完成或原生显示 |
| `06-menu-images-software.png` | 加入真实LZ77资源后，背景、图标、选中条完整 | 当时输出完成链仍未闭合，不能称已交付给显示端 |
| `07-menu-headless-committed.png` | 来自锁保护的私有快照和完整提交容器；原生第二帧返回、交换与完成通知另有日志互证 | AP-only软件输出不等于CP合成/实体面板或完整相机启动 |

早期01–04是控制台、黑帧或诊断说明图，**都不是成功菜单**。07与06可有相同图像内容，但证据强度不同；后来ramfb/QMP/GTK逐像素验收又证明这份提交真正到了QEMU显示设备。仅看“图长得一样”或“发送返回0”会漏掉这些区别。

**现在退出菜单后的黑帧也不是旧显示问题复发。** 正确MENU输入使原固件进入shooting后回到尚未完成的boot，系统提交了新的空AP图，ramfb如实显示它。当前需要继续恢复媒体首次状态、power-on和序列初始化（§16），不能只调GTK、改分辨率或保留旧菜单图片来解决。

## 3. 原内核启动不了，修改命令行也不生效

- **现象**：通用 `virt` 板上早期故障；改 `-append` 后实际 `Kernel command line` 没变；把原 initrd 当 cpio 处理也不对。
- **根因与证据**：原内核在这一镜像的非 QEMU 启动路径使用特定高地址。日志为 `invalid cmdline override @ 0x400001000. Using default cmdline.`，回落到 DTB `/chosen/bootargs`；原 `initrd.img` 是 ext2。Sony IRQ 辅助代码还要求固定 GIC 虚拟别名。[E01]
- **解决动作**：Image 放 `0x400080000`、DTB 放 `0x400002000`；修 `/chosen/bootargs`、initrd start/end；增加 GIC `iomap`（物理 `0x08000000` → 虚拟 `0xfffffffff13a1000`）。历史最小实验 initrd 放 `0x440000000`，必须与 DTB一致；厂商 `loadaddr.txt` 则给出 `0x408000000`。两者不可混搭。
- **验证**：三次最小启动及对照实验；客体真实执行 `uname`，输出 `4.9.337-scel+ aarch64`；`BOOT_MARK_$(uname -m)` 展开为 `BOOT_MARK_aarch64`，不是主机拼出的日志。
- **限制**：历史 `-m 24576` 是为了通用板 RAM backing 覆盖 Sony 高物理地址，**不是相机有24GiB内存**。最小 ext2 initrd→shell 与当前菜单的生成 cpio 根文件系统是两种启动档位。早期报告一处“内核走 QEMU 分支”的解释与后续源码/实测不一致，不能照抄该句。

外层保持 `bubblewrap` 隔离、只读原资产、可写独立输出、`-nic none`；图形版只额外绑定本地 X11/Xwayland 所需 socket、认证与字体，不开放真机设备或网络。

## 4. 驱动与 IPC：缺符号、Oops、“没有发送回调”不是同一种错误

- **现象**：去掉 initcall blacklist 后 timer Oops；补 timer 后 GPIO 再 Oops；附加模块报 `Unknown symbol Backup_read…`；运行中有 `No snd_msg registered`。
- **根因与证据**：最小 DTB 未映射 Sony 寄存器；随后存在未建模外设。另一批失败来自 harness 把 input/ioexp/indctr/udate 放在 Backup 之前，indctr 又早于 ioexp，并不是所有失败都源于硬件。[E01、E02]
- **解决动作**：最小启动跳过7个私有 initcall；应用路径按原脚本依赖重排 Backup、DMM及附加模块。按模块各自证据保留或跳过，不以统一假成功替代。
- **验证**：顺序修复后 input/ioexp/indctr/udate 等从缺符号变为真正加载成功。进一步暴露 USB 的 `usb_otgcore_set_vbus+0x20` NULL访问，以及 MMC 的 `mmc_os_read32+0x4` 访问 `0xfffffffff13b8000`。`safe` 配置排除这些控制器后恢复稳定；MS 属因未建模预先排除，不是已观测其崩溃。
- **限制**：`pcieatu` 等仍依赖真实 PCIe 环境；仅 insmod 不能恢复 CP。成功菜单对照采用已验证核心模块，不代表原38模块和完整设备启动都通过。

应用的 CAPID、SA、HDMI、DMM、身份及显示信息等缺口使用明确的实验服务处理。服务必须逐项核对请求和实际行为；缺少公共发送桥的日志不能单独定位“菜单卡点”。尤其：

| 早期判断 | 后续纠正 |
|---|---|
| `0x811006e9` 是无人应答的启动订阅 | 它与 `0x81106114` 属 `kikilog.ko`；`sid=0x2a/sublogid=0xf02…` 是日志注册，内核消费不经过用户态接收钩子 |
| 对 `0x81106114` send 返回0表示投递成功 | 原内核报 invalid qid，不能当作消费者收到了消息 |
| 重复发送 BB ready 会推动建屏 | `{0x2003,0x8201,...}` 所涉 promise 属录像编码器；重复兑现可触发 `future_error(2)`，默认注入已关闭 |
| WBI有很多队列、观察器只有一条，所以模型未建立 | 全局内核表与单进程钩子覆盖范围不同；该数量差不能直接当模型实例数或独立成功判据 |

## 5. 探针和反编译地址错误曾伪装成固件故障

- **现象**：summary说 appFw 未加载、没有建屏、没有注册；手工调用后崩溃；工具给出的符号和反编译不一致。
- **根因与证据**：观察器窗口只有前64KiB，`tail -40` 又截掉注册记录；函数地址混用了文件偏移、ELF VA和 Ghidra 导入基址；入口跳板漏保留 LR/参数寄存器。[E03]
- **解决动作**：guest完整日志做 census，区分镜像重复行；库反编译使用程序身份及 imageBase，Ghidra库项目通常加 `0x100000`。入口桩保存 `x0..x8/x30/q0..q7/NZCV`、对齐栈、重放原指令并同步 icache。`init` 从对象的 **vtable+0x20** 读取；建屏 active/returned 门控避免在函数内部误重入。
- **验证**：AArch64实测保留整数/浮点/栈参数及返回地址。`view::create_screen` 的虚表槽位是 **VA `0x58dbf38`**，旧 `0x58cbf38` 是文件偏移；运行中对象vptr为 `appFw+0x58dbf28`，不要把方法槽位与对象vptr混用。修正地址后自然观测到 `create_screen(id=0x308,count=28)`。真正 BitBlit PLT 是 libObj **`0x59d40`**；旧解析漏乘64位 LDR 偏移缩放，错认了 `0x5b2b0` 字符串桩。
- **限制**：修正前的“从不建屏”“某函数窗口外必崩”“内存含vptr即为活对象”均不能继承为事实。盲扫内存既可能误认对象，也会干扰时序；需用构造捕获、对象字段及原生命周期互证。

观察器曾使用新编译器 outline atomics，产生 guest libc 不含的 `__aarch64_ldadd4_relax`；构建增加 `-mno-outline-atomics`，按原 glibc ABI验证。新 `.so` 编好但未重打 cpio 同样会运行旧探针，因此启动器默认自动重建两者。

## 6. 只建启动控件，如何正确进入菜单

- **现象**：初始只有屏集776（`0x308`）、28项对话框/启动控件，GPE只有清屏；直接构造菜单或拿 presenter 调 controller 方法会崩。
- **根因与证据**：两个 dispatcher 不同：`0x1e4bacc` 调工厂表 `+0` presenter，`0x1e4bb68` 调 `+8` controller。表项 `+0x14` 是 workspace索引，不是屏ID；`0x308` 和 `0x346` 共用index0池，裸构造会覆盖存活对象。捕获到的 `*(manager+0x38)` 也不能用减 `0x40` 反推manager。[E04]
- **解决动作**：使用固件自身 boot 已调用的完整九参数生命周期入口 **`0x1e78870`**，在原UI线程自然首帧返回后、有前置检查地请求菜单 `0x346`。保留退出、析构、清槽、构造、登记、虚init与后续请求泵；不强写模式值。
- **验证**：`run-fjol_vt0` 实际记录 `MENU_APPLY_ENTER/RETURN`、菜单ctor、`MODE_SLOT4 mode=menu`，随后原请求泵创建 `0x2ca/count44`。当前菜单导航是在这些真实控件上进行。
- **限制**：这是受控菜单入口，**不是正常 power-on 全链完成**。早期“只能 init 内调用”“只有 CP ready 才能建菜单”等结论不能作为现状。取消 `MENU_APPLY_MENU` 的 `run-w3yln924` 仍停 `0x308`，故过早切菜单也不是普通启动停滞的唯一原因。

## 7. 菜单已激活但缺文字、图片或只有清屏

### 7.1 GPE 操作未实现或参数解析错误

- **现象**：菜单第二帧产生69条不支持位图请求，旧后端遇到 BitBlit后还永久标记失败；只直调 `notifyDraw` 仍无完整画面。
- **根因与证据**：完整帧应依次 `notifyDraw → drawFramebuffer`。BitBlit参数144B，真实生产者是 libObj `grm::CmdRectDraw::stage1`；callback在 **`+0x80`**，不是Fill的 `+0x28`。源图和临时图实际在 `0x410…`，旧地址白名单过窄；普通同尺寸CME不读取scale，0也可合法。[E05]
- **解决动作**：先实现有几何、stride、范围、重叠检查的同格式复制；再以显式开关启用 RGBA4444、A8字体着色、source-over、tile、nearest伸缩。未知模式/非支持callback仍拒绝。实际写入后才按原格式完成，不能“无条件完成”解锁流水线。
- **验证**：宿主ASan/UBSan与ARM64像素测试、真实请求回放；69条请求由10条精确copy和59条显式软件模型覆盖。真实字形缓冲出现“My Menu Setting”，之后整屏文字由原控件绘制。
- **限制**：硬件混合舍入、预乘约定及全部格式映射尚未逐像素闭合。软件模型画面对比证明桥接一致，不证明与真实相机硬件相同。

### 7.2 背景/图标全零，文字叠在一起

- **现象**：资源文件都在，文字可读，但背景、图标缺失，部分文字重叠。
- **根因与证据**：成功打开三份 `image_00/01/02_32_IPSX.uxc` 和字体；动态记录31次真实 format7 LZ77解码都因 `/dev/ldec` 不存在失败。文件存在不等于解码成功，请求后零缓冲也可能被复用，须以调用链证明。[E05]
- **解决动作**：移植已有 MIT Sony LZ77算法，检查块终止、字典回距、短读、输入消费及精确输出尺寸；通过原位图锁/解锁写真实解码像素。没有创建假设备或将失败直接改成成功。
- **验证**：9份原资源，8361B输入→50976B输出，与Python参考逐字节一致；`run-854thlgz` 31/31成功，写入1,244,864B。图片恢复后背景、侧栏图标、选中条出现，旧重叠消除。
- **限制**：只验证指定资源格式/路径；LDEC真实DMA驱动和其他硬件解码能力未恢复。交付源码为 `runtime/analysis/sony-lz77.*`；本次核对时 `test-sony-lz77.c`、`test-software-lz.py` 仍在工作区历史RA目录，未随本成果提供，不能声称仅取公开包就能运行这两项历史测试。

## 8. 像素已经画好，第二帧却一直等待

- **现象**：离屏菜单存在，`drawFramebuffer` 第二次不返回；发送返回0，但下一帧不能完成。
- **根因与证据**：完整日志显示 ready信号量 **`0x02886047` 已返回**，随后等的是 `ExternalOSDIF` gate **`0x02886044`**，不能混为一把锁。原帧完成链经 `grm_gpe/CmdGpeFlush → 同步Flush → callback → 交换缓冲`。输出74d/84d内核队列积压20/1条，客户端数均0；已消费的本地队列则0条/1客户端。[E06]
- **原生接收端**：`av-cam.bin` 中 LIRO task147先收 `0x8280074d` 控制，再按1002内容接 `0x8280084d/84e` 的1003/1005，最后发6003/6005。原路径还依赖跨芯片传输等，不是向任意队列回包即可修复。
- **解决动作**：显式 AP-only headless后端实现配对、冻结metadata、真实帧身份检查；1003复制当前有效AP像素，1005只能引用上一成功保留帧。文件完整提交并读回后才通知消费完成；超时、格式错误、提交失败停止且不ACK。
- **像素租期**：gate关闭和前后descriptor相同都不足以证明稳定。采用 FB+90生命周期锁、Bitmap AddRef/ReadLock和所有软件Fill/Blit共用的raster锁，复制到私有缓冲后逆序释放；文件IO/ACK在锁外。退出前stop/join，防止边析构边读。
- **验证**：并发writer/resize、busy回滚、shutdown join、故障提交等离线测试；`run-2w0kuchf`实际第二帧返回、双缓冲交换、AP收到完成。`run-0qkp_2mp`的完整容器得到已提交菜单截图，而非事后读取可能变化的显存。
- **限制**：这解决的是软件消费者的契约，不等于原 LIRO/CP/面板恢复。metadata-only事务不能计作新绘制；以 `pixel_source_seq`识别像素来源。`grm_fin`入口多指令补丁与并发teardown的安装竞态仍有P2限制，目前只作冷启动实验，不支持热接管。

HOSDAP02将128B头、24930B元数据和614400B像素绑定为同一639458B容器，按同一inode读取、完整校验后导出。原子rename保证读到一份提交，不会把两个事务拼成一帧。

## 9. 为什么要接 ramfb，而不是简单打开 QEMU 窗口

- **现象**：旧QEMU仅有none/curses；启图形窗口也不会自动看到Sony的私有OSD。
- **根因与证据**：Sony应用不向标准PC虚拟显卡画图，GTK/SDL只是宿主显示后端，仍需明确像素入口。[E07]
- **解决动作**：离线构建GTK/SDL；独立 `ramfb-viewer`校验已提交容器，将RGBA4444黑底合成为XRGB8888，再通过标准fw_cfg DMA配置 `etc/ramfb`。使用单独预留区 `[0x41e100000,0x41e600000)`，不覆盖原固件/CP/DMM池。
- **验证**：宿主、ARM64及原glibc2.25测试；原Sony内核通过 `/dev/mem` 访问fw_cfg、DMA配置确实执行。QMP截帧与AP转换结果逐像素一致；最终GTK实际画布的九步验证也一致，正常Quit返回0。
- **限制**：这是真正QEMU窗口，但显示的是已声明的软件AP输出；无CMOS取景或CP图层合成。F12是界面快照，不是快门拍照。

## 10. 键送到了 UI，却没有想要的动作

- **现象**：旧MENU消息到UI主循环，屏集/像素不变；方向键一次跨多项；点击后键盘失焦。
- **根因与证据**：旧 `0x14000102` 被原表映射成 `0x306`，不是当前UI的MENU；普通键的按下值应为1、释放0。长按1秒触发固件连发；GTK事件/焦点与固件输入还分属不同层。[E08]
- **解决动作**：核对原重映射和handler后使用下表；普通键guest 50ms短按，拨盘单发有符号增量，不附加假释放。QEMU设备只绑定指定ramfb console；抑制held repeat、未知键、暂停/断线输入；点击后恢复画布焦点。

| 操作 | 原始键码 | 事件 | 已验证效果 |
|---|---|---|---|
| 上/下/左/右 | `0x3 / 0x30 / 0x15 / 0x1af` | 按1、放0 | 项目移动、回卷、分类/项目列切换 |
| 确认 | `0x1ac` | 按1、放0 | My Menu → Add Item；**不保证所有确定入口正常** |
| MENU | `0x1ab` | 按1、放0 | 返回上层；最外层退出进入未完成boot/shooting |
| 删除/C2 | `0x1f8` | 按1、放0 | 菜单帮助页，不是已验证删除照片 |
| 右后拨盘左/右 | `0x10000013`（REAR2） | 单发 `-1024/+1024` | 项目变化/回卷，键盘PageUp/PageDown |

- **验证**：七按钮及拨盘双向的GUI测试覆盖焦点、暂停、断线、正常退出；`check-xqunv9ei`九步为启动、Enter、四方向、PageDown、PageUp、MENU，每次窗口与AP像素一致。
- **限制**：播放、C1、REC、模式档位/三档开关尚未完成验收。模式是位置事件、拨盘是增量、普通键是press/release，不能统一发送；C1还有自定义绑定。**九步验收是场景覆盖，不是穷举所有菜单。**

## 11. 已修复病例：日期设置按确定退出，随后队列满

### 11.1 根因与修复：原日期驱动还需要启动时钟初始化

- **现象**：在 `Setup → Area/Date → Area/Date/Time Setting` 按确定，固件进入 `Exception>`；随后重复点击得到“已发送，未确认画面变化”和“队列已满”。
- **根因与证据**：用户原run `run-e55ghxim`及重放 `run-ondy2ti8`均进入异常监视器。定位的目标是 `0x2d3 / scr0103_0002_controller`；原日期读取向 **`0x0090013d`** 发同步请求，未提供有效日期，model更新路径保留/写入全零日期。地区转换在 **`appFw+0x2524f98`** 进入断言路径，函数为 `get_date_time_by_changing_current_area`，`model_api.cpp:0x5d03`。[E09]
- **已经完成的动作**：按原用户操作重放，保存确定前画面、异常日志，核对原日期读取/模型写入/转换链。没有删除assert、把零日期当合法值，或返回伪成功。
- **仅加载驱动的阴性对照**：在独立目录只读复用输入资产，`run-rj3zudqn` 明确输出 **`UDATE_INSMOD_STATUS=0`**，随后按115步输入序列重放仍出现同一断言。因此不能仅归因为“未加载udate”；模块加载返回0不证明日期服务的数据已初始化。
- **进一步的实际IPC取证**：`date-runtime-w87gi_9b` 中，原服务命令2返回 `transport=0/status=0`，但当时日期为 `0000-00-00`、原始flags=3；命令3正确返回原Backup默认日期 `2023-01-01`、flags=0。这把缺口收窄到原启动时钟初始化，而非按键投递或日期服务注册。
- **实际修复**：启动器加载原 `udate.ko`，再由 `seed-date-clock.c` 读取命令2当前时钟；仅当它处于已核实的未初始化状态3时，读取命令3的原固件默认日期，通过原命令0、`BY_BOOT=4` 初始化UTC基准。随后读回合法年月日、检查原计时器推进。失败时阻止camera app启动，不改app model、断言或成功返回值；已有有效时钟不重置。
- **验证**：原时钟读回 `2023-01-01 00:00:00 → 00:00:02`；最终目录 `run-zthj5hky` 重放用户115次操作，再执行返回、重进、进入日期编辑屏 `0x98`、返回及再次进入，共123步，结束时仍running、error为空、pending=0。图像与结果保存在 `runtime/analysis/input-replay-check/replay-q8r0gl6o/`。最终GTK九步像素验证 `check-ewrx7bd2`、11项host/mock故障监控测试及ASan/UBSan时钟协议测试同时通过。
- **边界**：本次从固定ALLLANG的默认UTC日期 **2023-01-01** 起步，不代表自动同步主机时间或跨VM保存时钟。已验证打开日期设置、进入编辑页、返回及重进；尚未把日期编辑“保存到RTC/FEC”、所有地区/夏令时组合或全部菜单项声称为通过。新helper明确拒绝不合法日期、未知状态、未处理响应、非受支持默认时区和不推进的时钟。
- **仍需区分的等待**：先前共享runtime的原udate实验曾在等待用户打开的原生VM释放锁；这不是guest驱动hang。独立目录对照给出了实际加载结果，但没有把该故障修好。
- **边界**：这是日期服务/有效数据初始化问题，**不要混同 §16 的 `sequence_still_record(0x4bd)` 空对象mutex异常**。原按键协议及其他菜单入口可用，不能因此宣称确定键传输整体错误；也不能因其他入口通过而忽略这条断言。

### 11.2 已修复的次生问题：崩溃后继续积压输入

- **根因**：只在单次串口命令内部找异常，会漏掉两次点击之间的异常；后续仍收命令，等90秒超时才停止，队列随后满。
- **修复**：`menu_live.py::FirmwareMonitor`持续增量读完整console，保留跨chunk尾部并处理日志截断；live与host调用它检查 `Entering exception monitor.` / `Exception>`。发现异常转失败、停止收键、清pending，不让队列继续掩盖原故障。
- **验证**：主会话记录11项host/mock检查通过；真实重放及时报告固件异常、`pending=0`。本次整理核对了实现和测试断言，没有重新执行测试。
- **限制与使用建议**：异常监视只处理故障可见性和队列收尾，不能救活已经assert的guest。遇到其他未恢复功能的异常应保存本次日志，用启动器正常停止/重新启动；不要反复点击或直接删除共享锁。当前日期入口修复也需要重新启动会话，不能热修复已加载的旧guest。

## 12. 看似系统卡住，实际是启动/采集基础设施错误

| 现象 | 根因与修复 | 验证及限制 |
|---|---|---|
| 深目录/带空格目录启动失败 | AF_UNIX路径过长；打开run目录fd后用 `/proc/self/fd/…` 短路径连接socket，路径严格传参 | 干净ALLLANG新目录、长路径、从不同cwd启动通过 |
| 两次启动互相覆盖observer/cpio | 以前构建在锁外；现从构建前持有flock，fd9继承并核对inode/device | 构建与运行共用锁；旧锁文件mtime不是持有证明；运行中的VM不应被误杀 |
| “只记录”实验其实执行了调用 | `MENU_OPEN_SET_LOG_ONLY`未转发；已补并在启动自报关键配置 | 验证开关实际到guest；旧run不能补称log-only |
| guest结束但host等不到完成 | 空KikiLog输出粘住完成标记、无head等applet、shell prompt被前一命令遗留 | 独立换行完成标记；每条诊断有唯一RC sentinel，旧prompt/回显不算完成 |
| Base64快照偶发损坏 | 内核或特定headless状态日志插进串口导出 | 仅剥离固定已识别记录，仍严格Base64、精确长度、全容器校验；未知噪声拒绝 |
| 新代码“没有触发” | 只重建so未重打cpio，或console镜像前缀漏配 | 默认重建observer+cpio、核对run输入身份；不用旧镜像推断新逻辑 |
| 崩溃前记录不完整 | stderr落文件、kmsg限流、前64KiB窗口/截断grep | 高价值记录直达console，完整census；同一记录可能镜像重复，不能双计 |

源码入口为 [menu_live.py](runtime/analysis/menu_live.py)、[menu_control.py](runtime/analysis/menu_control.py)、[headless_osd_capture.py](runtime/analysis/headless_osd_capture.py) 及 `run-menu-probe.sh`。[E10]

后台任务启动不等于guest已启动；需先看构建、锁等待、QEMU进程、guest日志分别到哪步。相反，任务退出0也不证明交互成功，要核对本次提交与画面。收到延迟通知时按run身份归档，不重复解释旧结果。

## 13. 为什么不能把研究目录直接打包给同好

- **现象**：研究rootfs含本机链接、私有setting/Backup、修改IMDB与旧helper，拷走可能缺依赖或泄露设备数据。
- **解决动作**：在唯一final目录维护交付；导入器从固定2.01镜像与FDAT头生成干净根，使用公开固件ALLLANG默认设置。原Sony资产、派生IMDB、身份卷、项目overlay分开标记。十项IMDB掩码清除7是实验变更，不称原厂文件。[E11]
- **审查修复**：schema1可被改manifest绕过非锚点检查、遗漏新增私有setting和执行权限。schema2用源码固定摘要绑定1290项资产，另加明确overlay，检查hash/size/mode；seed仅允许固定7文件，host生成root的setting必须为空，guest链接按guest根解析，不越到宿主。
- **验证**：独立新目录、空格路径、严格umask/时区下真实导入，9项回归通过；IMDB精确10项变化、身份FAT固定摘要。完整原生窗口曾在独立干净ALLLANG目录通过。
- **限制**：manifest不是数字签名，不能防同时改工具源码的攻击者；失败保留staging/部分安装，不承诺自动回滚。生成rootfs、Sony库/字体、设备dump、运行日志不自动属于可公开代码。

复现只读资产检查（从本成果目录）：

```bash
python3 runtime/analysis/prepare-assets.py --runtime runtime --check-runtime
```

首次导入与有意篡改负例测试的约束见 [ASSET-IMPORT.md](runtime/analysis/ASSET-IMPORT.md)。不要在用户运行中的runtime执行会短暂篡改资产的测试。

## 14. 一份 timer 补丁不足以重建当前 QEMU

- **现象**：本机程序能运行，但早期只保留timer改动，缺virt/bus/spacc、输入和GTK变更；新构建没有图形后端。
- **解决动作**：对固定QEMU基底导出完整11文件差分，实际apply后逐文件比对；附离线DTC源码、许可和manifest。`build-qemu.sh`使用无网络bubblewrap独立构建，最多8 job，测试staged程序后安装。[E12]
- **构建中遇到的问题**：上游EDK2含一个未用的绝对X11链接，安全解包会拒绝；只对该精确条目跳过，其他继续data filter。错误测试布局造成libpulsecommon不可见，改为符合实际隔离绑定的验证，而非关闭依赖检查。
- **验证**：完整离线重建已通过，GTK/SDL、Sony输入设备、user-QEMU与libfdt均检查；`source/build-programs.sha256`记录本次产物。最终重建程序完成九步实际窗口验证。
- **限制**：不声称跨发行版/编译器的bit-reproducible构建；宿主动态库仍需安装。只提高Ghidra CPU数也不会弥补错误基址或每请求重复导入；现使用持久项目、read-only/noanalysis查询，并以已知xref做阳性对照。

完整命令和许可证以 [BUILD.md](BUILD.md) 为准：

```bash
./build-qemu.sh
sha256sum --check source/build-programs.sha256
```

## 15. 原文件系统已经真实写盘，但不等于拍照

- **现象**：想保存到主机，但真实MMC控制器未建模；直接运行Fsys测试最初缺 `_ZTIN3MWF12EventHandlerE`。
- **根因与解决**：补原 `libMWF.so` 依赖，保留原Fsys；原MediaCommon自带普通文件dummy backend。独占实验先执行原参数init，再显式选择 `EXISTMEDIADRIVER=0`，且 `OMIT_WRITE=0`；原 `/dev/mmca` 对应guest普通文件 `/image_mmca`。[E13]
- **验证**：`run-k4pzww2c` 第一个原固件进程Mount/Create/Write/Flush/Close/Read/Unmount，第二个独立进程重新初始化、挂载并读回4096B一致；全4MiB镜像guest/host FNV一致，主机FAT目录/内容解析和 `fsck.fat -n` 均通过。
- **修过的测试问题**：早期 `run-vxhbaums` 内容正确但模板空卷标不符合fsck。修的是输入模板，再由固件完整重跑，不是修改旧输出伪造通过。
- **限制**：这里写的是确定性测试模式，不是JPEG/MP4；直接root挂载不证明MediaDetect/RM首次状态、原MPR/编码/快门链完成，也不证明掉电持久化。

复现、镜像及文件SHA见 [STORAGE-PROOF.md](STORAGE-PROOF.md)，不要把此结果引用成“已经拍照/录像”。

## 16. 退出菜单仍无主界面，以及另一种模式切换崩溃

- **现象一**：最外层MENU退出进入shooting，随后回boot，提交新空AP帧，窗口黑色。
- **证据与当前理解**：`rep_shooting_controller::init` 检查model17dd，未满足时请求 `rep_boot`。17dd表示boot控制器已init，不是CMOS/CP ready。实测退出后它从0变1，但两slot首次媒体状态flags仍0，boot保留目标343。slot相关内部命令395c/395d是media_inserted/media_eject，不能当wire事件随意发送。[E14]
- **现象二**：合法MAIN位置事件触发低地址mutex异常。
- **已定位根因**：mutex=`0x2a0`，父caller `appFw+0x349ea44`；`sequence_still_record::notify_initial_media_status` 对NULL的this加 `0x2a0` 后锁定。原lookup ID `0x4bd` 的对象初始化链尚未完成。诊断库只记录，仍调用原函数并保留异常，不把NULL锁改成成功。
- **已找到的正常路径**：`scr0039_boot_controller::process_power_on(0x209dd68)` → `boot_start_seq(0x2731660)` → 原请求链 → `model::create_sequence(0x24cc968)` → 工厂 `0x24836dc` → 分配/构造 `0x3369850/0x3368c98` → 分组登记调度。运行方法 `0x34adfc8` 还等首次媒体状态；部分后续状态生产者未闭合。
- **否定对照**：`run-w3yln924` 不强制切菜单，25秒后仍停308，未完成正常power-on。因此“太早进入菜单”最多是影响因素，不能写成唯一根因。
- **进一步定位**：原分派器 `0x1951844` 的类别 `0x01000369`、子索引 **`0x85`（十进制133）** 对应成员表 `0x592b3e0[0xe]`，目标 `0x209f300`。原事件为 **`0x01000090`**，producer是原主循环 `0x1799e8c → 0x24cb2d4`，`run-w3yln924` 记录了真实发送。新的 `run-3gciz6x5` UI线程栈进一步证明已经进入 `process_power_on → boot_start_seq`，停在 `model_future::wait(0x2e07044)`；等待前的原请求关联序列 `0x2a`。因此“没见到方法名日志”不能推导“没有派发”；当前缺口在后续完成条件。没有补发内部事件、直接调用私有callback或强制兑现promise。
- **尚未完成**：媒体RM SID `0x80d9016e` 的实际配置/状态/订阅、原MediaDetect通知与序列创建先后要继续闭合；原Fsys成功不自动解开这些门。主屏普通模式0/1/2及CP widget信息层也未完成动态验收。

长运行切 `0x309/count0` 的黑帧会如实显示；冻结旧菜单、反复apply或强写ready都不算主界面恢复。此问题与 §11 日期assert是两条不同链。

## 17. CP、CMOS、镜头、AF和编码链目前到哪里

- **原CP字节已经执行**：独立A32 QEMU从 `0x108000` 运行到 **`0x10897c: ldrb r1,[r0]`**，读取 **`0x03e42001`**，32MiB实验真实DataAbort，DFSR=`0x8`、DFAR一致；A15与A53/AArch32对照相同，装载镜像完整读回hash相符。[E15]
- **没有执行到**：runtime `0x619ea0`、RTOS或lens receiver `0x4b3a7c`。64MiB对照只核对default placement选择并在该读取之前停止，没有利用零填充伪造boot完成。
- **后续静态进展**：原NOR loader `0xfe0527b0` 读取Backup `fa0173` 并写EXEC_MODE字，已找到真实软件写入者；两个record提供 `0x403e42000/0x803e42000`。主会话执行了原字节取证脚本，地址/表项断言通过。低字节与DDR/XIP选择有关，第二字节来自loader context位；高地址与CP A32视图的别名、该位的来源及完整时序仍未闭合。见工作区 `analysis/cp-boot-probe/HANDOFF-CONTRACT.md`。不能把该字节叫作已证明的“镜像加载完成”，也不能清零后声称真实启动恢复。
- **AP静态接口**：已有focus命令 `0x4020 → 0x90030001`、lens/AF/位置通知和still/video编码请求入口，但接口存在不等于接收任务、算法、帧调度、时间戳和完成链可运行。
- **下一层验收**：虚拟CMOS需提供固件实际消费的帧；镜头/AF需由请求推进位置及反馈；编码后端需消费固件任务并把bitstream交回原MPR/Fsys封装落盘。独立host图片生成器、录屏或空ready均不能替代。

因此目前没有经过原拍摄链产出的照片或视频，也没有已验收的正常实时取景/对焦。

## 18. 被否证或需要撤回的“捷径”

| 做法/结论 | 为什么不能算修复 |
|---|---|
| 换真机Backup/setting、显示尺寸、DmmCfg或完整cmdline必然出菜单 | 做过相应阴性对照；它们没有让当时目标链前进，不能据配置看起来更真实就称成功 |
| 只看文件里的零、vptr命中、固定显存全零 | 可能是relocation/BSS、非活对象或另一块双缓冲；需运行身份与租期 |
| “未观察到”就是“没有发生” | 用户态钩子看不到内核消费，短日志遗漏后半段，错VA哨点不会触发 |
| guest命令/OSAL send返回0即为有效动作 | 可能无人接收、invalid qid、上层忽略错误；必须看消费者与内容 |
| 裸构造菜单、手拼manager、忽略NULL mutex | 会破坏共享池或掩盖缺失初始化；应恢复原生命周期 |
| 给未知队列空ACK/随意signal/重复BB ready | 能破坏所有权、时序或promise，只产生假进展或新崩溃 |
| 加载所有真机驱动就等于模拟真机 | USB/MMC/PCIe曾在应用启动前真实崩溃；硬件依赖必须逐项建模 |
| 九步导航通过，所以Enter在任何菜单项都安全 | 最新日期设置反例已否证；测试范围必须随报告一起给出 |
| Fsys写文件或F12截图证明已经拍摄 | 没有经过sensor/encoder/MPR/快门或REC完整链 |

## 19. 如何复核证据，而不是继承对话中的误判

### 19.1 路径与身份约定

下表中 **包内** 路径相对本成果目录；**工作区** 路径相对研究项目根。公开源码包不承诺附带所有 `menu-runs/`、大镜像、反编译数据库或设备dump；这些通常被gitignore排除。若只有源码，应按对应入口在自己的合法固件上复现，不能把缺少历史文件解释为已经验证。

本文函数地址除特别说明外为指定原库的 **ELF VA**，不是文件偏移，也不是运行时绝对地址；Ghidra库项目通常需减 `0x100000`。原appFw SHA-256：

```text
2ea02f7ecbc5bb917316fe8ef4ce5ffd87eb66efaae78ba2ce11420552df539b
```

原CP/av-cam是raw布局，`.ko`是ET_REL，不能套上述换算。生成资产身份以固定 `sony-assets.json` / schema2及构建manifest为准，不用旧生成so/cpio的hash替代本次输入记录。

### 19.2 Evidence → Finding → Path 索引

每节的“根因/验证”是对应Finding；本表给出至少一条可追踪Evidence。P01显示链见§2；P02日期故障链为“日期SID读取→零日期model→地区转换assert→持续监控停止收键”；P03存储链为“原文件backend→Fsys写入→第二进程重挂→主机结构/内容验证”。

| Evidence | source_ref / artifact_path | 支撑的Finding与路径 | 复核方式/限制 |
|---|---|---|---|
| E01 | 工作区 `analysis/qemu-boot-research/minimal-boot/REPORT.md`、`experiments/`；`board-model/REPORT.md` | 高地址、DTB、initcall；P01第一层 | 研究脚本 `run-minimal-boot.sh`；早期说明有已纠正语句，以实际日志/后续修正为准 |
| E02 | 工作区 `analysis/qemu-boot-research/runtime-survey-comparison.md`；历史RA `menu-runs/run-pfo3o_xx`、`run-ff2jd3bo`、`run-y_8fh61g` | 模块顺序与真实硬件故障 | 对照MOD_OK/Unknown symbol/Oops，排除shell命令回显中的OK/FAIL字面 |
| E03 | 工作区 `analysis/qemu-boot-research/PHASE-STATUS-2026-10-17.md` §24；历史RA `test-probe-abi.py` | ABI、地址、日志测量纠正 | 明确原参数/返回地址自测与整机日志是不同证据；该历史测试未随本成果提供 |
| E04 | 工作区 `analysis/qemu-boot-research/controller-path-review/REPORT.md`、`astra-menu-contract/REPORT.md`；历史RA `run-fjol_vt0` | 原生命周期成功；P01菜单层 | ctor/init/create_screen/后续帧联合核对，不只看apply返回 |
| E05 | 工作区 `analysis/qemu-boot-research/blit-layout-review/REPORT.md`、`menu-spc-semantics/REPORT.md`、`ldec-format/REPORT.md`；历史RA `run-854thlgz` | 实际绘图和资源解码；P01像素层 | 原样本对照+像素/边界测试；模型硬件等价性仍未知 |
| E06 | 工作区 `analysis/qemu-boot-research/liro-osd/REPORT.md`、`bitmap-lease/REPORT.md`、`headless-contract/IMPLEMENTATION-REVIEW.md`；历史RA `run-0qkp_2mp` | 输出积压、真实消费/提交；P01输出层 | 历史RA `check-headless-osd.sh` 与容器校验；该测试入口未随本成果提供，保留冷启动hook风险 |
| E07 | [DESIGN.md](DESIGN.md)；工作区 `analysis/qemu-boot-research/native-window/INPUT-AND-DISPLAY.md` | ramfb/GTK桥接 | QMP/实际画布与AP逐像素比较，不能只读提交日志 |
| E08 | 包内 `runtime/analysis/native-window-check/check-xqunv9ei/result.json`；工作区 `analysis/qemu-boot-research/extra-inputs/REPORT-V3.md` | 九步导航范围与原输入协议 | 独立GUI和真实固件场景分别检查，正常Quit=0 |
| E09 | 包内 `runtime/analysis/menu-runs/run-e55ghxim/console.log`、`run-ondy2ti8/console.log`（失败）及 `run-zthj5hky/console.log`（修复后）；`runtime/analysis/input-replay-check/replay-q8r0gl6o/`；工作区 `analysis/qemu-boot-research/enter-assert/{chain.c,producer.c,udate-main.c}` | 日期assert、原时钟初始化修复与次生积压；P02 | 原udate仅加载不足；最终123步重放通过。包内 `seed-date-clock.c/test-date-clock.c`、故障监控实现和测试可复核 |
| E10 | 包内 `runtime/analysis/{menu_live.py,headless_osd_capture.py,test-native-control.py,test-start-menu.py}` | 长路径、fd、导出和完成标记 | 本次命令sentinel和全容器校验均须通过 |
| E11 | [ASSET-IMPORT.md](runtime/analysis/ASSET-IMPORT.md)、`prepare-assets.py`、`test-assets.py`（后两者同目录）；工作区 `analysis/qemu-boot-research/native-delivery-plan/ASSET-REVIEW.md` | schema2审查修复 | 固定集合/权限/链接/私有seed负例；有意篡改测试仅在独占测试root |
| E12 | [BUILD.md](BUILD.md)、包内 `source/manifest.json`、`source/build-programs.sha256` | 完整离线重建 | `./build-qemu.sh`及清单检查，源码/宿主依赖按BUILD准备 |
| E13 | [STORAGE-PROOF.md](STORAGE-PROOF.md)、包内 `runtime/analysis/menu-runs/run-k4pzww2c/` | 原Fsys真实写入；P03 | 独占 `MENU_MEDIA_SELFTEST=1` + `verify-media-image.py`；无app/拍摄 |
| E14 | 工作区 `analysis/qemu-boot-research/still-sequence-init/REPORT-V2.md`、同目录 `v3-event-tables.txt` / `v3-event-map.raw.txt` / `v3-callback-table.txt` / `v3-dispatch-got.txt`，及 `analysis/qemu-boot-research/shooting-main/REPORT.md`；包内 `runtime/analysis/menu-runs/run-w3yln924/console.log` | 普通boot与序列4bd未完成 | 原创建链、事件分派及无菜单切入对照；没有第三轮报告或已修复结论 |
| E15 | 工作区 `analysis/cp-boot-probe/REPORT.md`、`cortex-a53-32m/result.json` | 原CP首个访问边界 | 独立probe脚本和SHA/寄存器/异常互证；未到RTOS/镜头receiver |

“历史RA”特指工作区 `analysis/qemu-runtime/analysis/`，仅是旧研究证据位置；当前交付运行代码以本成果 `runtime/analysis/` 为准，不维护第二套交付实现。

### 19.3 本次整理实际复核的摘要

以下hash为文档整理时对已有文件重新计算；不是本次重新执行实验。原始日志属于生成证据，分享前应另行审核是否需要脱敏；下表不要求公开包附带它们。

| 文件（按上表路径约定） | SHA-256 |
|---|---|
| 包内 `runtime/analysis/native-window-check/check-xqunv9ei/result.json` | `376a9dd3e4709393bced3cff52118fd31b887e39d431a019aa55be4eb297e8c1` |
| 包内 `runtime/analysis/menu-runs/run-e55ghxim/console.log` | `fe54d7197778409a95f73f4b97aec27b6b22541970c52d3086eda7bb911fed1c` |
| 包内 `runtime/analysis/menu-runs/run-ondy2ti8/console.log` | `12f853ae26631f6eaa09a3cd08a8472ae701824f65b662f5764ca77ae674ae67` |
| 工作区 `analysis/qemu-boot-research/enter-assert/date-runtime-sxe25prm/analysis/menu-runs/run-rj3zudqn/console.log` | `d4f6510c138364032d89be6e039473cb11edefb3bbb35e82fbe9f6635370f30d` |
| 包内 `runtime/analysis/menu-runs/run-k4pzww2c/media-disk.img` | `d2490baadea79eabbfda9e23bea5f70c2f821e41915d172d8603ed96fd5a7acf` |
| 包内 `runtime/analysis/menu-runs/run-w3yln924/console.log` | `ec08f93a7acfafbd3e836d373464baf0c8ecac1fe6d956a3e589a4f4172053c8` |
| 工作区 `analysis/qemu-boot-research/enter-assert/chain.c` | `8fd36a5b043679ff40e709fda3e22e0684fa56cc4cf6cfe2047248c77e277466` |
| 工作区 `analysis/qemu-boot-research/enter-assert/producer.c` | `3366b55381b657f9458fb474d1d1b856a290bc28cf0a78d1e38d56b0e6dbe0b1` |
| 工作区 `analysis/cp-boot-probe/cortex-a53-32m/result.json` | `2e48ba2896074bd3b253ca97f9cce7a75aafa3212f2ae6b3fc7720ff48ae1e84` |

## 20. 后续工作应满足的成功条件

1. **日期设置扩展**：已通过原时钟初始化、打开/重进及编辑页导航；继续验证修改日期的实际提交、地区/夏令时、时钟持久化。异常监控测试不能替代这些功能验证。
2. **普通拍摄启动**：原power-on和媒体首次状态按正常顺序推进，still序列可由原factory创建并收到通知，退出菜单进入有效主界面；不能靠写flags或跳过空对象调用。
3. **全部控件**：每个实体键、增量拨盘和位置开关分别有原映射、正确状态语义、适用上下文及动态行为证据。
4. **成像与存储**：固件发起并消费传感器/镜头/编码任务，原封装与Fsys生成可检查的照片/视频文件；测试文件和窗口录屏不能代替。
5. **可交付复现**：干净默认资产、离线构建、隔离原生启动、每项功能的验收日志及已知失败保持一致。原菜单截图是已完成成果，完整相机仿真仍是未完成目标。

# 某个 `.so` 损坏后，WiFi 还能不能起来

日期：2026-10-19。范围见 [scope.md](scope.md)。`flavor = null`。

本篇为 [SSH 研究成果](README.md)的恢复能力附篇，保留原 `.so` 损坏调查的代码证据。
这里讨论模块加载与网络控制面的静态容错边界；没有做损坏实验，
也没有证明应用损坏后一定能够联网或重新启动 SSH。

## 1. 结论

**分两种情况，取决于"损坏"是干净失败还是构造器崩溃：**

| 损坏形态 | `im.elf` | WiFi 控制面 | 判断 |
|---|---|---|---|
| `appFw.so` 缺失 / 被截断 / ELF 头或符号坏 → `dlopen` 或 `dlsym` 干净返回失败 | 加载循环继续 | 网络模块初始化阶段在应用阶段之前，不因这一失败被清理 | 保留初始化机会；自动关联未验证 |
| `appFw.so` 的 `DT_INIT` 在构造器里触发未恢复的 SIGSEGV | 整个进程终止 | 同进程网络控制线程一起终止 | 不能依赖此控制面恢复；已启动子进程结局未确认 |
| `libNetService.so` 本身损坏 | 视失败形态同上 | **直接失效** | 起不来 |
| `im.elf` 的 `NEEDED` 依赖损坏 | 进程根本起不来 | 无 | 起不来 |

必须区分干净加载失败与执行期间崩溃。现有静态证据只说明前者不会在该加载循环中直接终止进程，
不能推断实际损坏必然属于前者；构造器或运行期间的未恢复崩溃可能终止整个 `im.elf`。

## 2. 为什么：加载器不把失败当致命

`/usr/bin/im.elf` 自己就是模块宿主，用 `libIMDB.so` 的表 + `dlopen`/`dlsym` 装载模块。

**装载器不检查失败**（`im.elf@0x4064fc`，`0x48` 字节步长遍历表）：

```c
for (v2 = IMDB_get_entries() + a0*0x48; *v2 != 0x28 && a1 != *v2; v2 = &v2[0x12]) {
  if (a2 && (v2[2] & dat_419748) && (a3 & v2[1]))
    sub_406374(v2[3], 3, 0x94);      /* 返回值被丢弃 */
  a0 += 1;
}
```

**单模块装载的失败路径只有返回值**（`im.elf@0x405e24`）：

```c
handle = dlopen(file, dat_41973c);
if (!handle) return 0xffffffff;                 /* -1，不 abort */
...
v6 = dlsym(handle, ...);
if (v1 || !dlerror()) { if (v6) (*v6)(*v2); v8 = 0; }   /* .init 返回值被丢弃 */
else { v8 = 0xfffffffe; dlclose(handle); }      /* -2，只关闭自己 */
```

复核命令：

```sh
R=analysis/qemu-runtime/analysis/menu-root
kuna decompile "$R/usr/bin/im.elf" 0x4064fc --addr
kuna decompile "$R/usr/bin/im.elf" 0x405e24 --addr
kuna decompile "$R/usr/bin/im.elf" 0x4026bc --addr
```

## 3. 顺序：网络先于应用

`im.elf@0x4026bc` 先 `osal_wait_thread_exit()` 等 INFRA 初始化线程退出，之后（`0x40271c`）才进入应用阶段。
`libNetService.so@0x5c4a4` 创建网络线程、`0x623dc` 起按消息驱动。

所以常规目标匹配时，**NetService 已经先被初始化过**；随后 `appFw.so` 装载失败不会把它清理掉。

IMDB 原始记录（零基索引，字段 `type/线程mask/target/ID/static`，已与
`analysis/output/system-dump/a7c2-usr.img:/lib/libIMDB.so` 核对相同）：

| 索引 | 地址 | 文件 | 字段 | init |
|---|---|---|---|---|
| 114 | `0x1fb30` | `libNetService.so` | `0x20000002/4/0xffffff9f/0x13e/0` | `netservice_init` |
| 151 | `0x20598` | `appFw.so` | `0x20000004/1/0xffffff9f/0x83/0` | `Application_System_Init` |

两条记录里 mask 的 `4` / `1` 用于调度筛选（`im@0x4065c8`、`0x402f94`、`0x4030a8`），
**不是已证实的"必需/可选"位**——所以"必需位导致退出"这条分支没有被证实存在。

## 4. 崩溃形态为什么不同

`readelf -dW appFw.so` 给出 `DT_INIT=0x165cfb8`。`dlopen` 会在 `dlsym` 之前就执行 `DT_INIT`，
**在同一进程内、没有子进程隔离，也没有局部 SIGSEGV 恢复路径**。
所以构造器里的崩溃不是"返回 -1"，而是直接终止 `im.elf` 及其网络线程。

其他库自身的信号处理、看门狗，以及已启动的守护进程会怎样，**未确证**。

## 5. 没有独立的自动关联入口

已查 `sbin/init*`、`sbin/initrc/*`、`bootin.elf`、可执行目录与离线 `/etc`：
正常链路是 `init2:64 → bootin@0x401cb4 → im`，**未发现不经 `im` 的自动关联入口**。
`nflasha10.img` 里 `*.sh`/`*profile*`/`*inittab*` 检索为零文件。

存在独立测试代码：`usr/bin/Wltestcmd@0x401e40` 调用 `wl join`（经 `lib/libSTMcmdexecute.so@0xc44`
本地 `fork/execve`），不经过 `im`——但它的自动触发**未确证**。

## 6. 边界

- "模块继续运行" ≠ "已经完成关联并拿到 IP"。`libNetService` 起线程与驱动、`wpa_supplicant`、`dhcpcd`
  是分离的进程，关联是否成功还取决于保存的接入点、驱动状态与用户设置。
- 本报告全部来自离线静态读取，**没有在真机上做任何"故意弄坏文件"的实验**，也没有装载或修改模块。
- 未确证项：崩溃后的看门狗/重启行为、其他库的信号处理、`Wltestcmd` 的触发条件。

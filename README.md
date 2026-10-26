# MegaRAID Console

**给 fnOS 的 LSI / Broadcom MegaRAID 阵列卡监控与运维控制台** —— 浏览器里看清控制器、虚拟盘、物理盘、链路误码、事件日志和电池状态；需要动手时，在同一个界面完成蜂鸣器、定位灯、重建、磁盘状态等操作，危险动作要求重新输入账户密码。

![license](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)
![python](https://img.shields.io/badge/python-3.11%20stdlib-3776AB.svg)
![platform](https://img.shields.io/badge/platform-fnOS%20%C2%B7%20x86-lightgrey.svg)
![storcli](https://img.shields.io/badge/StorCLI-007.2705-informational.svg)
![tests](https://img.shields.io/badge/tests-32%20cases-brightgreen.svg)

纯 Python 标准库后端 · 零依赖手写前端 · 只开一个 Unix socket，不监听任何 TCP 端口 · GPL-3.0-or-later

> 本项目是第三方工具，与 Broadcom / Dell 无隶属关系。“MegaRAID”“PERC”等为各自权利人的商标，仅用于说明兼容对象。

<!-- 界面截图：放到 docs/screenshots/ 后在此插入
![overview](docs/screenshots/overview.png)
![battery](docs/screenshots/battery.png)
-->

---

## 目录

- [为什么需要它](#为什么需要它)
- [功能](#功能)
- [兼容性](#兼容性)
- [快速开始](#快速开始)
- [架构](#架构)
- [安全模型](#安全模型)
- [配置](#配置)
- [HTTP API](#http-api)
- [测试](#测试)
- [故障排查](#故障排查)
- [已知限制](#已知限制)
- [许可证](#许可证)

## 为什么需要它

fnOS 上的 MegaRAID 只能靠 `storcli` 命令行摸状态：`show all` 的输出几百行，误码、温度、重建进度要靠肉眼比对，定位灯和蜂鸣器还得记住 `set alarm=silence` 这类参数拼写。

这个项目把那套命令行包成一个只读为主、写操作有护栏的界面，并且处理了几件命令行不会替你处理的事：

- **两种备电设备都说人话**：9361-8i 这类 `CacheVault` 超级电容、PERC H840 这类 `BBU` 电池，`/cN/cv` 探不到就回退 `/cN/bbu`，界面用词随硬件切换
- **硬件不支持的操作提前置灰**：没有蜂鸣器（`Alarm ABSENT`）、直连盘没有 SES 机框（无法驱动定位灯）时，按钮禁用并说明原因，而不是让你撞一句 `Start Drive Locate Failed.`
- **危险操作逐次验密**：不是登录一次就放行，而是每次重建 / 离线 / 标记缺失都重新校验账户密码，并写入审计日志
- **不会把控制器压死**：采集单飞、后台任务期间自动放慢轮询、写入前检查是否有重建在跑

## 免责声明

本软件按“现状”提供，不附带任何担保。阵列运维操作（重建、离线、标记缺失、JBOD、外来配置导入 / 删除等）可能导致数据不可恢复；请自行确认备份与操作对象。作者不对任何数据丢失或业务中断负责！！

## 功能

**监控**

| 范围 | 内容 |
|---|---|
| 控制器 | 型号 / 序列号 / SAS 地址 / PCI 地址 / 板卡修订 / 生产日期 / 厂商与设备 ID / OEM / 安全 ID |
| 固件 | 固件包版本、CPLD、HII、Ctrl-R、Preboot CLI、NVDATA、Boot Block、芯片修订 |
| 硬件与缓存 | 缓存容量、NVRAM、Flash、FW 缓存、CacheVault Flash、TPM、接口、前后端端口、支持的磁盘与 RAID 级别、最大虚拟盘数、默认条带 |
| 健康 | ROC / 控制器温度与传感器状态、ECC 与内存错误计数、控制器状态 |
| 虚拟盘 | 状态、一致性、缓存策略、降级检测、后台任务 |
| 物理盘 | 槽位、序列号、温度、介质 / 其他错误、预测故障、SMART、固件、容量、逻辑与物理扇区、断言计数、托盘与机框位置 |
| 链路 | **SAS PHY 误码率实时增量采样**、重建 / 回拷 / 恢复进度与剩余时间 |
| 事件与备电 | 控制器事件日志（负缓存 + 单飞采集）、CacheVault / BBU 电池详情、学习周期 |

**运维操作**

蜂鸣器（开 / 关 / 静音）、硬盘定位灯（点亮 / 熄灭）、重建（开始 / 停止 / 暂停 / 恢复）、磁盘状态（上线 / 离线 / Unconfigured Good / 标记缺失 / JBOD）、一致性校验（开始 / 停止 / 暂停 / 恢复）、外来配置（导入 / 删除）、控制器参数（重建 / 校验 / 巡检速率、自动重建、PD 失效历史、活动灯定位、巡检开关）。

所有动作经过服务端白名单校验，编译成固定 argv 直接 `exec`；`danger` 级动作额外要求密码。

**1.0.6 亮点**

- **电池备电（BBU）支持**：先探 `/cN/cv`，失败自动回退 `/cN/bbu`，统一成一套模型并用 `kind` 区分；面板展示状态 / 电压 / 电流 / 温度 / 保持时间 / 设计容量 / 固件 / 厂商 / 序列号 / 生产日期 / 学习周期，学习周期单独提示
- **能力探测**：`/api/summary` 返回 `capabilities`（`alarm` / `locate`），硬件不支持的动作在服务端前置拒绝、前端置灰并给出原因
- **storcli 日志轮转**：`storcli.log` 达到 `storcli_log_max_mb`（默认 128 MB）后轮转为 `storcli.log.1`，只保留 1 份
- **修复写操作“假失败”**：定位灯、静音蜂鸣器等命令其实已下发，前端却在提交后读取已清空的 `pending`，误报 `Cannot read properties of null (reading 'target')`

完整历史见 [CHANGELOG.md](CHANGELOG.md)。

## 兼容性

| 项目 | 说明 |
|---|---|
| fnOS | ≥ 1.1.3100，x86 |
| Python | 3.11（系统自带）；后端只用标准库 |
| 阵列卡 | 实测 LSI MegaRAID **9361-8i**（CacheVault）与 Dell **PERC H840**（BBU）；背板拓扑 `/c0/eN/sN` 与直连拓扑 `/c0/sN` 均支持 |
| StorCLI | 需自行安装 Broadcom StorCLI（验证于 `007.2705.0000.0000`），默认 `/opt/MegaRAID/storcli/storcli64`，必须 root 拥有且同组/他人不可写 |
| 浏览器 | 任意现代浏览器；界面无框架、无构建步骤 |

## 快速开始

### 前置：准备 StorCLI

本项目**不包含也不分发** Broadcom StorCLI。请自行从官方渠道获取并放置：

```bash
sudo mkdir -p /opt/MegaRAID/storcli
sudo install -o root -g root -m 755 storcli64 /opt/MegaRAID/storcli/storcli64
/opt/MegaRAID/storcli/storcli64 -v      # 应输出 StorCli ... Ver 007.xxxx
sudo /opt/MegaRAID/storcli/storcli64 /c0 show | head   # 能列出控制器即可
```

### 方式一：应用中心安装（推荐）

把 `megaraid-console.fpk` 导入 fnOS 应用中心，或使用命令行工具（需 root）：

```bash
sudo appcenter-cli install-fpk ./megaraid-console.fpk
sudo appcenter-cli start megaraid-console
sudo appcenter-cli status megaraid-console
```

两个容易踩的点：

- **同版本不会重装**：已装 1.0.6 时再装 1.0.6，只会回一句 `[Info]Application [megaraid-console] is installed.` 并跳过。升级要版本号更高；强制重装需先 `appcenter-cli uninstall megaraid-console` —— 卸载脚本保留 `@appconf` / `@appdata`（配置与审计），只删除 PAM 服务文件（安装时重建）
- **新装落在默认存储卷**：`appcenter-cli default-volume` 查默认卷号，需要指定时用 `install-fpk -v <卷号>`；升级安装会保留原卷

### 方式二：从源码构建

需要 fnOS 自带的 `fnpack`（开发于 1.2.4）：

```bash
git clone https://github.com/emil1033628415/megaraid-console-fnOS.git
cd megaraid-console
fnpack build -d .
# 产物：megaraid-console.fpk（内含 app.tgz / cmd / config / manifest / 图标 / LICENSE）
```

`manifest` 里没有 `checksum` 字段：它是 `app.tgz` 的 MD5，由 `fnpack build` 每次写入（受文件 mtime 影响，同一份源码两次构建的值可能不同），因此不入库。包内的 `LICENSE` 由 fnpack 自动带入，分发二进制时请保持它在包内。

### 方式三：热补丁（开发用）

`tools/hotpatch.sh` 把仓库里的 `app/server` 与 `app/ui` 直接覆盖到已安装的应用上，自动备份、先跑测试、不调用 storcli：

```bash
sudo ./tools/hotpatch.sh --dry-run    # 先看计划
sudo ./tools/hotpatch.sh              # 安装并重启
```

这是调试手段，不是发布路径。正式升级请用 fpk。

## 架构

```
浏览器
  │  fnOS 网关鉴权，注入管理员身份（Remote-User）
  ▼
/app/megaraid-console ──► app.sock  (Unix socket, 0700)
                              │
                              ▼
                  server/raidwatchd.py  (root)
                     ├── raidcore.py    storcli 调用与 JSON 解析、日志轮转
                     ├── pam_verify.py  ctypes 调 libpam 做二次验密
                     └── storcli64 ──► MegaRAID 控制器
```

- **无第三方依赖**：`http.server`、`subprocess`、`threading`、`ctypes` —— fnOS 系统升级不会带走依赖
- **前端无构建步骤**：手写 ES5 风格 `ui/app.js`，`index.html` 里的 `__RW_BASE__` 由守护进程响应时替换，没有 webpack / vite / tsc
- **单进程单 socket**：只 bind 一个 Unix socket，以限制性 umask 创建
- **采集单飞**：多标签页或频繁刷新合并为一次采集（最小间隔 3 s），不重复压控制器

目录结构遵循 fnOS 应用规范：

```
app/        → 安装到 @appcenter（server/ + ui/ + config/）
cmd/        → 生命周期脚本（main / install / upgrade / uninstall / config）
config/     → privilege / resource
manifest    → 应用元数据
tests/      → 32 项回归测试（不调用 storcli）
```

## 安全模型

应用以 **root** 运行（访问控制器 ioctl 必须有 root），所以边界设计比功能更重要：

1. **不开放网络端口**：只有 Unix socket；身份来自 fnOS 网关注入的请求头
2. **危险操作逐次验密**：每个 `danger` 动作都用当前登录账户的密码过一遍 PAM。使用**专用** `/etc/pam.d/megaraid-console` —— 若复用 `su`/`sudo`，其 `pam_rootok.so` 会在 root 进程里短路整条认证链，任何密码都被放行
3. **storcli 路径校验**：必须存在、可执行、root 拥有、同组与其他用户不可写
4. **不拼 shell**：动作编译为固定 argv 直接 `exec`；目标路径必须匹配 `/c0`、`/c0/vN`、`/c0/eN/sN`、`/c0/sN`
5. **客户端不是边界**：`/api/actions` 返回的 `risk` / `requires` 只用于渲染，规则在 `prepare_action()` 里重算
6. **审计**：每次动作（成功 / 失败 / 被拒绝）写入审计日志并 `fsync`，超过 2 MB 轮转保留 1 份
7. **输入加固**：请求体限长 1 MiB；日志与审计字段清洗 C0 控制字符（含换行，防止伪造日志行）
8. **后台任务护栏**：控制器已有重建 / 回拷 / 校验时，按厂商规范拒绝下发状态变更类操作（停止 / 暂停仍可用）
9. **能力探测**：没有蜂鸣器、没有 SES 机框时不放行对应动作，避免出现“看似能点、实际必失败”的入口

漏洞报告方式与“属于设计而非漏洞”的范围见 [SECURITY.md](SECURITY.md)。

## 配置

配置文件：`@appconf/megaraid-console/config.json`（临时文件 + 原子替换写入，不会因中断写坏）。界面「设置」可改刷新间隔与日志上限，其余可用 `POST /api/config`。

| 键 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `storcli` | 自动探测 | — | StorCLI 路径；必须是 root 拥有、他人不可写 |
| `poll_interval` | `10` | 3–3600 秒 | 采集间隔；界面输入框限制 3–300，接口可用到 3600 |
| `temp_warn` | `50` | 0–120 °C | 温度预警阈值（不得超过告警阈值） |
| `temp_bad` | `58` | 0–120 °C | 温度告警阈值 |
| `phy_warn_rate` | `5.0` | 0–1e6 | 链路误码预警（次/秒，不得超过告警阈值） |
| `phy_bad_rate` | `50.0` | 0–1e6 | 链路误码告警 |
| `event_limit` | `60` | 1–500 | 事件日志显示条数 |
| `storcli_log_max_mb` | `128` | 1–4096 | `storcli.log` 上限，超过轮转为 `storcli.log.1`（保留 1 份） |

**为什么会有 `storcli.log`**：storcli 每次调用都会往工作目录追加日志，守护进程持续轮询时它会一直长。轮转把它约束在上限附近，总占用不超过约两倍上限；`/api/summary` 的 `storcli_log` 字段给出路径、当前大小、上限与已轮转次数。

## HTTP API

守护进程只监听 Unix socket，路由前缀 `/api/`。除 `/api/action`、`POST /api/config` 外均为只读。

| 方法与路径 | 说明 |
|---|---|
| `GET /api/ping` | 版本、当前身份、是否管理员 |
| `GET /api/summary` | 一次完整采集：controller / settings / cachevault / capabilities / schedule / topology / vds / pds / enclosures / drives / advisories / storcli_log |
| `GET /api/actions` | 动作目录（label / risk / target / value / needs_password / blocked_when_busy） |
| `POST /api/action` | 提交动作；返回 job 则异步执行并可在任务栏跟踪 |
| `GET /api/events?limit=N` | 控制器事件日志 |
| `GET /api/jobs`、`GET /api/job?id=` | 后台任务列表与单个任务进度 |
| `GET /api/foreign` | 外来配置 |
| `GET /api/audit?limit=N` | 操作审计记录 |
| `GET /api/config`、`POST /api/config` | 读取 / 修改配置（写入路径受白名单约束） |

调试时可以直接对 socket 发请求（需 root）：

```bash
# 卷号按实际安装卷替换（/vol1 或 /vol2 等），socket 权限为 root 专属
sudo curl -s --unix-socket /vol<N>/@appcenter/megaraid-console/app.sock \
  http://localhost/api/summary | python3 -m json.tool | head -40
```

## 测试

```bash
python3 -m unittest discover -s tests -v     # 32 项
node --check app/ui/app.js                   # 前端语法
```

测试**不调用 storcli**：`run_storcli_json` 被夹具替换，所以可以在生产机上安全运行，不会触发重建 / 离线等任何状态变更。覆盖：

- BBU 卡（只有 `/c0/bbu` 应答）、CacheVault 卡、两者都不应答、空应答不误判
- `show all` 中 `BBU_Info` 与 `Cachevault_Info` 两种提取
- 电池告警用词（电池 vs 超级电容）、需要更换、学习周期、Optimal 静默
- 概览与详细探测的合并优先级（占位值不覆盖真实值）
- 能力探测（`Alarm ABSENT` / 无 SES 机框）与前置拒绝文案
- 日志轮转：达阈值、小文件不动、旧备份被覆盖而非累积、上限钳制、统计字段
- 前端提交顺序回归（`pending.target` 必须在 `closeModal()` 之前取值）

## 故障排查

**电池面板显示“未检测到”**

控制器既没回 `/cN/cv` 也没回 `/cN/bbu`。先确认 storcli 能否看到备电对象：

```bash
sudo /opt/MegaRAID/storcli/storcli64 /c0 show all | grep -iE 'bbu|cachevault|battery'
sudo /opt/MegaRAID/storcli/storcli64 /c0/bbu show all
sudo /opt/MegaRAID/storcli/storcli64 /c0/cv  show all
```

若只有 Dell `perccli` 能报（部分 OEM 固件如此），需要再补一条采集路径 —— 欢迎带上面输出提 issue。

**蜂鸣器 / 定位灯按钮是灰的**

硬件或拓扑不支持，不是缺陷：

```bash
sudo /opt/MegaRAID/storcli/storcli64 /c0 show alarm   # Alarm ABSENT = 卡上没有蜂鸣器
sudo /opt/MegaRAID/storcli/storcli64 /c0/eall show    # No Enclosure Found! = 无 SES 机框
```

直连盘（`/c0/sN`）没有定位灯通道；实测把 `activityforlocate=on` 打开后 `start locate` 仍返回 `Start Drive Locate Failed.`，所以 1.0.6 直接置灰并在服务端拒绝。

**动作报 `Cannot read properties of null (reading 'target')`**

1.0.5 及以前的前端缺陷：命令已下发，只是提示错了。升级到 1.0.6，或用审计日志确认实际结果。

**`Controller 0 not found`**

非 root 运行，或 `/dev/megaraid_sas_ioctl_node` 权限不对（应为 `root:root crw-------`）。守护进程以 root 运行，手动排查记得 `sudo`。

**`未找到 storcli`**

按「快速开始 → 前置」放置 StorCLI，或在界面里指定路径（必须 root 拥有且不可被他人写）。

## 已知限制

- 定位灯依赖 SES 机框；直连拓扑下无解（已在界面说明）
- 部分 OEM 固件只通过自家 `perccli` 暴露电池信息，此时需要扩展采集路径
- 事件日志在部分控制器上约 19 MB，首次读取较慢，之后有 60 秒负缓存
- 只有 x86 构建；未验证 ARM 版 fnOS
- 危险操作（重建、离线、标记缺失、JBOD、外来配置）只做了代码路径审查与静态校验，未在真机上逐一触发

## 许可证

以 **GNU General Public License v3.0 或更高版本**（GPL-3.0-or-later）发布，全文见 [LICENSE](LICENSE)。可自由使用、修改、再分发；分发修改版时须以同一许可证提供完整对应源码。

- 仓库内 Python / JavaScript / CSS / HTML / 图标均为原创，无第三方代码，不链接 GPL 不兼容的库
- **StorCLI 是 Broadcom 专有软件，不在本许可证范围内，也不随本项目分发**：应用以独立进程方式调用它，属“聚合”而非衍生作品
- 因此：**不要在 issue / PR / release 中附带 storcli64 或任何 Broadcom 二进制**

希望网络侧也强制开源（例如未来加远程多用户访问）可改用 AGPL-3.0，替换 `LICENSE`、本文件与各文件 SPDX 头即可。

第三方组件与授权边界另见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 贡献

欢迎 issue / PR。提交即表示同意以 GPL-3.0-or-later 授权你的贡献。涉及硬件行为的改动请附脱敏后的 `storcli` 真实输出；危险操作类改动请说明测试环境。

---

## English

A self-hosted monitoring and operations console for LSI/Broadcom MegaRAID controllers on fnOS.
Pure-stdlib Python backend, dependency-free hand-written frontend, Unix-socket only (no TCP port),
32 hardware-free regression tests.

1.0.6 adds battery-backup (BBU) reporting for cards such as the Dell PERC H840 — `/cN/cv` is probed
first, then `/cN/bbu` — capability detection so buzzers/locate LEDs that the hardware cannot provide
are refused instead of failing cryptically, bounded rotation of storcli's own log (128 MB default),
and a fix for a frontend bug that reported every successful write action as a failure.

Licensed under GPL-3.0-or-later. Broadcom StorCLI is **not** included: install it yourself at
`/opt/MegaRAID/storcli/storcli64` (root-owned, not group/world writable). Build with
`fnpack build -d .`; test with `python3 -m unittest discover -s tests` (no storcli calls, safe on
production hosts).

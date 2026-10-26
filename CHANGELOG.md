# Changelog

本文件记录 MegaRAID Console 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

包标识曾于 1.0.2 从 `fn-raidwatch` 更名为 `megaraid-console`（安装目录与 PAM 服务名同步变更）。

## [1.0.7] - 2026-10-26

### Changed

- **概览卡片按需显示**：`最高链路误码` 与 `最高磁盘介质错误` 在数值为 0 时不再渲染 —— 健康阵列上
  一排 `0 /秒`、`0 个` 只会训练操作员忽略这一行；两项指标仍照常采集，一旦非零立刻出现，并标注来自
  哪块盘（`来自 s2`）。
- `SMART 告警磁盘` 卡片仅在存在告警时出现：判定覆盖 `S.M.A.R.T alert flagged by drive = Yes` 与
  `Predictive Failure Count > 0`（预测故障是同一类早期预警，只看 SMART 标志会漏掉正在衰减的盘）。
  盘名后标注 `(预测)`，超过 4 块折叠为 `s0、s1、s2、s3 等 6 块`。
- 卡片顺序：`… 物理盘 → 最高链路误码(条件) → 电池/CacheVault → 最高磁盘介质错误(条件) →
  SMART 告警磁盘(条件) → 控制器状态`，即新卡片紧挨控制器状态。

## [1.0.6] - 2026-10-26

### Added

- **电池备电（BBU）支持**：`gather_battery()` 先探 `/cN/cv`（CacheVault 超级电容），失败自动回退
  `/cN/bbu`（电池），两者统一成一套模型并用 `kind` 区分。戴尔 PERC H840 这类电池卡此前 `show all`
  里没有 `Cachevault_Info`，面板整块为空，现在可以正常显示。
- 电池字段：状态、电压、电流、温度、保持时间（Retention Time）、设计容量、模块固件、厂商、
  序列号、生产日期、学习周期 / 下次学习 / 学习模式，以及“需要更换”标记。
- 概览卡片与“电池（BBU）与计划任务”面板按实际硬件切换用词；电池学习中单独给出提示。
- 控制器既不上报 CacheVault 也不上报电池时，明确显示“未检测到”，而不是一屏 dash。
- 纯函数 `merge_power_backup()` 与 `power_backup_advisories()`，使告警措辞与合并优先级可单测。
- `tests/` 回归测试 17 项 + 3 个 storcli 夹具（BBU / CacheVault / 空应答），不调用真实 storcli。

### Fixed

- 修复写操作“假失败”：定位灯、静音/开关蜂鸣器等命令已成功下发，前端却在 `closeModal()` 清空
  `pending` 之后继续读取 `pending.target`，抛 `Cannot read properties of null (reading 'target')`
  并被 `catch` 渲染成“执行失败”。现在目标与标签在提交前固化为局部变量。
- BBU 卡的电池状态不再被误报为“CacheVault 非 Optimal”，避免用错硬件名词导致告警被忽略。

- **storcli 日志轮转**：storcli 每次调用都会往工作目录的 `storcli.log` 追加，守护进程 10 s 一采
  时该文件会无限增长（实测约 2 MB/天）。新增轮转：达到上限后改名为 `storcli.log.1`（只保留 1 份，
  磁盘占用上限约为 2 倍上限）。上限由配置项 `storcli_log_max_mb` 控制，**默认 128 MB**，范围
  1–4096，界面设置里可改；`/api/summary` 增加 `storcli_log`（路径 / 当前大小 / 上限 / 已轮转次数）
  便于核对。
- **能力探测与前置拒绝**：`/api/summary` 新增 `capabilities`（`alarm` / `locate`）。控制器报
  `Alarm ABSENT`（无蜂鸣器）或没有 SES 机框（`No Enclosure Found`，直连盘）时，蜂鸣器与定位灯动作
  在服务端被直接拒绝并给出原因，前端按钮置灰并带 tooltip，不再让用户撞 storcli 的
  `Start Drive Locate Failed.` / `Description = None`。能力未知时（首次采集前）不拦截。

### Changed

- 概览卡片副标题只显示带数值的读数：部分 OEM 固件（H840 实测）把电压 / 温度报成 `OK` 而不是
  数值，原来会渲染成“BBU · OK · OK”。

### Unchanged

- 危险操作（重建、离线、标记缺失、JBOD、外来配置、一致性校验）的动作定义、校验与执行路径未改动。

## [1.0.5] - 2026-10-19

### Fixed

- 兼容直连磁盘拓扑（外置硬盘盒）：磁盘不经机框上报（无 SES/SGPIO，路径为 `/c0/sN` 而非
  `/c0/eN/sN`）时，`/c0/eall/sall` 返回 `No drive found!` 导致摘要接口整体报错、磁盘列表为空。
  现在磁盘与 PHY 采集在 `eall/sall` 与 `sall` 之间自动回退，操作目标校验与槽位短名解析同时接受两种形式。

## [1.0.4] - 2026-10-19

### Security / Data safety

- 请求体限长 1 MiB：不再把客户端 `Content-Length` 直接交给 `read()`
- 日志与审计字段清洗 C0 控制字符（含换行），防止伪造日志行
- 事件日志负缓存：读取失败后 60 s 内不再重试（实机日志约 19 MB）
- 采集单飞：多标签页/频繁刷新合并为一次采集（最小间隔 3 s）
- 自适应刷新：检测到后台任务时下发 `poll_hint` 并跳过事件日志读取，把 I/O 让给重建
- 按厂商规范新增护栏：控制器已有后台任务时拒绝状态变更类操作（停止/暂停仍可用）
- 阵列降级时移除成员盘，除密码外还需输入目标槽位名二次确认，并明示数据不可恢复
- CacheVault 非 Optimal / 需更换、降级阵列启用写回缓存时，直接说明掉电数据风险
- 审计日志写入后 `fsync`，超 2 MB 轮转保留 1 份
- 辅助数据读取失败时降级为空并提示，不再整页 500
- 数值解析防溢出（超长数字不再解析成 `inf` 误判坏链路）

### Added

- `tests/` 回归测试（含实机 storcli 夹具）

### Changed

- 界面：按风险级别着色确认按钮、后台任务期间置灰冲突按钮、新增提示条与二次确认框

## [1.0.3] - 2026-10-19

### Fixed

- `/api/config` 传非法值返回 500 而非 400 → 补类型、取值范围与阈值交叉校验（刷新间隔 1–3600、
  事件条数 1–500、温度 0–120，且预警阈值不得大于告警阈值）
- storcli 路径改为只接受 root 拥有且非同组/他人可写的可执行文件，堵住“管理员把守护进程指向任意
  程序并以 root 执行”的风险
- `config.json` 先写临时文件再原子替换，避免并发或中断写坏导致设置静默回退默认值
- 后台任务回收不再淘汰仍在排队/运行中的任务
- Unix socket 在限制性 umask 下 bind，消除 bind 与 chmod 之间的短暂可连接窗口
- 前端刷新防重入，避免慢控制器下请求堆积
- CacheVault 解析兼容 storcli 的多种返回形态
- 生命周期脚本校验 PID 确实属于本守护进程（避免 SIGKILL 后 PID 复用导致 `start` 永远拒绝启动）
- 清理死代码

## [1.0.2] - 2026-10-19

### Changed

- 应用更名为 **MegaRAID Console**（原“RAID 看护”；包标识 `fn-raidwatch` → `megaraid-console`，
  安装目录与 PAM 服务名随之变更）

### Added

- “控制器信息”面板：型号 / 序列号 / SAS 地址 / PCI 地址 / 板卡修订 / 生产日期 / 厂商与设备 ID /
  OEM / 安全 ID、固件包 / CPLD / HII / Ctrl-R / Preboot CLI / NVDATA / Boot Block / 芯片修订、
  缓存 / NVRAM / Flash / CacheVault Flash / FW 缓存 / 备份电源 / TPM / 接口 / 后端端口 /
  支持的磁盘与 RAID 级别 / 最大虚拟盘数 / 默认条带、ROC 与控制器温度及传感器状态、ECC 与内存错误计数、机框详情
- CacheVault 参数补全：包能量 / 设计容量 / 电容值 / NVCache 状态 / 是否需更换 / 模块固件 / 厂商 /
  模块序列号 / 电芯生产日期 / Flash / PCB 与电池包组件号 / 学习周期与模式
- 物理盘新增固件、容量、逻辑与物理扇区、断言计数，并提供可展开的“完整参数”（强制与原始容量、
  连接器、托盘与机框位置、FDE/SED/加密/净化/PI/认证/多路径/备用盘等）

### Fixed

- Host Interface 与 Device Interface 一直显示分隔符（这两个字段在 Bus 段，原实现从 Basics/Version/Status 读取）

## [1.0.1] - 2026-10-19

### Fixed

- 应用窗口全黑：Unix socket 连接的客户端地址为空字符串，基类解析时抛 `IndexError`，导致每个请求在
  发送响应头前就中断
- 构建产物不再混入 storcli 运行日志

## [1.0.0] - 2026-10-19

### Added

- 首个版本：控制器 / 虚拟盘 / 物理盘 / PHY 误码 / 事件日志监控
- 危险操作的系统账户密码二次验证（专用 PAM 服务）
- 蜂鸣器、定位灯、重建、磁盘状态、一致性校验、外来配置、控制器参数等运维操作

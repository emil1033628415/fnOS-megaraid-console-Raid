# 第三方组件与授权说明

本仓库自身以 GPL-3.0-or-later 授权（见 [LICENSE](LICENSE)）。下面列出**不在**该授权范围内的外部组件，
以及它们与本项目的交互方式。

## 1. Broadcom StorCLI（`storcli64`）

| 项目 | 说明 |
|---|---|
| 权利归属 | Broadcom Inc.（原 LSI / Avago），专有软件 |
| 本项目是否分发 | **否**，仓库与 release 均不包含该二进制 |
| 交互方式 | 以独立进程方式调用（`subprocess` + 固定 argv），不链接、不内嵌、不修改 |
| 获取途径 | 用户自行从 Broadcom 官方渠道下载并安装，默认放置于 `/opt/MegaRAID/storcli/storcli64` |
| 许可证影响 | 独立进程调用属于“聚合（mere aggregation）”，不构成衍生作品，因此不影响本项目的 GPL 授权；但**不得**把 storcli64 放进本仓库、release 资产或 fpk 包中 |

验证于 `StorCli SAS Customization Utility Ver 007.2705.0000.0000`。其他版本可能字段名不同，本项目已对
常见别名做兜底（见 `app/server/raidcore.py` 中的 `_normalise_power_backup`）。

## 2. fnOS / fnpack

- `fnpack` 是 fnOS 提供的应用打包工具，仅用于构建 `.fpk`，不随本仓库分发
- `manifest`、`cmd/` 生命周期脚本、`config/privilege`、`config/resource`、`app/ui/config` 遵循 fnOS
  应用规范，属于运行所需的元数据
- 通过 fnOS 应用中心分发时，还需遵守 fnOS 的应用商店条款；GPL 与本项目授权不替代该等条款

## 3. Python 标准库

后端只使用 Python 标准库（PSF License，GPL 兼容），无第三方包依赖，因此不存在依赖许可证冲突。
`app/server/pam_verify.py` 通过 `ctypes` 调用系统 `libpam`（Linux-PAM，BSD/GPL 双许可），仅做运行时调用。

## 4. 前端

`app/ui/app.js`、`index.html`、`style.css` 为手写代码，无框架、无构建步骤、无第三方 JS 依赖。

## 5. 图标

`ICON.PNG`、`ICON_256.PNG`、`app/ui/images/icon_*.png` 为本项目自绘资源，随本项目一并按 GPL-3.0-or-later 授权。
若你替换为其他来源的图标，请自行确认其授权兼容性。

## 6. 许可证文本

`LICENSE` 为 GNU General Public License v3.0 官方文本（取自 <https://www.gnu.org/licenses/gpl-3.0.txt>），
按其自身条款可原样分发。

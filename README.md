# Agent4Company 公司管理平台

Agent4Company 是一个本地优先、面向非技术管理者的独立桌面工作台。它把原销售总监平台中稳定的受控执行机制提炼为公司级轻本体：平台核心只负责插件发现、Pi 主智能接入、受控 DAG、任务、审批、审计和共享能力编排；销售管理作为第一个可插拔业务域接入，不再定义整个平台。

当前版本为第一阶段基线 `0.1.0`，默认中文界面，正式产品路线为 Tauri 2 + WebView2 桌面应用。本机浏览器启动仅用于开发和诊断。

## 已完成的第一阶段能力

- 公司级统一工作台：首页、任务中心、审批中心、项目空间、知识与文件、业务域、审计和设置。
- 公司级默认 Profile `company-manager`，不默认启用任何业务域；`company-with-sales` 仅用于销售域组合验证。
- 插件清单、语义版本依赖、权限收敛、DAG 无环/入口/出口校验。
- 统一 SQLite 任务、审批和审计运行时；结构化写入必须具有唯一直接审批前驱。
- 审批绑定公司、业务域、项目、策略、任务版本、载荷 SHA-256 和存储 binding。
- 七个公司级共享能力契约：模型接入、聚合搜索、知识库、项目空间、文件处理、演示文稿和定时任务。
- 销售域插件 `domain.sales` 及首个可验证流程 `domain.sales.pipeline-review`。
- Pi 扩展入口与公司/销售 Skill；启动时隔离用户全局资源，只按 Profile 显式加载项目资源，并将 Agent 工具锁定为三项只读公司治理工具；RPC 测试验证实际接入。
- Windows Tauri 桌面壳、启动脚本、构建脚本和 `--self-test`。
- Python、TypeScript、HTTP 集成、审批防绕过、载荷防篡改和仓库洁净度测试。

第一阶段不会写入真实客户数据。销售域验证流程只读取空上下文、生成合成行动意图并验证审批/审计闭环。

## 快速开始（Windows）

前置条件：

- Windows 10/11 与 WebView2
- Python 3.11+
- Node.js 24.x、pnpm
- 开发或打包桌面版时需要 Rust/Cargo 与 Microsoft C++ Build Tools

```powershell
Set-Location E:\PersonalWorkSpace\Agent4Company
.\scripts\setup-windows.ps1
.\scripts\start-windows.ps1
```

没有 Rust 环境时，可先启动仅供诊断的本地工作台：

```powershell
.\scripts\start-windows.ps1 -WorkbenchOnly
```

执行自检与测试：

```powershell
python -m company_platform validate
python -m company_platform self-test
pnpm test
```

默认桌面使用 `company-manager`，已安装的销售域可见但未启用，不能发起销售流程。若要在空数据环境验证销售域界面、Pi Skill、审批和审计组合，可在当前 PowerShell 会话中显式选择验证 Profile：

```powershell
.\scripts\start-windows.ps1 -Profile company-with-sales
```

也可以只启动诊断工作台：

```powershell
python -m company_platform serve --profile company-with-sales --port 8766 --open-browser
```

构建 Windows 桌面程序：

```powershell
.\scripts\build-windows-desktop.ps1
.\Agent4Company.exe --self-test
```

完整步骤和故障排查见 [安装说明](docs/INSTALLATION.md)，开发约定见 [开发说明](docs/DEVELOPMENT.md)。

## 架构概览

```text
Tauri 公司管理桌面壳
  ├─ 公司级中文工作台
  ├─ Pi 主智能核心
  └─ 轻本体运行时
       ├─ 插件注册与依赖
       ├─ 受控 DAG / 任务 / 审批 / 审计
       ├─ 公司级共享能力插件
       └─ 可插拔业务域
            └─ 销售管理（第一阶段）
```

目录职责：

| 目录 | 职责 |
| --- | --- |
| `company_platform/` | 本地控制面、插件校验、统一任务/审批/审计运行时和 HTTP 工作台服务 |
| `pi/` | Pi 主智能扩展和公司/业务域技能 |
| `plugins/platform/` | 公司级共享能力契约 |
| `plugins/domains/` | 可插拔业务域及其工作流 |
| `profiles/` | 工作台组合，不把岗位角色定义为平台本体 |
| `ui/` | 中文非技术用户工作台 |
| `desktop/src-tauri/` | Windows 桌面壳与进程生命周期 |
| `contracts/` | 插件和受控 DAG Schema |
| `tests/` | 正向、负向、集成和仓库洁净验证 |
| `docs/` | 产品、设计、架构、迁移和验收文档；不使用 ReqGuard 门禁目录 |

## 数据与安全边界

- 仓库只包含可公开复用的代码、空模板、合成测试和文档。
- `.gitignore` 排除源/运行 Git 数据、`.pi`、`runtime`、数据库、日志、输入输出、凭据、依赖和安装产物。
- 本地服务只绑定 `127.0.0.1`，校验 Host；所有 POST 使用每次启动随机生成的会话令牌。
- Pi 使用项目内忽略目录 `.pi/company-runtime/pi-agent`，启动时禁用用户全局扩展、Skill、提示模板、主题、上下文文件和会话发现；默认离线，不继承本机其他 Pi 项目的运行资源。
- 第一阶段是本机单人身份，不代表已经具备企业 SSO、多人职责分离或生产级多租户隔离。
- 模型和公开搜索没有默认凭据；未配置时明确降级，工作台仍可离线启动。
- 定时任务只负责创建统一 DAG 任务，不引入第二套待办或审批事实源。
- `company-manager` 与 `company-with-sales` 共用 `runtime/company-platform.db`；Profile 只控制可发起的域工作流，不拆分任务、审批或审计事实源。

## 文档

- [产品范围与验收目标](docs/01-product-scope.md)
- [公司级信息架构与业务域边界](docs/02-information-architecture.md)
- [总体架构、权限与数据隔离](docs/03-system-architecture.md)
- [迁移终态与清理清单](docs/04-migration-and-final-state.md)
- [第一阶段验收计划与结果记录](docs/05-acceptance.md)
- [安装说明](docs/INSTALLATION.md)
- [开发说明](docs/DEVELOPMENT.md)

## 本地 Git 与后续远端

本目录是全新的本地 Git 仓库，不继承源项目历史、远端、分支或 worktree。第一阶段不会创建 GitHub、GitLab 或其他远端仓库。

只有在用户明确决定托管平台、仓库归属、可见性和名称后，才执行类似以下操作：

```powershell
git remote add origin <由用户确认的新仓库地址>
git push -u origin main
```

在获得该授权前，`git remote -v` 应保持为空。

## 当前限制

- 模型、公开搜索、知识写入、项目写入、文件上传、演示文稿生成和定时触发在第一阶段只完成插件契约、架构边界与工作台状态接入；真实提供方、Adapter 和产物链尚未迁入并完成端到端验证。
- 正常关窗目前通过强制回收子进程树保证不遗留后台服务；正式业务写入前仍需实现等待在途事务完成的优雅退出。
- 当前统一运行库用于第一阶段本机验证；生产版本仍需拆分核心库、知识库和各业务域数据库，并接入企业身份源。
- 桌面构建依赖本机 Rust、C++ Build Tools 和 WebView2；签名、安装器、自动升级和发布流程尚未配置。
- 未创建远端仓库，也未执行真实业务数据迁移。

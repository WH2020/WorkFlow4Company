# Windows 安装与启动说明

## 1. 适用范围

第一阶段支持 Windows 10/11 本地开发和桌面验证。正式桌面入口使用 Tauri 2 + WebView2；浏览器模式只用于诊断。

## 2. 前置环境

- Python 3.11 或更高版本。
- Node.js 24.x 与 pnpm。
- Windows WebView2 Runtime（Windows 11 通常已包含）。
- 构建桌面程序时：Rust/Cargo、Microsoft C++ Build Tools。

检查版本：

```powershell
python --version
node --version
pnpm --version
cargo --version
```

## 3. 首次安装

```powershell
Set-Location E:\PersonalWorkSpace\Agent4Company
.\scripts\setup-windows.ps1
```

脚本会：

1. 验证 Python 3.11+ 和 Node 24.x。
2. 创建本项目自己的 `.venv`。
3. 在本项目安装 Node/Pi/Tauri 依赖。
4. 校验插件、依赖、DAG、权限和审批边界。
5. 在临时数据库执行销售域空数据自检。
6. 通过 Pi 包解析器与 RPC 分别启动两个 Profile，验证公司扩展、Skill 和只读活动工具边界。

脚本不会创建或迁移真实业务数据，不会配置模型密钥，不会建立远端 Git 仓库。

## 4. 启动

开发桌面版：

```powershell
.\scripts\start-windows.ps1
```

如果根目录已存在本机构建的 `Agent4Company.exe`，脚本会启动它；否则使用 Tauri 开发模式。

仅诊断工作台：

```powershell
.\scripts\start-windows.ps1 -WorkbenchOnly
```

该模式会在系统浏览器打开 `http://127.0.0.1:8766`，不代表正式产品形态。

运行自检：

```powershell
.\scripts\start-windows.ps1 -SelfTest
```

默认使用 `company-manager`，不启用任何业务域。销售域是已安装候选，但默认不能发起任务。验证销售域组合时，显式设置本次进程的 Profile：

```powershell
.\scripts\start-windows.ps1 -Profile company-with-sales
```

浏览器诊断入口也可显式传入：

```powershell
python -m company_platform serve --profile company-with-sales --port 8766 --open-browser
```

两个 Profile 共用 `runtime/company-platform.db`。切换 Profile 不会创建第二套任务、审批或审计事实源；`enabled_domains` 只控制可发起的业务域工作流。

## 5. 构建桌面程序

```powershell
.\scripts\build-windows-desktop.ps1
```

构建脚本会先执行 TypeScript 检查、Pi 扩展/Skill RPC 验证和公司运行时自检，再使用锁定依赖编译 Rust/Tauri，生成根目录 `Agent4Company.exe`，最后运行 `--self-test`。

若只需要编译证据：

```powershell
cargo check --manifest-path .\desktop\src-tauri\Cargo.toml --locked
```

## 6. 本地文件

运行后可能生成：

- `.venv/`：本项目 Python 环境。
- `node_modules/`：本项目 Node 依赖。
- `.pi/company-runtime/`：桌面、工作台和 Pi 日志。
- `.pi/company-runtime/pi-agent/`：本项目隔离的 Pi 运行配置目录，不读取用户全局 Pi 资源。
- `runtime/company-platform.db`：本机任务、审批和审计验证数据。
- `desktop/src-tauri/target/`：Rust 构建产物。
- `Agent4Company.exe`：本机构建的可执行文件。

这些路径全部被 Git 忽略，不应提交或迁移。

## 7. 常见问题

### 端口 8766 被占用

先关闭旧的 Agent4Company 实例。桌面壳不会接管未知服务，以避免把其他本机页面当成公司工作台。

### Pi 核心未安装

重新运行 `scripts/setup-windows.ps1`。如果 `node_modules/.bin/pi.CMD` 不存在，桌面程序会拒绝启动智能核心。

第一阶段 Pi 默认离线，并且只加载项目显式声明的扩展和当前 Profile Skill；不会自动继承用户目录中的扩展、提示或模型配置。

### 模型或搜索显示未配置

这是第一阶段预期状态。工作台和销售域空数据验证不依赖外部密钥；真实提供方配置属于后续受控工作。

### Cargo 或链接失败

确认已安装 Rust 的 MSVC toolchain 和 Microsoft C++ Build Tools，并在新的 PowerShell 窗口重试。

### 工作台自检失败

运行：

```powershell
python -m company_platform validate
python -m company_platform self-test
python -m unittest discover -s tests -p "test_*.py" -v
```

桌面日志位于 `.pi/company-runtime/`。日志可能包含本机路径，不要提交或对外发送未审查的完整日志。

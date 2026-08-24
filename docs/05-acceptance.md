# 第一阶段验收计划与结果记录

本文件既定义验收矩阵，也记录实际执行证据。执行结果应只填写真实运行过的命令；未执行项保持“未验证”。

## 1. 需求到验证追踪

| 编号 | 验收目标 | 自动化证据 | 状态 |
| --- | --- | --- | --- |
| AC-01 | 新目录为独立 Git，默认分支 `main`，无远端 | Git 状态与 `test_no_remote_is_configured` | 通过 |
| AC-02 | 公司默认 Profile 不依赖销售 | Python/TypeScript Profile 测试 | 通过 |
| AC-03 | 销售域由插件发现并解析依赖 | Python/TypeScript 注册表测试 | 通过 |
| AC-04 | 无销售域时公司 Profile、HTTP 服务与共享插件仍加载 | Python 注册表/服务集成测试 | 通过 |
| AC-05 | DAG 无环、依赖无环、工具归属、权限不升级、入口/输出一致 | Python/TypeScript `validate` 与负向测试 | 通过 |
| AC-06 | 结构化写入不能绕过直接审批 | Python/TypeScript 负向测试 | 通过 |
| AC-07 | payload hash、版本或存储 binding 变化使审批失效 | Runtime 篡改负向测试 | 通过 |
| AC-08 | 非审批角色不能决策 | `test_non_approver_cannot_decide` | 通过 |
| AC-09 | 销售域空数据流程到审批并可批准完成，两个 Profile 共用事实库 | CLI、Runtime、HTTP 集成测试 | 通过 |
| AC-10 | 本地服务健康检查为公司 Profile，POST 有会话门禁 | `tests/test_server.py` | 通过 |
| AC-11 | 仓库不跟踪数据、凭据、日志、数据库和安装产物 | `tests/test_repository_hygiene.py` + 残留扫描 | 通过 |
| AC-12 | 不启用 ReqGuard，不安装第二套任务系统 | package/目录负向测试 | 通过 |
| AC-13 | Windows 启动脚本与 Tauri 编译通过 | PowerShell 5.1 解析、Cargo check/build | 通过 |
| AC-14 | Tauri `--self-test` 启动工作台并核验 Pi 版本 | 构建后 EXE 自检 | 通过 |
| AC-15 | 中文桌面布局无溢出、审批和空状态可用 | 1440×900 截图和真实窗口启动/关闭检查 | 通过（第一阶段页面） |
| AC-16 | Pi 不发现用户全局资源，Profile Skill 隔离，Agent 无终端/文件写工具 | 双 Profile Pi RPC `get_commands` + `active_tools` | 通过 |
| AC-17 | 两个 Profile 的真实桌面均启动，工作流贡献正确，关窗回收全部子进程 | 双 Profile EXE 生命周期检查 | 通过 |

## 2. 建议执行顺序

```powershell
python -m company_platform validate
python -m company_platform self-test
python -m unittest discover -s tests -p "test_*.py" -v
pnpm check:types
pnpm test:runtime
cargo check --manifest-path desktop/src-tauri/Cargo.toml --locked
.\scripts\start-windows.ps1 -WorkbenchOnly
.\scripts\build-windows-desktop.ps1
.\Agent4Company.exe --self-test
```

工作台启动验证应确认：

- `/api/health` 返回 `product_id=agent4company` 和 `profile_id=company-manager`。
- 公司首页没有销售金额、客户或投标等固定公司指标。
- 业务域页显示销售管理来自 `domain.sales`。
- 发起流程后任务停在审批中心。
- 批准后任务完成，审计页出现创建、节点、审批和完成事件。
- 模型、搜索和调度显示未配置/本地模式，而不是假装可用。
- Pi 活动工具只有三项只读公司治理工具，销售 Skill 不出现在默认 Profile。

## 3. 数据洁净负向检查

```powershell
git remote -v
git ls-files
rg -n -i --hidden -g '!node_modules/**' -g '!.git/**' -g '!runtime/**' `
  '(BEGIN (RSA|OPENSSH|PRIVATE)|sk-[A-Za-z0-9]|ghp_[A-Za-z0-9]|AKIA[0-9A-Z]{16})' .
rg -n -i -g 'package.json' -g 'pnpm-lock.yaml' '(juicesharp|rpiv-todo)' .
```

允许的历史文字匹配只限迁移/产品文档和负向测试；运行依赖、默认 Profile、桌面标识和核心代码不得保留源产品身份。

## 4. 第一阶段已知未覆盖风险

- 企业身份、职责分离、真实 RBAC 和生产级多租户未实现。
- 模型、公开搜索、知识写入、文件上传、PPT 生成和定时触发只有能力契约，尚无真实端到端迁移结果。
- Windows 安装器签名、自动更新、网络代理和杀毒软件环境未验证。
- 长时间运行、断电恢复、SQLite 损坏恢复和并发压力未验证。
- 销售域禁用后的历史任务只读兼容策略尚未实现。
- 正常关窗当前强制回收子进程树；正式业务写入前需实现等待在途事务完成的优雅退出。
- 数据迁移、降级和回滚未执行；任何真实数据迁移都需要单独授权。

## 5. 实际执行记录

执行日期：2026-08-24；环境：Windows、Python 3.11.9、Node.js 24.19.0、pnpm 11.22.0、Cargo 1.93.1、Rust 1.93.1。

| 命令/检查 | 实际结果 |
| --- | --- |
| `python -m company_platform validate` | 通过：8 个插件、7 个共享能力、1 个业务域、1 个工作流 |
| `python -m company_platform self-test` | 通过：销售域受控 DAG 到达审批并在批准后完成 |
| `python -m unittest discover -s tests -p "test_*.py" -v` | 23/23 通过 |
| `pnpm check:types` | 通过，无 TypeScript 类型错误 |
| `pnpm test:runtime` | 8/8 通过；包含双 Profile 真实 Pi RPC、资源隔离和活动工具白名单 |
| `pnpm test` | 通过；包含类型、Node 和 Python 测试 |
| `node --check ui/app.js` | 通过 |
| `powershell.exe ... start-pi-windows.ps1 --version` | 通过：PowerShell 5.1 可解析 UTF-8 BOM 脚本，Pi 0.84.2 |
| `cargo fmt --check` | 通过 |
| `cargo check --manifest-path desktop/src-tauri/Cargo.toml --locked` | 通过 |
| `scripts/setup-windows.ps1` | 通过：本地环境、锁定依赖、契约、自检和 Pi RPC 验证 |
| `scripts/build-windows-desktop.ps1` | 通过：Release 构建、EXE 自检；本地产物 10,771,968 字节，SHA-256 `b466eba16eafabf6966512190eaeb4c3f32f88f160aaf7cd4e68bc7d475a6747` |
| 正常启动 `Agent4Company.exe`（`company-manager`） | 通过：可见“公司管理平台”窗口、健康接口正确、0 个业务工作流、Pi 子进程保持运行 |
| 正常启动 `Agent4Company.exe`（`company-with-sales`） | 通过：可见“公司管理平台”窗口、健康接口正确、销售域贡献 1 个工作流、Pi 子进程保持运行 |
| 两个 Profile 主窗口关闭 | 通过：桌面退出码均为 0；端口 8766 与全部捕获子进程均释放 |
| Edge 1440×900 截图巡检 | 通过：首页、导航、空状态和动态销售域快捷流程无明显溢出 |
| `git diff --cached --check` | 通过 |
| 源标识/敏感模式/禁止路径/远端扫描 | 通过：117 个暂存文件无敏感模式、禁止路径或符号链接；4 处旧标识仅在迁移文档/负向测试；远端为空，源项目状态干净 |

执行中发现并修复：

1. SQLite 连接未显式关闭，导致 Windows 临时目录自检清理失败。
2. 新项目缺少独立图标导致 `tauri-build` 失败；已生成中性公司平台图标，未复用源销售品牌。
3. Windows PowerShell 5.1 对无 BOM 中文脚本解析失败；所有 `.ps1` 已加入 UTF-8 BOM。
4. Rust canonical path 的 `\\?\` 前缀使 PowerShell `$PSScriptRoot` 为空；已转换为普通 Windows 路径。
5. 单实例插件辅助窗口会干扰自动关闭检查；已按真正的“公司管理平台”主窗口验证，并在 CloseRequested 时显式清理子进程与退出。
6. Profile 最初被当成数据分片，可能产生平行事实源；已改为所有组合共用统一任务、审批和审计数据库，Profile 只控制新流程入口。
7. 业务工具权限曾由平台核心硬编码销售工具；现改为插件清单自声明，并用独立交付域样例证明新增域无需修改核心。
8. 审批的存储 binding 最初没有在决策/写入时重复检查；现已绑定真实 `company_id` 并与 payload、策略和版本一起复核。
9. Pi 普通启动最初会发现用户全局资源并保留内置写工具；现使用项目隔离目录、显式 Profile 资源和三项只读工具白名单，并由真实 RPC 负向验证。
10. Tauri 健康检查最初固定默认 Profile；现由白名单化环境选择驱动工作台、Pi 和健康检查，并完成双 Profile 真实桌面验证。

未覆盖项仍以第 4 节为准；尤其没有执行真实模型/搜索/PPT/文件/调度端到端、企业身份、多租户、压力、断电恢复、安装器签名或真实数据迁移。

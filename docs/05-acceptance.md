# 第一阶段验收计划与结果记录

本文件既定义验收矩阵，也记录实际执行证据。执行结果应只填写真实运行过的命令；未执行项保持“未验证”。

## 1. 需求到验证追踪

| 编号 | 验收目标 | 自动化证据 | 状态 |
| --- | --- | --- | --- |
| AC-01 | 新目录为独立 Git，默认分支 `main`，无远端 | Git 状态与 `test_no_remote_is_configured` | 通过 |
| AC-02 | 公司默认 Profile 不依赖销售 | Python/TypeScript Profile 测试 | 通过 |
| AC-03 | 销售域由插件发现并解析依赖 | Python/TypeScript 注册表测试 | 通过 |
| AC-04 | 无销售域时公司 Profile、HTTP 服务与共享插件仍加载 | Python 注册表/服务集成测试 | 通过 |
| AC-05 | DAG 无环、依赖无环、工具归属、显式读写效果、权限不升级、入口/输出一致 | Python/TypeScript `validate` 与负向测试 | 通过 |
| AC-06 | `.write/create/delete/mutate` 等命名不参与安全推断；所有 `effect=write` 工具不能绕过直接审批 | Python/TypeScript 负向测试 | 通过 |
| AC-07 | payload、插件版本、工作流指纹或存储 binding 变化使审批失效并停止旧任务 | Runtime 篡改、审批前升级、批准后重启升级测试 | 通过 |
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
| AC-18 | 新 Profile、业务域 Skill 与工作台提示无需在 CLI/服务/桌面壳中增加销售特例，Skill 声明与 frontmatter 一致 | 第三个合成 Profile、合成交付域 Python + 真实 Pi RPC、Skill 名不一致负向测试、核心扫描 | 通过 |
| AC-19 | 创建或批准提交后中断可跨 Profile 幂等恢复，不重放节点；规范变化时 fail closed | 销售任务中断后以 `enabled_domains=[]` 恢复及指纹失效测试 | 通过 |
| AC-20 | 桌面只从可执行文件祖先定位项目，不信任启动工作目录 | 伪项目工作目录真实 EXE 启动检查 | 通过 |

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
- 长时间运行、真实断电、SQLite 损坏恢复和并发压力未验证；当前只覆盖确定性进程中断后的已提交状态恢复。
- 销售域禁用后的历史任务只读兼容策略尚未实现。
- 正常关窗当前回收子进程树；正式外部写入 Adapter 接入前需实现等待在途事务完成的优雅退出、Adapter 幂等键和外部提交回执。
- 数据迁移、降级和回滚未执行；任何真实数据迁移都需要单独授权。

## 5. 实际执行记录

执行日期：2026-08-24；环境：Windows、Python 3.11.9、Node.js 24.19.0、pnpm 11.22.0、Cargo 1.93.1、Rust 1.93.1。

| 命令/检查 | 实际结果 |
| --- | --- |
| `python -m company_platform validate` | 通过：8 个插件、7 个共享能力、1 个业务域、1 个工作流 |
| `python -m company_platform self-test` | 通过：通用验证 Profile 自检器按状态处理审批；当前销售域 1 次审批后完成 |
| `python -m unittest discover -s tests -p "test_*.py" -v` | 33/33 通过 |
| `pnpm check:types` | 通过，无 TypeScript 类型错误 |
| `pnpm test:runtime` | 11/11 通过；包含双 Profile 与合成交付域真实 Pi RPC、资源隔离、活动工具白名单及读写效果边界 |
| `pnpm test` | 通过；包含类型、Node 和 Python 测试 |
| `node --check ui/app.js` | 通过 |
| `powershell.exe ... start-pi-windows.ps1 --version` | 通过：PowerShell 5.1 可解析 UTF-8 BOM 脚本，Pi 0.84.2 |
| `cargo fmt --check` | 通过 |
| `cargo check --manifest-path desktop/src-tauri/Cargo.toml --locked` | 通过 |
| `scripts/setup-windows.ps1` | 通过：本地环境、锁定依赖、契约、自检和 Pi RPC 验证 |
| `scripts/build-windows-desktop.ps1` | 通过：Release 构建、EXE 自检；本地产物 10,791,424 字节，SHA-256 `9875e03467ad00fe56d750d1d8d600c3181e10973c82c241f8ee5ec84ef43ab5` |
| 从含伪项目标记的工作目录启动 `Agent4Company.exe`（`company-manager`） | 通过：仍从 EXE 祖先定位真实项目；可见“公司管理平台”窗口、健康接口正确、0 个业务工作流、工作台与 Pi 子进程保持运行 |
| 正常启动 `Agent4Company.exe`（`company-with-sales`） | 通过：可见“公司管理平台”窗口、健康接口正确、销售域贡献 1 个工作流、Pi 子进程保持运行 |
| 两个 Profile 主窗口关闭 | 通过：桌面退出码均为 0；每次捕获 15 个子孙进程，端口 8766 与全部捕获进程均释放 |
| Edge 1440×900 截图巡检 | 通过：首页、导航、空状态和动态销售域快捷流程无明显溢出 |
| `git diff --cached --check` | 通过 |
| 源标识/敏感模式/禁止路径/远端扫描 | 通过：118 个提交跟踪文件无敏感模式、禁止路径或符号链接；旧标识仅在迁移文档/负向测试；远端为空，源项目状态干净 |

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
11. 桌面项目根最初信任当前工作目录；现只从可执行文件祖先解析受控项目根，避免伪工作目录注入本地启动代码。
12. CLI、服务和桌面组合最初固定加载销售 Profile/Skill/提示；现按本地 Profile 与启用业务域清单通用解析，并以合成交付域真实 Pi RPC 验证。
13. 写入判断最初依赖权限后缀；现由工具 `effect` 决定审批要求，由独立 `write_permissions` 标记写权限，并覆盖读写混合权限正向用例。
14. 任务创建或批准已提交但进程中断时可能停留在 `running`；现启动时幂等恢复且不重放已完成节点。
15. 旧审批最初未绑定插件/工作流语义；现任务和审批均绑定插件版本与规范指纹，审批前或批准后升级都会持久化失效/失败状态并禁止执行。
16. 通用自检最初隐含“一次审批完成”的销售流程形状；现按状态循环处理审批并设节点相关安全上限，可验证合法多阶段审批流程。
17. Skill 路径最初未核对内部名称；现要求插件声明、目录名和 `SKILL.md` frontmatter `name` 一致，并有负向测试。
18. Windows 安装脚本最初只检查 Node 主版本；现与 `package.json` 一致检查 `>=24.19.0 <25`。

未覆盖项仍以第 4 节为准；尤其没有执行真实模型/搜索/PPT/文件/调度端到端、企业身份、多租户、压力、断电恢复、安装器签名或真实数据迁移。

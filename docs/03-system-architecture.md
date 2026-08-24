# 总体架构、权限边界与数据隔离

## 1. 系统边界

```text
┌────────────────────────────────────────────┐
│ Tauri 2 / WebView2 公司桌面壳              │
│ 单实例、回环健康检查、子进程启动与回收      │
├────────────────────────────────────────────┤
│ 中文公司工作台 + 本地 Python 控制面         │
│ Core API / 静态 UI / 会话令牌               │
├────────────────────────────────────────────┤
│ Pi 主智能核心                               │
│ 意图理解 / 能力目录 / 受控工作流规划 / Skill│
├────────────────────────────────────────────┤
│ 轻本体                                      │
│ Plugin Registry / DAG / Task / Approval     │
│ Audit / Scope / Capability Adapters         │
├───────────────────┬────────────────────────┤
│ 公司级共享能力插件 │ 可插拔业务域             │
│ 知识/项目/文件/PPT │ 销售（第一阶段）          │
│ 模型/搜索/调度     │ 后续产品/交付/财务等       │
├───────────────────┴────────────────────────┤
│ 本地受控存储与 OS 凭据边界                   │
└────────────────────────────────────────────┘
```

外部依赖包括 Python、Node/Pi、Rust/Tauri、WebView2，以及后续显式配置的模型和搜索提供方。第一阶段不调用外部服务。

## 2. 关键设计决策

### 轻本体 + 插件

- 选择依据：第二业务域不应复制工作台、任务和审批实现。
- 备选：单体多模块。短期简单，但销售耦合容易回流。
- 取舍：第一阶段只允许本地清单插件，不加载任意远程代码，以降低安全和升级复杂度。
- 验证：无销售插件注册测试；新增域只需目录、清单和工作流。

### Python 本地控制面 + TypeScript Pi 扩展

- Python 负责桌面工作台、SQLite 单一事实源和可离线自检。
- TypeScript 负责 Pi 能力目录、DAG 规划和权限检查。
- 两套实现读取同一 JSON 清单；一致性通过相同正向 DAG 和绕过审批负向测试约束。
- Pi 包清单显式声明扩展与 Skill；本地启动命令使用项目内独立 `PI_CODING_AGENT_DIR`，关闭全局扩展、Skill、提示模板、主题、上下文、会话和联网发现，再按 Profile 传入资源路径。
- Pi Agent 的 `active_tools` 精确限制为能力目录、工作流规划和域权限检查三项只读公司工具；扩展还会在会话启动及每次工具调用时重复收敛，阻止 `bash/edit/write` 等内置工具绕过统一 DAG。
- RPC 资源测试分别启动 `company-manager` 与 `company-with-sales`，确认扩展、Profile Skill 和活动工具的实际加载边界。Pi 0.84.2 自带的隐藏 `/llama` 是仅供操作者调用的内置斜杠命令，不属于 Agent 工具；第一阶段仍以离线、未配置模型状态启动。
- 后续应把契约规则生成或共享化，减少长期双实现漂移。

### Profile 的安装、可用与启用语义

- 插件注册表表示“已安装”；Profile 的 `available_domains` 表示可选候选，候选未安装不阻止公司核心启动。
- `enabled_domains` 表示本次组合允许发起的域，必须是已安装候选；默认 `company-manager` 为空。
- HTTP、统一运行时和 Pi 规划入口都拒绝为未启用域发起任务；业务域页仍可把已安装候选显示为“未启用”。
- Profile 不是数据隔离或分库键。所有组合共用统一任务、审批与审计数据库，避免形成平行事实源。

### Tauri 桌面路线

- 保留源项目稳定的“桌面壳 + 本地服务 + Pi 子进程”模型。
- WebView 只导航至 `http://127.0.0.1:8766`，关闭窗口回收子进程树。
- 浏览器模式仅作为无 Rust 环境的诊断入口。

## 3. 受控 DAG

节点类型保留 `agent/tool/subagent/approval/parallel/join/validator`。第一阶段验证以下不变量：

- 节点权限是插件权限子集。
- 工具及其精确节点权限由所属插件清单声明，平台核心不硬编码任何业务域工具。
- DAG 无环，入口和输出与依赖图一致。
- Agent 只能引用插件声明的 Skill。
- Subagent 写入范围必须为空。
- 结构化写入只能发生在 Tool 节点，且只有一个直接 Approval 前驱。
- 一个 Approval 只保护一个直接结构化写入节点。

销售域首个 DAG：

```text
load_sales_context
→ draft_review
→ owner_approval
→ record_actions
→ verify_audit
```

## 4. 权限模型

最终有效权限应为：

```text
用户角色权限
∩ 用户数据作用域
∩ 插件声明权限
∩ 当前 DAG 节点权限
∩ Adapter 固定能力
```

第一阶段已实现插件/节点权限、公司/域/项目作用域字段和审批角色门禁。尚未实现企业身份认证，因此只声明“本机管理员验证模式”，不声明完整 RBAC。

角色规划：

| 角色 | 边界 |
| --- | --- |
| `company-admin` | 平台配置和本地验证；未来不应自动获得全部域数据 |
| `domain-owner` | 本域任务与审批 |
| `member` | 发起和读取授权范围任务，不可审批 |
| `auditor` | 只读审计，不执行、不审批 |

## 5. 审批与审计

审批记录包含：

- `company_id`、`domain_id`、`project_id`
- `requested_by`、`requested_role`、`decided_by`、`decided_role`
- `task_id`、`node_id`
- `policy_id`、`policy_version`
- canonical payload 与 `payload_sha256`
- `storage_binding`、`expected_version`
- `decision`、`reason`、创建/决定时间

批准时重新计算 payload hash 并检查任务版本。任一不一致都会拒绝继续。审计表追加记录任务创建、节点完成、审批请求、审批决定和任务完成；业务 API 没有修改审计事件的入口。

第一阶段没有真实业务写入 Adapter。`record_actions` 只在统一运行库记录合成行动意图，用于证明审批链路，避免误写客户数据。

## 6. 数据隔离

当前所有运行表都强制 `company_id`，业务任务和审批强制 `domain_id`，`project_id` 可选。所有 Profile 共用位于忽略目录的 `runtime/company-platform.db`；Profile 只控制可发起的域工作流，不切分任务事实。

生产演进目标：

```text
runtime/companies/<company_id>/core/
data/companies/<company_id>/knowledge/
data/companies/<company_id>/domains/<domain_id>/
inputs/companies/<company_id>/projects/<project_id>/
outputs/companies/<company_id>/projects/<project_id>/
```

规则：

- 核心库、知识库和业务域数据库分离。
- 域插件只能直接访问本域存储。
- 跨域读取走只读贡献接口；跨域写入分别审批和提交。
- 项目 ID 是作用域和链接，不是域数据主库。
- 缺失公司或业务域作用域时默认拒绝。
- 密钥只进入 OS 凭据存储或运行时环境，不进入数据库、日志或审计。

## 7. 共享能力接入状态

| 能力 | 第一阶段 | 后续验证 |
| --- | --- | --- |
| Pi 主核心 | 隔离资源目录、Profile Skill、三项只读治理工具与桌面启动链路 | 真实模型对话和任务 RPC E2E |
| 模型接入 | Provider 契约、设置状态、密钥边界说明 | OS 凭据存储、发现、调用和失败降级 |
| 聚合搜索 | Provider 契约、本地模式状态、来源要求 | SSRF、防敏感查询、超时、证据正文 E2E |
| 知识库 | 插件、空来源模板、作用域设计 | 独立知识库、检索与审批写入 |
| 项目空间 | 插件、IA、作用域字段 | 创建/成员/归档与跨域链接 |
| 文件处理 | 插件与目录隔离设计 | 上传大小/后缀/符号链接/无覆盖提交 |
| PPT | 插件与 plan/build/render 能力契约 | 新公司品牌模板、LibreOffice 渲染 QA |
| 定时任务 | 仅创建统一 DAG 的契约，默认关闭 | 到期入队、幂等、错过补发和不推进审批 |

源项目中的 PPT Profile 白名单曾将岗位 Profile 与输出能力耦合。新架构按 `plugin capability + domain scope` 判断，不复制该白名单；真实 PPT 迁移时必须增加公司 Profile + 销售域端到端测试。

## 8. 故障、降级与恢复

- 插件或工作流无效：启动验证失败，不跳过错误插件。
- 模型/搜索未配置：共享能力标记待配置，离线核心继续运行。
- 审批载荷或版本变化：拒绝旧审批，不进行补偿性猜测。
- 数据库写入：事务、外键、WAL 和 `synchronous=FULL`；正式版还需崩溃恢复和备份测试。
- 端口占用：Tauri 不接管既有服务。
- 销售域禁用：公司核心与共享能力继续加载；统一任务中心仍保留公司级既有任务、审批和审计视图，域专用页面与新建入口关闭。正式多人版还需按角色和数据作用域细化只读权限。
- 桌面退出：按相反顺序回收 Pi 和工作台进程。
- 正常关窗目前会强制回收子进程树；若恰逢审批写事务，依赖 SQLite WAL 恢复。正式业务写入前需实现“停止接收 → 等待在途事务 → checkpoint/关闭 → 超时强杀”的优雅退出，并验证关闭/重启一致性。

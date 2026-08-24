# 开发说明

## 1. 开发原则

- 平台核心保持轻量，只实现所有业务域共同需要的治理能力。
- 业务概念、存储和页面进入 `plugins/domains/<domain>` 或对应域包。
- 结构化写入必须走统一 DAG、Approval 和审计，不新增平行任务系统。
- 默认使用空数据和合成测试；真实业务数据、密钥、日志和输出不得进入仓库。
- 共享能力按 capability 和 domain scope 授权，不按岗位 Profile 字符串白名单授权。

## 2. 本地校验

```powershell
python -m company_platform validate
python -m company_platform self-test
python -m unittest discover -s tests -p "test_*.py" -v
pnpm check:types
pnpm test:runtime
```

`pnpm test:runtime` 会通过 Pi 0.84.2 的包资源解析器和真实 RPC 进程分别检查两个 Profile：确认公司扩展、`manage-company` 与销售验证 Skill 的加载边界，并断言 `active_tools` 只有三项只读公司治理工具、不含 `bash/edit/write`。仅检查 `pi --version` 不算接入验证。

桌面代码：

```powershell
cargo fmt --manifest-path .\desktop\src-tauri\Cargo.toml -- --check
cargo check --manifest-path .\desktop\src-tauri\Cargo.toml --locked
```

## 3. 添加业务域

1. 新建 `plugins/domains/<domain>/plugin.json`。
2. 使用 `kind=business-domain` 和独立命名空间，如 `domain.delivery`。
3. 声明最小权限、依赖、能力、Skill、工作流和数据作用域。
4. 工作流放在域目录，所有路径必须是相对路径且不能越出插件根。
5. 新增域 Skill；不得修改公司主 Skill 来嵌入域规则。
6. 添加正向加载、权限越界、审批防绕过和无该域时核心可启动测试。
7. 运行 Python 与 TypeScript 两套契约测试。

同时更新 Profile：`available_domains` 表示候选域，可以尚未安装；`enabled_domains` 表示当前组合实际启用的域，必须已安装。默认 `company-manager` 不得硬依赖任何业务域。Profile 不能作为任务数据库分片键，所有组合继续使用统一事实库。

最小插件示例：

```json
{
  "api_version": "company.platform/v1",
  "id": "domain.delivery",
  "version": "1.0.0",
  "kind": "business-domain",
  "display_name": "交付管理",
  "description": "交付业务域。",
  "permissions": ["delivery.read"],
  "tools": [{"name": "delivery.read", "permissions": ["delivery.read"]}],
  "dependencies": [{"id": "platform.project-space", "version": ">=1.0.0"}],
  "capabilities": ["delivery.review"],
  "skills": ["manage-delivery-domain"],
  "workflows": ["workflows/review.json"]
}
```

## 4. DAG 约束

- 节点 ID 唯一，依赖存在且不能指向自身。
- `entry_nodes` 和 `output_nodes` 必须与拓扑一致。
- 节点权限不能超过插件权限。
- Tool 名必须在逻辑工具权限表中登记。
- Agent Skill 必须由插件声明。
- Subagent 第一阶段只读，`write_scope=[]`。
- 任何 `*.write` Tool 只有一个直接 Approval 前驱。
- Approval 只保护一个直接写入，且直接跟随 Agent 或 Validator。

新增逻辑工具时，在所属插件清单的 `tools` 中声明工具名和精确节点权限，并增加对应 Adapter 与正/负向测试。平台核心不得维护销售、交付等业务域工具白名单；Python 与 Pi 都从插件清单校验工具。

## 5. 工作台 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 桌面健康检查 |
| GET | `/api/bootstrap` | 公司工作台启动数据与临时会话令牌 |
| GET | `/api/plugins` | 共享能力、业务域和工作流目录 |
| GET | `/api/tasks` | 统一任务列表 |
| POST | `/api/tasks` | 发起业务域 DAG 任务 |
| GET | `/api/approvals` | 待审批列表 |
| POST | `/api/approvals/<id>/decision` | 批准或驳回 |
| GET | `/api/audit` | 只读审计事件 |

POST 必须发送 `X-Company-Session`，服务只接受当前本机端口的 Host。第一阶段令牌来自 bootstrap，属于本机误操作防护，不是企业用户认证。

## 6. 数据与迁移

- `runtime/` 只放本机任务、审批和审计验证数据。
- `data/**/*.example.*` 必须为空模板或明显合成数据。
- 真实数据迁移要单独定义字段映射、公司/域/项目作用域、备份、激活回执和回滚。
- 未经明确授权，不执行真实数据库迁移、远端发布或插件安装。

## 7. 提交前检查

```powershell
git status --short
git diff --check
git remote -v
python -m unittest discover -s tests -p "test_*.py" -v
pnpm test
```

同时审查：

- 是否出现 `.env`、密钥、数据库、日志、输入输出、安装包或绝对用户路径。
- 是否新增 `docs/reqguard` 或第二套任务依赖。
- 是否让销售或其他业务域进入平台核心默认值。
- 是否声明了没有实际执行的测试或能力。

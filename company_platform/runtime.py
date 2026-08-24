from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

from .plugin_registry import PluginRegistry, Workflow, WorkflowNode


TERMINAL_TASK_STATES = {"completed", "rejected", "failed", "cancelled"}
APPROVER_ROLES = {"company-admin", "domain-owner"}


class RuntimeErrorBase(RuntimeError):
    """统一任务运行时错误。"""


class NotFoundError(RuntimeErrorBase):
    pass


class ConflictError(RuntimeErrorBase):
    pass


class PermissionDeniedError(RuntimeErrorBase):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RuntimeIdentity:
    actor_id: str = "local-admin"
    actor_role: str = "company-admin"
    actor_type: str = "human"


class RuntimeStore:
    """SQLite-backed single source of truth for DAG tasks, approvals, and audit events."""

    def __init__(
        self,
        database_path: Path | str,
        registry: PluginRegistry,
        *,
        company_id: str = "company-local",
        enabled_domains: Iterable[str] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.company_id = company_id
        installed_domains = {plugin.id for plugin in registry.business_domains}
        selected_domains = installed_domains if enabled_domains is None else set(enabled_domains)
        unavailable = sorted(selected_domains - installed_domains)
        if unavailable:
            raise ValueError(f"运行时启用了未安装业务域：{', '.join(unavailable)}")
        self.enabled_domains = frozenset(selected_domains)
        self._initialize()
        self._recover_active_tasks()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    project_id TEXT,
                    workflow_id TEXT NOT NULL,
                    plugin_version TEXT,
                    workflow_fingerprint TEXT,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_scope_status
                    ON tasks(company_id, domain_id, status, updated_at);

                CREATE TABLE IF NOT EXISTS task_nodes (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_summary TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY(task_id, node_id)
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    project_id TEXT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_role TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    plugin_version TEXT,
                    workflow_fingerprint TEXT,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    storage_binding TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    decided_by TEXT,
                    decided_role TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    UNIQUE(task_id, node_id)
                );
                CREATE INDEX IF NOT EXISTS approvals_scope_decision
                    ON approvals(company_id, domain_id, decision, created_at);

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    domain_id TEXT,
                    project_id TEXT,
                    task_id TEXT,
                    node_id TEXT,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result TEXT NOT NULL,
                    policy_id TEXT,
                    payload_sha256 TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_scope_time
                    ON audit_events(company_id, domain_id, created_at);
                """
            )
            self._add_column_if_missing(connection, "tasks", "plugin_version", "TEXT")
            self._add_column_if_missing(connection, "tasks", "workflow_fingerprint", "TEXT")
            self._add_column_if_missing(connection, "approvals", "plugin_version", "TEXT")
            self._add_column_if_missing(connection, "approvals", "workflow_fingerprint", "TEXT")

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _recover_active_tasks(self) -> None:
        """Resume running tasks and invalidate stale waiting approvals after restart."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT task_id, domain_id, workflow_id
                FROM tasks
                WHERE company_id = ? AND status IN ('running', 'waiting_approval')
                ORDER BY created_at, task_id
                """,
                (self.company_id,),
            ).fetchall()
        for row in rows:
            self._advance(row["task_id"])

    def _workflow_fingerprint(self, workflow: Workflow) -> str:
        plugin = self.registry.plugins[workflow.plugin]
        referenced_tools = sorted(
            {
                node.tool
                for node in workflow.nodes
                if node.type == "tool" and node.tool is not None
            }
        )
        contract = {
            "plugin": {
                "api_version": plugin.api_version,
                "id": plugin.id,
                "version": plugin.version,
                "write_permissions": list(plugin.write_permissions),
                "tools": [
                    {
                        "name": plugin.tool_map[name].name,
                        "effect": plugin.tool_map[name].effect,
                        "permissions": list(plugin.tool_map[name].permissions),
                    }
                    for name in referenced_tools
                ],
            },
            "workflow": {
                "id": workflow.id,
                "plugin": workflow.plugin,
                "display_name": workflow.display_name,
                "description": workflow.description,
                "entry_nodes": list(workflow.entry_nodes),
                "output_nodes": list(workflow.output_nodes),
                "nodes": [
                    {
                        "id": node.id,
                        "type": node.type,
                        "depends_on": list(node.depends_on),
                        "permissions": list(node.permissions),
                        "tool": node.tool,
                        "skill": node.skill,
                        "policy": node.policy,
                        "check": node.check,
                        "boundary": node.boundary,
                    }
                    for node in workflow.nodes
                ],
            },
        }
        return sha256_json(contract)

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        result: str,
        identity: RuntimeIdentity,
        domain_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        policy_id: str | None = None,
        payload_sha256: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, company_id, domain_id, project_id, task_id, node_id,
                actor_type, actor_id, actor_role, action, result, policy_id,
                payload_sha256, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                self.company_id,
                domain_id,
                project_id,
                task_id,
                node_id,
                identity.actor_type,
                identity.actor_id,
                identity.actor_role,
                action,
                result,
                policy_id,
                payload_sha256,
                canonical_json(details or {}),
                utc_now(),
            ),
        )

    def create_task(
        self,
        workflow_id: str,
        title: str,
        *,
        project_id: str | None = None,
        identity: RuntimeIdentity | None = None,
    ) -> dict[str, Any]:
        identity = identity or RuntimeIdentity()
        workflow = self.registry.workflows.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"未知工作流：{workflow_id}")
        plugin = self.registry.plugins[workflow.plugin]
        if plugin.kind != "business-domain":
            raise ConflictError("第一阶段任务入口只接受显式业务域工作流")
        if workflow.plugin not in self.enabled_domains:
            raise PermissionDeniedError(f"当前 Profile 未启用业务域：{workflow.plugin}")
        normalized_title = title.strip()
        if not 1 <= len(normalized_title) <= 160:
            raise ValueError("任务标题必须为 1-160 字")
        if project_id is not None and (not project_id or len(project_id) > 128):
            raise ValueError("项目 ID 无效")
        task_id = str(uuid4())
        timestamp = utc_now()
        workflow_fingerprint = self._workflow_fingerprint(workflow)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, company_id, domain_id, project_id, workflow_id,
                    plugin_version, workflow_fingerprint, title,
                    status, version, requested_by, requested_role, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 1, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    self.company_id,
                    workflow.plugin,
                    project_id,
                    workflow.id,
                    plugin.version,
                    workflow_fingerprint,
                    normalized_title,
                    identity.actor_id,
                    identity.actor_role,
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO task_nodes(task_id, node_id, node_type, status)
                VALUES (?, ?, ?, 'pending')
                """,
                [(task_id, node.id, node.type) for node in workflow.nodes],
            )
            self._audit(
                connection,
                action="task.created",
                result="accepted",
                identity=identity,
                domain_id=workflow.plugin,
                project_id=project_id,
                task_id=task_id,
                details={
                    "workflow_id": workflow.id,
                    "plugin_version": plugin.version,
                    "workflow_fingerprint": workflow_fingerprint,
                    "title": normalized_title,
                },
            )
        self._advance(task_id)
        return self.get_task(task_id)

    def _is_write_tool(self, workflow: Workflow, node: WorkflowNode) -> bool:
        plugin = self.registry.plugins[workflow.plugin]
        declared = plugin.tool_map.get(node.tool or "")
        return node.type == "tool" and declared is not None and declared.effect == "write"

    def _node_summary(self, workflow: Workflow, node: WorkflowNode) -> str:
        if node.type == "agent":
            return "已生成仅含建议的分析草稿；尚未改变业务数据。"
        if self._is_write_tool(workflow, node):
            return "已按批准载荷记录业务域行动意图；第一阶段不写入真实业务数据。"
        if node.type == "tool":
            return "已在当前公司与业务域范围内读取空上下文。"
        if node.type == "validator":
            return "已核对公司、业务域、审批和审计作用域。"
        return "节点已完成。"

    def _protected_write_node(self, workflow: Workflow, approval_node: WorkflowNode) -> WorkflowNode:
        protected = [
            node
            for node in workflow.nodes
            if node.depends_on == (approval_node.id,) and self._is_write_tool(workflow, node)
        ]
        if len(protected) != 1:
            raise ConflictError(f"审批节点 {approval_node.id} 未唯一绑定结构化写入")
        return protected[0]

    def _approval_payload(
        self,
        task: sqlite3.Row,
        workflow: Workflow,
        node: WorkflowNode,
        *,
        task_version: int | None = None,
    ) -> dict[str, Any]:
        plugin = self.registry.plugins[workflow.plugin]
        protected_write = self._protected_write_node(workflow, node)
        return {
            "schema_version": "1.0",
            "company_id": task["company_id"],
            "domain_id": task["domain_id"],
            "project_id": task["project_id"],
            "task_id": task["task_id"],
            "task_version": task["version"] if task_version is None else task_version,
            "workflow_id": workflow.id,
            "plugin_version": plugin.version,
            "workflow_fingerprint": self._workflow_fingerprint(workflow),
            "approval_node_id": node.id,
            "protected_write_node_id": protected_write.id,
            "protected_tool": protected_write.tool,
            "title": task["title"],
            "proposed_change": f"记录“{workflow.display_name}”的业务行动意图（空数据验证）",
        }

    @staticmethod
    def _storage_binding(task: sqlite3.Row) -> str:
        return f"sqlite://{task['company_id']}/{task['domain_id']}/task-intents"

    def _fail_stale_contract(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        identity: RuntimeIdentity,
        reason: str,
        *,
        node_id: str | None = None,
        policy_id: str | None = None,
        payload_sha256: str | None = None,
    ) -> None:
        timestamp = utc_now()
        connection.execute(
            """
            UPDATE task_nodes
            SET status = 'failed', result_summary = ?, completed_at = ?
            WHERE task_id = ? AND status NOT IN ('completed', 'rejected', 'failed', 'cancelled')
            """,
            (reason, timestamp, task["task_id"]),
        )
        connection.execute(
            """
            UPDATE tasks
            SET status = 'failed', version = version + 1, updated_at = ?
            WHERE task_id = ?
            """,
            (timestamp, task["task_id"]),
        )
        connection.execute(
            """
            UPDATE approvals
            SET decision = 'invalidated', decided_by = ?, decided_role = ?,
                reason = ?, decided_at = ?
            WHERE task_id = ? AND decision = 'pending'
            """,
            (
                identity.actor_id,
                identity.actor_role,
                reason,
                timestamp,
                task["task_id"],
            ),
        )
        self._audit(
            connection,
            action="runtime.contract_invalidated",
            result="rejected",
            identity=identity,
            domain_id=task["domain_id"],
            project_id=task["project_id"],
            task_id=task["task_id"],
            node_id=node_id,
            policy_id=policy_id,
            payload_sha256=payload_sha256,
            details={"reason": reason},
        )

    def _advance(self, task_id: str) -> None:
        system_identity = RuntimeIdentity("dag-runtime", "platform-runtime", "system")
        while True:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ? AND company_id = ?",
                    (task_id, self.company_id),
                ).fetchone()
                if task is None:
                    raise NotFoundError(f"任务不存在：{task_id}")
                if task["status"] in TERMINAL_TASK_STATES:
                    return
                workflow = self.registry.workflows.get(task["workflow_id"])
                if workflow is None or workflow.plugin != task["domain_id"]:
                    self._fail_stale_contract(
                        connection,
                        task,
                        system_identity,
                        "运行任务对应的工作流已删除或业务域已变化，需重新发起。",
                    )
                    return
                plugin = self.registry.plugins[workflow.plugin]
                workflow_fingerprint = self._workflow_fingerprint(workflow)
                if (
                    task["plugin_version"] != plugin.version
                    or task["workflow_fingerprint"] != workflow_fingerprint
                ):
                    self._fail_stale_contract(
                        connection,
                        task,
                        system_identity,
                        "插件版本或工作流规范已变化，旧任务已停止，需重新发起并审批。",
                    )
                    return
                if task["status"] == "waiting_approval":
                    return
                state_rows = connection.execute(
                    "SELECT node_id, status FROM task_nodes WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
                states = {row["node_id"]: row["status"] for row in state_rows}
                ready = [
                    node
                    for node in workflow.nodes
                    if states[node.id] == "pending"
                    and all(states[dependency] == "completed" for dependency in node.depends_on)
                ]
                if not ready:
                    if all(status == "completed" for status in states.values()):
                        timestamp = utc_now()
                        connection.execute(
                            "UPDATE tasks SET status = 'completed', version = version + 1, updated_at = ? WHERE task_id = ?",
                            (timestamp, task_id),
                        )
                        self._audit(
                            connection,
                            action="task.completed",
                            result="accepted",
                            identity=system_identity,
                            domain_id=task["domain_id"],
                            project_id=task["project_id"],
                            task_id=task_id,
                        )
                        return
                    raise ConflictError(f"任务 {task_id} 没有可推进节点")
                for node in ready:
                    timestamp = utc_now()
                    if node.type == "approval":
                        payload = self._approval_payload(task, workflow, node)
                        payload_hash = sha256_json(payload)
                        approval_id = str(uuid4())
                        connection.execute(
                            """
                            INSERT INTO approvals (
                                approval_id, company_id, domain_id, project_id, task_id, node_id,
                                requested_by, requested_role, policy_id, policy_version,
                                plugin_version, workflow_fingerprint, payload_json, payload_sha256,
                                storage_binding, expected_version, decision, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '1', ?, ?, ?, ?, ?, ?, 'pending', ?)
                            """,
                            (
                                approval_id,
                                self.company_id,
                                task["domain_id"],
                                task["project_id"],
                                task_id,
                                node.id,
                                task["requested_by"],
                                task["requested_role"],
                                node.policy,
                                plugin.version,
                                workflow_fingerprint,
                                canonical_json(payload),
                                payload_hash,
                                self._storage_binding(task),
                                task["version"],
                                timestamp,
                            ),
                        )
                        connection.execute(
                            "UPDATE task_nodes SET status = 'waiting', started_at = ? WHERE task_id = ? AND node_id = ?",
                            (timestamp, task_id, node.id),
                        )
                        connection.execute(
                            "UPDATE tasks SET status = 'waiting_approval', updated_at = ? WHERE task_id = ?",
                            (timestamp, task_id),
                        )
                        self._audit(
                            connection,
                            action="approval.requested",
                            result="accepted",
                            identity=system_identity,
                            domain_id=task["domain_id"],
                            project_id=task["project_id"],
                            task_id=task_id,
                            node_id=node.id,
                            policy_id=node.policy,
                            payload_sha256=payload_hash,
                            details={"approval_id": approval_id},
                        )
                        return
                    if self._is_write_tool(workflow, node):
                        approval_node_id = node.depends_on[0]
                        approval = connection.execute(
                            """
                            SELECT * FROM approvals
                            WHERE task_id = ? AND node_id = ? AND decision = 'approved'
                            """,
                            (task_id, approval_node_id),
                        ).fetchone()
                        if approval is None or approval["expected_version"] != task["version"] - 1:
                            raise ConflictError("写入节点没有与当前任务版本绑定的有效审批")
                        approval_node = workflow.node_map.get(approval_node_id)
                        if approval_node is None or approval_node.type != "approval":
                            self._fail_stale_contract(
                                connection,
                                task,
                                system_identity,
                                "已批准节点不再属于当前工作流，旧批准不得执行。",
                                node_id=node.id,
                                payload_sha256=approval["payload_sha256"],
                            )
                            return
                        try:
                            approved_payload = json.loads(approval["payload_json"])
                        except json.JSONDecodeError as error:
                            raise ConflictError("写入节点审批载荷无效") from error
                        expected_payload = self._approval_payload(
                            task,
                            workflow,
                            approval_node,
                            task_version=approval["expected_version"],
                        )
                        binding_matches = (
                            approval["storage_binding"] == self._storage_binding(task)
                            and approval["policy_id"] == approval_node.policy
                            and approval["policy_version"] == "1"
                            and approval["plugin_version"] == plugin.version
                            and approval["workflow_fingerprint"] == workflow_fingerprint
                            and sha256_json(approved_payload) == approval["payload_sha256"]
                            and canonical_json(approved_payload) == canonical_json(expected_payload)
                        )
                        if not binding_matches:
                            self._fail_stale_contract(
                                connection,
                                task,
                                system_identity,
                                "审批绑定的插件版本、工作流、策略、载荷或存储范围已变化，旧批准不得执行。",
                                node_id=node.id,
                                policy_id=approval["policy_id"],
                                payload_sha256=approval["payload_sha256"],
                            )
                            return
                    summary = self._node_summary(workflow, node)
                    connection.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'completed', result_summary = ?, started_at = COALESCE(started_at, ?), completed_at = ?
                        WHERE task_id = ? AND node_id = ?
                        """,
                        (summary, timestamp, timestamp, task_id, node.id),
                    )
                    self._audit(
                        connection,
                        action="node.completed",
                        result="accepted",
                        identity=system_identity,
                        domain_id=task["domain_id"],
                        project_id=task["project_id"],
                        task_id=task_id,
                        node_id=node.id,
                        details={"node_type": node.type, "summary": summary},
                    )

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        reason: str = "",
        identity: RuntimeIdentity | None = None,
    ) -> dict[str, Any]:
        identity = identity or RuntimeIdentity()
        if identity.actor_role not in APPROVER_ROLES:
            raise PermissionDeniedError("当前角色没有审批权限")
        if decision not in {"approved", "rejected"}:
            raise ValueError("审批决定必须是 approved 或 rejected")
        if len(reason) > 500:
            raise ValueError("审批意见不能超过 500 字")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            approval = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ? AND company_id = ?",
                (approval_id, self.company_id),
            ).fetchone()
            if approval is None:
                raise NotFoundError(f"审批不存在：{approval_id}")
            if approval["decision"] != "pending":
                raise ConflictError("该审批已经处理")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND company_id = ?",
                (approval["task_id"], self.company_id),
            ).fetchone()
            if task is None or task["status"] != "waiting_approval":
                raise ConflictError("审批对应任务状态已变化")
            workflow = self.registry.workflows.get(task["workflow_id"])
            approval_node: WorkflowNode | None = None
            payload: Any = None
            stale_reason: str | None = None
            if approval["expected_version"] != task["version"]:
                stale_reason = "任务版本已变化，旧审批失效"
            elif workflow is None or workflow.plugin != task["domain_id"]:
                stale_reason = "审批对应工作流已删除或业务域已变化"
            else:
                plugin = self.registry.plugins[workflow.plugin]
                workflow_fingerprint = self._workflow_fingerprint(workflow)
                if (
                    task["plugin_version"] != plugin.version
                    or task["workflow_fingerprint"] != workflow_fingerprint
                    or approval["plugin_version"] != plugin.version
                    or approval["workflow_fingerprint"] != workflow_fingerprint
                ):
                    stale_reason = "插件版本或工作流规范已变化，旧审批失效"
                else:
                    approval_node = workflow.node_map.get(approval["node_id"])
                    if approval_node is None or approval_node.type != "approval":
                        stale_reason = "审批节点已不属于当前工作流"
                    elif approval["policy_id"] != approval_node.policy or approval["policy_version"] != "1":
                        stale_reason = "审批策略已变化"
                    elif approval["storage_binding"] != self._storage_binding(task):
                        stale_reason = "审批存储绑定已变化"
                    else:
                        try:
                            payload = json.loads(approval["payload_json"])
                        except json.JSONDecodeError:
                            stale_reason = "审批载荷不是有效 JSON"
                        if stale_reason is None and sha256_json(payload) != approval["payload_sha256"]:
                            stale_reason = "审批载荷哈希不一致"
                        if (
                            stale_reason is None
                            and canonical_json(payload)
                            != canonical_json(self._approval_payload(task, workflow, approval_node))
                        ):
                            stale_reason = "审批载荷与当前任务不一致"
            if stale_reason is not None:
                timestamp = utc_now()
                system_identity = RuntimeIdentity("dag-runtime", "platform-runtime", "system")
                connection.execute(
                    """
                    UPDATE approvals
                    SET decision = 'invalidated', decided_by = ?, decided_role = ?,
                        reason = ?, decided_at = ?
                    WHERE approval_id = ?
                    """,
                    (
                        system_identity.actor_id,
                        system_identity.actor_role,
                        stale_reason,
                        timestamp,
                        approval_id,
                    ),
                )
                self._fail_stale_contract(
                    connection,
                    task,
                    system_identity,
                    f"{stale_reason}；任务已停止，需按当前规范重新发起并审批。",
                    node_id=approval["node_id"],
                    policy_id=approval["policy_id"],
                    payload_sha256=approval["payload_sha256"],
                )
                # 先持久化失效状态，再向调用方返回冲突；避免待审批永久悬挂。
                connection.commit()
                raise ConflictError(stale_reason)
            timestamp = utc_now()
            connection.execute(
                """
                UPDATE approvals
                SET decision = ?, decided_by = ?, decided_role = ?, reason = ?, decided_at = ?
                WHERE approval_id = ?
                """,
                (decision, identity.actor_id, identity.actor_role, reason.strip(), timestamp, approval_id),
            )
            if decision == "approved":
                connection.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'completed', result_summary = '审批已通过。', completed_at = ?
                    WHERE task_id = ? AND node_id = ?
                    """,
                    (timestamp, task["task_id"], approval["node_id"]),
                )
                connection.execute(
                    """
                    UPDATE tasks SET status = 'running', version = version + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (timestamp, task["task_id"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'rejected', result_summary = '审批已驳回。', completed_at = ?
                    WHERE task_id = ? AND node_id = ?
                    """,
                    (timestamp, task["task_id"], approval["node_id"]),
                )
                connection.execute(
                    """
                    UPDATE tasks SET status = 'rejected', version = version + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (timestamp, task["task_id"]),
                )
            self._audit(
                connection,
                action="approval.decided",
                result=decision,
                identity=identity,
                domain_id=approval["domain_id"],
                project_id=approval["project_id"],
                task_id=approval["task_id"],
                node_id=approval["node_id"],
                policy_id=approval["policy_id"],
                payload_sha256=approval["payload_sha256"],
                details={"approval_id": approval_id, "reason": reason.strip()},
            )
        if decision == "approved":
            self._advance(approval["task_id"])
        return self.get_task(approval["task_id"])

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND company_id = ?",
                (task_id, self.company_id),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"任务不存在：{task_id}")
            nodes = connection.execute(
                "SELECT * FROM task_nodes WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            approvals = connection.execute(
                "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        result = self._row_dict(task)
        result["nodes"] = [self._row_dict(row) for row in nodes]
        result["approvals"] = [self._public_approval(row) for row in approvals]
        return result

    @staticmethod
    def _public_approval(row: sqlite3.Row) -> dict[str, Any]:
        item = {key: row[key] for key in row.keys() if key != "payload_json"}
        item["payload"] = json.loads(row["payload_json"])
        return item

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE company_id = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (self.company_id, safe_limit),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def list_pending_approvals(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approvals
                WHERE company_id = ? AND decision = 'pending'
                ORDER BY created_at ASC LIMIT ?
                """,
                (self.company_id, safe_limit),
            ).fetchall()
        return [self._public_approval(row) for row in rows]

    def list_audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_events WHERE company_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (self.company_id, safe_limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._row_dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def record_platform_audit(
        self,
        action: str,
        result: str,
        details: dict[str, Any],
        *,
        payload_sha256: str | None = None,
        project_id: str | None = None,
        identity: RuntimeIdentity | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Append a non-task platform capability event to the unified audit log."""
        normalized_action = action.strip()
        if (
            not 1 <= len(normalized_action) <= 160
            or not normalized_action.startswith("library.")
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in normalized_action)
        ):
            raise ValueError("资料库审计动作无效")
        if result not in {"accepted", "rejected", "failed"}:
            raise ValueError("资料库审计结果无效")
        if not isinstance(details, dict) or len(canonical_json(details).encode("utf-8")) > 32 * 1024:
            raise ValueError("资料库审计详情无效或过大")
        if payload_sha256 is not None and (
            len(payload_sha256) != 64
            or any(character not in "0123456789abcdef" for character in payload_sha256)
        ):
            raise ValueError("资料库审计哈希无效")
        if project_id is not None and (not project_id or len(project_id) > 128):
            raise ValueError("项目编号无效")
        if connection is not None:
            databases = {
                Path(row[2]).resolve()
                for row in connection.execute("PRAGMA database_list").fetchall()
                if row[1] == "main" and row[2]
            }
            if self.database_path.resolve() not in databases:
                raise ValueError("资料库与统一审计必须使用同一个数据库")
            self._audit(
                connection,
                action=normalized_action,
                result=result,
                identity=identity or RuntimeIdentity(),
                project_id=project_id,
                node_id="platform.library",
                payload_sha256=payload_sha256,
                details=details,
            )
            return
        with self._connection() as owned_connection:
            self._audit(
                owned_connection,
                action=normalized_action,
                result=result,
                identity=identity or RuntimeIdentity(),
                project_id=project_id,
                node_id="platform.library",
                payload_sha256=payload_sha256,
                details=details,
            )

    def dashboard(self) -> dict[str, int]:
        with self._connection() as connection:
            task_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM tasks
                    WHERE company_id = ? GROUP BY status
                    """,
                    (self.company_id,),
                ).fetchall()
            }
            pending = connection.execute(
                "SELECT COUNT(*) FROM approvals WHERE company_id = ? AND decision = 'pending'",
                (self.company_id,),
            ).fetchone()[0]
        return {
            "running_tasks": task_counts.get("running", 0) + task_counts.get("waiting_approval", 0),
            "pending_approvals": pending,
            "completed_tasks": task_counts.get("completed", 0),
            "enabled_domains": len(self.enabled_domains),
        }

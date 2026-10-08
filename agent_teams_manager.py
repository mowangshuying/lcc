"""agent_teams_manager.py —— Lane C：Agent 团队管理器。

移植自 learn-claude-code/s13_agent_teams/code.py：
  L1146-1406  团队协议台账（四本账 + 请求-响应核销 + Plan Gate）
  L1409-1445  空闲任务发现（claim_next_task，pull 车道）
  L1448-1789  TeammateRuntime 常驻线程 + spawn_teammate 入职办证处
  L1792-1890  Lead 团队七工具
  L1968-2038  TEAMMATE_TOOLS / TEAM_TOOLS 工具定义

本模块封装三类职责：
  1. 控制协议台账：pending_requests 案卷柜、match_response 核销口、
     plan_gates 权限闸门、plan_request_ids 路由账、active_teammates 名册；
  2. TeammateRuntime：一人一线程一对话史，睡邮箱、合作式退场；
  3. Lead 侧团队工具 handler 与 schema，供 Lane D 并入 ToolsManager。

============================ 注入契约（Lane D 提供） ============================
本模块刻意不 import tools_manager / hooks / permission，避免环状依赖；
外部能力全部经构造参数注入：

1. tool_adapters: dict[str, Callable[..., str]]
   键为 BASH / READ_FILE / WRITE_FILE / EDIT_FILE / GLOB 五个工具名。
   调用约定：adapters[工具名](params: dict, cwd: str) -> str
     - params 即 tool_use block.input 的原始参数字典（键名与 lcc schema 一致：
       command / path,limit / path,content / path,old_string,new_string / pattern）；
     - cwd 为 TeammateRuntime 经租约校验（assignment_cwd）后的工作目录字符串，
       Lane D 需给 tools_manager 各 run_* 增加 cwd 参数后在此绑定。

2. permission_check: Callable[[block, prompt_user: bool], str | None] | None
   对应 Lane D 为 Permission.check_permission 新增的 prompt_user 形参。
   队友工具闸门按 check_permission(block, prompt_user=False) 调用：
   命中规则自动拒绝、不抢占控制台（控制台是 Lead 的资产），返回拒绝文案。
   未注入（None）时跳过该层（视为放行，由 hooks/工具自身兜底）。

3. hooks_trigger: Callable[..., str | None] | None
   调用形式：
     hooks_trigger("PreToolUse", block, skip_permission=True)
     hooks_trigger("PostToolUse", block, output)
   Lane D 须绑定到"队友安全"的 Hooks.trigger_hooks 变体：PreToolUse 链中
   必须跳过交互式 permission_hook（权限已在上一步以 prompt_user=False 手动
   检过，避免双重询问/双重拒绝）；返回非 None 视为拦截文案并阻断执行。
   未注入（None）时跳过钩子层。

============================ 锁序铁律 ============================
task_store_lock() → team_lock，全程单向；发布邮件（bus.send）一律放锁外。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from env import Env
from log import log_info, log_warn
from message_bus import MessageBus, RESERVED_TEAMMATE_NAMES, is_valid_agent_name
from task_manager import TaskManager, Task
from worktree_manager import WorktreeManager
from tool_names import (
    BASH, READ_FILE, WRITE_FILE, EDIT_FILE, GLOB,
    LIST_TASKS, CLAIM_TASK, COMPLETE_TASK,
    SPAWN_TEAMMATE, LIST_TEAMMATES, SEND_MESSAGE, REQUEST_SHUTDOWN,
    REQUEST_PLAN, REVIEW_PLAN, CREATE_WORKTREE, SUBMIT_PLAN,
)


# ===== 协议案卷 =====

# 一案一卷。注意 sender/target 方向在两种协议间是颠倒的——shutdown 是
# lead→teammate 发起，plan_approval 是 teammate→lead 发起；match_response
# 的镜像校验依据正是这个方向性。work_version + task_id = 授权快照：批准只
# 覆盖"提交那一刻的这个版本这份工作"，换活时 advance_assignment_version
# bump 版本号，旧批准在 apply_plan_response 校验处自动作废——防"一次批准=终身执照"。
@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)


def _last_assistant_text(content) -> str:
    """取回复里第一个 text 块；SDK 对象(getattr)/dict 两种形状都认。"""
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", "")).strip()
    return ""


# ===== 队友工具集零件（lcc tools_manager 同款书写字面量，不 import 以免成环）=====

_TEAMMATE_BASE_TOOLS = [
    {
        "name": BASH,
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            # 不给队友 run_in_background：后台任务归 Lead 调度
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": READ_FILE,
        "description": "Read file contents",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": WRITE_FILE,
        "description": "Write content to a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": EDIT_FILE,
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": GLOB,
        "description": "Find files matching a glob pattern; ** matches recursively.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]

# 三件任务工具与 lcc tools_manager 同款字面量，TEAMMATE_TOOLS 用 next(...) 借用
_BORROWED_TASK_TOOLS = [
    {
        "name": LIST_TASKS,
        "description": "List tasks with status, owner, and dependencies.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": CLAIM_TASK,
        "description": "Claim a pending task whose dependencies are complete.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": COMPLETE_TASK,
        "description": "Complete the task claimed by this agent.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]


class AgentTeamsManager:
    """Agent 团队：协议台账 + 常驻队友线程 + Lead 团队工具。"""

    # 与总线等待超时同值：一个心跳同时管"看邮箱"和"扫任务板"两张嘴
    IDLE_SCAN_INTERVAL = 2.0

    # 队友工具集 = BASE 五件套 + send_message + submit_plan + 三件 next(...) 借用。
    # 刻意没有 create_task/update_task/spawn/worktree：只有 Lead 能改依赖图、派活、开隔离区。
    TEAMMATE_TOOLS = [
        *_TEAMMATE_BASE_TOOLS,
        {
            "name": SEND_MESSAGE,
            "description": "Send an intermediate message to 'lead' or an active teammate.",
            "input_schema": {
                "type": "object",
                "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
                "required": ["to", "content"],
            },
        },
        {
            "name": SUBMIT_PLAN,
            "description": "Submit a work plan for Lead approval.",
            "input_schema": {
                "type": "object",
                "properties": {"plan": {"type": "string"}},
                "required": ["plan"],
            },
        },
        next(t for t in _BORROWED_TASK_TOOLS if t["name"] == LIST_TASKS),
        next(t for t in _BORROWED_TASK_TOOLS if t["name"] == CLAIM_TASK),
        next(t for t in _BORROWED_TASK_TOOLS if t["name"] == COMPLETE_TASK),
    ]

    # Lead 专属七件。schema 双胞胎：spawn 的 name 正则 = spawn_teammate 代码闸门的
    # 声明层副本；create_worktree 的 pattern 带 '..' 负向前瞻 = 防御层互不信任。
    # 声明层校验理论上可被恶意客户端绕过，但把约束写在模型看得见的地方本身即教学。
    TEAM_TOOLS = [
        {
            "name": SPAWN_TEAMMATE,
            "description": "Spawn a persistent teammate.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                    "role": {"type": "string"},
                    "prompt": {"type": "string"},
                    "task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                    "require_plan": {"type": "boolean"},
                },
                "required": ["name", "role", "prompt"],
            },
        },
        {
            "name": LIST_TEAMMATES,
            "description": "List active teammates.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": SEND_MESSAGE,
            "description": "Message a teammate.",
            "input_schema": {
                "type": "object",
                "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
                "required": ["to", "content"],
            },
        },
        {
            "name": REQUEST_SHUTDOWN,
            "description": "Ask a teammate to shut down.",
            "input_schema": {
                "type": "object",
                "properties": {"teammate": {"type": "string"}},
                "required": ["teammate"],
            },
        },
        {
            "name": REQUEST_PLAN,
            "description": "Require a teammate plan before workspace changes.",
            "input_schema": {
                "type": "object",
                "properties": {"teammate": {"type": "string"}, "task": {"type": "string"}},
                "required": ["teammate", "task"],
            },
        },
        {
            "name": REVIEW_PLAN,
            "description": "Approve or reject a plan.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "feedback": {"type": "string"},
                },
                "required": ["request_id", "approve"],
            },
        },
        {
            "name": CREATE_WORKTREE,
            "description": "Create and bind a task worktree.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "pattern": "^(?!.*\\.\\.)[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                        "maxLength": 64,
                    },
                    "task_id": {"type": "string"},
                },
                "required": ["name", "task_id"],
                "additionalProperties": False,
            },
        },
    ]

    def __init__(self, env: Env, bus: MessageBus,
                 task_manager: TaskManager, worktree_manager: WorktreeManager,
                 client, model: str,
                 tool_adapters: dict[str, Callable[..., str]],
                 permission_check: Callable[..., str | None] | None = None,
                 hooks_trigger: Callable[..., str | None] | None = None):
        self.env = env
        self.bus = bus
        self.taskManager = task_manager
        self.worktreeManager = worktree_manager
        self.client = client
        self.model = model
        # 见文件头注入契约
        self.toolAdapters = tool_adapters
        self.permissionCheck = permission_check
        self.hooksTrigger = hooks_trigger

        ### 台账四本账 + 线程名册（team_lock 统管全部协议可变状态）
        # activeTeammates  = 活跃度轴：working | waiting_approval | idle | stopping
        # planGates        = 权限轴：not_required | required | pending | approved | rejected
        # planRequestIds   = 路由轴：队友 → 当前在审案卷号
        # pendingRequests  = 案卷柜：request_id → ProtocolState（只放内存，协议天生短命）
        # teammateThreads  = 生命周期名册：只记账，从不 join 从不 kill，线程之死全靠自愿退场
        self.teamLock = threading.RLock()
        self.activeTeammates: dict[str, str] = {}
        self.planGates: dict[str, str] = {}
        self.planRequestIds: dict[str, str] = {}
        self.pendingRequests: dict[str, ProtocolState] = {}
        self.teammateThreads: dict[str, threading.Thread] = {}

        ### 接线 TaskManager 回调。三个回调均在持有 task 锁时被调，
        ### 回调内取 team_lock 符合 task_store_lock → team_lock 锁序铁律。
        task_manager.on_assignment_advanced = self._on_assignment_advanced
        task_manager.on_assignment_released = self._on_assignment_released
        task_manager.plan_gate_check = self._plan_gate_check

        ### Lead 团队工具分派表（name → bound method），供 Lane D 并入 toolsHandlers
        self.teamToolHandlers = {
            SPAWN_TEAMMATE: self.run_spawn_teammate,
            LIST_TEAMMATES: self.run_list_teammates,
            SEND_MESSAGE: self.run_send_message,
            REQUEST_SHUTDOWN: self.run_request_shutdown,
            REQUEST_PLAN: self.run_request_plan,
            REVIEW_PLAN: self.run_review_plan,
            CREATE_WORKTREE: self.run_create_worktree,
        }

    # ===== 访问器（lcc 惯例）=====

    def team_schemas(self) -> list:
        """Lead 侧团队工具 schema 清单。"""
        return self.TEAM_TOOLS

    def teammate_schemas(self) -> list:
        """队友工具集 schema 清单（runtime 内部自用，此处对外可读）。"""
        return self.TEAMMATE_TOOLS

    # ===== TaskManager 回调（换活/退租时的团队侧联动）=====

    def _on_assignment_advanced(self, owner: str) -> None:
        """换活即换批准：已发过牌的队友闸门重置为 required，路由单作废。"""
        # 调用方已持 task 锁；此处按 task→team 锁序取 team_lock
        with self.teamLock:
            if (owner in self.planGates
                    and self.planGates[owner] != "not_required"):
                self.planGates[owner] = "required"
            self.planRequestIds.pop(owner, None)

    def _on_assignment_released(self, owner: str) -> None:
        """退租后闸门置 not_required：下一段工作不再受本次审批牵制。"""
        with self.teamLock:
            self.planGates[owner] = "not_required"

    def _plan_gate_check(self, owner: str) -> str | None:
        """complete_task 的闸门回调：未获批不许交活。返回非 None 即拒绝并回显文案。

        有意差异：契约只回传 owner，拒绝文案不含 task_id（s13 原文案含）。
        """
        with self.teamLock:
            gate = self.planGates.get(owner, "not_required")
        if gate in {"required", "pending", "rejected"}:
            return f"Cannot complete while plan status is {gate}"
        return None

    # ===== 协议案卷操作 =====

    def new_request_id(self) -> str:
        """req_%06d 随机抽到不撞号为止。安全性不靠"ID 猜不到"——对账靠案卷柜命中+四字段吻合。"""
        while True:
            request_id = f"req_{random.randint(0, 999999):06d}"
            with self.teamLock:
                if request_id not in self.pendingRequests:
                    return request_id

    def match_response(self, response_type: str, request_id: str,
                       approve: bool, from_agent: str, to_agent: str) -> bool:
        """唯一的"响应核销"口：四道关，软失败永不 raise。

        ①request_id 必须在案卷柜命中（拒幻觉 ID）②期望响应类型从案卷 type 查映射表
        得出（来信不能自报类型）③镜像身份：from==state.target 且 to==state.sender
        ——没人能替别人回话 ④案卷仍 pending——一次性、防重放。
        它是 state.status 的唯一写者；刻意不碰执行账本（gates/teammates）——
        "裁决"与"执行"分离，落地是 apply_plan_response / apply_shutdown_request 的事。
        """
        with self.teamLock:
            state = self.pendingRequests.get(request_id)
            if not state:
                log_warn("team", f"protocol: unknown request_id {request_id}")
                return False
            expected = {
                "shutdown": "shutdown_response",
                "plan_approval": "plan_approval_response",
            }[state.type]
            if response_type != expected:
                log_warn("team",
                         f"protocol: expected {expected}, got {response_type}")
                return False
            if from_agent != state.target or to_agent != state.sender:
                log_warn("team", f"protocol: {request_id} responder mismatch")
                return False
            if state.status != "pending":
                log_warn("team", f"protocol: {request_id} already {state.status}")
                return False
            state.status = "approved" if approve else "rejected"
            log_info("team", f"protocol: {request_id} -> {state.status}")
            return True

    def consume_lead_inbox(self) -> list:
        """Lead 侧唯一邮箱消费者：协议回执先由内核核销，原始消息返回给上层渲染。

        凡 metadata 带 request_id 且 type 以 _response 结尾的自动 match_response；
        approve 字段缺失默认 False = fail-closed——"没明说批准就永远不算批准"。
        "宿主先结账，模型后看事件"：模型不是会计。
        """
        msgs = self.bus.read("lead")
        for msg in msgs:
            metadata = msg.get("metadata", {})
            request_id = metadata.get("request_id", "")
            if request_id and msg.get("type", "").endswith("_response"):
                self.match_response(msg["type"], request_id,
                                    metadata.get("approve", False),
                                    msg.get("from", ""), msg.get("to", ""))
        return msgs

    def format_team_events(self, msgs: list) -> str:
        """把 request_id 渲染进文本，Lead 模型才能抄回 review_plan 的参数——
        闭环"提交→信封→案卷→展示→模型引用→再对账"。"""
        lines = []
        for msg in msgs:
            metadata = msg.get("metadata", {})
            request_id = metadata.get("request_id")
            suffix = f" request_id={request_id}" if request_id else ""
            lines.append(f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}")
        return "[Team events]\n" + "\n".join(lines)

    def current_work_identity(self, owner: str) -> tuple:
        """一把 task 锁读出 (版本号, 在办任务) = "此刻干的是哪版哪活"，
        是 apply_plan_response / run_review_plan 快照校验的输入端。"""
        with self.taskManager.task_store_lock():
            assignment = self.taskManager.assignments.get(owner)
            task_id = str(assignment["task_id"]) if assignment else None
            return self.taskManager.assignment_versions.get(owner, 0), task_id

    # ===== 队友侧协议动作 =====

    def _teammate_submit_plan(self, name: str, plan: str) -> str:
        """提交工作计划入案卷柜，一次写四本账。

        锁序固定 task_store_lock→team_lock（与 advance_assignment_version 同款）。
        防刷屏关：gate 已 pending 再提交直接拒绝——不变量"一人只有一宗在审案"，
        否则新案卷覆盖路由单、Lead 批旧案对不上号、旧案成幽灵；rejected 后允许
        重提（"改稿再审"是正常循环）。bus.send 放锁外：缩短持锁、避免引入第三把锁。
        """
        with self.taskManager.task_store_lock():
            assignment = self.taskManager.assignments.get(name)
            task_id = str(assignment["task_id"]) if assignment else None
            work_version = self.taskManager.assignment_versions.get(name, 0)
            with self.teamLock:
                if self.planGates.get(name) == "pending":
                    return "A plan is already waiting for review."
                request_id = self.new_request_id()
                self.pendingRequests[request_id] = ProtocolState(
                    request_id=request_id,
                    type="plan_approval",
                    sender=name,
                    target="lead",
                    status="pending",
                    payload=plan,
                    work_version=work_version,
                    task_id=task_id,
                )
                self.planGates[name] = "pending"
                self.planRequestIds[name] = request_id
                self.activeTeammates[name] = "waiting_approval"
        self.bus.send(name, "lead", plan, "plan_approval_request",
                      {"request_id": request_id})
        return f"Plan submitted ({request_id}). Wait for Lead's decision."

    def _run_teammate_tool(self, name: str, block, handlers: dict) -> str:
        """队友工具调用总闸：plan gate 只查 bash/write_file/edit_file"干活三件套"，
        只读工具永远放行；not_required（spawn 时免审）全程直通。
        complete_task 由 TaskManager 的 plan_gate_check 再查一道——
        "未获批不能干活，也不能交活"，同一道闸管两头。

        有意差异：PreToolUse 钩子返回非 None 时按 lcc execute_tool 惯例拦截
        （s13 忽略了该返回值），fail-closed。
        """
        with self.teamLock:
            gate = self.planGates.get(name, "not_required")
        if block.name in {BASH, WRITE_FILE, EDIT_FILE}:
            if gate != "approved":
                if gate != "not_required":
                    return (f"Blocked: plan status is {gate}. Submit or revise the "
                            "plan and wait for approval before changing the workspace.")
            # 控制台是 Lead 的资产：队友的危险命令 prompt_user=False 自动拒绝并提示
            # "ask the Lead"——拒绝但不静默，引导升级。先手动查权限，
            # 再由下面 trigger_hooks(skip_permission=True) 防止权限被双重检查
            if self.permissionCheck is not None:
                blocked = self.permissionCheck(block, False)
                if blocked:
                    return blocked
        handler = handlers.get(block.name)
        if not handler:
            return f"Unknown tool: {block.name}"
        if self.hooksTrigger is not None:
            blocked = self.hooksTrigger("PreToolUse", block, skip_permission=True)
            if blocked:
                return str(blocked)
        try:
            output = str(handler(**block.input))
        except Exception as exc:
            output = f"Error: {type(exc).__name__}: {exc}"
        if self.hooksTrigger is not None:
            self.hooksTrigger("PostToolUse", block, output)
        return output

    def apply_plan_response(self, name: str, msg: dict) -> tuple:
        """11 条件合取核销批复，逐条都是"信与案卷互证"：
        ①from==lead ②to==name ③request_id==当前路由单（销单后重放必挂）④案卷存在
        ⑤type==plan_approval ⑥sender==name ⑦target==lead ⑧work_version==当前
        ⑨task_id==当前 ⑩status 已终态（早前由 match_response 裁决写入——
        "裁决/执行"分离的接缝）⑪metadata.approve 与 status 互证（缺省 False，fail-closed）。
        通过后 gate 取值以案卷 status 为准而非信件的 approve 字段——台账是权威、
        信件是证人，幻觉信/错位信翻不动闸门；任一不符软忽略 [Ignored plan response]。
        """
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id", "")
        work_version, task_id = self.current_work_identity(name)
        with self.teamLock:
            state = self.pendingRequests.get(request_id)
            expected_id = self.planRequestIds.get(name)
            valid = (
                msg.get("from") == "lead"
                and msg.get("to") == name
                and request_id == expected_id
                and state is not None
                and state.type == "plan_approval"
                and state.sender == name
                and state.target == "lead"
                and state.work_version == work_version
                and state.task_id == task_id
                and state.status in {"approved", "rejected"}
                and metadata.get("approve", False)
                == (state.status == "approved")
            )
            if not valid:
                return False, "[Ignored plan response: request mismatch]"
            self.planGates[name] = state.status
            self.activeTeammates[name] = "working"
            self.planRequestIds.pop(name, None)
            outcome = state.status
        return True, f"[Plan {outcome}] {msg['content']}"

    def apply_shutdown_request(self, name: str, msg: dict) -> tuple:
        """shutdown 是 plan_approval 的镜像协议：计划="队友提案 Lead 裁决"，关机=
        "Lead 提案 队友同意"；队友给自己案卷写 approved 不算越权——同意解散不增益。
        八道校验（from/to/命中/type/sender==lead/target==self/status==pending/
        未在 stopping）后返回 request_id，队友据此回执 shutdown_response，
        Lead 侧 consume_lead_inbox 自动核销。动机=协作式关机：线程不能在模型
        调用/工具执行中途安全强杀，只能通知+自愿在安全点退出。
        """
        request_id = msg.get("metadata", {}).get("request_id", "")
        with self.teamLock:
            state = self.pendingRequests.get(request_id)
            valid = (
                msg.get("from") == "lead"
                and msg.get("to") == name
                and state is not None
                and state.type == "shutdown"
                and state.sender == "lead"
                and state.target == name
                and state.status == "pending"
                and self.activeTeammates.get(name) != "stopping"
            )
            if not valid:
                return False, "[Ignored shutdown request: request mismatch]"
            self.activeTeammates[name] = "stopping"
        return True, request_id

    def _teammate_send_message(self, name: str, to: str, content: str) -> str:
        """收件人白名单='lead'（特判，它不在 roster 里）或 activeTeammates 在册——
        roster 就是通讯录：错名、已解散的幽灵、保留键 'agent' 全部拒收。"""
        with self.teamLock:
            if to != "lead" and to not in self.activeTeammates:
                return f"Agent '{to}' is not active"
        self.bus.send(name, to, content)
        return f"Sent to {to}"

    # ===== 空闲任务发现（调度双车道之 pull 道）=====

    def claim_next_task(self, owner: str):
        """Lead 显式派活是 push，队友空闲自取是 pull，汇入同一个 claim 漏斗。

        第一步自闸：名下已有租约/in_progress 直接拒——"先干完手里的活"，提前复刻
        claim_task 关卡省得白跑（_owner_in_progress 是对 Lane A 内部助手的刻意复用，
        与 task_store_lock 同线程重入安全）；第二步逐个试候选调真正的 claim_task，
        首个成功即返回。本函数自己从不写盘：所有认领必须走单锁路径，
        多人抢同一任务由 task_store_lock 串行化裁决。
        """
        with self.taskManager.task_store_lock():
            if (self.taskManager.assignments.get(owner)
                    or self.taskManager._owner_in_progress(owner)):
                return None
        for task in self.taskManager.scan_unclaimed_tasks():
            result = self.taskManager.claim_task(task.id, owner=owner)
            if result.startswith("Claimed "):
                return self.taskManager.load_task(task.id)
        return None

    # ===== 队友入职办证处 =====

    def spawn_teammate(self, name: str, role: str, prompt: str,
                       task_id: str | None = None,
                       require_plan: bool = False) -> str:
        """先认领初始 Task（如有），再点火一个常驻队友线程。

        三道名字关：① 合法性复用 message_bus 的邮箱正则——队友名就是邮箱文件名；
        ② lead/agent 保留名拒收，否则出现影子收件箱或抢走 Lead 账本键 'agent' 的幽灵队友；
        ③ casefold 查重放 team_lock 里做，"查+注册"是原子动作（Alice 与 alice 算同人），
        但路由和白名单用精确名当键。
        先注册后点火：planGates 在此发入职牌（required/not_required），
        assignmentVersions=0 是版本号的诞生地（s13 同款：写 task 域账本但不在 task 锁内，
        CPython 字典写原子、无嵌套锁，不构成锁序违例）。
        claim 失败回滚三本注册，顺序恰是注册的镜像——入职 all-or-nothing。
        startswith('Claimed ') 用文案前缀判成败：脆弱（措辞一改即碎），
        是"工具返回字符串"铁律被机器读者消费的代价，教学代码诚实保留这个折衷。
        """
        if not is_valid_agent_name(name):
            return ("Invalid teammate name: use 1-64 letters, digits, "
                    "underscores, or dashes")
        if name.lower() in RESERVED_TEAMMATE_NAMES:
            return f"Invalid teammate name: '{name}' is reserved by the runtime"
        with self.teamLock:
            if any(existing.casefold() == name.casefold()
                   for existing in self.activeTeammates):
                return f"Teammate '{name}' already exists"
            self.activeTeammates[name] = "working"
            self.planGates[name] = "required" if require_plan else "not_required"
            self.taskManager.assignment_versions[name] = 0

        if task_id:
            try:
                claimed = self.taskManager.claim_task(task_id, owner=name)
            except (FileNotFoundError, ValueError) as exc:
                claimed = f"Error: {exc}"
            if not claimed.startswith("Claimed "):
                with self.teamLock:
                    self.activeTeammates.pop(name, None)
                    self.planGates.pop(name, None)
                    self.taskManager.assignment_versions.pop(name, None)
                return f"Cannot spawn teammate '{name}': {claimed}"

        runtime = TeammateRuntime(self, name, role, prompt, task_id, require_plan)
        # daemon=True 是"永不强杀"的最后保险：协议劝退是主道，劝不动的进程退出时
        # 由解释器兜底带走。先入账再 start：秒死的线程也能在 finally 里从
        # teammateThreads 找到自己要 pop 的入口，台账不失联。
        thread = threading.Thread(target=runtime.run, daemon=True)
        with self.teamLock:
            self.teammateThreads[name] = thread
        thread.start()
        log_info("team", f"{name} spawned as {role}")
        assigned = f" for {task_id}" if task_id else " without an initial Task"
        # "End this turn; the runtime will deliver its events."：异步协议已发车，
        # 事件会由 Lead 主循环在回合边界送上门，赖在本回合"等队友"只会幻觉进度。
        return (
            f"Teammate '{name}' spawned as {role}{assigned}. "
            "End this turn; the runtime will deliver its events."
        )

    # ===== Lead 团队工具（控制面板薄壳）=====
    # 七个 run_xxx 都不带逻辑，只把模型意图翻译进上面的核心函数，
    # 返回值永远是写给模型看的字符串。

    def run_spawn_teammate(self, name: str, role: str, prompt: str,
                           task_id: str | None = None,
                           require_plan: bool = False) -> str:
        """纯转发，全模块最薄的一层壳：判断全在 spawn_teammate。"""
        return self.spawn_teammate(name, role, prompt, task_id, require_plan)

    def run_list_teammates(self) -> str:
        """只读仪表盘：就报 name: status 一列，队友在干什么去任务板看。"""
        with self.teamLock:
            if not self.activeTeammates:
                return "No active teammates."
            return "\n".join(
                f"{name}: {status}"
                for name, status in sorted(self.activeTeammates.items())
            )

    def run_send_message(self, to: str, content: str) -> str:
        """type='message' 是自由文本通道——协议信之外的捷径：不记案、无人解析。
        to 查名册白名单，防寄死信箱。"""
        if to not in self.activeTeammates:
            return f"Teammate '{to}' is not active"
        self.bus.send("lead", to, content)
        return f"Sent to {to}"

    def run_request_shutdown(self, teammate: str) -> str:
        """Lead 只能"提案"：建 shutdown 案（sender=lead、status=pending）再发信，
        去留由队友在 handle_inbox 协作式执行——镜像协议的工具面，强杀在这里不存在。"""
        if teammate not in self.activeTeammates:
            return f"Teammate '{teammate}' is not active"
        with self.teamLock:
            request_id = self.new_request_id()
            self.pendingRequests[request_id] = ProtocolState(
                request_id=request_id,
                type="shutdown",
                sender="lead",
                target=teammate,
                status="pending",
                payload="",
            )
        self.bus.send("lead", teammate, "Finish the current step and shut down.",
                      "shutdown_request", {"request_id": request_id})
        return f"Shutdown requested from {teammate} ({request_id})"

    def run_request_plan(self, teammate: str, task: str) -> str:
        """中途上闸：planGates=required 与 spawn 入职发牌互补（一个管出生、一个管事后
        加严），顺手发封 plan_request 催信——光上闸不催，队友要 until 下一堵墙才发现被拦。"""
        if teammate not in self.activeTeammates:
            return f"Teammate '{teammate}' is not active"
        with self.teamLock:
            self.planGates[teammate] = "required"
        self.bus.send("lead", teammate, task, "plan_request")
        return f"Plan requested from {teammate}"

    def run_review_plan(self, request_id: str, approve: bool,
                        feedback: str = "") -> str:
        """查卷再盖章：锁外先 get 只为快速失败，进锁后重新取卷再审——防两次读之间
        案卷被换的 TOCTOU。四道关：是不是 plan、还 pending 吗、快照对得上吗、
        还是 current plan 吗。这里只写案卷终态，不动 planGates——开闸动作在队友侧，
        由 apply_plan_response 的 11 条件审档兑现：裁决与执行分离。
        身份在 Lead 端比一次、队友端再比一次：刻意冗余——信路上跑的是 LLM，会幻觉会寄错。"""
        state = self.pendingRequests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        work_version, task_id = self.current_work_identity(state.sender)
        with self.teamLock:
            state = self.pendingRequests.get(request_id)
            if not state:
                return f"Request {request_id} not found"
            if state.type != "plan_approval":
                return f"Request {request_id} is not a plan"
            if state.status != "pending":
                return f"Request {request_id} already {state.status}"
            if (state.work_version != work_version or state.task_id != task_id):
                return f"Request {request_id} belongs to an earlier assignment"
            if self.planRequestIds.get(state.sender) != request_id:
                return f"Request {request_id} is not the current plan"
            state.status = "approved" if approve else "rejected"
        content = feedback or ("Plan approved." if approve
                               else "Revise the plan and submit it again.")
        self.bus.send("lead", state.sender, content, "plan_approval_response",
                      {"request_id": request_id, "approve": approve})
        return f"Plan {state.status} ({request_id})"

    def run_create_worktree(self, name: str, task_id: str) -> str:
        """直接包 WorktreeManager。注意工具清单里刻意没有 remove_worktree：
        销毁 worktree（可能有未提交心血）的权力留给宿主/用户——"删除归人"。"""
        return self.worktreeManager.create_worktree(name, task_id)

    # ===== 任务板格式化（与 lcc tools_manager.run_list_tasks 同款文案）=====

    def run_list_tasks(self) -> str:
        """list_tasks 的团队成员版：只读渲染任务板。"""
        tasks = self.taskManager.list_tasks()
        if not tasks:
            return "No tasks. Use create_task to add some."
        lines = []
        for task in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]",
                      "completed": "[x]"}.get(task.status, "[?]")
            owner = f" [{task.owner}]" if task.owner else ""
            dependencies = ""
            if task.blockedBy:
                dependencies = f" (blockedBy: {', '.join(task.blockedBy)})"
            lines.append(
                f"{marker} {task.id}: {task.subject} [{task.status}]"
                f"{owner}{dependencies}")
        return "\n".join(lines)


# ===== TeammateRuntime：队友常驻线程（一人一线程一对话史，睡邮箱、合作式退场）=====

class TeammateRuntime:
    """One persistent teammate with separate messages and WORK/IDLE phases."""

    def __init__(self, manager: AgentTeamsManager, name: str, role: str,
                 prompt: str, task_id: str | None, require_plan: bool):
        self.manager = manager
        self.name = name
        # 系统提示词是"岗前培训"：[Assigned task] 已认领别再 claim；要计划的先
        # submit_plan 等批复；收工文本 runtime 自动转告 Lead（模型不必自己发 result）；
        # 协调者叫 'lead'——就是收件白名单里那个拼写。
        self.system = (
            f"You are '{name}', a {role}. Use tools to complete the assigned "
            "Task, then call complete_task and report a concise result. "
            "If the first user message contains [Assigned task], that Task is "
            "already claimed; do not call claim_task for it again. "
            "When asked for a plan, call submit_plan and wait for approval "
            "before bash or file changes. File and shell tools use the Task's "
            "working directory; that directory is not a sandbox. The runtime "
            "delivers your final text to Lead. Use send_message only for "
            "intermediate coordination, and address the coordinator as 'lead'."
        )
        # 首条 user 消息三层拼装：Lead 给的 prompt 打底 + 可选 [Assigned task] 任务卡
        # + 可选 [Plan required] 横幅。任务卡用 load_task + assignment_cwd 把"你在哪个
        # 目录干活"诚实披露——模型没有别的渠道知道自己被隔离进了 worktree。
        self.messages = [{"role": "user", "content": prompt}]
        if task_id:
            task = manager.taskManager.load_task(task_id)
            cwd = manager.worktreeManager.assignment_cwd(name)
            self.messages[0]["content"] += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {cwd}"
            )
        if require_plan:
            self.messages[0]["content"] += (
                "\n\n[Plan required] Submit a plan and wait for Lead approval "
                "before changing files or using bash."
            )
        # handlers 是这个队友的私有工具表：send_message/submit_plan 用 lambda 闭包把
        # name 焊死进工具——模型没法伪造发件人；claim/complete 传 owner=self.name。
        # 对比 Lead 侧适配器硬编码 'agent'：同一套核心，两种身份立场。
        self.handlers = {
            BASH: lambda **params: self._run_base(BASH, params),
            READ_FILE: lambda **params: self._run_base(READ_FILE, params),
            WRITE_FILE: lambda **params: self._run_base(WRITE_FILE, params),
            EDIT_FILE: lambda **params: self._run_base(EDIT_FILE, params),
            GLOB: lambda **params: self._run_base(GLOB, params),
            SEND_MESSAGE: lambda to, content: manager._teammate_send_message(
                name, to, content),
            SUBMIT_PLAN: lambda plan: manager._teammate_submit_plan(name, plan),
            LIST_TASKS: manager.run_list_tasks,
            CLAIM_TASK: self.claim,
            COMPLETE_TASK: self.complete,
        }

    # 与 Lead 侧 _agent_cwd 同款 Go 风格 (值, 错误) 元组，只是 owner 换成 self.name。
    # 没领任务连读都不许——错误文案直接教模型先去 claim。
    def current_cwd(self) -> tuple:
        if self.name not in self.manager.taskManager.assignments:
            return None, "Error: Claim a Task before using workspace tools."
        try:
            return self.manager.worktreeManager.assignment_cwd(self.name), None
        except (FileNotFoundError, ValueError) as exc:
            return None, f"Error: Invalid task assignment: {exc}"

    # 空间工具统一走注入适配器：current_cwd 报错就短路返回，否则把验租后的 cwd
    # 塞给 tool_adapters（Lane D 绑定到带 cwd 形参的 base run_*）。工具本体零改动。
    def _run_base(self, tool: str, params: dict) -> str:
        cwd, error = self.current_cwd()
        if error:
            return error
        adapter = self.manager.toolAdapters.get(tool)
        if adapter is None:
            return f"Unknown tool: {tool}"
        return str(adapter(params, cwd=cwd))

    def claim(self, task_id: str) -> str:
        try:
            return self.manager.taskManager.claim_task(task_id, owner=self.name)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"

    def complete(self, task_id: str) -> str:
        try:
            return self.manager.taskManager.complete_task(task_id, owner=self.name)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"

    # 邮箱分拣机：按 type 路由，返回值 bool = "该不该停"（True=立即收线程）。
    # shutdown_request 获批：notice 变量换语义成 request_id，回执 shutdown_response 里
    # approve 写死 True（能走到这就是因为接受了），然后立刻 return True——攒下的
    # work_messages 整个丢弃、模型根本没看见关机信：合作式退场是 runtime 的事不是模型的事。
    # 被拒：拒绝理由喂给模型（有人想无权限指挥你，知道了继续干活）。
    # plan_approval_response：apply_plan_response 结账后把"裁决/忽略"文本注入成 user 消息。
    # 顺序陷阱：一批里合法 shutdown 夹在中间 → 它后面的信永久丢失（破坏性读取已
    # unlink），但 return 前已处理的批准已生效——顺序消费、边界在 return。
    # 多条拼成一条 user 消息：每次 API 调用只端一个上菜托盘。
    def handle_inbox(self, inbox: list) -> bool:
        """Append work messages and return True for a valid shutdown."""
        work_messages = []
        for msg in inbox:
            msg_type = msg.get("type", "message")
            if msg_type == "shutdown_request":
                accepted, notice = self.manager.apply_shutdown_request(self.name, msg)
                if not accepted:
                    work_messages.append(notice)
                    continue
                self.manager.bus.send(self.name, "lead", "Shutdown acknowledged.",
                                      "shutdown_response",
                                      {"request_id": notice, "approve": True})
                return True
            if msg_type == "plan_approval_response":
                _, notice = self.manager.apply_plan_response(self.name, msg)
                work_messages.append(notice)
                continue
            if msg_type == "plan_request":
                work_messages.append(f"[Plan required] {msg['content']}")
                continue
            work_messages.append(
                f"[Message from {msg['from']}] {msg['content']}"
            )
        if work_messages:
            self.messages.append({"role": "user",
                                  "content": "\n".join(work_messages)})
        return False

    # work() = 一次模型回合，返回 continue|idle|stop，永不阻塞。第一步永远是 drain 邮箱：
    # shutdown 能在任何 API 调用前截住。API 异常 → error 信给 Lead + stop——队友死了
    # 不能无声无息。tool_calls 分支逐块过 _run_teammate_tool（闸门）后 return continue，
    # 循环所有权归 run()：队友必须在两次状态之间可中断地睡在邮箱上，
    # 所以拆成"单步 + 状态机"。
    # 纯文本分支三岔，眼色全看 gate 是否 pending：
    #   非 pending → result 信给 Lead（兑现提示词的"收工我转告"）；
    #   pending → 不发 result 也不退租：这不是完工报告（Lead 手里已有
    #     plan_approval_request，假 result 污染事件流）；任务还 in_progress，
    #     租约必须活着，批准回来还在同一 worktree 续命；
    #   其余 → release_completed_assignment（刻意等到"回合边界"才释放的正是这里）+ idle。
    # "完工"与"可用"是两个正交事件：Lead 会看到 [result] + [idle_notification] 各一行。
    def work(self) -> str:
        """Run one model turn. Return continue, idle, or stop."""
        if self.handle_inbox(self.manager.bus.read(self.name)):
            return "stop"
        with self.manager.teamLock:
            self.manager.activeTeammates[self.name] = "working"
        try:
            response = self.manager.client.messages.create(
                model=self.manager.model,
                system=self.system,
                messages=self.messages,
                tools=AgentTeamsManager.TEAMMATE_TOOLS,
                max_tokens=8000,
            )
        except Exception as exc:
            self.manager.bus.send(self.name, "lead",
                                  f"{type(exc).__name__}: {exc}", "error")
            return "stop"

        self.messages.append({"role": "assistant", "content": response.content})
        tool_calls = [
            block for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]
        if tool_calls:
            results = []
            for block in tool_calls:
                output = self.manager._run_teammate_tool(
                    self.name, block, self.handlers)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
            self.messages.append({"role": "user", "content": results})
            return "continue"

        summary = _last_assistant_text(response.content)
        with self.manager.teamLock:
            gate = self.manager.planGates.get(self.name, "not_required")
        if gate != "pending" and summary:
            self.manager.bus.send(self.name, "lead", summary, "result")
        if gate == "pending":
            with self.manager.teamLock:
                self.manager.activeTeammates[self.name] = "waiting_approval"
        else:
            self.manager.taskManager.release_completed_assignment(self.name)
            with self.manager.teamLock:
                self.manager.activeTeammates[self.name] = "idle"
            self.manager.bus.send(self.name, "lead", "Waiting for more work.",
                                  "idle_notification")
        return "idle"

    # 慢车道：bool 返回（True=起床干活，False=该死了）。2 秒心跳管"看邮箱"和"扫任务板"
    # 两张嘴，死亡信优先于干活信。空手 → claim_next_task（pull 车道），领到后 append 的
    # 任务卡与 __init__ 首条消息同款格式——模型分不清"这次是新派的还是自己捡的"。
    # waiting_approval 的队友心跳照打，但 claim_next_task 的自闸（名下已有租约）挡住
    # 自取——"心跳不停、手不发痒"，纯靠租约实现，不用特判状态。
    def wait_for_work(self) -> bool:
        """Wait for a message or atomically claim the next ready Task."""
        while True:
            inbox = self.manager.bus.wait_for_messages(
                self.name, AgentTeamsManager.IDLE_SCAN_INTERVAL)
            if inbox:
                before = len(self.messages)
                if self.handle_inbox(inbox):
                    return False
                if len(self.messages) > before:
                    return True
                continue

            task = self.manager.claim_next_task(self.name)
            if not task:
                continue
            cwd = self.manager.worktreeManager.assignment_cwd(self.name)
            self.messages.append({
                "role": "user",
                "content": (
                    f"[Auto-claimed task {task.id}] {task.subject}\n"
                    f"{task.description}\nWork directory: {cwd}"
                ),
            })
            log_info("team", f"idle: {self.name} claimed {task.id}: {task.subject}")
            return True

    # 线程本体：非 idle 直接 work（快车道短循环），idle 才去 wait_for_work 睡慢车道。
    # 外层 except 是 work/wait 没局部化到的漏网异常兜底网——线程不能对着 stderr
    # 尖叫而死，必须给 Lead 发 error 信。finally 三件套 = 队友的后事：
    # release_teammate_assignment（清算：撂下的 in_progress 打回 pending，让别的
    # idle 队友捡）+ team_lock 下清四本账（名册、闸门、路由单、线程台账同时遗忘
    # 死者；漏网 pending_requests 靠 match_response 软校验自然过期）+ 报给人。
    # 全程没有任何 force-kill——shutdown 只"劝退"，每个出口都是线程自愿到达的安全点。
    # "租约非所有权"的三时点闭环在此凑齐：出生(claim)/回合边界(release_completed)/死亡(finally)。
    def run(self):
        try:
            state = "continue"
            while state != "stop":
                if state == "idle" and not self.wait_for_work():
                    break
                state = self.work()
        except Exception as exc:
            try:
                self.manager.bus.send(self.name, "lead",
                                      f"{type(exc).__name__}: {exc}", "error")
            except Exception:
                pass
        finally:
            try:
                self.manager.taskManager.release_teammate_assignment(self.name)
            except Exception as exc:
                try:
                    self.manager.bus.send(
                        self.name, "lead",
                        f"Assignment cleanup failed: {type(exc).__name__}: {exc}",
                        "error",
                    )
                except Exception:
                    pass
            with self.manager.teamLock:
                self.manager.activeTeammates.pop(self.name, None)
                self.manager.planGates.pop(self.name, None)
                self.manager.planRequestIds.pop(self.name, None)
                self.manager.teammateThreads.pop(self.name, None)
            log_info("team", f"{self.name} finished")

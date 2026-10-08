# Agent Teams 技术文档

> 对应源码：`message_bus.py`（本仓库当前版本 106 行）、`worktree_manager.py`（385 行）、
> `agent_teams_manager.py`（1063 行）
> 状态：完整，**已接入主循环**（组装根 `tools_manager.py:264-288`；Lead 七件团队工具的
> schema 与 handler 路由于 `tools_manager.py:331-332` 并入主工具表）
> 引入：对 learn-claude-code `s13_agent_teams` 的全量对等移植；本文写作时这批改动仍在
> 未提交工作区（基线 HEAD `4274d79`），故无引入提交号

## 1. 它解决什么问题

单个 `agent_loop` 只能串行地想一步做一步。agent teams 把"一个会话"扩成"一个团队"，
且**全程留在同一个 Python 进程内**——没有 socket、没有子进程、没有消息中间件：

1. **Lead + 常驻队友**：Lead 仍是主循环里那个 `"agent"`；teammate 是 `threading.Thread`
   （`daemon=True`，`agent_teams_manager.py:673` 创建），每个队友持有**自己的对话历史**
   和自己的慢循环，被 spawn 之后长期活着，不是一次性调用。
2. **共享任务板**：`task_manager.py` 的落盘任务看板成为团队唯一的事实来源——
   Lead 建图、队友认领、完成后解锁下游。
3. **文件邮箱总线**：`message_bus.py` 用 `.lcc/mailboxes/<name>.jsonl` 做单向追加 +
   破坏性读取，是 Lead 与队友之间**唯一**的通信通道。
4. **git worktree 隔离**：`worktree_manager.py` 让一个任务绑定一个独立工作目录，
   两个队友改同一个文件不再互相覆盖（注意：**这不是安全沙箱**，见 §11.6）。
5. **计划审批与关停协议**：`request_plan` / `submit_plan` / `review_plan` 与
   `request_shutdown` 走同一套"案卷台账"（`ProtocolState`），带工作身份快照防 TOCTOU。

对照 s13 的移植取向：**协议与状态机逐段对等**，凡 lcc 已有的基础设施（Env 路径、
log 分级、TaskManager、权限/Hook 总线）一律复用 lcc 形态而不是照抄 s13 的单文件写法，
差异清单见 §12。

## 2. 架构图

```
                         人类（控制台是 Lead 的私有资产）
                                    ^  |
                          input()   |  v  模型输出 / 审批询问
                     +--------------+--+--------------+
                     |        loop.py  Lead 主循环     |
                     |  agent_loop / _agent_loop_inner |
                     |  wait_for_cli_event -> "wake"   |
                     |  consume_lead_inbox + inject    |
                     +---+------------------------+----+
             工具调用 |                        | 团队事件注入
        TEAM_TOOLS 七件 |                        v
                     +--v-------------------------------------+
                     |     AgentTeamsManager (Lane C)         |
                     |  台账: activeTeammates / planGates /    |
                     |        planRequestIds / pendingRequests |
                     |        / teammateThreads  (teamLock)    |
                     +---+----------------+---------------+----+
        claim/complete |  |  bus.send      |  | 回调注入    |  |
                     +--v--+            +--v---+        +--v-----+
                     | TaskManager |    | MessageBus |   |Worktree|
                     | .lcc/task   |    | .lcc/mailboxes |  |Manager|
                     | + 租约台账   |    | {name}.jsonl  |   +---+---+
                     | assignments |    +------^-------+       |
                     +------+------+           |               v
                            \                  |         .lcc/worktrees/
                             \                 |          ├── fix-login/   (wt/fix-login)
                              v                |          └── add-cache/   (wt/add-cache)
                    +---------- 共享同一块盘 ----------+
                    |                                  |
     spawn 时创建   |  TeammateRuntime × N（daemon 线程）|
     ───────────────+----------------------------------+
     线程 a: work() <-> client.messages.create(TEAMMATE_TOOLS)
             工具闸门 _run_teammate_tool -> 租约 cwd 落文件
     线程 b: wait_for_work() -> 看邮箱 / 扫任务板（一个心跳 2s）
     收信: handle_inbox()  -> shutdown / plan 回复 / 普通消息
     退出: run().finally    -> 释放租约 + 摘台账
```

三条硬约束在图上体现为：

- **所有跨线程通信只走 `MessageBus`**（`bus.send` 永远在锁外发）。
- **所有可变共享状态只由两个 Manager 持有**，队友线程不碰全局。
- **队友永不触碰控制台**：输入输出、权限审批、`ask_user` 全部是 Lead 的资产。

## 3. 存储布局（Env 集中路径）

```
<workDir>/.lcc/              ← .gitignore:6  /.lcc
├── task/                    ← env.py:21  taskDirPath
│   ├── .lock                ← task_manager.py:41 LOCK_FILE_NAME（跨进程文件锁，
│   │                          隐藏文件名，glob("task_*.json") 天然忽略）
│   └── task_5dea2769.json   ← 一任务一文件；新增字段 worktree（task_manager.py:34）
├── mailboxes/               ← env.py:23  mailboxesDirPath
│   ├── lead.jsonl           ← 一个 agent 一个文件，name 即身份
│   └── fixer.jsonl
├── worktrees/               ← env.py:25  worktreesDirPath
│   └── fixer/               ← 分支 wt/fixer（worktree_manager.py:68）
└── temp/                    ← env.py:26  tempDirPath
```

- 三个目录都由使用方**按需 mkdir**（`message_bus.py:72` 每次 send 前 `mkdir(parents=True,
  exist_ok=True)`；`worktree_manager.py:273` 创建时建目录），启动时无副作用。
- s13 用仓库根的 `.tasks/` / `.mailboxes/` / `.worktrees/`；lcc 全部收进 `.lcc/` 由 Env
  单点提供，见 §12.1。

## 4. MessageBus（`message_bus.py`）

### 4.1 接口一览

| 接口 | 位置 | 语义 |
|---|---|---|
| `VALID_AGENT_NAME` | :14 | `^[A-Za-z0-9_-]{1,64}$`，邮箱文件名的唯一合法形态 |
| `RESERVED_TEAMMATE_NAMES` | :17 | `{"lead", "agent"}`，比较时 `casefold`，队友不得占用 |
| `is_valid_agent_name(name)` | :20 | 纯谓词，不抛异常 |
| `MessageBus(mailbox_dir, workspace_root)` | :27 | **无模块级单例**；由 `tools_manager.py:265` 构造后注入 |
| `_path(name)` | :38 | fail-closed 三重校验，见 §11.1 |
| `send(frm, to, content, type="message", metadata=None)` | :66 | 追加一行 JSONL |
| `read(name)` | :79 | 破坏性读取（读完即删文件） |
| `peek(name)` | :85 | 非破坏性探测：存在**且** `st_size > 0` |
| `wait_for_messages(name, timeout=None)` | :96 | 阻塞到 `peek` 为真或超时，超时返回 `[]` |

内部两把东西：`self._lock = threading.RLock()`（:31）与挂在它上面的
`self._changed = threading.Condition(self._lock)`（:34）。写与通知在同一个 Condition 里
完成（:71-75），因此"写成功但没唤醒"这种丢事件的情况不存在。

### 4.2 信封格式

`send` 落盘的是 6 字段一行 JSON（`message_bus.py:66-76`）：

```json
{"from": "lead", "to": "fixer", "content": "...", "type": "shutdown_request",
 "ts": "2026-10-08 15:22:04", "metadata": {"request_id": "req_000003"}}
```

- `ensure_ascii=True`，Windows 控制台/编辑器读它不会踩编码坑。
- `type` 是协议的分派键：`message` / `plan_approval_request` /
  `plan_approval_response` / `shutdown_request` / `shutdown_response` /
  `plan_request` / `result` / `idle_notification`（后者由 `agent_teams_manager.py:491`、
  `:730`、`:740`、`:769`、`:912`、`:981`、`:990` 发出）。
- 日志一律 `log_info("bus", "<from> -> <to>: (<type>) <前 50 字>")`。

### 4.3 at-most-once 语义

`_read_unlocked`（:52）读完后 `inbox.unlink()`：**没有 ack、没有重投**。
进程在"读完但没处理完"之间崩掉，这批消息就永久丢失。这是 s13 的原始取舍，
lcc 原样保留并在 §14.2 记为已知风险。

`peek` 额外要求 `st_size > 0` 是为防一种具体故障：写一半崩溃留下的 **0 字节文件**
会让 `peek` 恒真、`read` 恒返回空列表，Lead 因此陷入 wake → 无事 → wake 的空转。

### 4.4 `notify_all` 而非 `notify`

一个 Condition 服务所有邮箱。被唤醒的可能是别的邮箱的等待者，唤醒后各自重新 `peek`
自己的邮箱，不属于自己就继续等（超时驱动）。虚假唤醒无害，漏唤醒才致命。

## 5. WorktreeManager（`worktree_manager.py`）

### 5.1 注册表是唯一事实源

`_parse_registry()`（:101）跑 `git worktree list --porcelain` 并解析成
`{Path: {...}}`；`registered_worktrees()`（:123）在此基础上给出对外视图
`name -> path`，过滤条件三样：必须落在 `.lcc/worktrees` 下且不是根、目录必须真实存在、
`branch` 字段必须等于 `refs/heads/wt/{name}`。注册表读不出来时返回 `{}`——
**宁可不给路径，不给可疑路径**（fail-closed）。

磁盘上有目录但注册表里没有 → 视为不存在；注册表里有但目录没了 → 同样视为不存在。
`task_worktree_cwd(task)`（:169）与 `is_valid_worktree(name)`（:176）就挂在这两个视图上，
分别作为 TaskManager 的 `worktree_cwd_resolver` / `worktree_validator` 回调
（构造时 `worktree_manager.py:40-41` 自动接线）。

### 5.2 热路径：`assignment_cwd(owner)`（:190）

每次队友调 bash/read/write/edit/glob 都会走一遍（`agent_teams_manager.py:857`
`current_cwd` → `tools_manager.py:272` 的 adapters）。它只在 `task_store_lock()` 内做
内存与磁盘的交叉核对：

| 情形 | 结果 |
|---|---|
| 无租约且 `owner == "agent"` | 返回 `str(env.workDirPath)`（Lead 未认领任务时的常态） |
| 无租约且 owner 是队友 | 抛 `ValueError`（"先 claim 再用文件工具"） |
| 有租约，任务 `in_progress`/`completed` 且 owner 匹配 | 返回重算出的 cwd |
| 有租约，但 worktree 绑定坏了 | 抛 `ValueError`（该任务路径已不可信） |
| 重算 cwd ≠ 台账记录 | **自愈回写** `taskManager.assignments`，返回新值（s13 在此抛异常） |

### 5.3 `create_worktree(name, task_id)`（:234）闸门序列

进入即 `task_store_lock()`，随后按序，任一失败即返回错误字符串（不留半成品）：

①任务存在 → ②任务 `pending` 且 `owner` 为空 → ③任务未绑定别的 worktree →
④该 name 未被别的任务占用 → ⑤目标路径不存在 → ⑥`git rev-parse --show-toplevel`
必须等于 `env.workDirPath`（不在 git 工作区里就免谈）→ ⑦`check-ref-format --branch` →
⑧`wt/{name}` 分支不存在 → ⑨注册表可读 → ⑩路径不在注册表里。

全部通过才 `mkdir` + `git worktree add -b wt/{name} <path> HEAD`。git 失败时**绝不自动清理**，
而是重查注册表/分支/目录后返回 `Partial operation` 长文本，把残留物列清楚交人处置；
`task.worktree = name` 落盘失败时返回 `Partial success` 并保留已建好的 Git 数据。
这种"半成品如实报告"的取向贯穿整个模块。

### 5.4 `remove_worktree(name, discard_changes=False)`（:323）：删除归人

这是**唯一会销毁磁盘内容**的操作，因此**刻意不注册为 LLM 工具**——
`TEAM_TOOLS`（:217-295）里没有它，`teamToolHandlers`（:334-342）里也没有，
`run_create_worktree`（:773）旁边的注释把这件事说明文了。它只供人类/宿主在 Python 层调用。

同样在 `task_store_lock()` 内的五道闸：①注册表里有合法条目 ②确实被任务绑定 ③
**所有**绑定它的任务都 `completed` ④没有任何租约的 cwd 指向该路径（否则要求"等这一轮
工作结束"）⑤`git status --porcelain --ignored` 干净（命令失败也算不干净）。
`discard_changes=True` 只豁免第⑤道并给 git 加 `--force`，其余四道一律不免。
分支 `wt/{name}` 永远保留，删除后把 `task.worktree` 解绑为 `None`。

## 6. AgentTeamsManager（`agent_teams_manager.py`）

### 6.1 五本账（:320-325，由 `teamLock` 统管）

| 台账 | 类型 | 语义轴 |
|---|---|---|
| `activeTeammates` | `dict[str, str]` | 活跃度：`working` / `waiting_approval` / `idle` / `stopping` |
| `planGates` | `dict[str, str]` | 权限：`not_required` / `required` / `pending` / `approved` / `rejected` |
| `planRequestIds` | `dict[str, str]` | 路由：队友 → 当前在审案卷号 |
| `pendingRequests` | `dict[str, ProtocolState]` | 案卷柜：`request_id` → 状态；只放内存，协议天生短命 |
| `teammateThreads` | `dict[str, Thread]` | 名册：只记账，**从不 join、从不 kill** |

`ProtocolState`（:76）字段：`request_id / type / sender / target / status / payload /
work_version / task_id / created_at`。注意 `sender`/`target` 的方向在两类协议里是**镜像**的：
关停由 Lead 发起（`sender="lead"`），计划审批由队友发起（`sender=<name>`，`target="lead"`）——
`match_response` 因此比对的是"镜像身份"而非固定方向。

### 6.2 注入契约（文件头 docstring）

本模块不 import `tools_manager`（避免成环），五件套基础工具与两道治理层都以**回调**注入：

| 注入项 | 形状 | 组装处 |
|---|---|---|
| `tool_adapters` | `{BASH/READ_FILE/WRITE_FILE/EDIT_FILE/GLOB: (params: dict, cwd: str) -> str}` | `tools_manager.py:272` |
| `permission_check` | `(block, prompt_user=False) -> str \| None` | `tools_manager.py:286`（`self.hooks.permission.check_permission`） |
| `hooks_trigger` | `(event, *args, skip_permission=...)` | `tools_manager.py:287`（`self.hooks.trigger_hooks`） |
| `client` / `model` | Anthropic SDK 客户端 | `tools_manager.py:282-284` |

传 `None` 表示跳过该层。`permission_check` 恒以 `prompt_user=False` 调用
（`agent_teams_manager.py:515`），`hooks_trigger` 对 PreToolUse 传
`skip_permission=True`（`hooks.py:30`，只跳过权限钩子，其他钩子照常跑）。

### 6.3 锁序铁律

```
task_store_lock()  →  team_lock          （只能这个方向）
bus.send()         →  永远在锁外
```

TaskManager 的三个回调（`on_assignment_advanced` / `on_assignment_released` /
`plan_gate_check`）**都在持有 task 锁时被调用**，回调内部再取 `team_lock`，
正好符合这条序（接线见 `agent_teams_manager.py:329-331`，注释见 :327-328）。
`_teammate_submit_plan`（:462）也是"一次锁跨度 task → team，出锁再 `bus.send`"。

### 6.4 能力矩阵

`TEAM_TOOLS`（:217-295）是 Lead 专属七件；`TEAMMATE_TOOLS`（:189-212）是队友全集
（base 五件套 + `send_message` + `submit_plan` + `next(...)` 借用三件任务工具，
借用的字面量定义在 `_TEAMMATE_BASE_TOOLS` :100 与 `_BORROWED_TASK_TOOLS` :154）。

| 能力 | Lead（`agent`） | teammate | 依据 |
|---|---|---|---|
| `create_task` / `update_task` | ✅ | ❌ | 只有 Lead 能改依赖图（:187-188 注释） |
| `get_task` | ✅ | ❌ | 队友看板只有 `list_tasks` |
| `list_tasks` | ✅ | ✅ | `_BORROWED_TASK_TOOLS` :156 |
| `claim_task` | ✅ | ✅ | :161；队友侧 owner 由 runtime 绑定 |
| `complete_task` | ✅ | ✅ | :170 |
| `spawn_teammate` | ✅ | ❌ | :219 |
| `list_teammates` | ✅ | ❌ | :234 |
| `send_message` | ✅ | ✅ | :192 / :239（两份 schema，handler 不同） |
| `request_shutdown` | ✅ | ❌ | :248 |
| `request_plan` | ✅ | ❌ | :257 |
| `review_plan` | ✅ | ❌ | :266 |
| `create_worktree` | ✅ | ❌ | :279 |
| `remove_worktree` | ❌ | ❌ | **只由人类调用**，见 §5.4 |
| `bash` / `read_file` / `write_file` / `edit_file` / `glob` | ✅ | ✅ | 队友版无 `run_in_background`（:106） |
| `run_in_background`（后台 bash） | ✅ | ❌ | 后台任务归 Lead 调度 |
| `task`（子代理）/ cron / 记忆 等 | ✅ | ❌ | 团队之外的 lcc 能力不向队友开放 |
| 控制台 `input()` / 权限弹窗 | ✅ | ❌ | `prompt_user=False`，见 §11.4 |

两个易被忽略的细节：

- **`send_message` 在两张表里是同名不同 schema**：Lead 版描述 "Message a teammate."，
  队友版描述 "Send an intermediate message to 'lead' or an active teammate."。
  `tools_manager.py:332` 的 `update` 使团队表的键**覆盖**主表同名键——`send_message`
  这个名字此前只存在于团队路由表里，主表没有它，覆盖不是问题而是唯一来源。
- **队友的 `name` 无法伪造**：`send_message` / `submit_plan` 的 handler 是**闭包绑定
  `self.name`** 的（`agent_teams_manager.py:841` 起），模型只能填 `to` 和 `content`，
  `from` 由 runtime 注入；`claim`/`complete` 同理把 `owner=self.name` 写死（:876/:884）。

## 7. TeammateRuntime（:801）—— 一线程一人生

### 7.1 出生（`__init__` :804）

系统提示是一份"岗前培训"：看到 `[Assigned task]` 就别再抢活；`submit_plan` 之后停下等
回复；"文件与 shell 工具用任务的工作目录，**那个目录不是沙箱**"；最终文本由 runtime 自动
投递给 Lead，不必自己 `send_message` 复述；协调者称呼固定为 `lead`。

首条 user 消息 = spawn 时的 `prompt` + 可选任务卡片（`[Assigned task {id}]` subject /
description / `Work directory: {cwd}`）+ 可选 `[Plan required]` 横幅。

### 7.2 两条车道

```
run()（:1033）状态机
  ├─ idle → wait_for_work()（:998）
  │           bus.wait_for_messages(name, IDLE_SCAN_INTERVAL=2.0)
  │           ├─ 有信 → handle_inbox()：False 则线程结束；有消息则回 work
  │           └─ 无信 → claim_next_task(owner)（:607）
  │                       成功 → 追加 [Auto-claimed task ...] 同款卡片
  │                              log_info("team", "idle: {name} claimed ...")
  └─ 非 idle → work()（:942）→ continue | idle | stop
```

一个 2 秒心跳同时喂两张嘴（看邮箱 + 扫任务板），常数是 `IDLE_SCAN_INTERVAL`（:185）。

`work()` 的四种收场：

| 分支 | 动作 | 状态 |
|---|---|---|
| 有 `tool_use` | 逐个过 `_run_teammate_tool`（:495），拼 `tool_result` 后 `continue` | `working` |
| 出文本且 `planGates != pending` | `bus.send(name, "lead", summary, "result")`（:981） | → `idle`，再发 `idle_notification`（:990） |
| 出文本且 gate == `pending` | **不发 result**，保留租约不动 | `waiting_approval` |
| API 异常 | 给 Lead 发错误消息后 `stop` | 走 finally |

"完工"（`result`）与"可用"（`idle_notification`）是两个正交事件，Lead 由此能区分
"这活做完了"和"这人闲着，可以派下一活"。

`handle_inbox`（:901）把一批邮件渲染进**同一条** user 消息：
`shutdown_request` → `apply_shutdown_request`（:570），接受即回
`shutdown_response{approve=True}` 并 `return True`（**排队中的工作消息直接丢弃，
模型不会看到"你要被关停了"这件事**，协作式退场）；`plan_approval_response` →
`apply_plan_response`（:533）后把裁决文本注入对话；`plan_request` → `[Plan required]`；
其余 → `[Message from {from}] ...`。

### 7.3 死亡（`run().finally`）

```
release_teammate_assignment(name)   # 磁盘降级 + 摘租约 + 版本号 bump + 回调
team_lock 内 pop: activeTeammates / planGates / planRequestIds / teammateThreads
log_info("team", "{name} finished")
```

清理失败会给 Lead 发一条错误消息（不静默）。全模块**没有任何强制 kill**：
线程之死全靠自愿退场，`teammateThreads` 只记账。

## 8. 协议时序

### 8.1 计划审批（含反 TOCTOU）

```
Lead                          AgentTeamsManager              teammate 线程
 │ request_plan(teammate,task)      │                            │
 ├────────────────────────────────►│ planGates[t]=required      │
 │                                 │ bus.send plan_request ────►│ 看到 [Plan required]
 │                                 │                            │ work(): bash 被闸门挡住
 │                                 │◄── submit_plan(plan) ──────┤   （read/glob 仍放行）
 │                                 │ ①一次锁跨度 task→team       │
 │                                 │ gate: required→pending      │
 │                                 │ 快照 work_version + task_id │
 │                                 │ pendingRequests[req]=State  │
 │                                 │ planRequestIds[t]=req       │
 │                                 │ activeTeammates[t]=          │
 │                                 │   waiting_approval          │
 │                                 │ 出锁 bus.send ─────────────►│ 停在这里等裁决
 │   [Team events]                 │   plan_approval_request     │   （不轮询、不退出）
 │   plan_approval_request         │◄── handle_inbox 收 ─────────┤
 │   request_id=req_000001         │                             │
 │ review_plan(req, approve=True,  │                             │
 │             feedback="...")     │                             │
 ├────────────────────────────────►│ 出锁快取 → teamLock 内重读   │
 │                                 │ 校验: type/pending/          │
 │                                 │   work_version/task_id 快照  │
 │                                 │   且仍是该队友当前 plan 路由  │
 │                                 │ 只写案卷 status（不写闸门）   │
 │                                 │ bus.send ──────────────────►│ apply_plan_response:
 │                                 │   plan_approval_response    │ 11 条与条件全过才生效
 │  Plan approved (req_000001)     │   metadata={request_id,     │ gate←案卷 status
 │◄── [Team events] 下一次唤醒 ─────│    approve, ...}            │ activeTeammates←working
 │                                 │                             │ 继续 work(): bash 放行
```

关键设计：

- **裁决与执行分离**：`review_plan`（:743）**只写案卷 `status`**，闸门由队友线程在
  `apply_plan_response`（:533）里自己按案卷改——Lead 不越过台账去动 `planGates`。
- **闸门值来自案卷而不是信纸**：`apply_plan_response` 把 gate 设为**台账里的 status**
  而非消息里的 `approve` 布尔，两者不一致时以台账为准（且这本身就是校验项之一）。
- **一人一桩在审案**：gate 已经是 `pending` 时再次 `submit_plan` 直接被拒
  （`A plan is already waiting for review.`）；`rejected` 允许改完再交。
- **工作身份快照防 TOCTOU**：`work_version`（`assignment_versions` 计数，
  `task_manager.py:54`/`:136`）+ `task_id` 一起冻结在案卷里。若 Lead 审批期间该队友
  换了任务（`advance_assignment_version` 触发 `_on_assignment_advanced` :356 重置闸门、
  摘掉路由），旧案卷的快照与当前身份不再匹配，`apply_plan_response` 的 11 条与条件里
  有两条当场失败，返回 `[Ignored plan response: request mismatch]`——
  **迟到批文不可能批准到新工作上**。
- **写操作才受闸门**：`_run_teammate_tool`（:495）只对 `bash` / `write_file` /
  `edit_file` 检查 plan 闸门，`read_file` / `glob` 恒放行——看代码不需要审批。
  受阻文本 `Blocked: plan status is {gate}...`。

### 8.2 关停

```
Lead                              AgentTeamsManager             teammate 线程
 │ request_shutdown(teammate)           │                           │
 ├─────────────────────────────────────►│ pendingRequests[req]=      │
 │                                      │  ProtocolState(type=        │
 │                                      │  shutdown, sender=lead,     │
 │                                      │  status=pending)            │
 │                                      │ bus.send ─────────────────►│ handle_inbox
 │                                      │   shutdown_request          │ apply_shutdown_request
 │                                      │   metadata.request_id       │ (8 项校验) → stopping
 │                                      │◄── bus.send ────────────────┤ shutdown_response
 │                                      │   metadata={approve: True}  │        approve=True
 │                                      │                             │ return True → run() 退出
 │                                      │                             │ finally:
 │                                      │                             │  release_teammate_assignment
 │ [Team events]                         │ consume_lead_inbox 自动核销 │  台账 pop ×4
 │◄─────────────────────────────────────┤  shutdown_response → 案卷    │  残留 in_progress 任务
 │  （shutdown_response 不作为伪 user     │  approved；若仍带 request_id  │  → 降级 pending/owner=None
 │    turn 呈现给模型，见 §9 说明）        │  仍会被消费，不重演路由       │  租约与版本号回收
```

- **不发 `kill`**：Lead 只能"请求"。若队友正在跑一个长 bash，它跑完这一步才会看到关停
  请求（`work()` 每轮开头 drain 邮箱，:942 起）。
- **`stopping` 是单向门**：`activeTeammates[name] = "stopping"` 后 `list_teammates`
  能看到，但协议不再给它派新案卷。
- **租约一定回收**：即便队友是异常崩掉（外层 `except` 给 Lead 发错误消息），
  `finally` 里的 `release_teammate_assignment`（`task_manager.py:419`）照样跑——
  磁盘上残留的 `in_progress` 被降级为 `pending` 且 `owner=None`，别人能重新认领。
  这是"任务卡死"的唯一解药。

## 9. 事件驱动唤醒（Lane D，`loop.py`）

Lead 不轮询邮箱。事件投递借用现有的 CLI 等待循环：

```
run()（:292）
 ├─ 无待处理输入 → wait_for_cli_event()（:258）
 │     ① messageBus.peek("lead")  → 命中即 return ("wake", None)   # :263 非破坏性
 │     ② 否则后台任务 / cron / input() 原有分支
 ├─ wake 分支（:308）：
 │     events = agentTeamsManager.consume_lead_inbox()   # 破坏性读取
  │     ├─ 空 → continue（防御：peek 与 read 之间的竞态，别把空气端上桌子）
  │     ├─ 协议响应类且在案卷柜有对应 request_id → 台账核销，**不返回给渲染**
  │     └─ 剩余事件 → format_team_events() → "[Team events]\n[type request_id=x] from: content"
  │        log_info("team", "投递 {n} 条团队事件，唤醒 Lead")
 │        inject_team_events(messages, text)（:130）→ _run_turn()
 └─ 每轮之后 check_team_offline_edge()（:145）：activeTeammates 由非空到空的边沿
       打一条 "所有队友已下线，如需继续协作可再次 spawn_teammate"
```

三个要点：

1. **peek 只当门铃**（`("wake", None)`，无 payload），真正的取信在 wake 分支里
   `consume_lead_inbox()` 完成——这样"渲染逻辑"与"核销逻辑"只有一处入口，
   不会出现 peek 到的内容和投喂给模型的内容分叉。
2. **`consume_lead_inbox`（:424）优先做协议核销**：`*_response` 且带 `request_id`
   的消息先过 `match_response`（:391）四道闸（案卷存在 / 期望类型匹配 / 镜像身份 /
   仍 pending），命中即写案卷终态并**吞掉**；`metadata.get("approve", False)`
   ——**缺省即 False**，没有 request_id 的响应被当作普通消息渲染，进不了台账。
   剩下"没人认领"的原始消息才返回给 `format_team_events`。
   所以：合法/非法的协议回执都以台账为准，模型看到的 `[Team events]` 是**工作消息**
   （队友的 `result` / `idle_notification` / 普通留言 / 需要它去 `review_plan` 的
   `plan_approval_request`），而不是它已经代为盖过章的回执。
3. **注入形态与后台任务一致**：`inject_team_events` 是
   `inject_background_results` 的同构实现——末尾还是 user 消息就合并文本块，否则新开
   一轮 user turn。团队事件因此不会伪装成"人类说的话"。

**中断收尾**（`loop.py:292` 起）：`run()` 捕获 `KeyboardInterrupt` 后，`finally` 里先
`cron.stop_runtime_threads()`，再对 `list(activeTeammates)` 的快照逐个
`run_request_shutdown(name)`——**只发请求，从不 join daemon 线程**，进程该退就退。
队友的磁盘侧清理因此不能指望 Ctrl+C：租约回收的兜底是下一条的 turn 边界。

## 10. 租约与一人一活

租约台账在 TaskManager：`assignments`（`task_manager.py:52`，owner → `{"task_id", "cwd"}`，
键 `"agent"` 归 Lead）与 `assignment_versions`（:54，工作身份计数）。

### 10.1 `claim_task` 六道闸（`task_manager.py:312`）

| 序 | 闸门 | 失败文本（节选） |
|---|---|---|
| ① | 状态必须 `pending` | 已 `in_progress`/`completed` 直接拒 |
| ② | `owner` 必须为空 | 已被别人认领 |
| ③ | 内存无在租约 | `Owner {owner} must finish the current work turn for {task_id} before claiming another task` |
| ④ | 磁盘无在 `in_progress`（`_owner_in_progress` :159） | `... must complete {id} before claiming another task` |
| ⑤ | 依赖已满足（`can_start`） | `Blocked by: [...]` |
| ⑥ | worktree 完好（`_task_cwd` :147 → validator + resolver） | 绑定不可信则拒 |

③④ 合起来就是"一人一活"：**你手里活没干完，第二件都领不到**，
无论那件活是内存里还没收尾（③）还是压根还挂在盘上（④）。
成功后：`owner`/`status` 落盘 + 登记租约 + `advance_assignment_version`（:136，
顺带触发 `on_assignment_advanced` 重置该队友的 plan 闸门）。返回文本
`Claimed {id} ({subject})`。

### 10.2 三个释放时点

```
出生 ── claim_task 登记 assignments + version bump
  │        （spawn_teammate 带 task_id 时走同一条路）
  │  完成 ── complete_task（:354）**刻意不释放租约**
  │        只把任务置 completed；cwd 仍锚在原 worktree，
  │        因为收尾这一轮里模型还可能 read/write/验证
  │  turn 边界 ── release_completed_assignment(owner)（:400，幂等 bool）
  │        Lead 侧：agent_loop 的 finally（loop.py:153-157）
  │        队友侧：work() 出文本转 idle 前（:942 起）
  │        → 摘租约 + bump + on_assignment_released（闸门→not_required）
  │  死亡 ── release_teammate_assignment(owner)（:419）
           线程 finally 兜底：残留 in_progress 降级 pending/None + 摘账 + bump + 回调
```

为什么 `complete_task` 不立刻释放？因为"任务完成"和"这一轮工作结束"不是同一时刻：
模型说完"我改完了"之后往往还要跑一次测试、看一眼 diff。若 complete 就松锚，
这些后续动作会漂回 `workDirPath` 去改主工作树。`completed` 被允许留在
`assignment_cwd` 的合法状态集里（§5.2）正是为这个窗口服务。

`scan_unclaimed_tasks`（:435）是**只读**候选筛选（pending + 无 owner + `can_start` +
worktree ok），配合 `claim_next_task`（`agent_teams_manager.py:607`）构成队友的
pull 车道：候选循环里逐个 `claim_task`，**pull 侧自己不写盘**，所有闸门仍由
`claim_task` 把关。

## 11. 关键安全设计

### 11.1 fail-closed 路径三重校验

`MessageBus._path`（`message_bus.py:38`）三查全过才给路径：
①`name` 匹配 `VALID_AGENT_NAME`；②`mailboxDir.resolve()` 必须
`is_relative_to(workspaceRoot.resolve())`；③拼出的 `<name>.jsonl` 再 `resolve()` 后
仍必须在根内。`WorktreeManager._worktree_path`（`worktree_manager.py:58`）同构三查，
外加一条更狠的：`path != root` —— 允许 `remove_worktree` 指向隔离区根本身，
等于给一次删除整个 `.lcc/worktrees` 的机会。`validate_worktree_name`（:48）在此之外
还单独否掉 `.` / `..` / 内嵌 `..`。

任何一环不满足都是**抛异常/返回错误**，绝不"降级到某个还算安全的默认值"。

### 11.2 身份不可伪造

- 队友的 `from` / `owner` 由 runtime 闭包注入，模型的 `input` 里没有这两个字段
  （§6.4）；`_teammate_send_message`（:596）还要求 `to` 必须命中白名单
  （`lead` 或 `activeTeammates` 的键）。
- 回执必须过"镜像身份"闸（`match_response` :391）：`from == state.target` 且
  `to == state.sender`，冒充 Lead 发裁决会被当场判为 mismatch。

### 11.3 缺省与一次性

- `approve` 一律 `metadata.get("approve", False)`：**没有这个字段就是不同意**。
- `match_response` 要求案卷仍是 `pending`——**同一 `request_id` 只能被核销一次**，
  重放的响应落到 `format_team_events` 里成为无害文本。
- `pendingRequests` 只在内存：进程重启，所有在途审批自动作废，
  而磁盘上的 `in_progress` 任务由 §10 的降级路径重新变回可认领。

### 11.4 teammate 永不占用控制台

`permission_check(block, False)`（`permission.py:71`）：规则命中时只
`log_error("permission", "Permission required: {reason}")` 并把该串作为工具结果返回，
**绝不 `input()` / `ask_user`**。控制台是 Lead 与人类的唯一通道；
队友线程弹菜单会把两个交互者混进同一个 stdin，这是结构性错误而非风格问题。

### 11.5 钩子链的可控穿透

`hooks.py:30` 的 `skip_permission=True` 只跳过 `permission_hook` 一道（用 `==` 比较，
因为 bound method 不是 `is` 同一对象），其余用户钩子照常执行。队友工具因此**不是**
治理盲区：跳过的是"会阻塞等人"的那一层，保留的是"会拦截"的那一层。

### 11.6 worktree 是隔离，不是沙箱

`prompt_teams`（`loop.py:60-70`）与队友系统提示都写死了这句话：
"File and shell tools use the Task's working directory; that directory is not a sandbox"。
`bash` 能 `cd ..`。worktree 解决的是**并发写冲突**（两条线改同一个文件），
不解决**越权访问**。真正的越权防线只有 §11.4 的权限层与 `safe_path` 围栏
（`tools_manager.py:459`，围栏跟着传入的 cwd 走）。

## 12. 与 s13_agent_teams 的有意差异

| # | 差异 | s13 | lcc | 动机 |
|---|---|---|---|---|
| 12.1 | 路径集中 | 仓库根 `.tasks/` / `.mailboxes/` / `.worktrees/` | `env.py:21/23/25` 提供 `.lcc/task`、`.lcc/mailboxes`、`.lcc/worktrees`（`.gitignore:6` 已忽略 `/.lcc`） | 单点配置，运行产物不散落仓库 |
| 12.2 | 总线实例 | 模块级 `BUS` 单例 | `MessageBus(...)` 由 `tools_manager.py:265` 构造后注入 | 可测、无隐式全局 |
| 12.3 | 跨层调用 | `globals()` 里摸 `taskManager` / `plan_gates` | 回调注入：`plan_gate_check`、`worktree_validator`、`worktree_cwd_resolver`、`on_assignment_advanced/released`（`task_manager.py:58-67`）+ `tool_adapters`/`permission_check`/`hooks_trigger` | 模块边界清晰，不成环 |
| 12.4 | 输出 | 满屏 `print` | `log_info`/`log_warn`/`log_error` + 域 tag `bus` / `team` / `wt`（`log.py` 头注释） | 分级、着色、可关 |
| 12.5 | 名字校验 | `validate_worktree_name` 返回错误字符串（易被调用方忽略） | 抛 `ValueError`（`worktree_manager.py:48`） | 忘了接也漏不过去（fail-closed） |
| 12.6 | 租径漂移 | `assignment_cwd` 重算值与台账不符 → 抛异常 | **自愈回写** `taskManager.assignments` 后继续（`worktree_manager.py:190` 起） | 一次记账滞后不该打断整轮工作 |
| 12.7 | 工具参数名 | `edit_file` 用 s13 自有命名 | 沿用 lcc `old_string` / `new_string`（`agent_teams_manager.py:130-141`） | 与主工具表同款，模型不必学两套 |
| 12.8 | 完成回执 | `complete_task` 只报完成 | 保留 lcc 的 `Unblocked: ...` 差集报告（`task_manager.py:354`） | 依赖图解锁信息对团队调度有用 |
| 12.9 | PreToolUse 返回值 | 忽略钩子返回 | **非 None 返回即拦截**（`agent_teams_manager.py:495` 起） | 钩子终于真的能拦 |
| 12.10 | 计划闸门消息 | `Cannot complete while plan status is X for task_id` | 去掉 `task_id`（`_plan_gate_check` :370） | owner 已在上下文里，不重复 |
| 12.11 | 认领文本 | `Claimed {id}` | `Claimed {id} ({subject})`；spawn 侧靠 `startswith('Claimed ')` 判定成功 | 见 §14.1，诚实标注的脆弱耦合 |
| 12.12 | WorktreeManager 职责 | 直接改 `plan_gates` / `active_teammates` | 只碰任务侧；协议台账归 `AgentTeamsManager` | 车道分工（Lane A/B/C/D） |
| 12.13 | `task_worktree_cwd` | 返回 `(path, error)` | 返回 `str \| None`（:169），错误由 Lane A 的 `_task_cwd` 组装 | 贴合 `worktree_cwd_resolver` 契约 |
| 12.14 | 后台 bash | teammate 也可后台跑 | `run_in_background` 只给 Lead（:106） | 后台任务需要单一调度者 |

## 13. 快速上手

前提：`python loop.py` 正常起跑，`.lcc/` 可写，当前目录是一个 git 工作树
（要建 worktree 的话）。团队是**可选能力**——不 spawn 队友时，agent teams 对主循环零影响。

Lead 的典型编排（模型侧的工具调用序列，人类只需在开头确认一次）：

```
①  用户：给这个项目加缓存层，可以组队做
②  Lead 先提案并等确认（prompt_teams 的硬要求：先问再组队）
    "我打算开 2 个队友：cache-core 改存储、api-cache 改路由，
     各占一个 worktree。要我这么组吗？"
    —— 用户点头才继续，绝不自说自话 spawn

③  建任务图（Lead 独占）
    create_task(subject="抽出 CacheStore", description="...")      → task_a1b2c3d4
    create_task(subject="路由接入缓存", description="...")          → task_e5f6a7b8
    update_task(task_e5f6a7b8, addBlockedBy=["task_a1b2c3d4"])     # 依赖：后者等前者

④ （可选）为需要独立目录的任务开隔离区
    create_worktree(name="cache-core", task_id="task_a1b2c3d4")
    create_worktree(name="api-cache", task_id="task_e5f6a7b8")
    # 只在"同目录会互相覆盖"时才建；建完 task.worktree 绑定，
    # 认领时 _task_cwd 自动把该任务的工具 cwd 换成 worktree 路径

⑤  派活 + 立规矩
    spawn_teammate(name="cache-core", role="storage engineer",
                   prompt="按 CacheStore 抽象重构…… 交付物：xxx.py + 单测",
                   task_id="task_a1b2c3d4", require_plan=True)
    spawn_teammate(name="api-cache", role="backend engineer",
                   prompt="先读 CacheStore 再动路由，不要碰 storage 层文件")
    # require_plan=True → gate=required，它的 bash/write/edit 会一直被拦，
    #   直到 submit_plan 被 review_plan 批准
    # 队友在干活期间若自己找到下一件可做的活，会在 idle 心跳里自动 claim

⑥  结束这一轮（不轮询邮箱）
    Lead 输出 "两人已就位，等有回音" 并停手 —— 团队事件靠 §9 的唤醒机制送回

⑦  被事件唤醒后协调
    [Team events]
    [result] cache-core: CacheStore 抽好了，Unblocked 了 task_e5f6a7b8
    [idle_notification] api-cache: Waiting for more work.
    [plan_approval_request request_id=req_000001] cache-core: 计划：新建
      lcc/cache/store.py，把 …… 迁过去，改 3 个调用点
    → review_plan(request_id="req_000001", approve=True,
                  feedback="迁移调用点分批做，先补一个失败用例")
    → list_teammates() / list_tasks() 核对状态
    → send_message(to="api-cache", content="CacheStore 已就绪，可以动了")

⑧  收尾
    request_shutdown("cache-core")
    request_shutdown("api-cache")
    # 队友 ack 后线程 finally 自动摘账 + 回收租约；
    # list_teammates 变空时 loop 打一条"所有队友已下线"

⑨  人类做的事（LLM 工具表里没有这两件，别试图调）
    - git merge / rebase 把 wt/cache-core 的分支并回主干，分支不会被 remove_worktree 删掉
    - worktreeManager.remove_worktree("cache-core")   # Python 层调用
      # 五道闸：注册表合法 / 有绑定 / 绑定任务全 completed / 无租约指它 /
      #         git status --porcelain --ignored 干净
      # 有未提交改动又要硬删：remove_worktree("cache-core", discard_changes=True)
```

单活场景不必组队：Lead 自己 `claim_task` → 干活 → `complete_task` 即可，
`agent_loop` 的 finally 会在 turn 边界释放 `"agent"` 的租约（`loop.py:153-157`）。

## 14. 已知坑与设计注记

### 14.1 靠返回文本前缀判定认领成功

`spawn_teammate`（`agent_teams_manager.py:628` 起）带 `task_id` 时调
`taskManager.claim_task(...)`，**以结果字符串 `startswith('Claimed ')` 判断成败**，
失败则回滚三本台账。这是 s13 的原始实现，也是本移植保留的一处**脆弱耦合**：
`claim_task` 的措辞一旦改动，spawn 会误判为失败并把好端端的认领"回滚"成孤儿状态。
改动 `Claimed` 前缀（`task_manager.py:312` 起）时必须同步这里。

### 14.2 at-most-once 会丢消息

`read` 是"读完即删"（§4.3）。进程在处理中崩溃 → 那批邮件永久消失。
团队协议的后果有限（案卷在内存，重启即作废；任务状态在盘上，租约降级后可重认领），
但队友/Lead 之间的**业务约定**（"你把 X 改完发我"）可能凭空蒸发。
真需要可靠投递时，正确答案是加 ack，不是假装 JSONL 追加能兜住。

### 14.3 队友不会被打断

`request_shutdown` 只是"请求"，正在跑的长命令必须自己结束。
`teammateThreads` 从不 `join`（进程退出不能被一个卡住的队友绑架）。
`list_teammates` 里长期 `stopping` 的那个，就是卡在某一步的线程，去它的
`mailboxes/<name>.jsonl` 和最近一条 `team` tag 日志找线索。

### 14.4 一人一活带来的调度约束

闸门 ③④ 意味着：**Lead 自己占着任务时，队友领不到活**——不是队友的问题，
是 Lead 手里那份 `in_progress`。同理，让队友"顺手再做一件"必然被拒。
需要并行就多 spawn 一个队友，而不是给同一个 owner 塞两件活。

### 14.5 `require_plan` 的粒度

闸门只拦**写操作**（`bash` / `write_file` / `edit_file`）。队友在待审期间照样能读代码、
能 `glob`、能 `send_message`、能 `submit_plan`。这是刻意的：审批针对"改动"，
不针对"了解现状"。反过来也意味着 `bash` 里跑测试脚本这类"看起来是读"的动作会被拦，
需要它先交计划。

### 14.6 跨进程锁的双实现

`task_manager.py` 的 `task_store_lock`（:97）= 线程 `RLock` + 系统文件锁
（POSIX `fcntl.flock` / Windows `msvcrt.locking` 二选一，按 `os.name` 分派），
并用 `threading.local` 记深度，只有最外层真的加解锁。
锁文件是任务目录下的 `.lock`（:41），靠"不以 `task_` 开头"躲过 `glob("task_*.json")`。
Windows 侧用 `LK_LOCK` 重试环模拟"无限等待"。

同目录同时跑两个 lcc 进程时，租约台账（`assignments`）是**内存**的、只有磁盘
`in_progress` 能被对方看到——所以闸门 ④ 是跨进程的唯一防线，闸门 ③ 不是。

### 14.7 `send_message` 的键覆盖

`tools_manager.py:331-332` 用 `update` 并入团队 handler 表，`SEND_MESSAGE` 这个键
只存在于团队表，因此不存在"覆盖掉主表某个同名工具"的问题；但**新增同名工具时要当心**
这条 update 语义——后写者胜。

### 14.8 worktree 失败后的人工介入

`create_worktree` 半途而废时返回 `Partial operation` 并列出残留（分支建了没、目录建了没、
注册表进没进），**从不清理**。此时最省事的路线常是换个 `name` 重建，旧残留由人删。
`remove_worktree` 又要求"注册表里有合法条目"才放行，所以**没注册成功的目录它也不认**，
不能当作清理工具用。

## 15. 关联文档与接口索引

| 文档 | 与本模块的关系 |
|---|---|
| [TASK_MANAGER.md](TASK_MANAGER.md) | 任务板、状态机与 `blockedBy` 依赖；本文的租约台账、六闸、原子 save 是其扩展 |
| [TOOLS_MANAGER.md](TOOLS_MANAGER.md) | 组装根（:265-290）与 `run_agent_*` 的 cwd 路由（:524-548） |
| [PERMISSION.md](PERMISSION.md) | `check_permission(block, prompt_user=False)` 的非交互分支 |
| [HOOKS.md](HOOKS.md) | `trigger_hooks(..., skip_permission=True)` 与"非 None 即拦截" |
| [BACKGROUND_TASKS_MANAGER.md](BACKGROUND_TASKS_MANAGER.md) | `run_bash_process(command, cwd)`（:108）为队友透传 cwd 而改 |
| [ENV.md](ENV.md) | `mailboxesDirPath` / `worktreesDirPath` 两个新路径 |
| [CRON_SCHEDULER.md](CRON_SCHEDULER.md) | `stop_runtime_threads()` 与 `run()` finally 的退出顺序 |

关键接口速查（全部经 grep 核对存在于源码）：

| 模块 | 接口 |
|---|---|
| `message_bus.py` | `is_valid_agent_name` :20 · `MessageBus` :24 · `send` :66 · `read` :79 · `peek` :85 · `wait_for_messages` :96 |
| `worktree_manager.py` | `WorktreeManager` :29 · `validate_worktree_name` :48 · `run_git` :90 · `registered_worktrees` :123 · `task_worktree_cwd` :169 · `is_valid_worktree` :176 · `assignment_cwd` :190 · `release_completed_assignment` :218 · `release_teammate_assignment` :221 · `create_worktree` :234 · `remove_worktree` :323 |
| `agent_teams_manager.py` | `ProtocolState` :76 · `TEAMMATE_TOOLS` :189 · `TEAM_TOOLS` :217 · `team_schemas` :346 · `teammate_schemas` :350 · `_plan_gate_check` :370 · `new_request_id` :383 · `match_response` :391 · `consume_lead_inbox` :424 · `format_team_events` :441 · `current_work_identity` :452 · `_teammate_submit_plan` :462 · `_run_teammate_tool` :495 · `apply_plan_response` :533 · `apply_shutdown_request` :570 · `claim_next_task` :607 · `spawn_teammate` :628 · `run_spawn_teammate` :690 · `run_list_teammates` :696 · `run_send_message` :706 · `run_request_shutdown` :714 · `run_request_plan` :733 · `run_review_plan` :743 · `run_create_worktree` :773 · `run_list_tasks` :780 · `TeammateRuntime` :801 · `handle_inbox` :901 · `work` :942 · `wait_for_work` :998 · `run` :1033 |
| `task_manager.py` | `worktree` 字段 :34 · `assignments` :52 · `assignment_versions` :54 · 回调声明 :58-67 · `task_store_lock` :97 · `advance_assignment_version` :136 · `_task_cwd` :147 · `claim_task` :312 · `complete_task` :354 · `release_completed_assignment` :400 · `release_teammate_assignment` :419 · `scan_unclaimed_tasks` :435 |
| `loop.py` | `prompt_teams` :60 · `inject_team_events` :130 · `check_team_offline_edge` :145 · `agent_loop` :153 · `wait_for_cli_event` :258 · `run` :292 |
| `tools_manager.py` | 组装 :265-290 · 注册 :331-332 · `safe_path` :459 · `_agent_cwd` :524 · `run_agent_*` :530-548 |
| `tool_names.py` | 八个新常量 :24-31 |

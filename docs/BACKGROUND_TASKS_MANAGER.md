# BackgroundTasksManager 技术文档

> 对应源码：`background_tasks_manager.py`（本仓库当前版本 225 行）
> 状态：完整，**已接入主循环**（`tools_manager.py:264` 构造为 `ToolsManager` 的自持成员；
> 后台路由在 `tools_manager.py:372` 的 `execute_tool`、结果注入在 `loop.py:114/187` 的
> `inject_background_results`）。s15 集成后追加：构造时注入 `hooks_trigger` 回调
> （`tools_manager.py:262-266`，worker 完成时触发 PostToolUse）、`start` 接受派发时刻解析的
> `cwd`、`has_pending` 作为第四唤醒源参与主循环事件轮询（`loop.py:348-351`）
> 引入提交：`7fd5f0e` feat(tools): run bash tasks in background via BackgroundTasksManager

## 1. 它解决什么问题

`run_bash` 是阻塞的：一条命令最长挂 120 秒（`run_bash_process` 的
`communicate(timeout=120)`，`background_tasks_manager.py:163`），期间整个 agent 回合
干等。对"编译 / 跑测试 / 起服务"这类长任务，模型不该被卡在这一轮里。

本模块把 bash 变成**发射即忘 + 稍后收割**：

1. **异步启动**：模型在 `bash` 工具里带 `run_in_background: true`，`execute_tool` 不
   阻塞执行，而是登记一个后台任务、起一条守护线程去跑子进程，**立即**回一条
   "已启动"的占位 tool_result（§5）；
2. **结果收割**：主循环每轮开头调 `collect_background_results()`（`loop.py:114`），把
   此刻已完成的任务结果攒成 `<task_notification>` 文本、注入对话（§7）。s15 起 `has_pending`
   还是主循环第四唤醒源——即便模型终轮直接返回，待收割结果也能唤醒新一轮（§10）；
3. **进程托管**：所有子进程（含前台 bash）登记在 `shell_processes` 集合，退出/信号时
   统一收割，不留孤儿（§8）。

对外没有直接入口——模型侧只看到 `bash` 的一个可选参数（`tools_manager.py:33`）；
调度全在 `ToolsManager.execute_tool`（TOOLS_MANAGER.md §5），收割全在 `Loop`
（`loop.py:113-141`）。本类是被这两头驱动的引擎。

## 2. 数据结构（`__init__`，17-29）

| 成员 | 类型 | 用途 |
|---|---|---|
| `hooksTrigger` | 回调 / `None` | 组装根注入的 PostToolUse 触发器，签名 `(event, block, output)`；为 `None` 时 `run` 跳过钩子层（26-27，向后兼容） |
| `env` | `Env()` | 第 3 个 Env 实例（ENV.md §4）；只用 `workDirPath` 当子进程 cwd 缺省（156） |
| `tasks` | `dict[str, dict]` | 在途任务：`task_id → {tool_use_id, command, status, cwd}`；完成即被 `collect` 弹出（105） |
| `results` | `dict[str, str]` | 完成后的结果文本，键为 `task_id`；`collect` 弹出（106） |
| `ready` | `list[str]` | 已完成、待收割的 `task_id` 队列；`run` 追加（97），`collect` 清空（102） |
| `counter` | `int` | 自增序号，生成 `bg_{counter:04d}`（39-40） |
| `lock` | `threading.Lock` | 守护 `tasks/results/ready/counter` 的临界区 |
| `shell_processes` | `set[subprocess.Popen]` | 全部存活子进程（前台+后台），供退出收割（160-161/181-182） |
| `shell_processes_lock` | `threading.RLock` | 守护 `shell_processes`（与 `lock` 分离，因 `run_bash_process` 前台线程也会碰它） |

- **task_id 形态**：`f"bg_{self.counter:04d}"`（40）——`bg_0001` 起，4 位补零，进程内唯一
  （非跨进程，见 §10 不变量 4）。
- **两个锁**：任务表用 `self.lock`，子进程集合用 `self.shell_processes_lock`。后者是
  `RLock` 且独立，正是因为 `run_bash_process` 既被后台线程（经 `run`）调用、又被主线程
  的 `run_bash`（`tools_manager.py:481`）调用，两处都要往 `shell_processes` 增删。

## 3. 方法地图

| 分组 | 方法 | 行号 | 说明 |
|---|---|---|---|
| 生命周期 | `start` | 30-58 | 校验 → 登记 task（含派发时刻解析好的 `cwd`）→ 起 daemon 线程跑 `run` → 返回 `task_id` |
| | `run` | 61-97 | 线程体：`run_bash_process` → `format_bash_result` → **触发 PostToolUse 钩子**（经注入的 `hooksTrigger`，77-88）→ 写 `results`/`ready`、置 status |
| | `collect` | 99-125 | 排空 `ready`，弹 `tasks`/`results`，逐条渲染 `<task_notification>` |
| 对外薄封装 | `should_run_background` | 127-128 | 判定"是否该转后台"：`bash` 且 `run_in_background is True` |
| | `has_pending` | 130-135 | 是否有已落账待收割结果：**只查不清零**（收割仍由 `collect` 负责），供主循环 bg 唤醒源轮询 |
| | `start_background_task` | 140-141 | 转发 `start`（带 `cwd`） |
| | `collect_background_results` | 137-138 | 转发 `collect` |
| 进程执行 | `run_bash_process` | 145-182 | **前后台共用**：`Popen` + `communicate(120)` + 截断 50000 + 收尾收割 |
| | `format_bash_result` | 217-220 | 退出码非 0 → 前缀 `Error: command exited with status N` |
| 进程管理 | `stop_process_group` | 184-201 | POSIX `killpg(TERM→KILL)` / Windows `terminate/kill` |
| | `stop_all_shell_processes` | 203-207 | 遍历 `shell_processes` 逐个收 |
| | `handle_termination_signal` | 209-211 | SIGTERM → 收进程 → `SystemExit(128+signum)` |
| | `register_exit_handlers` | 213-215 | 注册 SIGTERM 处理 + `atexit`（构造时即调，29） |

## 4. 启动流程：start（30-58）

```
start(block, cwd=None)
├─ block.name != "bash" → raise Exception（31-32）        ← 只受理 bash
├─ command 取 block.input["command"]，非 str 或空 → raise ValueError（34-36）
├─ with lock:  ← 临界区
│     counter += 1；task_id = bg_{counter:04d}（39-40）
│     tasks[task_id] = {tool_use_id: block.id, command, status: "running",
│                       cwd}（41-47）  ← cwd 是调用方（execute_tool）派发时刻解析好的
│                                       Lead 租约工作目录，此处只记录不再解析
├─ thread = Thread(target=run, args=(task_id, command, cwd, block), daemon=True)（49-50）
├─ thread.start()；失败 → 回滚 tasks.pop 再 raise（51-56）
└─ log_info "[bg] started {task_id} {command[:60]}"（57）→ return task_id（58）
```

- **daemon 线程**（49-50）：主进程退出时不被后台任务吊住——配合 §8 的 `atexit` 收割；
- **登记先于起线程**：`tasks[task_id]` 在 `thread.start()` 之前写好，线程体 `run` 一定能查到
  自己的 task（否则 `run` 里 `self.tasks.get` 返回 None 会静默丢结果，92-93）；
- **回滚**：`thread.start()` 抛异常时把刚登记的 task 弹出再上抛（51-56），不留僵尸条目；
- `start` 抛出的异常（非 bash / 空命令 / 线程起不来）由 `execute_tool` 的 `try/except` 兜成
  文本 `Error: Failed to start background task: {Type}: {msg}`（`tools_manager.py:391-395`），不会炸穿主循环。

## 5. 线程体：run 与结果落地（61-97）

```
run(task_id, command, cwd=None, block=None)
├─ output, exit_code = run_bash_process(command, cwd)（64）
├─ result = format_bash_result(output, exit_code)（65）
├─ status = "completed" if exit_code == 0 else "failed"（67-69）
│     ← 异常 → result = "Error: {Type}: {e}", status = "failed"（71-73）
├─ hooksTrigger 非 None 时（77-88）：
│     intercepted = hooksTrigger("PostToolUse", block, result)
│         ← 拦截值非 None 时直接以 "{intercepted}\n{result}" 拼在前（87-88）
│     钩子自身 raise → result = "[hook error] {Type}: {e}\n{result}"，status 降级
│         "failed"（83-85）
└─ with lock:
      task = tasks.get(task_id)；None → return（92-93）
      task["status"] = status；results[task_id] = result；ready.append(task_id)（95-97）
```

- **status 语义**：`completed` 仅代表"子进程退出码为 0"；超时（`run_bash_process` 回
  `(..., None)`）、任何异常、非 0 退出码、钩子异常一律 `failed`；
- **钩子在锁外触发**（77-88）：PostToolUse 回调在后台线程执行，注册到该事件的钩子
  须自行保证线程安全（模块头注释 `background_tasks_manager.py:12-15` 的约定）；
- **result 存的是完整文本**：`run_bash_process` 内部已按字符截断到 50000（166），
  `format_bash_result` 可能再加 `Error: ...` 前缀——注意 §7 通知里还会再截一次 200。

## 6. 进程执行：run_bash_process（145-182）——前后台共用

这是**唯一真正跑 shell 的地方**：后台线程经 `run` 调它，前台 `run_bash`
（`tools_manager.py:481`）也直接调它，二者行为一致。

```
Popen(command, shell=True, stdout=PIPE, stderr=PIPE, text=True,
      errors="replace", cwd=cwd or env.workDirPath, start_new_session=True)  ← 149-158
  shell_processes.add(process)                                          ← 160-161
  stdout, stderr = process.communicate(timeout=120)                     ← 163
  output = (stdout + stderr).strip()                                    ← 164
  ├─ output 非空 → return (output[:50000], returncode)                  ← 166-167
  └─ 空         → return ("(no output)", returncode)                    ← 169
except TimeoutExpired → ("Error: Timeout(120s)", None)                  ← 170-171
except Exception      → ("Error: {Type}: {e}", None)                    ← 172-173
finally:
  stop_process_group(process) → process.wait(0.2) → shell_processes.discard(process)  ← 174-182
```

- **与旧前台 bash 的差异**：TOOLS_MANAGER.md §6.1 记的 `subprocess.run(...)` 已改由本函数
  承担，参数从 `cwd=env.workDir` 变为 **`cwd=cwd or env.workDirPath`（Path 对象）**、新增
  **`start_new_session=True`**（自成一个进程组，便于 §8 按组收割）；后台链路的 `cwd` 由
  `execute_tool` 在**派发时刻**解析 Lead 租约后透传进来（缺省/前台为 None → 回落
  `env.workDirPath`），见 §9 与 docs/INTEGRATED_HARNESS.md §6.2；
- **截断**：`[:50000]` 是 Python 字符切片，非 token 预算（中文更贵，同 TOOLS_MANAGER.md
  §11.3）；空输出 → `(no output)`；截断不产生任何"已截断"标记；
- **finally 必收尾**：无论成功/超时/异常，都 `stop_process_group` + `discard`——保证
  `shell_processes` 不泄漏已完成进程（`wait(timeout=0.2)` 的短暂宽限见 §11.2）。

## 7. 收割与通知格式：collect（99-125）

`collect()` 是**非阻塞快照**：只处理"此刻已在 `ready` 里的"，没有也不等。

```
with lock:                                        ← 101
    task_ids = list(ready); ready.clear()         ← 102-103
    for id: task = tasks.pop(id); result = results.pop(id, "")   ← 104-108   （一次性消费）
for (id, task, result) in ready:                  ← 111
    log_info "[bg] collected {id}: {status}" ← 112
    追加 <task_notification>…                     ← 116-123
return notifications: list[str]                   ← 125
```

通知格式（116-122）：

```xml
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <command>…原始命令…</command>
  <summary>…结果前 200 字…</summary>
</task_notification>
```

- **`summary = result[:200]`**（113-115）：喂给模型的结果被截到 **200 字符**（s15 起，旧版为
  500）——比 `run` 里存的 50000 短两个数量级，是后台结果"只给个摘要"的有意收窄（对比前台
  `run_bash` 可见 50000）；`failed` 与 `completed` 共用同一模板，由 `<status>` 区分；
- **一次性消费**：`tasks.pop` + `results.pop`（105-106）确保同一结果不被二次投递；
- **status 但非 tool_use_id**：task 里存了 `tool_use_id`（42）却**不进**通知——通知是无
  tool_use 关联的独立文本块，见 §9 为何这样设计能保持 API 配对。

## 8. 进程管理与退出收割（184-215）

- `stop_process_group`（184-201）分平台：
  - POSIX（`hasattr(os, "killpg")`）：对进程组 `killpg(SIGTERM)`，0.05s 后仍活着则
    `killpg(SIGKILL)`；`ProcessLookupError/OSError` 直接 return（已没了）；
  - Windows：无 `killpg` → `terminate()` + 0.05s + `kill()`（195-201）。⚠ 只能收割 shell
    本身，shell 再生的孙进程在 Windows 上可能残留（`start_new_session` 在 Windows 不被
    honored，见 §11.1）；
- `stop_all_shell_processes`（203-207）：快照 `shell_processes` 后逐个 `stop_process_group`；
- `register_exit_handlers`（213-215）：`__init__` 里即调用（29），注册 **SIGTERM →
  `handle_termination_signal`（收进程 + `SystemExit(128+signum)`）** 与 **`atexit →
  stop_all_shell_processes`**。⚠ `signal.signal` 必须在主线程注册——本类恰好只在
  `ToolsManager()` 构造时（主线程）被实例化一次，见 §10 不变量 2。

## 9. 与 execute_tool 的接线（TOOLS_MANAGER.md §5）

模型产出 `tool_use(bash, {command, run_in_background: true})` 后，闸门在
`tools_manager.py:379` 决策：

```
execute_tool(block, handlers, allow_background=True, skip_approval=False)
├─ allow_background 且 should_run_background(name, input)?   ← tools_manager.py:379
│     是 → cwd, cwd_error = _agent_cwd()；租约失效 → 直接 return cwd_error，
│           不启动任务                                    ← 380-384
│         start_background_task(block, cwd=cwd) → task_id ← 386
│           output = "[Background task {task_id} started] Result will arrive
│                     as a task_notification."             ← 387-390
│           （启动异常 → "Error: Failed to start background task: {Type}: {msg}"）
│                                                          ← 391-395
│         就地 return str(output)                           ← 396-398
│           ——后台分支**不经**前台 PostToolUse：占位 output 不触发钩子，
│             PostToolUse 改由 worker 在任务完成时触发（§5，避免双触发）
│     否 → 走前台 handler 分支，dict(block.input).pop("run_in_background") 吸收该参数
│          （见 TOOLS_MANAGER.md §5；子代理 allow_background=False → 前台降级）
└─ 前台分支 trigger PostToolUse(block, output) → return str(output)  ← tools_manager.py:414-415
```

- **立即回填的是占位文本**，不是真实结果。原始 `tool_use` 的配对在这一刻就闭合了——
  这正是 §7 通知"不带 tool_use_id、以独立 user 文本注入"仍安全的根本原因（不存在悬空
  tool_use）；
- **门控三条件**（`should_run_background` 127-128 + `allow_background`）：只有 ①调用方允许
  ②工具名是 `bash` ③`input.get("run_in_background") is True` 全满足才转后台。注意
  **`is True`** 是严格布尔判定（128），模型传字符串 `"true"` 不算数，会退回前台（见 §11.4）。

## 10. 与 inject_background_results 的闭环与轮询语义（loop.py）

**谁触发收割、何时触发——这是本模块最该说清的一件事：**

- 全库只有一处调用 `collect_background_results()`：`loop.py:114` 的
  `inject_background_results`；
- 而 `inject_background_results(messages)` 只在主循环 `while True` 的**每一轮顶部**被调用一次
  （`loop.py:187`），且**在** `compactManager.prepare`（`loop.py:188`）**之前**；
- **没有计时器、没有后台轮询线程**——"轮询"粒度 = 一个 agent 回合。`collect` 非阻塞，
  有就返回、没有就返回空 list（104-108 空则直接 return）。

注入逻辑（`loop.py:118-139`）：把每条通知包成 `{"type":"text","text":…}` 块，若
`messages[-1]` 是 user 角色就 `content.extend(blocks)`（内容已是 list，128-129）或把 str 内容升级成
list 再拼（131-134）；否则新建一条 user 消息（136-139）。这与既有的 todo `<reminder>` 注入
（`loop.py:315-319`）是同一套"往最后一批 user 内容里追加文本块"的手法。

```
主循环单轮（loop.py:186-）
while True:
    inject_background_results(messages)   ← 187  收割→合并进上一条 user / 新建 user
    compactManager.prepare(...)           ← 188
    response = withRetry(create(...))      ← 218-227  模型这一轮就能看见 <task_notification>
    ... execute_tool 可能又 start 新后台任务（386）
```

s15 起主循环事件等待 `wait_for_cli_event`（`loop.py:335-365`）把 **`has_pending` 列为第四
唤醒源**（`loop.py:348-351`）：inbox→cron→bg→stdin 的优先级轮询，命中 bg 时以空 prompt
进入新一轮 turn（不进对话内容），到圈首由 `inject` 正常收割——"收割"动作本身仍只在
`while` 顶部这一处。

**为什么是单实例（关键，曾踩坑）**：`Loop` **不再自建** `BackgroundTasksManager`，而是读
`self.toolsManager.backgroundTasksManager`（`loop.py:114`）。若两处各 new 一个，任务登记进
`ToolsManager` 那份、`Loop` 从自己那份 `collect`——`ready` 永远为空，结果**静默丢失**。
`ToolsManager.__init__` 里 `backgroundTasksManager = BackgroundTasksManager(...)`
（`tools_manager.py:264`）是**全库唯一实例**。

**side-channel 不收割**：`memory_manager` / `compact_manager` 的单轮 LLM 调用、以及
`run_subagent` 的子循环（`tools_manager.py:668` 起）**都不调 `inject_background_results`**。
推论：子代理回合内、以及任何旁路调用期间，主循环已启动的后台任务不会被收割；它们只在
下一次主循环 while 顶部被收。

### 轮询的固有延迟（终轮结果由 bg 唤醒源兜住）

结果被注入的时机取决于"任务完成"与"下一次 `inject`"的相对时序：

- 任务在第 N 轮的 `execute_tool` 里启动，若它在第 N+1 轮 `inject`（`loop.py:187`）之前跑完，
  则第 N+1 轮模型就能看见；没跑完则顺延到再下一轮；
- **终轮场景（s15 已解）**：若模型在启动后台任务后直接给出最终答复（该轮 `tool_calls` 为空，
  `loop.py:279-289` 直接 `return` 退出 `agent_loop`），本次请求内不再回到 while 顶部——但
  s15 起 `run()` 的事件循环会经 `has_pending` 感知到待收割结果，以 bg 事件自动开启下一轮
  空 prompt turn 收割（`loop.py:348-351`、:409-411 的 bg 分支），**无需用户再敲一句话**。
  旧版（s12-s14）没有这条兜底，终轮结果确实要等下一次用户交互。

## 11. 已知坑点

1. **Windows 上 `start_new_session` 无效果**：该参数只在 POSIX 触发 `setsid`，Windows 分支
   忽略它；且 Windows 无 `killpg`（185），`stop_process_group` 退化为 `terminate/kill`
   单个 shell 进程——`shell=True` 下 cmd 再 spawn 的孙进程可能杀不干净，长跑后台任务在
   Windows 退出时可能留孤儿子进程；
2. **`finally` 里 `wait(timeout=0.2)` 的吞并**（177-180）：`stop_process_group` 已发
   SIGTERM/SIGKILL 后只给 0.2 秒收尸，超时即 `pass`。极端下进程未真正reap，但至少
   信号已发出，属可接受的宽限；
3. **通知里的 200 字截断**（113-115）：模型看到的后台结果只有前 200 字符，长输出的尾部（含
   关键报错、汇总行）会被切掉且**无"已截断"标记**——与 §6 的 50000、前台的 50000 三档
   收窄，别指望 `<summary>` 里看到完整日志；
4. **`is True` 严格判定**（128）：`run_in_background` 必须是布尔 `true`。模型若吐字符串
   `"true"`/数字 `1`，`should_run_background` 判假 → 退回前台**阻塞执行**（不报错，观感是
   "说好的后台却卡住了"）；前台分支随后 `pop` 掉该键，handler 不会因它收到未知关键字
   （TOOLS_MANAGER.md §5）；
5. **`signal.signal` 的主线程约束**（214）：`register_exit_handlers` 在 `__init__` 即跑。
   本类只在主线程被实例化一次（§10），安然无恙；若将来有人在 worker 线程里 `new` 一个
   `BackgroundTasksManager`，构造当场 `ValueError: signal only works in main thread`；
6. **task_id 非跨进程唯一**：`counter` 是实例内存计数（25/40），重启进程从 `bg_0001` 重来。
   只在单次会话内做键用，别当持久 ID（本模块无任何落盘，与 TaskManager 的落盘看板
   TASK_MANAGER.md 完全不同物种）；
7. **无"取消/查询单个任务"接口**：`tasks`/`results` 只能通过 `collect` 一次性收割，模型无法
   中途取消一个后台任务、也无法"只看某个 bg_id"——只有跑完进 `ready` 才会现身。

## 12. 不变量（改代码前必读）

1. **全库唯一实例、且只在主线程构造**：`tools_manager.py:264` 那一个。`Loop` 经
   `toolsManager.backgroundTasksManager` 复用（`loop.py:114`），绝不另建（§10 双实例丢结果
   教训）；
2. **只有 bash 能后台**：`start` 硬拒非 bash（31-32），`should_run_background` 也硬卡
   `name == "bash"`（128）——给别的工具开后台是设计外的；
3. **占位先闭合配对，通知后独立注入**：启动即回 `[Background task …]` tool_result
   （`tools_manager.py:387-390`）满足 API 的 tool_use↔tool_result 配对；真结果一律走
    `<task_notification>` 的纯文本 user 块，不回填原 tool_use_id（§7/§9）；
4. **`collect` 一次性消费**：弹出 `tasks`/`results`/`ready`（102-106），同一结果只投一次；
5. **前后台共用 `run_bash_process`**（145-182）：改超时/截断/cwd 会同时影响前台
   `run_bash` 与后台 `run`，两处行为必须一致；
6. **子代理禁止后台**：schema 层 `sub_bash_info`（`tools_manager.py:421-424`）删掉
    `run_in_background`，执行层 `run_subagent` 传 `allow_background=False`
    （`tools_manager.py:696`）——双保险，见 TOOLS_MANAGER.md §2.6/§7。

## 13. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `tools_manager.py` | 唯一持有方：`tools_manager.py:264` 自建唯一实例（构造时注入 `hooks_trigger`，262-266）；`execute_tool` 调 `_agent_cwd`/`should_run_background`/`start_background_task`（`tools_manager.py:379-398`）；前台 `run_bash` 复用 `run_bash_process`/`format_bash_result`（`tools_manager.py:490-491`）；`sub_bash_info`（421-424）从 schema 层禁后台 |
| `loop.py` | 唯一收割方：`inject_background_results`（113-141）在 `agent_loop` while 顶部（187）调 `collect_background_results`（114），把通知并入上一条 user 消息；`has_pending` 是 `wait_for_cli_event` 的第四唤醒源（348-351，只查不清）；side-channel 调用与子代理循环不收割 |
| `env.py` | 取 `workDirPath` 作子进程 cwd 缺省（`background_tasks_manager.py:156`，后台链路派发时刻透传 `cwd` 优先）；第 3 个 Env 实例（ENV.md §4） |
| `hooks.py` | s15 起有直接关系：后台完成时由 **worker 线程**经注入的 `hooksTrigger` 触发 PostToolUse（`background_tasks_manager.py:77-88`，组装根 `tools_manager.py:262-266`）——钩子须线程安全；占位 output 不再触发 PostToolUse（后台分支就地 return，`tools_manager.py:396-398`） |
| `compact_manager.py` | 间接：`inject` 在 `prepare` 之前写消息，通知文本会随后续轮次被压缩流水线纳入历史（COMPACT_MANAGER.md） |
| `task_manager.py` | **物种不同**：TaskManager 是落盘的依赖图看板（TASK_MANAGER.md），本模块是纯内存、进程级、会话内的一次性后台 shell 队列，二者无代码交集 |

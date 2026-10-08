# Integrated Harness（s15）技术文档

> 对应源码：`recovery.py`（本仓库当前版本 89 行）+ 集成改动落在
> `loop.py` / `tools_manager.py` / `background_tasks_manager.py` / `permission.py` / `hooks.py` / `env.py`
> 状态：s15 **不引入新机制**，把 s10-s14 已移植的各子系统接进同一个循环——
> "后台完成即唤醒、调用失败即恢复、异步 turn 不抢审批"三段集成胶水已就位
> （恢复状态构造 `loop.py:180-181`、重试包裹 create `loop.py:214-227`、bg 唤醒源
> `loop.py:348-351`、异步拒绝 `permission.py:80-81`）
> 来源：learn-claude-code `s15_integrated_harness`（单文件测试台 `code.py`，3291 行）
> 注记：本版对应未提交工作区（基线 HEAD `1ea152e`，改动文件见文末 §11 表尾注）

## 1. 它解决什么问题

s10-s14 各子系统（后台任务、压缩、记忆、teams、MCP）在 lcc 里是**逐个移植**的，
每个都独立可用，但互相之间留有"断点"：

1. **后台任务完成了没人叫醒循环**：旧主循环只被 user 输入 / cron / 团队信箱唤醒；
   后台 bash 跑完后，结果要等"下一次用户交互"才现身（BACKGROUND_TASKS_MANAGER.md §10
   的终轮陷阱）。s15 补第四唤醒源：`has_pending` 有终态即触发一次异步 turn（§6.1）；
2. **模型调用失败直接炸穿 CLI**：旧 `loop.py` 对 API 异常要么上抛、要么只在
   prompt-too-long 时试压缩一次。s15 引入统一恢复层：429/529 指数退避重试、
   连续 529 达阈值切 fallback 模型、max_tokens 截断升档/续打、其余异常降级为
   `[Error]` 文本收尾（§3/§4）；
3. **异步 turn 会抢占控制台**：wake/cron/bg 驱动的 turn 若命中需确认的权限规则，
   会和 stdin 读线程抢 `input()`。s15 用 `skip_approval` 旗标贯穿调用链，
   异步轮命中即**fail-closed 拒绝**，绝不弹交互（§5）；
4. **后台任务的执行语义残缺**：cwd 不透传（永远在 workspace 根）、完成不触发
   PostToolUse、通知截断过宽。s15 把派发时刻的租约 cwd、worker 侧钩子触发、
   `<summary>` 200 字符收窄一并接上（§6）。

一句话：**s15 = 把这些机制的"接缝"焊上，不改各机制自身的内部逻辑。**

## 2. 架构图

```
run() 事件循环 (loop.py:385-430)
 │
 ├─ wait_for_cli_event() (loop.py:335-365)      ← 优先级：inbox → cron → bg → stdin
 │    ├─ messageBus.peek("lead")  → ("wake", None, False)        :342-343
 │    ├─ cron.has_cron_queue()    → ("cron", None, False)        :345-346
 │    ├─ bg.has_pending()         → ("bg",   None, False)        :348-351  ★新唤醒源
 │    └─ stdinQueue.get()         → ("user", line, True)         :353-365
 │
 ├─ user → _run_turn(..., skip_approval=False)                    :400
 ├─ wake/bg/cron → _run_turn(..., skip_approval=True)  :408/:411/:417  ★异步不抢审批
 │
 └─ _run_turn (loop.py:368-383)
      │  turn_start 切片 → 全轮打印 assistant text（M5）          :371-383
      ▼
    agent_loop → _agent_loop_inner (loop.py:174-323)
      │  state = RecoveryState(...) 每 turn 新建                  :180-181
      ▼
    while True:
      ├─ inject_background_results(messages)   ← bg 结果收割注入   :187
      ├─ assemble_pool()（独立 try，s14 语义）                    :194-206
      ├─ withRetry(lambda: client.messages.create(model=state.currentModel, ...), state)
      │        ★退避/换模都发生在这里                             :214-227
      ├─ except → reactive compact（一次性旗标）或 [Error] 降级    :228-246
      ├─ stop_reason == "max_tokens" → 升档重打 / 续打封顶 2 次     :248-265
      └─ execute_tool(block, pool_handlers, skip_approval=...)    :298
             │        (tools_manager.py:372-415)
             ├─ PreToolUse → permission_hook → check_permission(skip_approval)
             │        (hooks.py:32-46 / permission.py:83-122)
             └─ bg 派发：_agent_cwd() 租约闸门 → start_background_task(block, cwd)
                      (tools_manager.py:382-386)
                      ▼
             BackgroundTasksManager worker 线程 (background_tasks_manager.py:61-97)
               run_bash_process(command, cwd) → PostToolUse(hooksTrigger) → ready
                      ▼
               collect() → <task_notification>…<summary>[:200]    :99-125
               （下一圈 has_pending→"bg" 唤醒，圈首 inject 收割）
```

## 3. 恢复层：recovery.py

s15 的恢复三件套（`with_retry` / `RecoveryState` / `is_prompt_too_long_error`，
参考实现 code.py L2193-2241）逐段移植为独立叶子模块，**不 import anthropic SDK**——
异常分类靠 duck 判定（异常类名 + 消息小写子串，`recovery.py:56-59`）。

### 3.1 常量（recovery.py:16-24，对齐 s15 L64-69/L73）

| 常量 | 值 | 位置 | 语义 |
|---|---|---|---|
| `DEFAULT_MAX_TOKENS` | 8000 | :17 | 每轮 create 的默认预算（`loop.py:182`） |
| `ESCALATED_MAX_TOKENS` | 16000 | :18 | 截断一次后升档的预算（`loop.py:252`） |
| `MAX_RETRIES` | 3 | :19 | withRetry 默认重试上限 |
| `MAX_CONSECUTIVE_529` | 2 | :20 | 连续 529 达此数才考虑换模（防抖动误切） |
| `MAX_RECOVERY_RETRIES` | 2 | :21 | 升档后续打封顶次数（`loop.py:258`） |
| `BASE_DELAY_MS` | 500 | :22 | 退避基数 |
| `CONTINUATION_PROMPT` | "Continue from the previous response. Do not repeat completed work." | :24 | 续打指令（逐字 s15 L73） |

### 3.2 RecoveryState —— 七个字段的"一轮恢复账本"（recovery.py:27-38）

`s15 RecoveryState L2193-2199`（5 字段 snake_case）在 lcc 扩为 7 字段、参数化构造：

| 字段 | 语义 |
|---|---|
| `initialModel` | 本 turn 起始模型（`env.modelId`） |
| `currentModel` | 当前生效模型——create 每次读它（`loop.py:220`），529 换模的落点 |
| `fallbackModel` | 换模目标（`env.fallbackModelId`，缺省 None 不启用，§7） |
| `consecutive529` | 连续过载计数；任一成功即归零（:53-55） |
| `maxTokensEscalated` | 是否已升档到 16000 |
| `recoveryCount` | 续打已用次数（封顶 `MAX_RECOVERY_RETRIES`） |
| `hasAttemptedReactiveCompact` | 被动压缩**一次性旗标**（替代旧 lcc 计数语义，§4.3） |

生命周期：**每个 turn（`agent_loop` 一次调用）在 while 之外新建**
（`loop.py:180`，对齐 s15 L3105），该 turn 内部的多轮（退避重试、升档、续打）
复用同一 state；同时挂到 `self.recoveryState`（`loop.py:181`）供外部观察。
reactive compact 之后**不重建** state——旗标必须活过压缩继续生效。

### 3.3 withRetry —— 退避、换模、耗尽（recovery.py:47-81，对齐 s15 L2207-2234）

```
withRetry(operation, state, maxRetries=MAX_RETRIES)
├─ for attempt in 1..maxRetries：                                （:51）
│   ├─ 成功 → state.consecutive529 = 0；return result           （:53-55）
│   └─ except → kind/msg 小写 duck 判定                          （:56-59）
│       ├─ 429/ratelimit（msg 含 "429"/"rate limit"/"rate_limit" 或类名含
│       │    "ratelimit"）→ log_warn("recovery","[429] retry n/m after d.s")
│       │    + sleep 退避                                        （:60-64）
│       ├─ timeout（类名含 "timeout" 或 msg 含 "timeout"）→ 同上退避  ★lcc 新增，s15 无（:65-69）
│       ├─ 529/overloaded → consecutive529 += 1                  （:70-71）
│       │    ├─ 计数 ≥2 且配置了 fallbackModel → currentModel 换模 + 计数归零
│       │    │    （log_warn "切换 fallback 模型"，:72-75）
│       │    └─ sleep 退避重试                                   （:76-79）
│       └─ 其他异常 → 原样直抛（不吞非网络类错误，:80）
└─ 循环耗尽 → raise lastExc（最后一次异常本身，:81；s15 L2234 是
   RuntimeError("Max retries exceeded")，有意差异 §8-⑩）
```

退避公式 `retryDelay`（recovery.py:41-44，逐式 s15 L2202-2204）：

```
base = min(500 * 2^attempt, 32000) / 1000        # 秒
delay = base + random.uniform(0, base * 0.25)    # +25% 抖动
```

注意"连续 529"是**连续**计数：中间任何一次成功即清零（:53-55），
换模后计数也清零（:75）——避免一次抖动就永久换轨。

### 3.4 isPromptTooLong（recovery.py:84-88，逐字 s15 L2237-2241）

`(msg 同时含 "prompt" 和 "long") or "context_length_exceeded" in msg
 or "max_context_window" in msg`（类名+消息小写合并判定）。
它替代旧 lcc 的字面串检测（`("prompt_too_long", "too many tokens")`）——
旧串命中不到多数端点的真实报错文案，新判定面更宽（注释 `recovery.py:85`）。

## 4. 主循环新语义：loop.py

### 4.1 create 包进 withRetry（loop.py:214-227）

`_agent_loop_inner` 圈内的 `client.messages.create` 由 `withRetry(lambda: ..., state)`
包裹：`model=state.currentModel`（:220，529 达阈值后自动是 fallback）、
`max_tokens=max_tokens`（:224，随升档变化）。**权衡注释（:215-217）**：退避的
`time.sleep` 最长 ~32s 会阻塞事件循环——lcc 是单主线程轮询模型（§8-①），
与 s15 双轨下 `agent_lock` 串行化时的阻塞窗口等价，接受。

### 4.2 max_tokens 截断恢复（loop.py:248-265，对齐 s15 L3150-3165）

`response.stop_reason == "max_tokens"` 时两拍：

1. **未升档**（`state.maxTokensEscalated` 为假）：`max_tokens=16000` + 置旗标 +
   `log_warn("recovery","[max_tokens] 升档重打 ...")` + `continue`（:250-255）。
   **绝不 append 半截 response.content**——避免悬空 assistant 块（注释 :251）；
2. **已升档仍截断**：append 半截 assistant（:257），若 `recoveryCount < 2` 则
   append `CONTINUATION_PROMPT` user 消息续打（:258-262）；**耗尽 2 次后按正常
   收尾 return**（:263-265，对齐 s15 L3161-3162：此路径 Stop 钩子不触发）。

正常完成一轮则重置两态（:267-269，对齐 s15 L3164-3165）。

### 4.3 错误降级与 reactive compact 优先序（loop.py:228-246）

create 抛出（含 withRetry 耗尽后上抛的最后异常）时：

- **先试压缩**：`isPromptTooLong(error)` 且 `not state.hasAttemptedReactiveCompact`
  → `reactive_compact` + 置一次性旗标 + `continue`（:230-233，对齐 s15 L3137-3140）。
  旗标制替代旧 lcc 的 `MAX_REACTIVE_RETRIES=1` 计数类常量（已删除）——语义相同
  （每 turn 至多压一次），但状态归进 RecoveryState，随 turn 自然作废（§8-⑤）；
- **否则降级返回**：append 末条 assistant `[Error] {类型}: {消息}`（:237-243）+
  `log_error("loop", "API 调用失败，降级返回: ...")`（:244）+ `return`（:246）。
  **CLI 存活不崩**（旧版此分支 `raise` 掀翻 run()；对齐 s15 L3141-3145，此路径
  Stop 钩子不触发；request release 由外层 `agent_loop` finally 兜底，注释 :245）。

### 4.4 发现期错误仍"终止本轮"（loop.py:190-206）

s14 的 MCP 组装独立 try 原样保留：`assemble_pool` 的 `ValueError`（宿主不变量被
破坏）→ 假 assistant `[Error]` + Stop 钩子 + return（:196-206），不与 API 异常/
reactive compact 混进同一个 try（注释 :190-193）。s15 的恢复层围绕的是 create，
没有改动这条既有语义（§8-⑦）。

### 4.5 M5 全轮打印（loop.py:368-383）

`_run_turn` 进入前记 `turn_start = len(history)`（:371），turn 结束后对
`history[turn_start:]` 切片遍历，打印**本轮所有** assistant 的 text 块（:373-383，
dict/对象双形态取值）。对齐 s15 `print_turn_assistants`（L3227-3233），替换旧版
"只打印末条 assistant"——旧法在续打/降级多消息轮会漏打或重打（注释 :369-370）。

## 5. 异步 turn 的 fail-closed 审批

**问题**：run() 的 wake/bg/cron 分支驱动的 turn 与 stdin 读线程并发。若这类
异步轮命中"需要用户确认"的规则去 `input()`，就会与前台抢控制台（前台此刻可能
正等用户输入）。s15 用 `skip_approval` 显式旗标贯穿全链（对 s15 L1772-1774，
但 s15 是 `threading.current_thread()` 判定线程身份，lcc 改为显式透传，§8 追加注记）。

### 5.1 透传链

| 环节 | 位置 | 行为 |
|---|---|---|
| 事件源标记 | `loop.py:335-365` | `wait_for_cli_event` 返回三元组 `(kind, payload, interactive)`；仅 user 为 True（注释 :336-337） |
| run() 分支 | `loop.py:400/408/411/417` | user→`skip_approval=False`；wake/bg/cron→True |
| _run_turn | `loop.py:368-372` | 形参透传给 agent_loop |
| agent_loop / inner | `loop.py:168-175` | 形参透传 |
| execute_tool 调用 | `loop.py:298` | `execute_tool(block, pool_handlers, skip_approval=skip_approval)` |
| execute_tool 本体 | `tools_manager.py:372-377` | 透传给 `trigger_hooks("PreToolUse", block, skip_approval=...)`（注释 :374） |
| 钩子总线 | `hooks.py:32-43` | 仅对 permission_hook 特判注入该关键字（:37-40），其余钩子不消费（注释 :30） |
| permission_hook | `hooks.py:45-46` | 透传给 check_permission |
| check_permission | `permission.py:83-122` | 终判点（§5.2/§5.3） |

### 5.2 拒绝文案（permission.py:80-81）

```
ASYNC_APPROVAL_DENIED = ("Permission denied: interactive approval is unavailable "
                         "during an asynchronous turn")
```

逐字固定串，作为 tool_result 文本回流给模型。s15 原文案含 "interactive **shell**
approval"（L1773），lcc 去掉 shell 一词通用化——异步轮里被拒的不止 bash（§8-②）。

### 5.3 三段权限序（permission.py:83-122）

```
check_permission(block, prompt_user=True, skip_approval=False)
├─ ① bash 硬 deny 列表 —— 先行，skip_approval 也放行不了        （:85-89）
├─ ② check_rules 命中：
│     skip_approval=True → log_error + return ASYNC_APPROVAL_DENIED（:93-95，绝不 input()）
│     否则 prompt_user=False → "Permission required: ..."（队友 Lane D，:96-98）
│     否则交互式 ask_user [Y/N] → deny 则 "Permission denied by user"（:99-102）
└─ ③ MCP 策略段（mcp__ 前缀，policy != allow 时同样三段式）      （:104-121）
      skip_approval=True → ASYNC_APPROVAL_DENIED（:112-114）
      否则 prompt_user=False → "Permission required: ..."（:115-117）
      否则 ask_user [Y/N]（:118-121）
```

优先级：**硬 deny > skip_approval > prompt_user**（docstring :78-79）。
`skip_approval` 命中即拒不弹 input ——防的就是与 stdin 读线程抢控制台；
MCP 策略非 allow 一律要确认的 lcc 既有规则不变（沿用 s14 fail-closed，§8-⑪）。

## 6. 后台任务集成：四层胶水（对 s15 L2261-2339/L3243）

### 6.1 bg 第四唤醒源（loop.py:348-351）

`wait_for_cli_event` 在 inbox→cron 之后、stdin 之前插查
`backgroundTasksManager.has_pending()`（`background_tasks_manager.py:130-135`，
对照 s15 `has_pending_background` L2335-2339）——**只查不清零**，收割仍归
`_agent_loop_inner` 圈首的 `inject_background_results`（`loop.py:187`）。
命中返回 `("bg", None, False)`，run() 走 bg 分支
`_run_turn(history, "", skip_approval=True)`（:409-411）：不注入用户消息、
空 prompt 全靠圈首收割（注释 :410）。这就消除了"终轮陷阱"——任务一完成，
主循环 0.25s 级轮询内就会被叫醒收割。

### 6.2 派发时刻 cwd 解析 + 透传（tools_manager.py:380-386）

后台分支进门先 `cwd, cwd_error = self._agent_cwd()`（:382，定义 :559；解析 Lead
团队租约工作目录）。**租约失效（error）直接在派发点 return 错误串、不启动任务**
（:383-384，同 `run_agent_*` 的 `error or ...` 闸门语义；s15 是把 cwd_error 延后
到 worker 内 raise，L2270-2271——§8-⑧）。cwd 随
`start_background_task(block, cwd=cwd)`（:386 → `background_tasks_manager.py:140-141`）
进入 `start(block, cwd=None)`（:30-58），记入 task 的 `"cwd"` 字段
（:41-47，注释 :45 对照 s15 L2299），线程 args 透传给 `run`（:49-50），
最终到 `run_bash_process(command, cwd)`（:64 → :145-182），
`Popen(cwd=cwd or env.workDirPath)`（:156）——缺省回落工作区根。

### 6.3 worker 完成触发 PostToolUse（background_tasks_manager.py:77-88）

`ToolsManager.__init__` 组装根注入
`hooks_trigger=lambda event, block, output: self.hooks.trigger_hooks(event, block, output)`
（`tools_manager.py:262-266`，注释 :262-263 明示**后台线程触发须钩子线程安全**；
模块头注释 `background_tasks_manager.py:12-15` 同记此约定与对照 s15 L2280-2284）。
worker 拿到 result 后、置 status 前触发（:77-79）：

- 钩子返回非 None → 视为拦截文案，`f"{intercepted}\n{result}"` 前缀进 result（:87-88）；
- 钩子自身抛异常 → `[hook error] {类型}: {消息}\n{result}` 前缀 + status 降级
  `failed`（:83-85）；
- `hooksTrigger=None`（未接线）跳过钩子层，向后兼容（:26-27）。

与派发侧的呼应：`execute_tool` 后台分支**就地 return**（`tools_manager.py:396-398`），
不再走前台的 `trigger_hooks("PostToolUse", ...)`（:414）——避免同一 block 双触发
（注释 :396-397 对照 s15 L2280）。

### 6.4 通知格式收窄（background_tasks_manager.py:99-125）

`collect()` 摘要从旧版 `<result>` 前 500 字符收窄为 `<summary>` 前 200
（:113-115，注释对照 s15 L2324-2331；failed 与 completed 共用模板，由 `<status>`
区分），模板 :116-123：

```xml
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <command>…原始命令…</command>
  <summary>…结果前 200 字…</summary>
</task_notification>
```

派发侧占位文案同步更新（`tools_manager.py:387-390`）：

```
[Background task bg_0001 started] Result will arrive as a task_notification.
```

启动异常兜底（:391-395）：`Error: Failed to start background task: {类型}: {消息}`。

## 7. env.py 新键：FALLBACK_MODEL_ID

`fallbackModelId = os.getenv("FALLBACK_MODEL_ID") or None`（`env.py:12-13`）。
缺省 None——529 换模分支（`recovery.py:72`）的条件是"计数达 2 **且**
`state.fallbackModel` 非空"，不配置就永远只退避不换模。配置后经
`RecoveryState(self.env.modelId, self.env.fallbackModelId)`（`loop.py:180`）进入
恢复层。`.env` 中加一行即可启用，无任何代码改动点。

## 8. 与 s15 参考实现的有意差异

| # | 差异 | s15（code.py） | lcc | 动机 |
|---|---|---|---|---|
| ① | 事件模型 | 双轨：主线程 CLI + `async_event_loop` 线程（L3236-3263），`agent_lock` 串行化（L3239），1s 轮询 | 单主线程轮询 `wait_for_cli_event`（inbox→cron→bg→stdin），0.25s | lcc 既有模型，三源+bg 补齐即等价；退避 sleep 最长阻塞事件循环 ~32s——s15 双轨在 agent_lock 下同样串行阻塞（`loop.py:215-217` 注释明记权衡） |
| ② | 异步拒绝文案 | "interactive **shell** approval"（L1773） | 去掉 shell 一词（`permission.py:80-81`） | 异步轮被拒的不止 bash（写文件/MCP 同样命中） |
| ③ | 异常分类 | 依赖 anthropic SDK 类型名 | duck 判定类名+消息小写（`recovery.py:56-59`） | recovery.py 保持零 SDK 依赖叶子；lcc 还扩展了 "rate limit"/"rate_limit" 消息子串与 timeout 分支（:65-69，s15 无） |
| ④ | RecoveryState | 5 字段 snake_case、全局 PRIMARY_MODEL（L2193-2199） | 7 字段 camelCase、initialModel/fallbackModel 参数化注入（`recovery.py:31-38`） | 随 lcc 全库 camelCase 与"配置经构造注入"习惯；换模目标来自 env 而非硬编码 |
| ⑤ | 被动压缩限额 | `state` 上一次性旗标（L3137-3140） | 同款旗标 `hasAttemptedReactiveCompact`（`loop.py:230-232`），替换旧 lcc 的 `MAX_REACTIVE_RETRIES=1` 类常量计数 | 状态归一进 RecoveryState，随 turn 生命周期自然作废 |
| ⑥ | 重试耗尽/错误路径 | L3141-3145 降级 append [Error] 后 return | 同语义（`loop.py:237-246`）；旧 lcc 的 `raise` 上抛移除，但 run() 的 `except KeyboardInterrupt` 兜底保留（`loop.py:420-421`） | CLI 存活优先；不吞用户 Ctrl+C |
| ⑦ | 发现期错误 | 无 MCP 组装 | `assemble_pool` 独立 try 终止本轮（`loop.py:190-206`）原样保留 s14 语义 | s15 不动既有接缝：发现期≠调用期≠API 期，三类异常三套处理 |
| ⑧ | bg 租约 cwd 失效 | worker 内延后 raise（L2270-2271） | 派发点直接 return 错误串、不启动任务（`tools_manager.py:382-384`） | 失效状态不必起线程；与 `run_agent_*` 闸门语义统一 |
| ⑨ | 钩子拦截值 | `trigger_hooks` 返回值丢弃（L2280）；钩子异常文案 "Error: PostToolUse hook failed:"（L2282-2283） | 拦截文案前缀进 result（`background_tasks_manager.py:87-88`）；异常降级 `[hook error] ...` + status=failed（:83-85） | 拦截不反馈给模型等于没拦截 |
| ⑩ | 重试耗尽上抛 | `RuntimeError("Max retries exceeded.")`（L2234） | `raise lastExc`——最后一次原始异常（`recovery.py:81`） | 降级文案里保留真实类型/消息（`loop.py:241`），排障不隔靴 |
| ⑪ | MCP 权限段 | 无 MCP | 非 allow 一律确认、缺省 confirm 的规则沿用 s14（`permission.py:104-121`） | s15 只做"异步轮不抢控制台"的最小插入（:112-114），不改策略语义 |

**悬空风险注记（s15 既有取舍，lcc 未额外规避）**：§4.2 第二拍"已升档仍截断"会
append 半截 assistant 再续打——若半截 content 以**未闭合的 tool_use** 结尾，
后续再 append 一条 assistant/user 序列在严格校验的 API 上理论非法（tool_use 无
配对 tool_result）。s15 未处理（L3155-3160），lcc 逐段移植保持一致，如实记录。

## 9. 已知坑与设计注记

1. **withRetry 的 `maxRetries<=0` 边界**：循环一次不跑，落到 `raise lastExc`
   而 `lastExc` 仍是 None → `TypeError: exception: must be derived from
   BaseException`（`recovery.py:50/81`）。现调用点全走默认 3（`loop.py:218-226`
   未传该参），不触发；改签名默认值前先补这个洞。
2. **`s12>>` 提示符重打**：`prompt_visible` 是 `wait_for_cli_event` 的**局部变量**
   （`loop.py:338`），每次进入重打一次提示符——wake/bg/cron 循环高频时提示符会
   刷屏。HEAD 既有行为，s15 未动，记录在案。
3. **GBK 控制台**：拒绝/通知/日志均为 UTF-8 文本，Windows GBK 控制台打印不可编码
   内容直接 `UnicodeEncodeError`（如 `[Error]` 降级串含中文消息时）。手工测试脚本
   需 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。
4. **check_deny_list 冗余 f-string**：`permission.py:49` 的
   `f"Permission denied by deny list"` 无占位符，f 前缀多余——风格噪音，语义无害。

## 10. 快速上手

前提：`python loop.py` 起 CLI。观察点统一看 `log.py` 的 tag：`recovery` / `bg` / `permission`。

**剧本 ①——429 退避重试（无感知自愈）**
端点限流时模型调用被 withRetry 吞掉重试：控制台出现
`[recovery] [429] retry 1/3 after 0.6s` → 第 2/3 次内成功则本轮照常继续，模型
与用户都不感知。3 次全败 → 末条 assistant `[Error] RateLimitError: ...`，
CLI 存活，可继续下一句输入（§4.3）。

**剧本 ②——连续 529 换 fallback**
`.env` 配 `FALLBACK_MODEL_ID=<备用模型>`。端点持续过载：两次连续 529 后出现
`[recovery] 切换 fallback 模型: <备用>`（`recovery.py:72-75`），本 turn 后续 create
全部改用 `state.currentModel`（`loop.py:220`）。不配置该键则只退避、永不换模（§7）。

**剧本 ③——max_tokens 截断升档续打**
让模型生成超长输出撞截断：第一拍 `[recovery] [max_tokens] 升档重打
max_tokens=16000`（半截内容**不**入账）；仍截断则逐拍
`[max_tokens] 续打 1/2`、`2/2`，用 CONTINUATION_PROMPT 顶着继续写；耗尽则
`[max_tokens] 续打耗尽，收尾返回`，本轮以半截答复正常结束（§4.2）。

**剧本 ④——后台完成即唤醒**
说"后台跑 `sleep 5`"→ 模型 `bash(run_in_background=true)` → 立即回占位
`[Background task bg_0001 started] Result will arrive as a task_notification.`
→ 该轮收尾后主循环不退出：约 0.25s 粒度轮询到 `has_pending` → `("bg", ...)` →
异步 turn 圈首 inject 收割，注入 `<task_notification>…<summary>…</summary>` →
模型向用户转述结果。**全程无 input() 抢占**：若该异步轮再命中确认规则，直接
回流拒绝串 `Permission denied: interactive approval is unavailable during an
asynchronous turn`（§5）。

**剧本 ⑤——租约失效拒绝派发**
Lead 团队租约目录被外部删除后再起后台任务：`execute_tool` 在派发点直接回
`_agent_cwd` 的错误串（`tools_manager.py:382-384`），**不产生** bg task 条目、
不起线程（区别于 s15 的 worker 内延后报错）。

**加 fallback 模型的操作面**：只动 `.env`（§7）；无代码同步点。
**改钩子拦截语义注意**：worker 侧 PostToolUse 跑在后台线程，注册的钩子必须
线程安全（`tools_manager.py:262-263` 注释的组装根约定）。

## 11. 关联文档与接口索引

| 文档 | 关系 |
|---|---|
| [BACKGROUND_TASKS_MANAGER.md](BACKGROUND_TASKS_MANAGER.md) | 后台引擎本体；本文 §6 是其 s15 集成增量（cwd 透传/worker 钩子/bg 唤醒源/`<summary>` 收窄） |
| [TOOLS_MANAGER.md](TOOLS_MANAGER.md) | `execute_tool` 闸门全景；本文 §5/§6.2 补 skip_approval 透传与 bg 派发分支现状 |
| [PERMISSION.md](PERMISSION.md) | 权限三段序本体；本文 §5 补 skip_approval 优先级与新拒绝文案 |
| [COMPACT_MANAGER.md](COMPACT_MANAGER.md) | reactive compact 本体；本文 §4.3 的一次性旗标驱动其触发时机 |
| [MCP_TOOLS.md](MCP_TOOLS.md) | MCP 策略确认链；本文 §5.3 的 ③ 段叠加 skip_approval 后的行为 |
| [HOOKS.md](HOOKS.md) | 总线与 skip_permission/skip_approval 双关键字（`hooks.py:27-43`） |
| [ENV.md](ENV.md) | `FALLBACK_MODEL_ID` 新键（本文 §7） |

### 关键接口速查

| 模块 | 接口 |
|---|---|
| `recovery.py` | 常量 :17-24 · `RecoveryState` :27-38 · `retryDelay` :41-44 · `withRetry` :47-81 · `isPromptTooLong` :84-88 |
| `loop.py` | `RecoveryState` 构造 :180-181 · 发现期 try :190-206 · withRetry 包 create :214-227 · 压缩/降级 :228-246 · max_tokens :248-269 · `execute_tool` 透传 :298 · `wait_for_cli_event` :335-365（bg 源 :348-351）· `_run_turn` M5 :368-383 · `run` 分支 :385-430（skip_approval=True 于 :408/:411/:417；KeyboardInterrupt :420-421） |
| `tools_manager.py` | `hooks_trigger` 注入 :262-266 · `execute_tool(..., skip_approval)` :372-415 · cwd 闸门 :382-384 · 占位/异常文案 :387-395 · bg 就地闭合 :396-398 · 前台 PostToolUse :414 · `_agent_cwd` :559 |
| `background_tasks_manager.py` | 线程安全约定注释 :12-15 · `start(block, cwd)` :30-58（task cwd :41-47）· `run` worker :61-97（钩子段 :77-88）· `collect` `<summary>` :99-125 · `has_pending` :130-135 · `run_bash_process(cwd)` :145-182 |
| `permission.py` | `ASYNC_APPROVAL_DENIED` :80-81 · `check_permission(..., skip_approval)` :83-122（规则段 :91-102 · MCP 段 :104-121） |
| `hooks.py` | `trigger_hooks(..., skip_approval)` :32-43 · `permission_hook` :45-46 |
| `env.py` | `fallbackModelId` :12-13 |

> 表尾注：本文对应未提交工作区——`M background_tasks_manager.py / env.py / hooks.py /
> log.py / loop.py / permission.py / tools_manager.py` + 新增 `recovery.py`，
> 基线 HEAD `1ea152e`。

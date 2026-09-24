# CronScheduler 技术文档

> 对应源码：`cron_scheduler.py`（本仓库当前版本 394 行）
> 状态：完整，**已接入主循环与工具链**（三个 cron 工具 + 主循环注入闭环；at-least-once 投递时序收口在本模块 `run_delivery`，主循环只提供投递回调，见 §7/§8）

## 1. 它解决什么问题

"到点把一段 prompt 交给 agent 执行"：注册 5 字段 cron 表达式任务，后台线程
按秒轮询，命中的任务进入待投递队列，主循环在两次对话回合之间把队列内容注入
会话历史。带崩溃可恢复的落盘持久化（durable 任务）。

与 `background_tasks_manager.py` 方向相反：后台任务是"现在发起、异步执行、
回来收割"，定时任务是"到点发起、注入 prompt、由主循环驱动模型"。

## 2. 数据结构

```python
@dataclass
class CronJob:
    id: str                         # "cron_" + secrets.token_hex(4)，8 位十六进制
    cron: str                       # 5 字段表达式
    prompt: str                     # 到点注入的任务内容
    recurring: bool                 # 循环 / 一次性
    durable: bool                   # 是否落盘
    pending_delivery: bool = False  # 已触发、尚未被 ack
    last_fired: str | None = None   # 最后触发时刻，"%Y-%m-%d %H:%M"（分钟粒度）
```

调度器实例状态（`__init__`，cron_scheduler.py:21-31）：

| 字段 | 说明 |
|---|---|
| `env = Env()` | 自建 Env 实例（:22），只为取 `durablePath`——与 loop/tools_manager 各自的 Env **不是同一实例** |
| `scheduled_jobs: dict[str, CronJob]` | 注册表（:23） |
| `cron_queue: list[CronJob]` | 待投递队列（:24），存放已触发未 ack 的 job（与注册表共享同一对象引用） |
| `cron_lock = threading.RLock()` | 保护上述两者 + 落盘（:26）；注释点明用 RLock 的原因：`save_durable_jobs` 在持有 `cron_lock` 时被调用，普通 Lock 会自死锁 |
| `runtime_stop: Event` / `runtime_started: bool` / `scheduler_loop_thread` | 后台轮询线程的生命周期控制（:28、:29、:31） |

## 3. 对外接口清单

公开方法（`_` 前缀的内部方法见 §4、§5）：

| 方法 | 参数 | 返回 | 副作用 |
|---|---|---|---|
| `validate_cron(cron_expr)` | 表达式字符串 | 错误消息 `str` 或 `None` | 无 |
| `schedule_job(cron, prompt, recurring=True, durable=True)` | — | 成功 `CronJob`；失败**错误字符串**（`CronJob \| str` 判别式返回，:204-229） | 注册表插入；durable 时 `save_durable_jobs`（失败则回滚删除并**向上抛**）；print `[cron] scheduled` |
| `cancel_job(job_id)` | — | `"Cancelled {id}"` 或 `"Job {id} not found"`（:231-252） | 从注册表和队列移除；durable 时落盘（失败回滚）；print `[cron] cancelled` |
| `new_cron_id()` | — | `"cron_xxxxxxxx"`；100 次撞车则 `ValueError`（:197-202） | 无（查重 `scheduled_jobs`，调用方已持锁） |
| `poll_due_jobs(moment)` | 判定时刻 | — | 命中的 job 经 `_enqueue_due_job` 入队并落盘；print `[cron] due`（:272-284） |
| `consume_cron_queue()` | — | 队列快照 list，**并清空队列**（:286-290） | 无落盘 |
| `acknowledge_cron_jobs(jobs)` | 已交付列表 | — | recurring → `pending_delivery=False`；one-shot → 从注册表**删除**；有 durable 变化则落盘，失败回滚（:292-333） |
| `restore_cron_jobs(jobs)` | 交付失败的列表 | — | 重新置 `pending_delivery=True` 并在不在队首时重入队；**不落盘**（:335-349，磁盘上 fire 时已写入 pending=True） |
| `has_cron_queue()` | — | `bool`（:351-353） | 无 |
| `save_durable_jobs()` / `load_durable_jobs()` | — | — | 见 §6 |
| `cron_scheduler_loop()` | — | — | 轮询主循环，见 §5 |
| `start_runtime_threads()` | — | — | 幂等（`runtime_started` 守卫，:359-367）：先 `load_durable_jobs()`，再起 daemon 轮询线程 |
| `stop_runtime_threads()` | — | — | `runtime_stop.set()` + `join(timeout=1)`（:369-375） |
| `list_cron_jobs()` | — | 注册表快照 `list[CronJob]`，每次调用新列表（元素为原 job 引用）（:377-380） | 无——外部读注册表的**唯一公开入口**，调用方不再接触 `cron_lock`/`scheduled_jobs` |
| `run_delivery(deliver)` | 投递回调 `deliver(fired)`（收 `consume_cron_queue` 返回的整批 job） | 本批大小 `int`；空批返回 `0` | at-least-once 协议的**唯一执行者**（:382-394）：`consume_cron_queue`（:385）→ 空批直接返回、不调回调（:386-387）→ 回调 `deliver(fired)`（:389）成功 → `acknowledge_cron_jobs`（:393）；回调抛**任何** `BaseException` → `restore_cron_jobs`（:391）后原样 `raise`（:392），绝不静默丢批 |

## 4. cron 表达式语义

字段顺序 `分 时 日 月 星期`，恰好 5 段否则不匹配/校验失败（:59-62、:126-128）。
支持的单字段形态（`_cron_field_matches` :39-56，`_validate_cron_field` :89-123）：

| 形态 | 匹配 | 校验 |
|---|---|---|
| `*` | 恒真 | — |
| `*/N` | `value % N == 0` | 仅要求 N 为正整数，**不校验 N ≤ 字段上限** |
| `a,b,c` | 任一子项命中 | 子项递归校验（逗号列表无命中时返回 False，:46-50） |
| `a-b` | 闭区间 | `a≤b` 且落在字段范围内 |
| `n` | 精确相等 | 落在字段范围内 |

校验范围表 `field_rules`（:130-136）：minute 0-59、hour 0-23、day-of-month 1-31、
month 1-12、**day-of-week 0-6**。

两个星期制换算与一条经典语义：

- Python `weekday()` 周一=0，cron 惯例周日=0，故 `cron_weekday = (moment.weekday() + 1) % 7`（:66）；不支持 `7` 表示周日；
- 日/星期字段沿用 vixie-cron 语义（:78-87）：两字段都受限时按 **OR**（任一命中即触发），任一为 `*` 时按另一个字段 AND。

不支持的形态：步长区间 `1-10/2`（`"10/2".isdigit()` 为假 → `Invalid range`）、
大小写名称（`MON`）、秒字段。

## 5. 调度模型与时间源

```python
def cron_scheduler_loop(self):                       # :355-357
    while not self.runtime_stop.wait(1.0):
        self.poll_due_jobs(datetime.now())
```

- **时间源是进程本地时钟** `datetime.now()`，无时区概念，无 cron 的秒/年字段；
- daemon 线程每 1 秒 poll 一次（`wait(1.0)` 同时充当停止信号与间隔休眠），
  一分钟内最多约 60 次判定，靠 `minute_marker = moment.strftime("%Y-%m-%d %H:%M")`
  去重：`pending_delivery` 已置位或 `last_fired == minute_marker` 即跳过（:273、:277）；
- **不补跑**：进程停机期间错过的分钟永久丢失；同一分钟内重启不会二次触发
  （`last_fired` 对 durable 任务已落盘）；
- `poll_due_jobs` 整体持 `cron_lock`（:274），逐 job try/except——某个 job 落盘失败
  只打日志 `[cron] could not enqueue`，不影响其余 job（:283-284）；
- 触发入队的原子序（`_enqueue_due_job` :254-270）：先置 `pending_delivery=True` 与
  `last_fired` → durable 则落盘（失败回滚两个字段并抛）→ **最后**才 `cron_queue.append`。
  磁盘状态先行于内存队列，保证"落盘成功但进程立死"时重启能恢复投递。

## 6. 任务存储：格式与落盘位置

- 路径唯一来源 `env.durablePath`（env.py:24）= **`<cwd>/.lcc/scheduled_tasks.json`**；
- 格式：JSON 数组，仅 `durable=True` 的 job 全量快照，`asdict(job)` 逐字段
  （含 `pending_delivery`、`last_fired`），`indent=4`；每次保存**整体重写**（非追加）；
- 原子写（:150-155）：临时文件名 `<name>.<pid>.<tid>.tmp` → 写 → `os.replace`，
  `finally` 中 `unlink(missing_ok=True)` 清残留；同分区 rename 使读者永不见半截文件；
- 加载（`load_durable_jobs` :158-194）：文件缺失静默返回；坏 JSON / 非数组 →
  打印 `[cron] could not load ...` 后放弃整个文件；逐条容错——`CronJob(**item)`
  字段不符、cron 校验失败、`id` 不以 `cron_` 开头、prompt 为空，都打印
  `[cron] skipped invalid saved job` 跳过**该条**，其余照常装载；
- 装载时 `pending_delivery=True` 的 job 直接回灌 `cron_queue`（:190-191）——
  上一进程"已触发未确认"的任务重启后补投；
- `durable=False` 的 session 任务只存在于内存，进程退出即消失。

## 7. 投递协议：run_delivery（consume → 回调 → ack / restore）

**at-least-once** 时序由 `run_delivery`（:382-394）在调度器内部强制执行，调用方
（主循环，见 §8）只提供一个投递回调，拿不到也破坏不了协议骨架：

```
run_delivery(deliver):                  # cron_scheduler.py:382-394
    fired = consume_cron_queue()        # 整队取出并清空（:385，实现 :286-290）
    fired 为空 → return 0，不调回调      # :386-387
    try:
        deliver(fired)                  # :389 回调注入 history 并驱动 agent 回合
    except BaseException:               # :390 连 KeyboardInterrupt 也接住
        restore_cron_jobs(fired)        # :391 重新置 pending=True 并回灌队列（:335-349）
        raise                           # :392 原样上抛，绝不静默丢批
    acknowledge_cron_jobs(fired)        # :393（实现 :292-333）
    return len(fired)                   # :394
```

ack 内部（:292-333）：

```
    ├─ recurring → pending_delivery=False（等下一个匹配分钟再 fire）
    ├─ one-shot  → 从 scheduled_jobs 删除（删除动作在 ack，不在 consume）
    └─ durable 有变化 → 落盘；落盘失败 → 恢复被删 job、恢复 pending 标志、
                       把不在队列中的 job 重新入队，再抛（:319-333）
```

- 队列防重入靠 `pending_delivery`：true 期间 poll 不会再入队（:277）、
  restore/ack 回滚时先查 `queued_ids` 再 append（:326-332、:337-349）——
  同一 job 在队列中至多一份；
- **重复执行的窗口**：consume 与 ack 之间进程被杀 → 磁盘上仍是
  `pending_delivery=True` → 重启回灌队列 → 同一 prompt 再执行一次。这是
  at-least-once 的既定代价，无幂等去重；
- 交付粒度是**回合边界**：调度线程照常入队，但 `run_delivery` 只在 `run()` 的
  `wait_for_cli_event` 返回 `"cron"` 之后才被调（loop.py:255）——模型正在跑长回合
  时任务在队列里等待。

## 8. 接线现状（必读）

**持有方**：ToolsManager 单实例（tools_manager.py:15 `from cron_scheduler import CronScheduler`、
:258 `self.cronScheduler = CronScheduler()`）。

**三个工具**（schema :211-241，注册进主循环 `tools` 列表 :276-278，handler 映射
:295-297，实现 :656-678）：

| 工具 | handler | 转调 | 返回 |
|---|---|---|---|
| `schedule_cron` | `run_schedule_cron(cron, prompt, recurring=True, durable=True)` | `schedule_job` | 错误串包装成 `Error: {...}`；成功 `Scheduled {id}: {cron} -> {prompt}` |
| `list_crons` | `run_list_crons()` | 调 `list_cron_jobs()` 取注册表快照（调度器内部持锁） | 每行 `{id}: {cron} -> {prompt[:60]} [recurring/one-shot, durable/session]` |
| `cancel_cron` | `run_cancel_cron(job_id)` | `cancel_job` | 原样透传 |

`subTools`（:299-312）**不含** cron 三件套——子代理不能排/查/撤定时任务，
只有主 agent 可以。

**主循环**（loop.py；`run()` 只管事件循环与"怎么投递"，"何时 ack/restore"收口在
`run_delivery`。cron 句柄经 `Loop.__init__` 的单跳别名 `self.cron =
self.toolsManager.cronScheduler`（loop.py:29）取得，此后 loop 不再二跳 toolsManager）：

```
loop.py:238  self.cron.start_runtime_threads()        # 启动时：load 落盘任务 + 起轮询线程
loop.py:209-210  self.cron.has_cron_queue() → 返回 ("cron", None)   # 事件等待：cron 优先于用户输入
loop.py:255  self.cron.run_delivery(deliver)          # 投递唯一入口；deliver 闭包 :250-254：
             #   :252  history.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
             #   :253  print [cron] delivered {id}
             #   :254  _run_turn(history, "\n".join(job.prompt))  # payload 是无前缀原文
             #   协议内部：consume/ack/restore 全在 cron_scheduler.py:385/393/391，loop 不可见
loop.py:227-233  _run_turn → agent_loop(history, payload)   # user 分支（:248）与 cron 分支共用；
             #   payload 仅作 active_request（压缩摘要用），不重复入 history
loop.py:256  self.cron.stop_runtime_threads()         # 退出时
```

`wait_for_cli_event` 的优先级细节（loop.py:206-224）：每轮先查 cron 队列，非空立即返回
`"cron"`（**不打印 `s12>>` 提示符**、不等 stdin）；用户输入此刻还躺在
`stdinQueue` 里，下一个事件循环再取。即 cron 可以插队在先输入的用户消息之前。

## 9. 不变量（改代码前必读）

1. **错误即值，不是异常**：`schedule_job` 用返回类型 `CronJob | str`、
   `validate_cron` 用 `str | None` 表达失败——调用方必须做类型判别
   （`run_schedule_cron` :658 用 `isinstance(result, str)`），改成抛异常会同时
   打坏工具层和 `load_durable_jobs` 的复用逻辑；
2. **业务失败不抛，持久化失败才抛**：schedule/cancel/ack/enqueue 的落盘异常都先
   回滚内存再 `raise`——注意这条异常会穿透 `execute_tool`（无 try）直达
   `agent_loop` 乃至 `run()`，主循环对 cron 工具**没有**局部兜底；
3. `cron_lock` 必须是 **RLock**（:25-26 注释）：所有公开方法持锁 →
   `save_durable_jobs` 嵌套持锁；
4. 队列元素与注册表元素是**同一个 CronJob 对象引用**（非拷贝）：改
   `pending_delivery`/`last_fired` 两边同步生效，`consume` 返回的快照在 ack 前
   不要脱离锁去改字段；
5. `id` 前缀 `cron_` 是加载校验的一部分（:179）：换 ID 方案必须同步改
   `new_cron_id` 与 `load_durable_jobs` 两处；
6. 磁盘状态先行于内存队列（§5 触发原子序）：任何重排都破坏崩溃恢复语义。

## 10. 已知坑点与边界

- `cancel_job` 在 `for queued in self.cron_queue` 迭代中 `remove`（:240-242）——
  靠"同一 id 队列至多一份"的现实不变量才没有踩到边迭代边删的跳过问题；
- 同函数的落盘失败回滚用 `self.cron_queue.extend(previous_queue)`（:249）：
  `previous_queue` 是**删除前整队快照**，回滚会把队列里其他 job 也复制一份——
  仅在 `save_durable_jobs` 抛出的低概率路径上触发；
- `*/N` 不校验 N 与字段上限：`*/61`（分钟）能通过校验，匹配时
  `value % 61 == 0` 仅在 value=0 成立 → 实际**每小时第 0 分钟照响**，
  与"每 61 分钟"的直觉不符；
- `start_runtime_threads`/`stop_runtime_threads` 的幂等守卫是对 `runtime_started`
  的无锁裸读写（:360、:367、:370、:375），依赖"只在主线程调用一次"的约定，
  并非线程安全；
- `stop_runtime_threads` 的 `join(timeout=1)`：轮询线程一次 `poll_due_jobs`
  若被大量 durable 落盘拖过 1 秒，主线程会先走、daemon 线程随进程消亡；
- `prompt` 内容零校验（除非空）：注入 history 的是原文，长任务全量入会话，
  受 CompactManager 管辖但 cron 模块自身无截断/引用化机制；
- 仓库根目录现存一份游离的 `.scheduled_tasks.json`（一条 2026-09-23 的任务，
  untracked）：是 `.lcc/` 目录整合（commit 57febab）**之前**的遗留产物，
  当前代码只读写 `.lcc/scheduled_tasks.json`，该文件无人引用，且不在
  `.gitignore` 的排除模式内（同批遗留还有根目录 `.task_outputs/`、`.temp/`、
  `.transcripts/`）。

## 11. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `env.py` | 自建 `Env()` 实例，仅消费 `durablePath`（env.py:24，`<cwd>/.lcc/scheduled_tasks.json`；`.lcc` 目录由 Env 构造时 mkdir） |
| `tools_manager.py` | 持有唯一实例（:258）；`schedule_cron`/`list_crons`/`cancel_cron` 三个主 agent 工具的宿主 |
| `loop.py` | 生命周期（`self.cron.start/stop_runtime_threads`，loop.py:238/:256）与投递回调（`deliver` 闭包）的提供方；consume/ack/restore 时序由本模块 `run_delivery` 自行执行；cron 事件与用户输入共用同一事件循环 |
| `background_tasks_manager.py` | 概念对称但零耦合：那边是"发任务收结果"，这里是"到点发 prompt" |
| `hooks.py` | **不经过任何 hook**：cron 注入的 `[Scheduled]` user 消息不触发 `UserPromptSubmit`；但 cron 工具调用照常走唯一事件总线的 `PreToolUse`/`PostToolUse`（HOOKS.md §6） |
| `compact_manager.py` | 间接：cron 消息进入 history 后受常规压缩管辖 |

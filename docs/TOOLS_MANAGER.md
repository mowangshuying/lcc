# ToolsManager 技术文档

> 对应源码：`tools_manager.py`（本仓库当前版本 610 行）
> 状态：完整，**已接入主循环**（`loop.py:18` 构造、`loop.py:162` 执行；
> 子代理执行循环内置于本类 `run_subagent`；bash 后台任务委托 `backgroundTasksManager`，
> 详见 BACKGROUND_TASKS_MANAGER.md）

## 1. 它解决什么问题

把"工具"这件事的四块拼在一起：

1. **schema**（模型看到什么）：15 个工具的 JSON Schema 定义，详解见 §2；
2. **handler**（怎么执行）：`toolsHandlers` / `subToolsHandlers` 两张路由表；
3. **执行闸门**：`execute_tool` 统一走 PreToolUse → handler → PostToolUse；
4. **子代理**：`run_subagent` 自带一个缩小版的 agent 循环。

对外主入口只有两个：`tools`（喂给 `messages.create`）与
`execute_tool(block, handlers, allow_background=True)`；外加 `skills_catalog()` 供系统提示拼装。
其中 bash 的"后台执行"分支委托自持的 `backgroundTasksManager`（§5、§6.1），
完整机制见 BACKGROUND_TASKS_MANAGER.md。

## 2. 工具 schema 详解

### 2.1 schema 的结构与消费链路

每个工具是一个固定三键的 dict（15 个工具全部同一格式）：

```python
{
    "name": "edit_file",          # 工具标识：execute_tool 拿它查路由表的键
    "description": "...",         # 模型唯一能看到的"说明书"，决定何时、怎样调用
    "input_schema": {             # JSON Schema：描述参数包
        "type": "object",
        "properties": {...},      #   键 = handler 的 Python 形参名
        "required": [...],        #   必填参数名单（仅声明，见 §2.4）
    },
}
```

它的一生是一条闭环，每一环都靠"逐字同名"衔接：

```
类属性 dict ──登记──▶ self.tools ──JSON 序列化──▶ messages.create(tools=...)
                                                     │
模型产出 tool_use{name, input} ◀──读 name/description/input_schema──┘
        │
        ▼
handlers.get(block.name) ──▶ handler(**block.input)   # properties 键 ⇄ 形参名
```

三个键各司其职：`name` 决定执行落到哪个 handler，`input_schema.properties`
的键决定哪些形参能收到值，`description` 决定模型调不调、参数填得好不好。
§10 不变量 1/3 与 §11 坑点 1 都是从这条链推出来的。

### 2.2 逐字段注释示例：EDIT_FILE

```python
EDIT_FILE = {
    "name": "edit_file",           # 与 toolsHandlers 的键一致，execute_tool 按它路由
    "description": "Replace exact text in a file once.",
    # ↑ "once" 在声明"只替换第一处"语义——但模型未必遵守（§11 坑点 4）
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},        # → run_edit(path=...)
            "old_string": {"type": "string"},  # → run_edit(old_string=...)
            "new_string": {"type": "string"},  # → run_edit(new_string=...)
        },
        "required": ["path", "old_string", "new_string"],
        # 三个全必填、无默认值；模型漏任一个 → handler(**input) 缺形参
        # TypeError 炸穿主循环（§11 坑点 1）
    },
}
```

### 2.3 十五工具参数总表

| 工具 | 参数（类型） | 一句话语义 |
|---|---|---|
| `bash` | `command` string（必填）；`run_in_background` boolean（可选） | 执行 shell 命令（120 s 超时）；带 `run_in_background: true` 且允许后台时转异步（§5、BACKGROUND_TASKS_MANAGER.md） |
| `read_file` | `path` string（必填）；`limit` integer（可选） | 读文件；给了 limit 只取前 N 行 |
| `write_file` | `path` string（必填）；`content` string（必填） | 整写文件（自动建父目录） |
| `edit_file` | `path` `old_string` `new_string` string（均必填） | 精确匹配替换第一处 |
| `glob` | `pattern` string（必填） | 路径匹配，`**` 递归 |
| `todo_write` | `todos` array（必填；≤20 项；每项 `content` string minLength 1 + `status` enum pending/in_progress/completed，**项内不声明 required**） | 会话待办列表（事实来源=消息流，§6.4） |
| `task` | `prompt` string（必填，minLength 1） | fork 全新上下文子代理，只回最终文本 |
| `load_skill` | `name` string（必填） | 读取技能的 SKILL.md 全文 |
| `compact` | 无（`properties: {}`，连 `required` 键都没写） | 触发压缩；主循环按名字拦截（§8） |
| `create_task` | `subject` string（必填）；`description` string（可选）；`additionalProperties: False` | 建任务，返回运行时生成的 ID |
| `update_task` | `task_id` string（必填，`pattern ^task_[0-9a-f]{8}$`）；`addBlockedBy` array[string 同 pattern]（必填，minItems 1）；`additionalProperties: False` | 加依赖（禁自环禁成环，校验见 TASK_MANAGER.md §5.2） |
| `list_tasks` | 无（`properties: {}`） | 列全部任务（status/owner/依赖） |
| `get_task` | `task_id` string（必填） | 单任务全文 JSON |
| `claim_task` | `task_id` string（必填） | 认领 pending 且已解除依赖的任务 |
| `complete_task` | `task_id` string（必填） | 完成本代理已认领的任务 |

约束的不对称是现状（不是 bug，但改之前要心里有数）：

- `pattern` / `minItems` / `additionalProperties` 全在声明层——`execute_tool`
  对 `block.input` 零校验（§5）；ID 格式的真正执法者是 TaskManager 的
  `_path` 正则（TASK_MANAGER.md §3），不是 schema；
- `get/claim/complete_task` 的 `task_id` 连 pattern 都没声明，只有
  `update_task` 镜像了正则——同一道服务端防线，不同的声明密度；
- `todo_write` 的 items 内部不 required，而服务端 `update_todos` 的校验链
  更严（§6.4）——schema 比实现宽松。

### 2.4 schema 是软约束：命名要顺着模型先验

`input_schema` 序列化后只是提示词文本，**不是校验器**。模型吐什么参数名
由训练先验决定，少数派命名会被压过。本仓库有实证案底：

- 自 48327b7 拆分以来 `edit_file` 的属性名是 `old_text`/`new_text`，模型
  按先验高频产 `new_string` → `TypeError` 炸穿一轮；
- 7d78025 改名 `old_string`/`new_string` 对齐主流先验后症状消失
  （`git log -S old_text -- tools_manager.py` 可查证改名时点）。

由此两条设计纪律：

- **新工具参数名随主流先验**：`path`/`content`/`old_string` 这类公共语料
  里出现了几百万次的名字，模型输出才稳定；
- **schema 属性、handler 形参两处逐字同名**：`handler(**block.input)`
  的机制让名字不同名不是"软失败"而是当场 `TypeError`（§10 不变量 3 +
  §11 坑点 1）。`addBlockedBy` 是 15 工具里唯一的 camelCase 例外，
  同步时要格外小心（行为差异见 §6.6）。

### 2.5 格式纪律：schema 常量必须是 dict 字面量

`CREATE_TASK = {...},` 多写一个尾逗号，常量就成了**单元素 tuple**；序列化
后 `tools` 数组的元素是 `[{...}]`，网关直接 400
`Request body format invalid`——整个请求被拒，报错信息还指不到"逗号"
头上。7d78025 同日实测案底（TASK_MANAGER.md §9 有完整记录）。任何新工具
上线前先用 `all(isinstance(t, dict) for t in tools)` 过一遍。

### 2.6 可见性：主循环 15 个 vs 子代理 5 个

`subTools`（258-264）只收 `bash`/`read_file`/`write_file`/`edit_file`/`glob`
五个通用文件工具；其余 10 个（todo_write、task、load_skill、compact 与 6 个
任务依赖工具）主循环独享——防递归 fork、拦截语义只存在于主循环、`.lcc/task/`
看板归主代理（§10 不变量 5）。

子代理的 `bash` 用的是 `sub_bash_info()`（316-319）而非 `bash_info()`：`deepcopy`
主 `BASH` 后 `pop("run_in_background")`——**从 schema 层就不给子代理后台参数**，
与 §7 `run_subagent` 的 `allow_background=False`（554）构成双重禁令
（BACKGROUND_TASKS_MANAGER.md §9/§12）。

## 3. 工具注册总览

schema 本身与全部参数见 §2.3；这里只回答"谁路由到谁"：

| 工具 | 主循环 handler | 子代理可见/可执行 |
|---|---|---|
| `bash` | `run_bash` | ✓ / ✓（子代理 schema 为 `sub_bash_info`，**无** `run_in_background`） |
| `read_file` | `run_read` | ✓ / ✓ |
| `write_file` | `run_write` | ✓ / ✓ |
| `edit_file` | `run_edit` | ✓ / ✓ |
| `glob` | `run_glob` | ✓ / ✓ |
| `todo_write` | `run_todo_write` | ✗ |
| `task` | `run_subagent` | ✗（子代理不能再开孙代理） |
| `load_skill` | `run_load_skill` | ✗ |
| `compact` | **无 handler——主循环按名字拦截**（§8） | ✗ |
| `create_task` | `run_create_task`（§6.6） | ✗ |
| `update_task` | `run_update_task`（§6.6） | ✗ |
| `list_tasks` | `run_list_tasks`（§6.6） | ✗ |
| `get_task` | `run_get_task`（§6.6） | ✗ |
| `claim_task` | `run_claim_task`（§6.6） | ✗ |
| `complete_task` | `run_complete_task`（§6.6） | ✗ |

后 6 个是任务依赖工具（业务全在 `task_manager.py`，本类只作薄转发；行为
差异见 §6.6，细节见 TASK_MANAGER.md）。注意别把 `task`（子代理 fork，§7）
和 `create_task` 一系混为一谈。

注意两张表不对称：`self.tools` 有 15 个 schema，`toolsHandlers` 只有 14 个
handler——`compact` 仍是唯一"模型可见、但路由表查无此人"的工具。

## 4. 构造与依赖

`ToolsManager()` 无参构造（loop.py:18），内部自建全部依赖：

| 成员 | 来源 | 说明 |
|---|---|---|
| `env` | `Env()` | 第 2 个 Env 实例（ENV.md §4） |
| `subSystemPrompt` | 硬编码 | "coding agent at {workDir}...return a concise final answer" |
| `hooks` | `Hooks()` | **第 2 个 Hooks 实例**，Pre/PostToolUse 实际归它管（HOOKS.md §6） |
| `client` | `Anthropic(base_url=...)` | 仅子代理自用；主循环另有自己的 client（loop.py:17） |
| `skillManager` | `SkillManager(env.skillsDirPath)` | 构造即扫描技能目录（SKILL_MANAGER.md） |
| `taskManager` | `TaskManager(env.taskDirPath)` | 6 个任务依赖工具的共享实例（222，TASK_MANAGER.md） |
| `backgroundTasksManager` | `BackgroundTasksManager()` | 后台 bash 任务引擎（223）；**全库唯一实例**，`execute_tool` 的后台分支与 `run_bash` 前台共用它，`Loop` 经 `self.toolsManager` 复用它收结果（loop.py:72，BACKGROUND_TASKS_MANAGER.md） |
| 4 张列表 | 225-271 | `tools`(225-241) / `toolsHandlers`(242-257) / `subTools`(258-264) / `subToolsHandlers`(265-271) |

`MAX_SUBAGENT_TURNS = 50`（类常量，210 行）。全部依赖**不接受注入**，测试替身只能事后覆写属性。

## 5. execute_tool：唯一闸门入口（280-310）

```
execute_tool(block, handlers, allow_background=True)
├─ trigger PreToolUse → 非 None？ 直接 return str(blocked)   （281-283）
│     （handler 不执行、后台不启动、PostToolUse 也不触发——PERMISSION.md §6）
├─ allow_background 且 should_run_background(name, input)？   （285）
│     真（后台分支，仅主循环 bash + run_in_background=true）：
│        task_id = start_background_task(block)               （287）
│        output = "[Background task {id} started] ...later turn."（288-291）
│        启动异常 → output = "[Background task start error] {e}"（292-293）
│     假（前台分支）：
│        handler = handlers.get(name)
│           查无 → output = "Unknown:{name}"                  （299-300，仍走 PostToolUse）
│           有 handler →
│              tool_input = dict(block.input)                 （304）
│              pop("run_in_background", False) 且 allow_background=False
│                    → 打印降级提示，照常前台执行             （305-306）
│              output = handler(**tool_input)                 （307）
└─ trigger PostToolUse(block, output)（返回值丢弃）→ return str(output)（309-310）
```

关键语义：

- **后台门控三条件**（285 + `should_run_background`）：① `allow_background`（主循环默认
  True；`run_subagent` 传 False）② 工具名是 `bash` ③ `input.get("run_in_background") is
  True`（严格布尔）。全满足才转后台，立即回占位 tool_result，真结果由 `Loop` 后续轮收割
  （BACKGROUND_TASKS_MANAGER.md §9/§10）；
- **`run_in_background` 被"吸收"**：前台分支先 `dict(block.input)` 再
  `pop("run_in_background", False)`（304-305），使 `run_bash(command)` 这类形参里没有它的
  handler **不会**因这个键而 `TypeError`；不允许后台却带了它 → 打印
  `[background] not allowed in this context, running in foreground` 后前台执行（306）；
- **错误即数据**：handler 返回值（含 `Error:...`、占位 `[Background task …]` 文本）原样成为
  tool_result 喂回模型；handler 内部约定自吞异常（§6）；
- **裸调用仍在**（307）：除 `run_in_background` 外的其他 schema 外野参数、或漏 required
  参数 → `TypeError` **不被捕获**，一路炸穿主循环（§11.1）；
- 同一批多个 `tool_use` 逐个顺序执行（调用方 for 循环），无并行。

## 6. Handler 明细

### 6.1 run_bash（367-379）

- 自带 5 词危险子串黑名单：`rm -rf /`、`sudo`、`shutdown`、`reboot`、
  `> /dev/`——与 Permission 的 `DENY_LIST` **是两套独立清单**，内容有出入
  （此处 `> /dev/` 前缀更宽，但缺 `mkfs`/`dd if=`），命中时返回
  `Error: Dangerous command blocked`（不弹确认），详见 PERMISSION.md §7；
- 危险检查之后**委托** `backgroundTasksManager.run_bash_process(command)` +
  `format_bash_result(...)`（378-379）——与后台任务是**同一条执行路径**
  （BACKGROUND_TASKS_MANAGER.md §6）：`subprocess.Popen(shell=True,
  cwd=env.workDirPath, start_new_session=True, capture…)` +
  `communicate(timeout=120)`，阻塞最长 120 秒，无流式输出；
- stdout+stderr 合并 strip，**按字符截断前 50000**；空输出 → `(no output)`；
  退出码非 0 → `format_bash_result` 前缀 `Error: command exited with status N`；
- 捕获 `TimeoutExpired`（→ `Error: Timeout(120s)`）与泛 `Exception`（→
  `Error: {类型}: {e}`），二者退出码记 `None`；进程收尾走 `finally` 的
  `stop_process_group`（详见 BACKGROUND_TASKS_MANAGER.md §6/§8）。

### 6.2 文件三件套（全部经 `safe_path`）

| | 行为 | 返回 |
|---|---|---|
| `run_read`（388-395） | 整文件 `read_text` 后 `splitlines`；给了 `limit` 且小于总行数 → 前 limit 行 + `... (N more lines)` | 文件内容；**未给 limit 不截断** |
| `run_write`（398-405） | 先 `parent.mkdir(parents=True, exist_ok=True)` 再整写 | `Wrote N bytes to {path}` |
| `run_edit`（408-417） | 读全文 → `old_string` 必须存在否则报错 → `replace(old, new, 1)` **只替换第一处** | `Edited {path}` |

三者的 `except Exception` 把一切（含 `safe_path` 的 `ValueError`）转成
`Error:...` 文本；小瑕疵：`run_write` 的格式串是 `f"Error{e}"`，少了冒号。

### 6.3 run_glob（420-437）

- `glob.glob(pattern, root_dir=workDirPath, recursive=True)`；
- 每条命中单独过 `resolve + is_relative_to` 围栏——symlink/绝对 pattern
  指出的界外结果**静默剔除**（不问人、不报错）；
- `sorted` 稳定序，前 200 条，溢出追加
  `...(more matches omitted; narrow the pattern)`；空 → `(no matches)`。

### 6.4 todo_write（440-496、499-504）

`update_todos` 纯校验+渲染，**无任何持久化副作用**——todo 列表的唯一
事实来源就是模型消息流里这些格式化字符串：

- 字符串输入双解析：`json.loads` 失败 → `ast.literal_eval` 兜底 → 都败
  `raise ValueError`；
- 校验链：必须是 list、≤20 条、每条 dict、`content` 非空、`status` ∈
  {pending, in_progress, completed}（转小写后比）、**至多一条
  in_progress**；
- 渲染 `[ ] / [>] / [x]` + 末尾 `(done/total completed)`；空列表 → `No todos`。

外约：主循环连续 3 轮未调用 todo_write 时，往结果批里塞
`<reminder>Update your todos.</reminder>`（loop.py:174-183）——提醒逻辑在
loop 不在本类。

### 6.5 run_load_skill（565-566）

一行转发 `skillManager.load(name)`，无长度控制（SKILL_MANAGER.md §7）。

### 6.6 任务依赖工具（569-609，细节见 TASK_MANAGER.md）

6 个工具全部转发给 `taskManager`（222 行自建
`TaskManager(env.taskDirPath)`），本类只做薄包装：

| 工具 | 实现 |
|---|---|
| `create_task` | `run_create_task`（569-572） |
| `update_task` | `run_update_task`（574-578） |
| `list_tasks` | `run_list_tasks`（580-600） |
| `get_task` | `run_get_task`（602-603） |
| `claim_task` | `run_claim_task`（605-606） |
| `complete_task` | `run_complete_task`（608-609） |

schema 定义与登记方式与前 9 个工具完全同构（参数总表见 §2.3，三处登记见
§10 不变量 1），所以本节只记**行为差异**：

- 全是薄包装：`run_claim_task`/`run_complete_task` 写死
  `owner="agent"`（606、609）——没有第二方，认领即绑定；
- `run_update_task` 的形参名 `addBlockedBy` 是 15 工具里唯一的 camelCase
  （574），与 schema 属性名逐字一致（§10 不变量 3、§2.4），改哪头都得同步；
- `run_list_tasks`（580-600）渲染 `[ ]/[>]/[x]` + status + owner +
  blockedBy，观感刻意贴近 `update_todos`（§6.4），但事实来源是
   `.lcc/task/` 目录里的 JSON 文件，不是消息流；
- 6 个工具都**不在** `subTools`（258-264）——子代理碰不到任务体系，
  与 §10 不变量 5 的收窄原则一致。

## 7. task 与子代理（run_subagent，522-563）

```
messages = [{user: prompt}]                    ← 全新上下文，不带主对话历史
for _ in range(50):
    create(system=subSystemPrompt, tools=subTools, max_tokens=8000)
        API 异常 → return "Error: subagent API call failed: {e}"（整个子代理弃疗）
    append assistant 原文
    无 tool_use：
        trigger Stop（ ToolsManager 自己那份 hooks！）→ force 则续话，否则
        return extract_text(response.content)       ← 只回纯文本给主代理
    有 tool_use：
        逐个 execute_tool(block, subToolsHandlers, allow_background=False) → tool_result 批 → append
50 轮耗尽 → "Subagent stopped after 50 turns without a final answer."
```

- **一次性问答**：主代理只见最终文本，看不到子代理中间过程；
- 工具面收窄到 5 个（`subTools`，258-264，见 §2.6），因此拿不到
  task/load_skill/todo_write/6 个任务依赖工具（§6.6），
  也拿不到 compact 的 schema；
- 子代理的 bash schema 是 `sub_bash_info`（316-319，已删
  `run_in_background` 属性），且工具执行显式传
  `allow_background=False`（554）——schema 与路由双重禁止子代理起后台
  任务，误传参数会被吸收并降级前台执行（§5，
  BACKGROUND_TASKS_MANAGER.md §9）；
- 子代理工具调用同样经过 **Pre/PostToolUse**（同实例 B 的 hooks）→
  Permission 对孙调用一视同仁；
- 子代理的 messages **不经 CompactManager**（无 prepare/无 reactive
  兜底），溢出只能靠 API 报错自毁；
- `extract_text`（507-519）：只刮 `type=="text"` 块，无 text 块时返回
  `(no summary)`；不检查 `stop_reason`——`max_tokens` 截断的半截回答
  只要没带 tool_use 就会被当最终答案返回。

## 8. compact：注册但不路由（特殊公民）

- `COMPACT` schema 在 `self.tools`（128-137、登记 234）——模型可见可调；
- `toolsHandlers` **无** `compact` 项；主循环在分发前按名字拦截
  （loop.py:159-160），置位 `compact_requested`，回合工具结果 append 完后
  调 `compactManager.compact_history` 整列表替换（loop.py:186-187）；
- 若子代理幻觉调用 compact：不在 `subTools`，但 `execute_tool` 仍会被调 →
  路由表查无 → `Unknown:compact`；
- 配对语义与悬空 tool_use 分析见 COMPACT_MANAGER.md §10.3——**别给
  compact 补 handler**。

## 9. safe_path 与工作区硬线

```python
path = (env.workDirPath / p).resolve()
if not path.is_relative_to(env.workDirPath): raise ValueError(...)
```

- 这是**硬线**：不问人、不可被人工放行（与 permission 规则 1 的"问人"
  层互补，两层关系见 PERMISSION.md §7.1）；
- 绝对路径参数会直接替换基准（`Path / "/abs"` 语义），随后被围栏判住；
- 类内 `safe_path` 被定义了**两次**（273-277 与 381-385，内容逐字相同）：
  Python 类体内后定义覆盖前者，273 那份是死代码。行为无差异，但改动时
  只改 381 才生效——务必注意。

## 10. 不变量（改代码前必读）

1. 新工具登记 = 三处同步：schema 类常量（必须是 dict 字面量，§2.5）+
   `self.tools` + `toolsHandlers`（子代理可用则还要 `subTools`/
   `subToolsHandlers` 各一处）——漏注册表 = 模型看不见，漏 handler = `Unknown:`；
2. handler 返回值一律被当作喂给模型的**数据**（`str(output)`），
   出错要返回文本而不是抛异常（唯一例外：`execute_tool` 层的
   `TypeError` 会炸穿，见 §11.1）；
3. handler 的 Python 形参名必须与 schema 属性名逐字一致
   （`handler(**block.input)` 靠这个名字匹配；命名本身要顺主流先验，§2.4）；
4. `compact` 的"schema 注册但无 handler"是全库唯一例外，受主循环拦截
   保护（§8）；
5. 子代理永远不得获得 `task`（防递归 fork）、`compact`（拦截语义只在
   主循环实现）与 §6.6 的任务工具（`.lcc/task/` 状态归主代理独享）。

## 11. 已知坑点

1. **参数畸形 → 主循环崩溃**：模型漏传 required 或多传 schema 外的野参数，
   `handler(**tool_input)`（307）抛 `TypeError`，`execute_tool` 与 loop 都不接
   ——整个程序穿透退出。`run_in_background` 是唯一被前台分支吸收的参数
   （304-305，见 §5），其余野参数不在吸收之列；schema 对模型只是软约束
   （命名先验案底见 §2.4）；
2. `run_read` 无 limit 时全文返回，大文件一口烧穿上下文，事后只能靠
   CompactManager 五级流水线救（跨模块互不感知，同 SKILL_MANAGER.md §7
   的 load_skill 问题）；
3. `run_bash` 的 `[:50000]` 是 Python 字符切片，非 token 预算；
   中文内容同样 50000 字符但贵得多；且截断不产生任何"已截断"标记
   （对比 todo_write 的省略文案）；
4. `edit_file` 只替换第一处匹配（`replace(..., 1)`），无出现次数校验——
   `old_string` 不唯一时静默改错位置；schema 描述 "Replace exact text ...
   once" 已声明该语义，模型未必遵守；
5. `subSystemPrompt` 里 `f"agent at {self.env.workDir}."` 与下一字符串
   隐式拼接，句号和空格间无分隔（`at D:\x. Complete...`），纯观感问题；
6. 子代理 API 调用失败返回错误字符串而非抛出——主代理只看到一条普通
   tool_result，可能反复重试 `task`（无次数/熔断限制）；
7. 每个主循环 `Loop` 实际存在两个 `Anthropic` 客户端（loop.py:17 与
   tools_manager.py:220）和两个 `Hooks`（HOOKS.md §6）——改配置/换
   hook 时别只想到一处。

## 12. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `loop.py` | 构造本类；`messages.create(tools=self.toolsManager.tools)`；非 compact 工具全部经 `execute_tool(block, toolsHandlers)`（loop.py:162）；每轮开头经本类自持的 `backgroundTasksManager` 收割后台结果并注入（loop.py:72，BACKGROUND_TASKS_MANAGER.md §10） |
| `hooks.py` | 自建实例 B；Pre/PostToolUse、子代理 Stop 的宿主 |
| `permission.py` | 经 hooks 间接闸门所有工具执行（PERMISSION.md） |
| `skill_manager.py` | 持有唯一实例；`skills_catalog()` 被 loop 启动时调一次冻结进系统提示 |
| `task_manager.py` | 持有唯一实例（222 自建）；6 个任务依赖工具转发给它（§6.6，TASK_MANAGER.md） |
| `background_tasks_manager.py` | 持有唯一实例（223 自建）；`execute_tool` 后台分支接线（285-293）；`run_bash` 前台复用其 `run_bash_process`/`format_bash_result`（378-379）；子代理经 `sub_bash_info`（316-319）与 `allow_background=False`（554）双重禁用后台（BACKGROUND_TASKS_MANAGER.md） |
| `compact_manager.py` | `compact` schema 的"认领方"在主循环，本类只负责让它可见 |
| `env.py` | 工作区与模型配置的取值来源（ENV.md） |

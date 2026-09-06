# ToolsManager 技术文档

> 对应源码：`tools_manager.py`（本仓库当前版本 598 行）
> 状态：完整，**已接入主循环**（`loop.py:18` 构造、`loop.py:126` 执行；
> 子代理执行循环内置于本类 `run_subagent`）

## 1. 它解决什么问题

把"工具"这件事的四块拼在一起：

1. **schema**（模型看到什么）：15 个工具的 JSON Schema 定义，详解见 §2；
2. **handler**（怎么执行）：`toolsHandlers` / `subToolsHandlers` 两张路由表；
3. **执行闸门**：`execute_tool` 统一走 PreToolUse → handler → PostToolUse；
4. **子代理**：`run_subagent` 自带一个缩小版的 agent 循环。

对外主入口只有两个：`tools`（喂给 `messages.create`）与
`execute_tool(block, handlers)`；外加 `skills_catalog()` 供系统提示拼装。

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
| `bash` | `command` string（必填） | 执行 shell 命令（120 s 超时） |
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

`subTools`（252-258）只收 `bash`/`read_file`/`write_file`/`edit_file`/`glob`
五个通用文件工具；其余 10 个（todo_write、task、load_skill、compact 与 6 个
任务依赖工具）主循环独享——防递归 fork、拦截语义只存在于主循环、`.task/`
看板归主代理（§10 不变量 5）。

## 3. 工具注册总览

schema 本身与全部参数见 §2.3；这里只回答"谁路由到谁"：

| 工具 | 主循环 handler | 子代理可见/可执行 |
|---|---|---|
| `bash` | `run_bash` | ✓ / ✓ |
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
| `taskManager` | `TaskManager(env.taskDirPath)` | 6 个任务依赖工具的共享实例（217，TASK_MANAGER.md） |
| 4 张列表 | 219-265 | `tools`(219-235) / `toolsHandlers`(236-251) / `subTools`(252-258) / `subToolsHandlers`(259-265) |

`MAX_SUBAGENT_TURNS = 50`（类常量，205 行）。全部依赖**不接受注入**，测试替身只能事后覆写属性。

## 5. execute_tool：唯一闸门入口（274-287）

```
execute_tool(block, handlers)
├─ trigger PreToolUse → 非 None？ 直接 return str(blocked)
│     （handler 不执行，PostToolUse 也不触发——PERMISSION.md §6）
├─ handler = handlers.get(block.name)
│     查无此人 → output = "Unknown:{block.name}"   ← 注意这条路仍会走 PostToolUse
├─ 有 handler → output = handler(**block.input)
└─ trigger PostToolUse(block, output)（返回值丢弃）→ return str(output)
```

关键语义：

- **错误即数据**：handler 的返回值（含 `Error:...` 文本）原样成为
  tool_result 喂回模型；handler 内部约定自吞异常（§6）；
- `handler(**block.input)` 是裸调用（284）：模型传了 schema 外的参数名、或漏了
  required 参数 → `TypeError` **不被捕获**，一路炸穿主循环（§11.1）；
- 同一批多个 `tool_use` 逐个顺序执行（调用方 for 循环），无并行。

## 6. Handler 明细

### 6.1 run_bash（338-367）

- 自带 5 词危险子串黑名单：`rm -rf /`、`sudo`、`shutdown`、`reboot`、
  `> /dev/`——与 Permission 的 `DENY_LIST` **是两套独立清单**，内容有出入
  （此处 `> /dev/` 前缀更宽，但缺 `mkfs`/`dd if=`），命中时返回
  `Error: Dangerous command blocked`（不弹确认），详见 PERMISSION.md §7；
- `subprocess.run(shell=True, cwd=env.workDir, capture_output=True,
  text=True, errors="replace", timeout=120)`——阻塞最长 120 秒，无流式输出；
- stdout+stderr 合并 strip，**按字符截断前 50000**；空输出 → `(no output)`；
- 仅捕获 `TimeoutExpired`（→ `Error: Timeout(120s)`）与
  `FileNotFoundError/OSError`——其他异常穿透。

### 6.2 文件三件套（全部经 `safe_path`）

| | 行为 | 返回 |
|---|---|---|
| `run_read`（376-383） | 整文件 `read_text` 后 `splitlines`；给了 `limit` 且小于总行数 → 前 limit 行 + `... (N more lines)` | 文件内容；**未给 limit 不截断** |
| `run_write`（386-393） | 先 `parent.mkdir(parents=True, exist_ok=True)` 再整写 | `Wrote N bytes to {path}` |
| `run_edit`（396-405） | 读全文 → `old_string` 必须存在否则报错 → `replace(old, new, 1)` **只替换第一处** | `Edited {path}` |

三者的 `except Exception` 把一切（含 `safe_path` 的 `ValueError`）转成
`Error:...` 文本；小瑕疵：`run_write` 的格式串是 `f"Error{e}"`，少了冒号。

### 6.3 run_glob（408-425）

- `glob.glob(pattern, root_dir=workDirPath, recursive=True)`；
- 每条命中单独过 `resolve + is_relative_to` 围栏——symlink/绝对 pattern
  指出的界外结果**静默剔除**（不问人、不报错）；
- `sorted` 稳定序，前 200 条，溢出追加
  `...(more matches omitted; narrow the pattern)`；空 → `(no matches)`。

### 6.4 todo_write（428-484、487-492）

`update_todos` 纯校验+渲染，**无任何持久化副作用**——todo 列表的唯一
事实来源就是模型消息流里这些格式化字符串：

- 字符串输入双解析：`json.loads` 失败 → `ast.literal_eval` 兜底 → 都败
  `raise ValueError`；
- 校验链：必须是 list、≤20 条、每条 dict、`content` 非空、`status` ∈
  {pending, in_progress, completed}（转小写后比）、**至多一条
  in_progress**；
- 渲染 `[ ] / [>] / [x]` + 末尾 `(done/total completed)`；空列表 → `No todos`。

外约：主循环连续 3 轮未调用 todo_write 时，往结果批里塞
`<reminder>Update your todos.</reminder>`（loop.py:138-147）——提醒逻辑在
loop 不在本类。

### 6.5 run_load_skill（553-554）

一行转发 `skillManager.load(name)`，无长度控制（SKILL_MANAGER.md §7）。

### 6.6 任务依赖工具（557-597，细节见 TASK_MANAGER.md）

6 个工具全部转发给 `taskManager`（217 行自建
`TaskManager(env.taskDirPath)`），本类只做薄包装：

| 工具 | 实现 |
|---|---|
| `create_task` | `run_create_task`（557-560） |
| `update_task` | `run_update_task`（562-566） |
| `list_tasks` | `run_list_tasks`（568-588） |
| `get_task` | `run_get_task`（590-591） |
| `claim_task` | `run_claim_task`（593-594） |
| `complete_task` | `run_complete_task`（596-597） |

schema 定义与登记方式与前 9 个工具完全同构（参数总表见 §2.3，三处登记见
§10 不变量 1），所以本节只记**行为差异**：

- 全是薄包装：`run_claim_task`/`run_complete_task` 写死
  `owner="agent"`（594、597）——没有第二方，认领即绑定；
- `run_update_task` 的形参名 `addBlockedBy` 是 15 工具里唯一的 camelCase
  （562），与 schema 属性名逐字一致（§10 不变量 3、§2.4），改哪头都得同步；
- `run_list_tasks`（568-588）渲染 `[ ]/[>]/[x]` + status + owner +
  blockedBy，观感刻意贴近 `update_todos`（§6.4），但事实来源是
  `.task/` 目录里的 JSON 文件，不是消息流；
- 6 个工具都**不在** `subTools`（252-258）——子代理碰不到任务体系，
  与 §10 不变量 5 的收窄原则一致。

## 7. task 与子代理（run_subagent，510-551）

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
        逐个 execute_tool(block, subToolsHandlers) → tool_result 批 → append
50 轮耗尽 → "Subagent stopped after 50 turns without a final answer."
```

- **一次性问答**：主代理只见最终文本，看不到子代理中间过程；
- 工具面收窄到 5 个（`subTools`，252-258，见 §2.6），因此拿不到
  task/load_skill/todo_write/6 个任务依赖工具（§6.6），
  也拿不到 compact 的 schema；
- 子代理工具调用同样经过 **Pre/PostToolUse**（同实例 B 的 hooks）→
  Permission 对孙调用一视同仁；
- 子代理的 messages **不经 CompactManager**（无 prepare/无 reactive
  兜底），溢出只能靠 API 报错自毁；
- `extract_text`（495-507）：只刮 `type=="text"` 块，无 text 块时返回
  `(no summary)`；不检查 `stop_reason`——`max_tokens` 截断的半截回答
  只要没带 tool_use 就会被当最终答案返回。

## 8. compact：注册但不路由（特殊公民）

- `COMPACT` schema 在 `self.tools`（123-132、登记 228）——模型可见可调；
- `toolsHandlers` **无** `compact` 项；主循环在分发前按名字拦截
  （loop.py:123-124），置位 `compact_requested`，回合工具结果 append 完后
  调 `compactManager.compact_history` 整列表替换（loop.py:150-151）；
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
- 类内 `safe_path` 被定义了**两次**（267-271 与 369-373，内容逐字相同）：
  Python 类体内后定义覆盖前者，267 那份是死代码。行为无差异，但改动时
  只改 369 才生效——务必注意。

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
   主循环实现）与 §6.6 的任务工具（`.task/` 状态归主代理独享）。

## 11. 已知坑点

1. **参数畸形 → 主循环崩溃**：模型漏传 required 或多传野参数，
   `handler(**block.input)` 抛 `TypeError`，`execute_tool` 与 loop 都不接
   ——整个程序穿透退出。schema 对模型只是软约束（命名先验案底见 §2.4）；
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
   tools_manager.py:215）和两个 `Hooks`（HOOKS.md §6）——改配置/换
   hook 时别只想到一处。

## 12. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `loop.py` | 构造本类；`messages.create(tools=self.toolsManager.tools)`；非 compact 工具全部经 `execute_tool(block, toolsHandlers)`（loop.py:126） |
| `hooks.py` | 自建实例 B；Pre/PostToolUse、子代理 Stop 的宿主 |
| `permission.py` | 经 hooks 间接闸门所有工具执行（PERMISSION.md） |
| `skill_manager.py` | 持有唯一实例；`skills_catalog()` 被 loop 启动时调一次冻结进系统提示 |
| `task_manager.py` | 持有唯一实例（217 自建）；6 个任务依赖工具转发给它（§6.6，TASK_MANAGER.md） |
| `compact_manager.py` | `compact` schema 的"认领方"在主循环，本类只负责让它可见 |
| `env.py` | 工作区与模型配置的取值来源（ENV.md） |

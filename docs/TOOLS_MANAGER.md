# ToolsManager 技术文档

> 对应源码：`tools_manager.py`（本仓库当前版本 450 行）
> 状态：完整，**已接入主循环**（`loop.py:17` 构造、`loop.py:86` 执行；
> 子代理执行循环内置于本类 `run_subagent`）

## 1. 它解决什么问题

把"工具"这件事的四块拼在一起：

1. **schema**（模型看到什么）：9 个工具的 JSON Schema 类属性；
2. **handler**（怎么执行）：`toolsHandlers` / `subToolsHandlers` 两张路由表；
3. **执行闸门**：`execute_tool` 统一走 PreToolUse → handler → PostToolUse；
4. **子代理**：`run_subagent` 自带一个缩小版的 agent 循环。

对外主入口只有两个：`tools`（喂给 `messages.create`）与
`execute_tool(block, handlers)`；外加 `skills_catalog()` 供系统提示拼装。

## 2. 工具注册总览

| 工具 | schema 行 | 主循环 handler | 子代理可见/可执行 |
|---|---|---|---|
| `bash` | 14-22 | `run_bash` | ✓ / ✓ |
| `read_file` | 24-32 | `run_read` | ✓ / ✓ |
| `write_file` | 34-42 | `run_write` | ✓ / ✓ |
| `edit_file` | 44-56 | `run_edit` | ✓ / ✓ |
| `glob` | 58-66 | `run_glob` | ✓ / ✓ |
| `todo_write` | 68-94 | `run_todo_write` | ✗ |
| `task` | 96-104 | `run_subagent` | ✗（子代理不能再开孙代理） |
| `load_skill` | 106-118 | `run_load_skill` | ✗ |
| `compact` | 120-129 | **无 handler——主循环按名字拦截**（§7） | ✗ |

注意两张表不对称：`self.tools` 有 9 个 schema，`toolsHandlers` 只有 8 个
handler——`compact` 是唯一"模型可见、但路由表查无此人"的工具。

## 3. 构造与依赖

`ToolsManager()` 无参构造（loop.py:17），内部自建全部依赖：

| 成员 | 来源 | 说明 |
|---|---|---|
| `env` | `Env()` | 第 2 个 Env 实例（ENV.md §4） |
| `subSystemPrompt` | 硬编码 | "coding agent at {workDir}...return a concise final answer" |
| `hooks` | `Hooks()` | **第 2 个 Hooks 实例**，Pre/PostToolUse 实际归它管（HOOKS.md §6） |
| `client` | `Anthropic(base_url=...)` | 仅子代理自用；主循环另有自己的 client（loop.py:16） |
| `skillManager` | `SkillManager(env.skillsDirPath)` | 构造即扫描技能目录（SKILL_MANAGER.md） |
| 4 张列表 | §2 | `tools`/`toolsHandlers`/`subTools`/`subToolsHandlers` |

`MAX_SUBAGENT_TURNS = 50`（类常量）。全部依赖**不接受注入**，测试替身只能事后覆写属性。

## 4. execute_tool：唯一闸门入口（187-200）

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
  tool_result 喂回模型；handler 内部约定自吞异常（§5）；
- `handler(**block.input)` 是裸调用：模型传了 schema 外的参数名、或漏了
  required 参数 → `TypeError` **不被捕获**，一路炸穿主循环（§11）；
- 同一批多个 `tool_use` 逐个顺序执行（调用方 for 循环），无并行。

## 5. Handler 明细

### 5.1 run_bash（233-262）

- 自带 5 词危险子串黑名单：`rm -rf /`、`sudo`、`shutdown`、`reboot`、
  `> /dev/`——与 Permission 的 `DENY_LIST` **是两套独立清单**，内容有出入
  （此处 `> /dev/` 前缀更宽，但缺 `mkfs`/`dd if=`），命中时返回
  `Error: Dangerous command blocked`（不弹确认），详见 PERMISSION.md §7；
- `subprocess.run(shell=True, cwd=env.workDir, capture_output=True,
  text=True, errors="replace", timeout=120)`——阻塞最长 120 秒，无流式输出；
- stdout+stderr 合并 strip，**按字符截断前 50000**；空输出 → `(no output)`；
- 仅捕获 `TimeoutExpired`（→ `Error: Timeout(120s)`）与
  `FileNotFoundError/OSError`——其他异常穿透。

### 5.2 文件三件套（全部经 `safe_path`）

| | 行为 | 返回 |
|---|---|---|
| `run_read`（271-278） | 整文件 `read_text` 后 `splitlines`；给了 `limit` 且小于总行数 → 前 limit 行 + `... (N more lines)` | 文件内容；**未给 limit 不截断** |
| `run_write`（281-288） | 先 `parent.mkdir(parents=True, exist_ok=True)` 再整写 | `Wrote N bytes to {path}` |
| `run_edit`（291-300） | 读全文 → `old_text` 必须存在否则报错 → `replace(old, new, 1)` **只替换第一处** | `Edited {path}` |

三者的 `except Exception` 把一切（含 `safe_path` 的 `ValueError`）转成
`Error:...` 文本；小瑕疵：`run_write` 的格式串是 `f"Error{e}"`，少了冒号。

### 5.3 run_glob（303-320）

- `glob.glob(pattern, root_dir=workDirPath, recursive=True)`；
- 每条命中单独过 `resolve + is_relative_to` 围栏——symlink/绝对 pattern
  指出的界外结果**静默剔除**（不问人、不报错）；
- `sorted` 稳定序，前 200 条，溢出追加
  `...(more matches omitted; narrow the pattern)`；空 → `(no matches)`。

### 5.4 todo_write（323-387）

`update_todos` 纯校验+渲染，**无任何持久化副作用**——todo 列表的唯一
事实来源就是模型消息流里这些格式化字符串：

- 字符串输入双解析：`json.loads` 失败 → `ast.literal_eval` 兜底 → 都败
  `raise ValueError`；
- 校验链：必须是 list、≤20 条、每条 dict、`content` 非空、`status` ∈
  {pending, in_progress, completed}（转小写后比）、**至多一条
  in_progress**；
- 渲染 `[ ] / [>] / [x]` + 末尾 `(done/total completed)`；空列表 → `No todos`。

外约：主循环连续 3 轮未调用 todo_write 时，往结果批里塞
`<reminder>Update your todos.</reminder>`（loop.py:98-107）——提醒逻辑在
loop 不在本类。

### 5.5 run_load_skill（449-450）

一行转发 `skillManager.load(name)`，无长度控制（SKILL_MANAGER.md §7）。

## 6. task 与子代理（run_subagent，405-446）

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
- 工具面收窄到 5 个（`subTools`），因此拿不到 task/load_skill/todo_write，
  也拿不到 compact 的 schema；
- 子代理工具调用同样经过 **Pre/PostToolUse**（同实例 B 的 hooks）→
  Permission 对孙调用一视同仁；
- 子代理的 messages **不经 CompactManager**（无 prepare/无 reactive
  兜底），溢出只能靠 API 报错自毁；
- `extract_text`（390-402）：只刮 `type=="text"` 块，无 text 块时返回
  `(no summary)`；不检查 `stop_reason`——`max_tokens` 截断的半截回答
  只要没带 tool_use 就会被当最终答案返回。

## 7. compact：注册但不路由（特殊公民）

- `COMPACT` schema 在 `self.tools`（120-129、153）——模型可见可调；
- `toolsHandlers` **无** `compact` 项；主循环在分发前按名字拦截
  （loop.py:83-84），置位 `compact_requested`，回合工具结果 append 完后
  调 `compactManager.compact_history` 整列表替换（loop.py:110-111）；
- 若子代理幻觉调用 compact：不在 `subTools`，但 `execute_tool` 仍会被调 →
  路由表查无 → `Unknown:compact`；
- 配对语义与悬空 tool_use 分析见 COMPACT_MANAGER.md §10.3——**别给
  compact 补 handler**。

## 8. safe_path 与工作区硬线

```python
path = (env.workDirPath / p).resolve()
if not path.is_relative_to(env.workDirPath): raise ValueError(...)
```

- 这是**硬线**：不问人、不可被人工放行（与 permission 规则 1 的"问人"
  层互补，两层关系见 PERMISSION.md §7.1）；
- 绝对路径参数会直接替换基准（`Path / "/abs"` 语义），随后被围栏判住；
- 类内 `safe_path` 被定义了**两次**（180-184 与 264-268，内容逐字相同）：
  Python 类体内后定义覆盖前者，180 那份是死代码。行为无差异，但改动时
  只改 264 才生效——务必注意。

## 9. 不变量（改代码前必读）

1. 新工具登记 = 三处同步：schema 类常量 + `self.tools` + `toolsHandlers`
   （子代理可用则还要 `subTools`/`subToolsHandlers` 各一处）——
   漏注册表 = 模型看不见，漏 handler = `Unknown:`；
2. handler 返回值一律被当作喂给模型的**数据**（`str(output)`），
   出错要返回文本而不是抛异常（唯一例外：`execute_tool` 层的
   `TypeError` 会炸穿，见 §10.1）；
3. handler 的 Python 形参名必须与 schema 属性名逐字一致
   （`handler(**block.input)` 靠这个名字匹配）；
4. `compact` 的"schema 注册但无 handler"是全库唯一例外，受主循环拦截
   保护（§7）；
5. 子代理永远不得获得 `task`（防递归 fork）与 `compact`（拦截语义只在
   主循环实现）。

## 10. 已知坑点

1. **参数畸形 → 主循环崩溃**：模型漏传 required 或多传野参数，
   `handler(**block.input)` 抛 `TypeError`，`execute_tool` 与 loop 都不接
   ——整个程序穿透退出。schema 对模型只是软约束；
2. `run_read` 无 limit 时全文返回，大文件一口烧穿上下文，事后只能靠
   CompactManager 五级流水线救（跨模块互不感知，同 SKILL_MANAGER.md §7
   的 load_skill 问题）；
3. `run_bash` 的 `[:50000]` 是 Python 字符切片，非 token 预算；
   中文内容同样 50000 字符但贵得多；且截断不产生任何"已截断"标记
   （对比 todo_write 的省略文案）；
4. `edit_file` 只替换第一处匹配（`replace(..., 1)`），无出现次数校验——
   `old_text` 不唯一时静默改错位置；schema 描述 "Replace exact text ...
   once" 已声明该语义，模型未必遵守；
5. `subSystemPrompt` 里 `f"agent at {self.env.workDir}."` 与下一字符串
   隐式拼接，句号和空格间无分隔（`at D:\x. Complete...`），纯观感问题；
6. 子代理 API 调用失败返回错误字符串而非抛出——主代理只看到一条普通
   tool_result，可能反复重试 `task`（无次数/熔断限制）；
7. 每个主循环 `Loop` 实际存在两个 `Anthropic` 客户端（loop.py:16 与
   tools_manager.py:141）和两个 `Hooks`（HOOKS.md §6）——改配置/换
   hook 时别只想到一处。

## 11. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `loop.py` | 构造本类；`messages.create(tools=self.toolsManager.tools)`；非 compact 工具全部经 `execute_tool(block, toolsHandlers)`（loop.py:86） |
| `hooks.py` | 自建实例 B；Pre/PostToolUse、子代理 Stop 的宿主 |
| `permission.py` | 经 hooks 间接闸门所有工具执行（PERMISSION.md） |
| `skill_manager.py` | 持有唯一实例；`skills_catalog()` 被 loop 启动时调一次冻结进系统提示 |
| `compact_manager.py` | `compact` schema 的"认领方"在主循环，本类只负责让它可见 |
| `env.py` | 工作区与模型配置的取值来源（ENV.md） |

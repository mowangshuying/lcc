# MCP Tools 技术文档

> 对应源码：`mcp_manager.py`（本仓库当前 264 行）
> 集成落点：`loop.py` / `tools_manager.py` / `permission.py` / `tool_names.py` / `log.py`
> 状态：s14 全量移植完成，已接入主循环——组装根接线在 `tools_manager.py:292-294`，
> 每轮组装点在 `loop.py:180-192`
> 来源：learn-claude-code `s14_mcp_plugin`（参考实现为 542 行单文件 `code.py`）
> 注记：撰写本文时这批实现仍处于未提交工作区（基线 HEAD `ccfb960`），故不标注引入提交号

---

## 1. 它解决什么问题

1. **进程内 MCP 替身**。lcc 不接真实的 JSON-RPC / stdio transport：两个 mock server
   （`docs`、`deploy`）把工具定义与 handler 直接在进程内登记（`mcp_manager.py:100-159`）。
   教学目标是 MCP 的**机制**——发现、命名、组装、授权——而不是协议客户端本身。
2. **三段流水线**。connect 发现（`mcp_manager.py:169-183`）→ 动态工具池组装
   （`mcp_manager.py:187-238`，主循环每轮重算，`loop.py:180-181`）→ 宿主策略授权
   （策略快照 `toolPolicies` + 授权闸门 `permission.py:99-109`）。
3. **工具池是演化视图，不是静态表**。模型每轮看到的是内置工具 + 入口工具 `connect_mcp`；
   一旦连接某个 server，其工具从**下一轮**起以 `mcp__{server}__{tool}` 的名字进入池子。
   connect 的效果只改注册表（clients），进池要等下一次组装。
4. **授权由宿主说了算**。工具能否执行、是否需人工确认，唯一依据是宿主侧策略表
   `MCP_HOST_POLICY`（`mcp_manager.py:71-76`）；server 自述的 `annotations`
   （readOnlyHint / destructiveHint）在组装期被整体丢弃（`mcp_manager.py:217-219`），
   永远不参与授权——"声明工具的人不是授权的人"。
5. **s14 全量移植**。类结构、校验规则、错误文案、策略表内容与参考实现逐字对应；
   lcc 因自身架构（teams、hooks、统一日志）产生的 7 条有意差异集中在 §9 说明。

---

## 2. 架构图

```
                        人（console）
                  ▲ [y/N] 询问、日志 │ 用户输入
                  │                ▼
   +--------------+----------------+---------------------------+
   |        loop.py  _agent_loop_inner（每轮一圈）               |
   |  :181  assemble_pool → pool_tools / pool_handlers         |
   |  :182-192 发现期 ValueError → [Error] 假消息 + Stop，终止本轮 |
   |  :195-198 system += "Connected MCP servers: ..."（动态段）  |
   |  :201-207 messages.create(tools=pool_tools)               |
   |  :252   tool_use → execute_tool(block, pool_handlers)     |
   +-------+-----------------------------------------+--------+
 发现期 raise（组装）                        执行期（调用 handler）│
           │                PreToolUse 闸门 → Permission mcp__ 分支
           ▼                （permission.py:99-109，policy ← policy_for）
   +-------+---------------------+                             │
   | ToolsManager（组装根）        |                             ▼
   |  self.tools: 内置+团队+connect_mcp（:341-342）  +-----------+-----------+
   |  assemble_pool :364-365     │                    | mcp__ handler（lambda）|
   +-------+---------------------+                    | client.call_tool(原始名)|
            │ assemble(base_tools, base_handlers)     +-----------+-----------+
            ▼                                                     │
   +--------+-------------------------------------+               ▼
   | McpManager（mcp_manager.py）                  |    +---------------------+
   |  clients: server 名 → MCPClient（:92）         |    | MCPClient × N        |
   |  toolPolicies: 每轮快照、整体替换（:237）        |    |  tools / _handlers    |
   |  MCP_HOST_POLICY 宿主表（:71-76）              |    |  （进程内，无 transport）|
   |  MOCK_SERVERS 工厂表（:162-165）               |    +----------+----------+
   +--------+-------------------------------------+               │
            │ connect(name)：幂等 / Available 清单 / 注册（:169-183）▼
            └─────────────────────────► mock server 工厂 → handler 返回字符串
                                        错误在边界内转 "MCP error: ..." 字符串，
                                        作为 tool_result 回流给模型（不终止本轮）
```

---

## 3. MCPClient —— 进程内 server 替身

`MCPClient`（`mcp_manager.py:19-52`）是一个 server 的本地替身：`name` 是 server 名，
`tools` 是 MCP 原始格式的工具定义列表（camelCase `inputSchema`、可带 `annotations`），
`_handlers` 是"原始工具名 → 可调用对象"的分发表。

### 3.1 接口一览

| 接口 | 位置 | 语义 |
| --- | --- | --- |
| `__init__(name)` | `:22-25` | 空 client：tools=[]、_handlers={} |
| `register(tool_defs, handlers)` | `:31-41` | 四道校验后**整表替换式**落库（:40-41） |
| `call_tool(tool_name, args)` | `:45-52` | 分派 + 错误边界，永不 raise |

### 3.2 register：四道校验（逐字移植 s14 L171-181，`mcp_manager.py:27-30` 注释）

1. 工具名必须是 `str`（`mcp_manager.py:33`，isinstance 与非空共用一行判定）；
2. 工具名必须 truthy（非空串）——违反 1/2 → `ValueError: Every MCP tool needs a non-empty name`（`:34`）；
3. 同一 server 内不重名（`:35-36`）→ `ValueError: Duplicate MCP tool name on server {name!r}`；
4. 每个工具必须有对应 handler（`:37-39`）→ `ValueError: Missing MCP handlers: ...`（列出缺失名）。

有意**不做**反向"孤儿 handler"校验（`:27-30` 注释）：多出的 handler 没有入口，属无害冗余，s14 原样如此。
落库方式是整表替换而非增量合并（`:40-41`）——connect 每次用工厂造全新实例（`mcp_manager.py:176-177`），
理论上不存在重复注册路径。

### 3.3 call_tool：错误留在调用边界（`mcp_manager.py:45-52`，s14 L183-190）

- 未知工具名 → **返回**字符串 `MCP error: unknown tool '{tool_name}'`（`:46-48`）；
- handler 抛异常 → 捕获后返回 `MCP error: {类型名}: {消息}`（`:49-52`）；
- 正常返回统一 `str(...)` 化（`:50`），保证 tool_result 内容是字符串。

关键语义：这里全部是 **return 而非 raise**——调用期错误作为 tool_result 回流给模型，
模型可以改参数、换工具名自愈，本轮对话不中断。与发现期错误（§6.2、§9 第 2 条）形成有意的不对称。

---

## 4. normalize_mcp_name 与 `mcp__` 前缀

Anthropic 对工具名的字母表约束是 `[a-zA-Z0-9_-]`（上限 64 字符），而 MCP 的 server/tool
名可以含 `.`、中文等任意字符，必须先净化再拼名。

- `_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")`（`mcp_manager.py:56`）；
- `normalize_mcp_name`（`mcp_manager.py:59-64`）：逐字符替换为 `_`，属**有损净化**——
  `docs.one` 与 `docs_one` 会折叠成同一个名字（风险与兜底见 §10.1）；
- 净化后为空串 → `ValueError: MCP names cannot normalize to an empty string`（`mcp_manager.py:62-63`）。

最终工具名 = `MCP_PREFIX + safe_server + "__" + safe_tool`（`mcp_manager.py:202`）。
`MCP_PREFIX = "mcp__"` 定义在 `tool_names.py:37`，是**单一事实源**：命名处
（`mcp_manager.py:16,202`）与授权闸门的前缀识别处（`permission.py:6,99`）引用同一常量。
之所以上收到 tool_names 而不是写在 mcp_manager 内：Permission 需要识别前缀但绝不能
依赖 mcp_manager 模块（`tool_names.py:34-36` 注释），避免授权层反向耦合功能层。

双下划线分隔的动机（s14 README）：若拍平命名，`docs.one / get.version` 与
`docs_one / get_version` 净化后同名撞车；保留 `server__tool` 层级至少把碰撞面
缩到"同 server 内同名"或"跨 server 恰好同 tool 名"。

---

## 5. connect —— 连接发现三分支

`McpManager.connect(name)`（`mcp_manager.py:169-183`，移植 s14 L282-295）：

| 分支 | 条件 | 行为与返回 | 位置 |
| --- | --- | --- | --- |
| ① 幂等已连 | `name in self.clients` | 返回 `MCP server '{name}' already connected`；不重建 client，已登记工具原样保留 | `:171-172` |
| ② 未知 server | `MOCK_SERVERS` 查无 | 返回 `Unknown server '{name}'. Available: {', '.join(MOCK_SERVERS)}`——带可用清单，模型可据此改选（调用期错误，不终止本轮） | `:173-175` |
| ③ 正常连接 | 工厂命中 | `factory()` 实例化新 `MCPClient` 并登记进 `clients`（`:176-177`）；`log_info("mcp", "connected: ...")`（`:179`）；返回发现报告 `Connected to MCP server '{name}'. Discovered N tools: ...`（`:180-183`） | `:176-183` |

注意：connect 只改注册表，**mcp__ 工具要到下一轮 assemble 才进池**（§6）——"下一轮可见"
是 prompt 指引（`loop.py:75-76`）与系统提示动态段（`mcp_manager.py:260-264`）共同向模型声明的契约。

handler 入口是 `run_connect_mcp`（`mcp_manager.py:252-258`）：内部自带 try/except，
connect 若抛异常则转成 `Error: {类型名}: {消息}` 字符串返回（`:255-258`）——因为 lcc 的
`execute_tool` 不会替 handler 兜异常（与 s14 的有意差异，§9 第 1 条）。

**s14 设计债原样保留**：`CONNECT_TOOL` schema 的 `enum: ["docs", "deploy"]`
（`mcp_manager.py:85`）与 `MOCK_SERVERS` 的键（`mcp_manager.py:162-165`）是**手工同步**
关系而非自动派生，代码注释 `mcp_manager.py:78-79` 明示"新增 mock server 必须同时改这里，
否则模型无法选中该 server"。移植不修（§9 第 7 条），加 server 的同步清单见 §11。

---

## 6. assemble —— 工具池动态组装（每轮重算）

`McpManager.assemble(base_tools, base_handlers)`（`mcp_manager.py:187-238`，移植 s14 L316-359）
由 `ToolsManager.assemble_pool`（`tools_manager.py:364-365`）原样转发，主循环每轮调用一次
（`loop.py:180-181`）。

### 6.1 组装步骤

1. **base 浅拷贝**（`mcp_manager.py:191-192`）：内置 + connect_mcp 的工具表与 handler 表
   先复制再扩展，绝不污染 `ToolsManager` 的静态底表。
2. **origins 溯源表**（`:194-195`）：先把所有内置工具名登记为 `built-in tool '...'`；
   之后每个 MCP 工具入池前查 `origins`——与内置碰撞、或两个 MCP 工具彼此碰撞（净化撞名）
   → `ValueError: MCP tool name collision after normalization: ...`，报错同时给出双方来源
   （`:207-212`）。origins 兼作 schema 报错的定位锚（`:194` 注释、`:215`）。
3. **硬校验（全部 raise，属发现期）**：
   | 约束 | 条件 | 位置 |
   | --- | --- | --- |
   | 全名长度 | `mcp__` 前缀拼完 > 64 字符 | `:203-206` |
   | 名字唯一 | `prefixed` 已在 origins 中 | `:208-212` |
   | inputSchema | 非 dict，或 `type != "object"`（缺省取 `{}` 可过——视为空对象 schema） | `:213-215` |
4. **schema 翻译**（`:217-224`）：MCP 线上格式 camelCase `inputSchema` → Anthropic
   `input_schema`；只取 name / description / input_schema 三项，**annotations 整体丢弃**
   ——server 自述不构成授权依据（§7）。
5. **handler 绑定**（`:225-230`）：
   `lambda *, client=server, tool=raw_name, **kwargs: client.call_tool(tool, kwargs)`。
   keyword-only 默认参数**捕获当前循环变量**防止闭包晚绑定——否则所有 handler 会集体错投到
   最后一个 server 的最后一个工具（`:225-226` 注释）。kwargs 透传模型入参，`client`/`tool`
   被劫持的边界见 §10.2。
6. **策略登记**（`:231-233`）：`policies[prefixed] = MCP_HOST_POLICY.get((server_name, raw_name), "confirm")`
   ——查表键是 **(server, tool) 原始名元组**，不是净化名：策略表按 server 作者的原生命名书写，
   净化只影响模型可见的工具名。
7. **快照整体替换**（`:235-237`）：`self.toolPolicies = policies` 一次性赋值，不做增量合并——
   不留陈旧键，快照与本次返回的 handlers **原子成对**，Permission 读到的策略与本圈工具池必然一致。
   返回 `(tools, handlers)`（`:238`）。

### 6.2 错误语义的有意不对称

- **发现期**（assemble raise ValueError）= 宿主不变量被破坏（坏 schema / 撞名 / 超长名），
  绝不能带着残破工具池去调 API。`loop.py:182-192` 单独捕获 ValueError：向 history 塞一条
  `[Error] {类型名}: {消息}` 的 assistant 文本消息（`:183-189`）、触发 `Stop` hooks（`:190`）、
  直接 return 终止本轮（`:192`）；assignment 释放由外层 `agent_loop` 的 finally 兜底
  （`loop.py:161-164,191`）。组装 try 刻意放在 API try **之外**，与 reactive compact 的
  异常路径互不污染（`loop.py:176-179` 注释，零改动原则）。
- **调用期**（call_tool / run_connect_mcp 返回错误字符串）= 模型可自愈的输入问题，
  作为 tool_result 回流，不终止本轮。

---

## 7. 宿主授权策略

### 7.1 策略表

`MCP_HOST_POLICY`（`mcp_manager.py:71-76`），键为 `(server, tool)` **原始名**元组：

| server / tool | 策略 | 语义 |
| --- | --- | --- |
| `docs` / `search` | allow | 只读检索，静默放行 |
| `docs` / `get_version` | allow | 只读版本查询 |
| `deploy` / `status` | allow | 只读状态检查 |
| `deploy` / `trigger` | confirm | 触发部署，须人工确认 |

任何未登记的 `(server, tool)` 缺省 **confirm**——fail-closed（组装期 `:231-233`、
查询期 `policy_for` `:240-242` 两处兜底同文）。

### 7.2 server 自述不作数

mock 工具定义里刻意带了 `annotations`：docs/search、docs/get_version、deploy/status 标
`readOnlyHint: True`（`mcp_manager.py:112,118,151`），deploy/trigger 标
`destructiveHint: True`（`mcp_manager.py:141`）。但组装期只抄三项、annotations 整体丢弃
（`mcp_manager.py:217-219`），授权链路从头到尾不读它。若允许 server 自述参与授权，
等于让工具提供方给自己发通行证——宿主策略必须独立成表。

### 7.3 lcc 接线：回调注入而非全局直读

| 环节 | 位置 | 说明 |
| --- | --- | --- |
| `Permission.mcp_policy` 属性 | `permission.py:14-18` | `Callable[[str], str] \| None`，默认 None |
| 组装根注入 | `tools_manager.py:293-294` | `self.hooks.permission.mcp_policy = self.mcpManager.policy_for` |
| 快照查询 | `mcp_manager.py:240-242` | `policy_for(prefixed)` 读组装期快照，未登记一律 confirm |
| 闸门回退 | `permission.py:100` | `(self.mcp_policy or (lambda _: "confirm"))(block.name)`——未接线也 fail-closed |

`check_permission`（`permission.py:78`）的 `mcp__` 分支（`:95-109`）落在 deny 链与
`check_rules` **之后**：内置工具名不会以 `mcp__` 开头，天然互斥，不会重复询问
（`:95-98` 注释，对照 s14 permission_hook 第三段 L414-420）。策略非 allow 时两条路径：

- `prompt_user=True`（Lead 交互，默认）：`ask_user` 打印 reason 与 `Tool: 名(入参)`
  后弹 `Allow? [Y/N]`（`permission.py:66-72,106-109`）；拒绝 → 返回
  `Permission denied by user` 回流给模型；
- `prompt_user=False`（队友 / 非交互）：绝不占用 console input()，直接返回
  `Permission required: MCP tool policy requires confirmation`（`:103-105`）。

实践中 MCP 工具 Lead 独占（§8），队友池里根本没有 `mcp__` 工具，非交互分支是防御性
对齐——与 `check_rules` 的 Lane D 契约（`permission.py:75-77` docstring）保持一致。

---

## 8. 集成点（组装根接线清单）

**tools_manager.py**：

- `from mcp_manager import McpManager`（`:20`）、`CONNECT_MCP` 并入 tool_names 导入行（`:21`）；
- 组装根构造 `self.mcpManager = McpManager()`（`:292`，无参、不依赖 env/client，`:291` 注释）；
- 策略回调接线 `permission.mcp_policy = policy_for`（`:293-294`）；
- 入口工具静态并入：`self.tools += [connect_tool_info()]`（`:341`，访问器返回
  `CONNECT_TOOL`，`mcp_manager.py:246-248`）、`self.toolsHandlers[CONNECT_MCP] =
  self.mcpManager.run_connect_mcp`（`:342`）。`connect_mcp` 常驻静态表，`mcp__*`
  由 assemble_pool 每轮动态追加（`:339-340` 注释）；
- **Lead 独占**（`:343-344` 注释）：`subTools`（`:345-351`）与 `subToolsHandlers`
  （`:353-359`）不含 connect_mcp，队友/子代理工具池不参与 MCP 动态组装；
- `assemble_pool`（`:361-365`）：转发 `mcpManager.assemble(self.tools, self.toolsHandlers)`。

**loop.py**：

- system 指引段 `prompt_mcp`（`:73-77`）随其他段并入（`:98`）："调用 connect_mcp
  连接 server 后，其工具以 mcp__{server}__{tool} 名字在下一轮加入工具池；使用前若宿主
  策略未放行会向用户请求确认"；
- `_agent_loop_inner` 每轮：`pool_tools, pool_handlers = self.toolsManager.assemble_pool()`
  （`:180-181`），发现期 ValueError 兜底（`:182-192`，§6.2）；
- system 动态段：`system_prompt_note()`（`:195-198`；实现 `mcp_manager.py:260-264`——
  无连接返回 None 不加段，有连接追加 `Connected MCP servers: docs, deploy`，只报
  server 名不报工具清单）；
- API 传 `tools=pool_tools`（`:205`），执行传 `pool_handlers`（`:252`）——同一轮内
  模型看到的工具定义与实际可调的 handler 同源同刻。

**其余**：`tool_names.py` 定义 `CONNECT_MCP = "connect_mcp"`（`:32`）与
`MCP_PREFIX = "mcp__"`（`:34-37`）；`log.py:8` 的 tag 约定注释行新增 `mcp`，
实际打点唯一处在 `mcp_manager.py:179`。

---

## 9. 与 s14 参考实现的有意差异

| # | 差异 | s14（`code.py`） | lcc | 动机 |
| --- | --- | --- | --- | --- |
| 1 | handler 异常捕获位置 | `execute_tool` 对 handler 调用包 try/except（L469-472） | `execute_tool` 不捕获（`tools_manager.py:368-398`，`:395` 直接调 handler）；`run_connect_mcp` 在自身边界内 try（`mcp_manager.py:252-258`，注释 `:253-254`） | lcc 的 execute_tool 已承载 hooks/后台执行分支，在此兜异常会把**所有** handler 的错误悄悄吞成字符串；"错误留在边界内"由 MCP handler 自己兑现 |
| 2 | 发现期错误的捕获结构 | assemble 与 messages.create 同一个 try（L481-499） | assemble 独立 try、只捕 ValueError（`loop.py:180-192`） | 避免污染 API 异常路径与 reactive compact 重试逻辑（零改动原则，`loop.py:176-179` 注释） |
| 3 | MCP 工具 Lead 独占 | 无 subagent/teams 概念 | `subTools` 与队友池不并入 connect_mcp、不参与动态组装（`tools_manager.py:343-351`） | lcc 有 teams：每轮组装只发生在 Lead 主循环里，队友若拿到 `mcp__` 名字将没有对应 handler 池，必须从工具表隔离 |
| 4 | 策略读取方式 | permission_hook 直读模块级全局 `mcp_tool_policies`（L194,415） | `Permission.mcp_policy` 回调，由组装根注入（`permission.py:18`；`tools_manager.py:294`） | Permission 保持不知策略来源，可独立测试；回调缺席时 `or lambda: "confirm"` 兜底（`permission.py:100`） |
| 5 | MCP_PREFIX 上收 | `"mcp__"` 字面量散落两处（L332,414） | 定义于 `tool_names.py:37`，命名与识别两端引用同一常量 | 两处字面量是漂移隐患；且 permission 不应依赖 mcp_manager（`tool_names.py:34-36` 注释） |
| 6 | 输出通道 | `print`（L291） | `log_info("mcp", ...)`（`mcp_manager.py:179`） | 对齐 lcc 统一日志规范（tag 清单 `log.py:8`） |
| 7 | enum 设计债保留 | CONNECT_TOOL 的 enum 与 MOCK_SERVERS 手工同步（L307 对照 L276-279） | 原样保留、不自动派生（`mcp_manager.py:78-79,85`） | 教学代码如实保留同步点，提醒"模型可见菜单与真实注册表是两份东西"（§5、§11） |

---

## 10. 已知坑与设计注记

### 10.1 有损净化可能"串台"

`[^a-zA-Z0-9_-]→_` 是多对一映射：server `a` 的 tool `x.y` 与 server `a` 的 tool `x_y`
净化后同名。lcc 不去重、不加后缀，兜底是 origins 碰撞检测（`mcp_manager.py:208-212`）：
第二个撞名工具直接 raise，按 §6.2 属发现期错误——**终止本轮**而非错投，碰撞消息同时
列出双方来源便于定位。宁可炸也不串台，是 fail-loud 取向。

### 10.2 模型显式传 client / tool 参数可劫持分发（s14 已知边界，如实保留）

handler 是 `lambda *, client=server, tool=raw_name, **kwargs`（`mcp_manager.py:227-230`），
keyword-only 默认参数可被模型**显式传参覆盖**：若某 server 工具的 inputSchema 恰好声明了
名为 `client` 或 `tool` 的参数，模型传入的值会替换掉捕获的分派目标——`client.call_tool(...)`
轻则错投、重则对字符串调方法抛 `AttributeError`。且因差异表第 1 条（lcc `execute_tool`
不捕获 handler 异常，`tools_manager.py:395`），该异常会一路穿透 `_agent_loop_inner` 与
`run()`（`loop.py:327` 的 try 只捕 KeyboardInterrupt，`:354-355`），掀掉整个交互进程。
两个 mock server 均无这两个参数名故安全；接真实 MCP server 需改名参数或在 lambda 前加
剥离边界。本次移植不修，仅如实记录。

### 10.3 connect 之前调用 mcp__ 工具

此时名字不在 pool_tools 里，守规矩的模型不会调；若幻觉调用，会**先**撞 Permission 的
`mcp__` 闸门（PreToolUse 在 handler 查找之前，`tools_manager.py:369-371` 早于 `:386`；
快照为空 → 缺省 confirm → 弹 `[Y/N]`，`permission.py:99-105`），放行后 handler 查无，
返回 `Unknown:mcp__...` 字符串（`tools_manager.py:387-388`）回流，模型据此改调
connect_mcp 自愈。小代价是白耗一次人工确认；s14 行为相同。

### 10.4 每轮组装的开销

assemble 每轮做浅拷贝 + O(已注册 MCP 工具数) 的名字净化 / schema 翻译 / 策略查表
（`mcp_manager.py:191-238`）。纯本地 list/dict 操作在教学规模下可忽略；真实 MCP 场景
（多 server、长工具清单、昂贵发现）应考虑按 clients 版本号做增缓或缓存——刻意不做，
保持与 s14 逐字对应、逻辑透明。

---

## 11. 快速上手

前置：`python loop.py` 启动。未连接任何 server 时 MCP 对主循环零影响
（clients 为空 → assemble 返回内置池拷贝；note 返回 None，`mcp_manager.py:260-264`）。

**剧本①：连接 docs，allow 直通** —— 输入"连接 docs 并搜索 agent hooks"

```
第 1 轮：模型按 prompt_mcp 指引（loop.py:74-77）调 connect_mcp(name="docs")
        —— 名字不带 mcp__ 前缀，Permission 的 mcp__ 分支不触碰；
           handler 直连 run_connect_mcp（tools_manager.py:342）
        tool_result: "Connected to MCP server 'docs'. Discovered 2 tools: search, get_version"
第 2 轮：assemble 产出 mcp__docs__search / mcp__docs__get_version 进池；
        system 追加 "Connected MCP servers: docs"
        模型调 mcp__docs__search(query="agent hooks")
        → policy_for("mcp__docs__search") = "allow" → 静默放行不弹窗
        tool_result: "[docs] Found 3 results for 'agent hooks'"
```

**剧本②：连接 deploy，confirm 弹 [y/N]** —— 输入"连接 deploy 并触发 web 服务"

```
connect_mcp(name="deploy") → 下一轮池中出现 mcp__deploy__status / mcp__deploy__trigger
mcp__deploy__trigger(service="web")：
    policy = "confirm" → 弹窗（permission.py:66-72,106-109）：
        [permission] MCP tool policy requires confirmation
        [permission] Tool: mcp__deploy__trigger({'service': 'web'})
        Allow? [Y/N]
    y → tool_result "[deploy] Triggered: web"
    n → tool_result "Permission denied by user"（回流，模型如实转述）
mcp__deploy__status(service="web")：policy = "allow" → 直通
    tool_result "[deploy] web: running (v1.4.2)"
```

**剧本③：连不存在的 server** —— 模型调 connect_mcp(name="jira")

```
tool_result: "Unknown server 'jira'. Available: docs, deploy"
—— 调用期错误，不终止本轮；模型从 Available 清单改选（§5 分支②）
```

**新增一个 mock server 的三处同步**（缺一即坏）：

1. 加工厂静态方法并注册进 `MOCK_SERVERS`（仿 `mcp_manager.py:99-165`）；
2. 把同名加进 `CONNECT_TOOL` 的 `input_schema.properties.name.enum`
   （`mcp_manager.py:85`）——手工同步是设计债（`:78-79`），漏改则模型永远选不中它；
3. 在 `MCP_HOST_POLICY` 加 `(server, tool)` 授权条目（`mcp_manager.py:71-76`）——
   漏加不会坏，只是全部缺省 confirm（fail-closed，安全但啰嗦）。

约束提醒：annotations 填不填都不影响授权（§7.2）；原始名尽量留在 `[a-zA-Z0-9_-]`
内以免撞名（§10.1）；`mcp__{server}__{tool}` 全名 ≤ 64 字符否则组装炸（`:203-206`）。

---

## 12. 关联文档与接口索引

### 12.1 关联文档

| 文档 | 与本文的关系 |
| --- | --- |
| [PERMISSION.md](PERMISSION.md) | `mcp__` 授权分支（`permission.py:95-109`）所在闸门全貌；`prompt_user` 双路径契约 |
| [TOOLS_MANAGER.md](TOOLS_MANAGER.md) | 组装根：静态工具底表、`assemble_pool` 转发、`execute_tool` 的 hook 前置顺序 |
| [AGENT_TEAMS.md](AGENT_TEAMS.md) | MCP 工具 Lead 独占的对照组：队友工具池 / 非交互授权（`tools_manager.py:343-344`；`permission.py:103-105`） |
| [TASK_MANAGER.md](TASK_MANAGER.md) | 发现期终止本轮时 assignment 释放的外层兜底（`loop.py:161-164` 的 finally 语义与之同构） |
| [COMPACT_MANAGER.md](COMPACT_MANAGER.md) | 组装 try 独立于 reactive compact 异常路径的动机侧（`loop.py:176-179` 注释） |

### 12.2 关键接口速查

| 模块 | 接口（· 分隔，行为 `名 :行号`） |
| --- | --- |
| `mcp_manager.py` | `MCPClient :19` · `register :31` · `call_tool :45` · `normalize_mcp_name :59` · `McpManager :67` · `MCP_HOST_POLICY :71` · `CONNECT_TOOL :80` · `_mock_server_docs :100` · `_mock_server_deploy :129` · `MOCK_SERVERS :162` · `connect :169` · `assemble :187` · `policy_for :240` · `connect_tool_info :246` · `run_connect_mcp :252` · `system_prompt_note :260` |
| `tools_manager.py` | `import McpManager :20` · `McpManager 构造 :292` · `mcp_policy 接线 :294` · `connect_mcp schema 并入 :341` · `connect_mcp handler 注册 :342` · `subTools（不含 MCP）:345` · `assemble_pool :364` · `execute_tool :368` |
| `loop.py` | `prompt_mcp :74` · `并入 system :98` · `每轮组装 try :180` · `发现期兜底 :182-192` · `note 追加 :195-198` · `tools=pool_tools :205` · `pool_handlers 传参 :252` |
| `permission.py` | `MCP_PREFIX import :6` · `mcp_policy 属性 :18` · `check_permission :78` · `mcp__ 分支 :99-109` |
| `tool_names.py` | `CONNECT_MCP :32` · `MCP_PREFIX :37` |
| `log.py` | `tag 约定（含 mcp）:8` |

# TaskManager 技术文档

> 对应源码：`task_manager.py`（本仓库当前版本 222 行）
> 状态：完整，**已接入主循环**（`tools_manager.py:217` 构造；6 个工具 schema 注册于
> `tools_manager.py:229-234`、handler 路由于 `tools_manager.py:245-250`）
> 引入提交：`7d78025` feat(task): add TaskManager with dependency-aware task tools

## 1. 它解决什么问题

给模型一块**落盘的、带依赖图的任务看板**。与同为"任务"的 `todo_write`
（TOOLS_MANAGER.md §6.4：纯渲染字符串、零持久化）不同，TaskManager 提供：

1. **持久化**：一任务一 JSON 文件，重启/压缩后状态仍在盘上；
2. **依赖 DAG**：`blockedBy` 前置依赖 + 成环检测，任务只能按拓扑序推进；
3. **状态机**：`pending → in_progress → completed`，认领/完成各有闸门；
4. **解锁通知**：完成任务时精确报告"被这次完成解锁"的下游任务。

对外没有直接入口——全部经 `tools_manager.py` 的 6 个工具
（`create_task` / `update_task` / `list_tasks` / `get_task` /
`claim_task` / `complete_task`）进入，见 §6。

## 2. Task 数据结构（task_manager.py:9-17）

`@dataclass`，6 个字段：

| 字段 | 类型 | 创建时初值 | 说明 |
|---|---|---|---|
| `id` | `str` | `task_` + `secrets.token_hex(4)` | 8 位小写十六进制，见 §3 |
| `subject` | `str` | 入参（strip 后非空） | 标题 |
| `description` | `str` | 入参，默认 `""` | 描述 |
| `status` | `str` | `"pending"` | 合法集见下 |
| `owner` | `str \| None` | `None` | 认领者；工具层恒为 `"agent"`（tools_manager.py:594/597） |
| `blockedBy` | `list[str]` | `[]` | 前置任务 ID 列表 |

- **合法状态集**：`(pending, in_progress, completed)`，白名单校验只写在
  `load`（task_manager.py:126-127）——即"读时校验"，写路径靠状态机闸门
  （§5.4）保证不产生非法值；手工把文件改成 `"done"` 会在下次 load 抛
  `ValueError`。
- **序列化**：`asdict` + `json.dumps(indent=2)`（create 在 68、save 在
  131），文件即字段的直白映射，人肉可读可改（改坏了 load 会拒）。
- **反序列化**：`Task(**data)`（task_manager.py:122），键必须与字段**恰好
  一致**——多一个键、少一个键都是 `TypeError`（§9.4）。

## 3. 存储模型与路径围栏

```
<workDir>/.task/            ← env.py:19  taskDirPath；.gitignore 第 9 行 /.task
├── task_5dea2769.json      ← 一任务一文件，文件名 = id + ".json"
└── task_f7e35c92.json
```

- **ID 格式**：`TASK_ID_PATTERN = ^task_[0-9a-f]{8}$`（task_manager.py:22），
  ID 空间 2³²；所有入口只认这个正则。
- **`_path`（task_manager.py:37-45）双层防护**：
  1. `fullmatch` 拒绝任何不合法 ID（`task_id` 甚至不是 str 也在这层挡掉，
     38-39）；
  2. 拼路径后 `resolve()` + `is_relative_to(root)`（43-44）——ID 正则已
     排除 `/`、`\`、`..`，这层是纵深防御，双保险。
- **`_root`（task_manager.py:28-34）防逃逸出工作区**：根目录 `resolve()` 后
  必须 `is_relative_to(env.workDirPath.resolve())`（32-33），否则
  `ValueError("TaskManager escapes the workspace")`。与 TOOLS_MANAGER.md
  §9 的 `safe_path` 同一思路。⚠ 第 31 行 `self.directory.resolve(...)` 的
  传参有个名不副实的坑，见 §9.2。
- **懒建目录**：`_root(create=True)` 才 `mkdir`；只有写路径（create、
  save、`_path(create_root=True)`）会建目录，读路径不产生副作用——代价是
  目录不存在时读路径直接炸（§9.2）。
- `exists`（48-49）：`_path(...).is_file()`，供依赖存在性校验。

## 4. 方法地图

| 分组 | 方法 | 行号 | 说明 |
|---|---|---|---|
| 存储层 | `exists` | 48-49 | 按 ID 判文件在否 |
| | `load` | 120-128 | 读盘 + 三校验（键一致、ID 与文件名一致、status 白名单） |
| | `save` | 130-131 | 整文件覆写 |
| | `list` | 133-143 | 目录不存在→`[]`；`sorted(glob("task_*.json"))` 逐个 load |
| 图查询 | `_depends_on` | 76-91 | task_id 的传递闭包里能否触达 target_id |
| | `incomplete_dependencies` | 160-169 | 未完成的依赖（缺失/损坏一律算未完成） |
| | `can_start` | 171-172 | 无未完成依赖 |
| 业务 | `create` | 52-73 | 生成 ID 并落盘 |
| | `update_dependencies` | 94-118 | 加边（五层校验，§5.2） |
| | `claim_task` | 176-190 | pending→in_progress |
| | `complete_task` | 194-222 | in_progress→completed + 解锁通知 |
| 工具适配 | `create_task` / `update_task` / `load_task` / `list_tasks` | 145-155 | 一行转发 |
| | `get_task` | 157-158 | load 后序列化成 JSON 字符串返回（喂模型的形态） |

145-155 的四个薄封装存在的意义是给工具层留一层命名适配（`update_task` →
`update_dependencies` 等），逻辑零附加。

## 5. 核心流程

### 5.1 create（task_manager.py:52-73）

```
subject.strip() 非空校验 → _root(create=True) 建目录
└─ for _ in range(100):                      ← 58
     task = Task(id=task_<token_hex(4)>, status=pending, owner=None, blockedBy=[])
     open("x") 独占创建文件并 json.dump       ← 67，存在即 FileExistsError
     成功 → return task；FileExistsError → 换 ID 重试
   100 次全撞 → RuntimeError("Could not allocate a unique task ID")  ← 73
```

`open("x")` 让"ID 是否已被占用"由文件系统原子裁决——不维护内存注册表，
并发/重命名进程也不会互相覆盖。2³² 空间下真撞 100 次≈不可能，RuntimeError
是理论兜底。

### 5.2 update_dependencies（task_manager.py:94-118）：五层校验

全系统入参校验最密的函数，按序：

| # | 校验 | 行号 | 失败 |
|---|---|---|---|
| 1 | `add_blocked_by` 必须是 `list` | 95-96 | `ValueError` |
| 2 | 目标任务须 `pending` **且** `owner is None` | 99-100 | `ValueError`——已开工的任务不许改依赖 |
| 3 | 自依赖 `dependency == task_id` | 104-105 | `ValueError` |
| 4 | 依赖文件必须存在 | 107-108 | `ValueError: Dependency not found` |
| 5 | 成环检测 | 110-111 | `ValueError: Dependency cycle detected` |

细节：

- 入参先 `list(dict.fromkeys(...))` 去重、保序（102）；
- 第 5 层条件 `dependency not in task.blockedBy and self._depends_on(
  dependency, task_id)`（110）——若新边本就是重复边，跳过环检（重复添加
  无害，114 处也不会写入）；否则问"dependency 是否传递依赖 task_id"，
  是则新边成环；
- 全部通过后才追加新边并 `save`（113-117），**校验期间零副作用**。
- 小瑕疵：错误串 `"- >"` 中间多了个空格（111）。

### 5.3 _depends_on（task_manager.py:76-91）：迭代式 DFS

```
pending=[task_id]（栈），visited=set()
while pending:
    current = pending.pop()          ← 尾弹出栈 = 深度优先
    current == target_id → True
    current in visited → 跳过（防重复展开，也防既有环把搜索吊死）
    visited.add；pending.extend(self.load(current).blockedBy)
 exhausted → False
```

- 参数名 typo：`taks_id`（76）——纯位置传参无实害，但别当关键字传；
- 每触达一个节点都 `self.load(...)` 读盘：节点被删 → `FileNotFoundError`
  **不捕获**，直接穿透（§9.5）；
- 与 `incomplete_dependencies`（160-169）宽严不对称，见 §9.5。

### 5.4 状态机：claim_task / complete_task

```
        claim_task（仅 pending，且 incomplete_dependencies 为空）
pending ────────────────────────────────▶ in_progress
（依赖可改期：pending 且未认领）              │
                                            │ complete_task（仅 in_progress，owner 相符）
                                            ▼
                                        completed（终态，不可回退）
```

- **claim（176-190）**：status≠pending → 返回文本 `Task {id} is {status},
  cannot claim`（179）；有未完成依赖 → `Blocked by: [...]`（183）；两者都是
  **返回文本而非抛异常**——"错误即数据"，模型能自救（换个任务claim）。
  成功则 `owner/status` 落盘、`print [claim]`、返回 `Claimed {id} {subject}`。
- **complete（194-222）**：仅 in_progress（196-197）；owner 须相符
  （199-200，该行消息有真 bug，§9.1）。

- **unblocked 通知——`ready_before` 快照对比**（203-214）：
  1. 改状态**前**，全表扫一遍，记下"pending、有依赖、且现在就能开工"的
     任务 ID 集合 `ready_before`（203-206）；
  2. 置 completed 落盘（208-209）；
  3. 再扫一遍：pending 且有依赖、**不在** `ready_before`、现在
     `can_start` → 这就是"被这次完成解锁"的任务，收集 subject（211-214）；
  4. 有则消息尾部拼 `\nUnblocked: ...` 并 `print [unblocked]`（218-220）。

  为什么用前后快照而不是反向查依赖边：反向查要维护/遍历"谁依赖我"，快照
  对比用两遍全表扫换"永远与 can_start 判定口径一致"——实现朴素但难写错。
  无依赖的 pending 任务两侧都不进快照（`candidate.blockedBy` 为假被过滤），
  不会误报。

### 5.5 incomplete_dependencies / can_start（task_manager.py:160-172）

`except (FileNotFoundError, ValueError)`（166）——依赖文件缺失或内容损坏
**一律视为未完成**。这是宽接口：claim/complete 因此永远能给出
"Blocked by: [坏依赖]"的文本提示，而不是崩溃（对比 §5.3 的严接口）。

## 6. 与 tools_manager.py 的接线

| 工具 | schema 行 | 封装函数行 | 入参 | 返回给模型 |
|---|---|---|---|---|
| `create_task` | 136-148 | 557-560 | `subject`，`description?` | `Created {id}: {subject}` + print |
| `update_task` | 150-166 | 562-566 | `task_id`、`addBlockedBy` | `Updated {id} blockedBy: ...` + print |
| `list_tasks` | 168-172 | 568-588 | 无 | `[ ]/[>]/[x] {id}: {subject} [{status}] [owner] (blockedBy: ...)`；空→`No tasks. Use create_task to add some.` |
| `get_task` | 174-182 | 590-591 | `task_id` | 任务全文 JSON 字符串 |
| `claim_task` | 184-192 | 593-594 | `task_id` | claim 的文本（含 Blocked by） |
| `complete_task` | 194-202 | 596-597 | `task_id` | complete 的文本（含 Unblocked） |

- **登记三处同步**（TOOLS_MANAGER.md §10 不变量 1 在 task 上的体现）：
  schema 类属性 + `self.tools`（229-234，经 `*_info()` 转发方法
  319-335 取值）+ `toolsHandlers`（245-250）；
- **子代理不可见**：6 个 task 工具都不在 `subTools`（252-258），看板只归
  主代理管；
- `owner` 在工具层写死 `"agent"`（594、597）——当前没有多 agent 概念，
  owner 字段是为将来预留的；
- `run_*` 封装全部**无 try/except**：claim/complete 的"业务失败"以文本
  返回天然安全，但 create/update/get/list 触发的 `ValueError` /
  `FileNotFoundError` 会穿透 `execute_tool` 的裸调用
  （tools_manager.py:284）炸穿主循环（TOOLS_MANAGER.md §11.1 同款，
  详见 §9）；
- **参数名即契约**：`handler(**block.input)` 靠形参名匹配 schema 属性名，
  `update_task` 的 camelCase `addBlockedBy`（158 与 562）必须逐字一致；
- **历史坑（值得记住）**：本提交初版把 6 个 task schema 类属性写成
  `{...},`——尾逗号使每个 dict 字面量变一元 tuple，序列化后 `tools` 数组
  元素成了 `[{...}]`，网关直接 400 `Request body format invalid`。
  **schema 类属性必须是 dict，尾逗号是隐形炸弹**。

## 7. 不变量（改代码前必读）

1. **ID 即文件名，永不改**：`load` 校验字段 ID 与文件名一致
   （task_manager.py:123-124）；要换 ID 只能新建；
2. **状态只进不退**：pending→in_progress 只有 claim，in_progress→completed
   只有 complete；没有 unclaim/uncomplete；
3. **依赖只在"pending 且未认领"窗口可改**（99-100），且 `update_dependencies`
   是唯一加边入口——DAG 无环由第 5 层校验守住；
4. **文件是唯一事实源**：save 整文件覆写，无内存缓存；两个 TaskManager
   实例同读 `.task` 也互不踩踏（但见 §9.2 的存在性竞态说明）；
5. **`from __future__ import annotations`（第 1 行）不可删**：原因见 §10.1；
6. schema 必须是 dict 且三处登记齐全（§6）。

## 8. 常量与外部约定速查

| 项 | 值 | 位置 |
|---|---|---|
| ID 正则 | `^task_[0-9a-f]{8}$` | task_manager.py:22（UPDATE_TASK schema 里的 `pattern` 是它的镜像，tools_manager.py:156/159） |
| ID 随机源 | `secrets.token_hex(4)` | task_manager.py:59 |
| create 重试 | 100 次 | task_manager.py:58 |
| 合法状态 | `pending/in_progress/completed` | task_manager.py:126 |
| 存储目录 | `<workDir>/.task` | env.py:19 |
| git 忽略 | `/.task` | .gitignore:9 |

## 9. 已知坑

以下 1/2/3/4/5 均已在仓库 `.venv`（Python 3.13.1, win32）实测复现，
非纸面推演：

1. **complete 的 owner 不符消息必崩**（task_manager.py:200）：错误消息里
   写的是 `Task.owner`（类属性）而非 `task.owner`——dataclass 无默认值的
   字段在类上不存在，触发即 `AttributeError: type object 'Task' has no
   attribute 'owner'`。工具层 owner 恒为 `"agent"` 走不到这条，但手工改
   过 owner 的文件会让 complete 穿透主循环。
2. **`_root` 的 `resolve()` 参数名不副实**（task_manager.py:31）：
   `self.directory.resolve(self.env.workDirPath)` 疑似想"相对路径以
   workDirPath 为基准解析"，但 `Path.resolve()` 的第一个位置参数是
   `strict`——传 Path 恒为真值，**等效 `strict=True`**；基准语义完全没
   发生（目录恒为绝对路径 env.py:19，也无从发生）。副作用：`.task` 目录
   不存在时，任何经 `_path`→`_root(create=False)` 的读操作
   （exists/load/claim/get/update）直接 `FileNotFoundError`。唯一幸免的是
   `list`——133-135 先判了 `directory.exists()` 返回 `[]`。正常运行时模型
   必先 create_task（建目录）才可能有 ID，故日常不触发；但**手工删掉
   `.task` 目录再让模型 claim/get 老任务**就会炸穿主循环。
3. **脏文件毒死 list()**（task_manager.py:140-141）：glob 模式
   `task_*.json` 比 ID 正则宽，混进一个 `task_ZZZ.json` 之类的文件，
   load 即 `ValueError: Invalid task ID`，`run_list_tasks` 无保护 →
   穿透主循环。实测复现。
4. **文件 schema 漂移 → `Task(**data)` TypeError**（task_manager.py:122）：
   JSON 里多一个键（实测 `foo` → `unexpected keyword argument`）或少一个
   键都崩，且不区分于业务错误。字段增删必须配套数据迁移，改格式前读这条。
5. **环检测的传递闭包会踩删号依赖**：`update_dependencies` 的存在性校验
   （107）只查**新加的直接依赖**；`_depends_on`（89）遍历传递闭包时对
   每个节点裸 `load`——若闭包深处有依赖已被手删，实测 `FileNotFoundError`
   穿透。与 `incomplete_dependencies` 的容错（166）宽严不对称，是设计
   不一致而非刻意为之。
6. **尾逗号 → 请求体 400**（历史，见 §6）：schema 类属性多写一个逗号变
   一元 tuple，网关拒整个请求。任何新工具 schema 上线前用
   `all(isinstance(t, dict) for t in tools)` 之类的方式过一遍。
7. **错误处理不对称**：claim/complete 返回文本（错误即数据，TOOLS_MANAGER.md
       §5 约定），create/update/get/list 封装却裸奔——模型传空 subject、野
   task_id 都会把 `TypeError` 之外的校验异常送进主循环火坑。统一收口时
   优先在 `run_*` 层补 `except` 转文本，而不是动 TaskManager 的 raise 语义。
8. **性能天花板**：`complete_task` 两遍全表扫（203-214），`can_start` 内
   又逐依赖 `load`；`update_dependencies` 的环检测按节点读盘。任务数 n、
   平均依赖 d 时约 O(n²·d) 次文件 IO。看板规模到几百之前无所谓。

## 10. 设计注记

1. **`list` 方法遮蔽内置名，全靠 `from __future__ import annotations` 续命**
   （task_manager.py:1 / 133）：类体里第 133 行定义了方法 `list`，从这一刻
   起类体命名空间中的 `list` 不再是内置类型。其后所有 def 语句在**定义时**
   本要求值的注解——`-> list[Task]`（154）、`-> list[str]`（160）——在没有
   PEP 563 时会对着那个函数对象下标，实测删掉第 1 行后类定义直接
   `TypeError: 'function' object is not subscriptable`。future import 把全部
   注解变成惰性字符串才安然无恙。两个相关边界：
   - 方法**体内**的 `list(dict.fromkeys(...))`（102）不受影响——函数体作用
     链不含类命名空间，`list` 直落内置（这正是 CPython 作用域规则的经典
     陷阱：遮蔽只咬类体求值期，不咬方法运行期）；
   - `@dataclass` 只字符串比对 `ClassVar/InitVar`，不 resolve
     `blockedBy: list[str]`（17），同样无感。
   结论：改名 `list` 或删第 1 行都会引爆；后来者勿动。
2. **输出形态双轨**：`get_task` 回完整 JSON（机读、可往返），
   `run_list_tasks` 渲染 `[ ]/[>]/[x]` 紧凑行（人读、省 token）——同一数据
   两个视角，各给所需。
3. **claim/complete 里 `print` 是给终端旁观者的进度条**（189/216/220），
   与返回给模型的文本冗余但无害；若将来做流式 UI，这两路要合并。
4. **`_depends_on` 用 `visited` 兜圈**：即便盘上已有环（手改文件可造出来），
   搜索也只停不报——它的前提是 §7 不变量 3 保证入图无环，对存量环不设防。
5. 兄弟文档 TOOLS_MANAGER.md 的行号坐标已按 598 行基线回填：schema 详解在
    其 §2，任务工具薄包装在其 §6.6；本文 §6 仍是 task 接线细节的权威主。

## 11. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `tools_manager.py` | 唯一消费方：`tools_manager.py:11` 导入、`tools_manager.py:217` 以 `env.taskDirPath` 构造；6 schema（136-202）、`self.tools` 注册（229-234）、`toolsHandlers` 路由（245-250）、`run_*` 封装（557-597）；子代理 `subTools`（252-258）不含 task |
| `env.py` | 提供 `taskDirPath = <workDir>/.task`（env.py:19）与工作区基准 `workDirPath`（`_root` 逃逸检查的参照物） |
| `.gitignore` | `/.task`（.gitignore:9）——运行时看板不入版本库 |
| `loop.py` | 无直接引用；6 工具经 `execute_tool` 常规路由执行（无 compact 式拦截） |
| `todo_write`（tools_manager 内） | 同仓库两套"任务"，互不感知：todo 是会话内视图，task 是跨会话看板；风格上也刻意没共用代码 |
| `hooks.py` / `permission.py` | 与所有工具一样，task 工具同样过 Pre/PostToolUse 闸门（TOOLS_MANAGER.md §5）——目前没有 hook 对文件写操作拦截 `.task` 的既有语义 |

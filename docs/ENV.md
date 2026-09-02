# Env 技术文档

> 对应源码：`env.py`（本仓库当前版本 16 行）
> 状态：完整，**全模块公用**（loop / tools_manager / hooks / permission
> 各自实例化；compact_manager 走构造注入不碰本模块）

## 1. 它解决什么问题

集中收口"进程级环境事实"：`.env` 加载、模型端点与型号、工作区位置、
三个派生目录。所有需要环境信息的类都从这里取值，避免散落的
`os.getenv` / `os.getcwd`。

## 2. 字段速查

| 字段 | 来源 | 用途 |
|---|---|---|
| `httpUrl` | `ANTHROPIC_BASE_URL` | `Anthropic(base_url=...)`；未设则 None（SDK 走官方端点） |
| `modelId` | `MODEL_ID` | 主循环与子代理 `messages.create` 的 model 参数 |
| `workDir` | `os.getcwd()`（str） | bash 子进程 cwd、系统提示词里的位置描述 |
| `workDirPath` | `Path.cwd()` | **工作区围栏基准**：permission 规则 1、`safe_path`、glob root 全都以它为界 |
| `skillsDirPath` | `<cwd>/skills` | 传给 `SkillManager` 构造（SKILL_MANAGER.md §2） |
| `transcriptDirPath` | `<cwd>/.transcripts` | 传给 `CompactManager`：对话归档 JSONL（COMPACT_MANAGER.md §3） |
| `toolResultsDirPath` | `<cwd>/.task_outputs/tool-results` | 传给 `CompactManager`：大工具结果落盘 |

`workDir` 与 `workDirPath` 是同一目录的两种形态（str / Path），
分别服务 shell 子进程和 pathlib 运算——改动时两者必须同步。

## 3. 构造时发生的两件事

```python
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
```

1. **每次实例化都重读 `.env`**，`override=True` 意味着 `.env` 的值
   强制覆盖当前进程环境变量；
2. **双鉴权互斥处理**：设置了自定义端点时，从 `os.environ` 里删掉
   `ANTHROPIC_AUTH_TOKEN`——custom 网关只认一种鉴权
   （`x-api-key`，即 SDK 稍后从环境变量自取的 `ANTHROPIC_API_KEY`），
   避免 bearer-token 与 api-key 同时发出被网关拒绝。
   **顺序依赖**：必须先建 `Env()` 再建 `Anthropic()` 客户端，SDK 是在
   客户端构造时才读取环境变量的（loop.py:14→16、tools_manager.py:134→141
   都恰好满足，改动构造顺序会静默失效）。

## 4. 不是单例：一次启动会 new 六个

无单例模式，每个持有方自己 `Env()`：

```
Loop.env (loop.py:14)
├─ Loop 的 Hooks.env (hooks.py:8)
│   └─ 其 Permission.env (permission.py:7)
└─ ToolsManager.env (tools_manager.py:134)
    ├─ 其 Hooks.env (hooks.py:8)
    │   └─ 其 Permission.env (permission.py:7)
```

（CompactManager 例外——目录由 loop.py:19-24 注入，不持有 Env。）

后果：启动时 `load_dotenv` 执行 6 次（幂等，只有微小开销）；
更重要的语义是**每个实例都是构造时刻的快照**——运行期改
`os.environ` 不会传导到已存在的任何 `Env()`。

## 5. cwd 语义（最大的隐形约定）

所有路径基准都是**进程启动目录**（`Path.cwd()`），不是仓库或源码位置：

- 从 `D:\other` 下运行 `python D:\...\lcc\loop.py`，则 skills 只会在
  `D:\other\skills` 找、transcript 会写到 `D:\other\.transcripts`、
  工作区围栏也以 `D:\other` 为界；
- permission 越界判定、`safe_path` 硬线、glob 过滤、skills 扫描四个
  安全/功能边界**共享这同一个基准**，一处漂移全体漂移。

## 6. 无校验（fail 点分布）

- `.env` 缺失、变量缺失均不报错——字段安静地为 `None`；
- `ANTHROPIC_API_KEY` 本模块不感知，由 SDK 在构造 `Anthropic()` 时自行
  校验缺失并抛错；
- `MODEL_ID` 缺失会一路活到第一次 `messages.create(model=None)` 才炸；
- 仓库没有 `.env.example` 模板；`.env` 在 `.gitignore` 中（本地存在但不入库），
  所需变量以上表 §2 为准：`ANTHROPIC_BASE_URL`、`MODEL_ID`
  （外加 SDK 侧的 `ANTHROPIC_API_KEY`）。

## 7. 运行时产物与仓库卫生

`.transcripts/`、`.task_outputs/` 两个派生目录已被 `.gitignore` 排除；
目录本身是懒创建（CompactManager 首次落盘才 mkdir），Env 只负责给路径
不负责建目录。

## 8. 不变量（改代码前必读）

1. 字段在构造时冻结：新增环境变量请保持同样的快照语义，或明确改成
   property 惰性读——混用会造成"有的字段热、有的冷"的暗坑；
2. `load_dotenv(override=True)` 的覆盖方向（.env > 进程环境）是现状约定，
   反转它会改变部署行为；
3. pop `ANTHROPIC_AUTH_TOKEN` 是对**全局** `os.environ` 的副作用，
   同进程其他组件若指望该变量需在此之后读取；
4. 围栏三目录一律从 `workDirPath` 派生，不要出现第二基准
   （如 `__file__` 所在目录）。

## 9. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `loop.py` | 建 Env → 建 client → 建 ToolsManager → 注入 CompactManager（顺序依赖见 §3.2） |
| `tools_manager.py` | 持 Env；bash 用 `workDir`，文件围栏用 `workDirPath` |
| `permission.py` | 规则 1 的围栏基准（PERMISSION.md §4） |
| `skill_manager.py` | 不直接用 Env，目录经构造参数传入（tools_manager.py:142 取 `skillsDirPath`） |
| `compact_manager.py` | 两个产物目录路径的注入来源 |
| `.gitignore` | 排除 `.env`、`.transcripts/`、`.task_outputs/` |

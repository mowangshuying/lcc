# lcc

用 `anthropic` SDK 直连模型，逐级构建编码 Agent 的完整链路：工具调用循环 → 文件沙箱 → 分级权限审批 → Hooks → 子代理 → 会话压缩 → 技能注入。全部逻辑可读、可断点、可逐 commit 追溯。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`.env`（已被 git 忽略）：

```ini
ANTHROPIC_API_KEY=sk-ant-...    # 必填
MODEL_ID=qwen3.8-flash          # 任意 Anthropic 协议端点
ANTHROPIC_BASE_URL=             # 可选，走网关时设置
```

```powershell
python loop.py    # 启动目录即沙箱根；q / exit 退出
```

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `loop.py` | 主循环：用户输入 → 模型调用 → 工具执行 → 压缩/Hooks 接线（当前阶段 `s12`） |
| `env.py` | `.env` 配置集中加载，供各模块共享 |
| `log.py` | 统一控制台日志输出：分级 info/warn/error、域 tag、颜色拼装单一事实源 |
| `tools_manager.py` | 工具注册与执行，内置子代理（`task`）独立执行循环 |
| `background_tasks_manager.py` | 后台 bash 任务：异步启动、结果收割与 `<task_notification>` 注入 |
| `cron_scheduler.py` | 定时任务调度：cron 表达式匹配、到期投递队列与 durable 落盘 |
| `permission.py` | 分级权限审批，注册为 PreToolUse 第一顺位回调 |
| `hooks.py` | UserPromptSubmit / PreToolUse / Stop 钩子总线 |
| `compact_manager.py` | 会话压缩：主动 + 反应式，transcript 与工具结果落盘 |
| `skill_manager.py` | 技能发现与 `load_skill` 工具，目录注入系统提示 |
| `memory_manager.py` | 长期记忆：检索注入、会话提取与合并 |
| `task_manager.py` | 持久化任务管理：依赖图、状态机与任务工具接线 |
| `message_bus.py` | 文件邮箱消息总线：JSONL 追加/破坏性读取，Lead 与 teammate 线程间唯一通信通道 |
| `worktree_manager.py` | git worktree 隔离区：一任务一工作目录，注册表校验与创建/移除闸门 |
| `agent_teams_manager.py` | agent teams 编排：常驻 teammate 线程、共享任务板认领、计划审批与关停协议 |
| `mcp_manager.py` | MCP 服务器连接与工具池动态组装：connect 发现、`mcp__` 前缀命名、宿主策略授权 |
| `color.py` | 终端颜色常量 |
| `tool_names.py` | 工具名常量（零依赖叶子），跨模块名字比较的单一事实源 |
| `skills/` | 技能目录（含 `greeting` 示例） |

## 文档索引

| 文档 | 对应源码 |
| --- | --- |
| [TOOLS_MANAGER.md](docs/TOOLS_MANAGER.md) | `tools_manager.py` |
| [BACKGROUND_TASKS_MANAGER.md](docs/BACKGROUND_TASKS_MANAGER.md) | `background_tasks_manager.py` |
| [CRON_SCHEDULER.md](docs/CRON_SCHEDULER.md) | `cron_scheduler.py` |
| [PERMISSION.md](docs/PERMISSION.md) | `permission.py` |
| [HOOKS.md](docs/HOOKS.md) | `hooks.py` |
| [COMPACT_MANAGER.md](docs/COMPACT_MANAGER.md) | `compact_manager.py` |
| [SKILL_MANAGER.md](docs/SKILL_MANAGER.md) | `skill_manager.py` |
| [ENV.md](docs/ENV.md) | `env.py` |
| [TASK_MANAGER.md](docs/TASK_MANAGER.md) | `task_manager.py` |
| [AGENT_TEAMS.md](docs/AGENT_TEAMS.md) | `message_bus.py` / `worktree_manager.py` / `agent_teams_manager.py` |
| [MCP_TOOLS.md](docs/MCP_TOOLS.md) | `mcp_manager.py` |
| [MEMORY_MANAGER.md](docs/MEMORY_MANAGER.md) | `memory_manager.py` |
| [PROMPT.md](docs/PROMPT.md) | 测试专用提示词（手工验证用） |

## 参考

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) — 本仓库为其学习记录，实现思路可对照其源码与提交历史。

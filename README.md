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
| `loop.py` | 主循环：用户输入 → 模型调用 → 工具执行 → 压缩/Hooks 接线（当前阶段 `s08`） |
| `env.py` | `.env` 配置集中加载，供各模块共享 |
| `tools_manager.py` | 工具注册与执行，内置子代理（`task`）独立执行循环 |
| `permission.py` | 分级权限审批，注册为 PreToolUse 第一顺位回调 |
| `hooks.py` | UserPromptSubmit / PreToolUse / Stop 钩子总线 |
| `compact_manager.py` | 会话压缩：主动 + 反应式，transcript 与工具结果落盘 |
| `skill_manager.py` | 技能发现与 `load_skill` 工具，目录注入系统提示 |
| `color.py` | 终端颜色常量 |
| `skills/` | 技能目录（含 `greeting` 示例） |

## 文档索引

| 文档 | 对应源码 |
| --- | --- |
| [TOOLS_MANAGER.md](docs/TOOLS_MANAGER.md) | `tools_manager.py` |
| [PERMISSION.md](docs/PERMISSION.md) | `permission.py` |
| [HOOKS.md](docs/HOOKS.md) | `hooks.py` |
| [COMPACT_MANAGER.md](docs/COMPACT_MANAGER.md) | `compact_manager.py` |
| [SKILL_MANAGER.md](docs/SKILL_MANAGER.md) | `skill_manager.py` |
| [ENV.md](docs/ENV.md) | `env.py` |
| [PROMPT.md](docs/PROMPT.md) | 测试专用提示词（手工验证用） |

## 参考

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) — 本仓库为其学习记录，实现思路可对照其源码与提交历史。

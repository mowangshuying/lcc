### 工具名常量：全仓库唯一事实源
### 零依赖叶子模块（不 import 任何本仓模块），供 tools_manager / permission /
### hooks / background_tasks_manager / loop 共享，消除工具名字符串跨模块隐式契约。
### 值必须与注册到 Anthropic tools API 的 schema "name" 逐字一致（线上协议字段）。

BASH = "bash"
READ_FILE = "read_file"
WRITE_FILE = "write_file"
EDIT_FILE = "edit_file"
GLOB = "glob"
TODO_WRITE = "todo_write"
TASK = "task"
LOAD_SKILL = "load_skill"
COMPACT = "compact"
CREATE_TASK = "create_task"
UPDATE_TASK = "update_task"
LIST_TASKS = "list_tasks"
GET_TASK = "get_task"
CLAIM_TASK = "claim_task"
COMPLETE_TASK = "complete_task"
SCHEDULE_CRON = "schedule_cron"
LIST_CRONS = "list_crons"
CANCEL_CRON = "cancel_cron"
SPAWN_TEAMMATE = "spawn_teammate"
LIST_TEAMMATES = "list_teammates"
SEND_MESSAGE = "send_message"
REQUEST_SHUTDOWN = "request_shutdown"
REQUEST_PLAN = "request_plan"
REVIEW_PLAN = "review_plan"
CREATE_WORKTREE = "create_worktree"
SUBMIT_PLAN = "submit_plan"
CONNECT_MCP = "connect_mcp"

### MCP 动态工具名前缀（对照 s14 "mcp__{server}__{tool}"）。
### 定义在本模块而非 mcp_manager：permission.py 需要在不依赖 mcp_manager 的前提下
### 识别前缀，单一事实源，避免 "mcp__" 字符串在两个模块各写一遍的隐式契约。
MCP_PREFIX = "mcp__"

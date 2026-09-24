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

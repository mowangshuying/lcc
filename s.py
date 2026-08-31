import os
import subprocess
from anthropic import Anthropic
from dotenv import load_dotenv
from pathlib import Path
import glob
import re
import json
import ast

### 常用的文字颜色
# 30: 黑
# 31：红
# 32：绿
# 33：黄
# 34：蓝色
# 35: 洋红
# 36：青
# 37：白

### 案例
### \001 \033[0m \002
COLOR_DEFAULT = "\001\033[0m\002"
COLOR_BLACK = "\001\033[30m\002"
COLOR_RED = "\001\033[31m\002"
COLOR_GREEN = "\001\033[32m\002"
COLOR_YELLOW = "\001\033[33m\002"
COLOR_BLUE = "\001\033[34m\002"
COLOR_MAGENTA = "\001\033[35m\002"
COLOR_CYAN = "\001\033[36m\002"
COLOR_WHITE = "\001\033[37m\002"


### 加载一些全局变量
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

HTTPURL = os.getenv("ANTHROPIC_BASE_URL")
MODELID = os.getenv("MODEL_ID")
CLIENT = Anthropic(base_url=HTTPURL)

### dir
WORKDIR = os.getcwd()
WORKDIRPATH = Path.cwd()

### 提示词
SYSTEM_PROMPT = (  f"You are a coding agent at {WORKDIR}."
                    "Before starting any multi-step task, use todo_write to plan your steps."
                    "Update status as you go."
                    "Use task for focused exploration or a self-contained subtask."
                  )

SUB_SYSTEM_PROMPT = (
    f"You are coding agent at {WORKDIR}."
    # 完成指定任务后，返回一个简洁的最终答案
    "Complete the given task, then return a concise final answer"
)



### 工具定义
TOOLS = [
    ### bash
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    ### read_file
    {
        "name": "read_file",
        "description": "Read file contents",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    ### write_file
    {
        "name": "write_file",
        "description": "Write content to a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    ### edit_file
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required":["path", "old_text", "new_text"]
        },
    },
    ### glob
    {
        "name": "glob",
        "description": "Find files matching a glob pattern; ** matches recursively.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    
    ### todo_write
    {
        "name":"todo_write",
        "description": "Create and manage a task list for your current coding session.",
        "input_schema":{
            "type":"object",
            "properties":{
                "todos":{
                    "type":"array",
                    "maxItems":20,
                    "items":{
                        "type":"object",
                        "properties":{
                            "content":{
                                "type":"string",
                                "minLength":1,
                            },
                            "status":{
                                "type":"string",
                                "enum":["pending", "in_progress", "completed"]
                            }
                        }
                    }
                }
            },
            "required":["todos"]
        }
    },
    
    ### task
    {
        "name": "task",
        "description": "Run a subagent with fresh conversation context and return its final text.",
        "input_schema": {
            "type": "object",
            "properties":{
                "prompt": {
                    "type":"string",
                    "minLength":1
                }
            },
            "required":["prompt"]
        }
    }
]

### 工具函数抽出
def execute_tool(block, handlers: dict) -> str:
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)

    ### 工具路由
    handler = handlers.get(block.name)
    if not handler:
        output = f"Unknown:{block.name}"
    else:
        output = handler(**block.input)

    trigger_hooks("PostToolUse", block, output)
    return str(output)


### 工具函数
### bash
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

    found = False
    for d in dangerous:
        if d in command:
            found = True
            break
    if found:
        return "Error: Dangerous command blocked"

    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        if out:
            return out[:50000]
        else:
            return "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout(120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error:{e}"


def safe_path(p: str) -> Path:
    path = (WORKDIRPATH / p).resolve()
    if not path.is_relative_to(WORKDIRPATH):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


### read_file
def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error:{e}"


### write_file
def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error{e}"


### edit_file
def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error:{e}"


### glob
def run_glob(pattern: str) -> str:
    try:
        matches = []
        for match in glob.glob(pattern, root_dir=WORKDIRPATH, recursive=True):
            if (WORKDIRPATH / match).resolve().is_relative_to(WORKDIRPATH):
                matches.append(match)
        matches = sorted(matches)
        shown = matches[:200]
        if len(matches) > 200:
            ### 已省略更多结果，请缩小匹配范围
            shown.append("...(more matches omitted; narrow the pattern)")

        if len(matches) == 0:
            return "(no matches)"

        return "\n".join(shown)
    except Exception as e:
        return f"Error:{e}"


### todo list;
class TodoManager:
    def __init__(self):
        self.items: list[dict] = []
        
    def update(self, todos: list | str) -> str:
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError) as e:
                    raise ValueError("todos must be a list or JSON array string") from e
                
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        
        if len(todos) > 20:
            raise ValueError("Max 20 todos allowed")
        
        validated = []
        in_progress_count = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                raise ValueError(f"todos[{index}] must be an object")
            
            content = str(todo.get("content", "")).strip()
            status = str(todo.get("status", "pending")).lower()
            if not content:
                raise ValueError(f"todos[{index}] requires content")
            
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"todos[{index}] has invalid status '{status}'")
            
            if status == "in_progress":
                in_progress_count += 1
                
            validated.append({"content":content, "status":status})
            
        if in_progress_count > 1:
            raise ValueError("Only one todo can be in_progress at a time")
        
        self.items = validated
        return self.render()
    
    def render(self) -> str:
        if not self.items:
            return "No todos"
        
        lines = []
        for todo in self.items:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed":"[x]"
            }[todo["status"]]
            lines.append(f"{marker} {todo['content']}")
        
        done = 0
        for todo in self.items:
            if todo["status"] == "completed":
                done += 1
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)
        
TODO = TodoManager()


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    
    texts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            texts.append(getattr(block, "text", ""))
            
    if len(texts) == 0:
        return "(no summary)"
    
    return "\n".join(texts)


def run_todo_write(todos: list | str) -> str:
    try:
        output = TODO.update(todos)
    except ValueError as e:
        return f"Error:{e}"
    print(f"\n{COLOR_MAGENTA} Current Tasks \n {output}")
    return output

### subagent
def run_subagent(prompt:str) -> str:
    messages = [{"role":"user", "content":prompt}]
    for _ in range(50):
        response = CLIENT.messages.create(
            model=MODELID,
            system=SUB_SYSTEM_PROMPT,
            messages=messages,
            tools=SUB_TOOLS,
            max_tokens=8000
        )
        
        messages.append({"role":"assistant", "content": response.content})
        
        tool_calls = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(block)
                
        if len(tool_calls) == 0:
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role":"user", "content": force})
                continue
            
            output =  extract_text(response.content)
            print(f"{COLOR_MAGENTA} [Subagent done] {COLOR_DEFAULT}")
            return output
        
        results = []
        # for block in tool_calls:
        for block in tool_calls:
            output = execute_tool(block, SUB_HANDLERS)
            results.append({
                "type":"tool_result",
                "tool_use_id":block.id,
                "content":output
            })
            
        messages.append({"role":"user", "content":results})
    print(f"{COLOR_MAGENTA} [Subagent stopped]{COLOR_DEFAULT}")
    return "Subagent stopped after 50 turns without a final answer."
            
        
        
        

### 工具路由
HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write":run_todo_write,
    "task": run_subagent,
}


### 检查bash列表


### 硬编码禁止列表 总是禁止
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]


def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Permission denied by deny list"
    return None


### (?i) 忽略大小写
### (?:^|[;&|()\n{}"'`]) 匹配字符串的开头、分隔符号/花括号或引号（覆盖 powershell -Command "..." 与 & {...} 嵌套写法）
### \s* 多个空白符号
### (?:rm|del|erase|ri|rmdir|rd|Remove-Item) 匹配各类删除命令及其别名（ri/rd 为 PowerShell 别名，erase/rmdir 为 cmd 内置）
### (?=\s|$|[;&|(){}]) 往后看一眼，确认后面紧跟的是空格，字符串结尾(后面没有字符了)，或者特定的分隔符号
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"""(?i)(?:^|[;&|()\n{}"'`])\s*(?:rm|del|erase|ri|rmdir|rd|Remove-Item)(?=\s|$|[;&|(){}])"""
)


### 是否包含破坏性命令
def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


PERMISSION_RULES = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": lambda args: not (WORKDIRPATH / args.get("path", ""))
        .resolve()
        .is_relative_to(WORKDIRPATH),
        "message": "Writing outside workspace",
    },
    {
        "tools": ["bash"],
        "check": lambda args: contains_destructive_command(args.get("command", ""))
        or any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
        "message": "Potentially destructive command",
    },
]


### 检查规则
def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        if (tool_name in rule["tools"]) and rule["check"](args):
            return rule["message"]
    return None


def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n{COLOR_YELLOW}[permission]{reason}{COLOR_DEFAULT}")
    print(f"    {COLOR_YELLOW}Tool: {tool_name}({args}){COLOR_DEFAULT}")
    choice = input(f"    {COLOR_YELLOW}Allow? [Y/N]{COLOR_CYAN}").strip().lower()
    if choice in ("y", "yes"):
        return "allow"
    return "deny"


def check_permission(block) -> bool:
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"{COLOR_RED}{reason}{COLOR_DEFAULT}")
            return reason

    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            print(f"{COLOR_RED}Permission denied by user")
            return "Permission denied by user"
    return None


HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def permission_hook(block):
    return check_permission(block)


def log_before_use_tool_hook(block):
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"{COLOR_DEFAULT}[HOOK] {block.name}({args_preview}) {COLOR_DEFAULT}")
    return None


def log_after_use_tool_hook(block, output):
    # ### use tool info
    info = ""
    if block.name == "bash":
        info = f"command: {block.input['command']}"
    elif block.name == "read_file":
        info = f"path: {block.input['path']}"
    elif block.name == "write_file":
        info = f"path: {block.input['path']}"
    elif block.name == "edit_file":
        info = f"path: {block.input['path']}"
    elif block.name == "glob":
        info = f"pattern: {block.input['pattern']}"
    elif block.name == "todo_write":
        info = f"update task list:"
    elif block.name == "task":
        info = f"task: {block.input.get('prompt', '')}"
    
    print(f"{COLOR_GREEN}[HOOK] tool_use: {block.name} - {info} {COLOR_DEFAULT}")
    print(f"{COLOR_DEFAULT}[HOOK]tool_result:\n{output}{COLOR_DEFAULT}")


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(
            f"{COLOR_DEFAULT}[HOOK] Large output from {block.name}: {len(str(output))} chars {COLOR_DEFAULT}"
        )
    return None


def context_inject_hook(query: str):
    print(
        f"{COLOR_DEFAULT}[HOOK] UserPromtSubmit: working in {WORKDIRPATH} {COLOR_DEFAULT}"
    )


def summary_hook(messages: list):
    tool_count = 0
    for message in messages:
        if isinstance(message.get("content"), list):
            for block in message.get("content"):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_count += 1
    print(
        f"{COLOR_DEFAULT}[HOOK] Stop: session used {tool_count} tool calls {COLOR_DEFAULT}"
    )
    return None


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_before_use_tool_hook)
register_hook("PostToolUse", log_after_use_tool_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)

### New in s0
SUB_TOOLS = [
    ### bash
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    ### read_file
    {
        "name": "read_file",
        "description": "Read file contents",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    ### write_file
    {
        "name": "write_file",
        "description": "Write content to a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    ### edit_file
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required":["path", "old_text", "new_text"]
        },
    },
    ### glob
    {
        "name": "glob",
        "description": "Find files matching a glob pattern; ** matches recursively.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    }
]

SUB_HANDLERS =  {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


### loop;
def loop(messages: list):
    rounds_since_todo = 0
    while True:
        response = CLIENT.messages.create(
            model=MODELID,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        ### 将返回内容重新添加至message列表中
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if len(tool_calls) == 0:
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        results = []
        used_todo = False
        for block in tool_calls:
            output = execute_tool(block, HANDLERS)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
            
            if block.name == "todo_write":
                used_todo = True
            
        if not used_todo:
            rounds_since_todo += 1
        else:
            rounds_since_todo = 0
        
        if rounds_since_todo >= 3:
            results.append({
                "type":"text",
                "text":"<reminder>Update your todos.</reminder>"
            })
            rounds_since_todo = 0
            
        messages.append({"role": "user", "content": results})


### 主函数
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input(f"{COLOR_CYAN}s05>>")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})

        loop(history)

        lst_content = history[-1]["content"]
        if isinstance(lst_content, list):
            for block in lst_content:
                if getattr(block, "type", None) == "text":
                    print(f"{COLOR_DEFAULT}text:{block.text}{COLOR_DEFAULT}")

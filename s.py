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
color_default = "\001\033[0m\002"
color_black = "\001\033[30m\002"
color_red = "\001\033[31m\002"
color_green = "\001\033[32m\002"
color_yellow = "\001\033[33m\002"
color_blue = "\001\033[34m\002"
color_magenta = "\001\033[35m\002"
color_cyan = "\001\033[36m\002"
color_white = "\001\033[37m\002"


### 加载一些全局变量
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

g_httpUrl = os.getenv("ANTHROPIC_BASE_URL")
# g_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
g_modelId = os.getenv("MODEL_ID")
g_client = Anthropic(base_url=g_httpUrl)

### dir
g_workDir = os.getcwd()
g_workDirPath = Path.cwd()

### 提示词
g_systemPrompt = (  f"You are a coding agent at {g_workDir}."
                    "Before starting any multi-step task, use todo_write to plan your steps."
                    "Update status as you go."
                  )



### 工具定义
g_tools = [
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
        },
    },
    ### glob
    {
        "name": "glob",
        "description": "Find files matching a glob pattern; ** matches recursively.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "require": ["pattern"],
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
    }
]


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
            cwd=g_workDir,
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
    path = (g_workDirPath / p).resolve()
    if not path.is_relative_to(g_workDirPath):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


### read_file
def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit}) more lines)"]
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
        for match in glob.glob(pattern, root_dir=g_workDirPath, recursive=True):
            if (g_workDirPath / match).resolve().is_relative_to(g_workDirPath):
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

def run_todo_write(todos: list | str) -> str:
    try:
        output = TODO.update(todos)
    except ValueError as e:
        return f"Error:{e}"
    print(f"\n{color_magenta} Current Tasks \n {output}")
    return output

### 工具路由
g_toolHandlers = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write":run_todo_write
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
        "check": lambda args: not (g_workDirPath / args.get("path", ""))
        .resolve()
        .is_relative_to(g_workDirPath),
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
    print(f"\n{color_yellow}[permission]{reason}{color_default}")
    print(f"    {color_yellow}Tool: {tool_name}({args}){color_default}")
    choice = input(f"    {color_yellow}Allow? [Y/N]{color_blue}").strip().lower()
    if choice in ("y", "yes"):
        return "allow"
    return "deny"


def check_permission(block) -> bool:
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"{color_red}{reason}{color_default}")
            return reason

    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            print(f"{color_red}Permission denied by user")
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
    print(f"{color_default}[HOOK] {block.name}({args_preview}) {color_default}")
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
    print(f"{color_green}[HOOK] tool_use: {block.name} - {info} {color_default}")
    print(f"{color_default}[HOOK]tool_result:\n{output}{color_default}")


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(
            f"{color_default}[HOOK] Large output from {block.name}: {len(str(output))} chars {color_default}"
        )
    return None


def context_inject_hook(query: str):
    print(
        f"{color_default}[HOOK] UserPromtSubmit: working in {g_workDirPath} {color_default}"
    )


def summary_hook(messages: list):
    tool_count = 0
    for message in messages:
        if isinstance(message.get("content"), list):
            for block in message.get("content"):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_count += 1
    print(
        f"{color_default}[HOOK] Stop: session used {tool_count} tool calls {color_default}"
    )
    return None


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_before_use_tool_hook)
register_hook("PostToolUse", log_after_use_tool_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


### loop;
def loop(messages: list):
    while True:
        response = g_client.messages.create(
            model=g_modelId,
            system=g_systemPrompt,
            messages=messages,
            tools=g_tools,
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
                messages.append({"role": "user", "conent": force})
            return

        results = []
        used_todo = False
        for block in tool_calls:
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(blocked),
                    }
                )
                continue

            ### 工具路由
            handler = g_toolHandlers.get(block.name)
            if not handler:
                output = f"Unknown:{block.name}"
            else:
                output = handler(**block.input)

            trigger_hooks("PostToolUse", block, output)
            
            if block.name == "todo_write":
                used_todo = True
            
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )
            
        rounds_since_todo = 0
        if not used_todo:
            rounds_since_todo += 1
        
        if rounds_since_todo >= 3:
            results.append({
                "type":"text",
                "text":"<reminder>Update your todos.</remider>"
            })
            rounds_since_todo = 0
            
        messages.append({"role": "user", "content": results})


### 主函数
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input(f"{color_blue}s05>>")
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
                    print(f"{color_default}text:{block.text}{color_default}")

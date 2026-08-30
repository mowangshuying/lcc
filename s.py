import os
import subprocess

from anthropic import Anthropic
from dotenv import load_dotenv
from pathlib import Path
import glob
import re

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
g_systemPrompt = f"You are a coding agent at {g_workDir}. Use tools to solve tasks. Act, don't explain."


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


### 工具路由
g_toolHandlers = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


### 检查bash列表


### 硬编码禁止列表 总是禁止
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]


def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: {pattern} is on the deny list"
    return None


### (?i) 忽略大小写
### (?:^|[;&|()\n]) 匹配字符串的开头或者特定的分隔符号
### \s* 多个空白符号
### (?:rm|del) 匹配rm或者del
### (?=\s|$|[;&|()]) 往后看一眼，确认后面紧跟的是空格，字符串结尾(后面没有字符了)，或者特定的分隔符号
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del|Remove-Item)(?=\s|$|[;&|()])"
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

def ask_user(tool_name: str, args: dict, reason:str) -> str:
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
            return False
        
    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True
            


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
            return

        results = []
        for block in tool_calls:
            if not check_permission(block):
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Permission denied",
                    }
                )
                
                print(f"{color_red}deny: tool_use_id={block.id}")
                continue

            ### use tool info
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
            print(f"{color_green}tool_use: {block.name} - {info} {color_default}")

            ### 工具路由
            handler = g_toolHandlers.get(block.name)
            if not handler:
                output = f"Unknown:{block.name}"
                print(f"{color_red}tool_result:{output}{color_default}")
            else:
                output = handler(**block.input)
                print(f"{color_default}tool_result:{output}{color_default}")
                
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )
        messages.append({"role": "user", "content": results})


### 主函数
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input(f"{color_blue}s03>>")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})

        loop(history)

        lst_content = history[-1]["content"]
        if isinstance(lst_content, list):
            for block in lst_content:
                if getattr(block, "type", None) == "text":
                    print(f"{color_default}text:{block.text}{color_default}")

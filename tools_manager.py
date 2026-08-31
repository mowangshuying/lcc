import subprocess
from pathlib import Path
import glob
import os
import json
import ast
from hooks import Hooks
from anthropic import Anthropic
from env import Env


class ToolsManager:
    BASH = {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }

    READ_FILE = {
        "name": "read_file",
        "description": "Read file contents",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    }

    WRITE_FILE = {
        "name": "write_file",
        "description": "Write content to a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    }

    EDIT_FILE = {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    }

    GLOB = {
        "name": "glob",
        "description": "Find files matching a glob pattern; ** matches recursively.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    }

    TODO_WRITE = {
        "name": "todo_write",
        "description": "Create and manage a task list for your current coding session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                    },
                }
            },
            "required": ["todos"],
        },
    }

    TASK = {
        "name": "task",
        "description": "Run a subagent with fresh conversation context and return its final text.",
        "input_schema": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "minLength": 1}},
            "required": ["prompt"],
        },
    }

    MAX_SUBAGENT_TURNS = 50

    def __init__(self):
        self.env = Env()
        self.subSystemPrompt = (
            f"You are a coding agent at {self.env.workDir}."
            " Complete the given task, then return a concise final answer."
        )
        
        self.hooks = Hooks()
        self.client = Anthropic(base_url=self.env.httpUrl)

        self.tools = [
            self.bash_info(),
            self.read_file_info(),
            self.write_file_info(),
            self.edit_file_info(),
            self.glob_info(),
            self.todo_write_info(),
            self.task_info(),
        ]
        self.toolsHandlers = {
            "bash": self.run_bash,
            "read_file": self.run_read,
            "write_file": self.run_write,
            "edit_file": self.run_edit,
            "glob": self.run_glob,
            "todo_write": self.run_todo_write,
            "task": self.run_subagent,
        }
        self.subTools = [
            self.bash_info(),
            self.read_file_info(),
            self.write_file_info(),
            self.edit_file_info(),
            self.glob_info(),
        ]
        self.subToolsHandlers = {
            "bash": self.run_bash,
            "read_file": self.run_read,
            "write_file": self.run_write,
            "edit_file": self.run_edit,
            "glob": self.run_glob,
        }

    def safe_path(self, p: str) -> Path:
        path = (self.env.workDirPath / p).resolve()
        if not path.is_relative_to(self.env.workDirPath):
            raise ValueError(f"Path escapes workspace: {p}")
        return path
    
    ## 工具函数抽出
    def execute_tool(self, block, handlers: dict) -> str:
        blocked = self.hooks.trigger_hooks("PreToolUse", block)
        if blocked:
            return str(blocked)

        ### 工具路由
        handler = handlers.get(block.name)
        if not handler:
            output = f"Unknown:{block.name}"
        else:
            output = handler(**block.input)

        self.hooks.trigger_hooks("PostToolUse", block, output)
        return str(output)

    def bash_info(self):
        return self.BASH

    def read_file_info(self):
        return self.READ_FILE

    def write_file_info(self):
        return self.WRITE_FILE

    def edit_file_info(self):
        return self.EDIT_FILE

    def glob_info(self):
        return self.GLOB

    def todo_write_info(self):
        return self.TODO_WRITE

    def task_info(self):
        return self.TASK

    ### bash
    def run_bash(self, command: str) -> str:
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
                cwd=self.env.workDir,
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

    def safe_path(self, p: str) -> Path:
        path = (self.env.workDirPath / p).resolve()
        if not path.is_relative_to(self.env.workDirPath):
            raise ValueError(f"Path escapes workspace: {p}")
        return path

    ### read_file
    def run_read(self, path: str, limit: int | None = None) -> str:
        try:
            lines = self.safe_path(path).read_text(encoding="utf-8").splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as e:
            return f"Error:{e}"

    ### write_file
    def run_write(self, path: str, content: str) -> str:
        try:
            file_path = self.safe_path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error{e}"

    ### edit_file
    def run_edit(self, path: str, old_text: str, new_text: str) -> str:
        try:
            file_path = self.safe_path(path)
            text = file_path.read_text(encoding="utf-8")
            if old_text not in text:
                return f"Error: text not found in {path}"
            file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            return f"Edited {path}"
        except Exception as e:
            return f"Error:{e}"

    ### glob
    def run_glob(self, pattern: str) -> str:
        try:
            matches = []
            for match in glob.glob(pattern, root_dir=self.env.workDirPath, recursive=True):
                if (self.env.workDirPath / match).resolve().is_relative_to(self.env.workDirPath):
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
    
    ### update todos
    def update_todos(self, todos: list | str) -> str:
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
        
        
        if not validated:
            return "No todos"
        
        lines = []
        for todo in validated:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed":"[x]"
            }[todo["status"]]
            lines.append(f"{marker} {todo['content']}")
        
        done = 0
        for todo in validated:
            if todo["status"] == "completed":
                done += 1
        lines.append(f"\n({done}/{len(validated)} completed)")
        return "\n".join(lines)
        
    ### todo_write
    def run_todo_write(self, todos: list | str ) -> str:
        try:
            output = self.update_todos(todos)
        except ValueError as e:
            return f"Error:{e}"
        return output
        
    ### 提取文本块
    def extract_text(self, content) -> str:
        if not isinstance(content, list):
            return str(content)

        texts = []
        for block in content:
            if getattr(block, "type", None) == "text":
                texts.append(getattr(block, "text", ""))

        if len(texts) == 0:
            return "(no summary)"

        return "\n".join(texts)

    ### run_subagent
    def run_subagent(self, prompt: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        for _ in range(self.MAX_SUBAGENT_TURNS):
            try:
                response = self.client.messages.create(
                    model=self.env.modelId,
                    system=self.subSystemPrompt,
                    messages=messages,
                    tools=self.subTools,
                    max_tokens=8000,
                )
            except Exception as e:
                return f"Error: subagent API call failed: {e}"

            messages.append({"role": "assistant", "content": response.content})

            tool_calls = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_calls.append(block)

            ### 无工具调用即最终回答
            if len(tool_calls) == 0:
                force = self.hooks.trigger_hooks("Stop", messages)
                if force:
                    messages.append({"role": "user", "content": force})
                    continue
                output =  self.extract_text(response.content)
                return output

            results = []
            for block in tool_calls:
                output = self.execute_tool(block, self.subToolsHandlers)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

            messages.append({"role": "user", "content": results})

        return f"Subagent stopped after {self.MAX_SUBAGENT_TURNS} turns without a final answer."

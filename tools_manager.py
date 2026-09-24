import copy
import subprocess
from pathlib import Path
import glob
import os
import json
import ast
from hooks import Hooks
from anthropic import Anthropic
from env import Env
from skill_manager import SkillManager
from task_manager import TaskManager, Task
from dataclasses import asdict, dataclass
from background_tasks_manager import BackgroundTasksManager
from cron_scheduler import *
from permission import Permission



class ToolsManager:
    BASH = {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "run_in_background":{"type": "boolean"},
            },
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
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
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
    
    LOAD_SKILL = {
        "name": "load_skill",
        "description": "Load the full SKILL.md content by skill name.",
        "input_schema": {
            "type": "object",
            "properties":{
                "name" : {
                    "type": "string"
                }
            },
            "required": ["name"],
        }
    }
    
    COMPACT = {
        "name" : "compact",
        "description" : "Summarize earlier conversation to free context space",
        "input_schema" : {
            "type" : "object",
            "properties" : {
                ## EMPTY
            }
        }
    }


    #### task_manager
    CREATE_TASK = {
        "name": "create_task",
        "description": "Create a task and return its runtime-generated ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["subject"],
            "additionalProperties": False,
        },
    }

    UPDATE_TASK = {
        "name": "update_task",
        "description": "Add dependencies using IDs returned by create_task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                "addBlockedBy": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                    "minItems": 1,
                },
            },
            "required": ["task_id", "addBlockedBy"],
            "additionalProperties": False,
        },
    }

    LIST_TASKS = {
        "name": "list_tasks",
        "description": "List tasks with status, owner, and dependencies.",
        "input_schema": {"type": "object", "properties": {}},
    }

    GET_TASK = {
        "name": "get_task",
        "description": "Get a task by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    }

    CLAIM_TASK = {
        "name": "claim_task",
        "description": "Claim a pending task whose dependencies are complete.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    }

    COMPLETE_TASK = {
        "name": "complete_task",
        "description": "Complete the task claimed by this agent.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    }
    
    SCHEDULE_CRON = {
        "name": "schedule_cron",
        "description": "Schedule a prompt with a 5-field cron expression.",
        "input_schema": {
                "type": "object",
                "properties": {
                                    "cron": {"type": "string"},
                                    "prompt": {"type": "string"},
                                    "recurring": {"type": "boolean"},
                                    "durable": {"type": "boolean"}
                          },
                "required": ["cron", "prompt"]}}
    
    LIST_CRONS = {
        "name": "list_crons", 
        "description": "List scheduled cron jobs.",
        "input_schema": {
            "type": "object", 
            "properties": {}, 
            "required": []
            }}

    
    CANCEL_CRON = {
        "name": "cancel_cron", 
        "description": "Cancel a cron job by ID.",
        "input_schema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"]
            }}


    MAX_SUBAGENT_TURNS = 50

    def __init__(self, hooks: Hooks):
        self.env = Env()
        self.subSystemPrompt = (
            f"You are a coding agent at {self.env.workDir}."
            " Complete the given task, then return a concise final answer."
        )
        
        self.hooks = hooks
        self.client = Anthropic(base_url=self.env.httpUrl)
        self.skillManager = SkillManager(self.env.skillsDirPath)
        self.taskManager  = TaskManager(self.env.taskDirPath)
        self.backgroundTasksManager = BackgroundTasksManager()
        self.cronScheduler = CronScheduler()

        self.tools = [
            self.bash_info(),
            self.read_file_info(),
            self.write_file_info(),
            self.edit_file_info(),
            self.glob_info(),
            self.todo_write_info(),
            self.task_info(),
            self.load_skill_info(),
            self.compact_info(),
            self.create_task_info(),
            self.update_task_info(),
            self.list_tasks_info(),
            self.get_task_info(),
            self.claim_task_info(),
            self.complete_task_info(),
            self.schedule_cron_info(),
            self.list_crons_info(),
            self.cancel_cron_info(),
        ]
        self.toolsHandlers = {
            "bash": self.run_bash,
            "read_file": self.run_read,
            "write_file": self.run_write,
            "edit_file": self.run_edit,
            "glob": self.run_glob,
            "todo_write": self.run_todo_write,
            "task": self.run_subagent,
            "load_skill": self.run_load_skill,
            "create_task": self.run_create_task,
            "update_task": self.run_update_task,
            "list_tasks":self.run_list_tasks,
            "get_task": self.run_get_task,
            "claim_task": self.run_claim_task,
            "complete_task": self.run_complete_task,
            "schedule_cron": self.run_schedule_cron,
            "list_crons": self.run_list_crons,
            "cancel_cron": self.run_cancel_cron,
        }
        self.subTools = [
            self.sub_bash_info(),
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
    def execute_tool(self, block, handlers: dict, allow_background: bool = True) -> str:
        blocked = self.hooks.trigger_hooks("PreToolUse", block)
        if blocked:
            return str(blocked)
        
        if allow_background and self.backgroundTasksManager.should_run_background(block.name, block.input):
            try:
                task_id = self.backgroundTasksManager.start_background_task(block)
                output = (
                    f"[Background task {task_id} started] "
                    f"The result will be collected on a later turn."
                )
            except Exception as error:
                output = f"[Background task start error] {error}"
        
        else:

            ### 工具路由
            handler = handlers.get(block.name)
            if not handler:
                output = f"Unknown:{block.name}"
            else:
                ### run_in_background 不允许时降级为前台执行；
                ### 同时剔除该参数，避免 handler 收到未知关键字
                tool_input = dict(block.input)
                if tool_input.pop("run_in_background", False) and not allow_background:
                    print("[background] not allowed in this context, running in foreground")
                output = handler(**tool_input)

        self.hooks.trigger_hooks("PostToolUse", block, output)
        return str(output)

    def bash_info(self):
        return self.BASH

    ### 子代理专用 bash：不暴露 run_in_background，从源头禁止后台任务
    def sub_bash_info(self):
        info = copy.deepcopy(self.BASH)
        info["input_schema"]["properties"].pop("run_in_background", None)
        return info

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
    
    def load_skill_info(self):
        return self.LOAD_SKILL
    
    def skills_catalog(self) -> str:
        return self.skillManager.catalog()
    
    def compact_info(self):
        return self.COMPACT

    def create_task_info(self):
        return self.CREATE_TASK

    def update_task_info(self):
        return self.UPDATE_TASK

    def list_tasks_info(self):
        return self.LIST_TASKS

    def get_task_info(self):
        return self.GET_TASK

    def claim_task_info(self):
        return self.CLAIM_TASK

    def complete_task_info(self):
        return self.COMPLETE_TASK
    
    def schedule_cron_info(self):
        return self.SCHEDULE_CRON
    
    def list_crons_info(self):
        return self.LIST_CRONS
    
    def cancel_cron_info(self):
        return self.CANCEL_CRON

    ### bash
    def run_bash(self, command: str) -> str:
        found = False
        for d in Permission.DENY_LIST:
            if d in command:
                found = True
                break
        if found:
            return "Error: Dangerous command blocked"

        output, exit_code = self.backgroundTasksManager.run_bash_process(command)
        return self.backgroundTasksManager.format_bash_result(output, exit_code)

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
    def run_edit(self, path: str, old_string: str, new_string: str) -> str:
        try:
            file_path = self.safe_path(path)
            text = file_path.read_text(encoding="utf-8")
            if old_string not in text:
                return f"Error: text not found in {path}"
            file_path.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
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
                output =  self.extract_text(response.content)
                return output

            results = []
            for block in tool_calls:
                output = self.execute_tool(block, self.subToolsHandlers, allow_background=False)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

            messages.append({"role": "user", "content": results})

        return f"Subagent stopped after {self.MAX_SUBAGENT_TURNS} turns without a final answer."

    def run_load_skill(self, name: str) -> str:
        return self.skillManager.load(name)


    def run_create_task(self, subject: str, description: str = "") -> str:
        task = self.taskManager.create_task(subject, description)
        print(f"[Create] {task.subject}")
        return f"Created {task.id}: {task.subject}"

    def run_update_task(self, task_id: str, addBlockedBy: list[str]) -> str:
        task = self.taskManager.update_task(task_id, addBlockedBy)
        dependencies = ", ".join(task.blockedBy) or "(none)"
        print(f"[update] {task.subject} blockedBy: {dependencies}")
        return f"Updated {task.id} blockedBy: {dependencies}"

    def run_list_tasks(self) -> str:
        tasks = self.taskManager.list_tasks()
        if not tasks:
            return "No tasks. Use create_task to add some."

        lines = []
        for task in tasks:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }.get(task.status, "[?]")
            dependencies = (
                f" (blockedBy: {', '.join(task.blockedBy)})" if task.blockedBy else ""
            )
            owner = f" [{task.owner}]" if task.owner else ""
            lines.append(
                f"{marker} {task.id}: {task.subject} "
                f"[{task.status}]{owner}{dependencies}"
            )
        return "\n".join(lines)

    def run_get_task(self, task_id:str) -> str:
        return self.taskManager.get_task(task_id)

    def run_claim_task(self, task_id:str) -> str:
        return self.taskManager.claim_task(task_id, owner="agent")

    def run_complete_task(self, task_id:str) -> str:
        return self.taskManager.complete_task(task_id, owner="agent")
    
    
    def run_schedule_cron(self, cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> str:
        result = self.cronScheduler.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: {cron} -> {prompt}"
    
    def run_list_crons(self) -> str:
        jobs = self.cronScheduler.list_cron_jobs()
        if not jobs:
            return "No cron jobs."

        lines = []
        for job in jobs:
            frequency = "recurring" if job.recurring else "one-shot"
            storage = "durable" if job.durable else "session"
            lines.append(
                f"{job.id}: {job.cron} -> {job.prompt[:60]} "
                f"[{frequency}, {storage}]"
            )
        return "\n".join(lines)
    
    def run_cancel_cron(self, job_id: str) -> str:
        return self.cronScheduler.cancel_job(job_id)

    
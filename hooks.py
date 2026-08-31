import color
from env import Env
from permission import *
from color import *

class Hooks:
    def __init__(self):
        self.env = Env()
        self.permission = Permission()
        self.hooks = {
            "UserPromptSubmit": [],
            "PreToolUse": [],
            "PostToolUse": [],
            "Stop": [],
        }

        self.register_hook("UserPromptSubmit", self.context_inject_hook)
        self.register_hook("PreToolUse", self.permission_hook)
        self.register_hook("PreToolUse", self.log_before_use_tool_hook)
        self.register_hook("PostToolUse", self.log_after_use_tool_hook)
        self.register_hook("PostToolUse", self.large_output_hook)
        self.register_hook("Stop", self.summary_hook)

    def register_hook(self, event: str, callback):
        self.hooks[event].append(callback)

    def trigger_hooks(self, event: str, *args):
        for callback in self.hooks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None

    def permission_hook(self, block):
        return self.permission.check_permission(block)

    def log_before_use_tool_hook(self, block):
        args_preview = str(list(block.input.values())[:2])[:60]
        print(
            f"{COLOR_DEFAULT}[HOOK] {block.name}({args_preview}) {COLOR_DEFAULT}"
        )
        return None

    def log_after_use_tool_hook(self, block, output):
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

        print(
            f"{COLOR_GREEN}[HOOK] tool_use: {block.name} - {info} {COLOR_DEFAULT}"
        )
        print(f"{COLOR_DEFAULT}[HOOK]tool_result:\n{output}{COLOR_DEFAULT}")

    def large_output_hook(self, block, output):
        if len(str(output)) > 100000:
            print(
                f"{COLOR_DEFAULT}[HOOK] Large output from {block.name}: {len(str(output))} chars {COLOR_DEFAULT}"
            )
        return None

    def context_inject_hook(self, query: str):
        print(
            f"{COLOR_DEFAULT}[HOOK] UserPromtSubmit: working in {self.env.workDir} {COLOR_DEFAULT}"
        )
        return None

    def summary_hook(self, messages: list):
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

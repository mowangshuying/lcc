from env import Env
from permission import Permission
from log import log_info, log_warn
from tool_names import BASH, EDIT_FILE, GLOB, READ_FILE, TASK, TODO_WRITE, WRITE_FILE

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

    ### skip_permission=True（Lane D 队友链路）：仅跳过交互式 permission_hook，
    ### 其余钩子照常执行；权限判定已由调用方以 prompt_user=False 手动检过。
    ### skip_approval=True（异步 turn，对照 s15 N5 fail-closed）：透传给 permission_hook，
    ### 命中需确认规则时直接返回拒绝文案，绝不 input()；其他钩子不消费该关键字。
    ### 注意用 == 比较：存储的绑定方法与每次属性访问新生成的绑定方法 is 不成立但 == 成立。
    def trigger_hooks(self, event: str, *args, skip_permission: bool = False,
                      skip_approval: bool = False):
        for callback in self.hooks[event]:
            if skip_permission and callback == self.permission_hook:
                continue
            if callback == self.permission_hook:
                result = callback(*args, skip_approval=skip_approval)
            else:
                result = callback(*args)
            if result is not None:
                return result
        return None

    def permission_hook(self, block, skip_approval: bool = False):
        return self.permission.check_permission(block, skip_approval=skip_approval)

    def log_before_use_tool_hook(self, block):
        args_preview = str(list(block.input.values())[:2])[:60]
        log_info(
            "hook", f"{block.name}({args_preview})"
        )
        return None

    def log_after_use_tool_hook(self, block, output):
        # ### use tool info
        info = ""
        if block.name == BASH:
            info = f"command: {block.input['command']}"
        elif block.name == READ_FILE:
            info = f"path: {block.input['path']}"
        elif block.name == WRITE_FILE:
            info = f"path: {block.input['path']}"
        elif block.name == EDIT_FILE:
            info = f"path: {block.input['path']}"
        elif block.name == GLOB:
            info = f"pattern: {block.input['pattern']}"
        elif block.name == TODO_WRITE:
            info = f"update task list"
        elif block.name == TASK:
            info = f"task: {block.input.get('prompt', '')}"

        log_info(
            "hook", f"tool_use: {block.name} - {info}"
        )
        log_info("hook", f"tool_result:\n{output}")

    def large_output_hook(self, block, output):
        if len(str(output)) > 100000:
            log_warn(
                "hook", f"Large output from {block.name}: {len(str(output))} chars"
            )
        return None

    def context_inject_hook(self, query: str):
        log_info(
            "hook", f"UserPromptSubmit: working in {self.env.workDir}"
        )
        return None

    def summary_hook(self, messages: list):
        tool_count = 0
        for message in messages:
            if isinstance(message.get("content"), list):
                for block in message.get("content"):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_count += 1
        log_info(
            "hook", f"Stop: session used {tool_count} tool calls"
        )
        return None

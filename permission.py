import re
from typing import Callable
from color import COLOR_DEFAULT, COLOR_YELLOW
from env import Env
from log import log_error, log_warn
from tool_names import BASH, EDIT_FILE, MCP_PREFIX, READ_FILE, WRITE_FILE
class Permission:
    ### 硬编码禁止列表 总是禁止；单一事实源：Permission 链式检查与 ToolsManager.run_bash 共用本清单
    DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/"]

    def __init__(self):
        self.env = Env()

        ### MCP 策略回调：由组装根（ToolsManager.__init__）注入 McpManager.policy_for，
        ### 签名 (prefixed_tool_name: str) -> "allow" | "confirm"。
        ### None（未接线）时一律视为 confirm —— fail-closed：外部工具在宿主策略缺位时
        ### 永远要求人工确认，绝不默认放行。
        self.mcp_policy: Callable[[str], str] | None = None
        
        ### (?i) 忽略大小写
        ### (?:^|[;&|()\n{}"'`]) 匹配字符串的开头、分隔符号/花括号或引号（覆盖 powershell -Command "..." 与 & {...} 嵌套写法）
        ### \s* 多个空白符号
        ### (?:rm|del|erase|ri|rmdir|rd|Remove-Item) 匹配各类删除命令及其别名（ri/rd 为 PowerShell 别名，erase/rmdir 为 cmd 内置）
        ### (?=\s|$|[;&|(){}]) 往后看一眼，确认后面紧跟的是空格，字符串结尾(后面没有字符了)，或者特定的分隔符号
        self.DESTRUCTIVE_COMMAND_WORD = re.compile(
            r"""(?i)(?:^|[;&|()\n{}"'`])\s*(?:rm|del|erase|ri|rmdir|rd|Remove-Item)(?=\s|$|[;&|(){}])"""
        )
        
        self.PERMISSION_RULES = [
            {
                "tools": [READ_FILE, WRITE_FILE, EDIT_FILE],
                "check": lambda args: not (self.env.workDirPath / args.get("path", ""))
                .resolve()
                .is_relative_to(self.env.workDirPath),
                "message": "Writing outside workspace",
            },
            {
                "tools": [BASH],
                "check": lambda args: self.contains_destructive_command(args.get("command", ""))
                or any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
                "message": "Potentially destructive command",
            },
        ]


    def check_deny_list(self, command: str) -> str | None:
        for pattern in self.DENY_LIST:
            if pattern in command:
                return f"Permission denied by deny list"
        return None



    ### 是否包含破坏性命令
    def contains_destructive_command(self, command: str) -> bool:
        return bool(self.DESTRUCTIVE_COMMAND_WORD.search(command))

    ### 检查规则
    def check_rules(self, tool_name: str, args: dict) -> str | None:
        for rule in self.PERMISSION_RULES:
            if (tool_name in rule["tools"]) and rule["check"](args):
                return rule["message"]
        return None


    def ask_user(self, tool_name: str, args: dict, reason: str) -> str:
        log_warn("permission", reason, blank_before=True)
        log_warn("permission", f"Tool: {tool_name}({args})")
        choice = input(f"    {COLOR_YELLOW}Allow? [Y/N]{COLOR_DEFAULT}").strip().lower()
        if choice in ("y", "yes"):
            return "allow"
        return "deny"


    ### prompt_user=True（Lead 默认）：命中规则时交互式询问，行为与旧版逐字一致。
    ### prompt_user=False（队友场景，Lane D 注入契约）：绝不占用控制台 input()，
    ### 命中即返回拒绝文案（对照 s13 "Permission required: ..." 语义）。
    ### skip_approval=True（异步 turn，对照 s15 N5 fail-closed L1773）：优先级最高——
    ### 命中任一"需确认"规则直接返回该固定拒绝串，绝不 input()（硬 deny 列表仍先行拦截）。
    ASYNC_APPROVAL_DENIED = ("Permission denied: interactive approval is unavailable "
                             "during an asynchronous turn")

    def check_permission(self, block, prompt_user: bool = True,
                         skip_approval: bool = False) -> str | None:
        if block.name == BASH:
            reason = self.check_deny_list(block.input.get("command", ""))
            if reason:
                log_error("permission", reason)
                return reason

        reason = self.check_rules(block.name, block.input)
        if reason:
            if skip_approval:
                log_error("permission", self.ASYNC_APPROVAL_DENIED)
                return self.ASYNC_APPROVAL_DENIED
            if not prompt_user:
                log_error("permission", f"Permission required: {reason}")
                return f"Permission required: {reason}"
            decision = self.ask_user(block.name, block.input, reason)
            if decision == "deny":
                log_error("permission", "Permission denied by user")
                return "Permission denied by user"

        ### MCP 外部工具闸门（对照 s14 permission_hook 第三段）：
        ### 授权判定来自宿主策略表，与 check_rules 的内置规则链相互独立——
        ### bash/文件类工具名不会以 mcp__ 开头，天然互斥，因此落在 check_rules 之后
        ### 也不会重复询问。策略缺省 confirm（fail-closed）。
        if block.name.startswith(MCP_PREFIX):
            policy = (self.mcp_policy or (lambda _: "confirm"))(block.name)
            if policy != "allow":
                reason = "MCP tool policy requires confirmation"
                if skip_approval:
                    log_error("permission", self.ASYNC_APPROVAL_DENIED)
                    return self.ASYNC_APPROVAL_DENIED
                if not prompt_user:
                    log_error("permission", f"Permission required: {reason}")
                    return f"Permission required: {reason}"
                decision = self.ask_user(block.name, block.input, reason)
                if decision == "deny":
                    log_error("permission", "Permission denied by user")
                    return "Permission denied by user"
        return None
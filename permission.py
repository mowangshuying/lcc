import re
from color import *
from env import Env

class Permission:
    def __init__(self):
        self.env = Env()
        ### 硬编码禁止列表 总是禁止
        self.DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
        
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
                "tools": ["read_file", "write_file", "edit_file"],
                "check": lambda args: not (self.env.workDirPath / args.get("path", ""))
                .resolve()
                .is_relative_to(self.env.workDirPath),
                "message": "Writing outside workspace",
            },
            {
                "tools": ["bash"],
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
        print(f"\n{COLOR_YELLOW}[permission]{reason}{COLOR_DEFAULT}")
        print(f"    {COLOR_YELLOW}Tool: {tool_name}({args}){COLOR_DEFAULT}")
        choice = input(f"    {COLOR_YELLOW}Allow? [Y/N]{COLOR_CYAN}").strip().lower()
        if choice in ("y", "yes"):
            return "allow"
        return "deny"


    def check_permission(self, block) -> str | None:
        if block.name == "bash":
            reason = self.check_deny_list(block.input.get("command", ""))
            if reason:
                print(f"{COLOR_RED}{reason}{COLOR_DEFAULT}")
                return reason

        reason = self.check_rules(block.name, block.input)
        if reason:
            decision = self.ask_user(block.name, block.input, reason)
            if decision == "deny":
                print(f"{COLOR_RED}Permission denied by user")
                return "Permission denied by user"
        return None
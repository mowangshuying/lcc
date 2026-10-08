### s14 MCP tools：外部工具的发现与装配（lcc 移植版）
### MCPClient 是 MCP tools/list + tools/call 的进程内替身——本课没有真实 transport，
### 与 s14 参考实现一致：mock server 直接在进程内注册工具定义与 handler。
###
### 语义要点（照抄 s14，勿自创）：
###   1. 模型每轮看到 "静态内置工具 + connect_mcp"；调用 connect_mcp("docs") 之后，
###      下一轮组装的动态工具池里才出现 mcp__{server}__{tool} 前缀工具。
###   2. 授权来自宿主策略表 MCP_HOST_POLICY，而非 server 自述：annotations 里的
###      readOnlyHint / destructiveHint 只是 server 的自我声明，装配时被刻意丢弃，
###      绝不作为放行依据（自述不可信，越权风险由宿主承担）。
###   3. 策略缺省 confirm —— fail-closed：未在策略表登记的外部工具一律人工确认。

import re

from log import log_info
from tool_names import CONNECT_MCP, MCP_PREFIX


class MCPClient:
    """MCP tools/list 与 tools/call 的进程内替身（无真实 transport，同 s14）。"""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    ### 注册工具（四道校验，逐字移植 s14 L171-181）：
    ###   ① 名字必须是"非空 str 且 truthy"；② server 内不重名；
    ###   ③ 每个工具必须有对应 handler，缺则 raise ValueError。
    ### 有意不做反向"孤儿 handler"校验：与 s14 一致，多余 handler 无入口调用，属无害冗余。
    def register(self, tool_defs: list[dict], handlers: dict[str, callable]):
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    ### 调用工具（移植 s14 L183-190）：错误留在工具边界内——
    ### 未知工具与 handler 异常都转成字符串返回，不向上抛，让模型自行决定下一步。
    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as exc:
            return f"MCP error: {type(exc).__name__}: {exc}"


### 非法字符（模型工具名字母表之外）统一替换为下划线
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_mcp_name(name: str) -> str:
    """把名字净化到模型工具名字母表 [a-zA-Z0-9_-] 内。"""
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized


class McpManager:
    ### 宿主策略表：键为 (server 原始名, tool 原始名) 元组。
    ### 授权来自宿主配置而非 server 自述——server 的 annotations 在组装时被丢弃。
    ### 未登记的组合由 policy_for/assemble 兜底为 confirm（fail-closed）。
    MCP_HOST_POLICY = {
        ("docs", "search"): "allow",
        ("docs", "get_version"): "allow",
        ("deploy", "status"): "allow",
        ("deploy", "trigger"): "confirm",
    }

    ### connect_mcp 的入参 enum 与 MOCK_SERVERS 手工同步 —— s14 遗留设计债，
    ### 移植时保持原样：新增 mock server 必须同时改这里，否则模型无法选中该 server。
    CONNECT_TOOL = {
        "name": CONNECT_MCP,
        "description": "Connect to an MCP server and discover its tools.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": ["docs", "deploy"]}},
            "required": ["name"],
        },
    }

    def __init__(self):
        ### 无参构造：MCP 装配是纯本地逻辑，不依赖 Env、不碰 client。
        self.clients: dict[str, MCPClient] = {}
        ### 策略快照：只在 assemble() 整体替换，避免半更新状态；
        ### Permission 的 mcp_policy 回调读的就是这份快照。
        self.toolPolicies: dict[str, str] = {}

    # ===== mock server（工具定义逐字移植 s14 L214-279）=====

    @staticmethod
    def _mock_server_docs() -> MCPClient:
        server = MCPClient("docs")
        server.register(
            tool_defs=[
                {
                    "name": "search",
                    "description": "Search the documentation.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "get_version",
                    "description": "Get the documentation API version.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": True},
                },
            ],
            handlers={
                "search": lambda query: f"[docs] Found 3 results for '{query}'",
                "get_version": lambda: "[docs] API v2.1.0",
            },
        )
        return server

    @staticmethod
    def _mock_server_deploy() -> MCPClient:
        server = MCPClient("deploy")
        server.register(
            tool_defs=[
                {
                    "name": "trigger",
                    "description": "Trigger a deployment.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                        "required": ["service"],
                    },
                    "annotations": {"destructiveHint": True},
                },
                {
                    "name": "status",
                    "description": "Check deployment status.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                        "required": ["service"],
                    },
                    "annotations": {"readOnlyHint": True},
                },
            ],
            handlers={
                "trigger": lambda service: f"[deploy] Triggered: {service}",
                "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
            },
        )
        return server

    ### server 名 → 工厂函数（连接时才实例化，故可重复注册新实例）
    MOCK_SERVERS = {
        "docs": _mock_server_docs,
        "deploy": _mock_server_deploy,
    }

    # ===== 连接（tools/list 的替身）=====

    def connect(self, name: str) -> str:
        """连接 mock server 并登记其工具（移植 s14 L282-295）。"""
        if name in self.clients:
            return f"MCP server '{name}' already connected"
        factory = self.MOCK_SERVERS.get(name)
        if not factory:
            return f"Unknown server '{name}'. Available: {', '.join(self.MOCK_SERVERS)}"
        server = factory()
        self.clients[name] = server
        names = ", ".join(tool["name"] for tool in server.tools)
        log_info("mcp", f"connected: {name} -> {names}")
        return (
            f"Connected to MCP server '{name}'. "
            f"Discovered {len(server.tools)} tools: {names}"
        )

    # ===== 工具池组装（每轮重算）=====

    def assemble(
        self, base_tools: list[dict], base_handlers: dict
    ) -> tuple[list[dict], dict]:
        """内置工具 + 全部已连 server 的工具合成模型可见工具池（移植 s14 L316-359）。"""
        tools = list(base_tools)
        handlers = dict(base_handlers)
        policies: dict[str, str] = {}
        ### origins：最终名 → 来源描述，既用于碰撞诊断，也用于 schema 报错定位
        origins = {tool["name"]: f"built-in tool {tool['name']!r}" for tool in tools}

        for server_name, server in self.clients.items():
            safe_server = normalize_mcp_name(server_name)
            for tool_def in server.tools:
                raw_name = tool_def["name"]
                safe_tool = normalize_mcp_name(raw_name)
                prefixed = f"{MCP_PREFIX}{safe_server}__{safe_tool}"
                if len(prefixed) > 64:
                    raise ValueError(
                        f"MCP tool name is longer than 64 characters: {prefixed}"
                    )
                origin = f"MCP tool {server_name!r}/{raw_name!r}"
                if prefixed in origins:
                    raise ValueError(
                        "MCP tool name collision after normalization: "
                        f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                    )
                schema = tool_def.get("inputSchema", {})
                if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                    raise ValueError(f"Invalid input schema for {origin}")
                origins[prefixed] = origin
                ### schema 翻译点：MCP 线上格式 camelCase inputSchema -> Anthropic input_schema。
                ### 只取 name/description/input_schema 三项；annotations 刻意丢弃——
                ### server 自述不构成授权依据，授权只认 MCP_HOST_POLICY。
                tools.append({
                    "name": prefixed,
                    "description": tool_def.get("description", ""),
                    "input_schema": schema,
                })
                ### 必须用默认参数捕获 client/tool：闭包晚绑定会让所有 handler 指向
                ### 循环最后一次迭代的 server/raw_name（同名工具集体错投到最后一个 server）。
                handlers[prefixed] = (
                    lambda *, client=server, tool=raw_name, **kwargs:
                    client.call_tool(tool, kwargs)
                )
                policies[prefixed] = self.MCP_HOST_POLICY.get(
                    (server_name, raw_name), "confirm"
                )

        ### 整体替换快照：与本次返回的 handlers 原子成对，Permission 读到的策略
        ### 永远与本圈工具池一致
        self.toolPolicies = policies
        return tools, handlers

    def policy_for(self, prefixed_name: str) -> str:
        """查组装期策略快照；未登记一律 confirm（fail-closed）。"""
        return self.toolPolicies.get(prefixed_name, "confirm")

    # ===== 访问器（对齐 lcc schema 访问器模式）=====

    def connect_tool_info(self) -> dict:
        """connect_mcp 的 Anthropic 工具 schema。"""
        return self.CONNECT_TOOL

    # ===== handler 边界 =====

    def run_connect_mcp(self, name: str) -> str:
        ### 有意差异：s14 依赖 execute_tool 捕获 handler 异常；lcc 的 execute_tool
        ### 不做捕获，故在 handler 边界内自行 try/except，保持"错误留在工具边界内"语义。
        try:
            return self.connect(name)
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

    def system_prompt_note(self) -> str | None:
        """已连 server 提示（只报 server 名，不报工具清单）；无连接返回 None。"""
        if not self.clients:
            return None
        return "Connected MCP servers: " + ", ".join(self.clients)

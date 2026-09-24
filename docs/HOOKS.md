# Hooks 技术文档

> 对应源码：`hooks.py`（本仓库当前版本 90 行）
> 状态：完整，**已接入主循环与工具执行链**（全进程单一实例、构造函数注入，见 §6）

## 1. 它解决什么问题

给 agent 生命周期的四个节点提供可插拔的切入点：用户提交输入、工具执行前、
工具执行后、回合停止时。实现是一个极简事件总线——**注册列表 + 顺序执行 +
首个非 None 短路**，没有优先级数值、没有通配、没有注销。

## 2. 事件总览

| 事件 | 回调（按注册顺序） | 参数 | 返回值如何被消费 |
|---|---|---|---|
| `UserPromptSubmit` | `context_inject_hook` | `query: str` | **丢弃**（loop.py:247 不接收返回值） |
| `PreToolUse` | `permission_hook` → `log_before_use_tool_hook` | `block` | **非 None = 拦截**：execute_tool 直接把它当 tool_result 返回（tools_manager.py:323-325），handler 与 PostToolUse 全部跳过 |
| `PostToolUse` | `log_after_use_tool_hook` → `large_output_hook` | `block, output` | **丢弃**（tools_manager.py:351 不接收返回值） |
| `Stop` | `summary_hook` | `messages: list` | 接收为 `force`：非 None 会被 append 成 user 消息强制对话继续（唯一触发点 loop.py:152-155，主循环回合出口）。当前唯一 Stop 回调恒返回 None，**机制存在但无人使用** |

四个事件里真正能"改变行为"的只有 PreToolUse；Stop 的"强制续话"是预留能力。

## 3. 机制内核

```python
def trigger_hooks(self, event, *args):
    for callback in self.hooks[event]:
        result = callback(*args)
        if result is not None:      # 注意：判据是 is not None
            return result           # 首个"说话者"赢，后面的回调不再执行
    return None
```

- `register_hook` 就是 list.append——**注册顺序即执行顺序即优先级**；
- 四个事件键在 `__init__` 硬编码，新增事件类型需同时改初始字典；
- 无异常处理：任何回调抛出（如权限询问时 stdin 的 `EOFError`）会穿透
  `trigger_hooks` 直达调用方主循环。

## 4. 内置回调明细

| 回调 | 实际行为 | 备注 |
|---|---|---|
| `context_inject_hook` | `log_info`：`[hook] UserPromptSubmit: working in <workDir>`（hooks.py:75-77） | **名不副实**：不注入任何上下文，恒 None。真正的上下文注入（skills 目录）发生在 loop 的 `build_system_prompt`，与本回调无关 |
| `permission_hook` | 转发 `Permission.check_permission(block)` | 全事件体系里唯一会产生非 None 拦截的回调，详见 PERMISSION.md |
| `log_before_use_tool_hook` | `log_info`：`[hook] name([前两个参数str][:60])`（hooks.py:38-41） | 恒 None |
| `log_after_use_tool_hook` | 按工具名（`tool_names` 常量比较）拼一行 info（bash→command、read/write/edit→path、glob→pattern、todo_write→固定文案、task→prompt），`log_info` 两行：`[hook] tool_use: name - info` 与 `[hook] tool_result:` + 全文 output（hooks.py:46-65） | 未列举的工具（如 `load_skill`）info 为空串；恒返回 None（隐式） |
| `large_output_hook` | `len(str(output)) > 100000` 时 `log_warn` 一条黄色 `[hook] Large output from name: N chars`（hooks.py:68-71） | **只打日志不落盘**；大结果落盘职责在 CompactManager（见 COMPACT_MANAGER.md §9）；恒 None |
| `summary_hook` | 遍历 messages 统计 `tool_result` 块总数，`log_info`：`[hook] Stop: session used N tool calls`（hooks.py:80-89） | 恒 None |

## 5. block 形态约定

Pre/PostToolUse 回调收到的是 Anthropic SDK 的 `tool_use` 内容对象
（鸭子类型取 `.name`、`.input`；`.input` 是 dict）。与
`CompactManager.block_type` 那种"SDK 对象或 dict 双形态兼容"的防御风格不同，
hooks 这里**不做**双形态处理——现网调用链全部来自 SDK 响应，成立；
若未来有人造 dict 块走 execute_tool，`log_after_use_tool_hook` 会
`AttributeError`。

## 6. 接线方式：全进程唯一一条事件总线

```
loop.py:17   self.hooks = Hooks()              # 全进程唯一实例（唯一 Hooks() 构造点）
loop.py:22   self.toolsManager = ToolsManager(self.hooks)  # 构造函数注入
tools_manager.py:247 def __init__(self, hooks: Hooks): ... # 声明式接收
tools_manager.py:254 self.hooks = hooks                    # 只引用，不自建
```

- 主循环与 ToolsManager 引用**同一个** Hooks 对象——注册表只有一份，
  `UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop` 四类事件都走这条总线；
- 触发点全景：`UserPromptSubmit`（loop.py:247）、主循环 `Stop`（loop.py:152）、
  `PreToolUse`（tools_manager.py:323）、`PostToolUse`（tools_manager.py:351）；
  子代理工具执行复用 `run_subagent → execute_tool`（tools_manager.py:599），
  同样经过这条总线的 Pre/PostToolUse；
- `Stop` 归属裁决：**会话停止事件只由主循环出口触发一次**。历史上
  `run_subagent` 的最终回答分支复制了同样的 Stop 触发，语义是"子代理回合
  结束"而非"会话停止"——`summary_hook` 的打印口径（session used N tool
  calls）与旧版文档对该行为的定性（双实例副作用）都指向前者，且
  force 拦截机制（§2）在工具侧从无消费者，故工具侧触发已删除；
- `summary_hook` 现在每个逻辑回合只打印一次（统计主循环 messages）；
- `Hooks.__init__` 的 6 条默认自注册（hooks.py:17-22）保持不变；
  ToolsManager 不再自建 `Hooks()`，外部注册在唯一总线上全局生效。

## 7. 不变量（改代码前必读）

1. **None = 沉默，非 None = 接管**：回调想表达"继续"必须返回 None；
   PreToolUse 回调返回任何非 None 都会短路后续回调；
2. **PreToolUse 回调只允许返回 None 或非空字符串**：`trigger_hooks` 用
   `is not None` 短路，而 `execute_tool` 用 `if blocked:` 真值判断——
   返回 `""` 会造成"后续回调（可能包括 permission_hook，若注册在它之前）
   被跳过、但工具照常执行"的最坏组合；
3. **注册顺序即语义**：`permission_hook` 必须保持在 PreToolUse 首位
   （否则拦截前就会先打工具日志/先执行新加回调的副作用）；
4. `PostToolUse` 只在工具**真正执行过**后触发（拦截路径提前 return），
   且 `Unknown:{name}` 路径**也会**触发——它统计的是"执行过"，不是"成功"。

## 8. 已知坑点

- 返回值消费方式逐事件不同（§2 表），给 `PostToolUse`/`UserPromptSubmit`
  写"返回非 None 想改写行为"的回调是无效功——返回值被原地丢弃；
- `log_after_use_tool_hook` 全文打印 tool_result，与大输出几乎同时到达终端，
  日志噪音随会话线性增长（无开关）；
- `context_inject_hook` 的名字是愿景不是描述（§4）；
- 子代理与主循环共用同一条总线及其 `PreToolUse`：子代理触发权限询问时阻塞整个进程
  （此时主循环本来就停在 `task` 的返回值上），交互体验是"agent 卡住等 Y/N"；
- `Stop` 的 force 机制若被启用（返回非 None），主循环会向 messages 追加
  user 文本并 continue——注意这会进入 CompactManager `prepare` 的下一次
  往返，伪造的归档标记等文本受其 `is_archive_marker` 三重验证保护，不受影响。

## 9. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `permission.py` | PreToolUse 首位回调的委托对象 |
| `tools_manager.py` | 注入式消费方（`__init__` 接收 hooks 参数）；`execute_tool` 是 Pre/PostToolUse 的唯一触发器（含子代理） |
| `loop.py` | 唯一持有者与创建方（loop.py:17），并在 loop.py:22 注入 ToolsManager；触发 UserPromptSubmit 与主循环 Stop |
| `env.py` | `Hooks.__init__` 自建一个 `Env()` 与一个 `Permission()`（hooks.py:8-9），随唯一实例各一份 |
| `compact_manager.py` | `large_output_hook` 与 ① `tool_result_budget` 职责重叠但互不感知（COMPACT_MANAGER.md §9） |

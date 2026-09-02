# CompactManager 技术文档

> 对应源码：`compact_manager.py`（本仓库当前版本 391 行）
> 状态：引擎完整，**已接入主循环**（`loop.py`，接线细节见 §10）

## 1. 它解决什么问题

Agent 长对话中，`messages` 列表会无限膨胀：工具结果动辄几万字符，几十轮之后
上下文超出模型窗口，API 直接报错。`CompactManager` 的职责是在**每轮发给模型
之前**，把 `messages` 修剪到预算内，同时保证：

1. **信息不真丢**——被移出上下文的内容全部落盘（transcript / tool_results），
   留好路径，模型需要时可用 read 工具找回；
2. **语义不破**——绝不破坏 Claude API 的 `tool_use` ↔ `tool_result` 配对规则；
3. **成本分级**——免费手段（磁盘搬运行）优先，花真钱的（调 LLM 做摘要）垫底。

核心入口是 `prepare(messages, active_request)`；另有 `reactive_compact`
（API 报错兜底）与 `compact_history`（模型主动请求）由主循环直接调用，见 §10。

## 2. 常量速查

| 常量 | 值 | 含义 | 使用处 |
|---|---|---|---|
| `CONTEXT_CHAR_LIMIT` | 50,000 | 总上下文红线，`prepare` 各级复查的阈值 | prepare |
| `TOOL_RESULT_BATCH_CHAR_LIMIT` | 200,000 | 单批（最新一条 user 消息内全部）工具结果的预算 | tool_result_budget |
| `LARGE_RESULT_CHAR_LIMIT` | 30,000 | 单条工具结果超它就落盘换预览 | persist_large_output / tool_result_budget |
| `SUMMARY_INPUT_CHAR_LIMIT` | 80,000 | 喂给"总结模型"的素材上限 | summary_input |
| `KEEP_RECENT_RESULTS` | 3 | micro_compact 豁免的最近结果条数 | micro_compact |
| `KEEP_RECENT_MESSAGES` | 5 | reactive_compact 保留的最近消息条数 | reactive_compact |

派生值：`prepare` 内的压缩目标 `target = 50000 * 0.8 = 40000`。
打八折是为了留缓冲——压到贴着红线，下一轮稍一膨胀又得全套重压，
一次压到位可摊薄成本。

## 3. 构造与依赖注入

```python
CompactManager(llm_client, model, transcript_dir, tool_results_dir)
```

- `llm_client`：Anthropic SDK 客户端。类内**唯一**用到它的是 `summarize_history`。
  注入而非自建，方便测试时换假客户端。
- `transcript_dir`：对话全文 JSONL 归档目录。
- `tool_results_dir`：单条大工具结果的落盘目录。

两个目录都是懒创建（用到才 `mkdir(parents=True, exist_ok=True)`）。

## 4. 函数地图

按职责分四组（顺序与源码一致）：

### 4.1 度量与消息形态判断（纯函数）

| 函数 | 说明 |
|---|---|
| `estimate_chars(messages)` | `len(json.dumps(messages, default=str, ensure_ascii=False))`。字符数≈token 的粗略代理指标；注意每次调用都全量序列化，O(n) |
| `block_type(block)` | 静态方法。兼容块的双形态：SDK 对象或 dict，统一取 `type` |
| `has_tool_use(message)` | role 是 assistant 且 content 块里含 `tool_use` |
| `is_tool_result(message)` | role 是 user 且 content 块里含 `tool_result` |
| `is_archive_marker(message)` | 判断消息是否为**真实**归档标记：整条必须恰好匹配 `[N messages archived at 路径]`（`re.fullmatch`），且路径 resolve 后仍在 `transcript_dir` 内并真实存在。三重验证防伪造、防 `..` 穿越、防指向已删文件 |

### 4.2 "模型读过没有"——unseen 追踪

`unseen_tool_result_positions(messages) -> set[(msg_idx, block_idx)]`

- 倒扫找到最后一条 assistant 的位置（找不到用哨兵 `-1`，配合起点公式
  `last_assistant + 1 = 0` 自然覆盖"全是未读"的极端）；
- 其后所有 user 消息里的 `tool_result` 块即为 **unseen**（写入了但模型尚未回应）；
- 返回坐标集合（不是消息号，因为一条消息里可能混着多个块），供 `micro_compact`
  / `fit_tool_results` 做 O(1) 成员判断。

**意义**：micro 级压缩只许清空"模型已消化过的"结果；unseen 的内容模型从没
读过，清掉等于销毁一手资料。

### 4.3 落盘与占位符（写路径 + 读路径配套）

| 函数 | 说明 |
|---|---|
| `write_transcript(messages) -> Path` | 全量消息写 JSONL：`transcript_<uuid4hex>.jsonl`，`open("x")` 独占创建防覆盖，每条消息一行且**必须**补 `\n`（JSONL 的记录分隔符；`json.dumps` 会把内容里的换行转义，不会破坏格式） |
| `save_output(tool_use_id, output) -> Path` | 单条结果全文写 `tool_results/<safe_id>.txt`。`safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(tool_use_id))[:120] or "unknown"`——白名单消毒 + 截断 + 空值兜底；用 `tool_use_id` 当文件名使同一结果反复落盘只覆盖不累积 |
| `persisted_output_path(output) -> str \| None` | 读路径。识别两种既有占位符格式（见 §5），解析出路径后同样做 `resolve + is_relative_to + is_file` 三重验证 |
| `persisted_preview(tool_use_id, output, preview_chars=2000)` | 核心装配：已存过 → 从磁盘文件重读开头做预览（幂等，不套娃）；没存过 → `save_output` 后切前 2000 字符；统一返回占位符文本。读文件失败 `except OSError` 退回用 output 自己截断 |
| `persist_large_output(tool_use_id, output)` | ≤ 30k 原样返回，否则调 `persisted_preview` |

### 4.4 五级压缩手段（供 prepare 编排）

见 §6 流水线。

## 5. 占位符格式约定（写读必须成对理解）

落盘瘦身产生的文本有**两种格式**，两个正则各认一种：

```
格式A（persisted_preview 产出，预览较丰）:
<persisted-output>
Full output: {path}
Preview:
{preview}
</persisted-output>

格式B（micro_compact 产出，极简省字符）:
[Earlier tool result saved at {path}]
```

- `persisted_output_path` 两种都认 → 已压过的结果**不会被二次压缩**（幂等）；
- `is_archive_marker` 认的是第三种：snip 的消息流标记 `[N messages archived at {path}]`，
  用于防止对"只剩标记"的 middle 段重复归档（套娃）。

## 6. prepare() 流水线

设计哲学：**从便宜到昂贵逐级加码，每级压完立即复查，够预算就收手。**

```
prepare(messages, active_request)
│
├─ ① tool_result_budget   无条件 │ 最新批次总量 > 200k？
│      批次内按块从大到小，单块 > 30k 的落盘换 2000 字符预览，
│      每换一块重算，压回 200k 内即停
│
├─ ② snip_compact         无条件 │ 消息条数 > 50？
│      head 前3条 + tail 后46条保留，middle 全文 write_transcript 落盘，
│      换成一条 [N messages archived at 路径] 标记
│      边界微调：head 尾是 tool_use → while 吃掉后续整串结果；
│                tail 头是孤儿结果 → 生产者 assistant 拉进 tail
│
├─ 复查 estimate_chars > 50k？ 否 → 返回
│
├─ ③ micro_compact(target=40k)
│      清空"已消费"的 tool_result：跳过 unseen 坐标、跳过最近 3 条、
│      跳过已是占位符的，其余内容换成格式B极简指针，从最早的压起
│
├─ 复查 > 50k？ 否 → 返回
│
├─ ④ fit_tool_results(40k)
│      ③ 的保护名单可能恰好都是巨无霸 → 撕掉"最近3条"豁免继续落盘瘦身
│      （unseen 仍然不碰），按大小降序直到 ≤ 40k 或弹尽粮绝
│
├─ 复查 > 50k？ 否 → 返回
│
└─ ⑤ compact_history   【核弹：唯一花钱的环节】
       write_transcript 全文落盘（保底归路）
       → summarize_history:
            summary_input 把消息序列化成 JSON，> 80k 则掐中间
            （头 1/4 + 尾 3/4，中间插 "middle omitted..." 提示）
            → client.messages.create(system=防注入三件套, max_tokens=2000)
            → 刮 text 块 + or "(empty summary)" 兜底
       → summary_message("Compacted", active_request, 摘要, 转录路径)
       返回：只含这一条合成 user 消息的新列表（整个历史被顶替）
```

### 各级成本与损失度

| 级 | 触发条件 | 成本 | 信息损失 | 可逆性 |
|---|---|---|---|---|
| ① 预算 | 批次>200k | 磁盘 IO | 低（留2k预览） | 模型可 read 找回 |
| ② snip | 条数>50 | 磁盘 IO | 低（全文在盘） | 可 read 找回 |
| ③ micro | 总量>50k | 零 API | 中（已消化内容清空） | 原文未必已落盘* |
| ④ fit | 仍>50k | 零 API | 中 | 同上 |
| ⑤ history | 仍>50k | 1次 LLM 调用 | 高（二手摘要） | transcript 在盘 |

\* ③④ 压缩的旧结果如果当初没被 ① 落过盘，全文可能只存在于
transcript（② 写过的那份）里——这是链路上已知的信息保全缝隙。

### `reactive_compact`（被动兜底）

⑤ 的温和版：只把老历史摘要化，最近 `KEEP_RECENT_MESSAGES=5` 条原样保留
（同样做 tool_use/tool_result 边界修正）。已接线：主循环捕获到
`prompt_too_long` / `too many tokens` 类 API 报错后调用它救火并重试一次
（见 §10.2）。

## 7. 贯穿全程的不变量（改代码前必读）

1. **配对完整性**：任何裁剪不得让 `tool_result` 与它的 `tool_use` 分家或被
   单边删除（API 直接报错）。体现处：② 的边界 while/if、③④ 的 unseen 保护、
   `summary_message` 永远放列表首位且 role=user（对话以 user 开场合法）。
2. **unseen 不可侵犯**：最后一条 assistant 之后的结果，任何"清空型"压缩
   （③④）不得触碰。
3. **幂等**：占位符自带路径，`persisted_output_path` / `is_archive_marker`
   能认出"已压过"的内容，二次执行不套娃、不重复落盘。
4. **路径安全**：凡是从不可信文本（模型输出、历史消息）解析出的路径，使用前
   必须 `resolve()` + `is_relative_to(自家目录)` + `is_file()` 三重验证。
   写文件名前必须过白名单正则消毒。
5. **原地修改语义**：①②③④ 都是 in-place 改传入的列表/字典，`return` 只是
   礼貌回传；只有 ⑤⑥ 返回全新列表。调用方不能依赖"拿到新副本"。
6. **合成内容一律 role=user**：归档标记、摘要消息、工具结果载体都是。

## 8. 已知边界与接线现状

- **接线现状（s08 已完成，详见 §10）**：
  - `prepare()`：主循环每轮 `messages.create` 之前调用；
  - `reactive_compact`：API 报 overflow 时被动救火，带重试上限；
  - COMPACT 工具：`toolsHandlers` 中**仍无 handler**，但主循环在执行工具前
    按名字拦截（`block.name == "compact"`），不会再出现"找不到 handler"。
- **已知坑**：
  - overflow 判定是对异常文本做小写子串匹配（`prompt_too_long` /
    `too many tokens`），措辞依赖网关/SDK 版本，换端点可能漏判（漏判则异常照抛）；
  - `reactive_retries` 在任何一次成功响应后清零——"每轮请求最多触发一次被动压缩"，
    而非"每个用户回合只有一次机会"，读代码时别搞混；
  - `compact` 的 `tool_use` 块永远不会收到配对的 `tool_result`（见 §10.3），
    合法性完全依赖紧随其后的整列表替换，改动该处必须先想清楚配对；
  - `KEEP_RECENT_RESULTS` / `KEEP_RECENT_MESSAGES` 若改成 0，
    `consumed[:-0]` 是空列表，保护逻辑静默反转（`micro_compact`）；
  - `max_chars or 默认值` 写法：显式传 `0` 会被判假走默认值，无法表达"零预算"；
  - `snip_compact` 每次归档写的是**全量** messages（含 head/tail），
    反复触发时 transcript 目录冗余增长明显；
  - `estimate_chars` 每次调用全量序列化，`prepare` 一轮最多调 4 次，
    消息很多时是可感知的开销（现阶段无所谓）；
  - `summarize_history` 无 try/except、无重试、不检查 `stop_reason`——
    API 故障会一路穿透 `prepare`，生产环境需包降级路径；
    同理 `compact_history` 落盘 transcript 后才调摘要，摘要失败时
    磁盘上仍留有本轮全文 transcript 可查。
  - `summary_input` 的返回串含占位提示文本，实际长度可略超 80k（约 +50 字符），
    已知且无害。

## 9. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `hooks.py` | `large_output_hook` 目前只做**日志打印**（>100k 字符时提示），不落盘；大结果落盘完全由 ① `tool_result_budget` 承担 |
| `tools_manager.py` | COMPACT 工具 schema 已注册进 `self.tools`（模型可见、可调用）；`toolsHandlers` 无对应项，由主循环拦截执行（§10.3）；`subTools` 不含 compact，子代理无法触发 |
| `loop.py` | 已集成，三个触发点见 §10 |
| `env.py` | 提供 `transcriptDirPath`（`<workDir>/.transcripts`）与 `toolResultsDirPath`（`<workDir>/.task_outputs/tool-results`），构造时注入 |
| `.gitignore` | 已排除 `.transcripts/`、`.task_outputs/` 运行时产物 |

## 10. 主循环接线（loop.py，s08）

### 10.1 构造注入

```python
self.compactManager = CompactManager(
    self.client, self.env.modelId,
    self.env.transcriptDirPath, self.env.toolResultsDirPath,
)
```

与主循环共享同一个 `Anthropic` 客户端实例；`active_request` 由 `run()`
把本轮用户 query 传入 `agent_loop`，该回合内所有模型往返共用这一锚点。

### 10.2 三个触发点

| 触发 | 时机 | 动作 |
|---|---|---|
| 主动 | 每圈 while 开头、`messages.create` 之前 | `messages[:] = prepare(messages, active_request)` |
| 被动兜底 | `create` 抛异常且文本命中 overflow 特征 | `messages[:] = reactive_compact(...)` 后 `continue` 重试；`MAX_REACTIVE_RETRIES = 1`，成功响应即清零计数 |
| 模型主动 | 模型调用 `compact` 工具 | 本轮其余工具结果 append 完成后，`messages[:] = compact_history(...)`（即 ⑤ 核弹） |

所有回填统一用 `messages[:] = ...` **切片赋值**：`prepare` / `compact_history`
可能返回全新列表（§7 不变量 5），而 `run()` 持有的 `history` 必须是同一对象，
直接重新绑定会把压缩结果丢在局部变量里。

### 10.3 compact 工具的"悬空 tool_use"

工具执行循环里 `compact` 被拦截：不执行、**不产出** `tool_result`，仅置位
`compact_requested`。此刻 assistant 消息里那个 `tool_use` 块处于无配对状态
（形式上违反 §7 不变量 1）。它不会被发给 API——紧随其后的 `compact_history`
把整个列表替换为单条摘要 user 消息，悬空块随旧历史一起消失。

推论：若日后让 `compact` 与普通工具混在同一批调用里，其余工具的结果会先
正常 append、一起进入这次归档摘要，然后全部被摘要顶替——模型主动按 compact
等于**放弃对当批其他工具结果的即时可见性**（盘上仍有 transcript 兜底）。

# Permission 技术文档

> 对应源码：`permission.py`（本仓库当前版本 80 行）
> 状态：引擎完整，**已接入主循环**（`hooks.py` 实例化并注册为 PreToolUse
> 第一顺位回调，接线细节见 §6）

## 1. 它解决什么问题

模型自主决定调用 bash / 文件工具，意味着一次提示注入就可能让 agent
执行 `rm -rf` 或改写工作区外的文件。`Permission` 是工具执行前的**闸门**：
`tool_use` 块在真正分发到 handler 之前，先经过它做放行/拦截判定。

判定结果三选一：

| 结果 | 含义 | 后续 |
|---|---|---|
| 命中 deny list | 硬禁，**不问人** | 直接返回拒绝文本，工具不执行 |
| 命中 rule | 可疑，**问人** | 终端 `Allow? [Y/N]` 阻塞等待 |
| 都不命中 | 放行 | 返回 `None`，工具正常执行 |

对外唯一入口 `check_permission(block)`：返回 `None` = 放行，
返回字符串 = 拦截（该字符串原样成为 tool_result 回给模型）。

## 2. 构造与常量速查

`Permission()` 无参构造，实例化时一次性构建全部规则，
**无外部配置文件、无热重载**（规则是实例属性，改规则 = 改代码）。
例外：`DENY_LIST` 是**类级常量**（`permission.py:7`），不随实例重建，
且为全仓硬禁命令的**单一事实源**——`Permission.check_deny_list` 与
`ToolsManager.run_bash` 共用同一份（见 §7）。

| 常量 | 值 | 匹配方式 |
|---|---|---|
| `DENY_LIST` | `rm -rf /`、`sudo`、`shutdown`、`reboot`、`mkfs`、`dd if=`、`> /dev/` | **区分大小写**的纯子串包含 |
| `DESTRUCTIVE_COMMAND_WORD` | 见 §3 | 正则，忽略大小写 |
| `PERMISSION_RULES` | 2 条（见 §4） | lambda 谓词 |

## 3. 破坏性命令词正则

```python
(?i)(?:^|[;&|()\n{}"'`])\s*(?:rm|del|erase|ri|rmdir|rd|Remove-Item)(?=\s|$|[;&|(){}])
```

逐段拆解（与源码内注释一致）：

1. `(?i)` 忽略大小写；
2. **前置锚**：词前必须是行首或分隔符 `; & | ( ) 换行 { } " ' ` 之一——
   要求破坏性命令以"独立词"身份出现，同时覆盖 `cmd && rm ...`、
   `powershell -Command "rm ..."` 这类链式/嵌套写法；
3. 命令词族：`rm del erase ri rmdir rd Remove-Item`（cmd 内置 +
   PowerShell 别名全覆盖）；
4. **后置断言**：词后必须是空白、行尾或分隔符，防 `rm` 误配 `rmfoo`。

## 4. PERMISSION_RULES 明细

| # | 适用工具 | 触发条件（True = 可疑） | 提示语 |
|---|---|---|---|
| 1 | `read_file` / `write_file` / `edit_file` | `(workDirPath / path).resolve()` 不满足 `is_relative_to(workDirPath)` | `Writing outside workspace` |
| 2 | `bash` | 命中 §3 正则，**或**命令串包含 `rm `、`> /etc/`、`chmod 777` 之一 | `Potentially destructive command` |

要点：

- 路径判定先 `resolve()` 展开 `..`，再 `is_relative_to`——与
  `ToolsManager.safe_path`、`SkillManager.scan` 是同一套工作区围栏语义；
- 规则 2 里子串检查只枚举了 `rm `（带尾空格），PowerShell 的
  `del`/`Remove-Item` 等词族全靠 §3 正则兜住，两者互补而非重复；
- 其余工具（`glob` / `todo_write` / `task` / `load_skill` / `compact`）
  **不在任何规则内**，永远直接放行。

## 5. 判定流程（check_permission）

```
check_permission(block)
├─ block.name == "bash"?
│    └─ check_deny_list：任一 DENY_LIST 项是 command 的子串?
│         命中 → 红字打印，返回 "Permission denied by deny list"（不问人）
├─ check_rules：遍历 PERMISSION_RULES，
│    tool_name 在 rule["tools"] 且 rule["check"](args) 为 True?
│         命中 → ask_user 终端黄字询问（打印工具名+完整参数）：
│              input("Allow? [Y/N]") → strip().lower()
│              "y" / "yes" → 放行，继续往下
│              其他一切输入（含空回车）→ 返回 "Permission denied by user"
└─ 都没命中 → 返回 None（放行）
```

## 6. 接线现状

- `Hooks.__init__` 实例化唯一 `Permission`（hooks.py:9），`permission_hook`
  注册为 PreToolUse 的**第一个**回调（hooks.py:18），排在
  `log_before_use_tool_hook` 之前；`trigger_hooks` 顺序执行、首个非 None
  短路（hooks.py:27-32）→ 被拦截的调用连 `[HOOK]` 日志都不会打印；
- 消费方 `ToolsManager.execute_tool`（tools_manager.py:322-324）：
  PreToolUse 返回非空即 `return str(blocked)`——handler 不执行，拒绝文本
  作为 tool_result 回填给模型，**且 PostToolUse 整条链都不触发**；
- 两个入口共用同一条拦截链：主循环 `loop.py:168` 与子代理
  `tools_manager.py:598` 都调 `execute_tool`，走同一个 `self.hooks` →
  同一个 `Permission`，**子代理不享受任何豁免**；
- 实例化拓扑：全仓库 `Hooks()` 只在 `loop.py:15` 被 new **一次**，经
  `ToolsManager(self.hooks)`（loop.py:20）注入后，主循环与工具执行共用
  同一条事件总线、同一个 `Permission`——历史上 ToolsManager 曾自建第二份
  `Hooks`（各带独立注册表）导致"loop 侧 permission 回调永不执行"的脑裂，
  现已消除（见 HOOKS.md §6）。

## 7. 纵深防御与不变量（改代码前必读）

1. **规则 1 只是"问人"，硬线在 `safe_path`**：即使用户对越界路径答了 Y，
   `run_read/run_write/run_edit` 内部的 `safe_path`（tools_manager.py:314）
   仍会 `raise ValueError`，被 handler 的 `except Exception` 转成
   `Error:...` 文本。想把工作区真正放开，两处都得改；
 2. **`DENY_LIST` 单一事实源，两道校验共享同一份**：全仓只有
    `permission.py:7` 这一处定义；`Permission` 链式检查（`check_deny_list`，
    permission.py:39）与 `ToolsManager.run_bash`（直接 import `Permission`
    后遍历 `Permission.DENY_LIST`）是同一清单的两个消费方，内容永不漂移。
    改硬禁命令只需改这一处。`run_bash` 这道闸**不经过** PreToolUse 链，
    即使 permission hook 被绕过/失效，它仍独立拦截同一批命令（防御纵深）；
 3. **deny list 优先于人工放行**：`sudo` 在列表里，任何含 `sudo` 子串的
    bash 命令直接拒，根本不弹确认；
 4. **默认拒绝**：`ask_user` 只认 `y`/`yes`，空回车、误触、其他输入一律按
    deny 处理；
 5. **返回值语义**：`None` = 放行、非空 `str` = 拦截。`execute_tool` 用
   truthiness 判断（`if blocked:`），回调实现若返回 `""` 会被当成放行，
   还会短路掉后续 PreToolUse 回调——永远别返回空字符串。

## 8. 已知坑点

- **DENY_LIST 是大小写敏感子串匹配**：`echo "sudo made me do it"` 被误杀；
  `SUDO ...` 反而绕过 deny list（一般仍会被规则 2 拦下问人）。
  `rm -rf /` 拦不住 `rm  -rf /`（双空格）、`rm -rf ~` 这类变体；
- **读也被"写"的文案拦**：`read_file` 读工作区外文件同样触发规则 1，
  提示语却是 "Writing outside workspace"——范围正确、措辞误导；
- `args.get("path", "")` 缺参时解析成工作区自身 → 判安全放行
  （参数缺失不报错，交给 handler 层再失败）；
- **非交互环境会炸**：`ask_user` 用 `input()` 阻塞读 stdin，
  无 TTY（CI / 管道喂输入）时抛 `EOFError`、Ctrl+C 抛 `KeyboardInterrupt`，
  均无捕获，一路穿透 `execute_tool` 打断整个回合；
- 一次响应含多个 `tool_use` 时逐个同步询问，无批量确认；
- 拒绝文本只是喂回模型的普通 tool_result，模型可自行改写命令重试——
  闸门拦"这一条命令"，不记状态、不限重试次数（防注入靠行为链外层约束）。

## 9. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `hooks.py` | 实例化者 + PreToolUse 注册方（第一顺位，见 §6） |
| `tools_manager.py` | 拒绝结果的消费方（`execute_tool`）；`run_bash` 绕过 hook 链直接遍历 `Permission.DENY_LIST`（§7.2）；`safe_path` 是文件工具的硬防线（§7.1） |
| `env.py` | 围栏基准取 `Env().workDirPath`（= 进程 `Path.cwd()`，不是仓库位置） |
| `loop.py` | 主循环与子代理共用同一拦截链（§6） |
| `color.py` | 询问/拒绝的终端着色 |

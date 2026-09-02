# SkillManager 技术文档

> 对应源码：`skill_manager.py`（本仓库当前版本 93 行）
> 状态：引擎完整，**已接入主循环**（`tools_manager.py` 实例化并挂工具，
> `loop.py` 系统提示注入目录，接线细节见 §8）

## 1. 它解决什么问题

把"教模型做某类任务的说明书"从代码和硬编码提示词里拆出来，变成磁盘上的
Markdown 文件（skill），按需注入上下文。核心是**两级加载（渐进披露）**：

1. **目录级（常驻、便宜）**：会话启动时扫描 `skills/`，把每个技能的
   `name + description` 一行一条拼进系统提示——模型知道"我有哪些技能"；
2. **正文级（按需、贵）**：模型判断某技能适用时，调用 `load_skill` 工具
   拿回该技能 `SKILL.md` 的完整正文，再照做。

没有这套机制的话，要么所有说明书全文塞进系统提示（每轮都烧 token），
要么模型根本不知道它们存在。

对外入口共三个：构造（隐式触发 `scan`）、`catalog()`、`load(name)`。

## 2. 目录与文件约定

```
<工作目录>/skills/          ← env.skillsDirPath
  greeting/
    SKILL.md                ← 一个技能 = 一层目录下这一个文件
  other_skill/
    SKILL.md
```

`SKILL.md` 推荐结构（见 `skills/greeting/SKILL.md` 实例）：

```markdown
---
name: greeting
description: Greet the user politely at the start of a conversation and offer help.
---

正文：触发条件、步骤、示例……
```

## 3. 解析规则（parse_frontmatter + scan）

### frontmatter 判定

| 情形 | 结果 |
|---|---|
| 首行不是恰好 `---`（容忍 `\r\n`） | 无 frontmatter，全文即 body |
| 找不到闭合 `---` | 同上（**不报错**，整个文件当正文） |
| YAML 解析异常 / 结果不是 dict | `metadata = {}`，正文照常取 |
| 正常 | metadata + `---` 之后 strip 的 body |

### 字段回退链

- **name**：`metadata["name"]`（必须是 str，strip 后非空）→ 否则用**目录名**；
- **description**：`metadata["description"]`（同上）→ 否则用**正文第一行**；
- 两者最终都过一遍清洗：`lstrip("# ")` 去标题符号，再按连续空白折叠
  （`"#  Hello   World " → "Hello World"`）。

注意清洗只处理**开头**的 `#` 和空格；YAML 里写成 `description: on` 会被
safe_load 解析成布尔 `True`，过不了 isinstance(str) 检查 → 静默走正文
第一行回退，不报错。

### scan 行为

- 先 `clear()` 再全量重扫——幂等，可对同一实例手动再调；
- `skillsDir` 不存在：**静默返回**，目录为空（`catalog()` 输出
  `(no skills found)`），不报错不告警；
- 只认 `glob("*/SKILL.md")`（固定一层目录），`sorted()` 保证顺序稳定；
- 防越权：`manifest.resolve().is_relative_to(skillsRoot)`，符号链接指向
  目录外的 manifest 被跳过；
- 存储：`self.skills[name] = {name, description, content}`，其中
  **content 是整个文件的原始全文（含 frontmatter），不是剥离后的 body**。

## 4. catalog() —— 目录级注入

每技能一行 `- {name}: {description}`，`\n` 连接；空表返回
`(no skills found)`。返回值被拼进系统提示（§8.2）。

## 5. load(name) —— 正文级加载

- 精确字符串匹配（区分大小写），无模糊/别名；
- 命中：返回文件全文（模型会看到 frontmatter 的 `---` 块，无害但非正文）；
- 未命中：返回字符串 `Error: Unknown skill '{name}'`——**不抛异常**。
  这条会以普通 tool_result 身份回到对话里（没有 `is_error` 标记），
  由模型自行决定改名字重试或放弃。

## 6. 贯穿全程的不变量（改代码前必读）

1. **扫描只发生在构造时一次**：`SkillManager(...)` → `scan()`。运行中新增
   /修改 `skills/` 下的文件，**当前进程不可见**，重启才生效。没有热重载。
2. **系统提示同样冻结在启动时**：`Loop.__init__` 调一次
   `build_system_prompt()`，`catalog()` 的结果就固化进 `self.system_prompt`
   ——与不变量 1 叠加，等于技能集是"进程级常量"。
3. **后写覆盖**：两个技能解析出同名 name 时，`sorted()` 序靠后的目录
   静默覆盖前者（dict 赋值），无重名告警。
4. **正文级内容的信任级别等同用户输入**：`load_skill` 返回的全文原样进入
   对话，技能文件里写什么模型就读到什么——技能目录是可信边界，
   别让不可信方往 `skills/` 里塞文件。
5. **name 为空的兜底是目录名**：frontmatter 缺失/畸形不会导致技能消失，
   只会让它以目录名 + 正文首行摘要的形态出现。

## 7. 已知边界与坑点

- **description 回退到正文首行**时，若正文以 `# 标题` 开头，清洗后目录里
  展示的是标题而非摘要——能用 frontmatter 就用 frontmatter；
- **无长度上限**：`SKILL.md` 写多大，`load_skill` 的 tool_result 就多大；
  超大技能会直接挤占上下文，只能靠 CompactManager 事后压缩兜底
  （两者互不感知，见 §9）；
- **未知技能名不报错**：模型可能反复 `load_skill("Greeting")`（大小写不同）
  拿 `Error:` 字符串而不自知，依赖模型自身的纠错行为；
- **一层目录硬编码**：`skills/a/b/SKILL.md` 不会被发现；
- **路径基准是进程 cwd**：`env.skillsDirPath = Path.cwd() / "skills"`，
  从别的目录启动程序就会找错地方（且因不变量里"目录不存在静默为空"，
  症状只是"没有技能"，没有报错）。

## 8. 主循环接线

### 8.1 构造与实例归属

```python
# tools_manager.py __init__
self.skillManager = SkillManager(self.env.skillsDirPath)
```

实例挂在 `ToolsManager` 上（不是 `Loop`），全仓库唯一实例。

### 8.2 触发点一览

| 触发 | 时机 | 动作 |
|---|---|---|
| 目录注入 | `Loop.__init__` → `build_system_prompt()` | `skills_catalog()` → `catalog()` 拼进系统提示 |
| 正文加载 | 模型调用 `load_skill(name)` 工具 | `run_load_skill` → `skillManager.load(name)`，结果作为 tool_result 回填 |

`LOAD_SKILL` schema 注册进 `self.tools`（模型可见），handler 挂在
`toolsHandlers["load_skill"]`——与 `compact` 不同，这是**正常路由**的工具，
不需要主循环特殊拦截（对照 docs/COMPACT_MANAGER.md §9）。

## 9. 与其他模块的关系

| 模块 | 关系 |
|---|---|
| `env.py` | 提供 `skillsDirPath`（`<cwd>/skills`） |
| `tools_manager.py` | 持有唯一 `SkillManager` 实例；`LOAD_SKILL` schema + handler；**`subTools` 不含 `load_skill`**——子代理看不到也无法加载技能 |
| `loop.py` | 系统提示注入目录（§8.2）；对技能本体无直接依赖 |
| `compact_manager.py` | 互不感知：`load_skill` 的大结果同样会经过 ③④ 级压缩被落盘换预览，但技能没有"重新拉取"的专用通道，只能再调一次 `load_skill` |

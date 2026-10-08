from __future__ import annotations

import re
import subprocess
from pathlib import Path

from env import Env
from log import log_info, log_warn
from task_manager import Task, TaskManager

# ===== git worktree 隔离层（移植自 s13_agent_teams/code.py L429-769）=====
#
# 一句话心智模型：worktree = 同一仓库多个工作目录，各检出独立分支（wt/{name}），
# 队友之间物理隔离文件，互不踩踏。
#
# 工具暴露边界：create_worktree 供 Lane D 注册为 LLM 工具；
# remove_worktree **故意不注册为 LLM 工具**——它是全类唯一会销毁磁盘代码的操作，
# 五重安全闸的语义太重，交由人工/自动化清理路径处理（Lane D 不会给它建 schema）。
#
# 与 s13 的有意差异：
# - s13 在 release/create 中触碰 plan_gates / active_teammates 的语句全部跳过
#   （team_lock 域是 Lane C 的职责），本类只保留 task/worktree 域逻辑；
# - 控制台输出统一走 log.py（tag "wt"），不允许 print；
# - validate_worktree_name 由"返回错误串"改为 fail-closed 抛 ValueError；
# - task_worktree_cwd 由 s13 的 (path, error) 元组收敛为 str | None，
#   以对齐 Lane A 的 worktree_cwd_resolver 回调契约。


class WorktreeManager:
    ### worktree 名字白名单：首字符必须字母数字（防隐藏文件、防被当命令行参数），
    ### 总长 1-64，允许字母数字点下划线横线
    VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    def __init__(self, env: Env, task_manager: TaskManager):
        self.env = env
        self.taskManager = task_manager
        # ---- Lane A 对接契约：把两个回调 wired 到 task_manager ----
        # validator：claim_task 关⑥校验 task.worktree 名字是否存在（None/空名=未绑定=放行）
        # resolver：认领/租约自愈时解析任务工作目录字符串，None 回退 workDirPath
        task_manager.worktree_validator = self.is_valid_worktree
        task_manager.worktree_cwd_resolver = self.task_worktree_cwd

    # -- 名字与路径 --

    ### 名字格式校验（fail-closed）：非法直接抛 ValueError，文案直接给 AI 看。
    ### 正则已挡独立 '.'/'..'；这层再显式禁 '..' 是防 a..b 内嵌、也防未来正则被
    ### 放宽——防御层互不信任（s13 L440-447）
    def validate_worktree_name(self, name: str) -> None:
        if not isinstance(name, str) or not self.VALID_WORKTREE_NAME.fullmatch(name):
            raise ValueError("worktree name must be 1-64 letters, digits, dots, "
                             "underscores, or dashes, and start with a letter or digit")
        if name in {".", ".."} or ".." in name:
            raise ValueError("worktree name cannot contain '..'")

    ### 由名字定位 worktree 目录并做三关越界校验（s13 L452-458）：
    ### 隔离根必须在工作目录内；路径必须在隔离根内；不许拿隔离根本身当一个 worktree
    ### （删除时会端掉全部隔离区）
    def _worktree_path(self, name: str) -> Path:
        root = self.env.worktreesDirPath.resolve()
        if not root.is_relative_to(self.env.workDirPath.resolve()):
            raise ValueError("Worktrees root escapes the working directory")
        path = (self.env.worktreesDirPath / name).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError(f"Worktree path escapes directory: {name!r}")
        return path

    ### 分支名唯一派生自目录名（wt/{name}），所有分支判断都走这一个函数
    def _worktree_branch(self, name: str) -> str:
        return f"wt/{name}"

    # -- Git 子进程 --

    ### 列表传参禁 shell 注入；errors='replace' 遇烂字节不崩；timeout=30 防凭据弹窗吊死进程。
    ### stdout+stderr 合并（git 习惯把正常信息写 stderr）；空输出替换成 "(no output)"：
    ### 空串会让模型以为工具没跑而幻觉补脑；异常全部转成 (False, 文本)，调用方永不 try。
    ### 内部逻辑要全量输出一律用本函数（s13 L470-480）
    def _run_git(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["git", *args], cwd=cwd or self.env.workDirPath,
                capture_output=True, text=True, errors="replace", timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output or "(no output)"

    ### 对外版：截 5000 字符保护模型上下文（s13 L484-487）。
    ### 返回 (ok, text) 元组与 s13 一致，Lane D 注册工具时自行格式化
    def run_git(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        ok, output = self._run_git(args, cwd)
        return ok, output[:5000]

    # -- 台账（唯一事实源）--

    ### worktree 的唯一事实来源是 `git worktree list --porcelain`：ls 目录判不了——
    ### 手删目录=脏条目、有目录没注册≠worktree。--porcelain 是机器稳定格式；
    ### splitlines()+[""] 哨兵空行省掉收尾特判；partition(" ") 让带空格的路径不被切碎。
    ### 读不到台账返回 ({}, error)：分不清"没有"和"查不到"，就绝不做删除决定
    ### （s13 L494-509，内部原语，保留逐条粒度与错误串供 create/remove 精确报错）
    def _parse_registry(self) -> tuple[dict[Path, dict[str, str]], str | None]:
        ok, output = self._run_git(["worktree", "list", "--porcelain"])
        if not ok:
            return {}, f"cannot read Git worktree registry: {output}"
        entries: dict[Path, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in output.splitlines() + [""]:
            if not line:
                raw_path = current.get("worktree")
                if raw_path:
                    entries[Path(raw_path).resolve()] = current
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return entries, None

    ### 公开视图：name -> 路径字符串（s13 L494-531 两层逻辑收拢为一个 dict）。
    ### 逐层过滤：路径位于隔离根之下（且非隔离根本身）、目录真实存在、
    ### branch 必须等于 refs/heads/wt/{name}——被人手动 checkout 走的目录不算我们的 worktree。
    ### 台账读不出来返回空 dict：fail-closed，"查不到"一律按"没有"处理，
    ### 调用方据此拒绝放行/拒绝删除
    def registered_worktrees(self) -> dict[str, str]:
        entries, error = self._parse_registry()
        if error:
            log_warn("wt", error)
            return {}
        root = self.env.worktreesDirPath.resolve()
        result: dict[str, str] = {}
        for path, info in entries.items():
            name = path.name
            if path == root or not path.is_relative_to(root):
                continue
            expected = f"refs/heads/{self._worktree_branch(name)}"
            if info.get("branch") != expected:
                continue
            if not path.is_dir():
                continue
            result[name] = str(path)
        return result

    ### 单项精确校验（保留 s13 L515-531 的逐项错误文案，供 create/remove 报错）。
    ### 返回 (path, error) 元组链式传递；error 非 None 即不可用
    def _registered_entry(self, name: str) -> tuple[Path | None, str | None]:
        try:
            path = self._worktree_path(name)
        except ValueError as exc:
            return None, str(exc)
        entries, error = self._parse_registry()
        if error:
            return None, error
        if path not in entries:
            return None, f"worktree '{name}' is not registered with Git"
        if not path.is_dir():
            return None, f"worktree '{name}' is missing at {path}"
        expected = f"refs/heads/{self._worktree_branch(name)}"
        if entries[path].get("branch") != expected:
            return None, (f"worktree '{name}' is not registered on expected "
                          f"branch '{self._worktree_branch(name)}'")
        return path, None

    # -- Lane A 回调 --

    ### 注入 task_manager.worktree_cwd_resolver（对齐 s13 L536-541）。
    ### 未绑定返回 None，由 Lane A 回退 str(workDirPath)；已绑定必须出现在
    ### registered_worktrees（目录存在+分支匹配）才返回该路径，否则 fail-closed 返回 None。
    ### 用 None 而非 raise：它是 resolver 注入——拒绝放行的职责由下面的 validator 承担，
    ### Lane A 的 _task_cwd 会先过 validator 再走 resolver，坏绑定根本到不了这里
    def task_worktree_cwd(self, task: Task) -> str | None:
        if not task.worktree:
            return None
        return self.registered_worktrees().get(task.worktree)

    ### 注入 task_manager.worktree_validator（claim_task 关⑥）。
    ### None/空名 = 未绑定 worktree = 天然合法返回 True；否则须在台账中存在
    def is_valid_worktree(self, name: str | None) -> bool:
        if not name:
            return True
        return name in self.registered_worktrees()

    # -- 租约热路径 --

    ### 每次工具调用前的"验租"热路径（s13 L550-571）——内存租约当缓存、磁盘当真相。
    ### 无租约：Lead（"agent"）空闲走主工作目录；队友无租约无活可干 -> ValueError 熔断。
    ### 有租约：从磁盘重载任务——status 必须仍是 in_progress/completed 且 owner 匹配
    ### （completed 放行是配合"回合边界才退租"）；坏 worktree 绑定 fail-closed 熔断；
    ### 重算 cwd 与登记值不一致 -> 回写 assignments 自愈（s13 在此处是 raise，
    ### lcc 按 Lane A 契约改为回写：assignments 的本体归 task_manager，回写即登记新事实源）。
    ### 此函数 raise 而非返回错误串：环境坏了不是 AI 能自救的
    def assignment_cwd(self, owner: str) -> str:
        with self.taskManager.task_store_lock():
            assignment = self.taskManager.assignments.get(owner)
            if not assignment:
                if owner == "agent":
                    return str(self.env.workDirPath)
                raise ValueError(f"No active assignment for {owner}")

            task = self.taskManager.load_task(str(assignment["task_id"]))
            if task.status not in {"in_progress", "completed"} or task.owner != owner:
                raise ValueError(f"Assignment for {owner} is no longer active")

            if task.worktree:
                cwd = self.task_worktree_cwd(task)
                if cwd is None:
                    raise ValueError(f"Worktree '{task.worktree}' binding is "
                                     f"broken for task {task.id}")
            else:
                cwd = str(self.env.workDirPath)

            if cwd != assignment.get("cwd"):
                self.taskManager.assignments[owner] = {"task_id": task.id, "cwd": cwd}
            return cwd

    ### 善终退租 / 线程猝死清算：逻辑全部在 Lane A 的 task_manager（含租约 pop、
    ### 版本 bump、回调触发），此处仅保留薄封装让 worktree 域调用方（Lane C/主循环）
    ### 走同一个入口。s13 在此处的 plan_gates 联动（L587-588、L606-607）属 Lane C
    ### 回调职责，本实现跳过
    def release_completed_assignment(self, owner: str) -> bool:
        return self.taskManager.release_completed_assignment(owner)

    def release_teammate_assignment(self, owner: str) -> None:
        self.taskManager.release_teammate_assignment(owner)

    # -- 生命周期 --

    ### 主线=先验后动：可失败的事全部分类提前，真操作压最后，失败时讲清现场
    ### （s13 L612-701）。整个读-验证-建目录-改 task 的临界区置于 task_store_lock() 内
    ### （git 子进程也在锁内执行，保持简单；lcc 无独立 task_lock，Lane A 的单把大锁就是全部）。
    ### 锁内逐关校验：任务存在；pending 且无主（不能给在干的活换工作目录）；
    ### 未绑过 worktree（一任务一 worktree）；名字没被别的任务占用（共享目录=隔离失效）；
    ### 目标路径不存在；rev-parse --show-toplevel==workDirPath（只许给本仓库开）；
    ### check-ref-format --branch（正则合法≠git 分支名合法，命名裁判外包给 git）；
    ### 分支不存在（worktree add -b 是新建分支）；台账读不出=拒绝动手；path 不在台账
    def create_worktree(self, name: str, task_id: str) -> str:
        try:
            self.validate_worktree_name(name)
            path = self._worktree_path(name)
        except ValueError as exc:
            return f"Error: {exc}"
        branch = self._worktree_branch(name)

        with self.taskManager.task_store_lock():
            if not self.taskManager.exists(task_id):
                return f"Error: Task {task_id} not found"
            task = self.taskManager.load_task(task_id)
            if task.status != "pending" or task.owner is not None:
                return f"Error: Task {task_id} must be pending and unowned"
            if task.worktree:
                return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
            if any(t.worktree == name for t in self.taskManager.list_tasks() if t.id != task_id):
                return f"Error: Worktree '{name}' is already bound to another task"
            if path.exists():
                return f"Error: Worktree path already exists: {path}"

            ok, root = self.run_git(["rev-parse", "--show-toplevel"])
            if not ok or Path(root).resolve() != self.env.workDirPath.resolve():
                return "Error: Working directory must be the root of a Git repository"
            ok, branchCheck = self.run_git(["check-ref-format", "--branch", branch])
            if not ok:
                return f"Error: Invalid worktree branch '{branch}': {branchCheck}"
            exists, _ = self.run_git(["show-ref", "--verify", "--quiet",
                                      f"refs/heads/{branch}"])
            if exists:
                return f"Error: Branch '{branch}' already exists"
            # 台账读不出来本身就是拒绝理由（分不清状态=不动手）
            entries, registryError = self._parse_registry()
            if registryError:
                return f"Error: {registryError}"
            if path in entries:
                return f"Error: Worktree path is already registered: {path}"

            # 全部校验通过，真操作登场：worktree add -b 一步建分支+检出+登记
            self.env.worktreesDirPath.mkdir(parents=True, exist_ok=True)
            ok, result = self.run_git(["worktree", "add", "-b", branch,
                                       str(path), "HEAD"])
            # git 失败不能信"啥也没发生"：重查台账/分支/目录，看有没有登记残留
            if not ok:
                entries, registryError = self._parse_registry()
                branchExists, _ = self.run_git(
                    ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
                artifacts = []
                if path.exists():
                    artifacts.append(f"checkout path '{path}'")
                if registryError is None and path in entries:
                    artifacts.append("registered Git worktree")
                if branchExists:
                    artifacts.append(f"branch '{branch}'")
                # 有残留→返回 Partial operation 长文案：不自动清理（半坨现场可能含用户
                # 数据，删错不可逆），给出人肉检查步骤；零残留→普通 Git error
                if artifacts:
                    return (
                        "Partial operation: git worktree add reported an error "
                        f"after leaving {', '.join(artifacts)}. Task {task_id} "
                        "remains unbound and no Git data was deleted. Run "
                        f"`git worktree list`, inspect '{path}' and '{branch}', "
                        "then keep or remove those artifacts manually after "
                        f"preserving any work. Git error: {result}"
                    )
                return f"Git error: {result}"

            # git 成功但 save 失败→Partial success：Git 数据保留待人工恢复。
            # 孤儿 worktree 无害，下次同名会被上面的路径关挡住
            try:
                task.worktree = name
                self.taskManager.save(task)
            except Exception as exc:
                return (f"Partial success: Worktree '{name}' was created at "
                        f"{path} on branch '{branch}', but task binding failed: "
                        f"{exc}. Git data was retained for manual recovery.")

        log_info("wt", f"created: {name} at {path}")
        return f"Created worktree '{name}' at {path} for task {task_id}"

    ### 全类唯一会销毁磁盘代码的操作，task_store_lock 内五道安检逐一过
    ### （s13 L706-769）。分支永远保留：目录只是检出视图，已提交内容都在分支上——保底逻辑。
    ### ①正牌资格：未注册的目录哪怕真实存在也拒碰——只清理能证明归属的东西；
    ### ②必须绑着任务（来源不明不删）；③绑定任务必须全部 completed；
    ### ④没有任何租约的 cwd 指向此目录（配合"complete 后租约活到回合边界"，让 AI 等一拍）；
    ### ⑤git status --porcelain --ignored 必须干净：连 ignored 残留都算脏；
    ###   命令失败=分不清状态=不删（fail-closed）；discard_changes=True 只给 git 加
    ###   --force、只允许跳过第⑤关，①-④照过——它是"确认要扔"的保险丝，不是万能钥匙。
    ### 本方法故意不注册为 LLM 工具（见类注释）
    def remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        try:
            self.validate_worktree_name(name)
        except ValueError as exc:
            return f"Error: {exc}"

        with self.taskManager.task_store_lock():
            # ①正牌资格
            path, error = self._registered_entry(name)
            if error:
                return f"Error: {error}"

            # ②必须绑着任务
            bound = [task for task in self.taskManager.list_tasks()
                     if task.worktree == name]
            if not bound:
                return f"Error: Worktree '{name}' is not bound to a task"

            # ③绑定任务必须全部 completed（active 时报第一个 id："先完成再删"）
            active = [task for task in bound if task.status != "completed"]
            if active:
                return (f"Error: Worktree '{name}' is bound to active task "
                        f"{active[0].id}; complete it before removal")

            # ④没有任何租约的 cwd 指向此目录
            leased = [owner for owner, assignment in self.taskManager.assignments.items()
                      if Path(assignment["cwd"]).resolve() == path.resolve()]
            if leased:
                return (f"Error: Worktree '{name}' is still in use by "
                        f"{', '.join(sorted(leased))}; wait for the turn to end")

            # ⑤工作区必须干净；判空用 != "(no output)" 复用 _run_git 的占位符约定
            ok, status = self._run_git(["status", "--porcelain", "--ignored"], cwd=path)
            if not ok:
                return f"Error: Cannot verify worktree '{name}' status: {status}"
            if status != "(no output)" and not discard_changes:
                changed = len([line for line in status.splitlines() if line.strip()])
                return (f"Error: Worktree '{name}' has {changed} uncommitted "
                        "change(s); preserve or discard them manually")

            # git 真正执行移除：只删检出视图的目录，分支分毫不动
            args = ["worktree", "remove"]
            if discard_changes:
                args.append("--force")
            args.append(str(path))
            ok, result = self.run_git(args)
            if not ok:
                return f"Git error: {result}"

            # 删成后把绑定任务的 worktree 清 None。解绑失败返回 Partial success：
            # 悬空引用之后任何 task_worktree_cwd 都会 fail-closed 返回 None，
            # 脏引用不会静默放行任务
            try:
                for task in bound:
                    task.worktree = None
                    self.taskManager.save(task)
            except Exception as exc:
                return (f"Partial success: Worktree '{name}' was removed and "
                        f"branch '{self._worktree_branch(name)}' retained, but task "
                        f"unbinding failed: {exc}. Manual recovery is required.")

        log_info("wt", f"removed: {name}; branch retained")
        return f"Worktree '{name}' removed; branch '{self._worktree_branch(name)}' retained"

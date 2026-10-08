from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from env import Env
import json
import os
import re
import secrets
import threading
from datetime import datetime
from log import log_info

# 跨进程文件锁的双实现：fcntl 仅 POSIX、msvcrt 仅 Windows，按 os.name 条件导入，
# 供 task_store_lock 使用（移植自 s13 L41-45）
if os.name != "nt":
    import fcntl
else:
    import msvcrt

@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    timestamp: float
    # 任务的依赖列表
    blockedBy: list[str]
    # 绑定的 worktree 隔离目录名（None=在主工作目录干活）；
    # 旧任务文件缺此字段时 Task(**data) 自动回退 None 保持兼容
    worktree: str | None = None


class TaskManager:
    ### 匹配task_******** 合法id必须是task_ + 8位十六进制小写字符
    TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
    ### 跨进程锁文件：藏在任务目录里，点开头保证 glob("task_*.json") 与 list() 都看不见它
    LOCK_FILE_NAME = ".lock"

    def __init__(self, directory: Path):
        self.env = Env()
        self.directory = directory
        # 线程内重入锁：管同一进程内多线程的读-改-写不撕裂
        self._lock = threading.RLock()
        # 线程局部状态：存嵌套深度计数与跨进程锁句柄，配合 task_store_lock 实现可重入
        self._storeState = threading.local()
        # owner -> {"task_id": str, "cwd": str} 的内存"租约"：每人同时只干一活，
        # 文件工具按 owner 从这里路由 cwd；key "agent" 保留给 Lead 本人
        self.assignments: dict[str, dict] = {}
        # owner -> int 版本号：换一次任务就 +1，让旧的 plan 审批作废
        self.assignment_versions: dict[str, int] = {}
        # ---- 可注入回调（agent teams 各 Lane 对接点，全部默认 None = 跳过） ----
        # 租约版本推进后的 team 侧动作（Lane C 接 plan_gates：把非豁免的审批门打回 required）；
        # 注意：在持有 self._lock 的状态下被调用，实现方须遵守 task_lock -> team_lock 的锁顺序
        self.on_assignment_advanced: Callable[[str], None] | None = None
        # 租约释放后的 team 侧动作（Lane C 接 plan_gates：把审批门重置 not_required）
        self.on_assignment_released: Callable[[str], None] | None = None
        # worktree 名称有效性校验（Lane B 接 WorktreeManager）；None 时视为有效
        self.worktree_validator: Callable[[str], bool] | None = None
        # 任务工作目录解析（Lane B 接 WorktreeManager）：返回目录字符串，
        # None 则回退 str(env.workDirPath)
        self.worktree_cwd_resolver: Callable[[Task], str | None] | None = None
        # 完成前置审批门校验（Lane C 接 plan_gates）：入参 owner，返回非 None 则拒绝完成并回显该消息
        self.plan_gate_check: Callable[[str], str | None] | None = None

    ### 返回task的根目录
    def _root(self, create: bool = False) -> Path:
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve(self.env.workDirPath)
        if not root.is_relative_to(self.env.workDirPath.resolve()):
            raise ValueError("TaskManager escapes the workspace")
        return root

    ### 根据task_id返回文件路径
    def _path(self, task_id: str, create_root: bool = False) -> Path:
        if not isinstance(task_id, str) or not self.TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID:{task_id!r}")

        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Invalid task ID:{task_id!r}")
        return path

    ### 跨进程锁文件路径（位于任务目录内，随 _root 一起做越界校验）
    def _lockPath(self) -> Path:
        return self._root(create=True) / self.LOCK_FILE_NAME

    ### 两层锁：线程 RLock + .lock 锁文件上的跨进程排他锁（fcntl.flock / msvcrt 字节锁双实现）。
    ### threading.local 计数嵌套深度，只在最外层真正 acquire/release 跨进程锁——
    ### RLock 管线程、文件锁管多进程，嵌套调用不重复上锁（移植自 s13 L92-128）
    @contextmanager
    def task_store_lock(self):
        with self._lock:
            depth = getattr(self._storeState, "depth", 0)
            # 只有最外层才真正开文件句柄+上跨进程锁，内层嵌套只动计数器
            if depth == 0:
                lockPath = self._lockPath()
                fd = os.open(lockPath, os.O_RDWR | os.O_CREAT)
                if os.name != "nt":
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:
                    # Windows 无 flock：用 msvcrt 对锁文件第 0 字节做范围锁。LK_LOCK 会
                    # 阻塞重试约 10 秒后抛 OSError，捕获后立刻重试，等效 flock 的无限等待
                    os.lseek(fd, 0, os.SEEK_SET)
                    while True:
                        try:
                            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                            break
                        except OSError:
                            continue
                self._storeState.handle = fd
            self._storeState.depth = depth + 1
            try:
                yield
            finally:
                self._storeState.depth -= 1
                # 回到最外层才真正解锁并关闭句柄（可重入语义与 RLock 对齐）
                if self._storeState.depth == 0:
                    fd = self._storeState.handle
                    if os.name != "nt":
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    os.close(fd)
                    del self._storeState.handle

    ### 版本号 +1（换活即作废旧审批）；随后触发可选回调 on_assignment_advanced（Lane C 的 plan_gates
    ### 接入点，None 时跳过）。返回 bump 后的新版本号。
    ### 在持有 self._lock 状态下调用回调，与 s13 L134-151 一致：全系统固定 task_lock -> team_lock 锁顺序
    def advance_assignment_version(self, owner: str) -> int:
        with self._lock:
            version = self.assignment_versions.get(owner, 0) + 1
            self.assignment_versions[owner] = version
            if self.on_assignment_advanced is not None:
                self.on_assignment_advanced(owner)
            return version

    ### 解析任务工作目录：返回 (cwd字符串, 错误串)——错误非 None 表示 worktree 绑定不可用，
    ### 调用方须 fail-closed（拒绝认领/完成）。校验与解析全部走可注入回调，
    ### 本模块绝不 import worktree_manager（避免与并行 Lane 耦合）
    def _task_cwd(self, task: Task) -> tuple[str | None, str | None]:
        if task.worktree:
            # 未注入 validator（Lane B 尚未接线）时视为有效
            if self.worktree_validator is not None and not self.worktree_validator(task.worktree):
                return None, f"worktree '{task.worktree}' is not available for task {task.id}"
        if self.worktree_cwd_resolver is not None:
            cwd = self.worktree_cwd_resolver(task)
            if cwd is not None:
                return cwd, None
        return str(self.env.workDirPath), None

    ### 扫盘找该 owner 的 in_progress 任务（从磁盘侧兜底"一人一活"）；调用方须已持锁
    def _owner_in_progress(self, owner: str) -> Task | None:
        return next((task for task in self.list()
                     if task.status == "in_progress" and task.owner == owner), None)

    ### 根据task_id判断是否含有该文件
    def exists(self, task_id: str) -> bool:
        return self._path(task_id).is_file()

    ### 创建任务：subject是任务标题，description: 描述，直接落盘。
    ### 双层锁内最多 100 次尝试：open("x")=操作系统层原子"不存在才创建"，
    ### 两进程同刻算出同一 id 也只有一个成功，另一个 FileExistsError 换号重来
    def create(self, subject: str, description: str = "") -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")

        with self.task_store_lock():
            for _ in range(100):
                task = Task(id=f"task_{secrets.token_hex(4)}",
                            subject=subject,
                            description=description,
                            status="pending",
                            owner=None,
                            timestamp=datetime.now().timestamp(),
                            blockedBy=[],
                            worktree=None)

                try:
                    with self._path(task.id, create_root=True).open("x", encoding="utf-8") as handle:
                        json.dump(asdict(task), handle, indent=2)
                    return task
                except FileExistsError:
                    continue

        raise RuntimeError("Could not allocate a unique task ID")

    ### task_id是否依赖target_id
    def _depends_on(self, taks_id: str, target_id: str) -> bool:
        pending = [taks_id]
        visited = set()

        while pending:
            current = pending.pop()
            if current == target_id:
                return True

            if current in visited:
                continue

            visited.add(current)
            pending.extend(self.load(current).blockedBy)

        return False

    ### 给一个任务批量添加前置依赖，是整个任务系统里入参校验最密的函数
    def update_dependencies(self, task_id: str, add_blocked_by: list[str]) -> Task:
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list of task IDs")

        with self.task_store_lock():
            task = self.load(task_id)
            if task.status != "pending" or task.owner is not None:
                raise ValueError(f"Task {task_id} dependencies can only be updated while pending and unowned")

            dependencies = list(dict.fromkeys(add_blocked_by))
            for dependency in dependencies:
                if dependency == task_id:
                    raise ValueError("Task cannot depend on itself")

                if not self.exists(dependency):
                    raise ValueError(f"Dependency not found:{dependency}")

                if dependency not in task.blockedBy and self._depends_on(dependency, task_id):
                    raise ValueError(f"Dependency cycle detected: {task_id} - > {dependency}")

            for dependency in dependencies:
                if dependency not in task.blockedBy:
                    task.blockedBy.append(dependency)

            self.save(task)
            return task

    ### 纯读只需 RLock、不用 flock：os.replace 保证盘上任何时刻都是完整 JSON。
    ### 旧任务文件缺 worktree 字段时 dataclass 默认值 None 自动兼容
    def load(self, task_id: str) -> Task:
        with self._lock:
            data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
            task = Task(**data)
            if task.id != task_id:
                raise ValueError(f"Task file ID does not match {task_id}")

            if task.status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid task status:{task.status}")
            return task

    ### 原子写：先写临时文件（名字带 pid+线程 id 永不撞；点开头+.tmp 让 glob task_*.json 看不见它），
    ### 再 os.replace 原子改名——POSIX/NTFS 都保证读者永远看不到半截文件；finally 负责清理
    def save(self, task: Task) -> None:
        with self.task_store_lock():
            path = self._path(task.id, create_root=True)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def list(self) -> list[Task]:
        with self._lock:
            if not self.directory.exists():
                return []

            root = self._root()
            return [self.load(path.stem)
                    for path in sorted(root.glob("task_*.json"))]

    def create_task(self, subject: str, description: str = "") -> Task:
        return self.create(subject, description)

    def update_task(self, task_id: str, addBlockedBy: list[str]) -> Task:
        return self.update_dependencies(task_id, addBlockedBy)

    def load_task(self, task_id: str) -> Task:
        return self.load(task_id)

    def list_tasks(self) -> list[Task]:
        return self.list()

    def get_task(self, task_id: str) -> str:
        return json.dumps(asdict(self.load_task(task_id)), indent=2)

    def incomplete_dependencies(self, task: Task) -> list[str]:
        incomplete = []
        for dependency in task.blockedBy:
            try:
                if self.load_task(dependency).status != "completed":
                    incomplete.append(dependency)
            except (FileNotFoundError, ValueError):
                incomplete.append(dependency)

        return incomplete

    def can_start(self, task_id: str) -> bool:
        return not self.incomplete_dependencies(self.load_task(task_id))

    ### 认领task：同一把大锁内连过六关（移植自 s13 L358-388）：
    ### ①必须 pending ②owner 为空（脏数据兜底）
    ### ③内存租约账本里没有他（报错"须先结束当前工作回合"而非"完成任务"）
    ### ④磁盘上没有他的 in_progress 任务（租约在内存里、重启即失——③④故意冗余互为兜底）
    ### ⑤can_start，否则列出拦路者 ⑥绑定的 worktree 必须完好（_task_cwd）
    ### 全过：改盘（owner+in_progress）、登记租约（cwd=worktree 或工作目录）、bump 版本。
    ### 成功返回以 "Claimed " 开头的字符串（Lane C 依赖该前缀判断）
    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        with self.task_store_lock():
            task = self.load_task(task_id)
            if task.status != "pending":
                return f"Task {task_id} is {task.status}, cannot claim"

            if task.owner:
                return f"Task {task_id} is already owned by {task.owner}"

            # 关③：先查内存租约账本
            assignment = self.assignments.get(owner)
            if assignment:
                return (f"Owner {owner} must finish the current work turn for "
                        f"{assignment['task_id']} before claiming another task")

            # 关④：再查磁盘账本——内存租约重启即失，与③互为冗余兜底
            current = self._owner_in_progress(owner)
            if current:
                return (f"Owner {owner} must complete {current.id} before "
                        "claiming another task")

            # 关⑤：依赖未全完成就报出拦路者名单
            if not self.can_start(task_id):
                return f"Blocked by: {self.incomplete_dependencies(task)}"

            # 关⑥：worktree 绑定必须完好（fail-closed）
            cwd, error = self._task_cwd(task)
            if error:
                return f"Cannot claim {task_id}: {error}"

            task.owner = owner
            task.status = "in_progress"
            self.save(task)
            self.assignments[owner] = {"task_id": task.id, "cwd": cwd}
            self.advance_assignment_version(owner)

        log_info("task", f"claim {task.subject} -> in_progress (owner: {owner})")
        return f"Claimed {task.id} ({task.subject})"

    ### 完成任务：三道前置（in_progress、调用者==owner、审批门），完成后重扫刚被解锁的
    ### pending 任务返回 Unblocked 报告（保持既有 Lead 行为）。
    ### 故意不删租约：到回合边界才由 release_completed_assignment 释放——同回合模型还可能跑自查工具，cwd 须继续路由
    def complete_task(self, task_id: str, owner: str = "agent") -> str:
        with self.task_store_lock():
            task = self.load_task(task_id)
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"

            if task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}; cannot complete"

            # 审批门（Lane C 接 plan_gates）：返回非 None 即拒绝交付
            if self.plan_gate_check is not None:
                gate_message = self.plan_gate_check(owner)
                if gate_message is not None:
                    return gate_message

            # 租约缺失/指错：重算回填（进程重启自愈；worktree 坏了仍 fail-closed）
            assignment = self.assignments.get(owner)
            if not assignment or assignment.get("task_id") != task.id:
                cwd, error = self._task_cwd(task)
                if error:
                    return f"Task {task_id} cannot complete: {error}"
                self.assignments[owner] = {"task_id": task.id, "cwd": cwd}

            ready_before = []
            for candidate in self.list_tasks():
                if candidate.status == "pending" and candidate.blockedBy and self.can_start(candidate.id):
                    ready_before.append(candidate.id)

            task.status = "completed"
            self.save(task)

            unblocked = []
            for candidate in self.list_tasks():
                if candidate.status == "pending" and candidate.blockedBy and candidate.id not in ready_before and self.can_start(candidate.id):
                    unblocked.append(candidate.subject)

        log_info("task", f"complete {task.subject}")
        message = f"Completed {task.id} ({task.subject})"
        if unblocked:
            message += f"\nUnblocked: {', '.join(unblocked)}"
            log_info("task", f"unblocked {', '.join(unblocked)}")

        return message

    ### 善终退租：只释放"租约任务已 completed"的（in_progress 不放，否则文件工具漂回主目录），幂等。
    ### pop 租约+版本 bump+触发释放回调（回合边界调用，移植自 s13 L576-589）
    def release_completed_assignment(self, owner: str) -> bool:
        with self.task_store_lock():
            assignment = self.assignments.get(owner)
            if not assignment:
                return False

            task = self.load_task(str(assignment["task_id"]))
            if task.status != "completed" or task.owner != owner:
                return False

            self.assignments.pop(owner, None)
            self.advance_assignment_version(owner)
            if self.on_assignment_released is not None:
                self.on_assignment_released(owner)
            return True

    ### 线程猝死的清算（移植自 s13 L594-607）：try 里把遗留 in_progress 任务打回
    ### pending/清 owner（半成品留在 worktree 里，别人可接手）；
    ### finally 无条件 pop 租约+版本+释放回调——内存清理必须完成，否则死人永久占用名字
    def release_teammate_assignment(self, owner: str) -> None:
        with self.task_store_lock():
            try:
                task = self._owner_in_progress(owner)
                if task:
                    task.status = "pending"
                    task.owner = None
                    self.save(task)
            finally:
                self.assignments.pop(owner, None)
                self.advance_assignment_version(owner)
                if self.on_assignment_released is not None:
                    self.on_assignment_released(owner)

    ### 纯侦察只读扫描（移植自 s13 L1417-1428）：pending+无主+can_start+worktree 可用 -> 候选清单。
    ### 不改任何状态，认领是下一步的事
    def scan_unclaimed_tasks(self) -> list[Task]:
        with self._lock:
            ready = []
            for task in self.list():
                if (task.status != "pending" or task.owner is not None
                        or not self.can_start(task.id)):
                    continue

                _, error = self._task_cwd(task)
                if not error:
                    ready.append(task)
            return ready

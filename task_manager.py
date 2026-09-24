from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import Path
from env import Env
import re
import secrets
import json
from datetime import datetime
from log import log_info

@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    timestamp:float
    # 任务的依赖列表
    blockedBy: list[str]


class TaskManager:
    ### 匹配task_******** 合法id必须是task_ + 8位十六进制小写字符
    TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
    def __init__(self, directory: Path):
        self.env = Env()
        self.directory = directory
        #  datetime.now().timestamp()

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

    ### 根据task_id判断是否含有该文件
    def exists(self, task_id:str)-> bool:
        return self._path(task_id).is_file()

    ### 创建任务：subject是任务标题，description: 描述， 直接落盘
    def create(self, subject: str, description: str="") -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")

        self._root(create=True)
        for _ in range(100):
            task = Task(id=f"task_{secrets.token_hex(4)}",
                        subject=subject,
                        description=description,
                        status="pending",
                        owner=None,
                        timestamp=datetime.now().timestamp(),
                        blockedBy=[])

            try:
                with self._path(task.id, create_root=True).open("x", encoding="utf-8") as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue

        raise RuntimeError("Could not allocate a unique task ID")

    ### task_id是否依赖target_id
    def _depends_on(self, taks_id:str, target_id: str) -> bool:
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

    def load(self, task_id: str) -> Task:
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")

        if task.status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid task status:{task.status}")
        return task

    def save(self, task: Task) -> None:
        self._path(task.id, create_root=True).write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")

    def list(self) -> list[Task]:
        if not self.directory.exists():
            return []

        root = self._root()
        # return 
        tasks = []
        for path in sorted(root.glob("task_*.json")):
            tasks.append(self.load(path.stem))

        return tasks

    def create_task(self, subject: str, description: str = "") -> Task:
        return self.create(subject, description)

    def update_task(self, task_id: str, addBlockedBy: list[str]) -> Task:
        return self.update_dependencies(task_id, addBlockedBy)

    def load_task(self, task_id: str) -> Task:
        return self.load(task_id)

    def list_tasks(self) -> list[Task]:
        return self.list()

    def get_task(self, task_id:str) -> str:
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


    ### 认领task
    def claim_task( self, task_id, owner: str = "agent") -> str:
        task = self.load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"

        dependencies = self.incomplete_dependencies(task)
        if dependencies:
            return f"Blocked by: {dependencies}"

        task.owner = owner
        task.status = "in_progress"
        self.save(task)

        log_info("task", f"claim {task.subject} -> in_progress (owner: {owner})")
        return f"Claimed {task.id} {task.subject}"


    ### 完成任务
    def complete_task(self, task_id: str, owner: str = "agent") -> str:
        task = self.load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"

        if task.owner != owner:
            return f"Task {task_id} is owned by {Task.owner}, not {owner}"


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
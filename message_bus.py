from __future__ import annotations
import json
import re
import threading
import time
from pathlib import Path
from log import log_info

### MessageBus 消息总线（移植自 s13 L1040-1144）：一人一个 .jsonl 邮箱文件，
### 追加写 + 破坏性读取。文件当邮箱=持久、可现场查验（cat 一下就知道信发没发出去），
### 崩了重启账还在。不建模块级单例，实例由 Lane D 创建并注入各协作者。

### 名字正则：fullmatch 防 "abc/../evil" 这种前缀合法后缀越狱
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
### 保留名："lead" 是保留收件箱、"agent" 是租约账本保留 key，都不许当队友名。
### 比较时用 casefold（Lane B/C/D 复用本常量做保留名判定）
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}


def is_valid_agent_name(name: str) -> bool:
    return bool(VALID_AGENT_NAME.fullmatch(name))


class MessageBus:
    """线程安全的文件邮箱，支持破坏性读取与条件等待。"""

    def __init__(self, mailbox_dir: Path, workspace_root: Path):
        self.mailboxDir = mailbox_dir
        self.workspaceRoot = workspace_root
        # 内存锁管并发 + JSONL 文件管存储
        self._lock = threading.RLock()
        # Condition 借用同一把 RLock：send/peek/wait 全共此锁，
        # wait() 才能原子地"释锁+睡"（各用各的锁就没这保障）
        self._changed = threading.Condition(self._lock)

    ### fail-closed 三关：名字正则 -> resolve 归一化 -> 目录包含校验，
    ### 外加邮箱根目录自证在工作区内。任何一关不过直接 raise（宁报错不猜）
    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not is_valid_agent_name(name):
            raise ValueError(f"Invalid mailbox name: {name!r}")
        root = self.mailboxDir.resolve()
        if not root.is_relative_to(self.workspaceRoot.resolve()):
            raise ValueError("Mailbox directory escapes workspace")
        path = (root / f"{name}.jsonl").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Mailbox path escapes directory: {name!r}")
        return path

    ### 命名契约：方法名带 "_unlocked" 后缀 = 调用方必须已持锁才能调
    ### 破坏性读取：读文件+unlink 一步走——"文件在=有信，文件没了=已读"，
    ### 不需要游标也不需要已读标记；天然附带"同一封信只会被取走一次"（at-most-once，无 ack）
    def _read_unlocked(self, name: str) -> list[dict]:
        inbox = self._path(name)
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs

    ### 六字段信封 {from,to,content,type,ts,metadata}：type 是协议路由键
    ### （message/plan_approval_request/shutdown_response...），metadata 带 request_id 对账凭据。
    ### open("a") 追加+行尾换行 = JSONL 多生产者安全；ensure_ascii=True 让邮箱纯 ASCII，
    ### 在 utf-8 之外再给 Windows 一层跨平台保险；每次 send 顺手 mkdir——检查便宜，
    ### 换"任何时候可用"。落盘+notify_all 在同一把 Condition 锁内完成=对等待者原子
    def send(self, frm: str, to: str, content: str,
             type: str = "message", metadata: dict | None = None) -> None:
        msg = {"from": frm, "to": to,
               "content": content, "type": type,
               "ts": time.time(), "metadata": metadata or {}}
        with self._changed:
            self.mailboxDir.mkdir(parents=True, exist_ok=True)
            with self._path(to).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(msg, ensure_ascii=True) + "\n")
            self._changed.notify_all()
        log_info("bus", f"{frm} -> {to}: ({type}) {content[:50]}")

    ### 破坏性读取：取走即清空
    def read(self, name: str) -> list[dict]:
        with self._lock:
            return self._read_unlocked(name)

    ### 非破坏性偷看：exists 且 st_size>0——查尺寸是防"0 字节崩溃残留"把等待
    ### 循环活锁（文件在却永远读不出信，wait 醒来又睡、死循环）
    def peek(self, name: str) -> bool:
        with self._lock:
            inbox = self._path(name)
            return inbox.exists() and inbox.stat().st_size > 0

    ### wait() 原子地"释锁+睡"、醒来重拿锁——关掉"查完邮箱到入睡之间恰好来信"
    ### 的竞态窗口。醒来必须 while 重验：叫醒它的可能是真信、可能是 notify_all 误叫、
    ### 也可能只是超时。notify_all 而非 notify 是必须的——一把 Condition 上睡的是
    ### 不同信箱的人，单播可能叫错。副作用红利=至多一次投递：同信箱两个等待者
    ### 被锁串行化，第二个醒来抓到空。deadline 用 monotonic 每轮重算剩余，
    ### 不受系统时钟回拨影响；对比消息 ts 用墙钟——计时器和时间戳各司其职
    def wait_for_messages(self, name: str, timeout: float | None = None) -> list[dict]:
        """阻塞直到该信箱有信或超时；超时返回 []。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(name):
                remaining = (None if deadline is None
                             else deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(name)

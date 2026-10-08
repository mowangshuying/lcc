import threading
from pathlib import Path
from env import Env
import subprocess
import signal
import os
import time
import atexit
from tool_names import BASH
from log import log_info

### 后台 bash 任务引擎（对照 s15 Background Tasks 一节）。
### hooks_trigger 注入约定（仿 AgentTeamsManager）：签名 hooks_trigger(event, block, output)，
### 由组装根（tools_manager）绑定到 Hooks.trigger_hooks。
### ⚠ 任务在后台线程执行，因此注册进 PostToolUse 的钩子须线程安全。
class BackgroundTasksManager:
    def __init__(self, hooks_trigger=None):
        self.env = Env()
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self.ready:list[str] = []
        self.counter = 0
        self.lock = threading.Lock()
        self.shell_processes: set[subprocess.Popen] = set()
        self.shell_processes_lock = threading.RLock()
        ### None 时跳过钩子层（向后兼容，现状行为不变）
        self.hooksTrigger = hooks_trigger
        
        self.register_exit_handlers()
    def start(self, block, cwd: str | None = None) -> str:
        if block.name != BASH:
            raise Exception("Only bash blocks can be started in background")
        
        command = block.input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")
        
        with self.lock:
            self.counter += 1
            task_id = f"bg_{self.counter:04d}"
            self.tasks[task_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",
                ### 派发时刻解析好的工作目录（Lead 租约 cwd），对照 s15 L2299
                "cwd": str(cwd) if cwd else None,
            }
            
        thread = threading.Thread(target=self.run,
                                  args=(task_id, command, cwd, block), daemon=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.tasks.pop(task_id, None)
            raise
        log_info("bg", f"started {task_id} {command[:60]}")
        return task_id
        
            
    def run(self, task_id: str, command: str,
            cwd: str | None = None, block=None):
        try:
            output, exit_code = self.run_bash_process(command, cwd)
            result = self.format_bash_result(output, exit_code)
            
            status = "failed"
            if exit_code == 0:
                status = "completed"
                            
        except Exception as error:
            result = f"Error: {type(error).__name__}: {error}"
            status = "failed"
        
        result = str(result)
        
        ### 后台任务的 PostToolUse（对照 s15 L2280-2284）：
        ### 拿到 result 后、置 status 前触发；钩子返回非 None 视为拦截文案，前缀进 result；
        ### 钩子自身抛异常则打 [hook error] 前缀并把 status 降级为 failed。
        if self.hooksTrigger is not None:
            try:
                intercepted = self.hooksTrigger("PostToolUse", block, result)
            except Exception as error:
                result = f"[hook error] {type(error).__name__}: {error}\n{result}"
                status = "failed"
            else:
                if intercepted is not None:
                    result = f"{intercepted}\n{result}"
            
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            
            task["status"] = status
            self.results[task_id] = result
            self.ready.append(task_id)
    
    def collect(self) -> list[str]:
        ready = []
        with self.lock:
            task_ids = list(self.ready)
            self.ready.clear()
            for task_id in task_ids:
                task = self.tasks.pop(task_id, None)
                result = self.results.pop(task_id, "")
                if task is not None:
                    ready.append((task_id, task, result))
        
        notifications = []
        for task_id, task, result in ready:
            log_info("bg", f"collected {task_id}: {task['status']}")
            ### 摘要截断 200 字符（对照 s15 L2324-2331）；failed 与 completed 共用本模板，
            ### 由 <status> 标签区分语义。
            summary = result[:200] if len(result) > 200 else result
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{summary}</summary>\n"
                f"</task_notification>"
            )
        
        return notifications
    
    def should_run_background(self, tool_name: str, tool_input: dict) -> bool:
        return (tool_name == BASH) and (tool_input.get("run_in_background") is True)
    
    ### 是否已有终态（completed/failed）后台任务待收割（对照 s15 has_pending_background）：
    ### 只查不清零，收割仍由 collect 负责。
    def has_pending(self) -> bool:
        with self.lock:
            return any(task["status"] in {"completed", "failed"}
                       for task in self.tasks.values())
    
    def collect_background_results(self) -> list[str]:
        return self.collect()
    
    def start_background_task(self, block, cwd: str | None = None):
        return self.start(block, cwd=cwd)
    
    ### cwd=None 保持现行为（锁定 env.workDirPath）；后台链路的 cwd 由 start(block, cwd=...)
    ### 在派发时刻解析并记录进 task，再经 run 透传给本函数——本参数是本模块唯一允许的透传点。
    def run_bash_process(self, command: str,
                         cwd: str | Path | None = None) -> tuple[str, int | None]:
        process = None
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                cwd=cwd or self.env.workDirPath,
                start_new_session=True,
            )
            
            with self.shell_processes_lock:
                self.shell_processes.add(process)
            
            stdout, stderr = process.communicate(timeout=120)
            output = (stdout + stderr).strip()
            
            if output:
                return output[:50000], process.returncode
            
            return "(no output)", process.returncode
        except subprocess.TimeoutExpired:
            return "Error: Timeout(120s)", None
        except Exception as error:
            return f"Error: {type(error).__name__}: {error}", None
        finally:
            if process is not None:
                self.stop_process_group(process)
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
                with self.shell_processes_lock:
                    self.shell_processes.discard(process)
            
    def stop_process_group(self, process: subprocess.Popen):
        if hasattr(os, "killpg"):
            sigs = [signal.SIGTERM]
            if hasattr(signal, "SIGKILL"):
                sigs.append(signal.SIGKILL)
            for sig in sigs:
                try:
                    os.killpg(process.pid, sig)
                except (ProcessLookupError, OSError):
                    return
                time.sleep(0.05)
        else:
            try:
                process.terminate()
                time.sleep(0.05)
                process.kill()
            except OSError:
                return
            
    def stop_all_shell_processes(self):
        with self.shell_processes_lock:
            processes = list(self.shell_processes)
        for process in processes:
            self.stop_process_group(process)
    
    def handle_termination_signal(self, signum, _frame):
        self.stop_all_shell_processes()
        raise SystemExit(128 + signum)
    
    def register_exit_handlers(self):
        signal.signal(signal.SIGTERM, self.handle_termination_signal)
        atexit.register(self.stop_all_shell_processes)
        
    def format_bash_result(self, output: str, exit_code: int | None) -> str:
        if exit_code in (0, None):
            return output
        return f"Error: command exited with status {exit_code}:\n{output}"
        
    
            
                
            
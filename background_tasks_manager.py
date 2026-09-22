import threading
from env import Env
import subprocess
import signal
import os
import time
import atexit


class BackgroundTasksManager:
    def __init__(self):
        self.env = Env()
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self.ready:list[str] = []
        self.counter = 0
        self.lock = threading.Lock()
        self.shell_processes: set[subprocess.Popen] = set()
        self.shell_processes_lock = threading.RLock()
        
        self.register_exit_handlers()
    def start(self, block) -> str:
        if block.name != "bash":
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
            }
            
        thread = threading.Thread(target=self.run, args=(task_id, command), daemon=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.tasks.pop(task_id, None)
            raise
        print(f"[background] started {task_id} {command[:60]}")
        return task_id
        
            
    def run(self, task_id: str, command: str):
        try:
            output, exit_code = self.run_bash_process(command)
            result = self.format_bash_result(output, exit_code)
            
            status = "failed"
            if exit_code == 0:
                status = "completed"
                            
        except Exception as error:
            result = f"Error: {type(error).__name__}: {error}"
            status = "failed"
            
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
            print(f"[background] collected {task_id}: {task['status']}")
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <result>{result[:500]}</result>\n"
                f"</task_notification>"
            )
        
        return notifications
    
    def should_run_background(self, tool_name: str, tool_input: dict) -> bool:
        return (tool_name == "bash") and (tool_input.get("run_in_background") is True)
    
    def collect_background_results(self) -> list[str]:
        return self.collect()
    
    def start_background_task(self, block):
        return self.start(block)
    
    def run_bash_process(self, command: str) -> tuple[str, int | None]:
        process = None
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                cwd=self.env.workDirPath,
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
        
    
            
                
            
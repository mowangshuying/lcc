from dataclasses import asdict, dataclass
from datetime import datetime
import threading
from env import Env
import os
import json
import secrets

@dataclass
class CronJob:
    id: str                             # 任务ID 
    cron: str                           # 定时任务  
    prompt: str                         # 任务内容
    recurring: bool                     # 是否循环
    durable: bool                       # 是否持久
    pending_delivery: bool = False      # 是否待发送
    last_fired: str | None = None       # 最后一次执行时间
    
    
class CronScheduler:
    def __init__(self):
        self.env = Env()
        self.scheduled_jobs: dict[str, CronJob] = {}
        self.cron_queue: list[CronJob] = []
        # RLock：save_durable_jobs 会在持有 cron_lock 时被调用，普通 Lock 会自死锁
        self.cron_lock = threading.RLock()
        
        self.runtime_stop = threading.Event()
        self.runtime_started = False
        
        self.scheduler_loop_thread: threading.Thread | None = None
    
    # * 任意值
    # */N 取模为0  */15
    # a, b, c 列表任意命中  7,14,30
    # a-b 闭区间            1-10
    # n 精确等于 30         30
    #### field:匹配的表达式 value：待匹配的值
    def _cron_field_matches(self, field: str, value: int) -> bool:
        if field == "*":
            return True
        
        if field.startswith("*/"):
            return value % int(field[2:]) == 0
        
        if "," in field:
            for part in field.split(","):
                if self._cron_field_matches(part, value):
                    return True
            return False
        
        if "-" in field:
            start, end = field.split("-", 1)
            return int(start) <= value <= int(end)
        
        return int(field) == value
    
    ### 分 时 日 月 星期
    def _cron_matches(self, cron_expr: str, moment: datetime):
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return False
        
        ### python的weekday: 0-6, 0:星期一 6:星期天
        ### cron惯例：星期天=0, 星期一=1
        cron_weekday = (moment.weekday() + 1) % 7
        
        minute, hour, day, month, weekday = fields
        if not self._cron_field_matches(minute, moment.minute):
            return False
        
        if not self._cron_field_matches(hour, moment.hour):
            return False
        
        if not self._cron_field_matches(month, moment.month):
            return False
     
        if day == "*" and weekday == "*":
            return True
        
        if day == "*":
            return self._cron_field_matches(weekday, cron_weekday)
        
        if weekday == "*":
            return self._cron_field_matches(day, moment.day)
        
        return self._cron_field_matches(day, moment.day) or self._cron_field_matches(weekday, cron_weekday)
    
    def _validate_cron_field(self, field: str, minimum: int, maximum: int) -> str | None:
        if field == "*":
            return None
        
        if field.startswith("*/"):
            step = field[2:]
            if not step.isdigit() or int(step) <= 0:
                return f"Invalid step: {field}"
            return None
        
        if "," in field:
            for part in field.split(","):
                error = self._validate_cron_field(part.strip(), minimum, maximum)
                if error:
                    return error
            return None
        
        if "-" in field:
            start, end = field.split("-", 1)
            if not start.isdigit() or not end.isdigit():
                return f"Invalid range: {field}"
            start_value, end_value = int(start), int(end)
            if start_value > end_value:
                return f"Range start is greater than end: {field}"
            if start_value < minimum or end_value > maximum:
                return f"Range {field} is outside [{minimum}-{maximum}]"
            return None
        
        if not field.isdigit():
            return f"Invalid field: {field}"
        
        value = int(field)
        if value < minimum or value > maximum:
            return f"Value {value} is outside [{minimum}-{maximum}]"
        return None
    
    def validate_cron(self, cron_expr: str) -> str | None:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return f"Expected 5 fields, got {len(fields)}"

        field_rules = [
            ("minute", 0, 59),
            ("hour", 0, 23),
            ("day-of-month", 1, 31),
            ("month", 1, 12),
            ("day-of-week", 0, 6),
        ]
        for field, (name, minimum, maximum) in zip(fields, field_rules):
            error = self._validate_cron_field(field, minimum, maximum)
            if error:
                return f"{name}: {error}"
        return None
    
    def save_durable_jobs(self):
        with self.cron_lock:
            payload =  []
            for job in self.scheduled_jobs.values():
                if job.durable:
                    payload.append(asdict(job))
                    
            temporary = self.env.durablePath.with_name(f"{self.env.durablePath.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary.write_text(json.dumps(payload, indent=4), encoding="utf-8")
                os.replace(temporary, self.env.durablePath)
            finally:
                temporary.unlink(missing_ok=True)
                
                
    def load_durable_jobs(self):
        if not self.env.durablePath.exists():
            return
        
        try:
            payload = json.loads(self.env.durablePath.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("expected a JSON list")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(f"[cron] could not load {self.env.durablePath.name}: {error}")
            return
        
        loaded = 0
        with self.cron_lock:
            for item in payload:
                try:
                    job = CronJob(**item)
                    error = self.validate_cron(job.cron)
                    if error:
                        raise ValueError(error)
                    
                    if not job.id.startswith("cron_"):
                        raise ValueError("invalid job id")
                    
                    if not job.prompt.strip():
                        raise ValueError("prompt cannot be empty")
                    
                except (TypeError, ValueError) as error:
                    print(f"[cron] skipped invalid saved job: {error}")
                    continue
                
                self.scheduled_jobs[job.id] = job
                if job.pending_delivery:
                    self.cron_queue.append(job)
                loaded += 1
            if loaded:
                print(f"[cron] loaded {loaded} durable job(s)")
    
    
    def new_cron_id(self) -> str:
        for _ in range(100):
            job_id = f"cron_{secrets.token_hex(4)}"
            if job_id not in self.scheduled_jobs:
                return job_id
        raise ValueError("could not allocate a cron job id")
    
    def schedule_job(self, cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> CronJob | str:
        error = self.validate_cron(cron)                 
        if error:
            return error
        
        if not prompt.strip():
            return "prompt cannot be empty"
        
        with self.cron_lock:
            job = CronJob(
                id = self.new_cron_id(),
                cron=cron,
                prompt=prompt,
                recurring=recurring,
                durable=durable,
            )
            
            self.scheduled_jobs[job.id] = job
            try:
                if durable:
                    self.save_durable_jobs()
            except Exception:
                self.scheduled_jobs.pop(job.id, None)
                raise
        print(f"[cron] scheduled {job.id}: {cron} -> {job.prompt[:60]}")
        return job
    
    def cancel_job(self, job_id: str) -> str:
        with self.cron_lock:
            job = self.scheduled_jobs.get(job_id)
            if job is None:
                return f"Job {job_id} not found"
            
            previous_queue = list(self.cron_queue)
            
            self.scheduled_jobs.pop(job_id)
            for queued in self.cron_queue:
                if queued.id == job_id:
                    self.cron_queue.remove(queued)
                    
            try:
                if job.durable:
                    self.save_durable_jobs()
            except Exception:
                self.scheduled_jobs[job_id] = job
                self.cron_queue.extend(previous_queue)
                raise
        print(f"[cron] cancelled {job_id}")
        return f"Cancelled {job_id}"
    
    def _enqueue_due_job(self, job: CronJob, minute_marker: str | None = None):
        old_pending = job.pending_delivery
        old_last_fired = job.last_fired
        job.pending_delivery = True
        
        if minute_marker is not None:
            job.last_fired = minute_marker
            
        try:
            if job.durable:
                self.save_durable_jobs()
        except Exception:
            job.pending_delivery = old_pending
            job.last_fired = old_last_fired
            raise
        
        self.cron_queue.append(job)
        
    def poll_due_jobs(self, moment: datetime):
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        with self.cron_lock:
            for job in self.scheduled_jobs.values():
                try:
                    if job.pending_delivery or job.last_fired == minute_marker:
                        continue
                    
                    if self._cron_matches(job.cron, moment):
                        self._enqueue_due_job(job, minute_marker)
                        print(f"[cron] due {job.id}: {job.prompt[:60]}")
                except Exception as error:
                    print(f"[cron] could not enqueue {job.id}: {error}")
                    
    def consume_cron_queue(self)-> list[CronJob]:
        with self.cron_lock:
            jobs = list(self.cron_queue)
            self.cron_queue.clear()
        return jobs
    
    def acknowledge_cron_jobs(self, jobs: list[CronJob]):
        changed: list[tuple[CronJob, bool]] = []
        removed = []
        
        with self.cron_lock:
            for delivered in jobs:
                current = self.scheduled_jobs.get(delivered.id)
                if current is None:
                    continue
                
                changed.append((current, current.pending_delivery))
                if current.recurring:
                    current.pending_delivery = False
                else:
                    removed.append(current)
                    self.scheduled_jobs.pop(current.id)
                    

            # try:
            is_changed = False
            for job, pending in changed:
                if job.durable:
                    is_changed = True
            
            try:
                if is_changed:
                    self.save_durable_jobs()
            except Exception:
                for job in removed:
                    self.scheduled_jobs[job.id] = job
                    
                for job, pending in changed:
                    job.pending_delivery = pending
                    
                queued_ids = []
                for job in self.cron_queue:
                    queued_ids.append(job.id)
                
                for job, _ in changed:
                    if job.id not in queued_ids:
                        self.cron_queue.append(job)
                raise
            
    def restore_cron_jobs(self, jobs: list[CronJob]):
        with self.cron_lock:
            queued_ids = []
            for job in self.cron_queue:
                queued_ids.append(job.id)
            
            for delivered in jobs:
                current = self.scheduled_jobs.get(delivered.id)
                if current is None:
                    continue
                
                current.pending_delivery = True
                if current.id not in queued_ids:
                    self.cron_queue.append(current)
                    queued_ids.append(current.id)
                    
    def has_cron_queue(self) -> bool:
        with self.cron_lock:    
            return bool(self.cron_queue)
        
    def cron_scheduler_loop(self):
        while not self.runtime_stop.wait(1.0):
            self.poll_due_jobs(datetime.now())
            
    def start_runtime_threads(self):
        if self.runtime_started:
            return
        
        self.load_durable_jobs()
        self.runtime_stop.clear()
        self.scheduler_loop_thread = threading.Thread(target=self.cron_scheduler_loop, daemon=True)
        self.scheduler_loop_thread.start()
        self.runtime_started = True
        
    def stop_runtime_threads(self):
        if not self.runtime_started: 
            return
        
        self.runtime_stop.set()
        self.scheduler_loop_thread.join(timeout=1)
        self.runtime_started = False
        
    def list_cron_jobs(self) -> list[CronJob]:
        # 供外部只读展示的快照：内部持锁复制注册表，调用方不再接触 cron_lock / scheduled_jobs
        with self.cron_lock:
            return list(self.scheduled_jobs.values())

    def run_delivery(self, deliver) -> int:
        # at-least-once 投递协议的唯一执行者：整批取出 -> 交付回调成功才 ack，
        # 回调抛出任何异常则 restore 回队列并原样 re-raise，绝不静默丢批
        fired = self.consume_cron_queue()
        if not fired:
            return 0
        try:
            deliver(fired)
        except BaseException:
            self.restore_cron_jobs(fired)
            raise
        self.acknowledge_cron_jobs(fired)
        return len(fired)
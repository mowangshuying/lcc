from dotenv import load_dotenv
import os
from pathlib import Path

class Env:
    def __init__(self):
        load_dotenv(override=True)
        if os.getenv("ANTHROPIC_BASE_URL"):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        self.httpUrl = os.getenv("ANTHROPIC_BASE_URL")
        self.modelId = os.getenv("MODEL_ID")
        self.workDir = os.getcwd() 
        self.workDirPath = Path.cwd()
        self.lccDirPath = self.workDirPath / ".lcc"
        self.lccDirPath.mkdir(parents=True, exist_ok=True)
        self.skillsDirPath = self.workDirPath / "skills"
        self.transcriptDirPath = self.lccDirPath / "transcripts"
        self.toolResultsDirPath = self.lccDirPath / "task_outputs" / "tool-results"
        self.memoryDirPath = self.lccDirPath / "memory"
        self.memoryIndexPath = self.memoryDirPath / "MEMORY.md"
        self.taskDirPath = self.lccDirPath / "task"
        self.tempDirPath = self.lccDirPath / "temp"
        self.tempDirPath.mkdir(parents=True, exist_ok=True)
        self.durablePath = self.lccDirPath / "scheduled_tasks.json"

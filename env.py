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
        self.skillsDirPath = self.workDirPath / "skills"
        self.transcriptDirPath = self.workDirPath / ".transcripts"
        self.toolResultsDirPath = self.workDirPath / ".task_outputs" / "tool-results"
        self.memoryDirPath = self.workDirPath / ".memory"
        self.memoryIndexPath = self.memoryDirPath / "MEMORY.md"

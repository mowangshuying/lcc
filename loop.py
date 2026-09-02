from anthropic import Anthropic
from dotenv import load_dotenv
from pathlib import Path
from env import Env
from color import *
from hooks import *
from tools_manager import ToolsManager
from compact_manager import CompactManager


class Loop:
    MAX_REACTIVE_RETRIES = 1
    def __init__(self):
        self.env = Env()
        self.hooks = Hooks()
        self.client = Anthropic(base_url=self.env.httpUrl)
        self.toolsManager = ToolsManager()
        self.system_prompt = self.build_system_prompt()
        self.compactManager = CompactManager(
            self.client,
            self.env.modelId,
            self.env.transcriptDirPath,
            self.env.toolResultsDirPath,
        )

    def build_system_prompt(self):
        return (
            f"You are a coding agent at {self.env.workDir}. Use tools to solve tasks. "
            "Act, don't explain.\n\n"
            f"Skills available:\n{self.toolsManager.skills_catalog()}\n\n"
            "Use load_skill to read the full instructions when a skill applies."
        )

    ### loop;
    def agent_loop(self, messages: list, active_request: str):
        rounds_since_todo = 0
        reactive_retries = 0
        while True:
            messages[:] = self.compactManager.prepare(messages, active_request)
            
            try:
                response = self.client.messages.create(
                    model=self.env.modelId,
                    system=self.system_prompt,
                    messages=messages,
                    tools=self.toolsManager.tools,
                    max_tokens=8000,
                )
                
                reactive_retries = 0
            except Exception as error:
                too_long = False
                for text in ("prompt_too_long", "too many tokens"):
                    if text in str(error).lower():
                        too_long = True
                        break
                    
                if too_long and reactive_retries < self.MAX_REACTIVE_RETRIES:
                    messages[:] = self.compactManager.reactive_compact(messages, active_request)
                    reactive_retries += 1
                    continue
                raise

            ### 将返回内容重新添加至message列表中
            messages.append({"role": "assistant", "content": response.content})

            tool_calls = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_calls.append(block)

            if len(tool_calls) == 0:
                force = self.hooks.trigger_hooks("Stop", messages)
                if force:
                    messages.append({"role": "user", "content": force})
                    continue
                return

            results = []
            used_todo = False
            compact_requested = False
            for block in tool_calls:
                if block.name == "compact":
                    compact_requested = True
                else:
                    output = self.toolsManager.execute_tool(block, self.toolsManager.toolsHandlers)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output,
                        }
                    )

                if block.name == "todo_write":
                    used_todo = True

            if not used_todo:
                rounds_since_todo += 1
            else:
                rounds_since_todo = 0

            if rounds_since_todo >= 3:
                results.append(
                    {"type": "text", "text": "<reminder>Update your todos.</reminder>"}
                )
                rounds_since_todo = 0

            messages.append({"role": "user", "content": results})
            if compact_requested:
                messages[:] = self.compactManager.compact_history(messages, active_request)

    def run(self):
        history = []
        while True:
            try:
                query = input(f"\n{COLOR_CYAN}s07>>")
            except (EOFError, KeyboardInterrupt):
                break

            if query.strip().lower() in ("q", "exit", ""):
                break

            self.hooks.trigger_hooks("UserPromptSubmit", query)
            history.append({"role": "user", "content": query})

            self.agent_loop(history, query)

            lst_content = history[-1]["content"]
            if isinstance(lst_content, list):
                for block in lst_content:
                    if getattr(block, "type", None) == "text":
                        print(f"{COLOR_DEFAULT}text:{block.text}{COLOR_DEFAULT}")


### 主函数
if __name__ == "__main__":
    loop = Loop()
    loop.run()

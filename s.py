import os
import subprocess

from anthropic import Anthropic
from dotenv import load_dotenv


### 常用的文字颜色
# 30: 黑 
# 31：红
# 32：绿
# 33：黄
# 34：蓝色
# 35: 洋红
# 36：青
# 37：白 

### 案例
### \001 \033[0m \002
color_default   = "\001\033[0m\002"
color_black   = "\001\033[30m\002"
color_red     = "\001\033[31m\002"
color_green   = "\001\033[32m\002"
color_yellow  = "\001\033[33m\002"
color_blue    = "\001\033[34m\002"
color_magenta = "\001\033[35m\002"
color_cyan    = "\001\033[36m\002"
color_white   = "\001\033[37m\002"



### 加载一些全局变量
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

g_httpUrl = os.getenv("ANTHROPIC_BASE_URL")
# g_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
g_modelId = os.getenv("MODEL_ID")
g_client = Anthropic(base_url=g_httpUrl)

### dir
g_workDir = os.getcwd()

### 提示词
g_systemPrompt = f"You are a coding agent at {g_workDir}. Use bash to solve tasks. Act, don't explain."

### 工具定义
g_tools = [
    ### bash
    {
        "name":"bash",
        "description":"Run a shell command.",
        "input_schema": {
            "type":"object",
             "properties": {
                 "command": {
                     "type": "string"
                    }
                },
             "required": ["command"],
        }
    }
]

### 工具函数
def run_bash(command:str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

    found = False
    for d in dangerous:
        if d in command:
            found=True
            break
    if found:
        return "Error: Dangerous command blocked"

    try:
        r = subprocess.run(command, shell=True, cwd=g_workDir,
                           capture_output=True, text=True, errors="replace", timeout=120)
        out = (r.stdout + r.stderr).strip()
        if out:
            return out[:50000]
        else:
            return "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout(120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error:{e}"

# ### 处理httpurl及token
# def __init():
#     load_dotenv(override=True)
#     if os.getenv("ANTHROPIC_BASE_URL"):
#         os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

#     g_httpUrl = os.getenv("ANTHROPIC_AUTH_TOKEN")
#     g_modelId = os.getenv("MODEL_ID")

### loop;
def loop(messages:list):
    while True:
        response = g_client.messages.create(model=g_modelId, 
                                            system=g_systemPrompt, 
                                            messages=messages, 
                                            tools=g_tools, 
                                            max_tokens=8000)

        ### 将返回内容重新添加至message列表中
        messages.append({"role":"assistant", "content": response.content})

        tool_calls = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if len(tool_calls) == 0:
            return

        results = []
        for block in tool_calls:
            print(f"{color_green}tool_use: shell execute {block.input['command']}{color_default}")
            output = run_bash(block.input['command'])
            print(f"{color_magenta}tool_result:{output}{color_default}")
            results.append({
                "type":"tool_result",
                "tool_use_id":block.id,
                "content":output,
            })
        messages.append({"role":"user", "content": results})


### 主函数
if __name__ == "__main__":

    history = []
    while True:
        try:
            query = input(f"{color_red}s>>{color_default}")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in("q", "exit", ""):
            break

        history.append({"role":"user", "content":query})

        loop(history)

        lst_content = history[-1]["content"]
        if isinstance(lst_content, list):
            for block in lst_content:
                if getattr(block, "type", None) == "text":
                    print(f"{color_cyan}text:{block.text}{color_default}")

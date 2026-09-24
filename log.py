### 全仓日志输出唯一事实源：print 只允许出现在本模块（及 loop.py 的三处非日志例外）
### 零依赖叶子模块（只 import color），仿 tool_names.py 先例；业务代码一律经 log_* 输出，
### 禁止在调用方手拼颜色前缀或复位后缀。

### 约定：
###   行格式    [tag] message（tag 后有且仅有一个空格）
###   tag       按域命名、全小写；同域可跨模块共用（这是规范不是病灶）：
###             hook / cron / memory / task / bg / compact / permission
###   通道      log_info、log_warn -> stdout；log_error -> stderr
###   颜色      info 无色、warn 黄、error 红；每条输出行以 COLOR_DEFAULT 复位收尾。
###             颜色常量取自 color.py（readline 安全的 \001..\002 包裹格式）
###   blank_before  行前补一个空行，仅用于需要与先前输出做视觉分隔的场景
###             （memory 提取/合并结果、permission ask_user 提示块）
###   例外（非日志，不走本模块）：loop.py 的 s12>> 输入提示符、loop.py 的
###             模型文本转印、permission.py 的 input() 交互提示

import sys
from color import COLOR_DEFAULT, COLOR_RED, COLOR_YELLOW


def _emit(stream, color, tag, message, blank_before):
    if blank_before:
        print("", file=stream)
    print(f"{color}[{tag}] {message}{COLOR_DEFAULT}", file=stream)


def log_info(tag, message, blank_before=False):
    _emit(sys.stdout, "", tag, message, blank_before)


def log_warn(tag, message, blank_before=False):
    _emit(sys.stdout, COLOR_YELLOW, tag, message, blank_before)


def log_error(tag, message, blank_before=False):
    _emit(sys.stderr, COLOR_RED, tag, message, blank_before)

### s15 错误恢复与退避语义（lcc 移植版）：API 调用的重试/换模型/超限判定。
### 风格仿 mcp_manager.py：中文注释，控制台输出统一走 log_*（本模块只用 log_warn）。
###
### 语义要点（照抄 s15，勿自创）：
###   1. with_retry 用 duck 判定异常类别——不 import anthropic SDK 异常类，
###      仅按 type(exc).__name__ 与 str(exc) 的小写包含关系分流，方便冒烟用假异常驱动。
###   2. 529/overloaded 连续达 MAX_CONSECUTIVE_529 且配了 fallback 才换模型，换后计数归零。
###   3. 退避 time.sleep 阻塞当前线程：与 s15 双轨下 agent_lock 同样阻塞可接受
###      （最长 ~32s，调用方 loop.py 在其处已注释声明该权衡）。

import random
import time

from log import log_warn

### 常量对齐 s15 L64-69 / L73
DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
### 续打提示词（逐字 s15 L73）
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."


class RecoveryState:
    """单 turn 的错误恢复状态：模型切换 / 529 计数 / max_tokens 升档 / 续打次数 / 被动压缩旗标。
    对齐 s15 L2193-2199；命名随 lcc 类属性 camelCase 习惯。"""

    def __init__(self, initialModel: str, fallbackModel: str | None = None):
        self.initialModel = initialModel
        self.currentModel = initialModel
        self.fallbackModel = fallbackModel
        self.consecutive529 = 0
        self.maxTokensEscalated = False
        self.recoveryCount = 0
        self.hasAttemptedReactiveCompact = False


def retryDelay(attempt: int) -> float:
    ### 逐式 s15 L2202-2204：指数退避封顶 32s，叠加 0~25% 抖动避免同刻重打
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def withRetry(operation, state: RecoveryState, maxRetries: int = MAX_RETRIES):
    ### 对齐 s15 L2207-2234：RateLimit/429、timeout、529/overloaded 三类退避重试，
    ### 其他异常直接 raise；重试耗尽 raise 最后异常；成功归零 consecutive529。
    lastExc = None
    for attempt in range(maxRetries):
        try:
            result = operation()
            state.consecutive529 = 0
            return result
        except Exception as exc:
            lastExc = exc
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            if "ratelimit" in name or "429" in msg or "rate limit" in msg or "rate_limit" in msg:
                delay = retryDelay(attempt)
                log_warn("recovery", f"[429] retry {attempt + 1}/{maxRetries} after {delay:.1f}s")
                time.sleep(delay)
                continue
            if "timeout" in name or "timeout" in msg:
                delay = retryDelay(attempt)
                log_warn("recovery", f"[timeout] retry {attempt + 1}/{maxRetries} after {delay:.1f}s")
                time.sleep(delay)
                continue
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive529 += 1
                if state.consecutive529 >= MAX_CONSECUTIVE_529 and state.fallbackModel:
                    log_warn("recovery", f"[529] 切换 fallback 模型: {state.fallbackModel}")
                    state.currentModel = state.fallbackModel
                    state.consecutive529 = 0
                delay = retryDelay(attempt)
                log_warn("recovery", f"[529] retry {attempt + 1}/{maxRetries} after {delay:.1f}s")
                time.sleep(delay)
                continue
            raise
    raise lastExc


def isPromptTooLong(exc: Exception) -> bool:
    ### 逐字 s15 L2237-2241：Anthropic 真文常为 "prompt ... too long"，旧字面串命中不到
    m = str(exc).lower()
    return (("prompt" in m and "long" in m)
            or "context_length_exceeded" in m
            or "max_context_window" in m)

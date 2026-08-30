# lcc

用 `anthropic` SDK 直连模型，跑通「工具调用循环 → 文件沙箱 → 分级权限审批」完整链路，全部逻辑可读、可断点、可逐 commit 追溯。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`.env`（已被 git 忽略）：

```ini
ANTHROPIC_API_KEY=sk-ant-...    # 必填
MODEL_ID=qwen3.8-flash          # 任意 Anthropic 协议端点
ANTHROPIC_BASE_URL=             # 可选，走网关时设置
```

```powershell
python s.py    # 启动目录即沙箱根；q / exit 退出
```

## 参考

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) — 本仓库为其学习记录，实现思路可对照其源码与提交历史。

"""LLM 配置 —— 统一走 MiMo（OpenAI 兼容，订阅零 key，定位『量活』）。

- quick 档 = mimo-v2.5（快，跑分析师这种高频节点）
- deep  档 = mimo-v2.5-pro（推理模型，准，跑综合裁判这种收敛节点）

凭据从 ~/.config/mimo/mimo.env 读取（MIMO_API_KEY / MIMO_BASE_URL / MIMO_MODEL），
绝不硬编码 key。

接 DeepSeek 等其它 OpenAI 兼容端点：MIMO_BASE_URL 指过去，MIMO_MODEL（deep 档）与
MIMO_QUICK_MODEL（quick 档）填那家的模型名 —— quick 档以前写死 mimo-v2.5，换端点后
模型名对不上会直接 404（#5）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from langchain_openai import ChatOpenAI

from . import cli_llm

_MIMO_ENV = Path.home() / ".config" / "mimo" / "mimo.env"

_CREDS: dict[str, str] | None = None

# quick / deep 两档模型名。deep 优先用环境里的 MIMO_MODEL（默认 pro），
# quick 优先用 MIMO_QUICK_MODEL（默认下面这个）。
QUICK_MODEL = "mimo-v2.5"


def _ensure_mimo_loaded() -> None:
    """把 MiMo 凭据读进**进程内的字典**（只读一次）"""
    global _CREDS
    if _CREDS is not None:
        return
    creds = {}
    # ① 环境里已有就用（用户主动设的，尊重）
    for k in ("MIMO_API_KEY", "MIMO_BASE_URL", "MIMO_MODEL", "MIMO_QUICK_MODEL"):
        v = os.environ.get(k)
        if v:
            creds[k] = v
    if not creds.get("MIMO_API_KEY"):
        if not _MIMO_ENV.exists():
            raise RuntimeError(
                f"找不到 MiMo 凭据文件 {_MIMO_ENV}；请确认 ~/.config/mimo/mimo.env 存在。"
            )
        creds.update({k: v for k, v in dotenv_values(_MIMO_ENV).items() if v})
    if not creds.get("MIMO_API_KEY"):
        raise RuntimeError("MIMO_API_KEY 未设置（mimo.env 里没有或为空）。")
    _CREDS = creds


def make_llm(deep: bool = False, temperature: float = 0.6):
    """构造复盘用的 LLM"""
    kind = cli_llm.wanted_kind()
    if kind:
        return cli_llm.make_cli_llm(deep=deep)

    _ensure_mimo_loaded()
    assert _CREDS is not None
    base_url = _CREDS.get("MIMO_BASE_URL") or "https://token-plan-cn.xiaomimimo.com/v1"
    api_key = _CREDS["MIMO_API_KEY"]
    model = ((_CREDS.get("MIMO_MODEL") or "mimo-v2.5-pro") if deep
             else (_CREDS.get("MIMO_QUICK_MODEL") or QUICK_MODEL))
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout=180,
        max_retries=2,
    )

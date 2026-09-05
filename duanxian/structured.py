"""结构化输出助手（JSON 模式，对 MiMo 比 with_structured_output 稳）"""

from __future__ import annotations

import json
import logging

from .llm_errors import raise_if_config_error
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def extract_first_json(text: str) -> Optional[dict]:
    """从模型回复里解析出第一个完整的 JSON 对象（容忍前后解释/围栏/追加内容）。"""
    if not text:
        return None
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return None
        try:
            obj, _ = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
            idx = start + 1
        except json.JSONDecodeError:
            idx = start + 1


def invoke_json_schema(
    llm: Any,
    base_prompt: str,
    schema: type,
    render: Callable[[Any], str],
    agent_name: str,
    skeleton: str,
    retries: int = 2,
) -> tuple[str, Optional[Any]]:
    """JSON 模式结构化输出：prompt 给英文键骨架 → 抠 JSON → pydantic 校验 → 失败重试 → 退回自由文本。

    返回 (markdown 文本, 校验通过的 pydantic 对象 or None)。
    LLM 调用本身也在 try 内，API 失败同样触发重试/降级。
    """
    instr = (
        "\n\n请严格只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。"
        "键名必须用下面骨架里的英文原样（值填中文内容）：\n" + skeleton
    )
    prompt = base_prompt + instr
    last_err: Optional[Exception] = None
    for _ in range(retries):
        try:
            resp = llm.invoke(prompt)
            raw = getattr(resp, "content", None) or str(resp)
            obj = extract_first_json(raw)
            if obj is None:
                raise ValueError("回复中未找到 JSON 对象")
            validated = schema.model_validate(obj)
            return render(validated), validated
        except Exception as exc:  # noqa: BLE001  含 LLM 调用失败 + 解析/校验失败
            # 配置错误重试没有意义，而且降级会让整份复盘"成功但全空"
            raise_if_config_error(exc, agent_name)
            last_err = exc
            prompt = base_prompt + instr + f"\n\n（上次输出无法解析/不合规：{exc}；请重新只输出合法 JSON。）"
    logger.warning("%s: JSON 结构化输出连续失败(%s)，退回自由文本", agent_name, last_err)
    try:
        return llm.invoke(base_prompt).content, None
    except Exception as exc:  # noqa: BLE001  连自由文本都失败：给安全占位，不炸流水线
        raise_if_config_error(exc, agent_name)
        logger.error("%s: 自由文本兜底也失败(%s)", agent_name, exc)
        # 占位里要带上**真实报错**（#9：用户只看到「AI 环节没跑通」、根因只在服务端日志里）。
        # 截到 120 字：占位必须短于 review_store 的可用阈值，否则失败产物会被当成可用复盘落盘。
        detail = f"{type(exc).__name__}: {str(exc).strip()}"[:120]
        return f"（{agent_name}生成失败：{detail}，请稍后重试）", None

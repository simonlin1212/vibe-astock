"""复盘产物的读写 —— server 和 `main.py` 共用这一份"""

from __future__ import annotations

import html
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, NamedTuple

from . import reflection
from .roles import ROLES
from .util import china_now, is_degraded_report, safe_join

DIR = os.path.expanduser("~/.duanxian-agents/reviews")

REJECT_DIR = os.path.join(DIR, "_rejected")

SCHEMA_VERSION = 1

_MIN_FOCUS_MD = 200


class SaveResult(NamedTuple):
    written: bool
    reason: str = ""
    rejected_path: str = ""   # 被拒产物的另存路径


def usable(payload: Any) -> bool:
    """这份复盘算「可用」吗 —— 用来判断能不能覆盖已有结果。

    判据只看**AI 收敛的那一段**（明天关注点）：硬指标是纯数据算的，
    LLM 挂了它照样在，所以拿它判会把空复盘也算成可用。
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("focus"):            # 结构化关注点出来了 = 裁判跑通了
        return True
    return len((payload.get("focus_md") or "").strip()) >= _MIN_FOCUS_MD


def _atomic_write(path: str, payload: dict) -> None:
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"   # 唯一临时名，并发写不互踩
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)                    # 原子替换，读方永远看到完整文件


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def save(payload: dict, date: str) -> SaveResult:
    """写 `<date>.json` + `latest.json`。不可用产物**不覆盖**已有的可用产物。"""
    os.makedirs(DIR, exist_ok=True)
    dated = safe_join(DIR, f"{date}.json")
    latest = safe_join(DIR, "latest.json")

    if usable(payload):
        _atomic_write(dated, payload)
        _atomic_write(latest, payload)
        return SaveResult(True)

    kept: list[str] = []
    for path in (dated, latest):
        old = _read(path)
        if old is not None and usable(old):
            kept.append(os.path.basename(path))
        else:
            _atomic_write(path, payload)

    stamp = china_now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(REJECT_DIR, exist_ok=True)
    rejected = safe_join(REJECT_DIR, f"{date}.rejected-{stamp}.json")
    _atomic_write(rejected, payload)

    # 把 AI 段留下的占位（含真实报错）带进 reason —— 只报「没跑通」用户无从下手（#9）
    left = " ".join((payload.get("focus_md") or "").split())[:160]
    reason = (
        "本次复盘的「明天关注点」是空的（AI 环节没跑通）"
        + (f"：{left}" if left else "")
        + (f"，已保留原有的 {'、'.join(kept)} 不被覆盖" if kept else "")
        + f"。产物另存 _rejected/{os.path.basename(rejected)} 可查"
    )
    return SaveResult(False, reason, rejected)


def _with_baselines(env: dict | None) -> dict | None:
    """给验证条件补上「今日基准值 + 阈值」。

    放在**读取**这一步而不是落盘那一步：算它要用的 metrics / facts 就存在同一个
    envelope 里，所以早先落盘的那些复盘也能一并补上 —— 落盘时补的话，
    翻回上周三那份永远只有一句"预期下降"，明天拿什么对账。
    重复算是幂等的（同一份数据算出同一个结果）。
    """
    if not isinstance(env, dict):
        return env
    focus = env.get("focus")
    if not isinstance(focus, dict) or not focus.get("verification_items"):
        return env
    from . import verification

    return {**env, "focus": {**focus, "verification_items": verification.describe_items(
        focus["verification_items"],
        env.get("emotion_metrics") or {}, env.get("market_facts") or {})}}


def load(date: str | None = None) -> dict | None:
    """读某天的复盘；`date=None` 读 latest。文件损坏当不存在处理（不抛）。"""
    name = "latest.json" if date is None else f"{date}.json"
    path = safe_join(DIR, name)
    return _with_baselines(_read(path)) if os.path.exists(path) else None


def dates() -> list[str]:
    """有哪些历史复盘，新→旧。给看板的历史入口用"""
    out = []
    for p in Path(DIR).glob("*.json"):
        stem = p.stem
        if not (len(stem) == 10 and stem[4] == "-" and stem[7] == "-" and stem.replace("-", "").isdigit()):
            continue
        if usable(_read(str(p))):
            out.append(stem)
    return sorted(out, reverse=True)


# ---------------- markdown 极简渲染（深挖那条流水线也用，故为公开名）----------------
def md_to_html(text: str) -> str:
    if not text:
        return ""
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            continue
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        m = re.match(r"^(#+)\s*(.*)", s)
        if m:
            out.append(f"<h4>{m.group(2)}</h4>")
        else:
            out.append(f"<p>{s}</p>")
    return "\n".join(out)


def strip_prefix(text: str, prefix: str) -> str:
    return "\n".join(
        ln.strip()[len(prefix):].strip() if ln.strip().startswith(prefix) else ln
        for ln in text.split("\n")
    ).strip()


def serialize(final: dict, date: str, extra_warnings: list[str] | None = None) -> dict:
    """把图的产物打成落盘 envelope。

    `extra_warnings` 来自入口体检（preflight）—— 下面那个 `is_degraded_report`
    只认**报告以 `[⚠️` 开头**这一种降级；取数成功但内容是空的时候，模型会写出
    一份看着正常的报告、里面用散文说"数据缺失"，前缀判据看不出来，
    warnings 就会是空的。所以数据层的缺失由体检（preflight）那边告诉我们。
    """
    analysts = []
    warnings = list(extra_warnings or [])
    for role in ROLES:
        raw = final.get(role.report_field, "") or ""
        if is_degraded_report(raw):
            warnings.append(f"{role.title}：数据或生成降级")
        analysts.append({"key": role.key, "title": role.title, "tag": role.tag, "html": md_to_html(raw)})
    macro = final.get("macro_sector_report", "") or ""
    now = china_now().strftime("%Y-%m-%d %H:%M") + " CST"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_type": "daily_review",
        "review_id": f"daily_{date}",
        "target_date": date,
        "trade_date": date,       # 兼容旧前端字段
        "data_as_of": now,
        "generated_at": now,
        "warnings": warnings,
        "focus": final.get("focus_struct"),
        "focus_md": final.get("tomorrow_focus", ""),
        "emotion_metrics": final.get("emotion_metrics") or {},
        "market_facts": final.get("market_facts") or {},
        "macro_sector": macro,
        "reflection": reflection.latest_reflection(),   # 反思闭环：上期命中回看
        "analysts": analysts,
    }

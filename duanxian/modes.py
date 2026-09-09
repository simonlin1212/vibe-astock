"""个人模式卡：把自己的打法写下来，**带版本**，并按版本分段统计业绩。

打法改动后，改动前后的交易不能混在一起算平均，否则得到的均值对谁都没用。
所以每个版本带生效日期（`since`），交易按**发生日期落在哪个版本区间**归属，
用于回答"上一次调整之后表现有没有变化"。

## 卡片字段

`setup` / `entry` / `exit` / `sizing` / `phase` 全部是自由文本，
系统只存储，不校验内容、不给内容建议。它给 `as_planned` 提供一个具体参照物。

## ⚠️ 三条行为约束（改代码时不要破坏）

1. **不预填任何规则内容** —— 默认卡是空的。
2. **开新版本不覆盖旧版本** —— 新版本只影响 `since` 之后的交易归属，
   已发生的交易永远归属它当时生效的版本，历史不可被追溯重写。
3. **样本不够不做比较** —— 标 `enough=False`，不给倾向性结论。

## 边界

只记录使用者自己写的规则、只统计他自己的交易，不产出任何投资建议。
⛔ 本模块的数据**不接入任何 AI prompt**。
"""

from __future__ import annotations

from duanxian.paths import data_path
import json
import os
import uuid
from datetime import date as Date, timedelta
from statistics import median
from typing import Any, Optional

from .util import atomic_write_json, china_now

_DIR = data_path("modes")
_PATH = os.path.join(_DIR, "cards.json")
_SCHEMA = 1

# 每个版本至少要有这么多笔，才对它单独给业绩读数
_MIN_PER_VERSION = 6

# 自由文本字段（系统只存不判断内容）
_TEXT_FIELDS = ("setup", "entry", "exit", "sizing", "phase")

# ⚠️ `playbook` 必须进版本：它是"哪些交易归这张卡"的依据。
#    若只存在卡片层，`performance()` 会拿当前值去筛全部历史交易，
#    等于追溯重写历史归属 —— 正是上面第 2 条约束要防的情况。
_VERSIONED_FIELDS = _TEXT_FIELDS + ("playbook",)


def _load_env() -> dict:
    if not os.path.isfile(_PATH):
        return {"schema": _SCHEMA, "cards": []}
    try:
        with open(_PATH, encoding="utf-8") as fh:
            env = json.load(fh)
        if env.get("schema") == _SCHEMA and isinstance(env.get("cards"), list):
            return env
    except Exception as exc:
        raise ValueError("模式卡存档损坏，原件保留；修复前不允许覆盖写入") from exc
    raise ValueError("模式卡存档格式不受支持，原件保留；修复前不允许覆盖写入")


def list_cards() -> dict:
    env = _load_env()
    return {"cards": env.get("cards") or [], "playbooks_hint": _playbooks()}


def _playbooks() -> list[str]:
    from .journal import PLAYBOOKS

    return list(PLAYBOOKS)


def _clean_text(v: Any, limit: int = 800) -> str:
    return str(v or "").strip()[:limit]


def save_card(card: dict) -> dict:
    """新建或更新一张模式卡。

    `card` 需含 `name` 与 `playbook`（用于把账本里的交易归到这张卡）。
    带 `id` 就是更新，不带就是新建。
    """
    from .journal import PLAYBOOKS
    from .util import china_today

    name = _clean_text(card.get("name"), 60)
    if not name:
        raise ValueError("模式卡要有名字")
    pb = _clean_text(card.get("playbook"), 20)
    if pb not in PLAYBOOKS:
        raise ValueError(f"打法需为 {PLAYBOOKS} 之一，得到 {pb!r}")

    env = _load_env()
    cards = list(env.get("cards") or [])
    cid = card.get("id")
    body = {k: _clean_text(card.get(k)) for k in _TEXT_FIELDS}
    body["playbook"] = pb
    now = china_now().strftime("%Y-%m-%d %H:%M:%S") + " CST"

    idx = next((i for i, c in enumerate(cards) if c.get("id") == cid), None)
    if idx is None:
        if cid:
            raise ValueError("没找到这张模式卡，可能已被删除；请刷新后重试")
        cards.append({
            "id": uuid.uuid4().hex[:12], "name": name, "playbook": pb,
            "created_at": now,
            # 版本 1 从今天生效。⚠️ 不回溯到更早的日期。
            "versions": [{"version": 1, "since": china_today(),
                          "changes": "初版", **body, "saved_at": now}],
        })
        return {"ok": True, "created": True, "count": len(_save(env, cards))}

    cur = cards[idx]
    vers = list(cur.get("versions") or [])
    # ⚠️ 先回填再比较：老版本没有 playbook 字段时直接比会把"没改"误判成"改了"，
    #    导致每次保存都开新版本。
    prev_pb0 = cur.get("playbook")
    if prev_pb0:
        vers = [v if v.get("playbook") else dict(v, playbook=prev_pb0) for v in vers]
    last = vers[-1] if vers else None
    changed = last is None or any((last.get(k) or "") != body[k] for k in _VERSIONED_FIELDS)
    # ⚠️ 改卡片层 playbook 之前，必须先给老版本回填"改之前"的 playbook。
    #    否则 `performance()` 的回退 `v.get("playbook") or c.get("playbook")`
    #    会读到刚改成的新值，历史交易被错配 —— 又是一次追溯重写。
    cur = dict(cur, name=name, playbook=pb)
    if changed:
        # ⚠️ 规则变了开新版本，不覆盖旧版本 —— 旧版本是历史交易的归属依据。
        vers.append({"version": (last or {}).get("version", 0) + 1,
                     "since": (Date.fromisoformat(china_today()) + timedelta(days=1)).isoformat(),
                     "changes": _clean_text(card.get("changes"), 300) or "（未填写改了什么）",
                     **body, "saved_at": now})
    else:
        vers[-1] = dict(vers[-1], saved_at=now)
    cur["versions"] = vers
    cards[idx] = cur
    return {"ok": True, "created": False, "new_version": changed,
            "count": len(_save(env, cards))}


def delete_card(card_id: str) -> dict:
    env = _load_env()
    cards = [c for c in (env.get("cards") or []) if c.get("id") != card_id]
    if len(cards) == len(env.get("cards") or []):
        raise ValueError("没找到这张模式卡")
    return {"ok": True, "count": len(_save(env, cards))}


def _save(env: dict, cards: list[dict]) -> list[dict]:
    os.makedirs(_DIR, exist_ok=True)
    if not atomic_write_json(_PATH, {"schema": _SCHEMA, "cards": cards}):
        raise RuntimeError("模式卡写入失败")
    return cards


# ---------------------------------------------------------------- 按版本分段业绩
def _version_of(versions: list[dict], date: str) -> Optional[dict]:
    """这笔交易发生时，哪个版本在生效。

    ⚠️ 取 `since <= date` 里**最晚**的那个。早于第一个版本的交易返回 None：
    它们发生在卡片存在之前，不计入任何版本。
    """
    elig = [v for v in versions if (v.get("since") or "") <= date]
    return max(elig, key=lambda v: (v.get("since") or "", v.get("version", 0))) \
        if elig else None


# 业绩读数的固定键集。⚠️ 空样本也必须返回全部键（值为 None），
#    否则消费方取 `win_rate` 会直接 KeyError。
_STAT_KEYS = ("trades", "win_rate", "median_pct", "avg_pct", "best", "worst")


def _stats(pnls: list[float]) -> dict:
    if not pnls:
        return {k: (0 if k == "trades" else None) for k in _STAT_KEYS}
    wins = [v for v in pnls if v > 0]
    losses = [v for v in pnls if v < 0]
    decided = len(wins) + len(losses)
    return {
        "trades": len(pnls),
        # ⚠️ 持平不计入胜率分母（分母只含分出胜负的）。
        #    `journal._bucket_stats` 用同一口径，两处必须保持一致
        #    （由 TestWinRateCaliberIsShared 锁住）。
        "win_rate": round(len(wins) / decided, 3) if decided else None,
        "median_pct": round(median(pnls), 2),
        "avg_pct": round(sum(pnls) / len(pnls), 2),
        "best": round(max(pnls), 2), "worst": round(min(pnls), 2),
    }


def performance(limit: int = 1000) -> dict:
    """每张卡、每个版本的业绩分段。"""
    from . import journal

    env = _load_env()
    cards = env.get("cards") or []
    if not cards:
        return {"available": False,
                "reason": "还没有模式卡 —— 先把「我这套打法」写下来，统计才有分段依据"}
    try:
        trade_env = journal.list_trades(limit=limit) or {}
        trades = trade_env.get("trades") or []
        truncated = int(trade_env.get("total", len(trades))) > len(trades)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"读交易日志失败：{exc}"}

    valid_trades = []
    invalid_dates = 0
    for trade in trades:
        if trade.get("pnl_pct") is None:
            continue  # 未形成盈亏样本的记录不参与版本业绩，也不阻断比较。
        day = (trade.get("settled") or {}).get("first_buy") or trade.get("date") or ""
        try:
            parsed = Date.fromisoformat(day)
            if parsed.isoformat() != day:
                raise ValueError("非规范日期")
        except (TypeError, ValueError):
            invalid_dates += 1
            continue
        valid_trades.append(trade)
    out = []
    for c in cards:
        vers = c.get("versions") or []
        # ⚠️ 归属用"那天生效的那个版本的 playbook"，不是卡片当前的 playbook。
        scored = [t for t in valid_trades if t.get("pnl_pct") is not None]
        buckets: dict[int, list[float]] = {}
        before = []                     # 卡片诞生之前的交易，单独放，不进任何版本
        matched = 0
        for t in scored:
            d = (t.get("settled") or {}).get("first_buy") or t.get("date") or ""
            v = _version_of(vers, d)
            if v is None:
                # 卡片诞生之前：用**第一个版本**的 playbook 判断它是否算这张卡的历史
                first_pb = (vers[0].get("playbook") if vers else None) or c.get("playbook")
                if t.get("playbook") == first_pb:
                    matched += 1
                    before.append(float(t["pnl_pct"]))
                continue
            # 老版本可能没存 playbook（v1 结构）→ 退回卡片层的值
            pb_then = v.get("playbook") or c.get("playbook")
            if t.get("playbook") != pb_then:
                continue
            matched += 1
            buckets.setdefault(int(v.get("version", 1)), []).append(float(t["pnl_pct"]))
        by_version = []
        for v in vers:
            n = int(v.get("version", 1))
            st = _stats(buckets.get(n, []))
            by_version.append({
                "version": n, "since": v.get("since"),
                "changes": v.get("changes"),
                "playbook": v.get("playbook") or c.get("playbook"),
                **{k: v.get(k) for k in _TEXT_FIELDS},
                **st,
                # ⚠️ 样本不够不给倾向性比较
                "enough": not truncated and not invalid_dates and st.get("trades", 0) >= _MIN_PER_VERSION,
            })
        effective = [v for i, v in enumerate(by_version)
                     if not any(later["since"] == v["since"] for later in by_version[i+1:])]
        latest = effective[-1] if effective else None
        prev = effective[-2] if len(effective) >= 2 else None
        cmp_ = None
        if latest and prev and latest["enough"] and prev["enough"] \
                and latest.get("win_rate") is not None and prev.get("win_rate") is not None:
            cmp_ = {
                "win_rate_delta": round(latest["win_rate"] - prev["win_rate"], 3),
                "median_delta": round((latest["median_pct"] or 0) - (prev["median_pct"] or 0), 2),
                "from_version": prev["version"], "to_version": latest["version"],
            }
        out.append({
            "id": c.get("id"), "name": c.get("name"), "playbook": c.get("playbook"),
            "version_count": len(vers),
            "by_version": by_version,
            "matched_trades": matched,
            "before_card": {**_stats(before), "enough": not truncated and not invalid_dates and len(before) >= _MIN_PER_VERSION} if before else None,
            "latest_vs_prev": cmp_,
            "compare_blocked": (bool(latest and prev) and cmp_ is None),
        })
    return {
        "available": True,
        "cards": out,
        "min_per_version": _MIN_PER_VERSION,
        "truncated": truncated or bool(invalid_dates),
        "invalid_dates": invalid_dates,
        "note": ((f"有{invalid_dates}笔交易日期异常，未纳入统计，暂停版本比较。" if invalid_dates else "") + "交易按 `playbook` 归到卡片，再按**发生日期落在哪个版本的生效区间**分段。"
                 "按交易日归属：建卡当天纳入首版，修改从次日生效；不作日内先后归因。"
                 "建卡日前的交易单独列出，不计入任何版本 —— 否则等于用现在的规则"
                 "评价更早的操作。"),
    }

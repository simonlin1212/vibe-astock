"""在险资金：当前持仓在最坏情况下会亏掉多少。

    单笔在险 = (成本 − 计划止损价) × 持有股数

看的是**加总**而不是逐笔 —— 多笔各自"只亏 5%"，同时在场就是它们的和。

## ⚠️ 没写计划止损的仓位，风险是"未知"不是"零"

把没填 `planned_stop` 的仓位当 0 风险加进总数，会**系统性低估总在险**，
而且数字看着完全正常。所以：

- 有计划止损 → 算出具体金额
- 没有 → 归入 `unbounded`（未设边界），单独报数量与本金规模
- 汇总时明确写出"另有 N 笔未设边界、本金 X 元，最坏情况无从估计"

⚠️ 计划止损必须是**下单时写下的**值（`journal` v3 迁移一律补 None、不反推，
就是为了保证这一点）。

## 与账户规模的关系

占比的分母只能是使用者自己填的账户规模（`equity_base`）；没填就只给绝对金额、
不给占比。⚠️ **绝不用"历史最大投入"之类的值代替账户规模** —— 那会把占比算小，
而占比正是"是否超限"的判据。

## 边界

只统计使用者自己录入的交易与他自己写下的止损位。不建议止损该设在哪、
不判断该不该减仓，只把他自己的数字加起来。
⛔ 本模块的数据**不接入任何 AI prompt**。
"""

from __future__ import annotations

from duanxian.paths import data_path
import json
import math
import os
from typing import Optional

from .util import atomic_write_json

_DIR = data_path("risk")
_BASE_PATH = os.path.join(_DIR, "equity_base.json")
_BASE_SCHEMA = 1


def load_equity_base() -> Optional[float]:
    """账户规模（用户自己填）。没填返回 None。

    ⚠️ 没填就是 None，**不要拿历史最大投入之类的东西估一个** —— 估大了占比偏小，
    正好在"有没有超限"这个判断上出错。
    """
    if not os.path.exists(_BASE_PATH):
        return None
    try:
        with open(_BASE_PATH, encoding="utf-8") as fh:
            env = json.load(fh)
        if not isinstance(env, dict) or env.get("schema") != _BASE_SCHEMA or isinstance(env.get("equity_base"), bool):
            raise ValueError()
        v = float(env.get("equity_base"))
        if not math.isfinite(v) or v <= 0:
            raise ValueError()
        return v
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("账户规模文件损坏或无法读取；原文件已保留，请重新填写") from exc


def save_equity_base(value: float) -> dict:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("账户规模必须是数字") from exc
    if isinstance(value, bool) or not math.isfinite(v) or v <= 0:
        raise ValueError("账户规模必须是正数")
    os.makedirs(_DIR, exist_ok=True)
    if not atomic_write_json(_BASE_PATH, {"schema": _BASE_SCHEMA, "equity_base": v}):
        raise RuntimeError("账户规模写入失败")
    return {"ok": True, "equity_base": v}


def _open_shares(trade: dict) -> float:
    """还在手上的股数 = 买入总量 − 卖出总量。"""
    bought = sum(float(f.get("shares") or 0) for f in (trade.get("fills") or [])
                 if f.get("side") == "buy")
    sold = sum(float(f.get("shares") or 0) for f in (trade.get("fills") or [])
               if f.get("side") == "sell")
    return max(0.0, bought - sold)


def positions(trades: list[dict]) -> list[dict]:
    """当前未平仓的持仓（按笔）。"""
    out = []
    for t in trades:
        st = t.get("settled") or {}
        if st.get("closed"):
            continue
        shares = _open_shares(t)
        cost = st.get("avg_cost")
        if shares <= 0 or not cost:
            continue
        stop = t.get("planned_stop")
        capital = round(float(cost) * shares, 2)
        item = {
            "id": t.get("id"), "code": t.get("code"), "name": t.get("name"),
            "date": st.get("first_buy") or t.get("date"),
            "playbook": t.get("playbook"),
            "shares": shares, "avg_cost": float(cost), "capital": capital,
            "planned_stop": stop, "planned_target": t.get("planned_target"),
        }
        if stop:
            # ⚠️ 止损价高于成本时在险为 0（已经锁定盈利），不算负数 ——
            # 负的在险会把总数拉低，看着像"风险更小"，其实是另一回事。
            item["at_risk"] = round(max(0.0, (float(cost) - float(stop)) * shares), 2)
            item["at_risk_pct"] = round(max(0.0, (1 - float(stop) / float(cost))) * 100, 2)
            item["bounded"] = True
        else:
            item["at_risk"] = None
            item["at_risk_pct"] = None
            item["bounded"] = False
        out.append(item)
    return sorted(out, key=lambda r: r["date"])


def report() -> dict:
    """在险资金总览。"""
    from . import journal, risk

    try:
        trades = (journal.list_trades(limit=None) or {}).get("trades") or []
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": True, "reason": f"读交易日志失败：{exc}"}

    pos = positions(trades)
    if not pos:
        return {"available": False,
                "reason": "当前没有未平仓的持仓（需要填了成交明细、且还没卖完的记录）"}

    bounded = [p for p in pos if p["bounded"]]
    unbounded = [p for p in pos if not p["bounded"]]
    total_risk = round(sum(p["at_risk"] for p in bounded), 2)
    total_capital = round(sum(p["capital"] for p in pos), 2)
    unbounded_capital = round(sum(p["capital"] for p in unbounded), 2)

    base = load_equity_base()
    rules = risk.load_rules()
    per_trade_limit = rules.get("max_loss_per_trade_pct")
    max_positions = rules.get("max_positions")

    out = {
        "available": True,
        "positions": pos,
        "position_count": len(pos),
        "total_capital": total_capital,
        # ⚠️ 只把**有边界**的加起来。未设边界的不能当 0 混进总数
        "total_at_risk": total_risk,
        "bounded_count": len(bounded),
        "unbounded_count": len(unbounded),
        "unbounded_capital": unbounded_capital,
        "equity_base": base,
        "rules": {"max_loss_per_trade_pct": per_trade_limit,
                  "max_positions": max_positions,
                  "is_default": bool(rules.get("_is_default"))},
    }
    if base:
        out["at_risk_of_equity_pct"] = round(total_risk / base * 100, 2)
        out["capital_of_equity_pct"] = round(total_capital / base * 100, 2)
        # 每笔在险占账户的比例 vs 用户自己写的单笔上限
        if per_trade_limit:
            over = [p for p in bounded
                    if p["at_risk"] / base * 100 > float(per_trade_limit) + 1e-9]
            out["over_per_trade_limit"] = [
                {"name": p["name"], "code": p["code"],
                 "pct_of_equity": round(p["at_risk"] / base * 100, 2)} for p in over]
    else:
        out["equity_base_hint"] = ("填了账户规模才能给占比 —— 绝对金额说明不了"
                                   "'这个风险相对我的账户算大还是小'。")
    if max_positions and len({p["code"] for p in pos}) > int(max_positions):
        out["over_position_limit"] = {"actual": len({p["code"] for p in pos}), "limit": int(max_positions)}
    # 未设边界的必须显式提示，不能让它静静地不出现在总数里
    if unbounded:
        out["unbounded_note"] = (
            f"另有 {len(unbounded)} 笔**未设计划止损**、占用本金 "
            f"{unbounded_capital:,.0f} 元，最坏情况无从估计 —— "
            "它们没有算进上面的总在险，所以那个数字是**下限**。")
    return out


def render(rep: dict) -> str:
    """纯文本形式（给 UI 兜底 / 自用脚本读）。

    ⛔ **不要接进任何 AI prompt** —— 同 `risk.render()`：个人仓位进 prompt
    就成了个性化投资建议。
    """
    if not rep.get("available"):
        return "· 在险资金：" + rep.get("reason", "读取失败") if rep.get("error") else ""
    lines = [f"· 在险资金（{rep['position_count']} 笔在场）："]
    tail = (f"，占账户 {rep['at_risk_of_equity_pct']:.1f}%"
            if rep.get("at_risk_of_equity_pct") is not None else "")
    lines.append(f"  - 有边界的合计在险 {rep['total_at_risk']:,.0f} 元{tail}")
    if rep.get("unbounded_note"):
        lines.append("  ⚠️ " + rep["unbounded_note"].replace("**", ""))
    return "\n".join(lines)

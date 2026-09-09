"""账户风险与执行偏差诊断。

三块：

1. **风险宪法** —— 使用者自己写下的阈值（单笔/单日最大亏损、最大持仓数、连亏降档…）。
   系统只监控有没有违反**他自己写的**规则，不给推荐值。
2. **权益曲线状态** —— 距高点回撤多深多久、多少笔没创新高、连亏分布，
   以及去掉最好的 1 笔 / 3 笔之后还剩多少。
3. **纪律损益归因** —— 只保留标了「按计划」的交易时，曲线会是什么样。

## 边界

全部只统计使用者自己录入的交易数据，不涉及任何证券的分析、预测或建议。
不产出"今天该不该做"，只显示"你写下的规则是什么、有没有遵守、违反之后结果如何"。
⛔ 本模块的数据**不接入任何 AI prompt**。

## 与 journal 的分工

- `journal.py`：记录（一笔交易 = 多次成交，算加权成本 / 已实现盈亏 / 持有天数）
- `risk.py`（本模块）：诊断（回撤形状、纪律偏差、规则违反）
"""

from __future__ import annotations

from duanxian.paths import data_path
import json
import math
import os
from datetime import date as Date
from statistics import mean
from typing import Optional

from .util import atomic_write_json

_DIR = data_path("risk")
_RULES_PATH = os.path.join(_DIR, "rules.json")
_RULES_SCHEMA = 1

# 风险宪法的默认值。全部是"用户自己的规矩"，不是我们推荐的数值 ——
# 不同资金量、不同打法的合理阈值差得远，这里只给一套能跑起来的初值。
DEFAULT_RULES = {
    "max_loss_per_trade_pct": 5.0,     # 单笔最大亏损（%）
    "max_loss_per_day_pct": 8.0,       # 单日最大亏损（占用户填写的账户规模）
    "max_positions": 3,                # 最多同时持仓数
    "max_trades_per_day": 5,           # 单日最多开仓笔数（防手痒）
    "pause_after_losses": 3,           # 连亏几笔后应当停手
    "max_unplanned_ratio": 0.2,        # 计划外交易占比上限
}

_RULE_LABELS = {
    "max_loss_per_trade_pct": "单笔最大亏损",
    "max_loss_per_day_pct": "单日最大亏损",
    "max_positions": "最多同时持仓",
    "max_trades_per_day": "单日最多开仓",
    "pause_after_losses": "连亏几笔后停手",
    "max_unplanned_ratio": "计划外交易占比上限",
}


def load_rules() -> dict:
    """缺文件才用默认值；损坏配置必须显式报错，不能换回宽松阈值。"""
    if not os.path.exists(_RULES_PATH):
        return {**DEFAULT_RULES, "_is_default": True}
    try:
        with open(_RULES_PATH, encoding="utf-8") as fh:
            env = json.load(fh)
        if not isinstance(env, dict) or env.get("schema") != _RULES_SCHEMA or not isinstance(env.get("rules"), dict):
            raise ValueError()
        saved = env["rules"]
        for k, v in saved.items():
            if k not in DEFAULT_RULES or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError()
            if v < 0 or (v == 0 and k != "max_unplanned_ratio") or (k.endswith("_ratio") and v > 1):
                raise ValueError()
            if not k.endswith(("_pct", "_ratio")) and not float(v).is_integer():
                raise ValueError()
        if not saved:
            raise ValueError()
        return {**DEFAULT_RULES, **saved, "_is_default": False}
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("风险规则文件损坏或无法读取；原文件已保留，请重新填写完整规则") from exc


def save_rules(rules: dict) -> dict:
    """存风险宪法。只接受已知键，且必须是正数。"""
    clean = {}
    for k, v in (rules or {}).items():
        if k not in DEFAULT_RULES:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{_RULE_LABELS.get(k, k)} 必须是数字") from exc
        if isinstance(v, bool) or not math.isfinite(f) or f < 0 or (f == 0 and k != "max_unplanned_ratio"):
            raise ValueError(f"{_RULE_LABELS.get(k, k)} 必须是正数")
        if k.endswith("_ratio") and f > 1:
            raise ValueError("计划外交易占比须在0到1之间")
        if not k.endswith(("_pct", "_ratio")) and not f.is_integer():
            raise ValueError(f"{_RULE_LABELS[k]} 必须是整数")
        clean[k] = f if k.endswith(("_pct", "_ratio")) else int(f)
    if not clean:
        raise ValueError("没有可保存的规则")
    # 接受局部编辑，但不悄悄重置其余用户阈值。完整输入可显式修复坏配置。
    if set(clean) != set(DEFAULT_RULES):
        clean = {**{k: v for k, v in load_rules().items() if k in DEFAULT_RULES}, **clean}
    os.makedirs(_DIR, exist_ok=True)
    if not atomic_write_json(_RULES_PATH, {"schema": _RULES_SCHEMA, "rules": clean}):
        raise RuntimeError("风险宪法写入失败")
    return {"ok": True, "rules": clean}


# ---------------------------------------------------------------- 权益曲线
def equity_curve(trades: list[dict]) -> dict:
    """按平仓日排出权益曲线，并诊断它的**形状**。

    累计盈利多少没什么信息量。真正要看的是：距高点回撤多深、回撤多久没恢复、
    多久没创新高、连亏几次、盈利是不是高度依赖少数几笔。
    """
    closed = [t for t in trades
              if (t.get("settled") or {}).get("realized_pnl") is not None]
    if not closed:
        return {"available": False, "reason": "还没有已平仓且填了成交明细的交易"}

    # 按最后卖出日排序 —— 盈亏是在平仓那天落地的
    closed.sort(key=lambda t: (t["settled"].get("last_sell") or t["date"], (t.get("created_at") or "")))
    points, cum, peak, peak_date = [], 0.0, 0.0, None
    max_dd, max_dd_from, dd_start = 0.0, None, None
    longest_underwater, cur_underwater = 0, 0
    for t in closed:
        pnl = float(t["settled"]["realized_pnl"])
        cum += pnl
        d = t["settled"].get("last_sell") or t["date"]
        if cum >= peak:
            peak, peak_date, cur_underwater, dd_start = cum, d, 0, None
        else:
            cur_underwater += 1
            longest_underwater = max(longest_underwater, cur_underwater)
            if dd_start is None:
                dd_start = d
            dd = peak - cum
            if dd > max_dd:
                max_dd, max_dd_from = dd, dd_start
        points.append({"date": d, "cum_pnl": round(cum, 2),
                       "pnl": round(pnl, 2), "drawdown": round(peak - cum, 2)})

    pnls = [float(t["settled"]["realized_pnl"]) for t in closed]
    wins = [v for v in pnls if v > 0]
    losses = [v for v in pnls if v < 0]
    # 连亏分布：最长连亏几笔
    streak, worst_streak = 0, 0
    for v in pnls:
        streak = streak + 1 if v < 0 else 0
        worst_streak = max(worst_streak, streak)
    # 盈利集中度：去掉最好 1 笔 / 3 笔之后还剩多少
    top = sorted(pnls, reverse=True)
    return {
        "available": True,
        "points": points,
        "date_note": "缺平仓日期的记录按录入日排列" if any(not t["settled"].get("last_sell") for t in closed) else "按平仓日排列",
        "trades": len(closed),
        "net_pnl": round(cum, 2),
        "peak": round(peak, 2),
        "peak_date": peak_date,
        "current_drawdown": round(peak - cum, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_since": max_dd_from,
        # 多久没创新高（按笔数算，不是日历天）—— 这是"手感还在不在"的直接读数
        "trades_since_peak": cur_underwater,
        "longest_underwater": longest_underwater,
        # ⚠️ **盈亏恰好为 0 的笔不进分母**（同 journal 战绩与 rolling 的口径）：
        # 持平既不是赢也不是输，算进分母会把胜率稀释。两处口径不同的话，
        # 同一页会出现两个不同的"终身胜率"，而各自看都很正常。测试已锁。
        "win_rate": (round(len(wins) / (len(wins) + len(losses)), 3)
                     if (wins or losses) else None),
        "avg_win": round(mean(wins), 2) if wins else None,
        "avg_loss": round(mean(losses), 2) if losses else None,
        # 盈亏比与 Profit Factor —— 胜率单独看没用，得配上赔率
        "payoff_ratio": (round(mean(wins) / abs(mean(losses)), 2)
                         if wins and losses else None),
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses and sum(losses) else None),
        "worst_losing_streak": worst_streak,
        "worst_trade": round(min(pnls), 2),
        # ⚠️ 这两个数最能揭穿"我其实是靠一两笔运气"：去掉最好的几笔之后还剩多少
        "net_without_best1": round(cum - (top[0] if top else 0), 2),
        "net_without_best3": round(cum - sum(top[:3]), 2),
        "best_trade_share": (round(top[0] / cum, 3)
                             if top and cum > 0 else None),
    }


# ---------------------------------------------------------------- 纪律归因
def discipline(trades: list[dict]) -> dict:
    """纪律损益归因 —— **删掉所有计划外交易，曲线会怎样？**

    这一张账最狠也最有用：它把"纪律"从一句口号变成一个可比的数字。
    """
    scored = [t for t in trades if t.get("pnl_pct") is not None
              or (t.get("settled") or {}).get("realized_pnl") is not None]
    if not scored:
        return {"available": False, "reason": "还没有可统计的交易"}

    def money(t: dict) -> Optional[float]:
        v = (t.get("settled") or {}).get("realized_pnl")
        return float(v) if v is not None else None

    def bucket(rows: list[dict]) -> dict:
        pcts = [t["pnl_pct"] for t in rows if t.get("pnl_pct") is not None]
        moneys = [money(t) for t in rows if money(t) is not None]
        return {
            "count": len(rows),
            "win_rate": (round(sum(1 for v in pcts if v > 0) / len(pcts), 3)
                         if pcts else None),
            "avg_pct": round(mean(pcts), 2) if pcts else None,
            "net_pnl": round(sum(moneys), 2) if moneys else None,
        }

    planned = [t for t in scored if t.get("as_planned") is True]
    unplanned = [t for t in scored if t.get("as_planned") is False]
    untagged = [t for t in scored if t.get("as_planned") is None]

    all_money = [money(t) for t in scored if money(t) is not None]
    planned_money = [money(t) for t in planned if money(t) is not None]
    return {
        "available": True,
        "planned": bucket(planned),
        "unplanned": bucket(unplanned),
        "untagged": bucket(untagged),
        "execution_rate": (round(len(planned) / (len(planned) + len(unplanned)), 3)
                           if (planned or unplanned) else None),
        # ⭐ 最狠的一行：只做按计划的交易，账户会是什么样
        "what_if_only_planned": {
            "actual_net": round(sum(all_money), 2) if all_money else None,
            "planned_only_net": round(sum(planned_money), 2) if planned_money else None,
            "cost_of_indiscipline": (round(sum(all_money) - sum(planned_money), 2)
                                     if all_money and planned_money else None),
        },
        "note": ("「计划外」的交易需要你自己在录入时标注 —— 没标注的归到未标注，"
                 "不猜。这不是市场分析，是你自己的行为统计。"),
    }


# ---------------------------------------------------------------- 规则违反
def violations(trades: list[dict], rules: Optional[dict] = None) -> dict:
    """按用户自己的风险宪法逐条检查有没有违反。

    ⚠️ 这里**只对照用户写下的规则**，不替他判断该不该交易。
    "今天不该做"这种话只能由他自己的规则得出，系统负责执行与提醒。
    """
    rules = rules or load_rules()
    if not trades:
        return {"available": False, "reason": "还没有交易记录"}

    by_day: dict[str, list[dict]] = {}
    for t in trades:
        by_day.setdefault((t.get("settled") or {}).get("first_buy") or t.get("date") or "", []).append(t)

    # ⚠️ 每条规则都要报「查了没有」。只给一个总违规数的话，
    #    规则明明配了却从没被检查过，界面上会显示成"0 次违反"——
    #    使用者以为自己守住了，其实那条根本没跑。
    checked: dict[str, str] = {}

    # ---- 单日最大亏损：按**平仓日**汇总净盈亏 ----
    # 需要金额与账户规模才能算占比；缺任一样就如实标 unavailable，绝不按 0 处理。
    equity_base = None
    equity_error = None
    try:
        from .at_risk import load_equity_base

        equity_base = load_equity_base()
    except (OSError, ValueError) as exc:
        equity_error = str(exc)

    found = []
    day_pnl: dict[str, float] = {}
    for t in trades:
        st = t.get("settled") or {}
        # ⚠️ 优先用按成交日拆分的账。整笔累计额全挂到 last_sell 的话，
        #    分两天减仓时两天盈亏会并到后一天，这条规则就查不准。
        by_date = st.get("realized_by_date")
        if isinstance(by_date, dict) and by_date:
            for d, v in by_date.items():
                day_pnl[d] = day_pnl.get(d, 0.0) + float(v)
            continue
        pnl, sell_day = st.get("realized_pnl"), st.get("last_sell")
        if pnl is not None and sell_day:   # 老记录没有按日拆分，退回整笔挂平仓日
            day_pnl[sell_day] = day_pnl.get(sell_day, 0.0) + float(pnl)
    if equity_error:
        checked["max_loss_per_day_pct"] = "unavailable：" + equity_error
    elif not day_pnl:
        checked["max_loss_per_day_pct"] = "unavailable：没有带成交明细的已平仓交易，算不出单日盈亏"
    elif not equity_base:
        checked["max_loss_per_day_pct"] = "unavailable：没填账户规模，单日亏损占比没有分母"
    else:
        checked["max_loss_per_day_pct"] = "checked"
        cap = abs(rules["max_loss_per_day_pct"])
        for day, pnl in sorted(day_pnl.items()):
            pct = pnl / equity_base * 100
            if pct < -cap:
                found.append({"date": day, "rule": "max_loss_per_day_pct",
                              "label": _RULE_LABELS["max_loss_per_day_pct"],
                              "limit": -cap, "actual": round(pct, 2),
                              "detail": f"{day} 当日净亏 {round(pnl, 2)} 元，占账户 {round(pct, 2)}%"})

    # ---- 最大持仓数：按成交事件重建每天的并发持仓 ----
    # ⚠️ 原先只在"当前在险"里查一次，历史上曾经超限完全看不出来。
    # ---- 最大持仓数：按**代码**统计每天同时持有几只 ----
    # ⚠️ 三个口径写在这里，改之前先读：
    #    ① 按**代码**聚合，不是按记录条数 —— 同一只票分两笔建仓仍然只占一个仓位，
    #       与持仓页把同代码合并成一行是同一口径。
    #    ② 用**持有区间**判断，不维护一个逐事件游走的计数器 —— 后者在"当天买当天卖"
    #       这种记录上会先减到 0 被清掉、再加回 1，于是这只票永远留在持仓集合里，
    #       之后每一天的峰值都虚高，而且完全看不出来。
    #    ③ 区间是**左闭右开** `[建仓日, 平仓日)`：卖出当天不再计入持仓。
    #       所以换仓那天（卖 A 买 B）不会短暂多算一个；当日进出的做 T 不计入任何一天
    #       —— 它没有留下隔夜仓位，而这条规则限制的就是同时压着几只票。
    spans: list[tuple[str, str, Optional[str]]] = []   # (code, 建仓日, 平仓日或 None)
    # ⚠️ 用 all 不是 any：只要**有一条**记录没有成交明细，这一条的结论就是按日期近似出来的，
    #    必须如实标注。写成 any 的话，一条有明细就显示成 "checked"，把近似掩盖掉了。
    approx = 0
    undated_closed = 0
    invalid_spans = 0
    for t in trades:
        st = t.get("settled") or {}
        start = st.get("first_buy") or t.get("date")
        if (not st.get("has_fills") and t.get("pnl_pct") is not None) or (st.get("closed") and not st.get("last_sell")):
            undated_closed += 1
            continue
        end = st.get("last_sell") if st.get("closed") else None
        code = str(t.get("code") or "")
        try:
            if not code.isascii() or not code.isdigit() or len(code) > 6:
                raise ValueError("无效代码")
            if Date.fromisoformat(start).isoformat() != start:
                raise ValueError("无效建仓日")
            if end is not None and (Date.fromisoformat(end).isoformat() != end or end < start):
                raise ValueError("无效平仓日")
        except (TypeError, ValueError):
            invalid_spans += 1
            continue
        if not st.get("has_fills"):
            approx += 1
        spans.append((code.zfill(6), start, end))
    if not spans:
        checked["max_positions"] = "unavailable：没有可用的建仓/平仓日期"
    else:
        checked["max_positions"] = "checked" if approx == 0 else \
            f"checked（其中 {approx} 条没有成交明细，按记录日期近似）"
        cap_n = int(rules["max_positions"])
        days = sorted({d for _, s0, e0 in spans for d in (s0, e0) if d})
        for day in days:
            holding = {c for c, s0, e0 in spans if s0 <= day and (e0 is None or day < e0)}
            if len(holding) > cap_n:
                found.append({"date": day, "rule": "max_positions",
                              "label": _RULE_LABELS["max_positions"],
                              "limit": cap_n, "actual": len(holding),
                              "detail": f"{day} 同时持有 {len(holding)} 只"})

    if undated_closed:
        checked["max_positions"] = "unavailable：有手填盈亏但缺成交日期或缺平仓日期的记录，未能完整核对历史持仓上限；已确认的超限仍展示，结果可能漏计"
    if invalid_spans:
        reason = "有代码或建仓/平仓日期异常的记录，未纳入持仓区间"
        checked["max_positions"] = (checked["max_positions"] + "；另有" + reason[1:]) if undated_closed else \
            "unavailable：" + reason + "；已确认的超限仍展示，结果可能漏计"

    for day, rows in sorted(by_day.items()):
        # 单日开仓笔数
        if len(rows) > rules["max_trades_per_day"]:
            found.append({"date": day, "rule": "max_trades_per_day",
                          "label": _RULE_LABELS["max_trades_per_day"],
                          "limit": rules["max_trades_per_day"], "actual": len(rows),
                          "detail": f"{day} 开仓 {len(rows)} 笔"})
        # 单笔亏损
        for t in rows:
            p = t.get("pnl_pct")
            if p is not None and p < -abs(rules["max_loss_per_trade_pct"]):
                found.append({"date": day, "rule": "max_loss_per_trade_pct",
                              "label": _RULE_LABELS["max_loss_per_trade_pct"],
                              "limit": -abs(rules["max_loss_per_trade_pct"]), "actual": p,
                              "detail": f"{t.get('name') or t.get('code')} 亏 {p}%"})
        # 计划外占比
        tagged = [t for t in rows if t.get("as_planned") is not None]
        if tagged:
            un = sum(1 for t in tagged if t["as_planned"] is False)
            ratio = un / len(tagged)
            if ratio > rules["max_unplanned_ratio"]:
                found.append({"date": day, "rule": "max_unplanned_ratio",
                              "label": _RULE_LABELS["max_unplanned_ratio"],
                              "limit": rules["max_unplanned_ratio"], "actual": round(ratio, 3),
                              "detail": f"{day} 计划外 {un}/{len(tagged)} 笔"})

    # 连亏后是否继续交易 —— 只陈述历史事实，不下"该停手"的结论
    closed = sorted((t for t in trades if t.get("pnl_pct") is not None),
                    key=lambda t: (t.get("date") or "", t.get("created_at") or ""))
    streak, after_streak = 0, []
    limit = int(rules["pause_after_losses"])
    for t in closed:
        if streak >= limit:
            after_streak.append(t["pnl_pct"])
        streak = streak + 1 if t["pnl_pct"] < 0 else 0
    checked["max_trades_per_day"] = "checked"
    checked["max_loss_per_trade_pct"] = "checked"
    checked["max_unplanned_ratio"] = "checked" if any(
        t.get("as_planned") is not None for t in trades) else \
        "unavailable：还没有标注过「按计划 / 计划外」的交易"
    checked["pause_after_losses"] = "checked"

    return {
        "available": True,
        "rules": {k: v for k, v in rules.items() if not k.startswith("_")},
        "is_default_rules": rules.get("_is_default", False),
        # 每条规则查了没有。⚠️ 界面必须把"没查"和"查了没违反"分开显示。
        "rule_status": checked,
        "unchecked": [k for k, v in checked.items() if v.startswith("unavailable")],
        "violations": found,
        "violation_count": len(found),
        # 你自己的历史：连亏达到自设阈值后还继续做的那些笔，结果如何
        "after_loss_streak": {
            "threshold": limit,
            "trades": len(after_streak),
            "avg_pct": round(mean(after_streak), 2) if after_streak else None,
            "win_rate": (round(sum(1 for v in after_streak if v > 0) / len(after_streak), 3)
                         if after_streak else None),
        },
    }


def hold_days_of(t: dict) -> Optional[int]:
    st = t.get("settled") or {}
    return st.get("hold_days")


# ---------------------------------------------------------------- 滚动窗口
# ⚠️ **终身统计会把最近的退化藏起来。**
#
# 一个人做了 200 笔，前 150 笔赚钱、最近 50 笔一直在亏 —— 终身胜率、终身盈亏比
# 全都还是漂亮的，账面上完全看不出"手感已经没了"。而对短线来说，**最近这批
# 才是有效样本**（市场结构变了、自己的状态变了、打法可能已经失效）。
#
# 所以除了终身，另给近 10 / 20 / 50 笔三个窗口，并排看趋势。
# 10 笔看状态、20 笔看节奏、50 笔看打法是否还成立。
_WINDOWS = (10, 20, 50)

# 窗口小于这个数就不给"趋势"性描述（10 笔胜率本来就跳得厉害）
_MIN_TREND_WINDOW = 10


def _window_stats(closed: list[dict]) -> dict:
    """一个窗口内的核心读数。closed 必须**已按平仓日排好序**。"""
    pnls = [float(t["settled"]["realized_pnl"]) for t in closed]
    if not pnls:
        return {"trades": 0}
    wins = [v for v in pnls if v > 0]
    losses = [v for v in pnls if v < 0]
    # ⚠️ 盈亏恰好为 0 的笔**不进胜率分母**（同 journal 战绩口径："持平"不算输也不算赢）
    decided = len(wins) + len(losses)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    planned = [t for t in closed if t.get("as_planned") is True]
    unplanned = [t for t in closed if t.get("as_planned") is False]
    return {
        "trades": len(pnls),
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / decided, 3) if decided else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "payoff_ratio": (round((gross_win / len(wins)) / (gross_loss / len(losses)), 2)
                         if wins and losses else None),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        # 执行率也按窗口看 —— 纪律是会滑坡的，终身执行率看不出最近在放飞
        "execution_rate": (round(len(planned) / (len(planned) + len(unplanned)), 3)
                           if (planned or unplanned) else None),
        "date_note": "缺平仓日期的记录按录入日排列" if any(not t["settled"].get("last_sell") for t in closed) else "按平仓日排列",
        "date_from": (closed[0]["settled"].get("last_sell") or closed[0]["date"]),
        "date_to": (closed[-1]["settled"].get("last_sell") or closed[-1]["date"]),
    }


def rolling(trades: list[dict]) -> dict:
    """终身 + 近 10/20/50 笔的并排对比。

    ⚠️ 窗口按**平仓日**取最后 N 笔（盈亏是在平仓那天落地的），和权益曲线同一口径 ——
    两处用不同排序会让"最近 10 笔"和曲线尾巴对不上，而两边各自看都很正常。
    """
    closed = [t for t in trades
              if (t.get("settled") or {}).get("realized_pnl") is not None]
    if not closed:
        return {"available": False, "reason": "还没有已平仓且填了成交明细的交易"}
    closed.sort(key=lambda t: (t["settled"].get("last_sell") or t["date"], (t.get("created_at") or "")))

    out = {"available": True, "windows": {}, "lifetime": _window_stats(closed)}
    for n in _WINDOWS:
        # ⚠️ 样本不够时**不要拿全部冒充那个窗口**：只有 12 笔却报"近 50 笔"，
        # 会让人以为打法在 50 笔的尺度上验证过。如实标 enough=False。
        w = _window_stats(closed[-n:])
        w["window"] = n
        w["enough"] = len(closed) >= n
        out["windows"][str(n)] = w

    # 趋势：近 10 笔 vs 终身，落差最能说明"手感还在不在"
    w10 = out["windows"]["10"]
    life = out["lifetime"]
    if w10.get("enough") and w10.get("win_rate") is not None and life.get("win_rate") is not None:
        out["win_rate_drift"] = round(w10["win_rate"] - life["win_rate"], 3)
    if (w10.get("enough") and w10.get("profit_factor") is not None
            and life.get("profit_factor") is not None):
        out["profit_factor_drift"] = round(w10["profit_factor"] - life["profit_factor"], 2)
    out["note"] = ("10 笔看状态、20 笔看节奏、50 笔看打法是否还成立。"
                   "终身统计会把最近的退化藏起来 —— 前面赚够了，最近一直亏，"
                   "终身数字照样漂亮。")
    return out


def report() -> dict:
    """风控总报告：权益曲线 + 纪律归因 + 规则违反。全部基于用户自己的数据。"""
    from .journal import list_trades

    trades = list_trades(limit=None)["trades"]
    rules = load_rules()
    return {
        "equity": equity_curve(trades),
        "rolling": rolling(trades),
        "discipline": discipline(trades),
        "violations": violations(trades, rules),
        "trade_count": len(trades),
    }


def render(rep: dict) -> str:
    """风控报告 → 文本（给 UI 兜底展示 / 自用脚本读）。

    ⛔ **不要接进任何 AI prompt。** 早先这里写的是"给复盘 prompt 用，让 AI 知道
    用户自己的处境"—— 那个方向是错的：个人持仓与盈亏一旦进 prompt，模型的回答
    就变成"针对这个人当前处境"的意见，**那正是个性化投资建议**，是本项目合规
    立足点（非个性化）唯一不能碰的那条线。见记忆
    `project_vibe-astock-commercialization-legal`。

    个人数据只走只读 API 给前端渲染，AI 永远看不到。测试已锁
    （`TestPersonalDataNeverReachesPrompt`）。
    """
    eq = rep.get("equity") or {}
    dp = rep.get("discipline") or {}
    vi = rep.get("violations") or {}
    if not eq.get("available"):
        return f"[个人风控：{eq.get('reason', '暂无数据')}]"
    lines = [f"[个人风控（仅你自己的 {rep.get('trade_count')} 笔记录）]"]
    lines.append(
        f"· 权益：净盈亏 {eq['net_pnl']}，距高点回撤 {eq['current_drawdown']}"
        f"（历史最大 {eq['max_drawdown']}），已 {eq['trades_since_peak']} 笔未创新高；"
        + (f"胜率 {eq['win_rate']:.0%}" if eq.get("win_rate") is not None else "胜率 —（全部持平）")
        + (f"，盈亏比 {eq['payoff_ratio']}" if eq.get("payoff_ratio") else "")
        + (f"，Profit Factor {eq['profit_factor']}" if eq.get("profit_factor") else "")
    )
    if eq.get("best_trade_share") is not None:
        lines.append(f"· 盈利集中度：最好一笔占净利 {eq['best_trade_share']:.0%}；"
                     f"去掉最好 1 笔剩 {eq['net_without_best1']}，"
                     f"去掉最好 3 笔剩 {eq['net_without_best3']}")
    if dp.get("available") and dp.get("execution_rate") is not None:
        wi = dp["what_if_only_planned"]
        seg = f"· 纪律：执行率 {dp['execution_rate']:.0%}"
        if wi.get("cost_of_indiscipline") is not None:
            seg += (f"；只做按计划的交易，净盈亏会是 {wi['planned_only_net']}"
                    f"（实际 {wi['actual_net']}，差 {wi['cost_of_indiscipline']}）")
        lines.append(seg)
    if vi.get("available") and vi.get("violation_count"):
        lines.append(f"· 违反自设规则 {vi['violation_count']} 次"
                     + ("（用的还是默认阈值，建议先按自己的习惯改一遍）"
                        if vi.get("is_default_rules") else ""))
    return "\n".join(lines)

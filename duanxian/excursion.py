"""MFE / MAE 与盈利回吐。

一笔交易的 `realized_pct` 只说了终点，不说过程：

- **MFE**（最大浮盈）：这笔最多曾赚到多少。MFE 远高于实际落袋 = 盈利回吐。
- **MAE**（最大浮亏）：这笔最多曾亏到多少。
- **捕获率** = 实际落袋 ÷ MFE。

## ⚠️ 日 K 的先天限制（必须如实标出）

只能拿到日线的最高/最低价，看不出那个极值发生在盘中买入之前还是之后：

- **当日进出（T0）**：整段窗口都存疑，MFE 是**上界**而不是"确实赚到过这么多"。
- **跨日持有**：中间那些完整交易日的高低点一定在持仓期内，那部分可靠；
  只有买入日与卖出日两头存疑。

所以每笔都带 `precision` 与 `caveat`，并另给 `*_certain`
（只取严格落在买入日与卖出日之间的完整交易日）。

⚠️ **两个 certain 的方向相反，不是同一种"下界"**：

- `mfe_certain`：确定至少赚到过这么多 → 真实 MFE 落在 `[mfe_certain, mfe_pct]`
- `mae_certain`：确定至少差到过这个程度 → 真实 MAE 落在 `[mae_pct, mae_certain]`
  （`mae_pct` 是最坏可能，`mae_certain` 是"最不坏"的那一端）
  ⚠️ `mae_certain` **可能是正数**，那表示中间那几天从未浮亏，不要读成浮亏。

⚠️ 汇总用的是上界 MFE，**捕获率会被系统性低估**。这个偏差必须与结论并列展示。

## 边界

只统计使用者自己录入的交易 + 公开历史行情。不产出任何操作建议，
不说"应该拿到什么位置"，只陈述这笔曾经到过哪里、实际拿到了多少。
⛔ 本模块的数据**不接入任何 AI prompt**。
"""

from __future__ import annotations

from duanxian.paths import data_path
import json
import os
from statistics import median
from typing import Optional

from .util import atomic_write_json

_CACHE_DIR = data_path("cache/bars")

# 少于这么多笔就不给汇总性描述（几笔的捕获率中位数没有意义）
_MIN_TRADES = 6

# 判定"这段行情本来有肉"的门槛：MFE 低于此值时捕获率没有讨论价值
# （买进去就没怎么涨过，谈不上"卖早了"）
_MEANINGFUL_MFE = 3.0

# 捕获率低于此值提示"几乎没吃到"（与 inbox 的 _LOW_CAPTURE 同量级，各自独立不互相依赖）
_LOW_CAPTURE_HINT = 0.3


def _cache_path(code: str, start: str, end: str) -> str:
    return os.path.join(_CACHE_DIR, f"{code}_{start}_{end}.json")


def _tx_symbol(code: str) -> str:
    code = str(code).zfill(6)
    # 与 deepdive/data.py 同一口径：北交所 8x/4x/920 归 sz 前缀由数据源决定，
    # 这里只区分沪/深，取不到时上层会如实标 unavailable。
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def read_cached_bars(code: str, start: str, end: str) -> Optional[list[dict]]:
    """只读缓存里的行情，**任何情况下都不发请求**。读不到/坏了返回 None。

    ⚠️ 不能只判 `os.path.isfile`：缓存文件存在但**内容坏了**时，
    `bars()` 会把它当没命中、接着发 akshare 请求 —— "零网络"的承诺就破了。
    所以这里自己读、自己校验内容。
    """
    p = _cache_path(str(code).zfill(6), start, end)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:  # noqa: BLE001  坏缓存 = 没有，但**绝不因此去联网**
        return None
    return rows if isinstance(rows, list) and rows else None


def for_trade_cached_only(trade: dict) -> Optional[dict]:
    """只在**行情已缓存**时算 MFE/MAE，否则返回 None（一次网络请求都不发）。

    ⚠️ 存在的理由：`excursion` 单独成端点就是因为它**逐笔要拉行情**。
    调用方若内联调 `for_trade()`，几百笔的账本首次打开就是几百次串行请求，
    接口会卡几分钟甚至超时。
    """
    st = trade.get("settled") or {}
    buy, sell = st.get("first_buy"), st.get("last_sell")
    if not (buy and sell):
        return None
    rows = read_cached_bars(trade.get("code") or "", buy, sell)
    if rows is None:
        return None
    # ⚠️ 把读到的行**直接传下去**，不能再走一次 `bars()` —— 那条路在缓存坏时会联网
    r = _compute(trade, rows)
    return r if r.get("available") else None


def bars(code: str, start: str, end: str) -> Optional[list[dict]]:
    """取 [start, end] 的日线（含 high/low）。**历史事实不会变 → 永久缓存。**

    取不到返回 None（不是空列表）—— 上层要能区分"那段没有交易日"和"取数失败"。
    """
    p = _cache_path(code, start, end)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                rows = json.load(fh)
            if isinstance(rows, list):
                return rows
        except Exception:  # noqa: BLE001  坏缓存当没缓存
            pass
    try:
        import akshare as ak

        df = ak.stock_zh_a_hist_tx(symbol=_tx_symbol(code),
                                   start_date=start.replace("-", ""),
                                   end_date=end.replace("-", ""))
        if df is None or len(df) == 0:
            return None
        rows = [{"date": str(r["date"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"])}
                for _, r in df.iterrows()]
    except Exception:  # noqa: BLE001  取数失败如实返回 None，绝不当成"没波动"
        return None
    os.makedirs(_CACHE_DIR, exist_ok=True)
    atomic_write_json(p, rows)
    return rows


def _pct(v: float, cost: float) -> float:
    return round((v / cost - 1) * 100, 2)


def for_trade(trade: dict) -> dict:
    """单笔的 MFE / MAE / 回吐 / 捕获率。"""
    st = trade.get("settled") or {}
    cost = st.get("avg_cost")
    buy, sell = st.get("first_buy"), st.get("last_sell")
    realized = st.get("realized_pct")
    if not (cost and buy and sell and realized is not None):
        return {"available": False,
                "reason": "需要已平仓 + 有成交明细（要用加权成本与买卖日期）"}

    rows = bars(trade.get("code", ""), buy, sell)
    if not rows:
        return {"available": False, "reason": "取不到这段的日线行情"}
    return _compute(trade, rows)


def _compute(trade: dict, rows: list[dict]) -> dict:
    """给定行情行算出 MFE/MAE 等。**纯计算，不碰网络** —— 这样"只读缓存"的
    调用方可以复用同一套算法而不会意外触发请求。"""
    st = trade.get("settled") or {}
    cost = st.get("avg_cost")
    buy, sell = st.get("first_buy"), st.get("last_sell")
    realized = st.get("realized_pct")
    if not (cost and buy and sell and realized is not None):
        return {"available": False,
                "reason": "需要已平仓 + 有成交明细（要用加权成本与买卖日期）"}

    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    mfe = _pct(max(highs), float(cost))
    mae = _pct(min(lows), float(cost))

    # 只取**严格在**买卖日之间的完整交易日 → 这几天一定在持仓期内。
    # ⚠️ 对 MFE 是"确定赚到过"（下界），对 MAE 是"确定差到过"（最不坏的那一端），
    # 方向不同，别当成同一种界（第一版标错过）。
    inner = [r for r in rows if buy < r["date"] < sell]
    mfe_certain = _pct(max(r["high"] for r in inner), float(cost)) if inner else None
    mae_certain = _pct(min(r["low"] for r in inner), float(cost)) if inner else None

    same_day = buy == sell
    out = {
        "available": True,
        "code": trade.get("code"), "name": trade.get("name"),
        "date": buy, "exit_date": sell,
        "realized_pct": round(float(realized), 2),
        "mfe_pct": mfe, "mae_pct": mae,
        # ⚠️ 方向不同：mfe_certain = 确定至少赚到过这么多；
        #    mae_certain = 确定至少差到过这个程度（**可能为正 = 那几天从未浮亏**）
        "mfe_certain": mfe_certain, "mae_certain": mae_certain,
        "certain_note": (None if mae_certain is None else
                         ("中间完整交易日里从未浮亏（最差时仍 "
                          f"{mae_certain:+.2f}%）" if mae_certain > 0 else
                          f"中间完整交易日里确定浮亏过 {mae_certain:+.2f}%")),
        "bars": len(rows), "bars_inner": len(inner),
        "same_day": same_day,
        "precision": "上界（同日进出，日线分不清高点在买入前还是后）" if same_day
                     else ("买卖日两头存疑，中间完整交易日可靠" if inner
                           else "上界（相邻两日，无完整中间交易日）"),
    }
    # 盈利回吐：曾经到过的最好 减 实际落袋
    out["give_back_pct"] = round(mfe - out["realized_pct"], 2)
    # 捕获率：只在"这段本来有肉"时才有讨论价值
    out["capture_rate"] = (round(out["realized_pct"] / mfe, 3)
                           if mfe >= _MEANINGFUL_MFE else None)
    # ⚠️ **捕获率可能是负数**：MFE 为正（这段确实涨过）但最后亏损离场 → 比值为负。
    # 数学上没错，可"捕获率 −41%"读起来像故障，得给一句人话。
    # 这其实是**比"没吃到"更差的一档**：有肉不但没吃到，还亏出去了。
    cr = out["capture_rate"]
    out["capture_note"] = (
        "这段涨过，但最后是亏损离场 —— 比「没吃到」更差的一档" if (cr is not None and cr < 0)
        else ("几乎没吃到这段行情" if (cr is not None and cr < _LOW_CAPTURE_HINT) else None))
    return out


def summary(limit: int = 300) -> dict:
    """所有已平仓交易的 MFE/MAE 汇总。

    ⚠️ 逐笔要请求行情（历史已永久缓存，首次会慢）→ **单独端点，不进 report()**，
    否则每次打开交易日志页都要等一轮网络。
    """
    from . import journal

    try:
        trades = (journal.list_trades(limit=limit) or {}).get("trades") or []
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"读交易日志失败：{exc}"}

    items, failed = [], 0
    for t in trades:
        r = for_trade(t)
        if r.get("available"):
            items.append(r)
        else:
            failed += 1
    if not items:
        return {"available": False, "failed": failed,
                "reason": "还没有能算 MFE/MAE 的交易（需要已平仓 + 成交明细 + 能取到行情）"}

    caps = [r["capture_rate"] for r in items if r.get("capture_rate") is not None]
    gives = [r["give_back_pct"] for r in items]
    maes = [r["mae_pct"] for r in items]
    # 赚钱收场但中途深亏过 —— 扛出来的那些（这次赢了，但是坏习惯的样本）
    endured = [r for r in items if r["realized_pct"] > 0 and r["mae_pct"] <= -5]
    # 亏钱收场且从没怎么浮盈过 —— 进场就不对，不是拿不住
    bad_entry = [r for r in items
                 if r["realized_pct"] < 0 and r["mfe_pct"] < _MEANINGFUL_MFE]
    same_day_n = sum(1 for r in items if r["same_day"])
    # 有肉却亏损离场的笔数 —— 单独报，别让"负捕获率"看着像算错了
    lost_with_move = sum(1 for r in items
                         if r.get("capture_rate") is not None and r["capture_rate"] < 0)
    return {
        "available": True,
        "trades": len(items),
        "failed": failed,
        "enough_samples": len(items) >= _MIN_TRADES,
        # ⚠️ 一律用中位数：少数极端值会把均值拉得看不出典型情况
        "median_capture_rate": round(median(caps), 3) if caps else None,
        "capture_samples": len(caps),
        "median_give_back": round(median(gives), 2),
        "median_mae": round(median(maes), 2),
        "endured_count": len(endured),
        "bad_entry_count": len(bad_entry),
        "same_day_count": same_day_n,
        "lost_with_move_count": lost_with_move,
        "capture_note": (
            f"其中 {lost_with_move} 笔**捕获率为负** —— 那段确实涨过，但最后是亏损离场，"
            "比「没吃到」更差的一档（所以中位数可能是负值，不是算错了）。"
            if lost_with_move else None),
        "items": sorted(items, key=lambda r: r["date"], reverse=True),
        # 偏差必须和结论摆在一起
        "bias_note": ("MFE 取的是日线高点，含「买入之前」的那部分 → **捕获率被系统性低估**。"
                      f"其中 {same_day_n} 笔是同日进出，整段窗口都只能给上界。"
                      "真实 MFE 落在每笔的 mfe_certain（确定赚到过）与 mfe_pct（上界）之间。MAE 同样包含入场前或离场后的日线低点，只能视为最不利边界，不能当作实际持仓最大浮亏。"),
        "caveat": ("日线看不出最高点发生在盘中买入之前还是之后。跨日持有时，"
                   "中间完整交易日的高低点在持仓期内；本模块以未复权日线对照手填成交成本，遇除权除息、拆股或送转时两者可能不可比，应人工核对，不据此判断真实得失。"),
    }


def render(rep: dict) -> str:
    """纯文本形式（给 UI 兜底 / 自用脚本读）。

    ⛔ **不要接进任何 AI prompt** —— 同 `risk.render()`：个人交易数据进 prompt
    就成了个性化投资建议。
    """
    if not rep.get("available"):
        return ""
    lines = [f"· MFE/MAE（{rep['trades']} 笔已平仓）："]
    if rep.get("median_capture_rate") is not None:
        lines.append(f"  - 捕获率中位数 {rep['median_capture_rate']:.0%}"
                     f"（{rep['capture_samples']} 笔有讨论价值的行情）")
    lines.append(f"  - 盈利回吐中位数 {rep['median_give_back']:+.1f}%"
                 f"，最大浮亏中位数 {rep['median_mae']:+.1f}%")
    if not rep.get("enough_samples"):
        lines.append(f"  ⚠️ 只有 {rep['trades']} 笔，样本太少，别下结论")
    return "\n".join(lines)

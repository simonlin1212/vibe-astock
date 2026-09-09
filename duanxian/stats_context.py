"""统计语境 —— 给读数配上"这个数在历史上算什么位置"，以及"今天和昨天有什么不同" """

from __future__ import annotations

from duanxian.paths import data_path
import logging
from statistics import median
from typing import Callable, Optional

from . import trade_calendar

logger = logging.getLogger(__name__)

# 少于这么多天不给分位 —— 样本不够时"分位"是误导
_MIN_SAMPLES = 10
# 到这个分位就值得标出来（两头都算异常）
_HIGH = 0.85
_LOW = 0.15


class Reading:
    """一个可做历史对比的市场读数。

    `higher_is_hotter` 决定"高分位"怎么解读：涨停家数高=情绪热，
    炸板率高=情绪冷。分位本身只是位置，语义要靠它翻译。
    `kind` 决定怎么显示：比率要转成百分数（0.2156… 直接摆出来没人看）。
    """

    def __init__(self, key: str, label: str, unit: str,
                 getter: Callable[[dict], Optional[float]],
                 higher_is_hotter: bool = True, diff_eps: float = 0.0,
                 kind: str = "count"):
        self.key, self.label, self.unit = key, label, unit
        self.getter = getter
        self.higher_is_hotter = higher_is_hotter
        # diff 视图的"非平凡变化"阈值 —— 没有它的话每天都是几十条噪声
        self.diff_eps = diff_eps
        self.kind = kind          # count / rate / pct

    def fmt(self, v: Optional[float]) -> str:
        """把读数格式化成人看的样子。"""
        if v is None:
            return "—"
        if self.kind == "rate":
            return f"{v:.0%}"
        if self.kind == "pct":
            return f"{v:+.2f}%"
        return f"{v:g}{self.unit}"


def _pool_median_ret(rows: list[dict]) -> Optional[float]:
    vals = [r["ret"] for r in rows if r.get("ret") is not None]
    return round(median(vals), 2) if vals else None


def _pool_deep_loss(rows: list[dict]) -> Optional[float]:
    vals = [r["ret"] for r in rows if r.get("ret") is not None]
    return float(sum(1 for v in vals if v <= -5)) if vals else None


def _pool_promotion(rows: list[dict]) -> Optional[float]:
    """昨日首板今日仍涨停的比例（1进2）—— 最敏感的一档。"""
    from .data import is_limit_up

    firsts = [r for r in rows if int(r.get("prev_boards") or 0) == 1]
    if not firsts:
        return None
    judged = [is_limit_up(r) for r in firsts]
    ok = [j for j in judged if j is not None]
    return round(sum(1 for j in ok if j) / len(ok), 3) if ok else None


# 每日读数的定义。`day` 是 {"summary": …, "pool": […]} 的组合。
READINGS: list[Reading] = [
    Reading("limit_up", "涨停家数", "家",
            lambda d: (d["summary"] or {}).get("limit_up"), diff_eps=8),
    Reading("highest_board", "最高连板", "板",
            lambda d: (d["summary"] or {}).get("highest_consec"), diff_eps=0.5),
    Reading("broken_rate", "炸板率", "",
            lambda d: (d["summary"] or {}).get("broken_rate"),
            higher_is_hotter=False, diff_eps=0.08, kind="rate"),
    Reading("money_effect", "赚钱效应中位数", "%",
            lambda d: _pool_median_ret(d["pool"] or []), diff_eps=1.0, kind="pct"),
    Reading("deep_loss", "跌超5%家数", "家",
            lambda d: _pool_deep_loss(d["pool"] or []),
            higher_is_hotter=False, diff_eps=8),
    Reading("promotion_1to2", "1进2 晋级率", "",
            lambda d: _pool_promotion(d["pool"] or []), diff_eps=0.08, kind="rate"),
]

_BY_KEY = {r.key: r for r in READINGS}


_CACHE_DIRS = ("zt_summary", "prev_pool")


def _cached_days_per_dir() -> tuple[frozenset[str], ...]:
    """两份缓存目录**各自**囤了哪些交易日。顺序同 `_CACHE_DIRS`。"""
    import os

    out = []
    for name in _CACHE_DIRS:
        d = data_path("cache", name)
        try:
            out.append(frozenset(f[:-5] for f in os.listdir(d) if f.endswith(".json")))
        except FileNotFoundError:
            out.append(frozenset())
    return tuple(out)


def _cached_days() -> set[str]:
    """磁盘上已囤了哪些交易日的原料（两份缓存目录的并集）。

    历史序列只用这些日子 —— 没囤的日子数据源已经取不到了，去请求纯属白等。
    """
    return set().union(*_cached_days_per_dir())


def _day_data(date: str) -> dict:
    """某日的原料（都走已有缓存，历史日零网络）。"""
    from . import emotion_metrics as em
    from .data import fetch_prev_pool

    return {"date": date, "summary": em.day_summary(date), "pool": fetch_prev_pool(date)}


_SERIES_CACHE: dict[tuple[int, str], tuple[list[dict], tuple]] = {}
_SERIES_CACHE_MAX = 8


def _file_fingerprint(dir_name: str, day: str) -> tuple:
    """某天那个缓存文件的指纹 `(mtime_ns, size)`。**这是测试用的接缝，测试 patch 它。**"""
    import os
    import time

    path = data_path("cache", dir_name, f"{day}.json")
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError as exc:
        # 每次调用都不同 → 签名必不相等 → 强制重算，同时出声
        logger.warning("取不到缓存文件指纹（%s）：%s: %s —— 该窗口这轮不走缓存",
                       path, type(exc).__name__, exc)
        return ("stat-failed", time.monotonic_ns())


def _window_state(window: list[str]) -> tuple:
    """窗口内原料的状态签名：每个缓存目录里、窗口内每个文件的 `(日期, mtime, size)`"""
    win = set(window)
    out = []
    for name, days in zip(_CACHE_DIRS, _cached_days_per_dir()):
        out.append(tuple((day, *_file_fingerprint(name, day)) for day in sorted(days & win)))
    return tuple(out)


def _days_in_state(state: tuple) -> set[str]:
    """从签名里还原"窗口内哪些天有原料"，避免为此再扫一次磁盘。"""
    return {row[0] for per_dir in state for row in per_dir}


def series(days: int = 30, end: Optional[str] = None) -> list[dict]:
    """最近 N 个交易日的读数序列（升序）。取不到的日子直接跳过。"""
    end = end or trade_calendar.latest_session()
    if not end:
        return []
    key = (days, end)
    settled = trade_calendar.is_settled(end)
    window = trade_calendar.trade_dates_ending_at(end, days)
    state = _window_state(window)
    if settled and key in _SERIES_CACHE:
        rows, cached_state = _SERIES_CACHE[key]
        # 原料状态没变 → 结果必然一样，直接用（那 84 秒的优化就靠这条）。
        # 变了（补上了 / 少了 / 某目录后到）→ 必须重算。
        if cached_state == state:
            return rows

    on_disk = _days_in_state(state)
    dates = [d for d in window if d in on_disk]
    out = []
    for d in dates:
        data = _day_data(d)
        if data["summary"] is None and not data["pool"]:
            continue           # 那天两份缓存都没有 → 跳过，不补零
        row = {"date": d}
        for r in READINGS:
            row[r.key] = r.getter(data)
        out.append(row)
    if settled:
        if len(_SERIES_CACHE) >= _SERIES_CACHE_MAX:
            _SERIES_CACHE.pop(next(iter(_SERIES_CACHE)), None)
        _SERIES_CACHE[key] = (out, state)
    return out


def trend(days: int = 10, end: Optional[str] = None) -> dict:
    """最近 N 个交易日的多指标趋势 —— 给前端画连续曲线用"""
    rows = series(max(days, _MIN_SAMPLES), end=end)
    if not rows:
        return {"available": False, "reason": "历史读数序列为空（缓存还没囤起来）"}
    rows = rows[-days:]
    return {
        "available": True,
        "days": [r["date"] for r in rows],
        "metrics": [
            {
                "key": r.key, "label": r.label, "unit": r.unit, "kind": r.kind,
                "higher_is_hotter": r.higher_is_hotter,
                "values": [row.get(r.key) for row in rows],
            }
            for r in READINGS
        ],
        "sample_days": len(rows),
    }


def percentile(value: Optional[float], hist: list[float]) -> Optional[float]:
    """value 在 hist 里的分位（0~1）。样本不足或值缺失返回 None。

    用"小于等于它的比例"定义 —— 直观且不需要插值。
    """
    if value is None:
        return None
    vals = [v for v in hist if v is not None]
    if len(vals) < _MIN_SAMPLES:
        return None
    return round(sum(1 for v in vals if v <= value) / len(vals), 3)


def context_for(date: Optional[str] = None, days: int = 30) -> dict:
    """今日各读数 + 历史分位 + 异常标记"""
    date = date or trade_calendar.latest_session()
    if not date:
        return {"available": False, "reason": "取不到最近已收盘交易日"}
    hist = series(days, end=date)
    if not hist:
        return {"available": False, "reason": "历史读数序列为空（缓存还没囤起来）"}

    today = hist[-1]
    if today["date"] != date:
        return {"available": False, "reason": f"{date} 无缓存读数（当天数据还没落盘）"}
    past = hist[:-1]          # 分位对比用**历史**，不含今天自己

    items = []
    for r in READINGS:
        cur = today.get(r.key)
        pct = percentile(cur, [h.get(r.key) for h in past])
        extreme = None
        if pct is not None:
            if pct >= _HIGH:
                extreme = "偏热" if r.higher_is_hotter else "偏冷"
            elif pct <= _LOW:
                extreme = "偏冷" if r.higher_is_hotter else "偏热"
        items.append({
            "key": r.key, "label": r.label, "unit": r.unit,
            "value": cur, "value_text": r.fmt(cur),
            "percentile": pct, "extreme": extreme, "kind": r.kind,
            "higher_is_hotter": r.higher_is_hotter,
            # 给个直观参照：历史区间与中位
            "hist_min": min((h[r.key] for h in past if h.get(r.key) is not None), default=None),
            "hist_max": max((h[r.key] for h in past if h.get(r.key) is not None), default=None),
            "hist_median": (round(median([h[r.key] for h in past
                                          if h.get(r.key) is not None]), 2)
                            if any(h.get(r.key) is not None for h in past) else None),
        })
    return {
        "available": True,
        "date": date,
        "sample_days": len(past),
        "min_samples": _MIN_SAMPLES,
        "items": items,
        # 有几个读数处在两头 —— 都不极端说明今天是平淡的一天，本身也是信息
        "extreme_count": sum(1 for it in items if it["extreme"]),
    }


def diff(date: Optional[str] = None, days: int = 30) -> dict:
    """今日 vs 昨日：只列**非平凡变化**。

    全景看板给"今天全貌"，但复盘时心里问的是"今天和昨天有什么不同"。
    阈值（`Reading.diff_eps`）是必需的 —— 没有它，涨停 40→41 也会被列成"变化"，
    每天几十条噪声等于没有 diff。
    """
    date = date or trade_calendar.latest_session()
    if not date:
        return {"available": False, "reason": "取不到最近已收盘交易日"}
    hist = series(days, end=date)
    if len(hist) < 2:
        return {"available": False, "reason": "历史不足两天，无法比较"}
    today, prev = hist[-1], hist[-2]
    if today["date"] != date:
        return {"available": False, "reason": f"{date} 无缓存读数"}

    changes = []
    for r in READINGS:
        a, b = prev.get(r.key), today.get(r.key)
        if a is None or b is None:
            continue
        delta = b - a
        if abs(delta) <= r.diff_eps:
            continue           # 变化在噪声阈值内 → 不算"不同"
        changes.append({
            "key": r.key, "label": r.label, "unit": r.unit,
            "prev": a, "cur": b, "delta": round(delta, 2),
            "prev_text": r.fmt(a), "cur_text": r.fmt(b), "kind": r.kind,
            # 情绪是变热还是变冷 —— 由 higher_is_hotter 翻译方向
            "hotter": (delta > 0) == r.higher_is_hotter,
        })
    # 变动幅度相对各自阈值的倍数排序 —— 让"最不寻常的变化"排在最前
    changes.sort(key=lambda c: -abs(c["delta"]) / max(_BY_KEY[c["key"]].diff_eps, 1e-9))
    return {
        "available": True,
        "date": date,
        "prev_date": prev["date"],
        "changes": changes,
        "unchanged_count": len(READINGS) - len(changes),
    }


def render(ctx: dict, df: dict) -> str:
    """分位 + diff → 分析师 prompt 能吃的文本。"""
    lines = []
    if ctx.get("available"):
        lines.append(f"[历史统计位置｜对比近 {ctx['sample_days']} 个交易日]")
        for it in ctx["items"]:
            if it["value"] is None:
                continue
            r = _BY_KEY[it["key"]]
            seg = f"· {it['label']}：{r.fmt(it['value'])}"
            if it["percentile"] is not None:
                seg += f"（近 {ctx['sample_days']} 日 {it['percentile']:.0%} 分位"
                if it["hist_median"] is not None:
                    seg += f"，历史中位 {r.fmt(it['hist_median'])}"
                seg += "）"
                if it["extreme"]:
                    seg += f" ← **{it['extreme']}**"
            else:
                seg += f"（样本不足 {ctx['min_samples']} 天，暂不给分位）"
            lines.append(seg)
    elif ctx:
        lines.append(f"[历史统计位置：不可用（{ctx.get('reason', '未知')}）]")

    if df.get("available"):
        lines.append(f"[今日 vs {df['prev_date']}｜只列非平凡变化]")
        if not df["changes"]:
            lines.append("· 六项关键读数全部在噪声阈值内 —— 今天与昨天没有实质变化")
        for c in df["changes"]:
            r = _BY_KEY[c["key"]]
            arrow = "↑" if c["delta"] > 0 else "↓"
            lines.append(f"· {c['label']}：{r.fmt(c['prev'])} → {r.fmt(c['cur'])} "
                         f"（{arrow}，{'转热' if c['hotter'] else '转冷'}）")
    return "\n".join(lines)

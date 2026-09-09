"""结构漂移检测 —— "图上这个断点，是市场变了还是规则变了"。

## 为什么这是十年体系的必需品

所有历史统计都隐含一个假设：**过去和现在是同一个游戏**。这个假设经常不成立：

1. **制度变化** —— 注册制改革、涨跌幅从 10% 改 20%、920 号段启用、
   连板计数口径调整…… 之后"涨停家数"这个数根本不是同一个东西了。
2. **数据源变化** —— 问财改口径、akshare 换字段、三池发布时间调整。
   派生曲线上会冒出一个断点，**它跟市场毫无关系**。
3. **市场结构变化** —— 20cm 标的占比上升、题材集中度长期抬升。这是真的市场变化，
   但也会让"跟三年前比"变得没意义。

不区分这三类，就会把①②读成③，然后基于一个假信号调整打法。

## 本模块做什么

- **字段漂移**（数据源层）：直接用 `archive.field_drift()`。字段清单变过 = 数据源改了。
- **结构基线**（市场层）：用归档算出各板块占比、20cm 占比、涨停家数分布，
  比较**最近窗口 vs 之前窗口**，把明显移位的项列出来。
- **制度日历**（人工登记）：已知的制度变化日期，用户/维护者手工记。
  ⚠️ **不自动推断制度变化** —— 从数据反推"这天大概改了规则"是猜，
  猜错会把市场波动当成制度事件。宁可留空。

## ⚠️ 只报"这里不连续"，不报"所以该怎么改打法"

漂移检测的产出是**一句警告**：跨这个日期的统计要小心。怎么处理是用户的事。

## 合规

只统计公开盘面数据的结构变化，不涉及任何证券的分析或建议。
"""

from __future__ import annotations

from duanxian.paths import data_path
import json
import os
from statistics import median
from typing import Optional

from .util import atomic_write_json

_DIR = data_path("drift")
_CAL_PATH = os.path.join(_DIR, "regime_calendar.json")
_CAL_SCHEMA = 1

# 归档里要跟踪的数据集
_SLUGS = ("zt_pool", "zb_pool", "dt_pool", "prev_pool", "zt_reasons")

# 最近窗口 / 之前窗口各取多少个交易日
_RECENT = 10
_PRIOR = 20

# 比较两个窗口时，相对变化超过这个比例才算"移位"（宁可少报不多报）
_SHIFT_RATIO = 0.35
# 占比类指标（0~1）用绝对变化，避免小基数被放大
_SHIFT_ABS = 0.10

# 两个窗口各自至少要有这么多天，否则不做结构比较
_MIN_DAYS = 5


# ---------------------------------------------------------------- 制度日历
def load_calendar() -> list[dict]:
    """已登记的制度变化。**只读用户手工登记的，不自动推断。**"""
    if not os.path.isfile(_CAL_PATH):
        return []
    try:
        with open(_CAL_PATH, encoding="utf-8") as fh:
            env = json.load(fh)
        if env.get("schema") == _CAL_SCHEMA and isinstance(env.get("events"), list):
            return sorted(env["events"], key=lambda e: e.get("date") or "")
    except Exception:  # noqa: BLE001  坏了当没登记
        pass
    return []


def save_calendar(events: list[dict]) -> dict:
    """登记制度变化。每条 `{date, title, note?}`。"""
    clean = []
    for e in events or []:
        d = str((e or {}).get("date") or "").strip()
        title = str((e or {}).get("title") or "").strip()
        if not d or not title:
            raise ValueError("每条制度事件都要有日期和标题")
        clean.append({"date": _valid_date(d), "title": title[:120],
                      "note": str((e or {}).get("note") or "").strip()[:500]})
    os.makedirs(_DIR, exist_ok=True)
    if not atomic_write_json(_CAL_PATH, {"schema": _CAL_SCHEMA, "events": clean}):
        raise RuntimeError("制度日历写入失败")
    return {"ok": True, "count": len(clean)}


def _valid_date(raw: str) -> str:
    """制度事件的日期校验：格式 + 不许未来。**允许周末。**

    ⚠️ 这里**故意不用 `util.validate_trade_date`** —— 那个是给"数据查询与文件名"
    准备的交易日闸门，会拒掉周末。但制度变化**经常周末公布、周一实施**
    （证监会/交易所的规则通知多是这样），用交易日闸门会让这类事件根本记不进来。
    本日历不参与任何文件名或按交易日的数据查询，所以不需要那层约束。
    """
    import datetime

    try:
        d = datetime.datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期需为 YYYY-MM-DD，得到 {raw!r}") from exc
    from .util import china_today

    if raw > china_today():
        raise ValueError(f"{raw} 是未来日期")
    return d.isoformat()


# ---------------------------------------------------------------- 结构基线
def _day_structure(date: str) -> Optional[dict]:
    """某天的结构读数（全部走归档，零网络）。"""
    from . import archive, market_facts as mf

    env = archive.get(date, "zt_pool")
    if env is None:
        return None
    rows = env.get("rows") or []
    if not rows:
        return None
    boards: dict[str, int] = {}
    for r in rows:
        # 归档里的 `board` 已经是 market_facts 归一化过的（10cm / 20cm / 北交所…）。
        # ⚠️ 优先用它而不是重算 —— 重算会用**今天的** board_of 规则去套历史数据，
        # 那正是本模块要检测的那种"用新规则解释旧数据"的错误。
        # 只有老归档缺这个字段时才回退重算，并如实标出。
        b = r.get("board") or mf.board_of(str(r.get("code") or ""),
                                          str(r.get("name") or ""))
        boards[b] = boards.get(b, 0) + 1
    n = len(rows)
    return {
        "date": date,
        "limit_up": n,
        # 各板块占比 —— 20cm 占比长期抬升是真实的市场结构变化
        "share_20cm": round((boards.get("20cm", 0) + boards.get("20cm(ST)", 0)) / n, 4),
        "share_10cm": round((boards.get("10cm", 0) + boards.get("主板ST", 0)) / n, 4),
        "share_bj": round(boards.get("北交所", 0) / n, 4),
    }


_METRIC_LABELS = {
    "limit_up": ("涨停家数", "count"),
    "share_20cm": ("20cm 标的占比", "share"),
    "share_10cm": ("10cm 标的占比", "share"),
    "share_bj": ("北交所占比", "share"),
}


def structure_shift() -> dict:
    """最近窗口 vs 之前窗口的结构比较。"""
    from . import archive

    days = archive.days("zt_pool")
    if len(days) < _MIN_DAYS * 2:
        return {"available": False,
                "reason": (f"归档只有 {len(days)} 天，做结构比较至少要 "
                           f"{_MIN_DAYS * 2} 天（每个窗口 {_MIN_DAYS} 天）")}
    recent_days = days[-_RECENT:]
    prior_days = days[-(_RECENT + _PRIOR):-_RECENT]
    if len(prior_days) < _MIN_DAYS:
        return {"available": False,
                "reason": f"之前窗口只有 {len(prior_days)} 天，不足 {_MIN_DAYS} 天"}

    def collect(ds: list[str]) -> dict[str, list[float]]:
        acc: dict[str, list[float]] = {k: [] for k in _METRIC_LABELS}
        for d in ds:
            row = _day_structure(d)
            if row is None:
                continue
            for k in _METRIC_LABELS:
                v = row.get(k)
                if v is not None:
                    acc[k].append(float(v))
        return acc

    ra, pa = collect(recent_days), collect(prior_days)
    shifts = []
    for k, (label, kind) in _METRIC_LABELS.items():
        if len(ra[k]) < _MIN_DAYS or len(pa[k]) < _MIN_DAYS:
            continue
        # ⚠️ 一律用中位数：少数极端日（大面日/涨停潮）会把均值拉飞，
        # 那看起来就像"结构变了"，其实只是有一天很极端。
        r, pv = median(ra[k]), median(pa[k])
        if kind == "share":
            moved = abs(r - pv) >= _SHIFT_ABS
            rel = None
        else:
            rel = (r - pv) / pv if pv else None
            moved = rel is not None and abs(rel) >= _SHIFT_RATIO
        shifts.append({
            "metric": k, "label": label, "kind": kind,
            "recent": round(r, 4), "prior": round(pv, 4),
            "delta": round(r - pv, 4),
            "rel_change": None if rel is None else round(rel, 3),
            "shifted": bool(moved),
        })
    moved = [s for s in shifts if s["shifted"]]
    return {
        "available": True,
        "recent_window": {"days": len(recent_days),
                          "from": recent_days[0], "to": recent_days[-1]},
        "prior_window": {"days": len(prior_days),
                         "from": prior_days[0], "to": prior_days[-1]},
        "metrics": shifts,
        "shifted": moved,
        "shifted_count": len(moved),
        "thresholds": {"share_abs": _SHIFT_ABS, "count_rel": _SHIFT_RATIO},
        "note": ("结构移位**不等于**制度变化 —— 也可能是数据源改口径（看字段漂移）"
                 "或就是真的市场变了。三类要分开看，别把前两类读成第三类。"),
    }


def report() -> dict:
    """漂移总览：数据源字段 + 市场结构 + 已登记的制度事件。"""
    from . import archive

    drift = {}
    for slug in _SLUGS:
        d = archive.field_drift(slug)
        if d.get("days"):
            drift[slug] = d
    changed = [k for k, v in drift.items() if v.get("changed")]
    struct = structure_shift()
    cal = load_calendar()
    return {
        "available": bool(drift) or struct.get("available"),
        # ① 数据源层：字段清单变过 = 源改了，与市场无关
        "field_drift": drift,
        "field_changed": changed,
        # ② 市场层：最近 vs 之前的结构比较
        "structure": struct,
        # ③ 制度层：**只有人工登记的**，绝不从数据反推
        "regime_events": cal,
        "regime_note": ("制度变化只收**人工登记**的条目。从数据反推「这天大概改了规则」"
                        "是猜，猜错会把市场波动当成制度事件 —— 宁可留空。"),
        "summary": _summarize(changed, struct, cal),
    }


def _summarize(field_changed: list[str], struct: dict, cal: list[dict]) -> str:
    bits = []
    if field_changed:
        bits.append(f"{len(field_changed)} 类数据的字段清单变过（{'、'.join(field_changed)}）"
                    "—— 跨那些日期的统计里可能有一个与市场无关的断点")
    if struct.get("available"):
        n = struct.get("shifted_count") or 0
        bits.append(f"结构比较：{n} 项明显移位" if n else "结构比较：没有明显移位")
    else:
        bits.append(f"结构比较暂不可用（{struct.get('reason')}）")
    if cal:
        bits.append(f"已登记 {len(cal)} 条制度事件")
    else:
        bits.append("尚未登记任何制度事件（需要手工登记，系统不自动推断）")
    return "；".join(bits) + "。"

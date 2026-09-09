"""派生情绪指标 —— 短线复盘真正的命根子"""

from __future__ import annotations
from duanxian.paths import data_path
from .cache_policy import fresh as cache_fresh, write as write_cache

import json
import os
import urllib.request
from statistics import mean, median
from typing import Optional

from . import trade_calendar
from .util import atomic_write_json

from . import fetchers as dr

_TENCENT = "http://qt.gtimg.cn/q="
_BATCH = 50          # 腾讯批量行情单次上限，取保守值
_F_PCT = 32


def _tx_symbol(code: str) -> str:
    """6 位代码 → 腾讯符号。北交所 8/4 开头走 bj，6 开头 sh，其余 sz。"""
    code = str(code).zfill(6)
    if code[0] in ("8", "4"):
        return f"bj{code}"
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def batch_pct(codes: list[str], *, opening_date: str | None = None) -> dict[str, float]:
    """批量取当前涨跌幅 {code: pct}。单批失败只丢该批，不影响其它批。"""
    out: dict[str, float] = {}
    uniq = list(dict.fromkeys(str(c).zfill(6) for c in codes if c))
    for i in range(0, len(uniq), _BATCH):
        chunk = uniq[i : i + _BATCH]
        try:
            url = _TENCENT + ",".join(_tx_symbol(c) for c in chunk)
            raw = urllib.request.urlopen(url, timeout=10).read().decode("gbk", "ignore")
        except Exception:  # noqa: BLE001  单批失败不拖累整体
            continue
        for line in raw.split(";"):
            if "~" not in line:
                continue
            f = line.split("~")
            if len(f) <= _F_PCT:
                continue
            code = f[0].split("=")[0].strip().strip('"')[-6:]
            try:
                if opening_date:
                    import math
                    stamp = f[30]
                    opened, previous = float(f[5]), float(f[4])
                    if (stamp[:8] != opening_date.replace("-", "") or stamp[8:12] < "0925"
                            or not math.isfinite(opened) or not math.isfinite(previous)
                            or opened <= 0 or previous <= 0):
                        continue
                    out[code] = (opened / previous - 1) * 100
                else:
                    value = float(f[_F_PCT])
                    if __import__("math").isfinite(value):
                        out[code] = value
            except (ValueError, IndexError):
                continue
    return out


_POOL_CACHE: dict[str, dict] = {}
_POOL_TIMES: dict[str, float] = {}
_POOL_CACHE_MAX = 240   # 约一年交易日；上界防常驻进程无限增长


def _zt_pool(date: str) -> Optional[dict]:
    """取某日涨停池；失败或标了 error_zt 一律返回 None（不把失败伪装成 0 家）。"""
    import time
    settled = trade_calendar.is_settled(date)
    if settled and date in _POOL_CACHE and time.monotonic() - _POOL_TIMES.get(date, 0) < 300:
        return _POOL_CACHE[date]
    try:
        zt = dr.fetch_zt_pool(date.replace("-", ""))
    except Exception:  # noqa: BLE001
        return None
    if not zt or zt.get("error_zt") or zt.get("zt") is None or len(zt["zt"]) == 0:
        return None
    if settled:
        if len(_POOL_CACHE) >= _POOL_CACHE_MAX:
            _POOL_CACHE.pop(next(iter(_POOL_CACHE)), None)   # 简单 FIFO 淘汰
        _POOL_CACHE[date] = zt
        _POOL_TIMES[date] = time.monotonic()
        for key in list(_POOL_TIMES):
            if key not in _POOL_CACHE: _POOL_TIMES.pop(key, None)
    return zt


def _ladder_by_boards(zt: dict) -> list[dict]:
    """涨停池的连板梯队，每项 {code, name, consec_boards}。"""
    return [
        {"code": str(x.get("code", "")).zfill(6),
         "name": x.get("name", ""),
         "boards": int(x.get("consec_boards", 1) or 1)}
        for x in (zt.get("ladder") or [])
        if x.get("code")
    ]


def _pool_codes(zt: dict) -> set[str]:
    """涨停池全部代码集合（用于判断"今日是否仍涨停"）。"""
    df = zt.get("zt")
    codes: set[str] = set()
    try:
        for col in ("代码", "code", "股票代码"):
            if col in df.columns:
                codes = {str(c).zfill(6) for c in df[col].tolist()}
                break
    except Exception:  # noqa: BLE001  DataFrame 结构异常 → 退回梯队里的代码
        pass
    if not codes:
        codes = {x["code"] for x in _ladder_by_boards(zt)}
    return codes


def promotion_rates(date: str, prev: Optional[str] = None) -> dict:
    """晋级率：昨日各档连板中，今日仍涨停的比例。任意历史日可算。"""
    prev = prev or trade_calendar.prev_trade_date(date)
    if not prev:
        return {"available": False, "reason": "取不到前一交易日"}
    prev_zt, today_zt = _zt_pool(prev), _zt_pool(date)
    if prev_zt is None or today_zt is None:
        return {"available": False, "reason": f"涨停池取数失败（{prev} 或 {date}）"}

    today_codes = _pool_codes(today_zt)
    buckets = {"1进2": [], "2进3": [], "3板以上晋级": []}
    for s in _ladder_by_boards(prev_zt):
        key = "1进2" if s["boards"] == 1 else ("2进3" if s["boards"] == 2 else "3板以上晋级")
        buckets[key].append(s["code"] in today_codes)

    tiers = {
        k: {"base": len(v), "promoted": sum(v),
            "rate": round(sum(v) / len(v), 3) if v else None}
        for k, v in buckets.items()
    }
    total_base = sum(t["base"] for t in tiers.values())
    total_promoted = sum(t["promoted"] for t in tiers.values())
    return {
        "available": True,
        "prev_date": prev,
        "tiers": tiers,
        "overall": {"base": total_base, "promoted": total_promoted,
                    "rate": round(total_promoted / total_base, 3) if total_base else None},
        # 两天的涨停家数：池子已经取了，顺手带出来给情绪档位验证用，不必再查一次
        "limit_up_count": len(today_codes),
        "prev_limit_up_count": len(_pool_codes(prev_zt)),
    }


# 行情覆盖率闸门。批量行情半死不活时只回来几只票，若照样出结论，就是拿
# 3 只票的表现冒充全体赚钱效应 —— 数字看着完全正常，是最难发现的一类错。
_COVERAGE_MIN = 0.5      # 低于此：判定不可用，如实说取不到
_COVERAGE_PARTIAL = 0.9  # 低于此：可用但标 partial，prompt/UI 都要提示样本不全


def _coverage(vals: list[float], expected: int) -> dict:
    """样本覆盖情况。expected = 本该拿到的只数。"""
    rate = round(len(vals) / expected, 3) if expected else None
    return {
        "sample": len(vals),
        "expected_sample": expected,
        "coverage_rate": rate,
        "partial": bool(rate is not None and rate < _COVERAGE_PARTIAL),
    }


def _settled_pool(date: str) -> Optional[list[dict]]:
    """`date` 那一场的**定稿记录**：昨日涨停股在 `date` 当天的表现。

    每行自带 `ret`（该股在 `date` 的涨跌幅）、`prev_boards`、`close`、`limit_price`
    —— 也就是说"昨天进去的人赚不赚钱"这一整段**不需要实时行情**也算得出。

    🔴 这条路必须**优先于实时行情**：实时行情只在"目标日就是最近已收盘那一场"
       那一小段时间内可用，一旦今天开盘，它就变成今天的价、算不了昨天那一场 ——
       于是"想看 07-29 的复盘"就永远看不到了（而这是复盘系统的基本功能）。
       定稿记录对任何历史日期都取得到（已收盘的读落盘缓存，否则走东财昨日涨停池）。
    """
    from .data import fetch_prev_pool

    try:
        return fetch_prev_pool(date)
    except Exception:  # noqa: BLE001  取不到就退回实时那条路
        return None


def _stats_from_pool(rows: list[dict], today_codes: Optional[set]) -> dict:
    """从定稿记录算赚钱效应。样本就是记录本身，无所谓"覆盖率"。"""
    from .data import is_limit_up

    vals = [r["ret"] for r in rows if r.get("ret") is not None]
    if not vals:
        return {}
    again = [r for r in rows if r.get("ret") is not None and (r["code"] in today_codes if today_codes is not None else is_limit_up(r))]
    return {
        "available": True,
        "sample": len(vals),
        "coverage": len(vals),
        "coverage_rate": 1.0,
        "partial": False,
        "avg": round(mean(vals), 2),
        "median": round(median(vals), 2),
        "positive_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        # 优先和晋级率共用东财池成员；无池时只保留历史价格推定口径
        "limit_up_again_rate": round(len(again) / len(vals), 3),
        "source": "settled",
    }


def money_effect(date: str, prev: Optional[str] = None) -> dict:
    """赚钱效应：昨日涨停股在目标日的表现。

    先用**定稿记录**（任何历史日期都算得出），拿不到才退回实时行情。
    """
    prev = prev or trade_calendar.prev_trade_date(date)
    rows = _settled_pool(date)
    if rows:
        today_pool = _zt_pool(date)
        stats = _stats_from_pool(rows, _pool_codes(today_pool) if today_pool is not None else None)
        if stats:
            return {**stats, "prev_date": prev}

    ok, why = trade_calendar.live_quotes_are_close_of(date)
    if not ok:
        return {"available": False, "reason": why}
    if not prev:
        return {"available": False, "reason": "取不到前一交易日"}
    prev_zt = _zt_pool(prev)
    if prev_zt is None:
        return {"available": False, "reason": f"{prev} 涨停池取数失败"}

    codes = list(_pool_codes(prev_zt))
    if not codes:
        return {"available": False, "reason": f"{prev} 涨停池为空"}
    today_zt = _zt_pool(date)
    today_codes = _pool_codes(today_zt) if today_zt is not None else None
    pct = batch_pct(codes)
    vals = [v for v in pct.values() if v is not None]
    cov = _coverage(vals, len(codes))
    if not vals:
        return {"available": False, "reason": "批量行情全部取数失败", **cov}
    if cov["coverage_rate"] is not None and cov["coverage_rate"] < _COVERAGE_MIN:
        return {"available": False,
                "reason": f"批量行情只取到 {len(vals)}/{len(codes)} 只"
                          f"（{cov['coverage_rate']:.0%}），样本不足以代表全体",
                **cov}
    return {
        "available": True,
        "prev_date": prev,
        **cov,
        "avg": round(mean(vals), 2),
        "median": round(median(vals), 2),
        "positive_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        # 涨停池取不到时宁可给 None 也不用涨幅阈值糊弄（阈值会高估，见上）
        "limit_up_again_rate": (
            round(sum(1 for c in pct if c in today_codes) / len(vals), 3)
            if today_codes is not None else None
        ),
    }


def consec_premium(date: str, prev: Optional[str] = None) -> dict:
    """连板溢价：昨日 2 板以上个股在目标日的平均涨幅；只描述样本表现，不单独判断承接。

    同 `money_effect`：定稿记录优先，实时行情兜底。
    """
    prev = prev or trade_calendar.prev_trade_date(date)
    rows = _settled_pool(date)
    if rows:
        hi = [r for r in rows
              if (r.get("prev_boards") or 0) >= 2 and r.get("ret") is not None]
        if hi:
            vals = [r["ret"] for r in hi]
            return {"available": True, "prev_date": prev, "source": "settled",
                    "sample": len(vals), "coverage": len(vals),
                    "coverage_rate": 1.0, "partial": False,
                    "avg": round(mean(vals), 2), "median": round(median(vals), 2),
                    "positive_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3)}

    ok, why = trade_calendar.live_quotes_are_close_of(date)
    if not ok:
        return {"available": False, "reason": why}
    if not prev:
        return {"available": False, "reason": "取不到前一交易日"}
    prev_zt = _zt_pool(prev)
    if prev_zt is None:
        return {"available": False, "reason": f"{prev} 涨停池取数失败"}

    codes = [s["code"] for s in _ladder_by_boards(prev_zt) if s["boards"] >= 2]
    if not codes:
        return {"available": False, "reason": f"{prev} 无 2 板以上个股"}
    pct = batch_pct(codes)
    vals = [v for v in pct.values() if v is not None]
    cov = _coverage(vals, len(codes))
    if not vals:
        return {"available": False, "reason": "批量行情全部取数失败", **cov}
    if cov["coverage_rate"] is not None and cov["coverage_rate"] < _COVERAGE_MIN:
        return {"available": False,
                "reason": f"批量行情只取到 {len(vals)}/{len(codes)} 只"
                          f"（{cov['coverage_rate']:.0%}），样本不足以代表昨日连板股表现",
                **cov}
    return {
        "available": True,
        "prev_date": prev,
        **cov,
        "avg": round(mean(vals), 2),
        "median": round(median(vals), 2),
        "positive_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
    }


def ladder_gap(date: str) -> dict:
    """连板梯队断层：高标和低位之间有没有空档。

    例：有 5 板和 2 板、却没有 3-4 板 → 高标"悬空"，一旦断板没有下一梯队承接，
    是情绪断层的典型信号。梯队连续则说明资金在逐级接力。
    """
    zt = _zt_pool(date)
    if zt is None:
        return {"available": False, "reason": f"{date} 涨停池取数失败"}
    ladder = _ladder_by_boards(zt)
    multi = [s for s in ladder if s["boards"] >= 2]
    if not multi:
        return {"available": True, "highest": max((s["boards"] for s in ladder), default=0),
                "tiers": {}, "gaps": [], "continuous": True,
                "note": "无 2 板以上个股，梯队无从谈起"}

    counts: dict[int, int] = {}
    for s in multi:
        counts[s["boards"]] = counts.get(s["boards"], 0) + 1
    highest = max(counts)
    # 从 2 板到最高板，中间缺哪几档
    gaps = [b for b in range(2, highest) if b not in counts]
    return {
        "available": True,
        "highest": highest,
        "tiers": {str(b): counts[b] for b in sorted(counts, reverse=True)},
        "gaps": gaps,
        "continuous": not gaps,
        "note": ("全市场板位结构连续（注：跨题材，不代表同一题材内部有梯队）" if not gaps
                 else f"板位缺档 {'、'.join(f'{g}板' for g in gaps)} → 最高标下方断层，是否有同题材承接需另查，板位缺档本身不能判定"),
    }


_SUMMARY_CACHE_DIR = data_path("cache/zt_summary")

_SUMMARY_SCHEMA = 1
_SUMMARY_SOURCE = "akshare_zt_pool"


def _summarize(zt: dict) -> Optional[dict]:
    """把一天的涨停池压成三个原始读数（都不依赖行情，任意历史日可算）。"""
    df = zt.get("zt")
    if df is None or len(df) == 0:
        return None
    n_zt = int(len(df))
    n_zb = int(zt.get("zb_count", 0) or 0)
    hc = int(zt.get("highest_consec", 0) or 0)
    br = (n_zb / (n_zb + n_zt)) if (n_zb + n_zt) else None
    return {"limit_up": n_zt, "highest_consec": hc, "broken_rate": br}


def day_summary(date: str) -> Optional[dict]:
    """某日的情绪原始读数，**历史日落盘缓存**"""
    is_past = trade_calendar.is_settled(date)
    path = os.path.join(_SUMMARY_CACHE_DIR, f"{date}.json")
    if is_past and os.path.isfile(path) and cache_fresh(path, date):
        try:
            with open(path, encoding="utf-8") as fh:
                env = json.load(fh)
            # 版本 / 数据源 / 日期三者都要对得上才认。日期也校验：文件名被改或错拷时
            # 会拿别的一天的读数冒充这天，曲线照样画得出来。
            if (
                isinstance(env, dict)
                and env.get("schema") == _SUMMARY_SCHEMA
                and env.get("source") == _SUMMARY_SOURCE
                and env.get("date") == date
                and isinstance(env.get("summary"), dict)
                and env["summary"].get("limit_up", 0) > 0
            ):
                return env["summary"]
        except Exception:  # noqa: BLE001  缓存坏了就当没有，重新取
            pass

    zt = _zt_pool(date)
    s = _summarize(zt) if zt is not None else None
    if s and is_past:
        write_cache(
            path,
            {"schema": _SUMMARY_SCHEMA, "source": _SUMMARY_SOURCE, "date": date, "summary": s},
        )
    return s


def _minmax(vals: list[float]) -> list[float]:
    """窗口内归一化到 0~1；全等时一律给 0.5（避免除零，也避免假装有差异）。"""
    lo, hi = min(vals), max(vals)
    return [0.5] * len(vals) if hi == lo else [(v - lo) / (hi - lo) for v in vals]


def _recent_trend(scores: list[float], eps: float = 0.03) -> str:
    """最近的走向 —— 只看尾部斜率，和"相对窗口低点的位置"是两回事。

    "比十日最低点高" 和 "正在往上走" 完全可以同时不成立（高位连续转弱），
    所以这两件事必须分开输出，不能用一个 rising 布尔糊过去。
    """
    if len(scores) < 3:
        return "样本不足"
    d1, d2 = scores[-1] - scores[-2], scores[-2] - scores[-3]
    if d1 > eps and d2 > eps:
        return "连续两日走强"
    if d1 < -eps and d2 < -eps:
        return "连续两日转弱"
    if d1 > eps:
        return "今日走强"
    if d1 < -eps:
        return "今日转弱"
    return "基本走平"


def cycle_position(date: str, lookback: int = 10) -> dict:
    """情绪周期位置：这波情绪从哪天启动、今天是第几天"""
    dates = trade_calendar.trade_dates_ending_at(date, lookback)
    if not dates:   # 交易日历取不到时至少保住目标日，让下面按"池子天数不足"如实报
        dates = [date]
    if len(dates) < 3:
        return {"available": False, "reason": "可用交易日不足 3 天，无法定位周期"}

    series = []
    for d in dates:
        s = day_summary(d)
        if s:
            series.append({"date": d, **s})
    if len(series) < 3:
        return {"available": False, "reason": f"涨停池可用天数不足（{len(series)}/3）"}

    # 炸板率是"越高越冷"，所以取反后再平均 —— 三项加起来才都指向同一个方向。
    # 界面口径里必须说这件事，否则读者会以为炸板率高情绪分也高。
    n_zt = _minmax([float(s["limit_up"]) for s in series])
    n_hc = _minmax([float(s["highest_consec"]) for s in series])
    brs = [s["broken_rate"] for s in series]
    # 炸板率缺失的天按窗口均值补，避免整条曲线因个别天报废
    known = [b for b in brs if b is not None]
    fill = sum(known) / len(known) if known else 0.5
    n_br = _minmax([float(b if b is not None else fill) for b in brs])

    for i, s in enumerate(series):
        s["score"] = round((n_zt[i] + n_hc[i] + (1 - n_br[i])) / 3, 3)

    trough = min(series, key=lambda s: s["score"])
    trough_idx = [s["date"] for s in series].index(trough["date"])
    day_n = len(series) - trough_idx
    return {
        "available": True,
        "window": len(series),
        "trough_date": trough["date"],
        "trough_score": trough["score"],
        "current_score": series[-1]["score"],
        "day_n": day_n,
        "rising": series[-1]["score"] > trough["score"],
        "trend": _recent_trend([s["score"] for s in series]),
        "pctile": (round((series[-1]["score"] - trough["score"])
                         / (max(s["score"] for s in series) - trough["score"]), 3)
                   if max(s["score"] for s in series) > trough["score"] else None),
        "series": series,
    }


def build_metrics(date: str, with_cycle: bool = True) -> dict:
    """派生指标一起算（共用同一个前一交易日 + 涨停池缓存，少查很多次）。

    `with_cycle=False` 可跳过周期位置——它要拉 ~10 天涨停池，是这里最慢的一项。
    """
    prev = trade_calendar.prev_trade_date(date)
    out = {
        "date": date,
        "prev_date": prev,
        "money_effect": money_effect(date, prev),
        "promotion": promotion_rates(date, prev),
        "consec_premium": consec_premium(date, prev),
        "ladder_gap": ladder_gap(date),
    }
    if with_cycle:
        out["cycle"] = cycle_position(date)
    return out


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0%}"


def _cov_note(d: dict) -> str:
    """样本不全时的警示后缀。覆盖率是判断"这个数字信不信得过"的前提，
    不写进 prompt 的话，模型会把 30% 覆盖的均值当全体读数用。"""
    if not d.get("partial"):
        return ""
    exp, got = d.get("expected_sample"), d.get("sample")
    return f"｜⚠️样本不全：仅取到 {got}/{exp}，结论按部分样本看"


def render_metrics(m: dict) -> str:
    """渲染成分析师 prompt 直接能吃的文本块。不可用的部分如实说明原因。"""
    date, prev = m.get("date", ""), m.get("prev_date") or "—"
    lines = [f"[派生情绪指标 {date}｜对照前一交易日 {prev}]"]

    me = m.get("money_effect", {})
    if me.get("available"):
        lines.append(
            f"· 赚钱效应：{prev} 涨停 {me['sample']} 家，在 {date} 平均 {me['avg']:+.2f}%、"
            f"中位 {me['median']:+.2f}%；翻红率 {_pct(me['positive_rate'])}、"
            f"再度涨停 {_pct(me['limit_up_again_rate'])}" + _cov_note(me)
        )
    else:
        lines.append(f"· 赚钱效应：不可用（{me.get('reason', '未知')}）")

    pr = m.get("promotion", {})
    if pr.get("available"):
        t = pr["tiers"]
        seg = "；".join(
            f"{k} {v['promoted']}/{v['base']}（{_pct(v['rate'])}）" for k, v in t.items() if v["base"]
        ) or "无可统计档位"
        lines.append(f"· 晋级率：{seg}；整体 {_pct(pr['overall']['rate'])}")
    else:
        lines.append(f"· 晋级率：不可用（{pr.get('reason', '未知')}）")

    cp = m.get("consec_premium", {})
    if cp.get("available"):
        lines.append(
            f"· 连板溢价（承接度）：{prev} 的 {cp['sample']} 只 2 板以上个股，"
            f"在 {date} 平均 {cp['avg']:+.2f}%、中位 {cp['median']:+.2f}%，"
            f"翻红率 {_pct(cp['positive_rate'])}" + _cov_note(cp)
        )
    else:
        lines.append(f"· 连板溢价：不可用（{cp.get('reason', '未知')}）")

    lg = m.get("ladder_gap", {})
    if lg.get("available"):
        tiers = "、".join(f"{b}板×{c}" for b, c in (lg.get("tiers") or {}).items()) or "无 2 板以上"
        lines.append(f"· 梯队结构：最高 {lg.get('highest', 0)} 板；{tiers}。{lg.get('note', '')}")
    elif lg:
        lines.append(f"· 梯队结构：不可用（{lg.get('reason', '未知')}）")

    cy = m.get("cycle", {})
    if cy.get("available"):
        pctile = cy.get("pctile")
        pos = f"位于十日区间 {pctile:.0%} 分位" if isinstance(pctile, (int, float)) else "分位未知"
        lines.append(
            f"· 情绪周期（⚠️ 这是十日窗口内的**相对**读数，没有绝对含义）："
            f"窗口最低点在 {cy['trough_date']}（{cy['trough_score']}），今天距它第 {cy['day_n']} 天；"
            f"当前相对分 {cy['current_score']}，{pos}，最近走向：**{cy.get('trend', '未知')}**。"
            f"⚠️「距低点第 N 天」和「正在往上走」是两件事，别混为一谈"
        )
    elif cy:
        lines.append(f"· 情绪周期：不可用（{cy.get('reason', '未知')}）")

    return "\n".join(lines)

"""客观市场事实表 —— 复盘要回答的是"今天发生了什么"，不是"明天买什么" """

from __future__ import annotations

import os
from typing import Optional

from . import data as _data  # noqa: F401  仅为副作用：注入项目根 sys.path
from . import trade_calendar
from .util import atomic_write_json

_CACHE_DIR = os.path.expanduser("~/.duanxian-agents/cache/market_facts")

_FACTS_SCHEMA = 3
_FACTS_SCHEMA_READABLE = (2, 3)


# ---------------------------------------------------------------- 涨跌幅制度
def board_of(code: str, name: str = "") -> str:
    """这只票属于哪种涨跌幅制度"""
    code = str(code).zfill(6)
    is_st = "ST" in str(name).upper()
    if code[0] in ("8", "4") or code.startswith("920"):
        return "北交所"                       # 30cm
    if code.startswith("30") or code.startswith("688"):
        return "20cm(ST)" if is_st else "20cm"   # 创业板 / 科创板：ST 也是 20%
    return "主板ST" if is_st else "10cm"


_LIMIT_PCT = {"主板ST": 5.0, "北交所": 30.0, "20cm": 20.0, "20cm(ST)": 20.0, "10cm": 10.0}


def limit_pct(code: str, name: str = "") -> float:
    """这只票的涨停幅度（%）。别再用统一的 `ret >= 9.8`——board_of`"""
    return _LIMIT_PCT[board_of(code, name)]


def is_limit_up(ret: float, code: str, name: str = "", tol: float = 0.3) -> bool:
    """按各自的涨停幅度判是否涨停。tol 容忍四舍五入与除权误差。"""
    return ret >= limit_pct(code, name) - tol


def board_label(boards: int, zt_stat: Optional[str] = None) -> str:
    """连板标注：普通连板写「N板」；断板后反包的（东财涨停统计 N/M，N > M）写「N天M板」。

    东财「连板数」对反包票只给 1（今天重新涨停），单看它会写成「1板」，
    把「3天2板」这种回流反包结构完全抹掉 —— 所以优先用涨停统计。
    """
    if zt_stat:
        try:
            d, b = str(zt_stat).split("/")
            d, b = int(d), int(b)
            if b > 0 and d > b:
                return f"{d}天{b}板"
            if b > 0:
                return f"{b}板"
        except (ValueError, TypeError):
            pass
    return f"{int(boards or 1)}板"


def stat_boards(zt_stat: Optional[str]) -> int:
    """涨停统计「N/M」里的 M（有效高度）。取不到给 0。"""
    try:
        return int(str(zt_stat).split("/")[1])
    except (ValueError, TypeError, IndexError, AttributeError):
        return 0


# ---------------------------------------------------------------- 三池明细
def _df_rows(df, mapping: dict[str, str]) -> list[dict]:
    """DataFrame → list[dict]，按 mapping 取列（缺列给 None，不让整表报废）。"""
    if df is None or not len(df):
        return []
    out = []
    for _, r in df.iterrows():
        row = {}
        for dst, src in mapping.items():
            v = r.get(src)
            row[dst] = None if v is None or (isinstance(v, float) and v != v) else v
        code = str(row.get("code") or "").zfill(6)
        if not code or code == "000000":
            continue
        row["code"] = code
        row["name"] = str(row.get("name") or "")
        row["board"] = board_of(code, row["name"])
        out.append(row)
    return out


_ZT_MAP = {
    "code": "代码", "name": "名称", "ret": "涨跌幅", "amount": "成交额",
    "turnover": "换手率", "seal_fund": "封板资金", "first_seal": "首次封板时间",
    "last_seal": "最后封板时间", "broken_times": "炸板次数", "boards": "连板数",
    "zt_stat": "涨停统计", "sector": "所属行业",
}
_ZB_MAP = {
    "code": "代码", "name": "名称", "ret": "涨跌幅", "turnover": "换手率",
    "first_seal": "首次封板时间", "broken_times": "炸板次数",
    "amplitude": "振幅", "sector": "所属行业",
}
_DT_MAP = {
    "code": "代码", "name": "名称", "ret": "涨跌幅", "seal_fund": "封单资金",
    "last_seal": "最后封板时间", "consec_dt": "连续跌停",
    "open_times": "开板次数", "sector": "所属行业",
}


def _raw_rows(df) -> list[dict]:
    """DataFrame → list[dict]，**一列都不丢、一个名都不改**"""
    if df is None or not len(df):
        return []
    out = []
    for _, r in df.iterrows():
        row = {}
        for k in df.columns:
            v = r.get(k)
            # NaN 不能进 JSON；其余一律原样（含源自己的列名）
            row[str(k)] = None if v is None or (isinstance(v, float) and v != v) else (
                v if isinstance(v, (str, int, float, bool)) else str(v))
        out.append(row)
    return out


def pools(date: str) -> Optional[dict]:
    """某日三池的**完整明细**。取数失败返回 None（不把失败伪装成空表）。

    历史日落盘缓存 —— 事实不会变，且这三次请求是本模块所有派生表的共同上游。

    返回的 dict 里除了归一化的 `zt/zb/dt`，还带 **`raw`**（源的原样行）供归档用 ——
    这样归档拿到真正的原始数据，而**不额外发一次网络请求**。
    """
    path = os.path.join(_CACHE_DIR, f"{date}.json")
    settled = trade_calendar.is_settled(date)
    if settled and os.path.isfile(path):
        try:
            import json

            with open(path, encoding="utf-8") as fh:
                env = json.load(fh)
            if (isinstance(env, dict) and env.get("schema") in _FACTS_SCHEMA_READABLE
                    and env.get("date") == date and isinstance(env.get("pools"), dict)):
                return env["pools"]
        except Exception:  # noqa: BLE001  缓存坏了当没有
            pass

    try:
        import akshare as ak

        d = date.replace("-", "")
        zt_df = ak.stock_zt_pool_em(date=d)
        zt = _df_rows(zt_df, _ZT_MAP)
        if not zt:
            return None       # 涨停池空 = 非交易日或数据未更新，别继续
        zb_df = ak.stock_zt_pool_zbgc_em(date=d)
        dt_df = ak.stock_zt_pool_dtgc_em(date=d)
        zb = _df_rows(zb_df, _ZB_MAP)
        dt = _df_rows(dt_df, _DT_MAP)
        raw = {"zt": _raw_rows(zt_df), "zb": _raw_rows(zb_df), "dt": _raw_rows(dt_df)}
    except Exception:  # noqa: BLE001
        return None

    out = {"zt": zt, "zb": zb, "dt": dt, "raw": raw}
    if settled:
        atomic_write_json(path, {"schema": _FACTS_SCHEMA, "date": date, "pools": out})
    return out


# ---------------------------------------------------------------- 封板质量
def _hhmmss(t) -> str:
    return str(t or "").strip()


def seal_quality(date: str) -> dict:
    """今日涨停股的**封板质量**：什么时候封的、封稳了没、炸过几次。

    "涨停 40 家"说不出这 40 个板的成色。同样是涨停，开盘秒板且没炸过，
    和炸了 6 次尾盘才回封，是完全不同的两件事。
    """
    p = pools(date)
    if p is None:
        return {"available": False, "reason": f"{date} 涨停池取数失败"}
    zt = p["zt"]
    if not zt:
        return {"available": False, "reason": f"{date} 涨停池为空"}

    zb = p["zb"]
    never_broken = [r for r in zt if not (r.get("broken_times") or 0)]
    seconds = [r for r in zt if _hhmmss(r.get("first_seal")) and _hhmmss(r["first_seal"]) <= "093500"]
    late = [r for r in zt if _hhmmss(r.get("last_seal")) and _hhmmss(r["last_seal"]) >= "143000"]
    broken_pool = [r for r in zt if (r.get("broken_times") or 0) >= 1]
    return {
        "available": True,
        "total": len(zt),
        # 一次没炸过 = 板最硬；炸过又回封的越多，说明分歧越大
        "never_broken": len(never_broken),
        "never_broken_rate": round(len(never_broken) / len(zt), 3),
        "opening_seconds": len(seconds),      # 开盘 5 分钟内首封
        "late_seal": len(late),               # 14:30 后才最终封住（含反复炸板后回封）
        "reopened": len(broken_pool),         # 炸过至少一次但最终封住
        "avg_broken_times": round(
            sum((r.get("broken_times") or 0) for r in zt) / len(zt), 2),
        "broken_pool": len(zb),
        "broken_rate": (round(len(zb) / (len(zt) + len(zb)), 3)
                        if (len(zt) + len(zb)) else None),
        # 保留这个但改名说清含义，避免再被当成炸板率用
        "ever_opened_rate": round(len(broken_pool) / len(zt), 3),
    }


def _settled_rows(date: str) -> Optional[list[dict]]:
    """`date` 那一场的定稿记录（昨日涨停股在 `date` 的表现）。

    🔴 这条路必须优先于实时行情：实时行情只在"目标日就是最近已收盘那一场"的
       那一小段时间内可用，一旦今天开盘就变成今天的价、算不了昨天那一场 ——
       于是"想看 07-29 的复盘"永远看不到，而这是复盘系统的基本功能。
       详见 emotion_metrics._settled_pool。

    ⚠️ 定稿记录只覆盖**昨日涨停**那批，不含昨日炸板股 ——
       用它算的块要如实说明少了炸板那一档，别默默当成 0。
    """
    from .data import fetch_prev_pool

    try:
        return fetch_prev_pool(date)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 亏钱效应 / 大面
def loss_effect(date: str, prev: Optional[str] = None) -> dict:
    """亏钱效应 —— 昨日强势股今天**死在哪**（定稿记录优先，实时行情兜底）"""
    from .data import is_limit_up

    prev = prev or trade_calendar.prev_trade_date(date)
    rows = _settled_rows(date)
    if rows:
        got = [r for r in rows if r.get("ret") is not None]
        if got:
            n = len(got)
            deep5 = [r for r in got if r["ret"] <= -5]
            return {
                "available": True, "prev_date": prev, "source": "settled",
                "sample": n, "coverage": n, "coverage_rate": 1.0, "partial": False,
                "deep_loss_5_count": len(deep5),
                "deep_loss_5_rate": round(len(deep5) / n, 3),
                "deep_loss_7_count": sum(1 for r in got if r["ret"] <= -7),
                # 定稿记录带涨停价，跌停判据用「收在跌停价」比比对今日跌停池更直接；
                # 定稿记录没有跌停价字段，所以按**各自制度**的跌幅上限判
                "limit_down_count": sum(1 for r in got if _is_limit_down(r)),
                "worst": round(min(r["ret"] for r in got), 2),
                "market_limit_down": None,      # 需要今日跌停池，定稿路径给不了
                "prev_broken_recovery": None,   # 需要昨日炸板股，定稿记录不含
                "note": "由定稿记录算；「昨日炸板股修复」与「全市场跌停家数」需当日实时池，本次未计",
            }

    ok, why = trade_calendar.live_quotes_are_close_of(date)
    if not ok:
        return {"available": False, "reason": why}
    if not prev:
        return {"available": False, "reason": "取不到前一交易日"}
    pp, tp = pools(prev), pools(date)
    if pp is None or tp is None:
        return {"available": False, "reason": f"涨停池取数失败（{prev} 或 {date}）"}

    from .emotion_metrics import _COVERAGE_MIN, _coverage, batch_pct

    prev_zt = pp["zt"]
    codes = [r["code"] for r in prev_zt]
    pct = batch_pct(codes)
    got = [(r, pct[r["code"]]) for r in prev_zt if r["code"] in pct]
    if not got:
        return {"available": False, "reason": "批量行情全部取数失败"}
    cov = _coverage([v for _, v in got], len(codes))
    if cov["coverage_rate"] is not None and cov["coverage_rate"] < _COVERAGE_MIN:
        return {"available": False,
                "reason": f"批量行情只取到 {len(got)}/{len(codes)} 只"
                          f"（{cov['coverage_rate']:.0%}），样本不足以代表全体",
                **cov}

    today_dt = {r["code"] for r in tp["dt"]}
    n = len(got)
    deep5 = [r for r, v in got if v <= -5]
    deep7 = [r for r, v in got if v <= -7]
    limit_down = [r for r, _ in got if r["code"] in today_dt]
    # 昨日炸板股今天修复没有 —— 昨日炸板今天能涨，说明试错代价低、容错在恢复
    prev_zb_codes = {r["code"] for r in pp["zb"]}
    zb_pct = batch_pct(list(prev_zb_codes)) if prev_zb_codes else {}
    zb_vals = list(zb_pct.values())
    return {
        "available": True,
        "prev_date": prev,
        **cov,
        "sample": n,
        "deep_loss_5_count": len(deep5),
        "deep_loss_5_rate": round(len(deep5) / n, 3),
        "deep_loss_7_count": len(deep7),
        "limit_down_count": len(limit_down),
        "worst": round(min(v for _, v in got), 2),
        # 今日全市场跌停家数（不限于昨日涨停股）—— 大面的绝对量级
        "market_limit_down": len(tp["dt"]),
        "prev_broken_recovery": (
            {"sample": len(zb_vals),
             "avg": round(sum(zb_vals) / len(zb_vals), 2),
             "positive_rate": round(sum(1 for v in zb_vals if v > 0) / len(zb_vals), 3)}
            if zb_vals else None
        ),
    }


# ---------------------------------------------------------------- 昨日强势股反馈矩阵
_RESULT_ORDER = ["晋级涨停", "收红", "小跌", "跌超5%", "跌停"]


def _classify(ret: float, code: str, name: str, in_today_zt: bool, in_today_dt: bool) -> str:
    if in_today_zt:
        return "晋级涨停"
    if in_today_dt:
        return "跌停"
    if ret <= -5:
        return "跌超5%"
    if ret > 0:
        return "收红"
    return "小跌"



def _is_limit_down(row: dict) -> bool:
    """这一行今天跌停了吗 —— 按**它自己的涨跌幅制度**判。

    定稿记录里没有跌停价，只能用当日涨跌幅比对制度上限（留 0.3 个点余量，
    同 `is_limit_up` 缺价时的做法）。别用统一的 -9.8%：那会把 20cm 的票
    跌 12% 也算成跌停，而这一档在界面上是"今天最惨的那批"，算多了会夸大退潮。
    """
    ret = row.get("ret")
    if ret is None:
        return False
    return ret <= -(limit_pct(row.get("code", ""), row.get("name", "")) - 0.3)

def feedback_matrix(date: str, prev: Optional[str] = None) -> dict:
    """昨日强势股反馈矩阵：**按昨日板位分组**，看今天各落到什么结果

    定稿记录优先。⚠️ 定稿记录不含昨日炸板股，所以那一档会缺 —— 如实写进 note。
    """
    from .data import is_limit_up

    prev = prev or trade_calendar.prev_trade_date(date)
    srows = _settled_rows(date)
    if srows:
        got = [r for r in srows if r.get("ret") is not None]
        if got:
            def tier(b: int) -> str:
                # 板位写具体高度（3板/4板/…/9板），不再笼统归成「3板及以上」
                return "首板" if b <= 1 else f"{b}板"

            matrix: dict[str, dict] = {}
            details: list[dict] = []
            for r in got:
                b = int(r.get("prev_boards") or 1)
                t = tier(b)
                res = ("晋级涨停" if is_limit_up(r) else
                       # 跌停按**这只票自己的制度**判（10cm / 20cm / ST 各不同）。
                       # 一刀 -9.8% 会把 20cm 跌 12% 的票打成"跌停"；
                       # 涨的那一侧早就是制度感知的（is_limit_up 走涨停价）。
                       "跌停" if _is_limit_down(r) else
                       "跌超5%" if r["ret"] <= -5 else
                       "收红" if r["ret"] > 0 else "小跌")
                matrix.setdefault(t, {k: 0 for k in _RESULT_ORDER})[res] += 1
                details.append({"code": r["code"], "name": r["name"], "board": b,
                                "prev_tier": t, "ret": round(r["ret"], 2),
                                "result": res, "sector": r.get("sector") or ""})
            for t, cell in matrix.items():
                base = sum(cell.values())
                cell["合计"] = base
                cell["晋级率"] = round(cell["晋级涨停"] / base, 3) if base else None
            details.sort(key=lambda d: d["ret"])
            return {
                "available": True, "prev_date": prev, "source": "settled",
                "sample": len(details), "coverage": len(details),
                "coverage_rate": 1.0, "partial": False,
                "order": _RESULT_ORDER, "matrix": matrix, "details": details,
                "note": "由定稿记录算；定稿记录不含昨日炸板股，故缺「昨日炸板」这一档",
            }

    ok, why = trade_calendar.live_quotes_are_close_of(date)
    if not ok:
        return {"available": False, "reason": why}
    if not prev:
        return {"available": False, "reason": "取不到前一交易日"}
    pp, tp = pools(prev), pools(date)
    if pp is None or tp is None:
        return {"available": False, "reason": f"涨停池取数失败（{prev} 或 {date}）"}

    from .emotion_metrics import _COVERAGE_MIN, _coverage, batch_pct

    today_zt = {r["code"] for r in tp["zt"]}
    today_dt = {r["code"] for r in tp["dt"]}
    rows = pp["zt"] + [dict(r, boards=0) for r in pp["zb"]]   # 昨日炸板股单列一档
    pct = batch_pct([r["code"] for r in rows])
    # 同 loss_effect：覆盖率不够就不出矩阵（P0-5）
    _cov = _coverage([v for v in pct.values() if v is not None], len(rows))
    if _cov["coverage_rate"] is not None and _cov["coverage_rate"] < _COVERAGE_MIN:
        return {"available": False,
                "reason": f"批量行情只取到 {_cov['sample']}/{len(rows)} 只"
                          f"（{_cov['coverage_rate']:.0%}），矩阵会失真",
                **_cov}

    def tier_of(r: dict) -> str:
        b = int(r.get("boards") or 0)
        if b == 0:
            return "昨日炸板"
        if b == 1:
            return "首板"
        return f"{b}板"   # 同定稿路径：写具体高度，不归并「3板及以上」

    matrix: dict[str, dict] = {}
    details: list[dict] = []
    for r in rows:
        v = pct.get(r["code"])
        if v is None:
            continue
        tier = tier_of(r)
        res = _classify(v, r["code"], r["name"], r["code"] in today_zt, r["code"] in today_dt)
        cell = matrix.setdefault(tier, {k: 0 for k in _RESULT_ORDER})
        cell[res] += 1
        details.append({
            "code": r["code"], "name": r["name"], "board": r["board"],
            "prev_tier": tier, "ret": round(v, 2), "result": res,
            "sector": r.get("sector") or "",
        })

    if not details:
        return {"available": False, "reason": "批量行情全部取数失败"}
    for tier, cell in matrix.items():
        total = sum(cell.values())
        cell["合计"] = total
        cell["晋级率"] = round(cell["晋级涨停"] / total, 3) if total else None
    return {
        "available": True,
        "prev_date": prev,
        **_cov,
        "result_order": _RESULT_ORDER,
        "matrix": matrix,
        # 明细按今日结果从差到好排：复盘先看死在哪，不是先看谁涨得好
        "details": sorted(details, key=lambda d: d["ret"]),
    }


# ---------------------------------------------------------------- 题材结构 + 发酵时间轴
def theme_structure(date: str, top: int = 8) -> dict:
    """今日题材结构：每个题材几个板、最高几板、炸了几个、**几点开始发酵**"""
    p = pools(date)
    if p is None:
        return {"available": False, "reason": f"{date} 涨停池取数失败"}
    zt, zb = p["zt"], p["zb"]
    if not zt:
        return {"available": False, "reason": f"{date} 涨停池为空"}

    groups: dict[str, dict] = {}
    for r in zt:
        s = (r.get("sector") or "").strip() or "未分类"
        g = groups.setdefault(s, {"sector": s, "limit_up": 0, "first_boards": 0,
                                  "consec_boards": 0, "highest": 0, "broken": 0,
                                  "seal_times": [], "names": []})
        g["limit_up"] += 1
        b = int(r.get("boards") or 1)
        g["highest"] = max(g["highest"], b)
        if b <= 1:
            g["first_boards"] += 1
        else:
            g["consec_boards"] += 1
        t = _hhmmss(r.get("first_seal"))
        if t:
            g["seal_times"].append(t)
        g["names"].append({"code": r["code"], "name": r["name"], "boards": b,
                           "first_seal": t, "broken_times": r.get("broken_times") or 0})
    for r in zb:   # 炸板股算进所属题材的炸板数
        s = (r.get("sector") or "").strip() or "未分类"
        if s in groups:
            groups[s]["broken"] += 1

    out = []
    for g in groups.values():
        ts = sorted(g.pop("seal_times"))
        total_attempt = g["limit_up"] + g["broken"]
        g["first_seal"] = ts[0] if ts else None          # 该题材第一只涨停的时间
        g["last_seal"] = ts[-1] if ts else None          # 最后一只 —— 两者拉开=持续发酵
        g["broken_rate"] = round(g["broken"] / total_attempt, 3) if total_attempt else None
        g["names"] = sorted(g["names"], key=lambda x: -x["boards"])[:6]
        out.append(g)
    out.sort(key=lambda g: (-g["limit_up"], -g["highest"]))

    # 全市场涨停的时间分布 —— 早盘集中 vs 尾盘补涨，含义完全不同
    all_times = sorted(_hhmmss(r.get("first_seal")) for r in zt if _hhmmss(r.get("first_seal")))
    slots = [("09:25-09:35", "093500"), ("09:35-10:00", "100000"), ("10:00-11:30", "113000"),
             ("11:30-14:00", "140000"), ("14:00-15:00", "150500")]
    timeline, prev_bound = [], "000000"
    for label, ub in slots:
        n = sum(1 for t in all_times if prev_bound < t <= ub)
        timeline.append({"slot": label, "count": n})
        prev_bound = ub
    return {
        "available": True,
        "themes": out[:top],
        "theme_count": len(out),
        # 头部集中度：第一题材占全市场涨停的比例，越高越是一枝独秀
        "concentration": round(out[0]["limit_up"] / len(zt), 3) if out else None,
        "timeline": timeline,
    }


# ---------------------------------------------------------------- 关键事件账本
def _prev_boards_map(prev: Optional[str]) -> dict[str, int]:
    """前一交易日 代码→连板数。用来给今天的炸板/跌停票补上"昨天几板" """
    if not prev:
        return {}
    pp = pools(prev)
    if pp is None:
        return {}
    return {r["code"]: int(r.get("boards") or 1) for r in pp["zt"]}


def event_ledger(date: str, prev: Optional[str] = None) -> dict:
    """关键事件账本：今天真正值得看的那十几只票，自动挑出来"""
    p = pools(date)
    if p is None:
        return {"available": False, "reason": f"{date} 涨停池取数失败"}
    zt, zb, dt = p["zt"], p["zb"], p["dt"]
    prev = prev or trade_calendar.prev_trade_date(date)
    prev_boards = _prev_boards_map(prev)     # 代码 → 昨天几板
    events: list[dict] = []

    def add(tag: str, r: dict, note: str = ""):
        events.append({
            "tag": tag, "code": r["code"], "name": r["name"], "board": r["board"],
            "boards": int(r.get("boards") or 0), "sector": r.get("sector") or "",
            "ret": round(float(r.get("ret") or 0), 2),
            "first_seal": _hhmmss(r.get("first_seal")) or None,
            "last_seal": _hhmmss(r.get("last_seal")) or None,
            "broken_times": int(r.get("broken_times") or 0),
            "turnover": r.get("turnover"), "note": note,
            # 昨天几板 —— 断板事件的意义全在这个数上
            "prev_boards": prev_boards.get(r["code"]),
        })

    if zt:
        top = max(int(r.get("boards") or 1) for r in zt)
        for r in zt:
            if int(r.get("boards") or 1) == top:
                add("最高标", r, f"今日最高 {top} 板")
        # 反复炸板又回封 —— 分歧最大的票
        for r in sorted(zt, key=lambda x: -(x.get("broken_times") or 0))[:3]:
            if (r.get("broken_times") or 0) >= 2:
                add("反复开板", r, f"炸板 {r['broken_times']} 次后回封")
        # 每个题材最早封板的那只 = 该方向的发起者
        seen: set[str] = set()
        for r in sorted(zt, key=lambda x: _hhmmss(x.get("first_seal")) or "999999"):
            s = (r.get("sector") or "").strip()
            if s and s not in seen and _hhmmss(r.get("first_seal")):
                seen.add(s)
                add("题材首封", r, f"{s} 当日最早封板")
            if len(seen) >= 4:
                break
    zb_sorted = sorted(zb, key=lambda x: (-(prev_boards.get(x["code"]) or 0),
                                          -(x.get("broken_times") or 0)))
    for r in zb_sorted[:6]:
        pb = prev_boards.get(r["code"])
        if pb and pb >= 3:
            add("高位断板", r, f"昨日 {pb} 板，今日炸板未回封（炸 {r.get('broken_times') or 0} 次）")
        elif pb and pb >= 2:
            add("连板断板", r, f"昨日 {pb} 板，今日炸板未回封")
        else:
            add("炸板", r, f"炸板 {r.get('broken_times') or 0} 次未回封"
                          + (f"，昨日 {pb} 板" if pb else "（昨日未涨停）"))
    # 大面：连续跌停最狠；昨日是高标的今天跌停，比普通跌停更值得记
    dt_sorted = sorted(dt, key=lambda x: (-(prev_boards.get(x["code"]) or 0),
                                          -(x.get("consec_dt") or 0)))
    for r in dt_sorted[:5]:
        pb = prev_boards.get(r["code"])
        note = f"连续跌停 {int(r.get('consec_dt') or 1)} 天"
        add("高标跌停" if (pb and pb >= 2) else "跌停", r,
            note + (f"｜昨日 {pb} 板" if pb else ""))

    _LOSS = {"高位断板", "连板断板", "炸板", "高标跌停", "跌停"}
    _GAIN = {"最高标", "题材首封"}
    return {
        "available": True, "events": events, "count": len(events),
        "loss_events": [e for e in events if e["tag"] in _LOSS],
        "gain_events": [e for e in events if e["tag"] in _GAIN],
        # 「反复开板」既不是纯赚也不是纯亏 —— 它是**分歧**，单独一堆
        "split_events": [e for e in events if e["tag"] not in _LOSS | _GAIN],
    }


# ---------------------------------------------------------------- 分板块生态
def by_board(date: str, prev: Optional[str] = None) -> dict:
    """按涨跌幅制度拆开看：10cm / 20cm / 北交所 / ST 各自的涨停与晋级生态"""
    prev = prev or trade_calendar.prev_trade_date(date)
    tp = pools(date)
    if tp is None:
        return {"available": False, "reason": f"{date} 涨停池取数失败"}
    pp = pools(prev) if prev else None
    today_codes = {r["code"] for r in tp["zt"]}

    out: dict[str, dict] = {}
    for r in tp["zt"]:
        g = out.setdefault(r["board"], {"limit_up": 0, "highest": 0, "prev_base": 0, "promoted": 0})
        g["limit_up"] += 1
        g["highest"] = max(g["highest"], int(r.get("boards") or 1))
    for b in out:
        out[b]["broken"] = sum(1 for r in tp["zb"] if r["board"] == b)
        out[b]["limit_down"] = sum(1 for r in tp["dt"] if r["board"] == b)
    if pp:
        for r in pp["zt"]:
            g = out.setdefault(r["board"], {"limit_up": 0, "highest": 0,
                                            "prev_base": 0, "promoted": 0,
                                            "broken": 0, "limit_down": 0})
            g["prev_base"] += 1
            if r["code"] in today_codes:
                g["promoted"] += 1
    for g in out.values():
        g["promotion_rate"] = (round(g["promoted"] / g["prev_base"], 3)
                               if g.get("prev_base") else None)
    return {"available": True, "prev_date": prev, "boards": out}

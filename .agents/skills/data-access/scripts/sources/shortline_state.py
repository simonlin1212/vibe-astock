"""情绪周期状态机(六态:冰点/退潮/分歧/高潮/启动/发酵)。

移植自 Vibe AStock v2.0.0 的 duanxian/emotion_state.py,而它本身移植自
a-share-daily-review-dashboard(v4c)的状态机:**加权 rank 分位合成 S**
+ 候选态 + 非对称 T+1 确认。算法与口径不变;数据层从本地缓存
(stats_context.series)改为逐日调用 `sources.shortline_emotion.daily_readings`
—— 六项读数的字段名与口径完全一致(limit_up / highest_board / broken_rate /
money_effect / promotion_1to2 / deep_loss),状态机本体一行没改。

  指标映射(dashboard metrics_daily → daily_readings):
    nu(涨停家数)        → limit_up
    h(最高连板)         → highest_board
    fr(炸板率)          → broken_rate(同一公式 炸板/(涨停+炸板))
    prem_median(昨涨停溢价中位) → money_effect(昨日涨停池今日收益中位数)
    p12(1进2晋级率)     → promotion_1to2
    dm(大面率)          → 无同口径序列;风险组用 deep_loss(昨涨停今日跌超5%家数)代替
    nd(跌停家数)        → 无历史缓存,对应分支裁剪(见下)

  分组(组内平均、组间加权,消广度族冗余的思路与 dashboard 一致):
    广度 0.35: limit_up(+1), highest_board(+1)
    质量 0.35: money_effect(+1), promotion_1to2(+1)
    风险 0.30: broken_rate(-1), deep_loss(-1)

  裁剪与替换(每一条都是 dashboard 有、本项目没有的):
    · 冰点分支的「跌停家数分位 ≥ 60」腿去掉 —— nd 无历史。只保留 s < ice_pct/2
      的极低分腿,因此冰点会比 dashboard 更保守(更难触发)。
    · two_day_weak 的 dm > 25% 腿换成 deep_loss 分位 ≥ 75(近窗内杀跌够重)。
    · 高潮共振的三指标 nu_p/nl_p/pr_p > 85 换成 limit_up/highest_board/money_effect
      三者分位均 > 85。

  铁律沿用:
    · 候选态优先级:冰点 > 退潮 > 分歧 > 高潮 > 启动/发酵(防御优先)
    · 退潮带是**纯区间捕获**(ice_pct ≤ sp < start_pct),退潮带上沿 ≡ 启动带下沿,
      无规则空带在构造上不存在
    · 非对称 T+1 确认:转防御(退潮/冰点/分歧/高潮)立即生效,转进攻需与前一
      候选态一致才确认 —— 加仓要确认,减仓不等待
    · 一致性夹紧:无显式转移且 sp ≥ start_pct 时,原"缺省停留"不得让读数与状态
      矛盾 —— 溢价为正 → 分歧(观察一日),否则 → 退潮

  坦白口径(输出里必须带):
    · 阈值沿用 dashboard 冻结分位值(ice 15 / start 40 / boom 85 / p12_start 60)。
      本地缓存通常只有几十个交易日,**不足以真正标定阈值** —— 样本少于
      MIN_BASELINE 天时只输出序列与读数、不判定状态(baseline_ok=False)。
    · S 是历史 rank 分位的合成,分位本身依赖窗口长度;样本少时分位粗糙,
      状态是**分析框架**,不是客观事实,更不是交易结论。

  与旧版的差异(数据层):
    · 旧版从本地 SQLite 缓存读序列,零网络;新版逐日拉东财池(每天约 4 次请求),
      默认窗口 60 天 ≈ 240 次请求,首次很慢 —— 已收盘的日子池子结果不变,
      取数服务对 cn_shortline_daily 的缓存让历史越积越快,但端点内部直调
      daily_readings 不经过那层缓存,窗口是每次现算的。
    · 每项读数单独可缺(缺的写 None),状态机沿用旧版的缺项处理:
      MIN_S_INPUTS 缺任一 → 当日 S 不可算;组内缺项按可用组重归一。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sources import shortline as S  # noqa: E402
from sources import shortline_emotion as E  # noqa: E402

# ── 阈值(口径定义,沿用 dashboard 冻结值;样本不足时不判定,见上)──────────
THRESHOLDS = {
    "ice_pct": 15,        # 冰点/退潮 分界(s 分位)
    "start_pct": 40,      # 退潮/启动 分界(s 分位;退潮带上沿 ≡ 此值)
    "boom_pct": 85,       # 高潮共振:三指标分位均需高于此
    "p12_weak": 0.25,     # two_day_weak 的晋级率绝对阈值(p12 是 0-1 比率)
    "prem_weak": -3.0,    # two_day_weak 的溢价中位阈值(%)
    "ice_half": 7.5,      # 冰点的极低分腿:s < ice_pct/2
    "p12_start_pct": 60,  # 启动/发酵的晋级率分位门槛
    "deep_weak_pct": 75,  # two_day_weak 的 deep_loss 分位门槛(替代 dm 腿)
}
MIN_BASELINE = 30        # 分位基线样本下限;低于此只描述不判定
MIN_S_INPUTS = ("limit_up", "money_effect", "broken_rate")  # 缺任一 → 当日 S 不可算
MIN_WINDOW, MAX_WINDOW = 20, 120  # 窗口上下限:太短没有分位可言,太长取数撑不住

# (组名, [(读数键, 方向)], 组权重) —— 与 docstring 的映射一一对应
_GROUPS: tuple[tuple[str, tuple[tuple[str, int], ...], float], ...] = (
    ("广度", (("limit_up", 1), ("highest_board", 1)), 0.35),
    ("质量", (("money_effect", 1), ("promotion_1to2", 1)), 0.35),
    ("风险", (("broken_rate", -1), ("deep_loss", -1)), 0.30),
)

STATE_ORDER = ("冰点", "退潮", "分歧", "高潮", "启动", "发酵")
_DEFENSIVE = ("退潮", "冰点", "分歧", "高潮")


# ── 分位工具(与旧版逐字一致)──────────────────────────────────────────────
def _rank_pct(vals: list[Optional[float]], i: int) -> Optional[float]:
    """vals[0..i] 窗口内 vals[i] 的分位(0-100,≤ 当值的占比)。

    扩张窗,与 dashboard metrics 系一致。当前值或历史样本为 None 的不进分母。
    样本(含当前)不足 2 个时返回 None —— 单样本的分位没有意义。
    """
    cur = vals[i]
    if cur is None:
        return None
    window = [v for v in vals[:i + 1] if v is not None]
    if len(window) < 2:
        return None
    return round(sum(1 for v in window if v <= cur) / len(window) * 100, 2)


# ── 单日候选态判定(纯函数,与旧版逐字一致,便于独立测试)────────────────────
def _classify(i: int, cols: dict, s_series: list, deep_pcts: list, p12_pcts: list,
              prev_state: str) -> str:
    """第 i 天的候选态。cols/s_series 等为全序列(扩张窗分位在调用方已算好)。"""
    sp = s_series[i]
    if sp is None:
        return ""
    prem = cols["money_effect"][i]
    p12 = cols["promotion_1to2"][i]
    lu_p = _rank_pct(cols["limit_up"], i)
    hb_p = _rank_pct(cols["highest_board"], i)
    me_p = _rank_pct(cols["money_effect"], i)
    cand = prev_state  # 缺省=停留原态
    # 优先级:冰点 > 退潮 > 分歧 > 高潮 > 启动/发酵(防御优先)
    two_day_weak = (i >= 1 and p12 is not None and p12 < THRESHOLDS["p12_weak"]
                    and prem is not None and prem < THRESHOLDS["prem_weak"]
                    and deep_pcts[i] is not None
                    and deep_pcts[i] >= THRESHOLDS["deep_weak_pct"])
    ebb_hi = THRESHOLDS["start_pct"]
    if sp < THRESHOLDS["ice_half"]:
        # 冰点:dashboard 还要求「跌停家数分位 ≥60 或 s 极低」,nd 序列本项目
        # 没有,只保留极低分腿 —— 冰点更难触发,宁可少判不可误判
        cand = "冰点"
    elif two_day_weak or THRESHOLDS["ice_pct"] <= sp < ebb_hi:
        cand = "退潮"
    elif (prev_state in ("高潮", "发酵", "启动") and lu_p is not None and lu_p > 50
          and ((cols["broken_rate"][i] is not None and cols["broken_rate"][i] > 0.35)
               or (i >= 1 and s_series[i - 1] is not None
                   and sp < s_series[i - 1] - 15))):
        cand = "分歧"
    elif (lu_p is not None and hb_p is not None and me_p is not None
          and lu_p > THRESHOLDS["boom_pct"] and hb_p > THRESHOLDS["boom_pct"]
          and me_p > THRESHOLDS["boom_pct"]):
        cand = "高潮"  # 三指标共振(limit_up / highest_board / money_effect)
    elif (sp >= THRESHOLDS["start_pct"]
          and (p12_pcts[i] is None or p12_pcts[i] >= THRESHOLDS["p12_start_pct"])
          and (prev_state != "退潮" or (prem is not None and prem > 0))):
        # 复苏边(退潮→进攻)额外要求昨涨停溢价为正,防下跌中途转多;
        # 分歧日被反包重回一致(分歧→发酵)是常见形态,前态集合含全六态
        cand = "发酵" if sp >= 55 else "启动"
    elif cand == prev_state and sp >= THRESHOLDS["start_pct"] and cand in ("冰点", "退潮"):
        # 一致性夹紧:分数中位以上却挂着防御态,按赚钱效应二选一
        cand = "分歧" if (prem is not None and prem > 0) else "退潮"
    return cand


# ── 主入口 ────────────────────────────────────────────────────────────────
def build_state(date: Optional[str] = None, days: int = 60) -> dict:
    """目标日的情绪状态。返回结构化 dict(渲染与映射层共用同一份)。

    date: 'YYYY-MM-DD'(已收盘交易日)。历史读数逐日取自
    `shortline_emotion.daily_readings`(三张东财池 + 昨日涨停池,任意历史日
    都算得出);当天没有可用读数 → available=False。
    """
    d = S.review_day(date)
    days = int(days)
    if not MIN_WINDOW <= days <= MAX_WINDOW:
        raise ValueError(f"days 要在 {MIN_WINDOW}–{MAX_WINDOW} 个交易日之间")

    try:
        dates = S.trade_dates_ending_at(d, days)
    except Exception:  # noqa: BLE001  交易日历取不到时至少保住目标日,让下面按天数不足如实报
        dates = [S.iso(d)]
    if not dates or dates[-1] != S.iso(d):
        dates = (dates or []) + [S.iso(d)]

    hist: list[dict] = []
    for dt in dates:
        try:
            r = E.daily_readings(dt)
        except Exception:  # noqa: BLE001  单日取数失败按缺项处理,不炸整个窗口
            r = {k: None for k in ("limit_up", "highest_board", "broken_rate",
                                   "money_effect", "promotion_1to2", "deep_loss")}
        hist.append({"date": dt, **r})

    if not any(h.get(k) is not None for h in hist
               for k in ("limit_up", "money_effect", "broken_rate")):
        return {"available": False, "date": d,
                "reason": "窗口内一天可用读数都没有(东财涨停 / 炸板 / 昨日涨停池全没取到)"}
    if not any(h.get(k) is not None for k in ("limit_up", "money_effect", "broken_rate")
               for h in [hist[-1]]):
        return {"available": False, "date": d,
                "reason": f"{d} 无可用读数(涨停池或炸板池没取到;序列止于最近有读数的交易日)"}

    n = len(hist)
    keys = ("limit_up", "highest_board", "broken_rate", "money_effect",
            "promotion_1to2", "deep_loss")
    cols: dict[str, list[Optional[float]]] = {k: [h.get(k) for h in hist] for k in keys}

    # ── S 合成:组内平均、组间加权(缺项按可用组重归一,与 dashboard 一致)──
    s_series: list[Optional[float]] = []
    for i in range(n):
        missing_core = any(cols[k][i] is None for k in MIN_S_INPUTS)
        gnum = gden = 0.0
        for _gname, gkeys, gw in _GROUPS:
            vals: list[float] = []
            for key, sign in gkeys:
                pct = _rank_pct(cols[key], i)
                if pct is None:
                    continue
                vals.append((100.0 - pct) if sign < 0 else pct)
            if vals:
                gnum += gw * (sum(vals) / len(vals))
                gden += gw
        s_series.append(None if (gden == 0 or missing_core) else round(gnum / gden, 2))

    sample_days = n  # 序列长度即分位基线样本量(扩张窗)
    baseline_ok = sample_days >= MIN_BASELINE

    # deep_loss 的近窗分位(two_day_weak 的 dm 替代腿):用扩张窗
    deep_pcts = [_rank_pct(cols["deep_loss"], i) for i in range(n)]
    p12_pcts = [_rank_pct(cols["promotion_1to2"], i) for i in range(n)]

    # ── 候选态 ────────────────────────────────────────────────────────────
    states: list[str] = []
    prev_state = "冰点"  # 冷启动初始态(与 dashboard 一致)
    for i in range(n):
        if s_series[i] is None or not baseline_ok:
            # 基线不足或当日 S 不可算:不判定,记空态(输出里如实说明)
            states.append("")
            continue  # 前态不推进(未判定不算转移)
        cand = _classify(i, cols, s_series, deep_pcts, p12_pcts, prev_state)
        states.append(cand)
        prev_state = cand

    # ── 非对称 T+1 确认:转防御立即生效,转进攻需与前一候选态一致 ──────────
    confirmed: list[str] = []
    for i in range(n):
        cur = states[i]
        if not cur:
            confirmed.append("")  # 未判定日照空
            continue
        if i == 0 or cur in _DEFENSIVE or cur == states[i - 1]:
            confirmed.append(cur)
        else:
            confirmed.append(confirmed[i - 1])

    # 最近一个**已判定**日才是"当前状态";空串只出现在未判定段
    cur_idx = max((i for i in range(n) if confirmed[i]), default=-1)
    if cur_idx < 0:
        return {
            "available": False, "date": d, "sample_days": sample_days,
            "min_baseline": MIN_BASELINE,
            "reason": f"分位基线不足({sample_days} < {MIN_BASELINE} 个交易日),只描述不判定",
        }

    today = {
        "s": s_series[cur_idx],
        "s_pct": s_series[cur_idx],  # S 已是分位刻度,二者同名(与 dashboard 一致)
        "candidate": states[cur_idx],
        "confirmed": confirmed[cur_idx],
        "readings": {k: cols[k][cur_idx] for k in keys},
    }
    history = [{"date": hist[i]["date"], "s_pct": s_series[i],
                "state": confirmed[i] or None}
               for i in range(max(0, n - 20), n)]

    return {
        "available": True,
        "date": d,
        "settled": S.is_settled(d),
        "requested_days": days,
        "sample_days": sample_days,
        "min_baseline": MIN_BASELINE,
        "baseline_ok": baseline_ok,
        **today,
        "history": history,
        "thresholds": dict(THRESHOLDS),
        # 移植口径说明:给界面与 prompt 原样展示,谁接手都能知道差异在哪
        "adaptation": ("指标映射:nu→limit_up、h→highest_board、fr→broken_rate、"
                       "prem_median→money_effect、p12→promotion_1to2;"
                       "nd(跌停家数)/dm(大面率)无历史序列,冰点腿裁剪、"
                       "大面腿用 deep_loss 分位替代。"),
    }


def render(state: dict) -> str:
    """状态机结果 → 分析师 prompt 直接能吃的文本。不可用就如实说明。"""
    if not state.get("available"):
        return (f"[情绪状态机不可用:{state.get('reason', '未知')}]"
                "—— 这是移植的辅助框架,缺席不影响其他情绪读数。")
    lines = ["[情绪周期状态机(移植自开源复盘看板 v4c,六态:冰点/退潮/分歧/高潮/启动/发酵)]"]
    lines.append(
        f"· 综合情绪分 S={state['s_pct']:.1f}(历史 rank 分位合成,0-100;"
        f"分位基线 {state['sample_days']} 个交易日"
        + ("，已达到判定下限" if state.get("baseline_ok") else f"，低于判定下限 {state['min_baseline']}，仅供参考")
        + "）"
    )
    lines.append(f"· 候选态「{state['candidate']}」→ 确认态「{state['confirmed']}」"
                 "（转防御立即生效、转进攻需 T+1 一致：加仓要确认，减仓不等待）")
    r = state.get("readings") or {}
    fmt = lambda v, suf="": "—" if v is None else f"{v}{suf}"  # noqa: E731
    lines.append("· 当日读数:涨停 "
                 + fmt(r.get("limit_up"), " 家") + ";最高连板 "
                 + fmt(r.get("highest_board"), " 板") + ";炸板率 "
                 + (f"{r['broken_rate']:.0%}" if r.get("broken_rate") is not None else "—")
                 + ";昨涨停溢价中位 "
                 + (f"{r['money_effect']:+.2f}%" if r.get("money_effect") is not None else "—")
                 + ";1进2晋级率 "
                 + (f"{r['promotion_1to2']:.0%}" if r.get("promotion_1to2") is not None else "—")
                 + ";昨涨停跌超5% "
                 + fmt(r.get("deep_loss"), " 家"))
    hist = state.get("history") or []
    if len(hist) >= 2:
        tail = "、".join(f"{h['date'][5:]}:{h['state'] or '—'}" for h in hist[-10:])
        lines.append(f"· 近段确认态轨迹(升序):{tail}")
    lines.append("· " + state.get("adaptation", ""))
    lines.append("⚠ 状态机是分析框架不是客观事实;本地样本通常不足以标定阈值,"
                 "状态读数与绝对涨跌都不能直接推出交易结论。")
    return "\n".join(lines)

"""短线复盘数据层 —— 在 `fetchers` 的取数函数之上做缓存、降级与文本化。"""

from __future__ import annotations

from duanxian.paths import data_path
from typing import Optional

import json
import os
import socket
from collections import Counter

from . import fetchers as dr
from .util import is_today, safe_join

socket.setdefaulttimeout(45)

_LEADER_DIR = data_path("leaders")

_MACRO_GROUPS = {
    "AI算力": ["算力", "CPO", "光模块", "光通信", "液冷", "PCB", "铜连接", "服务器"],
    "人形机器人": ["机器人", "减速器", "丝杠", "灵巧手", "人形"],
    "商业航天": ["航天", "火箭", "卫星"],
}


def _ymd(date: str) -> str:
    return date.replace("-", "")


def _degrade_msg(label: str, date: str, msg: str) -> str:
    """取数没成功时**唯一**的返回长相。

    这里只有一种约定是要紧的：所有失败都得长成 `[⚠️ …]`。判断"这一路数据能不能用"
    的地方不止一处（体检闸、降级计数、界面提示），它们都认这个前缀；
    谁要是自己发明一种失败措辞（比如「XX取数失败：…」这种裸文本），
    那几处就一起看不见它 —— 非空、没前缀，在它们眼里跟正常数据一模一样。
    """
    return f"[⚠️ {label}｜{date} 数据获取失败已降级：{str(msg)[:120]}]"


def _degrade(label: str, date: str, exc: Exception) -> str:
    return _degrade_msg(label, date, f"{type(exc).__name__}: {exc}")


def _asof_note(date: str) -> str:
    """仅在取数前后均通过参考行情日期校验的分支追加。"""
    return (f"\n（口径提示：取数前后参考行情均对应 {date} 收盘；"
            "资金流/板块来自当前接口，并非独立历史快照，端点本身的时间戳未单独核实。）")


# ============ ① 情绪面 ============
def get_sentiment_data(date: str) -> str:
    try:
        zt = dr.fetch_zt_pool(_ymd(date))
        ztdf = zt.get("zt")
        n_zt = int(len(ztdf)) if ztdf is not None else 0
        n_zb = int(zt.get("zb_count", 0) or 0)
        n_dt = int(zt.get("dt_count", 0) or 0)
        hc = int(zt.get("highest_consec", 0) or 0)
        br = n_zb / (n_zb + n_zt) if (n_zb + n_zt) else 0
        ladder = zt.get("ladder", [])
        highs = [x for x in ladder if x["consec_boards"] >= 2][:8]
        ladder_txt = "、".join(f"{x['name']}({x['consec_boards']}板)" for x in highs) or "无 2 板以上"
        if n_zt == 0 and n_dt == 0:
            return f"[⚠️ {date} 涨停池为空，可能非交易日或数据未更新，情绪面数据不可用]"
        return (
            f"[真实数据 {date}] 涨停 {n_zt} 家，炸板 {n_zb} 家，跌停 {n_dt} 家；"
            f"炸板率 {br:.0%}；最高连板 {hc} 板。连板梯队（2 板以上）：{ladder_txt}"
        )
    except Exception as exc:  # noqa: BLE001
        return _degrade("情绪面", date, exc)


def get_emotion_metrics(date: str) -> tuple[str, dict]:
    """派生情绪指标（赚钱效应 / 晋级率 / 连板溢价）"""
    try:
        from . import emotion_metrics as em

        m = em.build_metrics(date)
        return em.render_metrics(m), m
    except Exception as exc:  # noqa: BLE001
        return _degrade("派生情绪指标", date, exc), {}


def get_market_facts(date: str) -> tuple[str, dict]:
    """客观事实表（封板质量 / 亏钱效应 / 反馈矩阵 / 题材结构 / 事件账本 / 分板块）。

    与 `get_emotion_metrics` 一样返回 **(文本块, 结构化 dict)**：文本喂 prompt，
    结构化那份供 UI 直接渲染。事实层是"今天发生了什么"，指标层是"什么温度"，
    两者互补，别互相替代。

    延迟导入同 emotion_metrics（market_facts 反过来要借 data 注入 sys.path）。
    """
    try:
        from . import market_facts as mf

        from . import stats_context as sctx
        from . import theme_tree as tt

        from . import breadth as bd

        facts = {
            "breadth": bd.market_breadth(date),
            "stats_context": sctx.context_for(date),
            "day_diff": sctx.diff(date),
            "trend": sctx.trend(10, end=date),
            "theme_tree": tt.build(date),
            "seal_quality": mf.seal_quality(date),
            "loss_effect": mf.loss_effect(date),
            "feedback_matrix": mf.feedback_matrix(date),
            "theme_structure": mf.theme_structure(date),
            "event_ledger": mf.event_ledger(date),
            "by_board": mf.by_board(date),
        }
        return render_market_facts(facts), facts
    except Exception as exc:  # noqa: BLE001
        return _degrade("市场事实表", date, exc), {}


def _pct(v) -> str:
    return "—" if v is None else f"{v:.0%}"


def render_market_facts(f: dict) -> str:
    """事实表 → 分析师 prompt 直接能吃的文本。不可用的部分如实说明。"""
    lines = ["[今日客观事实表]"]

    bd_ = f.get("breadth") or {}
    if bd_:
        from . import breadth as _bd

        lines.append(_bd.render(bd_))

    sq = f.get("seal_quality") or {}
    if sq.get("available"):
        lines.append(
            f"· 封板质量：涨停 {sq['total']} 家，其中 {sq['never_broken']} 家全天没炸过板"
            f"（{_pct(sq['never_broken_rate'])}）；开盘 5 分钟内封板 {sq['opening_seconds']} 家；"
            f"14:30 后才最终封住 {sq['late_seal']} 家；炸过又回封 {sq['reopened']} 家；"
            f"平均炸板 {sq['avg_broken_times']} 次"
        )
    else:
        lines.append(f"· 封板质量：不可用（{sq.get('reason', '未知')}）")

    le = f.get("loss_effect") or {}
    if le.get("available"):
        rec = le.get("prev_broken_recovery")
        rec_txt = (f"；昨日炸板股今日均 {rec['avg']:+.2f}%、翻红 {_pct(rec['positive_rate'])}"
                   if rec else "")
        lines.append(
            f"· 亏钱效应：{le['prev_date']} 涨停的 {le['sample']} 只里，今日跌超 5% 有 "
            f"{le['deep_loss_5_count']} 只（{_pct(le['deep_loss_5_rate'])}）、跌超 7% 有 "
            f"{le['deep_loss_7_count']} 只、跌停 {le['limit_down_count']} 只，最差 {le['worst']:+.2f}%；"
            + (f"全市场跌停 {le['market_limit_down']} 家" if le.get("market_limit_down") is not None
             else "本统计模块未获取全市场跌停数") + rec_txt
        )
    else:
        lines.append(f"· 亏钱效应：不可用（{le.get('reason', '未知')}）")

    fm = f.get("feedback_matrix") or {}
    if fm.get("available"):
        segs = []
        for tier, cell in fm["matrix"].items():
            segs.append(f"{tier}({cell['合计']}只)：晋级{cell['晋级涨停']}/收红{cell['收红']}"
                        f"/跌超5%{cell['跌超5%']}/跌停{cell['跌停']}")
        lines.append("· 昨日强势股反馈（按昨日板位分组）：" + "；".join(segs))

    # 统计语境放最前 —— 先知道"今天算不算异常"，再看细节
    sc_, dd_ = f.get("stats_context") or {}, f.get("day_diff") or {}
    if sc_ or dd_:
        from . import stats_context as _sctx

        txt = _sctx.render(sc_, dd_)
        if txt:
            lines.append(txt)

    tt_ = f.get("theme_tree") or {}
    if tt_.get("available"):
        from . import theme_tree as _tt

        lines.append(_tt.render(tt_))
    elif tt_:
        lines.append(f"· 题材事件树：不可用（{tt_.get('reason', '未知')}）→ 下面退回行业分类，"
                     "注意行业≠题材")

    ts = f.get("theme_structure") or {}
    if ts.get("available"):
        top = ts["themes"][:5]
        segs = [f"{t['sector']}(涨停{t['limit_up']}/最高{t['highest']}板/炸{t['broken']}"
                f"/首封{(t['first_seal'] or '—')[:4]})" for t in top]
        tl = "、".join(f"{x['slot']} {x['count']}家" for x in ts["timeline"])
        lines.append(f"· 题材结构（共 {ts['theme_count']} 个方向，头部占比 {_pct(ts['concentration'])}）："
                     + "；".join(segs))
        lines.append(f"· 涨停时间分布（按首次封板）：{tl}"
                     "　←　早盘集中=主动发酵，午后才起来=被动轮动")

    bb = f.get("by_board") or {}
    if bb.get("available"):
        segs = [f"{b}：涨停{g['limit_up']}/最高{g['highest']}板/晋级率{_pct(g.get('promotion_rate'))}"
                for b, g in bb["boards"].items() if g.get("limit_up")]
        lines.append("· 分涨跌幅制度（10cm/20cm/ST 生态不同，别混着读）：" + "；".join(segs))

    el = f.get("event_ledger") or {}
    if el.get("available") and el["events"]:
        segs = [f"[{e['tag']}]{e['name']}({e['boards']}板,{e['sector']})" for e in el["events"][:10]]
        lines.append("· 关键事件：" + "、".join(segs))
    return "\n".join(lines)


# ============ ② 资金面 ============
def get_capital_data(date: str) -> str:
    from .trade_calendar import live_quotes_are_close_of
    matched, reason = live_quotes_are_close_of(date)
    if not matched:
        try:
            from .historical_sources import historical_activity
            return historical_activity(date)
        except Exception as exc:
            return _degrade("资金面历史补源", date, exc)
    try:
        ind = dr.fetch_sector_flow("2")  # 行业
        dr.enrich_trend(ind)

        def _top(lst, n=6, rev=True):
            xs = [s for s in lst if s.get("inflow_today") is not None]
            xs.sort(key=lambda s: s["inflow_today"], reverse=rev)
            return xs[:n]

        lines = ["行业主力净流入 TOP："]
        for s in _top(ind):
            lines.append(
                f"  {s['name']} 今{s['inflow_today']}亿/5日{s['inflow_5d']}亿 "
                f"涨{s['change_pct']}% 领涨{s['lead_stock']}"
            )
        lines.append("行业主力净流出 TOP：")
        for s in _top(ind, rev=False):
            lines.append(f"  {s['name']} 今{s['inflow_today']}亿")
        try:
            turn = dr.fetch_turnover_top20(10)
            lines.append("全市场成交额 TOP：")
            for t in turn:
                lines.append(f"  {t['name']} {t['amount']}亿 涨{t['change_pct']}% {t.get('sector') or ''}")
        except Exception as exc:
            lines.append(f"（成交额榜获取失败已跳过：{type(exc).__name__}）")
        matched, reason = live_quotes_are_close_of(date)
        if not matched:
            return _degrade_msg("实时资金与板块", date, "取数期间行情日期已变化，本批未使用：" + reason)
        return "\n".join(lines) + _asof_note(date)
    except Exception as exc:  # noqa: BLE001
        return _degrade("资金面", date, exc)


# ============ 大板块本周 ============
def get_macro_sector_data(date: str) -> str:
    from .trade_calendar import live_quotes_are_close_of
    matched, reason = live_quotes_are_close_of(date)
    if not matched:
        try:
            from .historical_sources import historical_activity
            return historical_activity(date, _MACRO_GROUPS)
        except Exception as exc:
            return _degrade("大板块历史补源", date, exc)
    try:
        concept = dr.fetch_sector_flow("3")  # 概念
        dr.enrich_trend(concept)
        lines = ["大赛道跟踪（概念板块口径，近5交易日不等于本周）："]
        for grp, kws in _MACRO_GROUPS.items():
            ms = [c for c in concept if any(k in c["name"] for k in kws)]
            if not ms:
                lines.append(f"  {grp}：无匹配概念板块")
                continue
            lines.append(f"  {grp}（概念成分有重叠，逐项看，不合计）：")
            for c in sorted(ms, key=lambda x: x.get("inflow_5d") or 0, reverse=True):
                chg = f"{c['change_pct']:+.2f}%" if c.get('change_pct') is not None else '未覆盖'
                flow = f"{c['inflow_5d']:+.2f}亿" if c.get('inflow_5d') is not None else '未覆盖'
                lines.append(f"    {c['name']}：当日涨幅{chg}，近5交易日净额{flow}")
        matched, reason = live_quotes_are_close_of(date)
        if not matched:
            return _degrade_msg("实时资金与板块", date, "取数期间行情日期已变化，本批未使用：" + reason)
        return "\n".join(lines) + _asof_note(date)
    except Exception as exc:  # noqa: BLE001
        return _degrade("大板块本周", date, exc)


# ============ ③ 题材热点（涨停原因题材串）============
def get_theme_reasons(date: str) -> str:
    try:
        reasons, err = dr.fetch_zt_reasons(_ymd(date))
        if not reasons:
            return _degrade_msg("题材涨停原因", date, f"涨停原因题材串未取到：{err}")
        tags = Counter()
        for r in reasons.values():
            for t in r.split("+"):
                t = t.strip()
                if t:
                    tags[t] += 1
        hot = tags.most_common(12)
        return f"{date} 涨停题材串热度 TOP：" + "、".join(f"{t}×{c}" for t, c in hot) + (f"\n{err}" if err else "\n来源：问财目标日涨停原因。")
    except Exception as exc:  # noqa: BLE001
        return _degrade("题材涨停原因", date, exc)


# ============ ④ 龙虎榜游资 ============
def get_dragon_tiger_data(date: str) -> str:
    try:
        rows = dr.fetch_lhb(_ymd(date), top=15)
        if rows and isinstance(rows[0], dict) and rows[0].get("error"):
            return _degrade_msg("龙虎榜", date, rows[0]["error"])
        if not rows:
            return f"{date} 无龙虎榜数据"
        lines = ["龙虎榜净买额榜（亿，负=净卖）："]
        for r in rows:
            nb = r.get("net_buy")
            nb_s = f"{nb:+.2f}" if nb is not None else "—"
            lines.append(f"  {r['name']} 净买{nb_s}亿 [{r.get('reason', '')}]")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return _degrade("龙虎榜游资", date, exc)


# ============ ⑤ 龙头跟踪（含持久化）============
def get_leader_data(date: str) -> str:
    try:
        d = _ymd(date)
        zt = dr.fetch_zt_pool(d)
        ladder = [x for x in zt.get("ladder", []) if x["consec_boards"] >= 1]
        top = sorted(ladder, key=lambda x: x["consec_boards"], reverse=True)[:12]

        if top:
            os.makedirs(_LEADER_DIR, exist_ok=True)
            try:
                path = safe_join(_LEADER_DIR, f"{d}.json")
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"date": date, "ladder": top}, fh, ensure_ascii=False)
                os.replace(tmp, path)  # 原子替换
            except Exception:
                pass

        if not top:
            return f"[⚠️ {date} 无有效连板数据（可能非交易日），龙头跟踪不可用]"

        lines = [f"连板梯队（{date}，共 {len(ladder)} 只；以下仅展示前 {min(8, len(top))} 只）："]
        for x in top[:8]:
            lines.append(f"  {x['name']}({x['consec_boards']}板·{x.get('sector', '')})")

        # 载入最近 5 份已有快照，归档日期不一定连续。
        try:
            files = sorted(f for f in os.listdir(_LEADER_DIR) if f.endswith(".json") and f[:8] < d)
        except FileNotFoundError:
            files = []
        hist = files[-5:]
        if hist:
            lines.append("最近 5 份已有历史归档（日期可能不连续，不代表最近五个交易日）：")
            for hf in hist:
                try:
                    with open(os.path.join(_LEADER_DIR, hf), encoding="utf-8") as fh:
                        h = json.load(fh)
                    hl = h.get("ladder", [])
                    if hl:
                        lead = hl[0]
                        lines.append(
                            f"  {h.get('date', hf[:8])}: 最高{lead['consec_boards']}板 "
                            f"{lead['name']}({lead.get('sector', '')})"
                        )
                except Exception:
                    continue
        else:
            lines.append("（首次运行，近 5 日龙头谱系需逐日快照攒够后才有对比）")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return _degrade("龙头跟踪", date, exc)

# ---------- 前日涨停池（复盘的昨日反馈 / 晋级率 / 多日趋势都要用）----------

_PREV_POOL_DIR = data_path("cache/prev_pool")


def is_limit_up(row: dict) -> "Optional[bool]":
    """这一行今天涨停了吗。优先用「现价 == 涨停价」，缺价格字段时按制度推定。"""
    close, limit = row.get("close"), row.get("limit_price")
    if close is not None and limit is not None and limit > 0:
        return abs(close - limit) < 0.011
    ret = row.get("ret")
    if ret is None:
        return None
    from .market_facts import limit_pct

    return ret >= limit_pct(row.get("code", ""), row.get("name", "")) - 0.3


def fetch_prev_pool(date: str) -> "Optional[list[dict]]":
    """取「前一交易日涨停股在 date 当天的表现」，已定稿的日子落盘缓存。"""
    from . import trade_calendar
    from .cache_policy import fresh as cache_fresh, write as write_cache

    is_past = trade_calendar.is_settled(date)
    os.makedirs(_PREV_POOL_DIR, exist_ok=True)
    path = os.path.join(_PREV_POOL_DIR, f"{date}.json")
    if is_past and os.path.isfile(path) and cache_fresh(path, date):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            pass

    try:
        import akshare as ak

        df = ak.stock_zt_pool_previous_em(date=date.replace("-", ""))
    except Exception:  # noqa: BLE001
        return None
    if df is None or not len(df):
        return None

    rows = []
    for _, r in df.iterrows():
        try:
            rows.append({
                "code": str(r["代码"]).zfill(6),
                "name": str(r["名称"]),
                "ret": float(r["涨跌幅"]),
                "prev_boards": int(r["昨日连板数"]),
                "seal_time": str(r.get("昨日封板时间", "")),
                "sector": str(r.get("所属行业", "")),
                "close": float(r["最新价"]) if r.get("最新价") is not None else None,
                "limit_price": float(r["涨停价"]) if r.get("涨停价") is not None else None,
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        return None
    if is_past:
        write_cache(path, rows)
    return rows

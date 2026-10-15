"""映射层:短线情绪的计算型端点(cn_shortline_emotion / cn_shortline_cycle / cn_shortline_facts / cn_shortline_daily /
cn_shortline_settled / cn_shortline_structure / cn_shortline_themes / cn_shortline_firstboard / cn_shortline_live / cn_shortline_ladder /
cn_shortline_turnover_top / cn_shortline_bigcaps / cn_shortline_board_ladder / cn_shortline_theme_rotation)。约定同 mappers.py。

- 比例一律按**小数**入证据(unit「小数」),涨跌幅按 **%**:数字闸门对「24%」会同时按比例比,两种写法都对得上。
- 同一天多条同名证据靠 record_key 区分(档位 / 制度 / 题材 / 时段 / 事件)。
- 某一块算不出来 → 进 missing 并标 partial,其余照出;一条证据都没有才 failed。
- 🔴 **没收盘的那一天一律 partial**:取数服务只缓存 exit 0 的结果,
  盘中算出来的数若被缓存住,晚上打开还是下午两点的样子,而且看不出来。
"""
from __future__ import annotations

import hashlib
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sources.mappers import empty, ev, out  # noqa: E402
from sources.shortline_boards import render_board_ladder, render_theme_rotation  # noqa: E402
from sources.shortline_emotion import render_cycle, render_daily, render_metrics, render_settled  # noqa: E402
from sources.shortline_extra import render_extra  # noqa: E402
from sources.shortline_state import render as render_state  # noqa: E402
from sources.shortline_facts import render_facts  # noqa: E402
from sources.shortline_live import render_live  # noqa: E402
from sources.shortline_structure import POOLS, render_structure  # noqa: E402
from sources.shortline_themes import render_firstboard, render_themes  # noqa: E402
from sources.shortline_watch import render_bigcaps, render_ladder, render_turnover_top  # noqa: E402

_UNSETTLED = "这一天还没收盘：数字收盘前还会变，本次结果不缓存"


class _Acc:
    """攒证据 / 缺口 / 降级原因,最后一次性出信封。"""

    def __init__(self, ctx: dict, day: str) -> None:
        self.ctx, self.day = ctx, day
        self.evs: list[dict] = []
        self.missing: list[dict] = []
        self.degraded: list[str] = []

    def num(self, field: str, value: Any, unit: str, *, period: Optional[str] = None, key: Optional[str] = None, note: Optional[str] = None) -> None:
        if value is None or isinstance(value, bool):
            return
        p = period or self.day
        # 区间期间(a..b)的 as_of 取区间末日
        self.evs.append(ev(self.ctx, field, value, unit, p, as_of=p.split("..")[-1], record_key=key, note=note))

    def text(self, field: str, value: Any, *, period: Optional[str] = None, key: Optional[str] = None, note: Optional[str] = None) -> None:
        if value in (None, ""):
            return
        self.num(field, str(value)[:300], "text", period=period, key=key, note=note)

    def block(self, name: str, d: Any) -> bool:
        """这一块能不能用;不能用就记缺口。"""
        if isinstance(d, dict) and d.get("available"):
            if d.get("partial"):
                self.degraded.append(f"{name}样本不全（{d.get('sample')}/{d.get('expected_sample')}）")
            return True
        reason = (d or {}).get("reason") if isinstance(d, dict) else None
        self.missing.append({"field": name, "period": self.day, "reason": reason or "未算出"})
        return False

    def finish(self, settled: bool, extra: dict, empty_reason: str) -> dict:
        if not self.evs:
            r = empty(empty_reason + ("：" + "；".join(m["reason"] for m in self.missing) if self.missing else ""))
            r["missing"] = self.missing
            return r
        if not settled:
            self.degraded.insert(0, _UNSETTLED)
        partial = bool(self.missing or self.degraded)
        return out(self.evs, extra=extra, missing=self.missing, status="partial" if partial else None,
                   degraded="；".join(self.degraded) or None)


def _src_note(d: dict) -> str:
    return "定稿记录（东财昨日涨停池当日表现）" if d.get("source") == "settled" else "腾讯实时行情兜底（已核对行情时间属于这一天）"


def _computation(fn: str, params: dict, note: str = "池子取东财涨停 / 炸板 / 跌停 / 昨日涨停池") -> dict:
    return {"kind": "computed_endpoint", "library": f"sources.{fn}", "params": params,
            "note": f"取数层确定性计算(非 calc DAG);移植自 Vibe AStock v2.0.0,{note}"}


# ---------------------------------------------------------------- 当天四项
def shortline_emotion_map(result: dict, ctx: dict) -> dict:
    if not result:
        return empty("情绪指标计算为空")
    day, prev = result["date"], result.get("prev_date")
    a = _Acc(ctx, day)

    me: dict = result.get("money_effect") or {}
    if a.block("money_effect", me):
        n = f"{prev} 涨停股在 {day} 的表现；{_src_note(me)}"
        a.num("money_effect_sample", me["sample"], "只", note=n)
        a.num("money_effect_avg_ret", me["avg"], "%", note=n)
        a.num("money_effect_median_ret", me["median"], "%", note=n)
        a.num("money_effect_positive_rate", me["positive_rate"], "小数", note=n)
        a.num("money_effect_limit_up_again_rate", me["limit_up_again_rate"], "小数", note=n)
        a.num("money_effect_coverage_rate", me.get("coverage_rate"), "小数", note="取到当日涨跌幅的只数 ÷ 名单只数")

    pr: dict = result.get("promotion") or {}
    if a.block("promotion", pr):
        n = f"{prev} 各档涨停股在 {day} 仍涨停的比例（以东财涨停池成员为准）"
        for tier, v in list(pr["tiers"].items()) + [("整体", pr["overall"])]:
            a.num("promotion_base", v["base"], "只", key=tier, note=n)
            a.num("promotion_promoted", v["promoted"], "只", key=tier, note=n)
            a.num("promotion_rate", v["rate"], "小数", key=tier, note=n)
        a.num("limit_up_count", pr.get("limit_up_count"), "只", note="东财涨停池只数")
        if prev:
            a.num("limit_up_count", pr.get("prev_limit_up_count"), "只", period=prev, note="前一交易日东财涨停池只数")

    cp: dict = result.get("consec_premium") or {}
    if a.block("consec_premium", cp):
        n = f"{prev} 的 2 板以上个股在 {day} 的表现；{_src_note(cp)}"
        a.num("consec_premium_sample", cp["sample"], "只", note=n)
        a.num("consec_premium_avg_ret", cp["avg"], "%", note=n)
        a.num("consec_premium_median_ret", cp["median"], "%", note=n)
        a.num("consec_premium_positive_rate", cp["positive_rate"], "小数", note=n)

    lg: dict = result.get("ladder_gap") or {}
    if a.block("ladder_gap", lg):
        a.num("ladder_highest", lg.get("highest"), "板")
        for b, c in (lg.get("tiers") or {}).items():
            a.num("ladder_tier_count", c, "只", key=f"{b}板")
        a.text("ladder_note", lg.get("note"))

    extra = {"date": day, "prev_date": prev, "settled": result.get("settled"), "text": render_metrics(result), "result": result,
             "computation": _computation("shortline_emotion.build_metrics", {"date": day})}
    return a.finish(bool(result.get("settled")), extra, "情绪指标四项都没算出来")


# ---------------------------------------------------------------- 每日六项读数
def shortline_daily_map(result: dict, ctx: dict) -> dict:
    """字段名和口径与情绪四项、事实表里的同名证据一致:同一天两边取到的是同一个数。"""
    if not result:
        return empty("每日情绪读数计算为空")
    day, prev = result["date"], result.get("prev_date")
    a = _Acc(ctx, day)
    r: dict = result.get("readings") or {}
    a.num("limit_up_count", r.get("limit_up"), "只", note="东财涨停池只数")
    a.num("ladder_highest", r.get("highest_board"), "板")
    a.num("broken_rate", r.get("broken_rate"), "小数", note="炸板 ÷ (涨停 + 炸板)")
    n = f"{prev} 涨停股在 {day} 的表现；定稿记录（东财昨日涨停池当日表现）"
    a.num("money_effect_sample", r.get("sample"), "只", note=n)
    a.num("money_effect_median_ret", r.get("money_effect"), "%", note=n)
    a.num("loss_deep5_count", r.get("deep_loss"), "只", note=n + "；跌幅 ≥ 5%")
    n = f"{prev} 首板在 {day} 仍涨停的比例（以东财涨停池成员为准）"
    a.num("promotion_base", r.get("promotion_base"), "只", key="1进2", note=n)
    a.num("promotion_promoted", r.get("promotion_promoted"), "只", key="1进2", note=n)
    a.num("promotion_rate", r.get("promotion_1to2"), "小数", key="1进2", note=n)
    fields = {"summary": "limit_up_count", "settled": "money_effect_median_ret", "settled_partial": "money_effect_sample", "promotion": "promotion_rate"}
    for k, why in (r.get("reasons") or {}).items():
        a.missing.append({"field": fields.get(k, k), "period": day, "reason": why})
    extra = {"date": day, "prev_date": prev, "settled": result.get("settled"), "text": render_daily(result), "result": result,
             "computation": _computation("shortline_emotion.build_daily", {"date": day})}
    return a.finish(bool(result.get("settled")), extra, "每日情绪读数一项都没算出来")


# ---------------------------------------------------------------- 昨日涨停股当日表现明细
def shortline_settled_map(result: dict, ctx: dict) -> dict:
    """逐只证据以代码作 record_key。是否再涨停写成「是 / 否」文字(布尔不进数字证据);判不了就不写。"""
    if not result:
        return empty("昨日涨停股当日表现为空")
    day, prev = result["date"], result.get("prev_date")
    a = _Acc(ctx, day)
    if a.block("settled_pool", result):
        a.num("settled_sample", result.get("sample"), "只", note=f"{prev} 收盘涨停、在 {day} 有表现记录的只数；定稿记录（东财昨日涨停池当日表现）")
        for r in result.get("rows") or []:
            k = r["code"]
            a.text("settled_name", r.get("name"), key=k)
            a.num("settled_ret", r.get("ret"), "%", key=k, note=f"{day} 涨跌幅")
            a.num("settled_prev_boards", r.get("prev_boards"), "板", key=k, note=f"{prev} 的连板数")
            a.text("settled_seal_time", r.get("seal_time"), key=k, note=f"{prev} 的最后封板时间（HHMMSS），不是首封")
            a.text("settled_sector", r.get("sector"), key=k, note="东财行业（不是题材）")
            if r.get("limit_up") is not None:
                a.text("settled_limit_up", "是" if r["limit_up"] else "否", key=k, note=f"{day} 收盘是否在涨停价（按这只票自己的涨跌幅制度）")
    extra = {"date": day, "prev_date": prev, "settled": result.get("settled"), "text": render_settled(result), "result": result,
             "computation": _computation("shortline_emotion.build_settled", {"date": day}, "取东财昨日涨停池")}
    return a.finish(bool(result.get("settled")), extra, "昨日涨停股当日表现没取到")


# ---------------------------------------------------------------- 涨停结构
def fields_text(fields: list[str]) -> str:
    """字段清单写成逗号连接的文字;超过 300 字(证据文字上限)写摘要,截断会被当成字段变了。"""
    s = ",".join(fields)
    if len(s) <= 300:
        return s
    return f"{len(fields)} 个字段,摘要 sha1:{hashlib.sha1(s.encode('utf-8')).hexdigest()[:12]}"


def shortline_structure_map(result: dict, ctx: dict) -> dict:
    """制度构成按类(record_key = 20% / 10% / 北交所),最高标按代码,字段清单按池子(record_key = zt / zb / dt / yzt)。

    池子为空看不出字段,只在 extra 里说明 —— 跌停池为空是正常的一天,标 partial 会让这天永远不进缓存;
    取数出错才进 missing(partial,下次重取)。
    """
    if not result:
        return empty("涨停结构为空")
    day = result["date"]
    a = _Acc(ctx, day)
    if a.block("structure", result):
        a.num("limit_up_count", result.get("limit_up"), "只", note="东财涨停池只数")
        n = "按当天的涨跌幅上限归类（10% 含主板 ST 当时的 5%）"
        for k, c in (result.get("mix") or {}).items():
            a.num("structure_mix_count", c, "只", key=k, note=n)
        a.num("structure_share_20cm", result.get("share_20cm"), "小数", note="20% 制度的涨停只数 ÷ 涨停池；" + n)
        a.num("structure_share_10cm", result.get("share_10cm"), "小数", note="10% 制度的涨停只数 ÷ 涨停池；" + n)
        a.num("structure_share_bj", result.get("share_bj"), "小数", note="北交所涨停只数 ÷ 涨停池")
        a.num("ladder_highest", result.get("highest_board"), "板")
        a.num("structure_leader_total", result.get("leader_total"), "只", note="连板数等于最高板的只数（明细最多列 5 只）")
        for x in result.get("leaders") or []:
            k = x["code"]
            a.text("leader_name", x.get("name"), key=k)
            a.num("leader_boards", x.get("boards"), "板", key=k, note="东财连板数")
            a.text("leader_sector", x.get("sector"), key=k, note="东财行业（不是题材）")
            a.text("leader_first_seal", x.get("first_seal"), key=k, note="首封时间（HHMMSS）")
        for kind, f in (result.get("fields") or {}).items():
            if f:
                a.num("raw_field_count", len(f), "个", key=kind, note=f"{POOLS.get(kind, kind)}上游原始行的键数")
                a.text("raw_fields", fields_text(f), key=kind, note="上游原始行的键（按字母排序，含 zttj 子键）；用来看数据源改没改字段")
        for kind, why in (result.get("fields_unknown") or {}).items():
            if why.startswith("没取到"):
                a.missing.append({"field": "raw_fields", "period": day, "reason": f"{POOLS.get(kind, kind)}{why}"})
    extra = {"date": day, "settled": result.get("settled"), "text": render_structure(result), "result": result,
             "fields_unknown": result.get("fields_unknown") or {},
             "computation": _computation("shortline_structure.build_structure", {"date": day})}
    return a.finish(bool(result.get("settled")), extra, "涨停结构没取到")


# ---------------------------------------------------------------- 情绪周期
def shortline_cycle_map(result: dict, ctx: dict) -> dict:
    if not result:
        return empty("情绪周期计算为空")
    day = result["date"]
    a = _Acc(ctx, day)
    cy: dict = result.get("cycle") or {}
    if a.block("cycle", cy):
        series = cy.get("series") or []
        cur = cy.get("current_date") or day
        per = f"{series[0]['date']}..{cur}" if series else cur
        n = f"{cy.get('window')} 个交易日窗口内的相对读数（0 = 窗口最冷，1 = 最热），没有绝对含义"
        a.num("cycle_current_score", cy.get("current_score"), "相对分", period=per, note=n)
        a.num("cycle_trough_score", cy.get("trough_score"), "相对分", period=per, note=n)
        a.text("cycle_trough_date", cy.get("trough_date"), period=per)
        a.num("cycle_day_n", cy.get("day_n"), "天", period=per, note="距窗口最低点第几天；不等于正在往上走")
        a.num("cycle_pctile", cy.get("pctile"), "小数", period=per, note=n)
        a.text("cycle_trend", cy.get("trend"), period=per)
        a.num("cycle_window", cy.get("window"), "个交易日", period=per,
              note=f"请求 {cy.get('requested_window')} 天，实际可用 {cy.get('window')} 天")
        for s in series:
            d = s["date"]
            a.num("cycle_day_score", s.get("score"), "相对分", period=d, note=n)
            a.num("cycle_day_limit_up", s.get("limit_up"), "只", period=d)
            a.num("cycle_day_highest_consec", s.get("highest_consec"), "板", period=d)
            a.num("cycle_day_broken_rate", s.get("broken_rate"), "小数", period=d, note="炸板 ÷ (涨停 + 炸板)")
        if cur != day:
            # 目标日的池子没算进来(取数失败,或盘中今天还没收盘):读数是截至前一天的,「今天」不在里面
            a.degraded.append(f"周期读数截至 {cur}，{day} 没算进来")
        if cy.get("window") and cy.get("requested_window") and cy["window"] < cy["requested_window"]:
            a.degraded.append(f"窗口只凑到 {cy['window']}/{cy['requested_window']} 个交易日（部分日子涨停池没取到）")
    extra = {"date": day, "text": render_cycle(cy or {}),
             "result": result, "computation": _computation("shortline_emotion.build_cycle", {"date": day, "lookback": ctx["args"].get("lookback", 10)})}
    return a.finish(bool(result.get("settled")), extra, "情绪周期没算出来")


# ---------------------------------------------------------------- 六张事实表
def shortline_facts_map(result: dict, ctx: dict) -> dict:
    if not result:
        return empty("盘面事实计算为空")
    day, prev = result["date"], result.get("prev_date")
    a = _Acc(ctx, day)

    sq: dict = result.get("seal_quality") or {}
    if a.block("seal_quality", sq):
        a.num("seal_total", sq["total"], "只")
        a.num("seal_never_broken", sq["never_broken"], "只", note="涨停后一次没开过板")
        a.num("seal_never_broken_rate", sq["never_broken_rate"], "小数")
        a.num("seal_early", sq["opening_seconds"], "只", note="09:35 前首封（含集合竞价一字）")
        a.num("seal_late", sq["late_seal"], "只", note="14:30 后才最终封住")
        a.num("seal_reopened", sq["reopened"], "只", note="开过板又回封")
        a.num("seal_avg_broken_times", sq["avg_broken_times"], "次")
        a.num("broken_pool_count", sq["broken_pool"], "只", note="东财炸板池")
        a.num("broken_rate", sq["broken_rate"], "小数", note="炸板 ÷ (涨停 + 炸板)")
        a.num("seal_ever_opened_rate", sq["ever_opened_rate"], "小数", note="涨停股里盘中开过板的比例，不是炸板率")

    le: dict = result.get("loss_effect") or {}
    if a.block("loss_effect", le):
        n = f"{prev} 涨停股在 {day} 的表现；{_src_note(le)}"
        a.num("loss_sample", le["sample"], "只", note=n)
        a.num("loss_deep5_count", le["deep_loss_5_count"], "只", note=n + "；跌幅 ≥ 5%")
        a.num("loss_deep5_rate", le["deep_loss_5_rate"], "小数", note=n)
        a.num("loss_deep7_count", le["deep_loss_7_count"], "只", note=n + "；跌幅 ≥ 7%")
        a.num("loss_limit_down_count", le["limit_down_count"], "只", note=n + "；按各自涨跌幅制度判跌停")
        a.num("loss_worst_ret", le["worst"], "%", note=n)
        a.num("limit_down_count", le.get("market_limit_down"), "只", note="全市场东财跌停池（不限于昨日涨停股）")
        rec = le.get("prev_broken_recovery")
        if isinstance(rec, dict):
            a.num("prev_broken_recovery_avg_ret", rec.get("avg"), "%", note=f"{prev} 炸板股在 {day} 的平均涨跌")
            a.num("prev_broken_recovery_positive_rate", rec.get("positive_rate"), "小数", note=f"{prev} 炸板股在 {day} 收红的比例")

    fb: dict = result.get("feedback") or {}
    if a.block("feedback", fb):
        n = f"{prev} 涨停股按昨日板位分组，看 {day} 的结果；{_src_note(fb)}"
        order = fb.get("result_order") or []
        for tier, cell in (fb.get("matrix") or {}).items():
            for res in order:
                a.num("feedback_count", cell.get(res), "只", key=f"{tier}|{res}", note=n)
            a.num("feedback_tier_total", cell.get("合计"), "只", key=tier, note=n)
            a.num("feedback_tier_promotion_rate", cell.get("晋级率"), "小数", key=tier, note=n)

    th: dict = result.get("themes") or {}
    if a.block("themes", th):
        a.num("theme_count", th.get("theme_count"), "个", note="有涨停的东财所属行业个数")
        a.num("theme_concentration", th.get("concentration"), "小数", note="第一方向涨停 ÷ 全部涨停")
        for t in th.get("themes") or []:
            k = t["sector"]
            a.num("theme_limit_up", t["limit_up"], "只", key=k)
            a.num("theme_highest", t["highest"], "板", key=k)
            a.num("theme_broken", t["broken"], "只", key=k)
            a.num("theme_broken_rate", t.get("broken_rate"), "小数", key=k)
            a.text("theme_first_seal", t.get("first_seal"), key=k, note="该方向第一只涨停的首封时间（HHMMSS）")
        for s in th.get("timeline") or []:
            a.num("limit_up_by_slot", s["count"], "只", key=s["slot"], note="按首封时间分段")

    evs: dict = result.get("events") or {}
    if a.block("events", evs):
        for e in evs.get("events") or []:
            k = f"{e['tag']}|{e['code']}"
            a.text("event", f"{e['tag']}｜{e['name']}（{e['code']}）｜{e['note']}", key=k, note=f"{e.get('sector') or '未分类'}；{e.get('board')}")
            a.num("event_ret", e.get("ret"), "%", key=k)
        if evs.get("prev_available") is False:
            # 断板 / 高标跌停要靠昨日板数分档;没取到就标缺口,信封是「部分」、不进缓存
            a.missing.append({"field": "event_prev_boards", "period": day, "reason": "前一交易日涨停池未取得，断板事件没法按昨日板数分档"})

    bb: dict = result.get("by_board") or {}
    if a.block("by_board", bb):
        for b, g in (bb.get("boards") or {}).items():
            a.num("board_limit_up", g["limit_up"], "只", key=b)
            a.num("board_highest", g["highest"], "板", key=b)
            a.num("board_broken", g["broken"], "只", key=b)
            a.num("board_limit_down", g["limit_down"], "只", key=b)
            if bb.get("prev_available", True):
                a.num("board_prev_base", g["prev_base"], "只", key=b, note=f"{prev} 涨停只数")
                a.num("board_promoted", g["promoted"], "只", key=b)
                a.num("board_promotion_rate", g.get("promotion_rate"), "小数", key=b)
        if not bb.get("prev_available", True):
            a.missing.append({"field": "board_promotion_rate", "period": day, "reason": "前一交易日涨停池未取得"})

    extra = {"date": day, "prev_date": prev, "settled": result.get("settled"), "text": render_facts(result), "result": result,
             "computation": _computation("shortline_facts.build_facts", {"date": day})}
    return a.finish(bool(result.get("settled")), extra, "盘面事实六张表都没算出来")


# ---------------------------------------------------------------- 题材事件树
def shortline_themes_map(result: dict, ctx: dict) -> dict:
    if not result:
        return empty("题材事件树计算为空")
    day, prev = result["date"], result.get("prev_date")
    a = _Acc(ctx, day)
    tree: dict = result.get("tree") or {}
    if a.block("tree", tree):
        src = tree.get("source_note") or ""
        a.num("topic_count", tree.get("tag_count"), "个", note="当天有涨停或昨日有涨停的事件题材个数（已滤掉国企改革、破净等属性词）")
        a.num("topic_concentration", tree.get("concentration"), "小数", note="第一题材涨停 ÷ 全部涨停")
        a.num("reason_covered", tree.get("covered"), "只", note="涨停原因对上当天东财涨停池的只数；" + src)
        a.num("reason_coverage_rate", tree.get("coverage_rate"), "小数", note="涨停原因覆盖 ÷ 东财涨停只数")
        a.num("prev_reason_covered", tree.get("prev_covered"), "只", note=f"{prev} 涨停股里有事件题材的只数")
        a.num("prev_limit_up_total", tree.get("prev_total"), "只", note=f"{prev} 东财涨停池只数")
        n = f"按涨停原因串归组（一只票可属于多个题材）；昨日 = {prev}"
        for t in tree.get("themes") or []:
            k = t["tag"]
            a.text("topic_state", t["state"], key=k, note="只由已发生的读数推出，不含对明天的判断")
            a.num("topic_limit_up", t["limit_up"], "只", key=k, note=n)
            a.num("topic_first_boards", t["first_boards"], "只", key=k)
            a.num("topic_consec_boards", t["consec_boards"], "只", key=k)
            a.num("topic_highest", t["highest"], "板", key=k)
            a.num("topic_broken", t["broken"], "只", key=k, note="东财炸板池里属于这个题材的（今天没涨停的用昨日题材串归组）")
            a.num("topic_limit_down", t["limit_down"], "只", key=k)
            a.num("topic_broken_rate", t.get("broken_rate"), "小数", key=k, note="炸板 ÷ (涨停 + 炸板)")
            a.text("topic_first_seal", t.get("first_seal"), key=k, note="该题材第一只涨停的首封时间（HHMMSS）")
            a.text("topic_last_seal", t.get("last_seal"), key=k, note="该题材最后一只涨停的首封时间（HHMMSS）")
            if t["prev_members"]:
                a.num("topic_prev_members", t["prev_members"], "只", key=k, note=f"{prev} 这个题材的涨停只数")
                a.num("topic_prev_still_up", t["prev_still_up"], "只", key=k, note=f"其中 {day} 仍涨停的只数")
                a.num("topic_continuation_rate", t.get("continuation_rate"), "小数", key=k)
            names = "、".join(f"{m['name']}（{m['code']}）{m['label']}" for m in t.get("members") or [])
            a.text("topic_members", names, key=k, note="按有效高度排的前 8 只")
        if not tree.get("has_prev"):
            a.degraded.append("昨日涨停池或昨日题材串没取到，题材新旧与延续没法判断")
    extra = {"date": day, "prev_date": prev, "settled": result.get("settled"), "text": render_themes(result), "result": result,
             "computation": _computation("shortline_themes.build_themes", {"date": day, "top": result.get("top")},
                                         "题材串取问财(配了 IWENCAI_API_KEY)或同花顺涨停揭秘,池子取东财涨停 / 炸板 / 跌停池")}
    return a.finish(bool(result.get("settled")), extra, "题材事件树没算出来")


# ---------------------------------------------------------------- 首板名单
def shortline_firstboard_map(result: dict, ctx: dict) -> dict:
    if not result:
        return empty("首板名单计算为空")
    day = result["date"]
    a = _Acc(ctx, day)
    if a.block("firstboard", result):
        a.num("limit_up_count", result.get("total_limit_up"), "只", note="东财涨停池只数")
        a.num("first_board_count", result.get("first_count"), "只", note="东财连板数 = 1（含反包）")
        a.num("first_board_rebound_count", result.get("rebound_count"), "只", note="连板数 1 但涨停统计 N 天 M 板（M ≥ 2）的反包票")
        if result.get("reason_available"):
            a.num("first_board_reason_covered", result.get("reason_covered"), "只", note=result.get("reason_note"))
        else:
            a.missing.append({"field": "first_board_reason", "period": day, "reason": result.get("reason_note") or "涨停原因没取到"})
        for s in result.get("stocks") or []:
            k = s["code"]
            a.text("first_board", f"{s['name']}（{k}）｜{s['label']}｜{s['sector'] or '未分类'}", key=k)
            a.text("first_board_seal_time", s.get("first_seal"), key=k, note="首次封板时间（HHMMSS）")
            a.num("first_board_broken_times", s.get("broken_times"), "次", key=k, note="当天开板次数")
            a.num("first_board_ret", s.get("ret"), "%", key=k)
            a.num("first_board_amount", s.get("amount"), "元", key=k, note="成交额")
            a.num("first_board_float_cap", s.get("float_cap"), "元", key=k, note="流通市值")
            a.text("first_board_reason", s.get("reason"), key=k, note="源站编辑归因，非公司确认")
    extra = {"date": day, "settled": result.get("settled"), "text": render_firstboard(result), "result": result,
             "computation": _computation("shortline_themes.build_firstboard", {"date": day},
                                         "名单取东财涨停池,原因取问财(配了 IWENCAI_API_KEY)或同花顺涨停揭秘")}
    return a.finish(bool(result.get("settled")), extra, "首板名单没算出来")


# ---------------------------------------------------------------- 盘中情绪
def shortline_live_map(result: dict, ctx: dict) -> dict:
    """盘中快照。🔴 不按「没收盘就 partial」处理:新鲜度靠注册表 cache_max_age_sec = 15 控制(快照 15 秒后作废,隔天更用不上)。"""
    if not result:
        return empty("盘中情绪计算为空")
    day = result["date"]
    a = _Acc(ctx, day)
    v: dict = result.get("live") or {}
    n = f"盘中快照 {result.get('as_of')}（{result.get('phase')}），随盘变化"
    a.num("limit_up_count", v.get("limit_up"), "只", note=n)
    a.num("break_board_count", v.get("broken"), "只", note=n + "；东财炸板池（炸板未回封）")
    a.num("limit_down_count", v.get("limit_down"), "只", note=n)
    a.num("ladder_highest", v.get("highest_board"), "板", note=n)
    a.num("consec_count", v.get("consec_count"), "只", note=n + "；连板数 ≥ 2")
    a.num("seal_rate", v.get("seal_rate"), "小数", note=n + "；封住 ÷ (封住 + 炸板未回封)")
    a.num("broken_rate", v.get("broken_rate"), "小数", note=n + "；炸板 ÷ (涨停 + 炸板)")
    prev = result.get("prev_date")
    a.num("promotion_base", v.get("promotion_all_base"), "只", key="昨日全部涨停", note=f"{prev} 涨停只数")
    a.num("promotion_promoted", v.get("promotion_all_promoted"), "只", key="昨日全部涨停", note=n + "；此刻仍封住")
    a.num("promotion_rate", v.get("promotion_all"), "小数", key="昨日全部涨停", note=n)
    a.num("promotion_base", v.get("promotion_base"), "只", key="1进2", note=f"{prev} 首板只数")
    a.num("promotion_promoted", v.get("promotion_promoted"), "只", key="1进2", note=n)
    a.num("promotion_rate", v.get("promotion_1to2"), "小数", key="1进2", note=n + "；与复盘同口径（以东财涨停池成员为准）")
    m = f"{prev} 涨停股此刻的涨跌幅；{n}"
    a.num("money_effect_sample", v.get("sample"), "只", note=m)
    a.num("money_effect_median_ret", v.get("money_effect"), "%", note=m)
    a.num("loss_deep5_count", v.get("deep_loss"), "只", note=m + "；跌幅 ≥ 5%")
    fields = {"promotion": ("promotion_rate",), "settled": ("money_effect_median_ret", "loss_deep5_count"), "settled_partial": ("money_effect_sample",),
              "zb": ("break_board_count", "seal_rate", "broken_rate"), "dt": ("limit_down_count",)}
    for k, why in (result.get("reasons") or {}).items():
        a.missing.extend({"field": f, "period": day, "reason": why} for f in fields.get(k, (k,)))
    extra = {"date": day, "as_of": result.get("as_of"), "phase": result.get("phase"), "prev_date": prev, "settled": result.get("settled"),
             "text": render_live(result), "result": result, "computation": _computation("shortline_live.build_live", {})}
    return a.finish(True, extra, "盘中情绪一项都没算出来")


# ---------------------------------------------------------------- 盯盘名单
def shortline_ladder_map(result: dict, ctx: dict) -> dict:
    """昨日梯队:样本日(前一交易日)已收盘,结果不变。"""
    if not result:
        return empty("昨日梯队计算为空")
    day = result["sample_date"]
    a = _Acc(ctx, day)
    n = f"{day} 东财涨停池，盯 {result['quote_date']} 这一场"
    a.num("ladder_sample_count", result.get("total"), "只", note=n)
    a.num("ladder_highest", result.get("highest"), "板", note=n)
    for t in result.get("tiers") or []:
        a.num("ladder_tier_count", t["count"], "只", key=f"{t['boards']}板", note=n)
    for st in result.get("stocks") or []:
        a.num("ladder_boards", st["boards"], "板", key=st["code"], note=f"{st['name']}｜{st['label']}｜{st['sector'] or '—'}")
    extra = {"quote_date": result["quote_date"], "sample_date": day, "text": render_ladder(result), "result": result,
             "computation": _computation("shortline_watch.build_ladder", {"date": result["quote_date"]}, note="东财涨停池(前一交易日)")}
    return a.finish(True, extra, "昨日梯队一只都没有")


def shortline_turnover_top_map(result: dict, ctx: dict) -> dict:
    """某日成交额前 N:字段名和单位与 em_turnover_rank 一致(record_key = stock|代码),期间是那一天。"""
    if not result:
        return empty("成交额前 N 计算为空")
    day = result["date"]
    a = _Acc(ctx, day)
    for st in result.get("stocks") or []:
        k, n = f"stock|{st['code']}", f"{st['name']}({st['code']})；{day} 成交额第 {st['rank']} 名（通达信盘后包，有成交的 A 股 {result.get('total_traded')} 只）"
        a.num("turnover_amount", st["amount"], "元", key=k, note=n)
        a.num("turnover_rank", st["rank"], "名", key=k, note=n)
        a.num("close", st.get("close"), "元", key=k, note=n)
        a.num("change_pct", st.get("pct"), "%", key=k, note=n + "；收盘价 ÷ 前收 − 1")
    extra = {"date": day, "top": result.get("top"), "text": render_turnover_top(result), "result": result,
             "computation": _computation("shortline_watch.build_turnover_top", {"date": day, "top": result.get("top")}, note=f"通达信盘后包 {result.get('source_url')}")}
    return a.finish(True, extra, "成交额前 N 一只都没有")


def shortline_bigcaps_map(result: dict, ctx: dict) -> dict:
    """大市值名单:只出总市值(价格和涨跌幅界面另取实时行情)。新鲜度靠注册表 cache_max_age_sec。"""
    if not result:
        return empty("大市值名单为空")
    day = result["date"]
    a = _Acc(ctx, day)
    n = f"新浪总市值降序（{day} 行情，{result.get('as_of')} 取）；门槛 {result.get('min_cap_yi'):g} 亿"
    a.num("bigcap_count", result.get("count"), "只", note=n)
    for st in result.get("stocks") or []:
        a.num("market_cap", st["mcap"], "元", key=f"stock|{st['code']}", note=f"{st['name']}({st['code']})；{n}")
    extra = {"date": day, "as_of": result.get("as_of"), "min_cap_yi": result.get("min_cap_yi"), "text": render_bigcaps(result), "result": result,
             "computation": _computation("shortline_watch.build_bigcaps", {"min_cap_yi": result.get("min_cap_yi")}, note="新浪 hs_a 节点按总市值降序翻页")}
    return a.finish(True, extra, "大市值名单一只都没有")


# ---------------------------------------------------------------- 连板天梯
def _codes(codes: list[str]) -> str:
    return "、".join(codes[:5]) + (" 等" if len(codes) > 5 else "")


def shortline_board_ladder_map(result: dict, ctx: dict) -> dict:
    if not result:
        return empty("连板天梯计算为空")
    day, prev = result["date"], result.get("prev_date")
    a = _Acc(ctx, day)
    lad: dict = result.get("ladder") or {}
    if a.block("board_ladder", lad):
        a.num("limit_up_count", lad.get("total_limit_up"), "只", note="东财涨停池只数")
        a.num("ladder_highest", lad.get("highest"), "板")
        a.num("consec_count", lad.get("consec_count"), "只", note="东财连板数 ≥ 2")
        for lv in lad.get("levels") or []:
            k = f"{lv['boards']}板"
            a.num("ladder_tier_count", len(lv["stocks"]), "只", key=k)
            if lv.get("base"):
                n = f"{prev} 连板数 {lv['boards'] - 1} 的涨停股，{day} 仍在涨停池里的只数"
                a.num("ladder_tier_base", lv["base"], "只", key=k, note=f"{prev} 连板数 {lv['boards'] - 1} 的涨停只数")
                a.num("ladder_tier_promoted", lv["promoted"], "只", key=k, note=n)
                a.num("ladder_tier_promotion_rate", lv.get("rate"), "小数", key=k, note=n)
            for st in lv["stocks"]:
                a.text("ladder_stock", f"{st['name']}（{st['code']}）｜{st['label']}｜首封 {st.get('first_seal') or '—'}"
                       f"｜开板 {st['broken_times']} 次｜{st['reason'] or ('同花顺不收录北交所' if st.get('board') == '北交所' else '原因没取到')}", key=st["code"], note="原因是源站编辑归因，非公司确认")
            for f in lv["fell"]:
                a.text("ladder_fell", f"{f['name']}（{f['code']}）｜昨日 {f['prev_label']}｜今天{f['outcome']}", key=f["code"])
                a.num("ladder_fell_ret", f.get("ret"), "%", key=f["code"], note=f"{prev} 涨停、{day} 没再涨停的当天涨跌幅（定稿记录优先）")
        if not lad.get("prev_available"):
            a.missing.append({"field": "ladder_tier_promotion_rate", "period": day, "reason": lad.get("prev_note") or "前一交易日涨停池未取得"})
        # 没接上的票去向 / 涨跌幅没取到、沪深涨停股缺原因 = 缺数,不能当完整的结果长期缓存
        unknown = [f["code"] for lv in lad.get("levels") or [] for f in lv["fell"] if f["ret"] is None]
        if unknown:
            a.missing.append({"field": "ladder_fell_ret", "period": day,
                              "reason": f"{len(unknown)} 只昨天涨停、今天没再涨停的票当天表现没取到（{_codes(unknown)}）"})
        if lad.get("reason_missing"):
            a.missing.append({"field": "ladder_stock_reason", "period": day,
                              "reason": f"{len(lad['reason_missing'])} 只沪深涨停股没有涨停原因（{_codes(lad['reason_missing'])}）"})
        if not lad.get("reason_available"):
            a.missing.append({"field": "ladder_stock_reason", "period": day, "reason": lad.get("reason_note") or "涨停原因没取到"})
    extra = {"date": day, "prev_date": prev, "settled": result.get("settled"), "text": render_board_ladder(result), "result": result,
             "computation": _computation("shortline_boards.build_board_ladder", {"date": day},
                                         "池子取东财涨停 / 炸板 / 跌停 / 昨日涨停池,原因取问财(配了 IWENCAI_API_KEY)或同花顺涨停揭秘")}
    return a.finish(bool(result.get("settled")), extra, "连板天梯没算出来")


# ---------------------------------------------------------------- 题材轮动
def shortline_theme_rotation_map(result: dict, ctx: dict) -> dict:
    if not result:
        return empty("题材轮动计算为空")
    day = result["date"]
    a = _Acc(ctx, day)
    rot: dict = result.get("rotation") or {}
    if a.block("theme_rotation", rot):
        dates = rot["dates"]
        span = f"{dates[0]}..{dates[-1]}"
        for d in rot.get("days") or []:
            if d["available"]:
                a.num("limit_up_count", d["limit_up"], "只", period=d["date"], note="东财涨停池只数")
                a.num("reason_covered", d["covered"], "只", period=d["date"], note="涨停原因里有事件题材、且对上东财涨停池的只数")
            else:
                a.missing.append({"field": "rotation_day", "period": d["date"], "reason": d["reason"]})
            if d.get("unknown"):
                a.missing.append({"field": "rotation_day", "period": d["date"],
                                  "reason": f"{d['unknown']} 只沪深涨停股没有涨停原因：这天没出现的题材按缺数处理，出现的只数是下限"})
        for t in rot.get("themes") or []:
            k = t["tag"]
            for d, v, rk in zip(dates, t["series"], t["ranks"]):   # 缺数的那天是 None,a.num 不写
                a.num("rotation_topic_limit_up", v, "只", period=d, key=k, note="这个题材当天的涨停只数（按涨停原因标签归组）")
                a.num("rotation_topic_rank", rk, "名", period=d, key=k, note="当天按涨停只数排第几（并列同名次）")
            a.text("rotation_topic_status", t["status"], period=span, key=k, note="只由这几天的读数推出，不含对明天的判断")
            a.num("rotation_topic_active_days", t["active_days"], "天", period=span, key=k)
            a.num("rotation_topic_top3_days", t["top3_days"], "天", period=span, key=k)
    extra = {"date": day, "days": result.get("days"), "settled": result.get("settled"), "text": render_theme_rotation(result), "result": result,
             "computation": _computation("shortline_boards.build_theme_rotation", {"date": day, "days": result.get("days")},
                                         "每天的东财涨停池 + 问财(配了 IWENCAI_API_KEY)或同花顺涨停揭秘的涨停原因")}
    return a.finish(bool(result.get("settled")), extra, "题材轮动没算出来")


# ---------------------------------------------------------------- 情绪周期状态机(移植)
def shortline_state_map(result: dict, ctx: dict) -> dict:
    """六态状态机(移植自开源复盘看板 v4c,经 Vibe AStock v2.0.0 duanxian/emotion_state)。

    读数本身来自 cn_shortline_daily 同一口径的 daily_readings,这里不重复出
    涨停数 / 炸板率等读数证据(同一天同名字段在别的端点里已有);状态机只出
    自己的增量:S 综合分、候选态 / 确认态、近 20 天确认态轨迹。
    """
    if not result:
        return empty("情绪状态机计算为空")
    day = result["date"]
    if not result.get("available"):
        return empty(f"情绪状态机不可用:{result.get('reason', '未知')}")
    a = _Acc(ctx, day)
    n = ("综合情绪分 S:组内平均、组间加权的历史 rank 分位合成(0-100),"
         "分位基线 " + str(result.get("sample_days")) + " 个交易日;"
         "阈值沿用 dashboard 冻结值,样本通常不足以真正标定")
    a.num("emotion_s", result.get("s"), "分位", note=n)
    a.text("emotion_state_candidate", result.get("candidate"),
           note="候选态(六态:冰点/退潮/分歧/高潮/启动/发酵;防御优先)")
    a.text("emotion_state_confirmed", result.get("confirmed"),
           note="确认态:转防御立即生效,转进攻需 T+1 与前一候选态一致(加仓要确认,减仓不等待)")
    a.num("emotion_state_sample_days", result.get("sample_days"), "个交易日",
          note=f"请求 {result.get('requested_days')} 天,实际可用 {result.get('sample_days')} 天;判定下限 {result.get('min_baseline')} 天")
    for h in result.get("history") or []:
        if h.get("state"):
            a.text("emotion_state_day", h["state"], period=h["date"], note="当日确认态")
        if h.get("s_pct") is not None:
            a.num("emotion_state_day_s", h["s_pct"], "分位", period=h["date"], note="当日综合情绪分")
    if not result.get("baseline_ok"):
        a.degraded.append(f"分位基线不足({result.get('sample_days')} < {result.get('min_baseline')} 个交易日),状态仅供参考")
    extra = {"date": day, "settled": result.get("settled"), "text": render_state(result), "result": result,
             "computation": _computation("shortline_state.build_state",
                                         {"date": day, "days": result.get("requested_days")},
                                         "六项读数逐日取东财涨停 / 炸板 / 昨日涨停池(daily_readings 同口径)")}
    return a.finish(bool(result.get("settled")), extra, "情绪状态机没算出来")


# ---------------------------------------------------------------- 盘外增强源(移植)
def shortline_extra_map(result: dict, ctx: dict) -> dict:
    """开盘啦 / 短线侠 / hhxg 三路盘外读数,各块独立可用。

    🔴 三家都只有**当下**(KPL 按日期回显核对、短线侠仅当日实时、hhxg 仅最新
    一期),新鲜度靠注册表 cache_max_age_sec;目标日不是今天时三块都按缺数报。
    口径与主取数层(东财池 / 问财)不同,证据字段一律带 extra_ 前缀防混用。
    """
    if not result:
        return empty("盘外增强读数为空")
    day = result["date"]
    a = _Acc(ctx, day)

    k: dict = result.get("kpl_mood") or {}
    if a.block("kpl_mood", k):
        n = f"开盘啦 App 接口({k.get('date', '')} 场次,日期回显已核对);与东财池口径不同,只作交叉参考"
        a.num("extra_kpl_limit_up_count", k.get("limit_up_count"), "只", note=n)
        a.num("extra_kpl_max_consec_boards", k.get("max_consec_boards"), "板", note=n)
        a.num("extra_kpl_sentiment", k.get("sentiment"), "值", note=n + ";KPL 自有情绪值,没有绝对含义")
        a.num("extra_kpl_drawdown_count", k.get("drawdown_count"), "只", note=n + ";当日大幅回撤家数")
        a.text("extra_kpl_tip", k.get("tip"), note="KPL 端提示语,原样透传")

    z: dict = result.get("ztlive") or {}
    if a.block("ztlive", z):
        n = "短线侠涨停播报(仅当日实时,条目无日期字段);涨停原因是源站编辑归因,题材串与问财不同源"
        a.num("extra_ztlive_count", z.get("count"), "只", note=n)
        for r in z.get("rows") or []:
            a.text("extra_ztlive_stock", f"{r['name']}（{r['code']}）｜{r['boards'] or '—'}板｜{r['time'] or '—'}｜{r['reason'] or '原因未取到'}",
                   key=r["code"], note=n)

    h: dict = result.get("theme_heat") or {}
    if a.block("theme_heat", h):
        n = f"hhxg 题材热度榜({h.get('date', '')} 这一期,仅最新、无历史路径);schema v3:yz=溢价、zt=涨停家数、ratio=占比"
        for it in h.get("items") or []:
            k_key = it["name"]
            a.num("extra_theme_limit_up", it.get("limit_up"), "只", key=k_key, note=n)
            a.num("extra_theme_premium", it.get("premium"), "%", key=k_key, note=n)
            a.num("extra_theme_ratio", it.get("ratio"), "小数", key=k_key, note=n)
    if not result.get("is_today"):
        a.degraded.append(f"盘外三源都只有当下数据,目标日 {day} 不是今天,全部按缺数处理")
    extra = {"date": day, "settled": result.get("settled"), "text": render_extra(result), "result": result,
             "computation": _computation("shortline_extra.build_extra", {"date": day},
                                         "盘外三源:开盘啦 App 接口 + 短线侠涨停播报 + hhxg 题材热度榜")}
    return a.finish(True, extra, "盘外三路读数都不可用")

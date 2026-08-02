"""题材事件树 —— 按**真实事件题材**复盘，不是按行业分类。


"电网设备""元件""IT服务"是东财的行业分类，**不是打板选手复盘时说的题材**。
同一个行业里可能装着完全不同的炒作原因；同一个题材又常横跨好几个行业。

问财的涨停原因串给的才是真东西：

```
000417: 间接投资长鑫科技+百货零售+合肥国资
000670: 存储芯片+电子元器件分销+重大资产重组
000533: 数据中心+干式变压器+电网设备+控制权变更预期
```

「间接投资长鑫科技」「存储芯片」「控制权变更预期」—— 这些是事件，行业分类里找不到。

## 这棵树回答什么

对每个题材，按短线选手的复盘顺序摆事实：

```
昨日该题材强势股今天什么反馈 → 今天几点首封 → 扩散到几只 → 连板梯队多高
→ 炸了几个 / 有没有大面 → 次日该看什么验证它延不延续
```

## 数据与降级

题材串来自问财（`fetchers.fetch_zt_reasons`），按交易日查，实测能回溯到一年前。
每天复盘时仍**落盘囤起来**——省一次请求、也让没配 `IWENCAI_API_KEY` 的时候
历史场次照样看得到。缓存没有就现查，查回来的东西由 `fetch_zt_reasons`
用返回列名里的日期核对过场次，不会把别的交易日的题材塞进来。

拿不到题材串时整棵树标 unavailable，**绝不退回行业分类冒充题材** ——
那正是这个模块要解决的问题。

## 合规

全部是当天已发生的客观事实汇总（哪些票涨停、属于什么题材、几点封的板、炸了几个）。
不预测、不给个股倾向、不做品种选择。题材层面的"延续性"只陈述已发生的读数变化。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from . import trade_calendar
from .util import atomic_write_json

_CACHE_DIR = os.path.expanduser("~/.duanxian-agents/cache/zt_reasons")
_SCHEMA = 1

_GENERIC = {
    "国企改革", "央企改革", "破净", "低价股", "高送转", "摘帽", "壳资源",
    "小盘股", "微盘股", "次新股", "超跌反弹", "股权激励", "员工持股",
}
# 地方国资类（xx国资 / xx国企）也属于属性词，用后缀判
_GENERIC_SUFFIX = ("国资", "国企", "板块")


def _is_generic(tag: str) -> bool:
    return tag in _GENERIC or tag.endswith(_GENERIC_SUFFIX)


def reasons_of(date: str) -> tuple[dict[str, str], Optional[str]]:
    """某日的 代码→题材串。先读缓存，没有就按那一天现查并落盘。"""
    path = os.path.join(_CACHE_DIR, f"{date}.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                env = json.load(fh)
            if env.get("schema") == _SCHEMA and env.get("date") == date:
                return env.get("reasons") or {}, None
        except Exception:  # noqa: BLE001
            pass

    try:
        from . import fetchers as dr

        reasons, err = dr.fetch_zt_reasons(date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"
    if not reasons:
        return {}, err or "问财未返回题材串"
    # 只有定稿的日子才落盘（同 emotion_metrics 的判据）
    if trade_calendar.is_settled(date):
        atomic_write_json(path, {"schema": _SCHEMA, "date": date, "reasons": reasons})
    return reasons, None


def capture(date: Optional[str] = None) -> dict:
    """囤最近已收盘交易日的题材串（复盘跑完时调用，1 次请求）"""
    date = date or trade_calendar.latest_session()
    if not date:
        return {"ok": False, "reason": "取不到最近已收盘交易日（可能非交易日或未收盘）"}
    reasons, err = reasons_of(date)
    return {"ok": bool(reasons), "date": date, "count": len(reasons), "reason": err}


def _tags(reason: str) -> list[str]:
    """题材串 → tag 列表。过滤纯属性词，保留事件词。"""
    out = []
    for t in str(reason or "").split("+"):
        t = t.strip()
        if t and not _is_generic(t):
            out.append(t)
    return out


def build(date: str, prev: Optional[str] = None, top: int = 10) -> dict:
    """搭当日的题材事件树"""
    from . import market_facts as mf

    p = mf.pools(date)
    if p is None:
        return {"available": False, "reason": f"{date} 涨停池取数失败"}
    reasons, err = reasons_of(date)
    if not reasons:
        return {"available": False,
                "reason": f"题材串不可用（{err}）。⚠️ 不用行业分类顶替 —— 行业≠题材。"}
    matched = 0      # 题材串里真正对上今日涨停池的只数（问财口径与东财涨停池会有出入）

    prev = prev or trade_calendar.prev_trade_date(date)
    zt, zb, dt = p["zt"], p["zb"], p["dt"]
    zt_by_code = {r["code"]: r for r in zt}
    today_zt_codes = set(zt_by_code)
    today_dt_codes = {r["code"] for r in dt}
    zb_codes = {r["code"] for r in zb}

    # 昨日各票的题材与板位 —— 用来算"这个题材昨天的强势股今天怎么样"
    prev_reasons, _prev_err = reasons_of(prev) if prev else ({}, "无前一交易日")
    has_prev_reasons = bool(prev_reasons)
    pp = mf.pools(prev) if prev else None
    prev_boards = {r["code"]: int(r.get("boards") or 1) for r in (pp["zt"] if pp else [])}

    groups: dict[str, dict] = {}

    def g(tag: str) -> dict:
        return groups.setdefault(tag, {
            "tag": tag, "limit_up": 0, "first_boards": 0, "consec_boards": 0,
            "highest": 0, "broken": 0, "limit_down": 0,
            "seal_times": [], "members": [],
            "prev_members": 0, "prev_still_up": 0, "prev_broken_or_down": 0,
        })

    # 今天的涨停股按题材归组
    for code, reason in reasons.items():
        r = zt_by_code.get(str(code).zfill(6))
        if r is None:
            continue                 # 问财有、东财涨停池没有 → 不计入覆盖
        matched += 1
        b = int(r.get("boards") or 1)
        stat = str(r.get("zt_stat") or "") or None
        t = str(r.get("first_seal") or "").strip()
        for tag in _tags(reason):
            grp = g(tag)
            grp["limit_up"] += 1
            grp["highest"] = max(grp["highest"], b)
            if b <= 1:
                grp["first_boards"] += 1
            else:
                grp["consec_boards"] += 1
            if t:
                grp["seal_times"].append(t)
            grp["members"].append({
                "code": r["code"], "name": r["name"], "boards": b,
                "zt_stat": stat, "label": mf.board_label(b, stat),
                # 反包票（3天2板）东财连板数只给 1，排序按有效高度，别沉到榜尾
                "_h": max(b, mf.stat_boards(stat)),
                "first_seal": t or None, "broken_times": int(r.get("broken_times") or 0),
            })

    # 今天炸板/跌停的票也归到它们的题材下（用昨日题材串兜底 —— 今天没涨停就没有今日串）
    for codes, key in ((zb_codes, "broken"), (today_dt_codes, "limit_down")):
        for code in codes:
            src = reasons.get(code) or prev_reasons.get(code) or ""
            for tag in _tags(src):
                if tag in groups:
                    groups[tag][key] += 1

    # 题材延续性：昨天这个题材里的涨停股，今天还在不在
    for code, reason in (prev_reasons or {}).items():
        code = str(code).zfill(6)
        if code not in prev_boards:
            continue
        for tag in _tags(reason):
            if tag not in groups:
                continue
            grp = groups[tag]
            grp["prev_members"] += 1
            if code in today_zt_codes:
                grp["prev_still_up"] += 1
            elif code in zb_codes or code in today_dt_codes:
                grp["prev_broken_or_down"] += 1

    out = []
    for grp in groups.values():
        if grp["limit_up"] < 1:
            continue
        ts = sorted(grp.pop("seal_times"))
        attempts = grp["limit_up"] + grp["broken"]
        grp["first_seal"] = ts[0] if ts else None       # 该题材第一只涨停的时间
        grp["last_seal"] = ts[-1] if ts else None       # 最后一只 —— 与首封拉开=持续发酵
        grp["broken_rate"] = round(grp["broken"] / attempts, 3) if attempts else None
        # 延续率：昨天该题材的涨停股今天还有多少仍涨停
        grp["continuation_rate"] = (round(grp["prev_still_up"] / grp["prev_members"], 3)
                                    if grp["prev_members"] else None)
        # 状态标签只由客观读数推出，不含前瞻判断
        grp["state"] = _state_of(grp, has_prev_reasons)
        grp["members"] = sorted(grp["members"], key=lambda x: -x["_h"])[:8]
        for m in grp["members"]:
            m.pop("_h", None)
        out.append(grp)
    out.sort(key=lambda x: (-x["limit_up"], -x["highest"]))

    total_zt = len(zt)
    return {
        "available": True,
        "date": date,
        "prev_date": prev,
        "tag_count": len(out),
        "themes": out[:top],
        # 头部集中度：第一题材占全市场涨停的比例
        "concentration": round(out[0]["limit_up"] / total_zt, 3) if out and total_zt else None,
        # 覆盖率 = 对上涨停池的只数 / 涨停家数。问财返回的条数可能多于涨停池（口径差异），
        # 直接用 len(reasons) 会出现"42/40"这种看着像 bug 的数
        "covered": matched,
        "total_limit_up": total_zt,
        "coverage_rate": round(matched / total_zt, 3) if total_zt else None,
    }


def _state_of(grp: dict, has_prev: bool = True) -> str:
    """题材状态标签。**只由已发生的读数推出**，不含对明天的判断"""
    lu, hi = grp["limit_up"], grp["highest"]
    prev_n = grp["prev_members"]
    cont = grp["continuation_rate"]
    br = grp["broken_rate"] or 0

    if not has_prev:
        return "无昨日题材数据"
    if prev_n == 0:
        return "今日新出现"
    if cont is not None and cont <= 0.15 and lu <= 2:
        return "接力断档"
    if br >= 0.5:
        return "分歧加大"
    if lu >= 4 and grp["first_boards"] >= 2:
        return "扩散中"
    if hi >= 3 and lu <= 2:
        return "高标独活"
    if cont is not None and cont >= 0.5:
        return "延续"
    return "维持"


def render(tree: dict) -> str:
    """题材树 → 分析师 prompt 能吃的文本。"""
    if not tree.get("available"):
        return f"[题材事件树：不可用（{tree.get('reason', '未知')}）]"
    cr = tree.get("coverage_rate")
    lines = [f"[题材事件树 {tree['date']}｜题材串覆盖 {tree['covered']}/{tree['total_limit_up']} 只涨停"
             + (f"（{cr:.0%}）" if cr is not None else "") + "]"]
    for t in tree["themes"]:
        seg = (f"· {t['tag']}［{t['state']}］涨停{t['limit_up']}"
               f"（首板{t['first_boards']}/连板{t['consec_boards']}）最高{t['highest']}板")
        if t["first_seal"]:
            seg += f"，首封{t['first_seal'][:4]}"
            if t["last_seal"] and t["last_seal"] != t["first_seal"]:
                seg += f"→末封{t['last_seal'][:4]}"
        if t["broken"]:
            seg += f"，炸{t['broken']}"
        if t["limit_down"]:
            seg += f"，跌停{t['limit_down']}"
        if t["prev_members"]:
            cr = t["continuation_rate"]
            seg += f"；昨日该题材{t['prev_members']}只涨停，今日仍涨停{t['prev_still_up']}只"
            if cr is not None:
                seg += f"（延续率{cr:.0%}）"
        names = "、".join(f"{m['name']}{m.get('label') or str(m['boards']) + '板'}" for m in t["members"][:3])
        if names:
            seg += f"；代表：{names}"
        lines.append(seg)
    if tree.get("concentration") is not None:
        lines.append(f"· 头部题材占全市场涨停 {tree['concentration']:.0%}"
                     f"（共 {tree['tag_count']} 个事件题材）")
    return "\n".join(lines)

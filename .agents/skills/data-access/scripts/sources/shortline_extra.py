"""盘外增强数据源:开盘啦(KPL) / 短线侠免费通道 / hhxg.top。

移植自 Vibe AStock v2.0.0 的 duanxian/extra_sources.py,定位不变:**补充源**,
不是替代 —— 主取数层(东财池 / 问财)口径不足时,给复盘与盯盘多一路参考读数。
三家的性质各不相同,先说清楚再用:

  · 开盘啦 (apphq.longhuvip.com) —— App 抓包接口,实时情绪值 / 连板高度 /
    大幅回撤家数。情绪接口返回 `Day` 日期回显,**逐次核验**;回显对不上目标日
    就按不可用处理 —— 把别的交易日的情绪当成被复盘那天的,比缺数据更糟。
  · 短线侠 (duanxianxia.cn) —— 免 token 的 `vendor/livedata/ztlive.json`
    涨停播报,带涨停原因(ztyy)。**仅当日实时**(条目只有 HH:MM 时点、
    无日期字段),免费通道限流 ≥10 秒/次,密集调用实测整站 403。
  · hhxg.top —— 静态 JSON(assistant/recent_theme_latest.json 题材热度榜),
    只有 `latest_date` 一个日期、无历史路径,**只对最新一期可用**。

每个函数的共同约定(与旧版的差异):
  · 旧版走 fetchers._direct_get、失败抛异常由 data.py 统一降级;
    新版走 `sources._http.http_get`(响应原文自动落盘进捕获上下文),
    失败在本模块内捕住、按 {"available": False, "reason": ...} 返回 ——
    三个块各自独立可用,一块挂了不影响另外两块,由映射层记 missing。
  · 能核验的日期回显必须核验;核不了的就明确标注"仅实时/仅最新"。
  · 旧版的 dr._clean_reason(问财题材串清洗)不适用 —— 短线侠的题材串
    是源站自己的口径,与问财不同源,这里原样保留只做基本清洗。
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sources import _http  # noqa: E402
from sources import shortline as S  # noqa: E402

_UNAVAILABLE = "available"  # 每块的统一信封键

# ---- 开盘啦 (KPL / 淘股吧世界) ----------------------------------------------
# App 抓包参数(DeviceID/Token/UserID 是公共演示值,来自抓包样例,非个人凭据)。
KPL_URL = "https://apphq.longhuvip.com/w1/api/index.php?a=ChangeStatistics&st=10&c=HomeDingPan"
KPL_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 14; V2178A Build/UP1A.231005.007)",
}


def _fail(reason: str) -> dict:
    return {_UNAVAILABLE: False, "reason": reason}


def kpl_mood(expected_ymd: str) -> dict:
    """开盘啦情绪指标(实时):涨停家数 / 连板高度 / 情绪值 / 大幅回撤家数。

    expected_ymd: 期望交易日 'YYYYMMDD'。接口返回 `Day` 日期回显,对不上
    即不可用 —— 接口只回当前场次,没有历史参数。
    """
    try:
        r = _http.http_get(KPL_URL, headers=KPL_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"开盘啦情绪接口取数失败:{exc}")
    try:
        info = (data.get("info") or [{}])[0] if isinstance(data.get("info"), list) else {}
        day = str(info.get("Day") or "").strip()
        if not day:
            return _fail("KPL 情绪接口无日期回显,无法核对场次")
        if day.replace("-", "") != expected_ymd:
            return _fail(f"KPL 情绪是 {day} 的,不是目标日 {expected_ymd}(该源仅实时、无历史)")
        return {
            _UNAVAILABLE: True,
            "date": day,
            "limit_up_count": int(info.get("ztjs") or 0),
            "drawdown_count": int(info.get("df_num") or 0),
            "sentiment": int(info.get("strong") or 0),
            "max_consec_boards": int(info.get("lbgd") or 0),
            "tip": str(data.get("tip") or ""),
        }
    except (TypeError, ValueError) as exc:
        return _fail(f"KPL 情绪字段无法解析:{exc}")


# ---- 短线侠免费通道 ----------------------------------------------------------
DXZX_ZTLIVE = "https://duanxianxia.cn/vendor/livedata/ztlive.json"
DXZX_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# 免费通道限流:≥10 秒/次(实测密集调用整站 403)。进程内节流,跨调用共享。
_DXZX_MIN_INTERVAL = 10.0
_dxzx_lock = threading.Lock()
_dxzx_last = [0.0]


def _dxzx_throttle() -> None:
    with _dxzx_lock:
        wait = _DXZX_MIN_INTERVAL - (time.time() - _dxzx_last[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.2, 0.8))
        _dxzx_last[0] = time.time()


def _clean_reason(reason: str) -> str:
    """源站题材串的基本清洗:分隔符统一为「、」,去掉 nan / 空串。"""
    reason = (reason or "").strip()
    if not reason or reason.lower() == "nan":
        return ""
    for sep in ("+", "|", ",", "，", " "):
        reason = reason.replace(sep, "、")
    return "、".join(x for x in (p.strip() for p in reason.split("、")) if x)


def duanxianxia_ztlive() -> dict:
    """短线侠涨停播报(免 token JSON,**仅当日实时**)。

    返回 {"available", "count", "rows": [{code, name, reason, boards, time}]}。
    reason 是源站编辑的涨停原因(zty/y,"+" 分隔题材串,与问财口径不同源)。
    条目只有 HH:MM 时点、无日期字段,因此**调用方必须只在目标日 == 当天时
    调用**,本函数没法替你核验场次。
    """
    _dxzx_throttle()
    try:
        r = _http.http_get(DXZX_ZTLIVE, headers=DXZX_HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"短线侠涨停播报取数失败:{exc}")
    if not isinstance(data, dict) or data.get("result") != "success":
        return _fail("短线侠涨停播报返回异常信封")
    rows_raw = data.get("list")
    if not isinstance(rows_raw, list):
        return _fail("短线侠涨停播报缺 list 字段")
    rows = []
    for x in rows_raw:
        if not isinstance(x, dict):
            continue
        code = str(x.get("code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            continue  # 无效代码直接丢弃,不猜
        reason = str(x.get("zty") if x.get("zty") is not None else x.get("ztyy") or "").strip()
        rows.append({
            "code": code,
            "name": str(x.get("name") or ""),
            "reason": _clean_reason(reason),
            "boards": str(x.get("zt") or "").strip(),
            "time": str(x.get("time") or "").strip(),
        })
    if not rows:
        return _fail("短线侠涨停播报无有效条目")
    return {_UNAVAILABLE: True, "count": len(rows), "rows": rows}


# ---- hhxg.top ----------------------------------------------------------------
HHXG_URL = "https://hhxg.top/static/data/assistant/recent_theme_latest.json"
HHXG_HEADERS = {"User-Agent": "hhxg-skill/1.0", "X-Skill-Client": "clawhub"}


def hhxg_theme_heat(expected_date: str, top_n: int = 15) -> dict:
    """hhxg 题材热度榜(assistant/recent_theme_latest.json,**仅最新一期**)。

    expected_date: 'YYYY-MM-DD'。数据带 `latest_date` 字段,对不上(多半是拿
    历史日期来问)就不可用 —— 这份数据没有历史路径,冒充不了。
    返回 {"date", "items": [{name, premium, limit_up, ratio}]}(yz=溢价,
    zt=涨停家数,ratio=占比;字段缺失填 None,不编数)。
    """
    try:
        r = _http.http_get(HHXG_URL, headers=HHXG_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"hhxg 题材热度取数失败:{exc}")
    if not isinstance(data, dict):
        return _fail("hhxg 题材热度返回非 JSON 对象")
    latest = str(data.get("latest_date") or "").strip()
    if not latest:
        return _fail("hhxg 题材热度无 latest_date,无法核对场次")
    if latest != expected_date:
        return _fail(f"hhxg 题材热度是 {latest} 这一期,不是目标日 {expected_date}"
                     "(该源仅提供最新一期,无历史路径)")
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        # 实测 schema v3 条目:{name, yz(溢价), zt(涨停家数), avg10, ratio(占比)}
        items.append({
            "name": name,
            "premium": it.get("yz"),
            "limit_up": it.get("zt"),
            "ratio": it.get("ratio"),
        })
        if len(items) >= top_n:
            break
    if not items:
        return _fail("hhxg 题材热度榜无有效条目")
    return {_UNAVAILABLE: True, "date": latest, "items": items}


# ---------------------------------------------------------------- 端点入口
def build_extra(date: Optional[str] = None) -> dict:
    """端点 `cn_shortline_extra` 的入口:三路盘外读数各算各的,一块挂了不拖累别家。

    🔴 三家都只有**当下**:KPL 情绪按日期回显核对、短线侠仅当日实时、
    hhxg 仅最新一期。目标日不是今天时,三块都按不可用如实报,不冒充。
    """
    d = S.review_day(date)
    today = S.today_sh()
    out: dict = {"date": d, "is_today": d == today, "settled": S.is_settled(d)}
    kpl = kpl_mood(S.compact(d))
    out["kpl_mood"] = kpl
    if d == today:
        out["ztlive"] = duanxianxia_ztlive()
    else:
        out["ztlive"] = _fail("短线侠涨停播报仅当日实时(条目无日期字段),历史日不可用")
    out["theme_heat"] = hhxg_theme_heat(d)
    return out


def render_extra(result: dict) -> str:
    """渲染成 Agent 复盘能直接读的文字块;不可用的块如实说明原因。"""
    d = result.get("date", "")
    lines = [f"[盘外增强读数 {d}｜开盘啦 / 短线侠 / hhxg,三路独立,均为补充口径]"]

    k = result.get("kpl_mood") or {}
    if k.get("available"):
        lines.append(f"· 开盘啦情绪({k.get('date', '')}):涨停 {k['limit_up_count']} 家、"
                     f"最高连板 {k['max_consec_boards']} 板、情绪值 {k['sentiment']}、"
                     f"大幅回撤 {k['drawdown_count']} 家"
                     + (f";{k['tip']}" if k.get("tip") else ""))
    else:
        lines.append(f"· 开盘啦情绪:不可用({k.get('reason', '未知')})")

    z = result.get("ztlive") or {}
    if z.get("available"):
        names = "、".join(f"{r['name']}({r['code']})" for r in (z.get("rows") or [])[:10])
        more = f" 等 {z['count']} 只" if z.get("count", 0) > 10 else ""
        lines.append(f"· 短线侠涨停播报(仅当日实时):{z['count']} 只;前 10:{names}{more}"
                     "(涨停原因是源站编辑归因,题材串与问财不同源)")
    else:
        lines.append(f"· 短线侠涨停播报:不可用({z.get('reason', '未知')})")

    h = result.get("theme_heat") or {}
    if h.get("available"):
        tops = "、".join(f"{it['name']}(涨停 {it.get('limit_up') if it.get('limit_up') is not None else '—'}家)"
                        for it in (h.get("items") or [])[:8])
        lines.append(f"· hhxg 题材热度榜({h.get('date', '')} 这一期):{tops}")
    else:
        lines.append(f"· hhxg 题材热度榜:不可用({h.get('reason', '未知')})")

    lines.append("⚠ 三路都是盘外补充源,口径与主取数层(东财池 / 问财)不同;"
                 "与主层数字冲突时以主层为准,这里的读数只作交叉参考。")
    return "\n".join(lines)

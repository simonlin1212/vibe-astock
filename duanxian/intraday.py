"""盘中快照与竞价核验 —— 让复盘的结论在第二天被事实检验。

## 为什么需要

只做盘后复盘的工具，第二天早上就和用户断线了：昨晚判了"退潮"，今早到底应验没有？
09:25 竞价一出来其实就有答案，但没人把它和昨晚的判断对上。

本模块补两件事：

1. **09:25 竞价核验** —— 昨日涨停股整体高开还是低开、各板位竞价强弱、
   最高标什么状态、**昨晚立的验证条件有没有出现苗头**。
2. **盘中五时点快照** —— 同样是收盘 40 家涨停，早上 60 家炸到 40 家，
   和早上 15 家扩散到 40 家，含义完全相反。只有留下路径才看得出来。

## 合规

只产出**市场层面聚合**（高开/低开家数、各板位均值、题材前排整体强弱）。
个股一律只列客观读数（开盘涨跌幅、是否涨停），**不排序、不评分、不给参与倾向**。
"竞价后告诉你今天能打哪只"属于品种选择 + 买卖时机，明确不做。

## 数据

只用腾讯实时行情（`emotion_metrics.batch_pct`，不封 IP）+ 已缓存的昨日涨停池。
不需要 Level-2、不需要集合竞价逐笔。
"""

from __future__ import annotations

from duanxian.paths import data_path
import json
import os
import re
from statistics import mean, median
from typing import Optional

from . import trade_calendar
from .util import atomic_write_json, china_now, china_today

_DIR = data_path("intraday")

# 一天要留的快照时点（上海时间 HH:MM）。收盘后那个是当天定稿，前面几个记录路径。
SNAPSHOT_SLOTS = ["09:25", "09:35", "10:00", "11:30", "14:00", "15:00"]

# 快照结构版本，改字段就 +1
_SNAP_SCHEMA = 3   # v3：加盘面家数(live) + 覆盖率

# slot 与真实抓取时刻允许的最大偏差（分钟）。超过就不能算该时点的读数 ——
# 竞价快照尤其敏感：09:25 的"高开占比"和 10:30 的实时涨幅完全是两回事。
_MAX_DRIFT_MIN = 8


def _drift_minutes(slot: str, now_hhmm: str) -> Optional[int]:
    """now 距 slot 过了多少分钟（负数=还没到，也算不合法）。格式坏了返回 None。"""
    try:
        sh, sm = int(slot[:2]), int(slot[3:5])
        nh, nm = int(now_hhmm[:2]), int(now_hhmm[3:5])
    except (ValueError, IndexError):
        return None
    d = (nh * 60 + nm) - (sh * 60 + sm)
    return d if d >= 0 else None


def _valid_session_window(slot: str, captured: str) -> bool:
    """只接纳成交时段；午收/日收快照允许取数延迟八分钟。"""
    shape = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
    if not re.fullmatch(shape, slot) or not re.fullmatch(shape, captured):
        return False
    drift = _drift_minutes(slot, captured)
    if drift is None or drift > _MAX_DRIFT_MIN:
        return False
    if slot == "09:25":
        return "09:25" <= captured < "09:30"
    if slot in {"11:30", "15:00"}:
        return True
    return (("09:30" <= slot < "11:30" and captured < "11:30") or
            ("13:00" <= slot < "15:00" and captured < "15:00"))


def _day_dir(date: str) -> str:
    return os.path.join(_DIR, date)


def _slot_path(date: str, slot: str) -> str:
    return os.path.join(_day_dir(date), f"{slot.replace(':', '')}.json")


def capture(slot: Optional[str] = None, date: Optional[str] = None) -> dict:
    """抓一张当前盘面快照。`slot` 缺省用当前时间归到最近的时点。

    ⚠️ 抓的是**当前**行情，所以只能在当天盘中调用；历史日补不回来
    （这正是要定时抓的原因 —— 过了这个点就永远没有了）。
    """
    date = date or china_today()
    now = china_now()
    slot = slot or now.strftime("%H:%M")

    # ⚠️ **历史日绝不现抓**。拿的是当前行情，给历史日抓 = 用今天的价格伪造那天的快照，
    # 而文件里会写着那天的日期和时点 —— 永久污染，且看不出异样。
    if date != china_today():
        return {"ok": False, "reason": f"{date} 不是今天；盘中快照拿的是实时行情，历史日补不回来"}

    # ⚠️ 必须确认今天真开市。节假日（工作日但休市）调度线程照样会转到点，
    # 而这时 batch_pct 拿到的是**上一交易日的收盘价** —— 会被当成"今天 09:25 的竞价"
    # 存进快照，且数字完全合理、看不出异样。周末靠调度层的 is_weekend 挡，
    # 节假日只能靠这一层（同 trade_calendar.latest_session 的第三层判据）。
    if date == china_today() and trade_calendar.quote_trade_day() != date:
        return {"ok": False, "reason": f"{date} 未开市（参考股行情时间戳不是今天），不抓快照"}

    # ⚠️ slot 与真实抓取时刻的偏差要有上限。10:30 打开页面却抓一张标着 "09:25" 的快照，
    # 前端会当成竞价读数展示"高开占比"——那是彻头彻尾的假事实。
    clock = now.strftime("%H:%M")
    if clock < slot:
        return {"ok": False, "reason": f"当前 {clock} 尚未到 {slot}，请到时再抓取"}
    if clock < "09:25":
        return {"ok": False, "reason": "竞价尚未完成，试撮合报价不能记为开盘快照"}
    if slot == "09:25" and clock >= "09:30":
        return {"ok": False, "reason": "已进入连续交易，不能补抓 09:25 竞价快照"}
    drift = _drift_minutes(slot, clock)
    if drift is None or drift > _MAX_DRIFT_MIN:
        return {"ok": False,
                "reason": f"当前 {now.strftime('%H:%M')} 距 {slot} 已超 {_MAX_DRIFT_MIN} 分钟，"
                          f"不能当作该时点的快照"}

    if not _valid_session_window(slot, clock):
        return {"ok": False, "reason": "当前不在有效成交时段，午休和夜间行情不能保存为盘中快照"}

    prev = trade_calendar.prev_trade_date(date)
    if not prev:
        return {"ok": False, "reason": "取不到前一交易日"}

    from . import market_facts as mf
    from .emotion_metrics import batch_pct

    pp = mf.pools(prev)
    if pp is None:
        return {"ok": False, "reason": f"{prev} 涨停池取数失败"}

    # ⚠️ 光记"昨日涨停股的当前涨跌"答不了"早上 60 家涨停炸到 40 家还是 15 家扩散到 40 家"
    # —— 那需要**当时的涨停/炸板家数**。第一版只有前者却在文案里承诺后者。
    live = mf.pools(date)      # 盘中调用时拿到的就是"此刻"的三池
    live_counts = ({"limit_up": len(live["zt"]), "broken": len(live["zb"]),
                    "limit_down": len(live["dt"]),
                    "highest_board": max((int(r.get("boards") or 1) for r in live["zt"]), default=0),
                    "sectors": len({(r.get("sector") or "").strip()
                                    for r in live["zt"] if (r.get("sector") or "").strip()})}
                   if live is not None else None)

    prev_zt = pp["zt"]
    codes = [r["code"] for r in prev_zt]
    pct = batch_pct(codes, opening_date=date) if slot == "09:25" else batch_pct(codes)
    now = china_now()
    if slot == "09:25" and now.strftime("%H:%M") >= "09:30":
        return {"ok": False, "reason": "抓取跨过开盘时点，本次未保存为竞价快照"}
    drift = _drift_minutes(slot, now.strftime("%H:%M"))
    if now.strftime("%Y-%m-%d") != date or not _valid_session_window(slot, now.strftime("%H:%M")):
        return {"ok": False, "reason": "取数超过该时点的有效窗口，本次未保存快照"}
    got = [(r, pct[r["code"]]) for r in prev_zt if r["code"] in pct]
    if not got:
        return {"ok": False, "reason": "未取得有效09:25开盘价，可能尚未发布或取数失败；窗口内可重试" if slot == "09:25" else "未取得有效的实时行情"}

    vals = [v for _, v in got]
    # 按昨日板位分组看强弱 —— 首板和高位板的竞价含义完全不同
    tiers: dict[str, list[float]] = {}
    for r, v in got:
        b = int(r.get("boards") or 1)
        key = "首板" if b == 1 else ("2板" if b == 2 else "3板及以上")
        tiers.setdefault(key, []).append(v)

    # 最高标的客观状态（只列读数，不评价）
    top_board = max((int(r.get("boards") or 1) for r in prev_zt), default=0)
    tops = [{"code": r["code"], "name": r["name"], "boards": int(r.get("boards") or 1),
             "sector": r.get("sector") or "", "pct": round(v, 2)}
            for r, v in got if int(r.get("boards") or 1) == top_board]

    snap = {
        "schema": _SNAP_SCHEMA,
        "date": date,
        "slot": slot,
        "captured_at": now.strftime("%Y-%m-%d %H:%M:%S") + " CST",
        "basis_key": "open_vs_prev_close" if slot == "09:25" else "latest_vs_prev_close",
        "price_basis": "当日开盘价 / 昨收" if slot == "09:25" else "抓取时最新价 / 昨收",
        "drift_minutes": _drift_minutes(slot, now.strftime("%H:%M")),      # 真实抓取时刻距标称 slot 差几分钟
        "prev_date": prev,
        "sample": len(vals),
        "avg": round(mean(vals), 2),
        "median": round(median(vals), 2),
        "up_count": sum(1 for v in vals if v > 0),
        "down_count": sum(1 for v in vals if v < 0),
        "up_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        "deep_loss_5": sum(1 for v in vals if v <= -5),
        "by_tier": {
            k: {"sample": len(vs), "avg": round(mean(vs), 2),
                "median": round(median(vs), 2),
                "up_rate": round(sum(1 for v in vs if v > 0) / len(vs), 3)}
            for k, vs in tiers.items()
        },
        "top_boards": tops,
        "top_board_level": top_board,
        # 当时的盘面家数（涨停/炸板/跌停/最高板/题材数）—— 情绪路径靠它
        "live": live_counts,
        # 覆盖率（同 money_effect 口径）：只回来一小半时别拿它当全体读数
        "coverage_rate": round(len(vals) / len(prev_zt), 3) if prev_zt else None,
        "expected_sample": len(prev_zt),
    }
    try:
        os.makedirs(_day_dir(date), exist_ok=True)
        if not atomic_write_json(_slot_path(date, slot), snap):
            raise OSError("snapshot persistence failed")
    except OSError:  # 保存失败不能冒充已记录
        return {"ok": False, "reason": "快照保存失败，请检查本地目录权限或磁盘空间后重试"}
    return {"ok": True, "snapshot": snap}


def _opening_basis(snap: dict) -> bool:
    return snap.get("basis_key") == "open_vs_prev_close" or ("basis_key" not in snap and snap.get("price_basis") == "当日开盘价 / 昨收")


def load_day(date: Optional[str] = None) -> dict:
    """当天已留下的所有快照（按时点升序）。"""
    date = date or china_today()
    d = _day_dir(date)
    if not os.path.isdir(d):
        return {"date": date, "slots": [], "warnings": []}
    out = []
    warnings = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                s = json.load(fh)
            if s.get("schema") == _SNAP_SCHEMA:
                slot = str(s.get("slot", ""))
                if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", slot):
                    warnings.append(f"存档 {fn} 时点格式无效，原件保留，不纳入本次核验。")
                    continue
                captured = str(s.get("captured_at", ""))[11:16]
                if (s.get("date") != date or str(s.get("captured_at", ""))[:10] != date or
                    not _valid_session_window(slot, captured) or
                    (slot == "09:25" and (not _opening_basis(s) or captured >= "09:30"))):
                    warnings.append(f"{slot} 旧快照未证明为有效成交时点，原件保留，不纳入竞价核验或情绪路径。")
                    continue
                out.append(s)
        except Exception:  # noqa: BLE001
            warnings.append(f"存档 {fn} 读取或解析失败，原件保留，不纳入本次核验。")
            continue
    out.sort(key=lambda s: s.get("slot", ""))
    return {"date": date, "slots": out, "warnings": warnings}


def auction_check(date: Optional[str] = None) -> dict:
    """09:25 竞价核验：昨晚的判断，今早开盘就见分晓的那部分。

    产出三块：
    1. 昨日涨停股的竞价强弱（整体 + 分板位）
    2. 最高标的客观竞价状态
    3. **昨晚立下的验证条件**逐条给出"竞价阶段的早期读数"

    ⚠️ 第 3 块只对**竞价时就能看出苗头**的指标有意义（如赚钱效应中位数），
    像"炸板率"这种要等收盘才有的，明确标"待收盘"，不硬凑。
    """
    date = date or china_today()
    day = load_day(date)
    snap = next((s for s in day["slots"] if s["slot"] == "09:25"), None)
    # load_day通常已过滤；此守卫也保护独立调用/替代加载器，不用未知basis做核验。
    if snap is not None and not _opening_basis(snap):
        return {"available": False, "warnings": day.get("warnings", []),
                "reason": "旧竞价快照未证明使用09:25开盘成交价，已保留原件但不参与核验。"}
    if snap is None:
        # ⚠️ 缺存档就**如实说缺**，绝不现抓充数：10:30 抓一张标成 "09:25" 的快照，
        # 前端会把盘中实时涨幅当竞价"高开占比"展示。
        # 只在竞价窗口内才允许补抓一次。
        r = capture("09:25", date)
        if not r.get("ok"):
            return {"available": False,
                    "warnings": day.get("warnings", []),
                    "reason": ("09:25 旧快照已排除，未取得有效竞价记录。" if any(w.startswith("09:25 ") for w in day.get("warnings", [])) else "") + f"09:25 竞价快照缺失（{r.get('reason', '未知')}）。"
                              "盘中快照只在当天该时点前后有效，过点补不回来。"}
        snap = r["snapshot"]

    # 昨晚的验证条件
    from . import reflection

    prev = snap.get("prev_date")
    review = reflection._load_review(prev) if prev else None
    items = ((review or {}).get("focus") or {}).get("verification_items") or []
    early = []
    for it in items:
        key = it.get("metric")
        # 竞价阶段能给早期读数的只有"昨日涨停股表现"这一类
        if key == "money_effect_median":
            early.append({
                "metric": key, "label": "赚钱效应中位数", "expect": it.get("direction"),
                "early_value": snap["median"], "unit": "%",
                "note": "竞价阶段读数（昨日涨停股集合竞价中位涨幅），收盘才定稿",
            })
        else:
            early.append({
                "metric": key, "label": key, "expect": it.get("direction"),
                "early_value": None, "unit": "",
                "note": "该指标要等盘中/收盘才有读数",
            })

    return {
        "available": True,
        "warnings": day.get("warnings", []),
        "date": date,
        "prev_date": prev,
        "captured_at": snap.get("captured_at"),
        "overall": {
            "sample": snap["sample"], "avg": snap["avg"], "median": snap["median"],
            "up_count": snap["up_count"], "down_count": snap["down_count"],
            "up_rate": snap["up_rate"], "deep_loss_5": snap["deep_loss_5"],
        },
        "by_tier": snap["by_tier"],
        "top_boards": snap["top_boards"],
        "top_board_level": snap["top_board_level"],
        "verification_early": early,
    }


def path_summary(date: Optional[str] = None) -> dict:
    """一天的盘面路径：把各时点快照串成一条线。

    两条线各答一个问题，别混：
    - **昨日强势股承接路径**（median/up_rate）：昨天的票从竞价到收盘是修复还是走弱
    - **盘面家数路径**（live）：涨停从 60 家炸到 40 家，还是从 15 家扩散到 40 家
      —— 同样收盘 40 家，含义完全相反

    ⚠️ `up_rate` 只在 09:25 那张才是"高开率"；盘中时点它是**红盘率**（相对昨收）。
    """
    day = load_day(date)
    slots = day["slots"]
    if not slots:
        return {"available": False, "reason": "当天还没有有效快照（盘中才会产生）", "warnings": day.get("warnings", []), "date": day["date"]}
    return {
        "available": True,
        "date": day["date"],
        "warnings": day.get("warnings", []),
        "points": [
            {"slot": s["slot"], "median": s["median"], "avg": s["avg"],
             # 09:25=高开率，其余时点=红盘率（相对昨收），前端按 slot 显示不同标签
             "up_rate": s["up_rate"], "deep_loss_5": s["deep_loss_5"],
             "live": s.get("live"), "coverage_rate": s.get("coverage_rate")}
            for s in slots
        ],
    }

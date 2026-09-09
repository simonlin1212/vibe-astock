"""判断 vs 执行归因：把"市场判断成没成立"与"自己赚没赚钱"交叉起来。

系统里两套验证各回答半个问题：`reflection` / `verification` 说昨晚那份判断今天
成立没有，`journal` / `risk` 说这些天赚了还是亏了。只看一层会得出错的结论 ——
看对了却亏钱和看错了才亏钱要修的地方不同（前者改执行，后者改判断）。

## 四格

|            | 自己赚钱   | 自己亏钱             |
|------------|------------|----------------------|
| **判对了** | 顺风顺水   | ⭐**执行问题**       |
| **判错了** | ⚠️**运气** | 判断问题             |

⚠️「判错还赚钱」那格必须单独点出来，不能混进"总体盈利"里。

## 对齐哪一天（容易搞错）

交易在 **D 日入场**，依据是 **D-1 晚**那份判断；`reflection` 文件里
`prediction_date = D-1`、`eval_date = D`。所以对齐关系是：

    交易的入场日（settled.first_buy） ←→ reflection 的 eval_date

⚠️ **不能按平仓日对齐**：D 日买、D+2 日卖的交易，按平仓日会去比 D+1 晚做的判断 ——
那份判断没参与这笔决策，归因张冠李戴，而两个数都长得正常、看不出来。

## 边界

只统计使用者自己录入的交易与已落盘的复盘产物，不产出任何操作建议。
⛔ 本模块的数据**不接入任何 AI prompt**。
"""

from __future__ import annotations

from duanxian.paths import data_path
import glob
import json
import os
from typing import Optional

_REFL_DIR = data_path("reflections")

# 四格的名字。⚠️ 顺序别改，前端按 key 取。
QUADRANTS = {
    "right_win": "判对 + 赚钱",
    "right_lose": "判对 + 亏钱",       # ⭐ 执行问题
    "wrong_win": "判错 + 赚钱",        # ⚠️ 运气
    "wrong_lose": "判错 + 亏钱",
}

# 少于这么多天，四格分布是噪声，不给倾向性描述
_MIN_DAYS = 8


def _read_hits() -> dict[str, dict]:
    """每个交易日的「市场判断对没对」。key = eval_date（= 交易入场日）。

    只取 `phase_eval` 里已定稿的判定：`provisional`（只拿到一路信号）和
    `hit is None`（判不了）**一律不计入**，不能把"不知道"当成"判错"。
    """
    out: dict[str, dict] = {}
    for f in sorted(glob.glob(os.path.join(_REFL_DIR, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:  # noqa: BLE001  坏文件跳过，不让归因整个失败
            continue
        ev = d.get("eval_date")
        pe = d.get("phase_eval") or {}
        hit = pe.get("hit")
        if not ev or hit is None or pe.get("provisional") or d.get("provisional"):
            continue
        item = {
            "hit": bool(hit),
            "phase": pe.get("phase") or d.get("emotion_phase"),
            "prediction_date": d.get("prediction_date"),
            "signal_count": pe.get("signal_count"),
        }
        # 验证条件的命中率与超额（新 schema 才有），作为更细的读盘质量参考
        vs = d.get("verification_summary") or {}
        if vs.get("decided"):
            item["verification_rate"] = vs.get("rate")
            item["verification_edge"] = vs.get("edge")
        out[ev] = item
    return out


def _entry_day(trade: dict) -> Optional[str]:
    """交易的入场日。没有成交明细就退回记录日期。"""
    s = trade.get("settled") or {}
    return s.get("first_buy") or trade.get("date")


def _pnl(trade: dict) -> Optional[float]:
    """这笔的已实现盈亏（元）。只填了百分比、没填明细的算不出金额。"""
    s = trade.get("settled") or {}
    v = s.get("realized_pnl")
    return None if v is None else float(v)


def attribution(limit: int = 500) -> dict:
    """把有交易的每一天拆进四格。

    ⚠️ 只用**已平仓且填了成交明细**的交易 —— 没有金额就没有"那天赚没赚"，
    拿百分比按笔数平均会把 1 万的仓和 1 千的仓当成一样重。
    """
    from . import journal

    try:
        trades = (journal.list_trades(limit=limit) or {}).get("trades") or []
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"读交易日志失败：{exc}"}

    hits = _read_hits()
    if not hits:
        return {"available": False,
                "reason": "还没有可用的市场判断记录（回看结果），无法做判断/执行归因"}

    # 按入场日汇总当天的已实现盈亏
    by_day: dict[str, dict] = {}
    skipped_no_amount = 0
    skipped_no_read = 0
    for t in trades:
        d = _entry_day(t)
        pnl = _pnl(t)
        if d is None:
            continue
        if pnl is None:
            skipped_no_amount += 1
            continue
        if d not in hits:
            skipped_no_read += 1
            continue
        b = by_day.setdefault(d, {"date": d, "pnl": 0.0, "trades": 0,
                                  "unplanned": 0, **hits[d]})
        b["pnl"] += pnl
        b["trades"] += 1
        if t.get("as_planned") is False:
            b["unplanned"] += 1

    if not by_day:
        return {"available": False, "skipped_no_amount": skipped_no_amount,
                "skipped_no_read": skipped_no_read,
                "reason": ("没有能归因的交易日 —— 需要「填了成交明细的已平仓交易」"
                           "且那天有市场判断记录")}

    cells: dict[str, list[dict]] = {k: [] for k in QUADRANTS}
    for b in sorted(by_day.values(), key=lambda x: x["date"]):
        b["pnl"] = round(b["pnl"], 2)
        # ⚠️ 盈亏恰好为 0 的日子不进任何一格：它既不是赚也不是亏，
        # 硬塞进"亏钱"格会凭空造出执行问题（同 journal 战绩里"持平不进分母"）。
        if abs(b["pnl"]) < 1e-6:
            continue
        key = ("right_" if b["hit"] else "wrong_") + ("win" if b["pnl"] > 0 else "lose")
        cells[key].append(b)

    def _sum(rows: list[dict]) -> dict:
        return {"days": len(rows), "pnl": round(sum(r["pnl"] for r in rows), 2),
                "days_list": [r["date"] for r in rows]}

    summary = {k: _sum(v) for k, v in cells.items()}
    counted = sum(s["days"] for s in summary.values())
    return {
        "available": True,
        "days_counted": counted,
        "enough_samples": counted >= _MIN_DAYS,
        "quadrant_labels": QUADRANTS,
        "quadrants": summary,
        "cells": cells,
        "skipped_no_amount": skipped_no_amount,
        "skipped_no_read": skipped_no_read,
        "note": ("按**入场日**对齐：那天的操作依据是前一晚那份判断。"
                 "盈亏为 0 的日子不进任何一格。"),
    }


def render(rep: dict) -> str:
    """四格的纯文本形式（给 UI 兜底展示 / 自用脚本读）。

    ⛔ **不要把它接进任何 AI prompt。** 个人持仓与盈亏一旦进 prompt，模型的
    回答就变成"针对这个人当前处境"的意见 —— 那正是个性化投资建议，
    是本项目合规立足点（非个性化）唯一不能碰的那条线。
    见记忆 `project_vibe-astock-commercialization-legal`。
    个人数据只走只读 API 给前端渲染，AI 永远看不到。
    """
    if not rep.get("available"):
        return ""
    q = rep.get("quadrants") or {}
    lines = [f"· 判断/执行归因（{rep['days_counted']} 个有盈亏的交易日）："]
    for k, label in QUADRANTS.items():
        c = q.get(k) or {}
        if c.get("days"):
            lines.append(f"  - {label}：{c['days']} 天，合计 {c['pnl']:+.0f} 元")
    exe = (q.get("right_lose") or {}).get("days") or 0
    luck = (q.get("wrong_win") or {}).get("days") or 0
    if not rep.get("enough_samples"):
        lines.append(f"  ⚠️ 只有 {rep['days_counted']} 天，样本太少，别下结论")
    else:
        if exe:
            lines.append(f"  ⭐ 有 {exe} 天是「看对了却亏钱」—— 这几天的问题在执行不在判断")
        if luck:
            lines.append(f"  ⚠️ 有 {luck} 天是「看错了还赚钱」—— 赚钱会给错判断发正反馈")
    return "\n".join(lines)

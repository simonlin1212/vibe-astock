"""个人交易日志：记录自己的每一笔交易，并附上当时的市场环境快照。

每笔记录会自动带上成交那天的情绪档位、赚钱效应、以及该股当天的客观状态
（几板、几点封板、炸板次数、所属题材），用于事后按环境/打法/是否按计划分组自查。

## 边界

只统计使用者自己录入的历史行为，不产出任何选股、买卖时机或参与倾向。
⛔ 本模块的数据**不接入任何 AI prompt**。

## 数据

`~/.duanxian-agents/journal/trades.json`（单文件、本地、不上传）。
市场环境快照从已落盘的 reviews / market_facts 读取，不额外发起请求。
"""

from __future__ import annotations

from duanxian.paths import data_path
import datetime
import json
import math
import os
import threading
import uuid
from statistics import mean
from typing import Optional

from .util import atomic_write_json, china_now, validate_trade_date

_DIR = data_path("journal")
_PATH = os.path.join(_DIR, "trades.json")

# 交易记录结构版本。
# v2 起一笔交易 = 多次成交（fills），以支持分批建仓、做 T、买卖不同日。
# v3 增加计划退出边界（planned_stop / planned_target）。
_SCHEMA = 3

# 一次成交
#   side: buy / sell
#   date: 成交日（买卖可以不同日）
#   price / shares: 价与量 —— 有它们才能算加权成本、仓位、盈亏金额
_SIDES = ("buy", "sell")

# 打法标签：交易按模式归类（而非按个股）
PLAYBOOKS = ["打板", "低吸", "接力", "半路", "套利", "其它"]

# 交易费用。⚠️ 下面这些是**能跑起来的初值，不是推荐值也不是你的真实费率** ——
# 各家券商佣金差别很大，印花税与过户费也会随政策调整。请按自己账户的实际费率改。
# 对高换手的短线打法，费用不是小数：一堆薄利交易在计费后可能接近持平甚至转亏，
# "这套打法还灵不灵"的结论会因此反向，所以宁可让用户显式确认，也不默默按 0 算。
_FEE_PATH = os.path.join(_DIR, "fees.json")
DEFAULT_FEES = {
    "commission_rate": 0.00025,    # 佣金费率（双向），万 2.5
    "commission_min": 5.0,         # 单笔佣金最低收取（元）
    "stamp_tax_rate": 0.0005,      # 印花税，**仅卖出**收
    "transfer_fee_rate": 0.00001,  # 过户费（双向）
}
_FEE_LABELS = {
    "commission_rate": "佣金费率（双向）",
    "commission_min": "单笔佣金最低（元）",
    "stamp_tax_rate": "印花税（仅卖出）",
    "transfer_fee_rate": "过户费（双向）",
}


def load_fees() -> dict:
    """仅文件不存在时使用初值；已有配置异常不得悄悄改变结算费用。"""
    try:
        with open(_FEE_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        return {**DEFAULT_FEES, "is_default": True}
    except (OSError, ValueError) as exc:
        raise ValueError("交易费率配置无法读取，原件保留；请在交易费率中重新填写完整配置并保存") from exc
    if not isinstance(saved, dict) or any(k not in saved for k in DEFAULT_FEES):
        raise ValueError("交易费率配置不完整，原件保留；请重新填写完整配置并保存")
    cfg = {}
    for k in DEFAULT_FEES:
        v = saved[k]
        if (isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) or v < 0 or (k.endswith("_rate") and v > 0.01)):
            raise ValueError(f"{_FEE_LABELS[k]} 配置无效，原件保留；请重新填写完整配置并保存")
        cfg[k] = float(v)
    return {**cfg, "is_default": False}


def save_fees(cfg: dict) -> dict:
    """保存费率。只收已知字段，负数与非数字一律拒绝。"""
    if not isinstance(cfg, dict):
        raise ValueError("请填写完整费率配置")
    out = {}
    for k in DEFAULT_FEES:
        v = (cfg or {}).get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{_FEE_LABELS[k]} 必须是数字") from exc
        if isinstance(v, bool) or not math.isfinite(f) or f < 0:
            raise ValueError(f"{_FEE_LABELS[k]} 不能是负数")
        if k.endswith("_rate") and f > 0.01:
            raise ValueError(f"{_FEE_LABELS[k]} 看起来不像费率（{f}）—— 万 2.5 应填 0.00025")
        out[k] = f
    if len(out) != len(DEFAULT_FEES):
        raise ValueError("请填写全部费率项目，缺项不会自动使用初值")
    os.makedirs(_DIR, exist_ok=True)
    if not atomic_write_json(_FEE_PATH, out):
        raise RuntimeError("费率写入失败")
    return {"ok": True, "fees": {**out, "is_default": False}}


def _fee_of(fill: dict, cfg: dict) -> float:
    """一笔成交的费用。用户在这笔上填了 `fee` 就以它为准（那是对账单上的真实值）。"""
    if fill.get("fee") is not None:
        return float(fill["fee"])
    amt = fill["price"] * fill["shares"]
    fee = max(amt * cfg["commission_rate"], cfg["commission_min"])
    fee += amt * cfg["transfer_fee_rate"]
    if fill["side"] == "sell":
        fee += amt * cfg["stamp_tax_rate"]
    return fee


class JournalCorrupted(RuntimeError):
    """账本文件存在但读不出来 —— 必须停止写入，绝不能当空账本继续覆盖。"""


# ⚠️ 读-改-写必须串行。原子写只保证单次写入不会半截，挡不住两个请求各读一份、
#    各追加一条、后写的覆盖先写的 —— 那是静默丢单。
_LOCK = threading.Lock()


def _load_raw() -> list[dict]:
    """读账本。⚠️ 文件损坏时**抛异常**，绝不返回空表。

    返回空表会让下一次 add 把整本账覆盖成只剩一条，历史记录直接丢失。
    """
    if not os.path.isfile(_PATH):
        return []
    try:
        with open(_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        raise JournalCorrupted(f"账本文件无法解析（{type(exc).__name__}），已停止写入以防覆盖：{_PATH}") from exc
    if not isinstance(d, dict) or "trades" not in d:
        raise JournalCorrupted(f"账本文件结构异常，已停止写入以防覆盖：{_PATH}")
    got = d.get("schema")
    if got != _SCHEMA:
        # ⚠️ 能自动迁移的就迁，别让老用户卡死。**迁移前先备份** ——
        # 账本是永久数据，宁可多留一份也不能就地改坏。
        trades = _migrate(got, d.get("trades") or [])
        if trades is None:
            raise JournalCorrupted(
                f"账本 schema={got} 无法自动迁移到 {_SCHEMA}，已停止写入：{_PATH}")
        return trades
    return d.get("trades") or []


def _migrate(from_schema, trades: list[dict]) -> Optional[list[dict]]:
    """老版本账本 → 当前结构。迁不了返回 None。

    v1 → v2：v1 没有成交明细。⚠️ **不伪造 fills** —— 价与量当时没记录，
    补空结构即可，这些记录在"未填明细"分组里如实呈现。

    v2 → v3：新增计划退出边界（`planned_stop` / `planned_target`）。
    ⚠️ **老记录一律补 None，不做反推** —— 计划止损位是下单时的主观意图，
    按成交价反推出的值不是事实，而在险资金正是拿它计算的。
    """
    if from_schema not in (1, 2):
        return None
    _backup_once(f"v{from_schema}")
    out = []
    for t in trades:
        t = dict(t)
        if from_schema == 1:
            t.setdefault("fills", [])
            t.setdefault("settled", {"has_fills": False, "closed": False})
            t.setdefault("exit_market", None)
        # v3：计划退出边界。老记录没有就是没有 —— 补 None，不倒推
        t.setdefault("planned_stop", None)
        t.setdefault("planned_target", None)
        out.append(t)
    print(f"ℹ️ 交易日志已从 schema {from_schema} 迁移到 {_SCHEMA}（{len(out)} 条，原件已备份）")
    return out


def _backup_once(tag: str) -> None:
    """迁移前把原件另存一份。同 tag 只备份一次，不覆盖。"""
    src = _PATH
    dst = os.path.join(_DIR, f"trades.{tag}.bak.json")
    if os.path.isfile(src) and not os.path.isfile(dst):
        try:
            import shutil

            shutil.copy2(src, dst)
        except Exception as exc:  # noqa: BLE001  备份失败要出声，但不阻断读取
            print(f"⚠️ 账本备份失败（{type(exc).__name__}），迁移继续但请手动备份 {src}")


def _save(trades: list[dict]) -> bool:
    os.makedirs(_DIR, exist_ok=True)
    return atomic_write_json(_PATH, {"schema": _SCHEMA, "trades": trades})


def _market_context(date: str) -> dict:
    """那天的市场环境。从已落盘的复盘里取，取不到就给空 —— 不为一条记录去跑复盘。"""
    from .reflection import _load_review

    review = _load_review(date) or {}
    focus = review.get("focus") or {}
    m = review.get("emotion_metrics") or {}
    f = review.get("market_facts") or {}
    me = m.get("money_effect") or {}
    pr = m.get("promotion") or {}
    sq = f.get("seal_quality") or {}
    return {
        "emotion_phase": focus.get("emotion_phase"),
        "money_effect_median": me.get("median") if me.get("available") else None,
        "promotion_overall": (pr.get("overall") or {}).get("rate") if pr.get("available") else None,
        "limit_up_count": pr.get("limit_up_count") if pr.get("available") else None,
        "never_broken_rate": sq.get("never_broken_rate") if sq.get("available") else None,
        "has_review": bool(focus),
    }


def _stock_context(date: str, code: str) -> dict:
    """这只票那天的客观状态：几板、几点封的、炸过几次、什么题材。"""
    from . import market_facts as mf

    p = mf.pools(date)
    if p is None:
        return {}
    code = str(code).zfill(6)
    for r in p["zt"]:
        if r["code"] == code:
            return {
                "in_limit_up": True, "boards": int(r.get("boards") or 1),
                "first_seal": str(r.get("first_seal") or "") or None,
                "last_seal": str(r.get("last_seal") or "") or None,
                "broken_times": int(r.get("broken_times") or 0),
                "sector": r.get("sector") or "", "board_type": r.get("board"),
            }
    for r in p["zb"]:
        if r["code"] == code:
            return {"in_limit_up": False, "was_broken": True,
                    "broken_times": int(r.get("broken_times") or 0),
                    "sector": r.get("sector") or "", "board_type": r.get("board")}
    return {"in_limit_up": False}


# 「没传这个参数」与「显式传了 None」是两件事：前者保持原值，后者是清空。
# 用普通默认值 None 区分不了，所以要一个哨兵。
_UNSET = object()


def _price(v, label: str) -> Optional[float]:
    """计划边界只校验"是不是正数" —— 具体定在哪是使用者自己的事，不替他判断。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if f != f or f <= 0:
        raise ValueError(f"{label}必须是正数")
    return round(f, 4)


def _norm_fills(fills: list[dict]) -> list[dict]:
    """校验并规范化成交明细。空列表也允许（只记标签、盈亏后补）。"""
    out = []
    for i, f in enumerate(fills or []):
        if not isinstance(f, dict):
            raise ValueError(f"第 {i + 1} 笔成交格式不对")
        side = str(f.get("side") or "").strip().lower()
        if side not in _SIDES:
            raise ValueError(f"第 {i + 1} 笔的 side 需为 buy/sell，得到 {side!r}")
        d = validate_trade_date(str(f.get("date") or "").strip())
        try:
            price = float(f.get("price"))
            shares = float(f.get("shares"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {i + 1} 笔的价/量必须是数字") from exc
        for v, label in ((price, "价格"), (shares, "股数")):
            if v != v or v in (float("inf"), float("-inf")) or v <= 0:
                raise ValueError(f"第 {i + 1} 笔的{label}必须是正有限数")
        row = {"side": side, "date": d, "price": round(price, 4),
               "shares": round(shares, 2)}
        # 可选：这笔成交的**实际**费用（对账单上的数）。填了就以它为准，不再按费率估。
        if f.get("fee") not in (None, ""):
            try:
                fee = float(f["fee"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第 {i + 1} 笔的费用必须是数字") from exc
            if fee != fee or fee < 0:
                raise ValueError(f"第 {i + 1} 笔的费用不能是负数")
            row["fee"] = round(fee, 2)
        out.append(row)
    # ⚠️ 只按日期排，**同一天内保持录入顺序**（Python 的 sorted 是稳定排序）。
    #    成交明细没有时间字段，同日的先后只能靠录入顺序表达 —— 若把同日的买入
    #    一律排到卖出之前，"买→卖→买→卖"这种同日做 T 会被压成"买买卖卖"，
    #    两轮变成一轮，周期数与每轮的成本基准都不对了。
    #    代价：同日先录卖出再录买入会被判成超卖 —— 那本来就是录入错误，报错是对的。
    return sorted(out, key=lambda x: x["date"])


def _settle(fills: list[dict], fees_cfg: Optional[dict] = None) -> dict:
    """按成交**时序**逐笔结算：持仓、加权成本、已实现盈亏、持有天数。

    ⚠️ 必须按顺序维护剩余仓位，不能"所有买入求一个均价、所有卖出求一个均价"。
    后者有两个会算出错误结果、而界面上看着完全正常的情形：

    1. **卖出超过持仓**：买 100 卖 200 会凭空结算不存在的 100 股。
    2. **平仓后再买入**：10 买 100 → 20 全卖 → 30 再买 100，正确是"已实现 +1000、
       当前成本 30"；按总均价算会得到"已实现 0、成本 20" —— 后来的买入把
       **已经发生的**盈亏改写了。

    这两种错误会一路污染权益曲线、胜率、盈亏比、模式卡业绩、判断/执行归因与在险资金。

    ## 口径

    - **移动加权平均成本**（与券商对账口径一致）。每次卖出按**卖出那一刻**的持仓
      均价结转成本，剩余持仓的均价不因这次卖出而改变。
    - 持仓归零后再买入 = 开启**新的持仓周期**；已实现盈亏累加，成本重置。
    - `avg_cost`：**当前持仓**的加权成本（在险资金 / MFE-MAE / 收件箱都按这个口径读）。
      已全部平仓时给已实现部分的加权成本，让这些消费方仍有成本基准可用。
    - `amount`：整个过程中的**峰值占用资金**（这笔最多占用了多少钱），不是买入总额。
    - 只算**已实现**部分，未卖出的不计浮盈浮亏。
    - **`realized_pnl` 是净额（已扣费用）**。毛额另给 `gross_pnl`，费用给 `fees`。
      ⚠️ 对高换手的短线，佣金 + 印花税 + 过户费不是小数：一堆薄利交易在计费后
      可能接近持平甚至转亏，胜率与期望会被系统性高估。买入费用按卖出比例结转。

    卖出超过当时持仓会抛 `ValueError` —— 那是录入错误，不能默默算出一个数。
    """
    if not fills:
        return {"has_fills": False, "closed": False}
    fills = sorted(fills, key=lambda f: f["date"])  # 同日仍按录入先后结算
    buys = [f for f in fills if f["side"] == "buy"]
    if not buys:
        # 只有卖出、没有买入 —— 无从结算，如实说明而不是给一个 0
        raise ValueError("成交明细里只有卖出没有买入，无法结算：请补上买入成交")

    cfg = fees_cfg if fees_cfg is not None else load_fees()
    pos_shares = 0.0        # 当前持仓股数
    pos_cost = 0.0          # 当前持仓总成本
    pos_fee = 0.0           # 当前持仓对应的、尚未结转的买入费用
    realized_pnl = 0.0      # 累计已实现**毛**盈亏
    realized_cost = 0.0     # 已实现部分对应的成本（算百分比的分母）
    realized_shares = 0.0
    realized_fee = 0.0      # 已实现部分对应的费用（买入分摊 + 卖出）
    # ⚠️ 按**每笔卖出发生的那一天**记净盈亏。整笔累计额全部挂到最后一次卖出的话，
    #    分两天减仓时两天的盈亏会被并到后一天 —— 「单日最大亏损」这条规则就查不准。
    by_date: dict[str, float] = {}
    peak_capital = 0.0      # 峰值占用资金
    cycles = 0              # 持仓周期数（平仓后再买算新的一轮）

    sellable = 0.0
    inventory_day = None
    unverified_sales = []
    for n, f in enumerate(fills, 1):
        if f["date"] != inventory_day:
            inventory_day = f["date"]
            sellable = pos_shares
        if f["side"] == "buy":
            if pos_shares <= 1e-9:
                cycles += 1
            pos_shares += f["shares"]
            pos_cost += f["price"] * f["shares"]
            pos_fee += _fee_of(f, cfg)
            peak_capital = max(peak_capital, pos_cost)
        else:
            if f["shares"] > pos_shares + 1e-6:
                raise ValueError(
                    f"按日期结算的第 {n} 笔卖出 {f['shares']:g} 股，超过当时持有的 "
                    f"{pos_shares:g} 股 —— 请检查成交明细的顺序与数量")
            if f["shares"] > sellable + 1e-6:
                unverified_sales.append(f["date"])
            sellable = max(0, sellable - f["shares"])
            unit = pos_cost / pos_shares            # 卖出那一刻的持仓均价
            ratio = f["shares"] / pos_shares        # 这次卖掉了当前持仓的几成
            realized_pnl += (f["price"] - unit) * f["shares"]
            realized_cost += unit * f["shares"]
            realized_shares += f["shares"]
            # 买入费用按卖出比例结转 + 这次卖出自己的费用
            leg_fee = pos_fee * ratio + _fee_of(f, cfg)
            realized_fee += leg_fee
            by_date[f["date"]] = round(
                by_date.get(f["date"], 0.0) + (f["price"] - unit) * f["shares"] - leg_fee, 2)
            pos_fee -= pos_fee * ratio
            pos_cost -= unit * f["shares"]
            pos_shares -= f["shares"]
            if pos_shares <= 1e-9:                  # 落到 0 就归零，别留浮点残渣
                pos_shares = pos_cost = pos_fee = 0.0

    sells = [f for f in fills if f["side"] == "sell"]
    # 当前持仓成本优先；已全平则退回已实现部分的成本基准（消费方仍需要它）
    if pos_shares > 1e-9:
        avg_cost = pos_cost / pos_shares
    elif realized_shares > 0:
        avg_cost = realized_cost / realized_shares
    else:
        avg_cost = sum(f["price"] * f["shares"] for f in buys) / sum(f["shares"] for f in buys)

    out = {
        "has_fills": True,
        "buy_shares": round(sum(f["shares"] for f in buys), 2),
        "sell_shares": round(sum(f["shares"] for f in sells), 2),
        "open_shares": round(pos_shares, 2),        # 当前还持有多少
        "cycles": cycles,                           # >1 表示这条记录里有多轮进出
        "avg_cost": round(avg_cost, 4),
        "amount": round(peak_capital, 2),
        "closed": bool(sells) and pos_shares <= 1e-9,
        "first_buy": buys[0]["date"],
        "last_sell": sells[-1]["date"] if sells else None,
    }
    if sells:
        out["avg_sell"] = round(
            sum(f["price"] * f["shares"] for f in sells) / out["sell_shares"], 4)
        net = realized_pnl - realized_fee
        out["gross_pnl"] = round(realized_pnl, 2)      # 未计费用的毛额
        out["fees"] = round(realized_fee, 2)
        out["fees_are_estimated"] = not all(f.get("fee") is not None for f in fills)
        # ⚠️ `realized_pnl` 是**净额** —— 下游全部统计（权益曲线 / 胜率 / 盈亏比 /
        #    模式卡 / 归因）都读它，让它带费用才是使用者真正赚到的钱。
        out["realized_pnl"] = round(net, 2)
        out["realized_by_date"] = by_date      # {成交日: 当天的净已实现盈亏}
        out["realized_pct"] = round(net / realized_cost * 100, 2) if realized_cost else None
        # 持有天数按自然日算（跨周末的隔日单，日历天数才是真实占用时间）
        try:
            d0 = datetime.datetime.strptime(buys[0]["date"], "%Y-%m-%d")
            d1 = datetime.datetime.strptime(sells[-1]["date"], "%Y-%m-%d")
            out["hold_days"] = (d1 - d0).days
        except ValueError:
            pass
        out["is_t0"] = False  # 同日买卖不能证明有隔夜底仓可做T
        if unverified_sales:
            out["settlement_warning"] = "部分卖出未证明有足够隔夜可卖底仓，不认定为可执行的A股做T"
    return out


def add_trade(date: str, code: str, name: str, playbook: str,
              pnl_pct: Optional[float] = None, as_planned: Optional[bool] = None,
              note: str = "", fills: Optional[list[dict]] = None,
              planned_stop: Optional[float] = None,
              planned_target: Optional[float] = None, import_id: str | None = None) -> dict:
    """记一笔交易，并**自动钉上当时的市场环境**。

    两种记法（都支持，按你手边有什么填）：
    - **只填盈亏%**：最省事，适合事后补记。`pnl_pct` 自己填。
    - **填成交明细 `fills`**：`[{side:buy/sell, date, price, shares}, …]` ——
      支持分批建仓、做 T、隔日卖出。系统自动算加权成本、已实现盈亏、
      盈亏金额、持有天数、占用资金。⚠️ 只算已实现部分，不虚构浮盈。

    `as_planned` = 这笔是否按计划执行。

    `planned_stop` / `planned_target` = **下单时**写下的计划止损价 / 目标价。
    ⚠️ 必须是当时写下的值；在险资金按 `planned_stop` 计算，事后补填会让该口径失真。
    没写就留空。
    """
    if import_id is not None and (not isinstance(import_id, str) or len(import_id) != 64 or any(c not in "0123456789abcdef" for c in import_id)):
        raise ValueError("导入记录标识无效")
    date = validate_trade_date(date)
    if playbook not in PLAYBOOKS:
        raise ValueError(f"打法需为 {PLAYBOOKS} 之一，得到 {playbook!r}")
    code = str(code).strip().zfill(6)
    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"证券代码需为 6 位数字，得到 {code!r}")
    if pnl_pct is not None:
        pnl_pct = float(pnl_pct)
        # NaN/Inf 会顺着 json 写进账本，之后所有统计（均值/胜率）永久变成 NaN
        if pnl_pct != pnl_pct or pnl_pct in (float("inf"), float("-inf")):
            raise ValueError("盈亏必须是有限数字")
        if not -100.0 <= pnl_pct <= 1000.0:
            raise ValueError(f"盈亏 {pnl_pct}% 超出合理范围，请检查是否填错")
    if as_planned is not None and not isinstance(as_planned, bool):
        raise ValueError("as_planned 只能是 true / false / 不填")

    planned_stop = _price(planned_stop, "计划止损价")
    planned_target = _price(planned_target, "计划目标价")

    norm_fills = _norm_fills(fills or [])
    settled = _settle(norm_fills)
    # 有成交明细时，盈亏以算出来的为准（用户填的当参考）—— 算出来的才对得上明细
    if settled.get("realized_pct") is not None:
        pnl_pct = settled["realized_pct"]

    trade = {
        "id": uuid.uuid4().hex[:12],
        "date": date,
        "fills": norm_fills,
        "settled": settled,
        "code": code,
        "name": (name or "").strip(),
        "playbook": playbook,
        "pnl_pct": None if pnl_pct is None else round(float(pnl_pct), 2),
        "as_planned": as_planned,
        # ⚠️ 下单时写下的计划边界。在险资金拿它算，所以绝不事后倒推
        "planned_stop": planned_stop,
        "planned_target": planned_target,
        "note": (note or "").strip()[:500],
        "created_at": china_now().strftime("%Y-%m-%d %H:%M:%S") + " CST",
        # 环境快照在记录时固化（事后市场数据可能被重算，当时面对的环境不会变）。
        # 买入日与卖出日的环境各存一份 —— 只存一份会丢掉离场时的环境信息。
        "market": _market_context(settled.get("first_buy") or date),
        "stock": _stock_context(settled.get("first_buy") or date, code),
        "exit_market": (_market_context(settled["last_sell"])
                        if settled.get("last_sell") and settled["last_sell"] != (
                            settled.get("first_buy") or date) else None),
    }
    if import_id is not None:
        trade["legacy_import_id"] = import_id
    with _LOCK:
        trades = _load_raw()
        if import_id is not None and any(t.get("legacy_import_id") == import_id for t in trades):
            return {"ok": True, "skipped": True}
        trades.append(trade)
        if not _save(trades):
            raise RuntimeError("账本写入失败（磁盘满或权限问题），这一笔没记上")
    return {"ok": True, "trade": trade}


def update_trade(trade_id: str, *, fills=None, note=None, as_planned=_UNSET,
                 planned_stop=_UNSET, planned_target=_UNSET) -> dict:
    """更新一笔已有交易 —— 主要用途是**给持仓中的记录补上后来的成交**。

    只能改传进来的字段，其余原样保留。传了 `fills` 就按新的明细整份重算。

    ## 为什么必须有它

    没有这个接口时，买入当天记下计划止损、第二天要补卖出，只能删掉重录：
    `created_at` 与"下单时写下计划边界"这个证据就没了；若把卖出另建一笔，
    系统又不会把它和原买入配对，持仓、盈亏、MFE/MAE 全错。

    ## ⚠️ 计划边界改了要留痕

    在险资金的全部意义建立在"止损是**下单时**写下的"。事后改动不禁止
    （填错了要能纠正），但必须记下 `planned_edited_at`，让读数的人知道
    这个值不再是当时那个。
    """
    with _LOCK:
        trades = _load_raw()
        idx = next((i for i, t in enumerate(trades) if t.get("id") == trade_id), None)
        if idx is None:
            return {"ok": False, "reason": "没有这条记录"}
        t = dict(trades[idx])

        if fills is not None:
            norm = _norm_fills(fills)
            settled = _settle(norm)             # 超卖等录入错误会在这里抛 ValueError
            t["fills"] = norm
            t["settled"] = settled
            # 盈亏以明细算出来的为准（与 add_trade 同口径）
            # ⚠️ 算不出来时要**清掉**旧值，不能留着：撤掉卖出之后如果 pnl_pct 还挂着，
            #    报表会显示一个与现有成交明细对不上的数字，而界面上看不出来。
            if settled.get("realized_pct") is not None:
                t["pnl_pct"] = round(float(settled["realized_pct"]), 2)
            else:
                t["pnl_pct"] = None
            # 卖出日的市场环境跟着变；不再有跨日卖出时同样要清掉旧的
            first_buy, last_sell = settled.get("first_buy"), settled.get("last_sell")
            t["exit_market"] = (_market_context(last_sell)
                                if last_sell and last_sell != (first_buy or t.get("date"))
                                else None)
        if note is not None:
            t["note"] = str(note).strip()[:500]
        if as_planned is not _UNSET:
            if as_planned is not None and not isinstance(as_planned, bool):
                raise ValueError("as_planned 只能是 true / false / 不填")
            t["as_planned"] = as_planned
        for key, val in (("planned_stop", planned_stop), ("planned_target", planned_target)):
            if val is _UNSET:
                continue
            new_val = _price(val, "计划止损价" if key == "planned_stop" else "计划目标价")
            if new_val != t.get(key):
                t[key] = new_val
                # ⚠️ 留痕：在险资金按这个值算，读数的人有权知道它是不是事后改的
                t["planned_edited_at"] = china_now().strftime("%Y-%m-%d %H:%M:%S") + " CST"
        t["updated_at"] = china_now().strftime("%Y-%m-%d %H:%M:%S") + " CST"
        trades[idx] = t
        if not _save(trades):
            raise RuntimeError("账本写入失败，这次修改没保存上")
    return {"ok": True, "trade": t}


def delete_trade(trade_id: str) -> dict:
    with _LOCK:
        trades = _load_raw()
        left = [t for t in trades if t.get("id") != trade_id]
        if len(left) == len(trades):
            return {"ok": False, "reason": "没有这条记录"}
        if not _save(left):
            raise RuntimeError("账本写入失败，这条没删掉")
    return {"ok": True, "removed": 1}


def all_trades() -> list[dict]:
    """整本账，**不截断**。

    ⚠️ 持仓聚合与风控统计必须走这个：用 `list_trades(limit=N)` 的话，
    账本超过 N 条之后，较早但仍未平仓的记录会被静默漏掉 ——
    持仓少一只、在险资金偏小，而两边都不会报错。
    """
    return sorted(_load_raw(), key=lambda t: (t.get("date", ""), t.get("created_at", "")),
                  reverse=True)


def list_trades(limit: Optional[int] = 200, offset: int = 0) -> dict:
    trades = sorted(_load_raw(), key=lambda t: (t.get("date", ""), t.get("created_at", "")),
                    reverse=True)
    return {"trades": trades[offset:None if limit is None else offset + limit], "total": len(trades), "offset": offset}


def _bucket_stats(rows: list[dict]) -> dict:
    """一组交易的统计。只统计填了盈亏的记录。

    有成交明细的另外汇总**盈亏金额** —— 百分比与金额是两个口径，
    仓位不同时二者结论可能相反。
    """
    vals = [t["pnl_pct"] for t in rows if t.get("pnl_pct") is not None]
    money = [t["settled"]["realized_pnl"] for t in rows
             if isinstance(t.get("settled"), dict)
             and t["settled"].get("realized_pnl") is not None]
    base = {"count": len(rows), "scored": len(vals),
            "money_scored": len(money),
            "net_pnl": round(sum(money), 2) if money else None}
    if not vals:
        return {**base, "win_rate": None, "avg": None, "best": None, "worst": None}
    wins = [v for v in vals if v > 0]
    decided = len(wins) + sum(1 for v in vals if v < 0)
    return {
        **base,
        # ⚠️ 持平（0%）不计入胜率分母 —— 分母只含分出胜负的笔数。
        #    这与 `modes._stats` 是同一个口径；两处必须保持一致，否则同一页上
        #    会并排出现两个算法不同的"胜率"，而界面上看不出区别。
        #    全部持平时分母为 0，胜率给 None（不是 0）—— 那是"没有胜负可言"，不是"没赢过"。
        "win_rate": round(len(wins) / decided, 3) if decided else None,
        "avg": round(mean(vals), 2),
        "best": round(max(vals), 2),
        "worst": round(min(vals), 2),
    }


def stats() -> dict:
    """自我体检：按情绪环境 / 打法 / 是否按计划 分组看自己的历史表现。

    ⚠️ 这是对使用者自己历史行为的统计，不是对市场的预测，不产出任何操作建议。
    样本量小时读数没有意义，UI 会一并标注样本量。
    """
    trades = _load_raw()
    if not trades:
        return {"available": False, "reason": "还没有交易记录"}

    by_phase: dict[str, list[dict]] = {}
    by_playbook: dict[str, list[dict]] = {}
    by_planned: dict[str, list[dict]] = {}
    by_boards: dict[str, list[dict]] = {}
    by_hold: dict[str, list[dict]] = {}      # 做 T / 隔日 / 多日
    for t in trades:
        ph = (t.get("market") or {}).get("emotion_phase") or "未记录"
        by_phase.setdefault(ph, []).append(t)
        by_playbook.setdefault(t.get("playbook") or "其它", []).append(t)
        planned = t.get("as_planned")
        by_planned.setdefault(
            "按计划" if planned is True else ("计划外" if planned is False else "未标注"), []
        ).append(t)
        st = t.get("stock") or {}
        if st.get("in_limit_up"):
            b = int(st.get("boards") or 1)
            key = "首板" if b == 1 else ("2板" if b == 2 else "3板及以上")
        else:
            key = "非涨停"
        by_boards.setdefault(key, []).append(t)
        st = t.get("settled") or {}
        hd = st.get("hold_days")
        by_hold.setdefault(
            "同日买卖(底仓待核)" if hd == 0 else ("隔日" if hd == 1 else
                                       (f"持有{hd}天" if isinstance(hd, int) else "未填明细")),
            [],
        ).append(t)

    return {
        "available": True,
        "overall": _bucket_stats(trades),
        "by_phase": {k: _bucket_stats(v) for k, v in by_phase.items()},
        "by_playbook": {k: _bucket_stats(v) for k, v in by_playbook.items()},
        "by_planned": {k: _bucket_stats(v) for k, v in by_planned.items()},
        "by_boards": {k: _bucket_stats(v) for k, v in by_boards.items()},
        "by_hold": {k: _bucket_stats(v) for k, v in by_hold.items()},
        "playbooks": PLAYBOOKS,
    }

"""反思闭环 —— 明天关注点 T+1 命中回看 + 经验注入（借 TradingAgents reflection 思路，短线化）。

流程：
1. 每次复盘的『明天关注点』(focus_struct) 随 review 落盘（server 已存 reviews/{date}.json）。
2. `evaluate(prediction_date)`：取该预测的次一交易日，查预测龙头在次日的实际涨跌，
   算「方向命中率」「龙头次日均涨」，落盘 reflections/{date}.json。
3. `get_past_context()`：把近几次命中回看摘成一段，注入裁判 prompt —— 让它记吃记打，
   避免重复追高/踏空。

数据：akshare 名称→代码、交易日历、个股次日涨跌幅。akshare 在函数内惰性 import（不拖累主流程加载）。
"""

from __future__ import annotations

from duanxian.paths import data_path
import datetime
import re
import json
import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

from . import trade_calendar
from .util import china_today, is_a_share_closed, safe_join, validate_trade_date

_REVIEW_DIR = data_path("reviews")
_REFLECT_DIR = data_path("reflections")


_NAME_CODE: dict = {}


def _name_code_map() -> dict:
    """A 股 名称→代码 全表。只缓存非空结果（瞬时网络失败不毒化缓存）。"""
    global _NAME_CODE
    if _NAME_CODE:
        return _NAME_CODE
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        cols = {c.lower(): c for c in df.columns}
        cc = cols.get("code", df.columns[0])
        nc = cols.get("name", df.columns[1])
        m = {str(r[nc]).strip(): str(r[cc]).strip().zfill(6) for _, r in df.iterrows()}
        if m:
            _NAME_CODE = m
        return m
    except Exception:
        return {}


def _next_trade_date(date: str) -> Optional[str]:
    """prediction_date 的次一交易日（交易日历统一在 trade_calendar）。"""
    return trade_calendar.next_trade_date(date)


def _today() -> str:
    return china_today()


def _tx_symbol(code: str) -> str:
    """6 位代码 → 腾讯 hist 需要的带市场前缀符号。"""
    code = code.zfill(6)
    if code[0] in ("5", "6", "9"):
        return "sh" + code
    if code[0] in ("0", "2", "3"):
        return "sz" + code
    if code[0] in ("4", "8"):
        return "bj" + code
    return "sh" + code


def _stock_change_on(code: str, eval_date: str) -> Optional[float]:
    """个股 eval_date 的隔夜收益（前一交易日收盘 → eval_date 收盘，%）。

    用腾讯 hist（stock_zh_a_hist_tx，不被本机代理封，区别于被封的东财 push2his）；
    该接口无涨跌幅列，用收盘价环比自算。
    """
    try:
        import akshare as ak

        sym = _tx_symbol(code)
        start = (datetime.datetime.strptime(eval_date, "%Y-%m-%d") - datetime.timedelta(days=15)).strftime("%Y%m%d")
        end = eval_date.replace("-", "")
        df = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start, end_date=end)
        if df is None or len(df) < 2 or "close" not in df.columns or "date" not in df.columns:
            return None
        df = df.reset_index(drop=True)
        hits = df.index[df["date"].astype(str) == eval_date].tolist()
        if not hits or hits[0] == 0:
            return None
        i = hits[0]
        prev = float(df.iloc[i - 1]["close"])
        cur = float(df.iloc[i]["close"])
        if prev <= 0:
            return None
        return round((cur - prev) / prev * 100, 2)
    except Exception:
        return None


def _resolve_code(name: str, name2code: dict) -> Optional[str]:
    """龙头名 → 代码。精确匹配；板块名/带括号说明的匹配不到就跳过。"""
    n = (name or "").strip()
    if n in name2code:
        return name2code[n]
    # 去掉括号补充说明再试一次（如「盛屯矿业（龙虎榜强势）」）
    base = n.split("（")[0].split("(")[0].strip()
    return name2code.get(base)


# ---------------- 读写 ----------------
def _load_review(date: str) -> Optional[dict]:
    path = os.path.join(_REVIEW_DIR, f"{date}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _save_reflection(result: dict) -> None:
    os.makedirs(_REFLECT_DIR, exist_ok=True)
    try:
        path = safe_join(_REFLECT_DIR, f"{result['prediction_date']}.json")
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001  不静默吞掉
        logger.warning("保存 reflection 失败(%s)：%s", result.get("prediction_date"), exc)


def _after_close() -> bool:
    """当日是否已收盘（上海 15:05 后）——避免盘中把动态价当收盘价封存（ #3 + 时区）"""
    return is_a_share_closed()


_PHASE_EXPECT = {"冰点": "up", "修复": "up", "发酵": "up", "亢奋": "down", "退潮": "down"}

_MIN_SAMPLES_FOR_RATE = 30

_SIGNAL_LABEL = {
    "promotion_rate": "晋级率",
    "money_effect_median": "赚钱效应中位数",
    "limit_up_count": "涨停家数",
}

# 少于这么多路信号 → 结论只算**暂定**（provisional），日后会被自动重评。
# 三路取多数的前提是有多数可取；只剩一路时那一路的噪声就是结论。
_MIN_SIGNALS = 2

_EVAL_SCHEMA = 2


def _delta_dir(cur: Optional[float], prev: Optional[float], eps: float) -> Optional[int]:
    """比较两个读数的方向：+1 升 / 0 基本持平 / -1 降；任一缺失返回 None。

    阈值比较加 1e-9 容差：0.33-0.30 的浮点结果略大于 0.03，不加容差会把
    「恰好等于阈值」判成趋势——信号方向不该被浮点噪声翻转。
    """
    if cur is None or prev is None:
        return None
    diff = cur - prev
    if abs(diff) <= eps + 1e-9:
        return 0
    return 1 if diff > 0 else -1


def evaluate_phase(prediction_date: str, eval_date: str) -> Optional[dict]:
    """验证『情绪档位』判断：预测日的档位隐含的方向 vs 次日实际情绪走向。

    这是**市场层面**的靶子——不依赖个股预测，所以自带的 research 包也能被打分。
    三路信号：晋级率 / 赚钱效应中位数 / 涨停家数，各投一票取多数。
    """
    review = _load_review(prediction_date)
    phase = ((review or {}).get("focus") or {}).get("emotion_phase")
    expect = _PHASE_EXPECT.get(phase or "")
    if not expect:
        return None

    from . import emotion_metrics as em  # 延迟导入：em 依赖 data，data 不依赖本模块

    prev_m = (review or {}).get("emotion_metrics") or {}
    if not (prev_m.get("promotion") or {}).get("available"):
        # 老复盘没存指标（本功能之前生成的）→ 晋级率现算，它任意历史日都可算
        prev_m = {"promotion": em.promotion_rates(prediction_date),
                  "money_effect": prev_m.get("money_effect", {})}
    cur_m = em.build_metrics(eval_date)

    def _pick(m: dict, group: str, *path):
        g = m.get(group) or {}
        if not g.get("available"):
            return None
        node = g
        for k in path:
            node = (node or {}).get(k) if isinstance(node, dict) else None
        return node

    signals = {
        # 晋级率与赚钱效应用百分点/百分比阈值，避免把噪声当趋势
        "promotion_rate": _delta_dir(
            _pick(cur_m, "promotion", "overall", "rate"),
            _pick(prev_m, "promotion", "overall", "rate"), eps=0.03),
        "money_effect_median": _delta_dir(
            _pick(cur_m, "money_effect", "median"),
            _pick(prev_m, "money_effect", "median"), eps=0.5),
        "limit_up_count": _delta_dir(
            _pick(cur_m, "promotion", "limit_up_count"),
            _pick(cur_m, "promotion", "prev_limit_up_count"), eps=5),
    }
    votes = [v for v in signals.values() if v is not None]
    if not votes:
        return None  # 三路信号全缺 → 不评，留待重评

    score = sum(votes)
    actual = "up" if score > 0 else ("down" if score < 0 else "flat")
    hit = None if actual == "flat" else (actual == expect)
    provisional = len(votes) < _MIN_SIGNALS
    return {
        "phase": phase,
        "expected_direction": expect,
        "actual_direction": actual,
        "hit": hit,
        "signal_count": len(votes),
        "missing_signals": [_SIGNAL_LABEL[k] for k, v in signals.items() if v is None],
        "provisional": provisional,
        "signals": {_SIGNAL_LABEL[k]: v for k, v in signals.items()},
        "detail": {
            "promotion_rate": {"prev": _pick(prev_m, "promotion", "overall", "rate"),
                               "cur": _pick(cur_m, "promotion", "overall", "rate")},
            "money_effect_median": {"prev": _pick(prev_m, "money_effect", "median"),
                                    "cur": _pick(cur_m, "money_effect", "median")},
            "limit_up_count": {"prev": _pick(cur_m, "promotion", "prev_limit_up_count"),
                               "cur": _pick(cur_m, "promotion", "limit_up_count")},
        },
    }


# ---------------- 核心：命中评估 ----------------
def evaluate(prediction_date: str, eval_date: Optional[str] = None) -> Optional[dict]:
    """评估 prediction_date 的『明天关注点』在次一交易日的命中情况。"""
    prediction_date = validate_trade_date(prediction_date)
    review = _load_review(prediction_date)
    focus = (review or {}).get("focus")
    if not focus:
        return None
    eval_date = eval_date or _next_trade_date(prediction_date)
    if not eval_date or eval_date > _today():
        return None  # 次日还没到，暂不可评
    if eval_date == _today() and not _after_close():
        return None

    name2code = _name_code_map()
    ret_cache: dict = {}

    def _ret(code: str) -> Optional[float]:
        if code not in ret_cache:
            ret_cache[code] = _stock_change_on(code, eval_date)
        return ret_cache[code]

    dirs_eval = []
    for d in focus.get("focus_directions", []):
        perf = []
        seen = set()
        for name in d.get("leader_candidates", []):
            code = _resolve_code(name, name2code)
            if not code or code in seen:  # 同方向内也去重
                continue
            seen.add(code)
            chg = _ret(code)
            if chg is not None:
                perf.append({"name": name, "code": code, "ret": chg})
        avg = round(sum(p["ret"] for p in perf) / len(perf), 2) if perf else None
        dirs_eval.append({
            "direction": d.get("direction", ""),
            "avg_next_ret": avg,
            "hit": (avg is not None and avg > 0),
            "leaders": perf,
        })

    # 情绪档位验证：市场层面靶子，不依赖个股预测 —— 这是自带 research 包唯一的闭环
    phase_eval = evaluate_phase(prediction_date, eval_date)

    # 明日验证表逐条核验：昨晚立的条件今天成立没有
    verify_results, verify_summary = _verify_items(prediction_date, eval_date, review)

    evaluable = [d for d in dirs_eval if d["avg_next_ret"] is not None]
    if not evaluable and not phase_eval:
        return None

    uniq_rets = [v for v in ret_cache.values() if v is not None]  # 唯一龙头等权
    overall = round(sum(uniq_rets) / len(uniq_rets), 2) if uniq_rets else None
    hit_rate = (round(sum(1 for d in evaluable if d["hit"]) / len(evaluable), 2)
                if evaluable else None)
    result = {
        "eval_schema": _EVAL_SCHEMA,
        "prediction_date": prediction_date,
        "eval_date": eval_date,
        "emotion_phase": focus.get("emotion_phase"),
        "directions": dirs_eval,
        "overall_next_ret": overall,
        "direction_hit_rate": hit_rate,
        "direction_hits": sum(1 for d in evaluable if d["hit"]),
        "direction_samples": len(evaluable),
        "phase_eval": phase_eval,
        # 明日验证表：逐条核验 + 汇总（无验证项时为空，不影响其它靶子）
        "verification": verify_results,
        "verification_summary": verify_summary,
        # 暂定记录：信号不足或口径升级时会被自动重评，不算定论
        "provisional": bool((phase_eval or {}).get("provisional")),
    }
    _save_reflection(result)
    return result


def _verify_items(prediction_date: str, eval_date: str,
                  review: Optional[dict]) -> tuple[list, dict]:
    """核验昨晚立下的「明日验证条件」。

    昨天的读数直接从 review 文件里取（复盘时已算好、已落盘），今天的现算。
    取不到就返回空 —— 老复盘（本功能之前生成的）没有验证项，属正常。
    """
    focus = (review or {}).get("focus") or {}
    try:
        from . import verification as _vf

        # 用户自设的条件优先于 AI 的 —— 那是他自己昨晚写下的预期
        items = _vf.merged_items(prediction_date, focus.get("verification_items") or [])
    except Exception:  # noqa: BLE001
        items = focus.get("verification_items") or []
    if not items:
        return [], {}
    try:
        from . import emotion_metrics as em
        from . import verification
        from .data import get_market_facts

        prev_metrics = (review or {}).get("emotion_metrics") or {}
        prev_facts = (review or {}).get("market_facts") or {}
        cur_metrics = em.build_metrics(eval_date)
        _, cur_facts = get_market_facts(eval_date)
        results = verification.verify(items, prev_metrics, prev_facts, cur_metrics, cur_facts)
        try:
            results = verification.attach_baselines(results, end=prediction_date)
        except Exception as exc:  # noqa: BLE001  基准是增强，算不出就照常给命中率
            logger.warning("基准发生率计算失败(%s)：%s", prediction_date, exc)
        return results, verification.summarize(results)
    except Exception as exc:  # noqa: BLE001  核验失败不该拖垮整个回看
        logger.warning("验证条件核验失败(%s→%s)：%s", prediction_date, eval_date, exc)
        return [], {}


def _needs_reeval(path: str) -> bool:
    """已存在的回看记录是否需要重评"""
    try:
        with open(path, encoding="utf-8") as fh:
            r = json.load(fh)
    except Exception:  # noqa: BLE001  读不出来 = 坏文件，重评
        return True
    return bool(r.get("provisional")) or r.get("eval_schema") != _EVAL_SCHEMA


def auto_evaluate_prior(current_date: str, *, strict: bool = False) -> Optional[dict]:
    """回评最近可评的一期；不适用的日期跳过，资料故障不阻塞更早候选。"""
    from . import review_store
    failures = []
    for pd in sorted((d for d in review_store.dates() if d < current_date), reverse=True):
        try:
            done = os.path.join(_REFLECT_DIR, f"{pd}.json")
            if os.path.exists(done) and not _needs_reeval(done):
                continue
            next_day = _next_trade_date(pd)
            if (not next_day or next_day > current_date or next_day > _today()
                    or (next_day == _today() and not _after_close())):
                continue
            focus = (_load_review(pd) or {}).get("focus") or {}
            if not focus:
                continue
            res = evaluate(pd, eval_date=next_day)
            if res:
                return res
            # No recognized phase / resolvable leader means no evaluable target,
            # rather than a failed market-data request.
            recognized = focus.get("emotion_phase") in _PHASE_EXPECT
            if not recognized:
                names = _name_code_map()
                recognized = any(_resolve_code(name, names)
                    for d in focus.get("focus_directions", [])
                    for name in d.get("leader_candidates", []))
            if recognized:
                failures.append(pd)
        except Exception as exc:
            logger.warning("自动回评失败（%s）：%s", pd, type(exc).__name__)
            failures.append(pd)
    if strict and failures:
        raise RuntimeError("回评所需资料尚未完整，未完成，可重试")
    return None


def reflections_as_of(end: str) -> list[dict]:
    """Only evaluations already available at the requested session may be used."""
    validate_trade_date(end)
    return [r for r in _all_reflections() if isinstance(r, dict)
            and isinstance(r.get("prediction_date"), str)
            and isinstance(r.get("eval_date"), str)
            and r["prediction_date"] < r["eval_date"] <= end
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["eval_date"])]


def latest_reflection(end: str | None = None) -> Optional[dict]:
    if end is not None:
        rows = reflections_as_of(end)
        return max(rows, key=lambda r: r["eval_date"]) if rows else None
    if not os.path.isdir(_REFLECT_DIR):
        return None
    files = sorted(f for f in os.listdir(_REFLECT_DIR) if f.endswith(".json"))
    if not files:
        return None
    try:
        with open(os.path.join(_REFLECT_DIR, files[-1]), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _all_reflections() -> list[dict]:
    """按预测日升序读出全部命中回看记录。"""
    if not os.path.isdir(_REFLECT_DIR):
        return []
    out = []
    for f in sorted(f for f in os.listdir(_REFLECT_DIR) if f.endswith(".json")):
        try:
            with open(os.path.join(_REFLECT_DIR, f), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except Exception:  # noqa: BLE001  单个坏文件不拖累整体
            continue
    return out


def scoreboard(end: str | None = None) -> dict:
    """AI 判断的累计战绩 —— 「回测→先验→判断→次日验证」这个环的最后一块"""
    recs = reflections_as_of(end) if end else _all_reflections()
    decided = [(r, (r.get("phase_eval") or {})) for r in recs]
    phase_rows = [(r, pe) for r, pe in decided if pe.get("hit") is not None]

    by_phase: dict[str, dict] = {}
    for _r, pe in phase_rows:
        b = by_phase.setdefault(pe["phase"], {"n": 0, "hit": 0})
        b["n"] += 1
        b["hit"] += 1 if pe["hit"] else 0
    for b in by_phase.values():
        b["hit_rate"] = round(b["hit"] / b["n"], 3) if b["n"] else None

    hits = sum(1 for _r, pe in phase_rows if pe["hit"])
    flat = sum(1 for _r, pe in decided if pe.get("hit") is None and pe.get("phase"))

    stock_hits = stock_samples = 0
    stock_days = 0
    for r in recs:
        rate = r.get("direction_hit_rate")
        if rate is None:
            continue
        stock_days += 1
        n = r.get("direction_samples")
        h = r.get("direction_hits")
        if not isinstance(n, int) or not isinstance(h, int):
            n = len([d for d in (r.get("directions") or []) if d.get("avg_next_ret") is not None]) or 1
            h = round(rate * n)
        stock_samples += n
        stock_hits += h

    decided_n = len(phase_rows)
    return {
        "phase": {
            "decided": decided_n,
            "hits": hits,
            "flat": flat,          # 持平未分胜负，不计入分母
            "next_day_direction_rate": round(hits / decided_n, 3) if decided_n else None,
            # 样本够不够撑得起一个百分比 —— 前端据此决定显不显示醒目数字
            "enough_samples": decided_n >= _MIN_SAMPLES_FOR_RATE,
            "min_samples": _MIN_SAMPLES_FOR_RATE,
            "by_phase": by_phase,
        },
        "stock": {
            "days": stock_days,              # 有个股靶子的天数
            "samples": stock_samples,        # 累计评估过的方向数 = 命中率的分母
            "hits": stock_hits,
            "hit_rate": round(stock_hits / stock_samples, 3) if stock_samples else None,
        },
        "recent": [
            {"prediction_date": r.get("prediction_date"),
             "eval_date": r.get("eval_date"),
             "phase": (r.get("phase_eval") or {}).get("phase"),
             "hit": (r.get("phase_eval") or {}).get("hit")}
            for r in recs[-10:]
        ],
    }


def get_past_context(limit: int = 5, *, end: str | None = None) -> str:
    if end is not None:
        rows = sorted(reflections_as_of(end), key=lambda r: r["eval_date"])[-limit:]
        # Deliberately exclude the all-time scoreboard and personal stock trades.
        return ("历史市场观察验证（截至复盘日，非收益记录）：\n" + json.dumps([
            {"prediction_date": r["prediction_date"], "eval_date": r["eval_date"],
             "phase_eval": r.get("phase_eval")} for r in rows], ensure_ascii=False)) if rows else ""
    """把近 limit 次命中回看摘成一段，供裁判 prompt 参考（记吃记打）。"""
    if not os.path.isdir(_REFLECT_DIR):
        return ""
    files = sorted(f for f in os.listdir(_REFLECT_DIR) if f.endswith(".json"))[-limit:]
    if not files:
        return ""
    lines = ["【过往预测命中回看（供参考，避免重复追高/踏空）】"]

    # 累计战绩：让裁判知道自己历史上判得准不准，尤其是哪个档位常判错
    sb = scoreboard()["phase"]
    if sb["decided"]:
        weak = "；".join(
            f"{p} {b['hit']}/{b['n']}" for p, b in sb["by_phase"].items() if b["n"]
        )
        rate = sb.get("next_day_direction_rate")
        rate_txt = (f"，方向验证率 {rate:.0%}" if sb.get("enough_samples") and rate is not None
                    else f"（样本 {sb['decided']}/{sb.get('min_samples', 30)} 天，尚不足以谈比率）")
        lines.append(
            f"- 你过往档位隐含的次日方向：{sb['hits']}/{sb['decided']} 次对上{rate_txt}，"
            f"另有 {sb['flat']} 次次日持平未分胜负；分档位 [{weak}]。"
            f"⚠️ 这量的是「档位→次日方向」这套映射，不等于档位判断本身的对错。"
        )
    for f in reversed(files):
        try:
            with open(os.path.join(_REFLECT_DIR, f), encoding="utf-8") as fh:
                r = json.load(fh)
        except Exception:
            continue
        parts = []

        # 情绪档位验证（市场层面靶子，任何 prompt 包都有）
        pe = r.get("phase_eval") or {}
        if pe.get("phase"):
            verdict = {True: "判对", False: "判错", None: "持平未分胜负"}[pe.get("hit")]
            parts.append(
                f"档位[{pe['phase']}]（预期情绪{'走强' if pe['expected_direction'] == 'up' else '转弱'}"
                f"、实际{ {'up': '走强', 'down': '转弱', 'flat': '持平'}[pe['actual_direction']] }→{verdict}）"
            )
        elif r.get("emotion_phase"):
            parts.append(f"档位[{r['emotion_phase']}]")

        # 个股靶子（仅当 prompt 包产出龙头预测时才有）
        hr = r.get("direction_hit_rate")
        if hr is not None:
            onr = r.get("overall_next_ret")
            onr_s = f"{onr:+.1f}%" if onr is not None else "—"
            dirs = "、".join(
                f"{d['direction']}({d['avg_next_ret']:+.1f}%)" if d.get("avg_next_ret") is not None
                else f"{d['direction']}(无数据)"
                for d in r.get("directions", [])
            )
            parts.append(f"方向命中率{hr:.0%}、龙头次日均涨{onr_s}；分方向[{dirs}]")

        if parts:
            lines.append(f"- {r['prediction_date']}预测→{r.get('eval_date','次日')}：" + "；".join(parts))
    return "\n".join(lines) if len(lines) > 1 else ""

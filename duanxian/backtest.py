"""短线策略回测 —— 量一量你的模式到底行不行。

短线本质是概率游戏，但绝大多数人从没量过自己那套打法的**期望值**。
本模块把几种经典短线模式跑成统计：胜率、期望、分布、按日曲线、以及
**分情绪状态的表现**（同一套打法在发酵期和退潮期完全是两回事）。

⚠️ **产出的是「规则的历史统计」，不是「明天买什么」。** 策略是对一个**群体**
（如"所有首板"）的规则，样本里的个股是历史事实、不是推荐；本模块不产出任何前瞻标的。

## 数据来源：一天一次请求

`stock_zt_pool_previous_em(date=X)` 直接给出「在 X-1 涨停的股票，在 X 当天的表现」，
含 `昨日连板数`（分档）、`涨跌幅`（结果）、`昨日封板时间`、`所属行业`。

⚠️ **`昨日封板时间` 是「最后封板时间」，不是首次封板时间**（2026-07-24 实测：
与当日涨停池 `stock_zt_pool_em` 对照，116 只**全部**等于「最后封板时间」，
只有从未开板的 68 只碰巧等于首次封板）。所以本模块的"封板早晚"实际编码了两件事：
封得早 **且** 此后没再炸开。例：证通电子 09:31 首封 → 炸板 6 次 → 10:33 最终封板，
会落在"10:00-11:00"档而不是"开盘秒板"。结论要按这个口径读。
→ 60 个交易日的回测只要 60 次请求，且历史结果不会再变 → **落盘缓存**。

## ⚠️⚠️ 口径（最重要的一节，别糊弄自己）

**这里做的是「市场现象统计」，不是「策略回测」，更不是"打板策略的收益"。**
三者的区别必须分清：

| 层级 | 是什么 | 本模块 |
|---|---|---|
| ① 市场现象统计 | 某类票在历史上的表现 | **就是这一层** |
| ② 用户规则历史模拟 | 按你自己的规则、含成交概率与滑点回放 | 没做 |
| ③ 用户真实成交统计 | 你实际下单成交的结果 | 见 `journal.py` |

### 样本选择偏差（无法用现有数据消除，只能如实披露）

样本来自 `stock_zt_pool_previous_em` = **前一日收盘时留在涨停池里的票**。
这是「事后知道封住了」的名单，天然漏掉：

- **冲板但最终没封住的票**（炸板池）—— 实测 2026-07-23：141 只冲过板、
  只有 116 只封住，**18% 的失败样本完全不在统计里**
- 排队但没成交的（拿不到）
- 盘中看似可打、收盘被淘汰的
- 实际成交价与理论涨停价的差异（一字板根本买不进）

**真实打板时你并不知道它最后会不会封住。** 所以本模块的数字是
「昨日**收盘**涨停样本的次日表现」，不等于"打板这套打法的期望"——
后者必然更差。策略名保留是为了可读性，读数时请记住这层差别。

- 收益口径 = 昨日收盘涨停 → 当日收盘涨跌幅，等权，不含滑点与成交概率。
"""

from __future__ import annotations
from duanxian.paths import data_path
from .cache_policy import fresh as cache_fresh, write as write_cache

import hashlib
import json
import os
from statistics import mean, median
from typing import Callable, Optional

from . import data as _data  # noqa: F401  仅为副作用：清代理 + 注入项目根 sys.path
from . import emotion_metrics as em
from . import trade_calendar
from .util import atomic_write_json

_CACHE_DIR = data_path("cache/prev_pool")
# 回测**结果**的缓存目录。放在这里而不是 server.py：缓存该跟着算它的东西走，
# 这样复盘链路要读先验时不必反向依赖 web 层。
RESULT_DIR = data_path("backtest")

# 早盘封板的时间界（HHMMSS 字符串）。⚠️ 比的是**最后封板时间**（见模块 docstring）：
# 10:00 前"最终封住"= 早盘封板且此后没再炸开，比单纯"首封早"更强的信号。
_EARLY_SEAL = "100000"


def _fetch_prev_pool(date: str) -> Optional[list[dict]]:
    """取「前一交易日涨停股在 date 当天的表现」，数据定稿后落盘缓存。"""
    is_past = trade_calendar.is_settled(date)   # 含"今天且已收盘"——语料过期不候
    path = os.path.join(_CACHE_DIR, f"{date}.json")
    if is_past and os.path.isfile(path) and cache_fresh(path, date):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001  缓存坏了当没有
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
                # ⚠️ 带上这两个字段是为了用「现价==涨停价」判涨停 —— 这是数据源直接给的事实，
                # 自动适配所有涨跌幅制度（创业板/科创板 ST 是 20% 不是 5%、北交所 920 号段是 30%、
                # 以及任何未来的制度调整）。比任何硬编码规则表都可靠。
                "close": float(r["最新价"]) if r.get("最新价") is not None else None,
                "limit_price": float(r["涨停价"]) if r.get("涨停价") is not None else None,
            })
        except (KeyError, ValueError, TypeError):
            continue  # 单行脏数据不拖累整天
    if not rows:
        return None

    if is_past:
        write_cache(path, rows)   # 写失败不影响返回，函数内部已吞
    return rows


# ---------------------------------------------------------------- 策略定义
# 每条策略 = 对「昨日涨停股」这个群体的一个过滤规则。不含任何个股选择。
#
# filter 签名 (row, ctx) -> bool。ctx 携带当天的群体上下文（如当日主线行业），
# 让"只做主线"这类规则也能表达 —— 它依赖的是**当天涨停股的行业分布**，
# 仍是对群体的统计，不是挑个股。
_LATE_SEAL = "143000"   # 14:30 后封板 = 尾盘偷袭，与早封形成对照组


def _is_early(row: dict) -> bool:
    t = (row.get("seal_time") or "").strip()
    return bool(t) and t <= _EARLY_SEAL


def _is_late(row: dict) -> bool:
    t = (row.get("seal_time") or "").strip()
    return bool(t) and t >= _LATE_SEAL


STRATEGIES: dict[str, dict] = {
    "首板打板": {
        "desc": "昨日首次涨停的全部个股，今天持有到收盘",
        "filter": lambda r, _c: r["prev_boards"] == 1,
    },
    "首板·早封": {
        "desc": "昨日首板且 10:00 前**最终封板**（早盘封住且此后没再炸开）",
        "filter": lambda r, _c: r["prev_boards"] == 1 and _is_early(r),
    },
    "首板·尾盘封": {
        "desc": "昨日首板但 14:30 后才**最终封板**（含尾盘偷袭与全天反复炸板后回封）",
        "filter": lambda r, _c: r["prev_boards"] == 1 and _is_late(r),
    },
    # ⚠️ 叫"涨停数前二行业"而不是"主线"：短线语境里的主线是**题材**，不是东财行业分类。
    # 多个行业可能属于同一题材，一个行业也可能装着完全不同的炒作原因。等题材语料
    # （问财涨停原因）囤够了再做真正的题材版。
    "首板·涨停数前二行业": {
        "desc": "昨日首板且属于当日涨停家数最多的两个**行业**（注：行业≠题材）",
        "filter": lambda r, c: r["prev_boards"] == 1 and r["sector"] and r["sector"] in c["main_sectors"],
    },
    "连板接力": {
        "desc": "昨日已 2 板及以上的个股，今天持有到收盘",
        "filter": lambda r, _c: r["prev_boards"] >= 2,
    },
    "高标接力": {
        "desc": "昨日 3 板及以上的高位标，今天持有到收盘",
        "filter": lambda r, _c: r["prev_boards"] >= 3,
    },
    "全体涨停": {
        "desc": "昨日涨停的全部个股（基准线，用来对照其它策略有没有超额）",
        "filter": lambda _r, _c: True,
    },
}

# 主线判定：当日涨停股里家数最多的前 N 个行业
_MAIN_SECTOR_TOP = 2


def _day_context(rows: list[dict]) -> dict:
    """当天的群体上下文。目前只有"主线行业"，后续加规则可往里塞。"""
    counts: dict[str, int] = {}
    for r in rows:
        s = (r.get("sector") or "").strip()
        if s:
            counts[s] = counts.get(s, 0) + 1
    top = sorted(counts, key=lambda k: counts[k], reverse=True)[:_MAIN_SECTOR_TOP]
    return {"main_sectors": set(top), "sector_counts": counts}


# 封板时间分档（HHMMSS 上界，含）。⚠️ 分的是**最后封板时间**（见模块 docstring）。
# 分档而不是给一个可调阈值：与其让人猜"9:30 还是 10:00 更好"，
# 不如把整条曲线摆出来，悬崖在哪一眼就看见。
SEAL_BUCKETS: list[tuple[str, str]] = [
    ("开盘秒板", "093500"),
    ("9:35-10:00", "100000"),
    ("10:00-11:00", "110000"),
    ("11:00-14:00", "140000"),
    ("14:00后", "150500"),
]


def seal_time_curve(per_day: dict[str, list[dict]]) -> list[dict]:
    """首板按**最后封板时间**分档的收益曲线 —— 回答"什么时候把板封住值多少钱"。

    ⚠️ 是最后封板时间不是首封（见模块 docstring）：所以"晚"同时意味着
    "封得晚"或"中途炸开过又回封"，两者都指向同一件事——这个板不稳。
    只统计首板（连板股的封板时间含义不同，混在一起会污染结论）。
    """
    buckets: dict[str, list[dict]] = {name: [] for name, _ in SEAL_BUCKETS}
    unknown: list[dict] = []
    for rows in per_day.values():
        for r in rows:
            if r["prev_boards"] != 1:
                continue
            t = (r.get("seal_time") or "").strip()
            if not t:
                unknown.append(r)   # 时间缺失单独归堆，不硬塞进某一档
                continue
            for name, upper in SEAL_BUCKETS:
                if t <= upper:
                    buckets[name].append(r)
                    break
            else:
                buckets[SEAL_BUCKETS[-1][0]].append(r)

    out = [{"bucket": name, **_stats([x["ret"] for x in rs], rs)}
           for name, rs in buckets.items()]
    if unknown:
        out.append({"bucket": "封板时间缺失",
                    **_stats([x["ret"] for x in unknown], unknown)})
    return out


# 判涨停统一走 `data.is_limit_up` —— 复盘链路（emotion_metrics / market_facts /
# stats_context）用的就是它。⚠️ 这里**不要再写一份**：两份实现一旦分叉，回测与复盘
# 对"什么算涨停"就会用不同标准，而两边的数字各自看着都正常。
from .data import is_limit_up as _is_limit_up  # noqa: E402


def _stats(rets: list[float], rows: Optional[list[dict]] = None) -> dict:
    """一组收益的统计。样本为空返回 None 字段，不伪装成 0。

    `rows` 与 `rets` 一一对应时，"再涨停率"按每只票**自己的涨停幅度**判（10/20/30cm、ST 5cm）；
    不传 rows 则该项为 None —— 宁可不给，也不用统一阈值糊弄（见 `_limit_pct`）。
    """
    if not rets:
        return {"sample": 0, "win_rate": None, "avg": None, "median": None,
                "best": None, "worst": None, "limit_up_rate": None}
    lur = None
    if rows is not None and len(rows) == len(rets):
        judged = [_is_limit_up(row) for row in rows]
        ok = [j for j in judged if j is not None]
        # 判不了的（老缓存缺价格字段）不进分母，别拿一半样本冒充全体
        lur = round(sum(1 for j in ok if j) / len(ok), 3) if ok else None
    return {
        "sample": len(rets),
        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 3),
        "avg": round(mean(rets), 2),
        "median": round(median(rets), 2),
        "best": round(max(rets), 2),
        "worst": round(min(rets), 2),
        "limit_up_rate": lur,
    }


def _day_score(date: str) -> Optional[float]:
    """某日的情绪分（复用 emotion_metrics 的缓存摘要）。"""
    s = em.day_summary(date)
    if not s:
        return None
    # 这里只需要"相对强弱"，用一个不依赖窗口的粗分：涨停家数 × 连板高度 ÷ 炸板率惩罚
    br = s["broken_rate"] if s["broken_rate"] is not None else 0.3
    return round(s["limit_up"] * (1 + s["highest_consec"]) * (1 - br), 1)


def _entry_day_scores(day_list: list[str], seq: list[str]) -> list[tuple[str, Optional[float]]]:
    """给每个收益日配上「**入场前一交易日**」的情绪分，用于分档。

    ⚠️ 这一步是为了避免前视偏差。回测口径：`per_day[d]` 的收益是
    「d-1 涨停股在 d 当天的表现」，决策发生在 **d-1 收盘**。原先用 `_day_score(d)`
    （当天情绪）分档 = 拿"事后才知道的当天情绪"解释当天收益 —— 看板上"情绪弱时首板
    期望更低"会被读成可操作先验，但决策时点根本看不到 d 当天的情绪。改用 d-1
    （决策时已收盘、可见），"情绪弱环境"才等于"你决策当下看到的情绪弱"。

    `seq` = 覆盖窗口的连续交易日（升序）。用它建 d→前一交易日 映射，一次拿好，
    避免对每个回测日各调一次 `prev_trade_date`（15 天就是 15 次网络）。
    序列没覆盖到的（罕见、窗口最左端）才回退到单次网络查询。
    """
    pos = {d: i for i, d in enumerate(seq)}
    out: list[tuple[str, Optional[float]]] = []
    for d in day_list:
        i = pos.get(d)
        prev = seq[i - 1] if (i is not None and i > 0) else trade_calendar.prev_trade_date(d)
        out.append((d, _day_score(prev) if prev else None))
    return out


def run_backtest(days: int = 60, strategies: Optional[list[str]] = None) -> dict:
    """跑回测。

    Args:
        days: 回看多少个已收盘交易日。
        strategies: 只跑指定策略；None = 全部。
    """
    names = [n for n in (strategies or list(STRATEGIES)) if n in STRATEGIES]
    if not names:
        return {"available": False, "reason": "没有有效策略"}

    dates = trade_calendar.last_trade_dates(days)
    if len(dates) < 5:
        return {"available": False, "reason": f"可用交易日不足（{len(dates)}/5）"}

    # 逐日取「昨日涨停股今日表现」
    per_day: dict[str, list[dict]] = {}
    missing = []
    for d in dates:
        rows = _fetch_prev_pool(d)
        if rows is None:
            missing.append(d)
            continue
        per_day[d] = rows
    if len(per_day) < 5:
        return {"available": False,
                "reason": f"取数成功天数不足（{len(per_day)}/{len(dates)}）",
                "missing_days": missing}

    # 情绪分分档：按**入场前一交易日**的情绪强弱把收益日三等分（不是当天，见 _entry_day_scores）。
    # 这样"情绪弱环境"= 你决策当下看到的情绪弱，分环境期望才是可操作的先验、不是事后条件化。
    day_list = sorted(per_day)
    seq = trade_calendar.trade_dates_ending_at(day_list[-1], days + 8) if day_list else []
    scored = _entry_day_scores(day_list, seq)
    valid = sorted(s for _, s in scored if s is not None)
    if len(valid) >= 6:
        lo_cut, hi_cut = valid[len(valid) // 3], valid[len(valid) * 2 // 3]
    else:
        lo_cut = hi_cut = None

    def _regime(score: Optional[float]) -> str:
        if score is None or lo_cut is None:
            return "未知"
        return "情绪弱" if score <= lo_cut else ("情绪强" if score >= hi_cut else "情绪中")

    regimes = {d: _regime(s) for d, s in scored}

    # 每天的群体上下文算一次，所有策略共用
    ctx_by_day = {d: _day_context(rows) for d, rows in per_day.items()}

    results = {}
    for name in names:
        f: Callable[[dict, dict], bool] = STRATEGIES[name]["filter"]
        # 留着整行（不只是 ret）：再涨停率要按每只票自己的涨停幅度判（见 _limit_pct）
        all_rows: list[dict] = []
        daily = []
        by_regime: dict[str, list[dict]] = {"情绪强": [], "情绪中": [], "情绪弱": [], "环境未覆盖": []}
        equity = 100.0
        for d in sorted(per_day):
            ctx = ctx_by_day[d]
            hit = [r for r in per_day[d] if f(r, ctx)]
            rets = [r["ret"] for r in hit]
            all_rows.extend(hit)
            reg = regimes.get(d, "未知")
            by_regime[reg if reg in by_regime else "环境未覆盖"].extend(hit)
            day_avg = round(mean(rets), 2) if rets else None
            if day_avg is not None:
                equity *= (1 + day_avg / 100)   # 等权每日满仓，日频复利
            daily.append({"date": d, "sample": len(rets), "avg": day_avg,
                          "regime": reg, "equity": round(equity, 2)})
        results[name] = {
            "desc": STRATEGIES[name]["desc"],
            "overall": _stats([r["ret"] for r in all_rows], all_rows),
            "by_regime": {k: _stats([r["ret"] for r in v], v) for k, v in by_regime.items()},
            "daily": daily,
            "final_equity": round(equity, 2),
        }

    corpus = corpus_days()
    return {
        "available": True,
        # ⚠️ 让调用方（尤其前端）必须面对这层口径，别把它当"策略收益"
        "layer": "market_phenomenon",
        "sample_caveat": (
            "样本 = 昨日**收盘**留在涨停池里的票（事后名单）。"
            "包含一字板等未必能成交的股票，未排除排队买不到的情况；冲板失败未回封者不在样本内。"
            "没有成交模拟，不能推导真实收益或打法期望。这是市场现象统计，不是策略回测。"
        ),
        "schema": _RESULT_SCHEMA,
        # 口径 + 语料指纹，读缓存时逐项比对（见 load_result）
        "fingerprint": _fingerprint(_corpus_dates_in_window(min(per_day), max(per_day))),
        "days_requested": days,
        "days_used": len(per_day),
        "date_from": min(per_day),
        "date_to": max(per_day),
        # ⚠️ 不静默截断：取数失败的日子明说
        "missing_days": missing,
        # 已囤语料规模——窗口能开多长，取决于这个，不取决于数据源留存
        "corpus": {"days": len(corpus),
                   "from": corpus[0] if corpus else None,
                   "to": corpus[-1] if corpus else None},
        "regime_cuts": {"low": lo_cut, "high": hi_cut},
        "seal_curve": seal_time_curve(per_day),
        "strategies": results,
    }


def capture(date: Optional[str] = None) -> dict:
    """把某天的「前一日涨停股当日表现」抓下来存进缓存。

    ⚠️ **这是回测语料的唯一来源，且过期不候**：数据源只留最近约 15 个交易日，
    没在窗口期内抓下来的日子就永久缺失了。所以每次复盘跑完都顺手抓一次（1 次请求），
    语料会随着日子推移自己长长——半年后就有半年的窗口，不再受数据源留存限制。
    """
    date = date or trade_calendar.latest_session()
    if not date:
        return {"ok": False, "reason": "取不到最近收盘交易日"}
    rows = _fetch_prev_pool(date)
    if rows is None:
        return {"ok": False, "date": date, "reason": "取数失败"}
    return {"ok": True, "date": date, "rows": len(rows),
            "cached": os.path.isfile(os.path.join(_CACHE_DIR, f"{date}.json"))}


def corpus_days() -> list[str]:
    """已囤下来的语料日期（升序）。回测窗口的真实上限。"""
    try:
        return sorted(f[:-5] for f in os.listdir(_CACHE_DIR) if f.endswith(".json"))
    except FileNotFoundError:
        return []


def result_path(days: int) -> str:
    return os.path.join(RESULT_DIR, f"last{days}.json")


# 结果结构版本。**加了新字段就 +1** —— 否则旧缓存缺字段，界面看着正常但少东西。
_RESULT_SCHEMA = 2

# 策略**实现**版本。⚠️ 改任何一条 `filter` 的判断逻辑、或改 `_stats` / 情绪分公式 /
# `_day_context` 的算法时，手动 +1。
# 为什么需要它：filter 是 lambda，没法序列化比对。下面的 fingerprint 能自动抓住
# 所有**参数**变化（封板时间界、主线取前几、分档边界…），但抓不住"参数没变、逻辑变了"。
# 那种情况下缓存会继续返回旧数字，而界面完全看不出异样。
_STRATEGY_REVISION = 7   # v7：补环境未覆盖桶与可成交性披露；v6：加样本偏差披露（layer/sample_caveat）   # v2：分环境改用入场前一日情绪（修前视偏差）——旧缓存 by_regime 口径已错，必须失效


def _corpus_dates_in_window(date_from: Optional[str], date_to: Optional[str]) -> list[str]:
    """窗口内**磁盘上现存**的语料日期。

    ⚠️ 指纹读写必须用这同一个口径。别用"本次计算实际用到的日期"写、用磁盘现存的读：
    未定稿的当天会进计算但不落盘，两边永远差一天 → 指纹永不匹配 → 每次请求都重算
    （30 秒一次，界面看着只是"有点慢"）。
    """
    if not date_from or not date_to:
        return []
    return [d for d in corpus_days() if date_from <= d <= date_to]


def _fingerprint(corpus_dates: list[str]) -> dict:
    """结果缓存指纹：口径变了或语料变了，缓存就该作废。

    - `revision`/`strategies`/`params`：**口径**。策略集、描述、各类阈值参数。
    - `corpus`：**语料**。窗口内真实用到的交易日。只比 `date_to` 是不够的——
      后台补抓了窗口中间原本缺失的一天时，最新日期没变，但样本数和统计全变了。
      这里存日期列表的摘要，补齐与损坏恢复都能被发现。
    """
    return {
        "revision": _STRATEGY_REVISION,
        "strategies": {k: v["desc"] for k, v in sorted(STRATEGIES.items())},
        "params": {
            "early_seal": _EARLY_SEAL,
            "late_seal": _LATE_SEAL,
            "main_sector_top": _MAIN_SECTOR_TOP,
            # ⚠️ 转成 list：指纹要跟"从 JSON 读回来的那份"逐字段比，而 json 往返会把
            # tuple 变成 list —— 留着 tuple 的话现算值永远 != 缓存值，指纹恒不匹配，
            # 表现是每次请求都重算（30 秒），界面只是"有点慢"，看不出是 bug。
            "seal_buckets": [list(b) for b in SEAL_BUCKETS],
        },
        "corpus": {"n": len(corpus_dates), "digest": hashlib.sha1(
            ",".join(sorted(corpus_dates)).encode()).hexdigest()[:16]},
    }


def load_result(days: int) -> Optional[dict]:
    """读已缓存的回测结果；没有、**策略集已变**、或**结构版本过时**则 None。

    ⚠️ 这两道校验都必不可少：新增策略/新增字段都不会改变 `date_to`，
    光看日期的话缓存会一直返回旧结构的结果，新东西永远不出现。
    """
    try:
        with open(result_path(days), encoding="utf-8") as fh:
            r = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    if not r.get("available"):
        return None
    if r.get("schema") != _RESULT_SCHEMA:
        return None   # 结构变了 → 缓存作废，重算
    if set(r.get("strategies") or {}) != set(STRATEGIES):
        return None   # 策略集变了 → 缓存作废，重算
    fp = r.get("fingerprint")
    if not isinstance(fp, dict):
        return None   # 老缓存没指纹 → 一律重算
    # 口径部分现算现比；语料部分拿缓存自己记的日期算，比的是"当时用的语料"与
    # "现在能用的语料"是否一致（corpus_days 变了说明补抓/损坏恢复过）
    want = _fingerprint(_corpus_dates_in_window(r.get("date_from"), r.get("date_to")))
    if fp != want:
        return None
    return r


def prior_context(days: int = 30) -> str:
    """给复盘裁判的**历史先验**：同一套打法在不同情绪环境下的历史期望。

    ⚠️ **只读缓存，绝不在复盘链路里现算** —— 复盘已经要跑 6 分钟，不能再为它多等半分钟。
    没有缓存就返回空串，裁判照常工作。

    用途是**校准对情绪档位的判断**（"情绪弱时首板类历史期望为负"这种统计事实），
    不是给操作指令。
    """
    bt = load_result(days)
    if not bt:
        return ""
    lines = [
        f"【策略历史先验｜{bt['date_from']}~{bt['date_to']} 共 {bt['days_used']} 个交易日的统计】",
        "（口径：昨日**收盘**涨停→当日收盘、等权、不含滑点与成交概率；样本是事后名单，"
        "冲板未封住的不在内，也未完整模拟一字板可成交性、费用与滑点；不能据此推导真实打板期望。分环境按入场前一交易日的情绪强弱。"
        "这是**市场现象统计**，不是策略回测，也不是操作指令。）",
    ]
    for name, r in (bt.get("strategies") or {}).items():
        o = r["overall"]
        if not o["sample"] or o["avg"] is None:
            continue
        seg = "、".join(
            f"{k}{v['avg']:+.2f}%" for k, v in (r.get("by_regime") or {}).items() if v["sample"]
        )
        lines.append(f"- {name}：整体期望 {o['avg']:+.2f}%（中位 {o['median']:+.2f}%）；分环境 {seg}")
    return "\n".join(lines)


def render_backtest(bt: dict) -> str:
    """渲染成给 AI 读的文本块。"""
    if not bt.get("available"):
        return f"[回测不可用：{bt.get('reason', '未知')}]"
    lines = [
        f"[短线策略回测 {bt['date_from']} ~ {bt['date_to']}｜{bt['days_used']} 个交易日]",
        "口径：昨日涨停 → 当日收盘涨跌幅（等权、不含滑点与打板成交概率，实盘只会更差）",
    ]
    if bt["missing_days"]:
        lines.append(f"⚠️ 有 {len(bt['missing_days'])} 天取数失败已剔除：{'、'.join(bt['missing_days'][:5])}")
    curve = [b for b in (bt.get("seal_curve") or []) if b["sample"]]
    if curve:
        seg = "；".join(f"{b['bucket']} {b['avg']:+.2f}%(n={b['sample']})" for b in curve)
        lines.append(f"· 首板按封板时间分档：{seg}")

    for name, r in bt["strategies"].items():
        o = r["overall"]
        if not o["sample"]:
            lines.append(f"· {name}：无样本")
            continue
        seg = "；".join(
            f"{k} 期望{v['avg']:+.2f}%/胜率{v['win_rate']:.0%}"
            for k, v in r["by_regime"].items() if v["sample"]
        )
        lines.append(
            f"· {name}（{r['desc']}）：样本 {o['sample']}，胜率 {o['win_rate']:.0%}，"
            f"期望 {o['avg']:+.2f}%、中位 {o['median']:+.2f}%，再涨停率 {o['limit_up_rate']:.0%}；"
            f"分环境 → {seg or '样本不足'}"
        )
    return "\n".join(lines)

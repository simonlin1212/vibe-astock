"""明日市场验证表 —— 把「明天关注点」从叙述变成**可核验的条件**。


老手写复盘结论不会只写"关注电网设备"，而会写"明天用这几个市场事实验证今晚的判断"：
最高标是否继续抬高高度、昨日首板的红盘率是否改善、炸板率是否继续上升……
第二天一开盘就能逐条打勾 —— **这不是"明天买什么"，是"明天用哪些事实检验昨晚判断"**。


让 LLM 自由写"关注承接力度"这类条件，读着像回事，但**第二天没法自动核验** ——
要么靠人肉判断（等于没做），要么再喂给 LLM 判断（不可靠且烧 token）。

所以这里反过来：**给一份固定的可核验指标集，让裁判从中挑 3-5 项并给出预期方向**。
次日用已有数据自动比对，逐条出「成立 / 不成立 / 数据不足」。
指标全部是**市场层面**读数（涨停家数、晋级率、炸板率……），落在安全港内，
不涉及任何个股、点位或买卖时机。
"""

from __future__ import annotations

from duanxian.paths import data_path
from typing import Callable, Optional

# 方向的含义：预期这个读数明天往哪走
DIRECTIONS = ["上升", "下降", "持平"]


def _pick(d: dict, *path, need_available: bool = True):
    """从嵌套 dict 里取值；中途 available=False 或缺键都返回 None。"""
    node = d
    for i, k in enumerate(path):
        if not isinstance(node, dict):
            return None
        if i == 0 and need_available and not node.get(k, {}).get("available", True):
            return None
        node = node.get(k)
        if node is None:
            return None
    return node if isinstance(node, (int, float)) else None


class Metric:
    """一个可核验的市场读数。

    `eps` 是"变了才算变"的阈值 —— 没有它的话，涨停家数 40→41 会被当成"上升"，
    把噪声当成判断兑现。
    `higher_is_hotter` 只用于给 UI 提示语义（炸板率升 = 情绪转弱），不影响核验。
    """

    def __init__(self, key: str, label: str, hint: str, eps: float,
                 getter: Callable[[dict, dict], Optional[float]],
                 higher_is_hotter: bool = True, unit: str = ""):
        self.key, self.label, self.hint = key, label, hint
        self.eps, self.getter = eps, getter
        self.higher_is_hotter, self.unit = higher_is_hotter, unit


METRICS: list[Metric] = [
    Metric("limit_up_count", "涨停家数", "市场整体的赚钱意愿", eps=5,
           getter=lambda m, f: _pick(m, "promotion", "limit_up_count"), unit="家"),
    Metric("highest_board", "最高连板高度", "情绪的天花板，压缩=周期见顶", eps=0.5,
           getter=lambda m, f: _pick(m, "ladder_gap", "highest"), unit="板"),
    Metric("promotion_1to2", "1进2 晋级率", "最敏感的一档，走低=接力意愿收缩", eps=0.05,
           getter=lambda m, f: _pick(m, "promotion", "tiers", "1进2", "rate"), unit=""),
    Metric("money_effect_median", "赚钱效应中位数", "多数人的体感", eps=0.5,
           getter=lambda m, f: _pick(m, "money_effect", "median"), unit="%"),
    Metric("broken_rate", "炸板率", "资金分歧程度（炸板未回封占尝试数）", eps=0.05,
           getter=lambda m, f: _pick(f, "seal_quality", "broken_rate"),
           higher_is_hotter=False, unit=""),
    Metric("deep_loss_count", "跌超5%家数", "大面的量级，判退潮先看它", eps=5,
           getter=lambda m, f: _pick(f, "loss_effect", "deep_loss_5_count"),
           higher_is_hotter=False, unit="家"),
    Metric("theme_concentration", "头部题材集中度", "一枝独秀还是遍地开花", eps=0.05,
           getter=lambda m, f: _pick(f, "theme_structure", "concentration"), unit=""),
    Metric("market_limit_down", "全市场跌停家数", "极端风险的直接读数", eps=5,
           getter=lambda m, f: _pick(f, "loss_effect", "market_limit_down"),
           higher_is_hotter=False, unit="家"),
]

_BY_KEY = {m.key: m for m in METRICS}



def describe_items(items: list[dict], metrics: dict, facts: dict) -> list[dict]:
    """给今晚落盘的验证条件补上「今日基准值 + 变了才算变的阈值」。

    只写"预期上升"，第二天没法对账 —— 从多少涨到多少才算上升？
    阈值本来就定义在上面的 METRICS 里，今日读数也在同一份复盘里，
    不带出去的话，读者第二天只能凭感觉，而凭感觉的结论怎么变都能自圆其说。

    认不出的 metric key 原样返回（裁判偶尔会写出菜单外的键），不猜、不编。
    """
    out = []
    for it in items or []:
        m = _BY_KEY.get(str(it.get("metric") or ""))
        if m is None:
            out.append(dict(it))
            continue
        base = m.getter(metrics or {}, facts or {})
        out.append({**it, "label": m.label, "unit": m.unit, "eps": m.eps,
                    "base_value": base, "higher_is_hotter": m.higher_is_hotter})
    return out

def metric_menu() -> str:
    """给裁判 prompt 的可选指标清单。"""
    return "\n".join(f"  - {m.key}（{m.label}）：{m.hint}" for m in METRICS)


def _actual_direction(cur: Optional[float], prev: Optional[float], eps: float) -> Optional[str]:
    if cur is None or prev is None:
        return None
    diff = cur - prev
    if abs(diff) <= eps + 1e-9:      # 同 reflection._delta_dir：浮点容差，别让噪声翻转方向
        return "持平"
    return "上升" if diff > 0 else "下降"


def verify(items: list[dict], prev_metrics: dict, prev_facts: dict,
           cur_metrics: dict, cur_facts: dict) -> list[dict]:
    """逐条核验昨晚立下的条件。

    返回每项：预期方向 / 昨日值 / 今日值 / 实际方向 / 是否成立。
    数据取不到时 `verified=None`（不足以判定），**不算判错** —— 把"没数据"
    和"判错了"混为一谈会污染战绩。
    """
    out = []
    for it in items or []:
        key = it.get("metric")
        m = _BY_KEY.get(key)
        if not m:
            continue
        prev_v = m.getter(prev_metrics or {}, prev_facts or {})
        cur_v = m.getter(cur_metrics or {}, cur_facts or {})
        actual = _actual_direction(cur_v, prev_v, m.eps)
        expect = it.get("direction")
        out.append({
            "metric": key,
            "label": m.label,
            "unit": m.unit,
            "expect": expect,
            "reason": it.get("reason", ""),
            "prev_value": prev_v,
            "cur_value": cur_v,
            "actual": actual,
            "verified": None if actual is None or expect not in DIRECTIONS else (actual == expect),
        })
    return out


# 方向常见到这个程度，就认为"判对了没什么信息量"
_TRIVIAL_BASELINE = 0.70
# 方向罕见到这个程度，判对一次含量高
_RARE_BASELINE = 0.30

_SERIES_KEY = {
    "limit_up_count": "limit_up",
    "highest_board": "highest_board",
    "promotion_1to2": "promotion_1to2",
    "money_effect_median": "money_effect",
    "broken_rate": "broken_rate",
    "deep_loss_count": "deep_loss",
}

_MIN_BASELINE_SAMPLES = 12


def direction_baseline(metric_key: str, days: int = 60,
                       end: Optional[str] = None) -> dict:
    """某个指标各方向在历史上的发生频率。

    用 `stats_context.series()` 的历史读数逐日算 d-1 → d 的方向，
    再按 `Metric.eps` 判"变了才算变"。样本不足返回 available=False。
    """
    m = _BY_KEY.get(metric_key)
    if m is None:
        return {"available": False, "reason": f"未知指标 {metric_key!r}"}
    sk = _SERIES_KEY.get(metric_key)
    if sk is None:
        return {"available": False,
                "reason": f"{m.label} 没有历史序列，算不出基准发生率（不拿别的指标凑）"}

    from . import stats_context as sc

    rows = sc.series(days=days, end=end)
    vals = [(r.get("date"), r.get(sk)) for r in rows]
    counts = {d: 0 for d in DIRECTIONS}
    n = 0
    for (_, prev), (_, cur) in zip(vals, vals[1:]):
        d = _actual_direction(cur, prev, m.eps)
        if d is None:
            continue
        counts[d] += 1
        n += 1
    if n < _MIN_BASELINE_SAMPLES:
        return {"available": False, "samples": n,
                "reason": f"只有 {n} 对可比日，样本太少，基准发生率会误导"}
    return {
        "available": True,
        "metric": metric_key,
        "label": m.label,
        "samples": n,
        "eps": m.eps,
        "rates": {d: round(counts[d] / n, 3) for d in DIRECTIONS},
    }


def _baseline_note(rate: Optional[float], label: str = "", eps: Optional[float] = None) -> str:
    """把基准发生率翻成一句人话"""
    if rate is None:
        return ""
    if rate <= 1e-9:
        tail = (f"（{label}的「持平」要求变化不超过 {eps:g}）"
                if (label and eps is not None) else "")
        return f"历史上一次都没出现过这个方向，这条几乎不可能被验证成立{tail}"
    if rate >= _TRIVIAL_BASELINE:
        return "这个方向历史上本来就常发生，判对了信息量低"
    if rate <= _RARE_BASELINE:
        return "少见方向，判对一次含量高"
    return ""


def attach_baselines(results: list[dict], days: int = 60,
                     end: Optional[str] = None) -> list[dict]:
    """给核验结果补上基准发生率与超额。原地不改，返回新列表（不可变原则）。"""
    cache: dict[str, dict] = {}
    out = []
    for r in results or []:
        key = r.get("metric")
        if key not in cache:
            cache[key] = direction_baseline(key, days=days, end=end)
        b = cache[key]
        rate = None
        if b.get("available"):
            rate = (b.get("rates") or {}).get(r.get("expect"))
        item = dict(r)
        item["baseline"] = rate
        item["baseline_samples"] = b.get("samples") if b.get("available") else None
        item["baseline_reason"] = None if b.get("available") else b.get("reason")
        m = _BY_KEY.get(key)
        item["baseline_note"] = _baseline_note(
            rate, m.label if m else "", m.eps if m else None)
        item["eps"] = m.eps if m else None
        # 单条的"超额"：判定成立记 1、不成立记 0，减去基准
        v = r.get("verified")
        item["edge"] = None if (v is None or rate is None) else round((1.0 if v else 0.0) - rate, 3)
        out.append(item)
    return out


def summarize(results: list[dict]) -> dict:
    """核验结果汇总。分母只算判定得了的条目。"""
    decided = [r for r in results if r.get("verified") is not None]
    hit = sum(1 for r in decided if r["verified"])
    rate = round(hit / len(decided), 3) if decided else None
    withb = [r for r in decided if r.get("baseline") is not None]
    exp = sum(r["baseline"] for r in withb) / len(withb) if withb else None
    hit_withb = sum(1 for r in withb if r["verified"]) / len(withb) if withb else None
    return {
        "total": len(results),
        "decided": len(decided),
        "hit": hit,
        "rate": rate,
        "undecidable": len(results) - len(decided),
        # 同一批（有基准的）条目上：实际命中率 vs 随口按历史常态猜的命中率
        "baseline_covered": len(withb),
        "expected_rate": None if exp is None else round(exp, 3),
        "edge": None if (exp is None or hit_withb is None) else round(hit_withb - exp, 3),
        "edge_note": ("超额 = 命中率 减去 基准发生率。约等于 0 说明判的都是本来就会发生的事；"
                      "为负说明还不如按历史常态随口猜。"),
    }


_EXTRACT_SKELETON = """{
  "items": [
    {"metric": "指标key", "direction": "上升/下降/持平", "reason": "这条为什么能验证今晚的判断"}
  ]
}"""


def extract_items(llm, focus_md: str, phase: str = "", *, strict: bool = False) -> list[dict]:
    """从已产出的复盘结论里，提炼 2-5 条明日可核验条件。

    失败返回空列表 —— 验证条件是增强，不该拖垮整条复盘。
    """
    from pydantic import BaseModel, Field, field_validator

    keys = {m.key for m in METRICS}

    class _Item(BaseModel):
        metric: str = Field(description="指标 key")
        direction: str = Field(description="上升/下降/持平")
        reason: str = Field(min_length=2, description="理由")

        @field_validator("direction")
        @classmethod
        def _d(cls, v: str) -> str:
            for d in DIRECTIONS:
                if d in v:
                    return d
            raise ValueError(f"方向需含 {DIRECTIONS} 之一")

        @field_validator("metric")
        @classmethod
        def _m(cls, v: str) -> str:
            v = v.strip()
            if v not in keys:
                raise ValueError(f"未知指标 {v!r}，只能从 {sorted(keys)} 选")
            return v

    class _Items(BaseModel):
        items: list[_Item] = Field(min_length=2, max_length=5)

    prompt = f"""下面是今晚的 A 股短线复盘结论{f'（情绪档位：{phase}）' if phase else ''}：

{focus_md[:2500]}

请挑 2-5 个**明日可核验的市场读数**，用来第二天检验今晚这份判断对不对。
只能从下面这份清单里选 metric（填 key），并给出预期方向（上升/下降/持平）与理由。

{metric_menu()}

要求：
- 挑**最能分辨这份判断对错**的读数：判退潮就挑退潮该有的变化，判修复就挑修复该有的。
- 方向要和今晚的判断一致 —— 这是自我检验，不是许愿。
- ⚠️ 这不是"明天买什么"，是"明天用哪些客观读数证明或证伪今晚的档位判断"。
  只涉及市场层面读数，不涉及任何个股、点位或买卖时机。"""

    try:
        from .structured import invoke_json_schema

        _, obj = invoke_json_schema(
            llm, prompt, _Items, lambda o: "", "验证条件抽取", _EXTRACT_SKELETON, strict=strict,
        )
        if obj is None:
            return []
        return [it.model_dump() for it in obj.items]
    except Exception:  # noqa: BLE001  旧工作流允许增强缺失；严格复盘必须出声
        if strict:
            raise
        return []


import os

from .util import atomic_write_json

_USER_DIR = data_path("verification")
_USER_SCHEMA = 1


def _user_path(date: str) -> str:
    return os.path.join(_USER_DIR, f"{date}.json")


def load_user_items(date: str) -> list[dict]:
    """读某日用户自设的验证条件。"""
    path = _user_path(date)
    if not os.path.isfile(path):
        return []
    try:
        import json

        with open(path, encoding="utf-8") as fh:
            env = json.load(fh)
        if env.get("schema") == _USER_SCHEMA and env.get("date") == date:
            return env.get("items") or []
    except Exception:  # noqa: BLE001  坏了当没有，用户可以重设
        pass
    return []


def save_user_items(date: str, items: list[dict]) -> dict:
    """存用户自设条件。校验同 AI 产出的那套 —— 指标必须在清单里、方向必须合法。"""
    keys = {m.key for m in METRICS}
    clean = []
    for it in items or []:
        if not isinstance(it, dict):
            raise ValueError("条件格式不对")
        metric = str(it.get("metric") or "").strip()
        if metric not in keys:
            raise ValueError(f"未知指标 {metric!r}，只能从 {sorted(keys)} 里选")
        direction = str(it.get("direction") or "").strip()
        if direction not in DIRECTIONS:
            raise ValueError(f"方向需为 {DIRECTIONS} 之一，得到 {direction!r}")
        clean.append({"metric": metric, "direction": direction,
                      "reason": str(it.get("reason") or "")[:200], "by": "user"})
    if len(clean) > 8:
        raise ValueError("最多 8 条，太多了反而没重点")
    os.makedirs(_USER_DIR, exist_ok=True)
    ok = atomic_write_json(_user_path(date),
                           {"schema": _USER_SCHEMA, "date": date, "items": clean})
    if not ok:
        raise RuntimeError("验证条件写入失败")
    return {"ok": True, "count": len(clean)}


def merged_items(date: str, ai_items: list[dict]) -> list[dict]:
    """AI 的条件 + 用户自设的条件。同一指标以**用户的**为准。

    用户明确写下的预期优先于 AI 的判断 —— 这是他的复盘，不是 AI 的。
    """
    user = load_user_items(date)
    by_metric = {it["metric"]: dict(it, by="ai") for it in (ai_items or [])}
    for it in user:
        by_metric[it["metric"]] = it          # 用户覆盖 AI
    return list(by_metric.values())

"""原始数据永久归档 —— 让"半年后还能重算"成为可能。

## 为什么这是地基

现在系统囤的是**派生结果**（`zt_summary` 三个数字、`prev_pool` 抽好的字段）。
这带来两个致命后果：

1. **算法一改，历史无法重算。** 比如修正了"炸板率"的定义，
   历史那些天的旧口径数字永远留在缓存里，新旧混在同一条曲线上。
2. **数据源口径变化看不出来。** 问财改了字段名、akshare 换了列、
   三池发布时间调整 —— 图上会冒出一个**假断点**，而你会把它当成市场变了。

> "源只留 15 天是严重问题。15 天能做短期监测，做不了十年体系的失效识别。"

所以：**每天把原始响应整份存下来，永不删除**，并给每条记录带上足够的元数据。
磁盘很便宜（一天几百 KB），丢掉的历史买不回来。

## 存什么

`~/.duanxian-agents/archive/<date>/<slug>.json`：

```
{
  "meta": {
    "source": "akshare.stock_zt_pool_em",   # 谁给的
    "fetched_at": "…CST",                   # 什么时候抓的（不是数据日期）
    "date": "2026-07-24",                   # 数据对应的交易日
    "fields": [...],                        # 字段清单 —— 源改字段时一眼看出来
    "row_count": 40,
    "archive_schema": 1
  },
  "rows": [...]                             # **原始行，不做任何加工**
}
```

⚠️ `rows` 必须是原样。一旦在这里做加工，归档就退化成另一份派生缓存，
失去"重算"的全部意义。

## 与其它缓存的关系

- `cache/*`：**派生结果**，为了跑得快，可以随时删掉重建（只要归档还在）
- `archive/*`：**原始事实**，永不删除，是重算的唯一依据
"""

from __future__ import annotations

from duanxian.paths import data_path
import json
import os
from typing import Any, Optional

from .util import atomic_write_json, china_now

_DIR = data_path("archive")

# 归档信封版本。⚠️ 只在**信封结构**变化时 +1；`rows` 的内容永远是源的原样，
# 不受算法版本影响 —— 这正是归档能用来重算的原因。
_ARCHIVE_SCHEMA = 1


def _day_dir(date: str) -> str:
    return os.path.join(_DIR, date)


def path_of(date: str, slug: str) -> str:
    return os.path.join(_day_dir(date), f"{slug}.json")


def has(date: str, slug: str) -> bool:
    return os.path.isfile(path_of(date, slug))


def put(date: str, slug: str, source: str, rows: list[dict],
        extra_meta: Optional[dict] = None, overwrite: bool = False,
        raw: bool = False) -> dict:
    """归档一份原始数据。

    默认**不覆盖**已有归档 —— 同一天的原始事实只该有一份。
    真要重抓（比如当天早些时候抓的是半天数据）才传 `overwrite=True`。
    """
    if not rows:
        return {"ok": False, "reason": "空数据不归档（避免把取数失败固化成'那天没有'）"}
    p = path_of(date, slug)
    if os.path.isfile(p) and not overwrite:
        return {"ok": True, "skipped": True, "reason": "已归档"}

    meta = {
        "source": source,
        "fetched_at": china_now().strftime("%Y-%m-%d %H:%M:%S") + " CST",
        "date": date,
        # 字段清单：数据源加列/改名/删列时，对比历史归档一眼就能看出来
        "fields": sorted({k for r in rows for k in r}),
        "row_count": len(rows),
        "archive_schema": _ARCHIVE_SCHEMA,
        # ⚠️ **是不是源的原样行**。false = 经过了我们的列映射（键集被固定）→
        # 那种归档**检不出字段漂移**（源改列只会让某个键变成 None），
        # 被丢掉的列也拿不回来。调用方必须显式声明，不能默认当成 raw
        # ⚠️ 归一化过的行会同时失去这两样：既检不出漂移，也拿不回被丢掉的列。
        "raw": bool(raw),
    }
    meta.update(extra_meta or {})
    os.makedirs(_day_dir(date), exist_ok=True)
    ok = atomic_write_json(p, {"meta": meta, "rows": rows})
    return {"ok": ok, "path": p, "rows": len(rows)}


def get(date: str, slug: str) -> Optional[dict]:
    """读回归档（含 meta）。不存在或坏了返回 None。"""
    p = path_of(date, slug)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            env = json.load(fh)
        if isinstance(env, dict) and "rows" in env:
            return env
    except Exception:  # noqa: BLE001
        pass
    return None


def days(slug: Optional[str] = None) -> list[str]:
    """已归档的交易日（升序）。给 slug 则只算有该份数据的日子。"""
    try:
        ds = sorted(d for d in os.listdir(_DIR) if not d.startswith("."))
    except FileNotFoundError:
        return []
    if slug is None:
        return ds
    return [d for d in ds if has(d, slug)]


def field_drift(slug: str, limit: int = 60) -> dict:
    """字段漂移检测：这份数据的字段清单在历史上变过吗。

    ⚠️ **数据源变化要当成"制度变化"看待**。问财口径或 akshare 字段一变，
    派生曲线上会出现一个断点，而它跟市场毫无关系 —— 不检测的话
    你会把它读成"市场结构变了"。
    """
    ds = days(slug)[-limit:]
    seen: list[dict] = []
    raw_days, mapped_days = 0, 0
    for d in ds:
        env = get(d, slug)
        if env is None:
            continue
        meta = env.get("meta", {})
        # ⚠️ **只有原样归档能检出漂移**。归一化过的行键集被我们的 mapping 固定住，
        # 源改了列它也纹丝不动 —— 把它算进来会得出"字段一直很稳定"的假结论。
        if not meta.get("raw"):
            mapped_days += 1
            continue
        raw_days += 1
        fields = meta.get("fields") or []
        if not seen or seen[-1]["fields"] != fields:
            seen.append({"since": d, "fields": fields})
    changed = len(seen) > 1
    if changed:
        note = "字段清单变过 —— 跨这些日期的统计要小心，断点可能来自数据源而非市场"
    elif raw_days >= 2:
        note = f"字段清单稳定（{raw_days} 天原样归档）"
    else:
        note = (f"还检不出漂移：{raw_days} 天原样归档"
                + (f"，另有 {mapped_days} 天是早期归一化归档（键集被固定，看不出源变化）"
                   if mapped_days else "")
                + " —— 至少要 2 天原样归档才能比较")
    return {
        "slug": slug,
        "days": len(ds),
        "raw_days": raw_days,
        "mapped_days": mapped_days,
        "versions": seen,
        "changed": changed,
        # 检不出漂移 ≠ 没有漂移，这两件事必须分得开
        "detectable": raw_days >= 2,
        "note": note + (f"；仅覆盖 {ds[0]} 至 {ds[-1]}，不能证明之后的源结构稳定" if ds else ""),
        "date_from": ds[0] if ds else None, "date_to": ds[-1] if ds else None,
    }


def summary() -> dict:
    """归档总览：囤了多久、多大、有哪些数据、字段有没有漂移过。"""
    ds = days()
    slugs: dict[str, int] = {}
    total_bytes = 0
    for d in ds:
        try:
            for f in os.listdir(_day_dir(d)):
                if not f.endswith(".json"):
                    continue
                slugs[f[:-5]] = slugs.get(f[:-5], 0) + 1
                total_bytes += os.path.getsize(os.path.join(_day_dir(d), f))
        except FileNotFoundError:
            continue
    return {
        "available": bool(ds),
        "days": len(ds),
        "date_from": ds[0] if ds else None,
        "date_to": ds[-1] if ds else None,
        "size_mb": round(total_bytes / 1024 / 1024, 2),
        "datasets": slugs,
        # 一天几百 KB，十年也就几百 MB —— 用这点磁盘换"永远能重算"，非常划算
        "drift": {s: field_drift(s) for s in slugs},
    }


# ---------------------------------------------------------------- 每日归档
_SLUG_SOURCES = {
    "zt_pool": "akshare.stock_zt_pool_em",
    "zb_pool": "akshare.stock_zt_pool_zbgc_em",
    "dt_pool": "akshare.stock_zt_pool_dtgc_em",
    "prev_pool": "akshare.stock_zt_pool_previous_em",
    "zt_reasons": "iwencai.query2data(今日涨停 涨停原因)",
}


def capture_day(date: Optional[str] = None) -> dict:
    """把某个交易日的原始数据整套归档。复盘跑完调用。

    优先复用三池、昨日池与题材缓存；缓存缺失或过期时会重取公开数据。
    实际抓取时点写入元数据，归档不回填为原报告证据。
    """
    from . import market_facts as mf
    from . import theme_tree as tt
    from . import trade_calendar
    from .backtest import _fetch_prev_pool

    date = date or trade_calendar.latest_session()
    if not date:
        return {"ok": False, "reason": "取不到最近已收盘交易日"}

    results: dict[str, Any] = {}
    pools = mf.pools(date)
    if pools:
        # ⚠️ 优先归档 `raw`（源的原样行）。`pools()` 现在顺带缓存它，所以这里
        # **不额外发请求**。老缓存（schema 2）没有 raw → 退回归一化行，
        # 但如实标 raw=false，让 field_drift 知道那些天检不出漂移。
        raw = pools.get("raw") or {}
        for slug, key in (("zt_pool", "zt"), ("zb_pool", "zb"), ("dt_pool", "dt")):
            rows, is_raw = raw.get(key), True
            if not rows:
                rows, is_raw = pools.get(key) or [], False
            results[slug] = put(date, slug, _SLUG_SOURCES[slug], rows, raw=is_raw)
    prev = _fetch_prev_pool(date)
    if prev:
        # prev_pool 走 backtest 的缓存，那份也是映射过的 → 如实标 raw=false
        results["prev_pool"] = put(date, "prev_pool", _SLUG_SOURCES["prev_pool"], prev,
                                   raw=False)
    reasons, err = tt.reasons_of(date)
    if reasons:
        # 题材串是 dict，转成行以便和其它数据同构
        rows = [{"code": c, "reason": r} for c, r in sorted(reasons.items())]
        results["zt_reasons"] = put(date, "zt_reasons", _SLUG_SOURCES["zt_reasons"], rows,
                                    extra_meta={"fetch_error": err}, raw=False)
    ok = any(r.get("ok") for r in results.values())
    return {"ok": ok, "date": date,
            "archived": {k: v.get("rows") or v.get("reason") for k, v in results.items()}}


def coverage_status(date_to: str | None, expected_date: str | None) -> dict:
    """Latest-day coverage is separate from continuity of the stored history."""
    stale = bool(expected_date and (not date_to or date_to < expected_date))
    return {"last_archived_session": date_to, "expected_session": expected_date,
            "stale": stale, "coverage_note":
            (f"归档仅到 {date_to or '尚无记录'}，落后于最近已收盘场次 {expected_date}；不能据此判断当前字段或市场结构稳定" if stale else
             "当前归档截止日已覆盖最近已收盘场次；不代表中间每个交易日均有记录" if expected_date else
             "最近已收盘场次未核实；归档日期仅表示已有记录范围")}

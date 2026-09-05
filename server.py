"""短线复盘 Web 后端 —— FastAPI 包住复盘图 + 缓存 + 静态前端"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from duanxian import live_emotion, overseas, preflight, reflection, review_store, trade_calendar
from duanxian.review_store import md_to_html as _md_to_html, strip_prefix as _strip_prefix
from duanxian.config import make_llm
from duanxian.review_graph import build_review_graph
from duanxian.deepdive.graph import run as deepdive_run
from duanxian.roles import ROLES
from main import initial_state
from duanxian.util import (
    china_now, china_today, is_a_share_closed, is_degraded_report, is_weekend,
    safe_join, strip_model_noise, validate_trade_date,
)
from duanxian.prompts import PACK

SCHEMA_VERSION = 1
_HERE = os.path.dirname(os.path.abspath(__file__))
# React 构建产物：存在就优先服务它，单端口即可访问全部界面，不必另起 vite dev。
# 构建：cd frontend && npm run build
_DIST = os.path.join(_HERE, "frontend", "dist")
_REVIEW_DIR = os.path.expanduser("~/.duanxian-agents/reviews")
_DD_DIR = os.path.expanduser("~/.duanxian-agents/deepdive")
_WK_DIR = os.path.expanduser("~/.duanxian-agents/weekly")
os.makedirs(_REVIEW_DIR, exist_ok=True)
os.makedirs(_WK_DIR, exist_ok=True)
# 允许写操作（POST/DELETE）的 Host。默认只认本机；挂到域名下访问时，用
# `VIBE_ALLOW_HOSTS="myhost,www.myhost"`（逗号分隔）把域名加进来，否则写操作会 403。
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | {
    h.strip() for h in os.environ.get("VIBE_ALLOW_HOSTS", "").split(",") if h.strip()
}

app = FastAPI(title="短线每日复盘")

# ---------------------------------------------------------------- 并入 VR 后端
# 盘面数据 / 首板分析 / 盯盘 / 持仓股 / 自选股 / 个股数据 / 资讯雷达 这几个分栏的
# 后端放在 `vr/`，它的路由在这里并到本 app 上，一个进程一个端口即可访问全部界面。
# `vr/` 原样来自开源的 Vibe-Research，日后拉更新就是一次纯拷贝。改成包内相对
# import 会让每次同步都变成手工 merge。


def _alert(msg: str) -> None:
    """必达的告警输出"""
    import sys

    print(msg, file=sys.stderr, flush=True)


_VR_PATH_RES: list[re.Pattern] = []


def _merge_vr_routes() -> int:
    """把 `vr/` 的路由并进本 app。返回并入条数；失败不影响本仓库自有功能。"""
    import sys

    vr_dir = os.path.join(_HERE, "vr")
    if not os.path.isdir(vr_dir):
        return 0
    if vr_dir not in sys.path:
        sys.path.insert(0, vr_dir)   # 放最前，确保 `import astock` 等解析到 vr/ 内
    try:
        import app as vr_app  # noqa: PLC0415  vr/app.py（不是本仓库的模块）

        before = len(app.router.routes)
        # 只拿 /api/* 的；VR 没有非 /api 路由，这层过滤是为了防它日后加了根路由
        # 把我们的 SPA fallback 顶掉
        for r in vr_app.app.router.routes:
            path = getattr(r, "path", "")
            if not path.startswith("/api/"):
                continue
            app.router.routes.append(r)
            # 记下路径模板 → 正则（`{rid}` 这类参数换成"一段非斜杠"），
            # 供下面那道补偿闸判断"这个请求是不是打在 VR 路由上"
            _VR_PATH_RES.append(re.compile(
                "^" + re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(path)
                             .replace(r"\{", "{").replace(r"\}", "}")) + "$"))
        return len(app.router.routes) - before
    except Exception as exc:  # noqa: BLE001  VR 挂了不该让复盘/交易日志也用不了
        _alert(f"⚠️ VR 后端并入失败（那 7 个分栏会不可用）：{type(exc).__name__}: {exc}")
        return 0


def _guard_vr_userdata() -> None:
    """启动时给 VR 的**不可再生用户数据**留一份备份"""
    import shutil

    vr_home = os.path.expanduser("~/.vibe-research")
    pf = os.path.join(vr_home, "portfolio.json")
    if not os.path.isfile(pf):
        return
    try:
        with open(pf, encoding="utf-8") as fh:
            json.load(fh)
    except Exception as exc:  # noqa: BLE001
        # 损坏：把原始字节另存（带时间戳，不覆盖之前的），并大声说出来
        stamp = china_now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(vr_home, f"portfolio.corrupt-{stamp}.json")
        try:
            shutil.copy2(pf, dst)
        except Exception:  # noqa: BLE001
            dst = "（另存也失败了）"
        _alert(f"🔴 VR 持仓文件无法解析（{type(exc).__name__}）！"
               "vr/portfolio.py 会把它当成空持仓，并在 30 分钟内写回覆盖掉。")
        _alert(f"   原始字节已另存：{dst}")
        _alert(f"   历史可解析备份：{vr_home}/portfolio.good-*.json（挑最近的非空那份恢复）")
        return
    try:
        with open(pf, encoding="utf-8") as fh:
            cur = json.load(fh)
        holdings = (cur or {}).get("holdings") or []
        stamp = china_now().strftime("%Y%m%d")
        dst = os.path.join(vr_home, f"portfolio.good-{stamp}.json")
        if not holdings:
            # 空持仓 + 已经存在任何非空备份 → 这大概率是"损坏后被写成空"的产物，别覆盖
            import glob

            for old_bak in glob.glob(os.path.join(vr_home, "portfolio.good-*.json")):
                try:
                    with open(old_bak, encoding="utf-8") as fh:
                        if (json.load(fh) or {}).get("holdings"):
                            return   # 有非空的历史备份在，什么都不做
                except Exception:  # noqa: BLE001
                    continue
        shutil.copy2(pf, dst)
    except Exception as exc:  # noqa: BLE001  备份失败要出声，但不阻断启动
        _alert(f"⚠️ VR 持仓备份失败（{type(exc).__name__}）：{vr_home}/portfolio.good-*.json")


_SAFE_CLI_KINDS = frozenset({"claude"})


def _opted_in_clis() -> frozenset[str]:
    """`VIBE_ALLOW_UNSAFE_CLI=qwen,deepseek` —— 运行服务的人**显式**放开的"""
    raw = os.environ.get("VIBE_ALLOW_UNSAFE_CLI", "")
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


_ALLOWED_CLI_KINDS = _SAFE_CLI_KINDS | _opted_in_clis()

# 已知并有意禁掉的。只用于区分「预期内」和「上游新来的」，**不参与放行判断**
# —— 放行只看白名单，这个集合少写了什么也不会让谁被放进来。
_KNOWN_UNSAFE_CLIS = frozenset({"qwen", "deepseek", "codex", "opencode", "cursor", "kimi"})

# 摘除前的完整 CLI 定义快照 —— 给 `/api/cli/available` 报"这个 kind 装没装"用。
# 摘掉之后 `detect_cli()` 一律返回 None，分不清"没装"和"被禁"，而这两件事
# 对用户是完全不同的信息（一个去装、一个别想了）。
_ALL_CLI_BINS: dict[str, list[str]] = {}

from duanxian.cli_llm import _BINS_ATTR_NAME as _BINS_ATTR  # 单一来源，见那边注释


def _cli_runtime_modules() -> list:
    """所有**已加载的** cli_runtime 模块对象"""
    mods = [m for name, m in list(sys.modules.items())
            if m is not None and (name == "cli_runtime" or name.endswith(".cli_runtime"))
            and hasattr(m, "_CLI_DEFS")]
    if mods:
        return mods
    try:
        import cli_runtime as _cr  # noqa: PLC0415
    except Exception:              # noqa: BLE001  这一版可能根本没带这个文件
        return []
    return [_cr] if hasattr(_cr, "_CLI_DEFS") else []


def _disable_unsafe_clis() -> list[str]:
    """把不在白名单里的 CLI 从**每一份** CLI 运行时字典摘掉，返回实际摘掉的。"""
    mods = _cli_runtime_modules()
    if not mods:
        _alert("🔴 找不到 cli_runtime 模块 ——「接入AI」的自动批准 CLI 可能仍可用，请检查")
        return []

    for mod in mods:
        if not hasattr(mod, _BINS_ATTR):
            setattr(mod, _BINS_ATTR,
                    {k: list(d.get("bins") or []) for k, d in mod._CLI_DEFS.items()})
        _ALL_CLI_BINS.update(getattr(mod, _BINS_ATTR))

    removed: set[str] = set()
    for mod in mods:
        for k in [k for k in list(mod._CLI_DEFS) if k not in _ALLOWED_CLI_KINDS]:
            mod._CLI_DEFS.pop(k, None)
            removed.add(k)

    live = sys.modules.get("app")
    if live is None or not hasattr(live, "cli_runtime"):
        _alert("⚠️ 无法校验 CLI 禁用是否生效：没找到已加载的 VR app 模块")
    else:
        leftover = sorted(set(live.cli_runtime._CLI_DEFS) - _ALLOWED_CLI_KINDS)
        if leftover:
            _alert(f"🔴 禁用没生效！/api/chat 实际用的运行时里仍有 {leftover} —— 存在第三份 cli_runtime？")

    # 上游新增的（不在我们已知清单里的）要报警 —— 白名单挡住了它，但人得知道
    unknown = sorted(removed - _KNOWN_UNSAFE_CLIS)
    if unknown:
        _alert(f"⚠️ vr/ 上游新增了 CLI {unknown}，已按白名单摘掉。确认安全后再加进 _ALLOWED_CLI_KINDS")
    return sorted(removed)


def _add_vr_to_path() -> None:
    """只把 `vr/` 放进 sys.path，不挂它的路由"""
    import sys
    vr_dir = os.path.join(_HERE, "vr")
    if os.path.isdir(vr_dir) and vr_dir not in sys.path:
        sys.path.insert(0, vr_dir)


_VR_ROUTES = _merge_vr_routes()
def _pin_pool_to_settled_session() -> int:
    """把「涨停池」的可见范围钉在**已经收盘**的交易日上。返回 1 = 已生效。

    `vr/` 的首板分析、短线情绪都是"从今天往前回溯，第一天有池子就用它"。
    东财在**盘中**就发布当日池子，于是 09:35 打开看到的是「今天才 18 家涨停」
    这种半成品，而复盘看板是上一场（07-29 的 81 家）—— 同一个产品两个日期，
    而且每刷一次数字都变，最容易让人整块不信。

    复盘系统不做当日动态分析。所以这里不去改 `vr/`（要保持能同步上游），
    而是给它的取数口包一层：**未收盘的交易日，涨停池视为"还没有"**，
    它的回溯逻辑就自然落到上一场，一行都不用动它。

    影响面只在 `vr/` 那几个走涨停池的块；复盘链路走的是
    `duanxian/fetchers.py`（akshare），不受这层影响。
    """
    live = sys.modules.get("astock")
    if live is None or not hasattr(live, "em_zt_topic_pool"):
        _alert("⚠️ 没能钉住涨停池日期：找不到已加载的 vr astock —— "
               "首板/短线情绪可能显示盘中半成品")
        return 0
    if getattr(live, "_pool_pinned", False):
        return 1                                   # 幂等：重复调用无害

    orig = live.em_zt_topic_pool

    def guarded(kind, date, sort, *a, **kw):
        ymd = str(date)
        if len(ymd) == 8 and ymd.isdigit():
            iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            if not trade_calendar.is_settled(iso):
                return []                          # 还没收盘 → 当作还没有池子
        return orig(kind, date, sort, *a, **kw)

    live.em_zt_topic_pool = guarded
    live._pool_pinned = True
    # 留一个未加锁的原函数出口：盘面数据页要显示**今日实时**的打板情绪，
    # 而这道锁是给复盘类块用的。锁是钝器，需要今天数据的地方走这个口子。
    live._pool_unpinned = orig
    return 1


_POOL_PINNED = _pin_pool_to_settled_session()
_guard_vr_userdata()
_DISABLED_CLIS = _disable_unsafe_clis()

_VR_API_KEY = os.environ.get("VR_API_KEY", "").strip()

# 需要 Origin 校验的方法。我们自有的写操作都在 handler 里手工调 `_origin_ok`，
# 但 VR 的 handler **我们不改**（要保持上游原样）→ 只能在 middleware 层补。
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _is_vr_path(path: str) -> bool:
    return any(rx.match(path) for rx in _VR_PATH_RES)


@app.middleware("http")
async def _vr_guard(request: Request, call_next):
    """给并进来的 VR 路由补两道闸"""
    if _is_vr_path(request.url.path):
        if (_VR_API_KEY and request.method != "OPTIONS"
                and request.url.path != "/api/health"
                and request.headers.get("authorization", "") != f"Bearer {_VR_API_KEY}"):
            return JSONResponse({"error": "未授权：缺少或错误的 VR_API_KEY"}, status_code=401)
        if request.method in _MUTATING and not _origin_ok(request):
            return JSONResponse({"error": "非法来源"}, status_code=403)
    return await call_next(request)

_lock = threading.Lock()
_job = {
    "running": False, "job_id": None, "date": None, "error": None,
    "started": None, "elapsed": 0, "finished_at": None,
}
_dd_job = {
    "running": False, "job_id": None, "stock": None, "error": None,
    "started": None, "elapsed": 0, "finished_at": None,
}

_JOB_TIMEOUT = 15 * 60
_DD_TIMEOUT = 10 * 60


def _job_stuck(job: dict, limit: int) -> bool:
    """任务是否已超时卡死 —— 卡死的任务不该再挡住新任务。"""
    return bool(job.get("running") and job.get("started")
                and time.time() - job["started"] > limit)


def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)  # 缓存目录首次写时可能尚不存在
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # 原子替换，读方永远看到完整文件


def _capture_theme_reasons() -> None:
    """囤当天的题材串（1 次问财请求）。 只能查最近交易日，过期不候"""
    try:
        from duanxian import theme_tree

        r = theme_tree.capture()
        if not r.get("ok"):
            print(f"⚠️ 题材串囤积失败（{r.get('date')}）：{r.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 题材串囤积异常：{type(exc).__name__}: {exc}")


def _capture_archive(date: str) -> None:
    """把**被复盘那一天**的原始数据整套永久归档 —— 日后还能按新口径重算的唯一依据。

    ⚠️ 必须把 `date` 传下去：补跑历史某天的复盘时不传参，归档的会是"最近交易日"，
       而目标历史日照样缺失 —— 归档看着照常成功，缺口却一直在。
    """
    try:
        from duanxian import archive

        r = archive.capture_day(date)
        if not r.get("ok"):
            print(f"⚠️ 原始数据归档失败（{r.get('date')}）：{r.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 原始数据归档异常：{type(exc).__name__}: {exc}")


def _capture_backtest_corpus(date: str) -> None:
    """顺手把**被复盘那一天**的回测语料囤下来（1 次请求）。

    ⚠️ 数据源只留最近约 15 个交易日，**过期不候** —— 没在窗口期内抓下来的日子
    就永久缺失。挂在复盘完成之后，语料随日子推移自己长长，回测窗口就不再受
    数据源留存限制。失败只记日志，绝不影响复盘本身。
    """
    try:
        from duanxian.backtest import capture

        r = capture(date)
        if not r.get("ok"):
            print(f"⚠️ 回测语料捕获失败（{r.get('date')}）：{r.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 回测语料捕获异常：{type(exc).__name__}: {exc}")


def _run_review(date: str, job_id: str) -> None:
    try:
        # 先体检输入 —— 核心数据取不到就别跑。喂空数据进去，模型会硬凑出
        # 看着可信的结论（实测点到一只当天 -6.81% 的票当"主线代表"）。
        pre = preflight.check(date)
        if not pre["ok"]:
            raise RuntimeError(preflight.refuse_reason(pre, date))

        graph = build_review_graph()
        final = graph.invoke(initial_state(date), {"recursion_limit": 50})
        reflection.auto_evaluate_prior(date)  # 先回评上期预测（其次日=今天，数据已出）
        # 体检发现的非核心缺失要如实落进 warnings，界面才会显示「⚠ 部分数据降级」
        payload = review_store.serialize(final, date, pre["warnings"])
        res = review_store.save(payload, date)
        if not res.written:
            # 产物不可用 → 必须让用户看见，不能"任务成功但内容是空的"
            raise RuntimeError(res.reason)
        _capture_theme_reasons()     # 题材串同理（问财只给最近交易日）
        _capture_archive(date)           # 原始数据永久归档（零额外请求，走已有缓存）
        _capture_backtest_corpus(date)   # 复盘写完再囤语料，失败也不影响已产出的复盘
    except Exception as exc:  # noqa: BLE001
        with _lock:
            if _job["job_id"] == job_id:
                _job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with _lock:
            if _job["job_id"] == job_id:  # 只结束属于自己的任务（#5）
                _job["running"] = False
                if _job["started"]:
                    _job["elapsed"] = int(time.time() - _job["started"])  # 收尾定格 elapsed（#17）
                _job["finished_at"] = china_now().strftime("%Y-%m-%d %H:%M:%S") + " CST"


def _origin_ok(request: Request) -> bool:
    """只允许本机来源触发昂贵任务，挡掉恶意网页的跨站 POST（#22）。"""
    ref = request.headers.get("origin") or request.headers.get("referer") or ""
    if not ref:
        return True  # 无 Origin（如 curl 本地调用）放行
    return (urlparse(ref).hostname or "") in _ALLOWED_HOSTS


def _force_flag(request: Request) -> bool:
    """`?force=1` —— 已复盘过还要重跑。只认 POST 上的查询参数（本路由本身是 POST）。"""
    return str(request.query_params.get("force", "")).strip().lower() in ("1", "true", "yes")


@app.post("/api/review/run")
def api_run(request: Request, date: str | None = None):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    try:
        explicit = bool(date)
        if not explicit:
            # 复盘的对象只能是**已经收盘的那一场**。原来这里在工作日直接用 today，
            # 于是盘前点一下就为「还没开盘的今天」开跑：涨停池 / 龙虎榜 / 资金流全空，
            # 四个分析师如实说"数据缺失"，裁判却照样端出三个方向 + 点名个股
            # （实测点到一只当天 -6.81% 的票当"主线代表"）。
            # 复盘系统不做当日动态分析 —— 没收盘就还是上一场那份。
            date = trade_calendar.latest_session() or china_today()
        date = validate_trade_date(date)  # #1
        if is_weekend(date):
            return JSONResponse({"error": f"{date} 为周末非交易日"}, status_code=400)
        if not trade_calendar.is_settled(date):
            latest = trade_calendar.latest_session()
            return JSONResponse(
                {"error": f"{date} 还没收盘，复盘要用当天的收盘数据",
                 "suggest_date": latest,
                 "hint": f"最近已收盘的是 {latest}" if latest else None},
                status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # 那一场已经复盘过就别重跑 —— 直接告诉前端去看那天的页面。
    # 想重跑：带 force=1（改了口径/修了 bug 时才需要）。
    if not _force_flag(request) and review_store.usable(review_store.load(date)):
        return {"running": False, "date": date, "already_done": True,
                "message": f"{date} 已复盘"}

    with _lock:  # 原子 check-then-act（#5）
        if _job["running"] and not _job_stuck(_job, _JOB_TIMEOUT):
            return {"running": True, "date": _job["date"]}
        if _job["running"]:   # 卡死的旧任务：让位，并把原因如实写进 error
            _job["error"] = f"上一个任务（{_job.get('date')}）超过 {_JOB_TIMEOUT // 60} 分钟无响应，已判为卡死"
        job_id = uuid.uuid4().hex
        _job.update(running=True, job_id=job_id, date=date, error=None,
                    started=time.time(), elapsed=0, finished_at=None)
    threading.Thread(target=_run_review, args=(date, job_id), daemon=True).start()
    return {"running": True, "date": date, "job_id": job_id}


@app.get("/api/cli/available")
def api_cli_available():
    """哪些订阅 CLI 真的能用 —— **服务端是唯一权威**，UI 照这个渲染"""
    live = sys.modules.get("app")
    find_bin = getattr(getattr(live, "cli_runtime", None), "_find_bin", None)

    out = []
    for kind, bins in sorted(_ALL_CLI_BINS.items()):
        allowed = kind in _ALLOWED_CLI_KINDS
        # 被摘掉的 kind 用不了 `detect_cli`（它读的就是被摘空的那个 dict），
        # 所以直接拿摘除前记下的可执行名去找。
        installed = bool(find_bin and any(find_bin(b) for b in bins))
        out.append({
            "kind": kind,
            "allowed": allowed,
            "installed": installed,
            "reason": None if allowed else "自动批准 / 无沙箱，默认禁用",
        })
    return {
        "clis": out,
        # 放开的办法，直接告诉调用方，不用去翻文档
        "optInEnv": "VIBE_ALLOW_UNSAFE_CLI",
        "optedIn": sorted(_opted_in_clis()),
    }


@app.get("/api/review/status")
def api_status():
    with _lock:
        snap = dict(_job)
    if snap["running"] and snap["started"]:
        snap["elapsed"] = int(time.time() - snap["started"])
    snap.pop("started", None)
    return snap


@app.post("/api/review/evaluate")
def api_evaluate(request: Request, date: str):
    """手动评估某天的『明天关注点』命中情况（次一交易日实际涨跌）"""
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    try:
        date = validate_trade_date(date)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    res = reflection.evaluate(date)
    return res or {"error": "无可评估数据（缺该日预测，或次一交易日数据尚未出）"}


@app.get("/api/market/session")
def api_market_session():
    """此刻的「实时行情」到底属于哪一场。

    腾讯 / 东财的实时接口在盘前返回的是**上一场的收盘**，而且不带任何提示。
    界面若按「今天」给它们贴日期，盘前打开就会看到「今天 上证 +0.4%」——
    今天还没开盘，这个数是昨收。数字没错，标签错了，而这种错最容易让人
    对整块数据失去信任。所以把「哪一场」当成一等公民返回，由 UI 如实标注。
    """
    today = china_today()
    quotes_of = trade_calendar.quote_trade_day()
    is_today = bool(quotes_of) and quotes_of == today
    closed = is_a_share_closed()

    now = china_now()
    hhmm = now.hour * 60 + now.minute
    if not quotes_of:
        phase, label = "未知", "行情时间取不到"
    elif is_today and not closed and hhmm < 9 * 60 + 25:
        # 09:15-09:25 集合竞价：还没成交，指数等于昨收、涨跌幅是 0。
        # 不单独成一档的话，界面标"盘中·实时"而三个指数全是 0%，看着像数据坏了。
        phase, label = "集合竞价", "集合竞价 · 尚未成交"
    elif is_today and not closed:
        phase, label = "盘中", "盘中 · 实时"
    elif is_today:
        phase, label = "已收盘", f"{today} 收盘"
    elif is_weekend(today):
        phase, label = "非交易日", f"非交易日 · 显示 {quotes_of} 收盘"
    elif not closed:
        # 工作日、还没到收盘，而行情停在上一场 → 盘前（或今天是节假日）
        phase, label = "盘前", f"盘前 · 显示 {quotes_of} 收盘"
    else:
        phase, label = "非交易日", f"今日无成交 · 显示 {quotes_of} 收盘"

    return {"now": now.strftime("%Y-%m-%d %H:%M"), "today": today,
            "quotes_of": quotes_of, "is_today": is_today,
            "phase": phase, "label": label}


@app.get("/api/market/live-emotion")
def api_market_live_emotion():
    """今日**实时**打板情绪（盘面数据页用）。

    与 `vr/` 的 `/api/market/emotion` 并存：那条被锁在已收盘那一场（复盘口径），
    这条要的就是今天、随盘变化。两个块在界面上分别标清是哪一场。
    """
    return live_emotion.snapshot()


@app.get("/api/market/overseas")
def api_market_overseas():
    """隔夜外围：美港股指数 + 美股七姐妹，各自带交易日。

    与 `vr/` 的 `/api/global/indices` 并存 —— 那条不回行情时间，界面无法说清
    「这批数是哪一场的」（详见 duanxian/overseas.py 顶部）。
    """
    return overseas.overseas_snapshot()


@app.get("/api/review/dates")
def api_review_dates():
    """跑过复盘的交易日，新→旧。给看板的历史入口用。

    在此之前 `reviews/` 每天存一份、但只有 `latest.json` 有接口 ——
    对一个复盘产品来说「翻回上周三看看」是刚需，却没有入口。
    """
    return {"dates": review_store.dates()}


@app.get("/api/review/latest")
def api_latest(date: Optional[str] = None):
    """读复盘：不带 date 读最近一份；带 date 读那天的历史存档。"""
    if date:
        try:
            date = validate_trade_date(date)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    payload = review_store.load(date)   # 损坏/不存在都返回 None（不 500）
    if payload is None or not review_store.usable(payload):
        # 空对象 = 「这天没有」，前端据此显示"还没跑过"；带上 date 好让前端知道问的是哪天
        return JSONResponse({"requested_date": date} if date else {}, status_code=200)
    try:
        if date is None:
            payload["reflection"] = reflection.latest_reflection()
        payload["scoreboard"] = reflection.scoreboard()
    except Exception as exc:  # noqa: BLE001  战绩算不出来不该让整个复盘打不开
        print(f"⚠️ 战绩统计失败：{type(exc).__name__}: {exc}")
    return JSONResponse(payload)


# ==================== ③ 近 5 天热度 ====================
_wk_lock = threading.Lock()


def _wk_load():
    path = os.path.join(_WK_DIR, "latest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return None


def _wk_fresh(cached: dict) -> bool:
    """缓存是否仍是最新已收盘交易日窗口（ #3）。判不了(网络失败)→按新鲜处理不硬刷"""
    from duanxian.weekly import LINEAGE_SCHEMA

    days = cached.get("days") or []
    if not days:
        return False
    if cached.get("lineage_schema") != LINEAGE_SCHEMA:
        return False
    try:
        from duanxian.weekly import _last_trade_dates

        latest = _last_trade_dates(1)
        expected = latest[-1] if latest else None
    except Exception:  # noqa: BLE001
        expected = None
    return expected is None or days[-1].get("date") == expected


def _wk_good(w: dict) -> bool:
    """结果够格替换有效缓存吗（ #4：至少有一天真实取到数）"""
    return not w.get("error") and any((d.get("limit_up") is not None) for d in (w.get("days") or []))


@app.get("/api/weekly")
def api_weekly(request: Request, refresh: int = 0):
    """近 5 交易日热度 + 龙头谱系。缓存按交易日过期；单飞锁防并发重复计算（~40s）"""
    return _weekly(force=False)


@app.post("/api/weekly/refresh")
def api_weekly_refresh(request: Request):
    """强制重算近 5 天热度。写操作走 POST + Origin 校验（见 api_weekly 的说明）。"""
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    return _weekly(force=True)


def _weekly(force: bool):
    """近 5 天热度的真正实现。`force` **只能由 POST 处理器传入**，不接受查询参数。"""
    from duanxian.weekly import build_weekly

    cached = _wk_load()
    if not force and cached and _wk_fresh(cached):
        return JSONResponse(cached)

    if not _wk_lock.acquire(blocking=False):
        if cached:
            return JSONResponse({**cached, "busy": True})
        return JSONResponse({"error": "正在计算中，请稍后重试", "busy": True}, status_code=409)
    try:
        cached2 = _wk_load()
        if not force and cached2 and _wk_fresh(cached2):
            return JSONResponse(cached2)
        w = build_weekly(5)
        w["generated_at"] = china_now().strftime("%Y-%m-%d %H:%M")
        days = w.get("days") or []
        w["last_trade_date"] = days[-1].get("date") if days else None
        if _wk_good(w):
            try:
                _atomic_write(safe_join(_WK_DIR, "latest.json"), w)
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse(w)
        if cached2 and (cached2.get("days")):
            stale = dict(cached2)
            stale["stale"] = True
            stale["warnings"] = (stale.get("warnings") or []) + ["刷新失败，展示上一份有效数据"]
            return JSONResponse(stale)
        return JSONResponse(w)
    finally:
        _wk_lock.release()


_chat_llm = None
_ROLE_MAP = {"user": "human", "assistant": "ai"}

# 对话体量上限。本机其它进程也能打这个口，不设限就能构造超长请求把 LLM 额度烧光。
_CHAT_MAX_MESSAGES = 40       # 超出部分只保留最近的（_chat 内还会再截到 12 轮）
_CHAT_MAX_CHARS_EACH = 4000
_CHAT_MAX_CHARS_TOTAL = 24000


def _sanitize_messages(msgs: object) -> tuple[list, Optional[str]]:
    """校验并裁剪对话消息。返回 (清洗后的消息, 错误信息)。"""
    if not isinstance(msgs, list) or not msgs:
        return [], "空消息"
    if len(msgs) > _CHAT_MAX_MESSAGES:
        msgs = msgs[-_CHAT_MAX_MESSAGES:]
    out, total = [], 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        if role not in _ROLE_MAP:
            return [], f"不支持的 role：{role!r}（只接受 user / assistant）"
        content = str(m.get("content", ""))[:_CHAT_MAX_CHARS_EACH]
        total += len(content)
        if total > _CHAT_MAX_CHARS_TOTAL:
            return [], "对话内容过长，请开新会话"
        out.append({"role": role, "content": content})
    return (out, None) if out else ([], "空消息")


def _chat_model():
    global _chat_llm
    if _chat_llm is None:
        _chat_llm = make_llm(deep=False)
    return _chat_llm


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _load_latest_json(dirpath: str) -> dict:
    path = os.path.join(dirpath, "latest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def _review_context() -> str:
    d = _load_latest_json(_REVIEW_DIR)
    if not d:
        return "（暂无复盘数据，请先在复盘 Agent 生成一次复盘。）"
    parts = [f"复盘交易日 {d.get('target_date', '')}", f"【明天关注点】\n{d.get('focus_md', '')}"]
    if d.get("macro_sector"):
        parts.append(f"【大板块本周】\n{d['macro_sector']}")
    for a in d.get("analysts", []):
        parts.append(f"【{a.get('title', '')}】\n{_strip_html(a.get('html', ''))}")
    return "\n\n".join(parts)[:8000]
def _chat(context: str, role_desc: str, messages: list) -> dict:
    system = (
        f"你是{role_desc}。下面是刚才多 agent 产出的结论与数据，用户会就它追问或让你展开。\n"
        f"{PACK.chat_guidance}\n"
        "不要提及你自己的身份或模型名。\n\n" + context
    )
    try:
        chain = [("system", system)] + [
            (_ROLE_MAP.get(m["role"], "human"), m["content"])
            for m in messages[-12:]
        ]
        ans = _chat_model().invoke(chain).content
        return {"answer": strip_model_noise(ans)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.post("/api/review/chat")
def api_review_chat(request: Request, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    msgs, err = _sanitize_messages(body.get("messages"))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return _chat(_review_context(), "A 股短线复盘助手", msgs)


def index():
    dist_index = os.path.join(_DIST, "index.html")
    if os.path.isfile(dist_index):
        return FileResponse(dist_index)
    return JSONResponse(
        {"error": "未找到前端构建产物，请先执行：cd frontend && npm install && npm run build"},
        status_code=503,
    )


@app.get("/api/verification/menu")
def api_verify_menu():
    """可选指标清单（前端下拉用）。"""
    from duanxian.verification import DIRECTIONS, METRICS

    return JSONResponse({
        "directions": DIRECTIONS,
        "metrics": [{"key": m.key, "label": m.label, "hint": m.hint,
                     "unit": m.unit, "higher_is_hotter": m.higher_is_hotter}
                    for m in METRICS],
    })


@app.get("/api/verification/items")
def api_verify_items(date: str):
    from duanxian import verification

    try:
        date = validate_trade_date(date)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"date": date, "items": verification.load_user_items(date)})


@app.post("/api/verification/items")
def api_verify_save(request: Request, date: str, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import verification

    try:
        date = validate_trade_date(date)
        return JSONResponse(verification.save_user_items(date, body.get("items") or []))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ==================== ④ 交易日志与模式卡 ====================
# ⛔ 本段的数据全部是使用者自己录入的个人交易记录，**不接入任何 AI prompt**。


@app.get("/api/journal/list")
def api_journal_list(limit: int = 200):
    from duanxian import journal

    try:
        return JSONResponse(journal.list_trades(max(1, min(limit, 1000))))
    except journal.JournalCorrupted as exc:
        # ⚠️ 账本损坏必须报 500 并说明原因，绝不返回空表 —— 空表会被读成
        #    "记录丢了"，而下一次写入就真的把它覆盖掉了。
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)


@app.get("/api/journal/stats")
def api_journal_stats():
    from duanxian import journal

    try:
        return JSONResponse(journal.stats())
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)


@app.post("/api/journal/add")
def api_journal_add(request: Request, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import journal

    try:
        pnl = body.get("pnl_pct")
        return JSONResponse(journal.add_trade(
            date=str(body.get("date") or "").strip(),
            code=str(body.get("code") or "").strip(),
            name=str(body.get("name") or "").strip(),
            playbook=str(body.get("playbook") or "其它").strip(),
            pnl_pct=None if pnl in (None, "") else float(pnl),
            as_planned=body.get("as_planned"),
            note=str(body.get("note") or ""),
            # ⚠️ 每个字段都必须透传。漏掉 fills 会让界面上填的成交明细被静默丢弃，
            #    且前端不报错 —— 而加权成本、已实现盈亏、持有天数全靠它算。
            fills=body.get("fills") or [],
            planned_stop=body.get("planned_stop"),
            planned_target=body.get("planned_target"),
        ))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)
    except RuntimeError as exc:      # 写盘失败 —— 必须让用户知道这一笔没记上
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/journal/update")
def api_journal_update(request: Request, trade_id: str, body: dict = Body(...)):
    """更新一笔已有交易 —— 主要用来给持仓中的记录**补上后来的成交**。

    只处理请求里出现的字段，其余保持不变。没有这个接口时，补一笔卖出只能
    删掉重录，`created_at` 与"下单时写下的计划边界"这个证据就一起没了。
    """
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import journal

    kw = {}
    if "fills" in body:
        kw["fills"] = body.get("fills") or []
    if "note" in body:
        kw["note"] = str(body.get("note") or "")
    for k in ("as_planned", "planned_stop", "planned_target"):
        if k in body:                      # ⚠️ 用 in 判断：没传 = 不动，传了 null = 清空
            kw[k] = body[k]
    try:
        r = journal.update_trade(trade_id, **kw)
        if not r.get("ok"):
            return JSONResponse({"error": r.get("reason") or "更新失败"}, status_code=404)
        return JSONResponse(r)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/positions")
def api_positions():
    """当前持仓 —— 从交易日志的成交明细聚合，**不另存一份账**。

    ⚠️ 与 `vr` 的 `/api/portfolio` 是两套不同的账：那一套自己存 holdings，
    需要重复录入且和日志对不上。新界面一律走这个口，`/api/portfolio` 保留
    只为兼容旧数据（`vr/` 是上游逐字副本，不在这里改它）。
    """
    from duanxian import journal, positions

    try:
        return JSONResponse(positions.report())
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"available": False,
                             "reason": f"{type(exc).__name__}: {exc}"}, status_code=200)


@app.post("/api/positions/import-legacy")
def api_positions_import(request: Request):
    """把旧的 `vr` 持仓（`~/.vibe-research/portfolio.json`）一次性导入交易日志。

    ⚠️ 只导**当前持仓**，每条建成一笔"只有买入、尚未卖出"的交易。
    旧记录里没有成交日期，用 `date` 字段或今天，并在备注里注明是导入的 ——
    不伪造一个看起来精确的建仓日。已导过的（同代码同股数同成本）跳过，可重复调用。
    """
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import journal, positions

    try:
        import portfolio as vr_pf  # noqa: PLC0415  vr/ 已在 sys.path

        legacy = (vr_pf.load() or {}).get("holdings") or []
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"读不到旧持仓：{type(exc).__name__}: {exc}"},
                            status_code=500)
    if not legacy:
        return JSONResponse({"ok": True, "imported": 0, "skipped": 0,
                             "message": "旧持仓是空的，没有需要导入的内容"})

    existing = {(p["code"], p["shares"], p["cost"]) for p in positions.open_positions()}
    imported = skipped = 0
    errors = []
    for h in legacy:
        code = str(h.get("code") or "").zfill(6)
        shares, cost = float(h.get("shares") or 0), float(h.get("cost") or 0)
        if not code or shares <= 0 or cost <= 0:
            errors.append(f"{code or '(空代码)'}：股数或成本不可用，跳过")
            continue
        if (code, round(shares, 2), round(cost, 4)) in existing:
            skipped += 1
            continue
        day = str(h.get("date") or "").strip() or china_today()
        try:
            journal.add_trade(
                date=day, code=code, name=str(h.get("name") or ""), playbook="其它",
                note="由旧持仓导入（原记录没有成交明细，建仓日期可能不准）",
                fills=[{"side": "buy", "date": day, "price": cost, "shares": shares}])
            imported += 1
        except (ValueError, TypeError) as exc:
            errors.append(f"{code}：{exc}")
    return JSONResponse({"ok": True, "imported": imported, "skipped": skipped,
                         "errors": errors})


@app.get("/api/journal/fees")
def api_journal_fees():
    from duanxian import journal

    return JSONResponse({"fees": journal.load_fees(), "labels": journal._FEE_LABELS,
                         "defaults": journal.DEFAULT_FEES})


@app.post("/api/journal/fees")
def api_journal_save_fees(request: Request, body: dict = Body(...)):
    """保存**使用者自己的**费率。默认值只是能跑起来的初值，不是推荐值。"""
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import journal

    try:
        return JSONResponse(journal.save_fees(body.get("fees") or {}))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/journal/delete")
def api_journal_delete(request: Request, trade_id: str):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import journal

    try:
        return JSONResponse(journal.delete_trade(trade_id))
    except (journal.JournalCorrupted, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/modes")
def api_modes():
    """模式卡列表 + 按版本分段的业绩。"""
    from duanxian import modes

    try:
        return JSONResponse({**modes.list_cards(), "performance": modes.performance()})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.post("/api/modes")
def api_save_mode(request: Request, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import modes

    try:
        return JSONResponse(modes.save_card(body or {}))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/modes/delete")
def api_delete_mode(request: Request, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import modes

    try:
        return JSONResponse(modes.delete_card(str(body.get("id") or "")))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ==================== ⑤ 账户风险与执行偏差 ====================
# ⛔ 与 ④ 同：这一段读的全是使用者自己录入的交易数据，**不接入任何 AI prompt**。


@app.get("/api/risk/report")
def api_risk_report():
    from duanxian import journal, risk

    try:
        return JSONResponse(risk.report())
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)


@app.get("/api/risk/attribution")
def api_risk_attribution():
    """判断 vs 执行归因四格。看对了却亏钱 = 执行问题；看错了还赚钱 = 运气。"""
    from duanxian import attribution, journal

    try:
        return JSONResponse(attribution.attribution())
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)


@app.get("/api/risk/excursion")
def api_risk_excursion():
    """MFE/MAE 与盈利回吐。

    ⚠️ 逐笔要拉历史行情（已永久缓存，首次会慢）→ **单独端点，不塞进 /risk/report**，
    否则每次打开交易日志页都要干等一轮网络。
    """
    from duanxian import excursion, journal

    try:
        return JSONResponse(excursion.summary())
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)


@app.get("/api/risk/at-risk")
def api_at_risk():
    """在险资金：现在这些仓位最坏会亏掉多少（按用户自己写下的计划止损算）。"""
    from duanxian import at_risk, journal

    try:
        return JSONResponse(at_risk.report())
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)


@app.get("/api/risk/equity-base")
def api_get_equity_base():
    from duanxian import at_risk

    return JSONResponse({"equity_base": at_risk.load_equity_base()})


@app.post("/api/risk/equity-base")
def api_set_equity_base(request: Request, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import at_risk

    try:
        return JSONResponse(at_risk.save_equity_base(body.get("equity_base")))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/risk/inbox")
def api_inbox():
    """异常交易收件箱：值得回头看一眼的那几笔（判定基准全来自用户自己）。"""
    from duanxian import inbox, journal

    try:
        return JSONResponse(inbox.build())
    except journal.JournalCorrupted as exc:
        return JSONResponse({"error": str(exc), "corrupted": True}, status_code=500)


@app.get("/api/risk/rules")
def api_risk_rules():
    from duanxian import risk

    return JSONResponse({"rules": risk.load_rules(), "labels": risk._RULE_LABELS,
                         "defaults": risk.DEFAULT_RULES})


@app.post("/api/risk/rules")
def api_risk_save_rules(request: Request, body: dict = Body(...)):
    """保存**用户自己的**风险宪法。系统只监控他有没有违反自己定的规矩。"""
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import risk

    try:
        return JSONResponse(risk.save_rules(body.get("rules") or {}))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ==================== ⑥ 原始数据归档 · 结构漂移 · 策略回测 ====================

@app.get("/api/archive/summary")
def api_archive_summary():
    """原始数据归档总览：囤了多久、多大、字段有没有漂移过。"""
    from duanxian import archive

    return JSONResponse(archive.summary())




# ========== 第二段：个人交易日志 ==========

# ==================== 个人交易日志 ====================
# ⚠️ 只统计用户自己录入的历史行为，**不产出任何下一笔建议**。
# 它是记账本 + 体检报告，不是参谋：不选股、不给买卖时机、不给参与倾向。


@app.get("/api/drift")
def api_drift():
    """结构漂移：数据源字段 + 市场结构 + 已登记的制度事件。"""
    from duanxian import drift

    return JSONResponse(drift.report())


@app.get("/api/drift/calendar")
def api_get_regime_calendar():
    from duanxian import drift

    return JSONResponse({"events": drift.load_calendar()})


@app.post("/api/drift/calendar")
def api_save_regime_calendar(request: Request, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import drift

    try:
        return JSONResponse(drift.save_calendar(body.get("events") or []))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------- 策略回测
_bt_lock = threading.Lock()


def _bt_path(days: int) -> str:
    """回测结果缓存路径 —— 与 backtest.prior_context 读的是同一份，别两处各写一份。"""
    from duanxian.backtest import result_path

    return result_path(days)


def _bt_load(days: int) -> Optional[dict]:
    """读回测缓存。走 backtest.load_result —— 它会连**策略集是否变过**一起校验。"""
    from duanxian.backtest import load_result

    return load_result(days)


def _bt_fresh(cached: dict) -> bool:
    """缓存是否覆盖到最新已收盘交易日。判不了（网络失败）→ 按新鲜处理，不硬刷。

    ⚠️ 必须用 `latest_session()` 而不是裸的 `last_trade_dates(1)`：腾讯 hist 收盘后
    有延迟，后者会返回上一个交易日，导致当天的缓存永远判成过期、每次请求重算。
    """
    try:
        from duanxian.trade_calendar import latest_session

        expected = latest_session()
    except Exception:  # noqa: BLE001
        expected = None
    return expected is None or cached.get("date_to") == expected


@app.get("/api/backtest")
def api_backtest(request: Request, days: int = 60, refresh: int = 0):
    """短线策略回测。首次约 1-2 分钟（逐日取数），历史结果落盘缓存后很快。

    ⚠️ 产出是「规则的历史统计」，不是前瞻标的。
    ⚠️ 强制刷新走 POST `/api/backtest/refresh`，原因见 `api_weekly`。
    """
    from duanxian.backtest import run_backtest

    if refresh and not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    force = bool(refresh) and _origin_ok(request)

    days = max(10, min(int(days or 60), 120))   # 夹在合理区间，防误传
    cached = _bt_load(days)
    if not force and cached and cached.get("available") and _bt_fresh(cached):
        return JSONResponse(cached)

    # 非阻塞取锁：正在算就回缓存 + busy，不让请求线程干等一两分钟
    if not _bt_lock.acquire(blocking=False):
        if cached and cached.get("available"):
            return JSONResponse({**cached, "busy": True})
        return JSONResponse({"error": "正在回测中，请稍后重试", "busy": True}, status_code=409)
    try:
        cached2 = _bt_load(days)
        if not force and cached2 and cached2.get("available") and _bt_fresh(cached2):
            return JSONResponse(cached2)
        bt = run_backtest(days)
        bt["generated_at"] = china_now().strftime("%Y-%m-%d %H:%M")
        if bt.get("available"):
            try:
                _atomic_write(_bt_path(days), bt)
            except Exception as exc:  # noqa: BLE001  写缓存失败不该拖垮响应，但必须出声
                print(f"⚠️ 回测缓存写入失败（每次请求都会重算）：{type(exc).__name__}: {exc}")
            return JSONResponse(bt)
        # 算失败：不覆盖有效缓存，尽量返回上一份 + stale 标记
        if cached2 and cached2.get("available"):
            stale = dict(cached2)
            stale["stale"] = True
            stale["warnings"] = (stale.get("warnings") or []) + ["刷新失败，展示上一份有效数据"]
            return JSONResponse(stale)
        return JSONResponse(bt)
    finally:
        _bt_lock.release()


@app.post("/api/backtest/refresh")
def api_backtest_refresh(request: Request, days: int = 60):
    """强制重跑回测。写操作走 POST + Origin 校验（见 api_weekly 的说明）。"""
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    return api_backtest(request, days=days, refresh=1)


# ==================== 主线 B：个股深挖 ====================
def _serialize_dd(final: dict) -> dict:
    ds = final.get("debate_state", {}) or {}
    now = china_now().strftime("%Y-%m-%d %H:%M") + " CST"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_type": "stock_deepdive",
        "code": final.get("code", ""),
        "name": final.get("name", ""),
        "trade_date": final.get("trade_date", ""),
        "generated_at": now,
        "verdict": final.get("verdict_struct"),
        "verdict_md": final.get("verdict", ""),
        "reports": {
            "theme": _md_to_html(final.get("theme_report", "")),
            "capital": _md_to_html(final.get("capital_report", "")),
            "technical": _md_to_html(final.get("technical_report", "")),
            "risk": _md_to_html(final.get("risk_report", "")),
        },
        "debate": {
            "join": _md_to_html(_strip_prefix(ds.get("join_history", ""), "参与派:")),
            "avoid": _md_to_html(_strip_prefix(ds.get("avoid_history", ""), "回避派:")),
        },
    }


def _run_dd(stock: str, job_id: str) -> None:
    error = None
    try:
        final = deepdive_run(stock)
        if final.get("error"):
            error = final["error"]
        else:
            payload = _serialize_dd(final)
            _atomic_write(safe_join(_DD_DIR, "latest.json"), payload)
            if payload.get("code"):
                _atomic_write(safe_join(_DD_DIR, f"{payload['code']}.json"), payload)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        with _lock:
            if _dd_job["job_id"] == job_id:
                _dd_job["running"] = False
                _dd_job["error"] = error
                if _dd_job["started"]:
                    _dd_job["elapsed"] = int(time.time() - _dd_job["started"])
                _dd_job["finished_at"] = china_now().strftime("%Y-%m-%d %H:%M:%S") + " CST"


@app.post("/api/deepdive/run")
def api_dd_run(request: Request, stock: str):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    stock = (stock or "").strip()
    if not stock:
        return JSONResponse({"error": "缺 stock 参数（6 位代码或简称）"}, status_code=400)
    with _lock:
        if _dd_job["running"] and not _job_stuck(_dd_job, _DD_TIMEOUT):
            # 繁忙：明确告知当前占用的是哪只票，前端据此不误加载别人的结果
            return {"running": True, "busy": True, "stock": _dd_job["stock"]}
        if _dd_job["running"]:   # 卡死的旧任务让位（同 api_run，见 _job_stuck）
            _dd_job["error"] = (f"上一次深挖（{_dd_job.get('stock')}）超过 "
                                f"{_DD_TIMEOUT // 60} 分钟无响应，已判为卡死")
        job_id = uuid.uuid4().hex
        _dd_job.update(running=True, job_id=job_id, stock=stock, error=None,
                       started=time.time(), elapsed=0, finished_at=None)
    threading.Thread(target=_run_dd, args=(stock, job_id), daemon=True).start()
    return {"running": True, "busy": False, "stock": stock, "job_id": job_id}


@app.get("/api/deepdive/status")
def api_dd_status():
    with _lock:
        snap = dict(_dd_job)
    if snap["running"] and snap["started"]:
        snap["elapsed"] = int(time.time() - snap["started"])
    snap.pop("started", None)
    return snap


@app.get("/api/deepdive/latest")
def api_dd_latest():
    path = os.path.join(_DD_DIR, "latest.json")
    if not os.path.exists(path):
        return JSONResponse({}, status_code=200)
    try:
        with open(path, encoding="utf-8") as fh:
            return JSONResponse(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({}, status_code=200)


def _deepdive_context() -> str:
    d = _load_latest_json(_DD_DIR)
    if not d:
        return "（暂无个股深挖数据，请先在个股深挖 Agent 深挖一只票。）"
    parts = [f"标的 {d.get('name', '')}（{d.get('code', '')}）", f"【深挖结论】\n{d.get('verdict_md', '')}"]
    r = d.get("reports", {}) or {}
    for k, t in [("theme", "题材归属"), ("capital", "资金流向"), ("technical", "技术形态"), ("risk", "风险排查")]:
        if r.get(k):
            parts.append(f"【{t}】\n{_strip_html(r[k])}")
    return "\n\n".join(parts)[:8000]


@app.post("/api/deepdive/chat")
def api_dd_chat(request: Request, body: dict = Body(...)):
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    msgs, err = _sanitize_messages(body.get("messages"))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return _chat(_deepdive_context(), "A 股短线个股深挖助手", msgs)


# ==================== 盘中快照 / 竞价核验 ====================
# ⚠️ 盘中快照**过了那个点就永远没有了**（拿的是实时行情），所以要后台按点自动抓，
# 不能等有人想起来点一下。⚠️ 调度一律按**上海时区**判断，不用本机时区 ——
#    机器在别的时区时，用本机时间会整天抓不到任何一个时点，而界面上看不出异样。
_intraday_thread_started = False
_intraday_start_lock = threading.Lock()


def _intraday_scheduler() -> None:
    """按 SNAPSHOT_SLOTS 定时抓盘面快照。交易日之外空转。

    ⚠️ 导入放在循环内的 try 里：放在函数开头（try 之外）的话，任一导入失败会让线程
       **当场退出**，而 `_intraday_thread_started` 已经是 True、不会再重启，
       循环里的异常日志也永远打不出来 —— 表现是"快照整天没抓，日志里一个字都没有"。
    """
    done: set[str] = set()
    last_day = None
    while True:
        try:
            from duanxian import intraday
            from duanxian.util import china_now, china_today, is_weekend

            today = china_today()
            if last_day is None:
                last_day = today
            if today != last_day:      # 跨天清账
                done.clear()
                last_day = today
            if not is_weekend(today):
                hhmm = china_now().strftime("%H:%M")
                for slot in intraday.SNAPSHOT_SLOTS:
                    key = f"{today}#{slot}"
                    # 到点后 6 分钟内补抓（进程刚起或调度稍晚也不漏）
                    if key not in done and slot <= hhmm <= _plus_minutes(slot, 6):
                        r = intraday.capture(slot, today)
                        # ⚠️ **只有成功才标完成**：无条件标记会让某个时点第一次碰上
                        #    数据源未更新或瞬时超时就被永久放弃，白白浪费后面的重试窗口。
                        if r.get("ok"):
                            done.add(key)
                        else:
                            print(f"⚠️ 盘中快照 {slot} 失败（窗口内会重试）：{r.get('reason')}")
                        break
        except Exception as exc:  # noqa: BLE001  调度线程绝不能死
            print(f"⚠️ 盘中调度异常：{type(exc).__name__}: {exc}")
        time.sleep(60)


def _plus_minutes(hhmm: str, n: int) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:])
    m += n
    return f"{(h + m // 60) % 24:02d}:{m % 60:02d}"


def _start_intraday() -> None:
    """启动盘中调度线程。幂等 —— 重复调用不会起第二个。

    ⚠️ 先探一下模块在不在再起线程：不探的话，模块缺失时线程会在后台每轮 ImportError，
       而**接口全是好的、界面也正常**，只有日志里刷错 —— 典型的"看不出来的坏"。
    """
    global _intraday_thread_started
    with _intraday_start_lock:          # 检查与置位必须在同一把锁里，否则可能起两个线程
        if _intraday_thread_started:
            return
        # ⚠️ 两种失败的异常形态**实测过，不要凭直觉写**（Python 3.12）：
        #    · 子模块不存在  → ImportError，exc.name == "duanxian"（不是 "duanxian.intraday"），
        #                      消息形如 "cannot import name 'intraday' from 'duanxian'"
        #    · 内部依赖缺失  → ModuleNotFoundError，exc.name 是**缺失的那个依赖名**
        #    照直觉写成「ModuleNotFoundError 且 name == 'duanxian.intraday'」的话，
        #    兼容分支永远命中不了，而真故障反而被当成兼容情况。
        try:
            from duanxian import intraday  # noqa: F401  只探在不在
        except ModuleNotFoundError as exc:
            # 走到这里 = intraday 在，但它依赖的东西缺了 —— 真故障，必须出声
            print(f"⚠️ 盘中调度未启动：intraday 的依赖缺失（{exc}）")
            return
        except ImportError as exc:
            if exc.name == "duanxian" and "intraday" in str(exc):
                return          # 该版本不含开盘核验，安静跳过
            print(f"⚠️ 盘中调度未启动：导入 intraday 失败（{type(exc).__name__}: {exc}）")
            return
        t = threading.Thread(target=_intraday_scheduler, daemon=True)
        t.start()
        _intraday_thread_started = True  # ⚠️ 只在 start() 成功之后才置位


# ⚠️ 用 lifespan 不用 @app.on_event（后者已废弃，FastAPI 升级后会静默不执行 →
# 盘中快照全天不抓，而界面上看不出任何异样）。
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _start_intraday()
    yield


app.router.lifespan_context = _lifespan


@app.get("/api/intraday/auction")
def api_auction(date: str | None = None):
    """09:25 竞价核验。没有当日存档时会现抓一张（仅当天有效）。"""
    from duanxian import intraday

    try:
        return JSONResponse(intraday.auction_check(date))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"available": False, "reason": f"{type(exc).__name__}: {exc}"})


@app.get("/api/intraday/path")
def api_intraday_path(date: str | None = None):
    """当天的情绪路径（各时点快照串成线）。"""
    from duanxian import intraday

    try:
        return JSONResponse(intraday.path_summary(date))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"available": False, "reason": f"{type(exc).__name__}: {exc}"})


@app.post("/api/intraday/capture")
def api_intraday_capture(request: Request, slot: str | None = None):
    """手动抓一张快照（写操作，走 POST + Origin 校验）。"""
    if not _origin_ok(request):
        return JSONResponse({"error": "非法来源"}, status_code=403)
    from duanxian import intraday

    return JSONResponse(intraday.capture(slot))


def _mount_static() -> None:
    """挂静态资源 + SPA 兜底"""
    if not os.path.isdir(_DIST):
        print(f"ℹ️ 未找到前端构建产物（{_DIST}）。请先执行："
              f"cd frontend && npm install && npm run build")
        return
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")),
              name="assets")

    @app.get("/{full_path:path}")
    def _spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"error": f"未知接口 /{full_path}"}, status_code=404)
        # 真实存在的静态文件（favicon / manifest / 图片…）直接给
        candidate = safe_join(_DIST, full_path) if full_path else None
        if candidate and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_DIST, "index.html"))

    print(f"✓ React 构建产物已挂载（{_DIST}）")


_mount_static()

if __name__ == "__main__":
    import uvicorn

    # 端口可用 VIBE_PORT 覆盖，默认 8910（被占时不必改代码）
    port = int(os.environ.get("VIBE_PORT", "8910"))
    if _DISABLED_CLIS:
        # 有声地说出限制：不说的话，"少了几个可选项"看起来像 bug 而不是有意为之
        print(f"🔒 已禁用自动批准 CLI：{', '.join(_DISABLED_CLIS)}"
              f"（保留 {', '.join(sorted(_ALLOWED_CLI_KINDS))}）")
    if _opted_in_clis():
        # 放开了危险 CLI 就必须说清放开了什么、代价是什么 ——
        # 光印一句"保留 claude, codex"看不出后者是被特批进来的
        print(f"⚠️  VIBE_ALLOW_UNSAFE_CLI 已放开：{', '.join(sorted(_opted_in_clis()))}"
              f" —— 这些 CLI 会不经询问地读写文件、执行命令，"
              f"而问 AI 时抓来的外部新闻原文会原样进 prompt。确认你信任数据源再用。")
    print(f"→ 打开 http://127.0.0.1:{port}  （VR 分栏路由 {_VR_ROUTES} 条已并入）")
    uvicorn.run(app, host="127.0.0.1", port=port)

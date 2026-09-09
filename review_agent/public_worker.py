"""Run one allowlisted public data call under the same crash/deadline guard."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .process_io import GUARD_REAP_EXIT
from .engine_guard import guard_python
from .evidence import EvidenceError, canonical

QUERY_CALLS = {"query_quote", "query_valuation", "query_reports", "query_news", "query_global_stock"}
MIGRATED_CALLS = {"run_backtest", "macro_probability"}
DEEPDIVE_CALLS = {"resolve", "get_profile", "get_theme", "get_lhb", "get_kline"}


def fetch_public(name, args, directory, check, *, timeout=90):
    from .runtime import REPO, engine_environment, stop_process
    if name not in DEEPDIVE_CALLS | QUERY_CALLS | MIGRATED_CALLS:
        raise EvidenceError("取数接口不在本次允许范围")
    budget = min(float(check()), timeout)
    path = Path(directory) / ("worker-" + uuid.uuid4().hex + ".jsonl")
    env = engine_environment(Path(directory))
    env["PYTHONPATH"] = str(REPO)
    env["VIBE_ASTOCK_PROMPTS"] = "builtin"
    proc = None
    try:
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            proc = subprocess.Popen([guard_python(), str(REPO / "review_agent/engine_guard.py"),
                str(budget), str(os.getpid()), "--bridge-reap-group", sys.executable,
                "-m", "review_agent.public_worker", name, canonical(args)], cwd=REPO,
                env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                start_new_session=True)
            deadline = time.monotonic() + budget + 2
            while proc.poll() is None:
                check()
                if time.monotonic() > deadline:
                    raise EvidenceError("公开资料取数超时，已停止")
                if path.stat().st_size > 5_000_000:
                    raise EvidenceError("公开资料结果过大，已停止")
                time.sleep(.05)
            check()
        if path.stat().st_size > 5_000_000:
            raise EvidenceError("公开资料结果过大")
        try:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if (proc.returncode != GUARD_REAP_EXIT or len(events) != 2 or events[-1] != {"type": "bridge_exit", "code": 0}
                    or events[0].get("type") != "result" or "value" not in events[0]):
                raise ValueError()
        except (ValueError, UnicodeError, AttributeError):
            raise EvidenceError("公开资料取数未完整完成；请检查网络或稍后重试") from None
        return events[0]["value"]
    finally:
        if proc is not None: stop_process(proc)
        path.unlink(missing_ok=True)


def query_public(name, args):
    import re
    from .runtime import REPO
    if not isinstance(args, dict): raise ValueError("invalid arguments")
    if name == "query_quote":
        if (set(args) != {"codes"} or not isinstance(args["codes"], list) or not 1 <= len(args["codes"]) <= 10
                or any(not isinstance(c, str) or not re.fullmatch(r"\d{6}", c) for c in args["codes"])):
            raise ValueError("invalid codes")
    elif name == "query_global_stock":
        if set(args) != {"symbol"} or not isinstance(args["symbol"], str) or not re.fullmatch(r"[A-Za-z0-9.^-]{1,16}", args["symbol"]):
            raise ValueError("invalid symbol")
    elif name in {"query_valuation", "query_reports", "query_news"}:
        if set(args) != {"code"} or not isinstance(args["code"], str) or not re.fullmatch(r"\d{6}", args["code"]):
            raise ValueError("invalid code")
    else:
        raise ValueError("unknown tool")
    sys.path.insert(0, str(REPO / "vr"))
    import astock
    if name == "query_quote": return astock.tencent_quote(args["codes"])
    if name == "query_valuation": return astock.full_valuation(args["code"])
    if name == "query_reports":
        return [{k: row.get(k) for k in ("title", "publishDate", "orgSName", "emRatingName")}
                for row in astock.eastmoney_reports(args["code"], max_pages=1)[:15]]
    if name == "query_news":
        return [{k: row.get(k) for k in ("新闻标题", "发布时间", "文章来源")}
                for row in astock.stock_news(args["code"], limit=15)]
    import gstock
    return gstock.us_hk_stock(args["symbol"]) or {"error": "没有查到该代码的公开资料"}


def main():
    import contextlib
    name, args = sys.argv[1], json.loads(sys.argv[2])
    if name not in DEEPDIVE_CALLS | QUERY_CALLS | MIGRATED_CALLS or not isinstance(args, list):
        raise ValueError("unsupported request")
    # Data libraries may print progress. Keep the protocol separate.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        if name == "run_backtest":
            from .backtesting import calculate
            result = calculate(*args)
        elif name == "macro_probability":
            from .backtesting import probability
            result = probability()
        elif name in QUERY_CALLS:
            result = query_public(name, *args)
        else:
            from duanxian.deepdive import data
            result = getattr(data, name)(*args)
    print(canonical({"type": "result", "value": result}), flush=True)


if __name__ == "__main__":
    main()

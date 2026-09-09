"""Deterministic post-review capture; never invokes a model or changes the report.

Each result is retained next to the generation evidence. Network/data failures
are explicit and may be retried without regenerating a paid AI report.
"""
from .engine_guard import guard_python
from threading import Lock

_CAPTURE_LOCK = Lock()

from duanxian import archive, backtest, theme_tree, reflection


def capture_after_review(date: str) -> dict:
    with _CAPTURE_LOCK:
        return _capture_after_review(date)


def _capture_after_review(date: str) -> dict:
    results = {}
    # Capture caches before archiving their source rows.
    for name, capture in (("theme_reasons", theme_tree.capture),
                          ("backtest_corpus", backtest.capture),
                          ("archive", archive.capture_day)):
        try:
            result = capture(date)
            results[name] = result if isinstance(result, dict) else {"ok": False, "reason": "未返回保存结果"}
        except Exception as exc:
            results[name] = {"ok": False, "reason": f"{type(exc).__name__}：未完成，可单独重试"}
    try:
        value = reflection.auto_evaluate_prior(date, strict=True)
        results["reflection"] = {"ok": True, "status": "evaluated" if value else "not_applicable", "reason": "已回评" if value else "没有可完成的上期回评；历史预测或次日数据未覆盖"}
    except Exception as exc:
        results["reflection"] = {"ok": False, "reason": "回评所需资料尚未完整，未完成，可重试" if str(exc) == "回评所需资料尚未完整，未完成，可重试" else f"{type(exc).__name__}：回评未完成，请检查数据源后重试"}
    return results


# Both automatic capture and explicit retry enter through this bounded worker.
# Isolate network calls so cancellation can actually stop a stalled fetch.
def capture_bounded(date: str, check=None, timeout: float = 90) -> dict:
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import tempfile
    import time
    from review_agent.evidence import valid_date
    from review_agent.runtime import REPO, stop_process
    valid_date(date)
    if not _CAPTURE_LOCK.acquire(blocking=False):
        return {"capture": {"ok": False, "reason": "归档正在处理，请稍后重试"}}
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="astock-capture-") as directory:
            result_path = Path(directory) / "result.json"
            process = subprocess.Popen([guard_python(), str(REPO / "review_agent/engine_guard.py"),
                                       str(timeout), str(os.getpid()), sys.executable, "-m", "review_agent.post_review", date, str(result_path)],
                                       cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                                       start_new_session=os.name == "posix")
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if check is not None:
                    check()
                if time.monotonic() >= deadline:
                    return {"capture": {"ok": False, "reason": "归档取数超时，报告已保存，可单独重试"}}
                try:
                    process.wait(timeout=min(0.2, max(0.01, deadline-time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode or not result_path.is_file():
                return {"capture": {"ok": False, "reason": "归档进程未完成，报告已保存，可重试"}}
            return json.loads(result_path.read_text(encoding="utf-8"))
    finally:
        if process is not None:
            stop_process(process)
        _CAPTURE_LOCK.release()


if __name__ == "__main__":
    import json
    from pathlib import Path
    import sys
    from review_agent.evidence import valid_date
    valid_date(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(_capture_after_review(sys.argv[1]), ensure_ascii=False), encoding="utf-8")

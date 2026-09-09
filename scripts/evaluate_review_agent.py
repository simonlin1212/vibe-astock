"""Explicit live-model regression. Keys are passed by environment, never in output.

Run from repo root: python -m scripts.evaluate_review_agent --provider mimo
Requires ASTOCK_EVAL_API_KEY for API mode. This command consumes model usage.
"""
from __future__ import annotations

from duanxian.paths import data_path
import argparse
import json
import os
import threading
import time
import uuid
from pathlib import Path

from review_agent.evidence import EvidenceError, build_bundle, canonical
from review_agent.runtime import Runtime
from review_agent.api import Manager
from review_agent.store import Store


def score(case: dict, result: dict | None, error: str | None) -> bool:
    if case.get("safe_refusal"):
        return (error is not None and "未开放" in error) or bool(result and result["status"] == "incomplete" and result["gaps"])
    if not result:
        return False
    if case.get("status") and result["status"] != case["status"]:
        return False
    if needed := case.get("required_market_evidence"):
        if not any(e["kind"] == "metric" and e.get("metric") == needed["metric"]
                   and e.get("date") == needed["date"] and e.get("available") is True for e in result["evidence"]):
            return False
    found = {e.get("metric") for e in result["evidence"] if e["kind"] == "calculation"
             and e.get("first_date") == case.get("expected_first_date") and e.get("date") == case.get("expected_last_date")}
    return set(case.get("calculations", [])) <= found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["mimo", "openai", "codex-private"], required=True)
    parser.add_argument("--model")
    parser.add_argument("--reviews", type=Path, default=Path(data_path("reviews")))
    parser.add_argument("--cases", type=Path, default=Path(__file__).resolve().parents[1] / "evals/review_agent.json")
    parser.add_argument("--state", type=Path, default=Path.home() / ".vibe-astock-agent")
    args = parser.parse_args()
    key = os.environ.get("ASTOCK_EVAL_API_KEY", "")
    if args.provider != "codex-private" and not key:
        parser.error("请通过 ASTOCK_EVAL_API_KEY 提供密钥，不要写在命令参数或样本文件中")
    if args.provider == "codex-private" and not args.model:
        parser.error("订阅评测请用 --model 指定接入页已测试的账户可用模型")
    source = {"provider": args.provider, "model": args.model or ("mimo-v2.5-pro" if args.provider == "mimo" else "gpt-5.4")}
    if args.provider != "codex-private":
        source["baseURL"] = "https://token-plan-cn.xiaomimimo.com/v1" if args.provider == "mimo" else "https://api.openai.com/v1"
    suite = json.loads(args.cases.read_text())
    bundle = build_bundle(args.reviews, suite["anchor"])
    runtime = Runtime(args.state.resolve())
    conv = {"bundle": bundle, "source": source, "turns": []}
    report = {"source": source, "bundle_revision": bundle["revision"], "cases": []}
    output = runtime.root / "evaluations" / (uuid.uuid4().hex + ".json")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    manager = Manager(Store(runtime.root), args.reviews, runtime)
    try:
        evaluate(suite, runtime, conv, key, report, output)
    finally:
        manager.shutdown()
    print(str(output))
    raise SystemExit(0 if all(c["passed"] for c in report["cases"]) else 1)


def evaluate(suite, runtime, conv, key, report, output):
    for index, case in enumerate(suite["cases"]):
        result, error, started = None, None, time.monotonic()
        try:
            result = runtime.run(conv, case["question"], key, uuid.uuid4().hex, threading.Event(), lambda _: None)
            conv["turns"].append({"question": case["question"], "status": result["status"], "result": result})
        except EvidenceError as exc:
            error = str(exc)
        row = {"case": case["name"], "passed": score(case, result, error), "seconds": round(time.monotonic() - started, 2),
               "corrections": result.get("format_corrections") if result else None, "result": result, "error": error}
        report["cases"].append(row)
        output.write_text(canonical(report))
        print(canonical({k: v for k, v in row.items() if k != "result"}), flush=True)
        if error and "请求限流" in error:
            # Do not hammer the provider or count unrun cases as model failures.
            for pending in suite["cases"][index + 1:]:
                report["cases"].append({"case": pending["name"], "passed": False, "status": "not_run",
                                        "reason": "供应商限流，已停止本次评测，待冷却后重新执行"})
            output.write_text(canonical(report))
            break


if __name__ == "__main__":
    main()

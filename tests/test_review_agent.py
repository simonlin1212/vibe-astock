"""Behavioral contracts for the bounded review Agent (offline)."""
import json
from pathlib import Path

import pytest

from review_agent.evidence import EvidenceError, build_bundle, ToolSession, validate_answer


def save_review(root, date, count=40):
    payload = {"target_date": date, "focus_md": "公开的历史复盘叙述",
               "emotion_metrics": {"promotion": {"available": True, "limit_up_count": count}},
               "market_facts": {}, "journal": "PRIVATE_CANARY", "api_key": "SECRET_CANARY"}
    (root / f"{date}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_bundle_pins_day_and_excludes_personal_fields(tmp_path):
    save_review(tmp_path, "2026-09-01", 30)
    save_review(tmp_path, "2026-09-02", 45)
    save_review(tmp_path, "2026-09-03", 60)
    bundle = build_bundle(tmp_path, "2026-09-02")
    text = json.dumps(bundle)
    assert "2026-09-03" not in text
    assert "PRIVATE_CANARY" not in text and "SECRET_CANARY" not in text
    assert bundle["dates"] == ["2026-09-02", "2026-09-01"]
    assert all(e["source_sha256"] for e in bundle["evidence"])


def test_selected_missing_never_falls_back_to_latest(tmp_path):
    save_review(tmp_path, "2026-09-01")
    with pytest.raises(EvidenceError, match="所选"):
        build_bundle(tmp_path, "2026-09-02")


def test_symlink_rejected(tmp_path):
    save_review(tmp_path, "2026-09-01")
    (tmp_path / "2026-09-02.json").symlink_to(tmp_path / "2026-09-01.json")
    with pytest.raises(EvidenceError):
        build_bundle(tmp_path, "2026-09-02")


def test_comparison_and_current_turn_citations(tmp_path):
    save_review(tmp_path, "2026-09-01", 30)
    save_review(tmp_path, "2026-09-02", 45)
    bundle = build_bundle(tmp_path, "2026-09-02")
    session = ToolSession(bundle, tmp_path / "calls.jsonl")
    calc = session.compare_metric("limit_up_count", "2026-09-01", "2026-09-02")
    assert calc["value"] == 15 and calc["unit"] == "家"
    assert len(calc["inputs"]) == 2
    payload = {"status": "complete", "findings": [{"text": "涨停家数增加。", "citations": [calc["id"]]}], "gaps": []}
    answer = validate_answer(payload, session)
    assert answer["evidence"][0]["value"] == 15
    with pytest.raises(EvidenceError):
        validate_answer(payload, ToolSession(bundle, tmp_path / "new.jsonl"))


def test_missing_is_not_zero_or_a_calculation(tmp_path):
    save_review(tmp_path, "2026-09-01", None)
    save_review(tmp_path, "2026-09-02", 45)
    session = ToolSession(build_bundle(tmp_path, "2026-09-02"), tmp_path / "calls.jsonl")
    with pytest.raises(EvidenceError, match="缺少"):
        session.compare_metric("limit_up_count", "2026-09-01", "2026-09-02")


def test_freehand_numbers_rejected(tmp_path):
    save_review(tmp_path, "2026-09-01")
    session = ToolSession(build_bundle(tmp_path, "2026-09-01"), tmp_path / "calls.jsonl")
    eid = session.bundle["evidence"][0]["id"]
    session.read_evidence([eid])
    for text in ["涨停增加了 999 家。", "涨停增加了九百家。", "炸板率升至百分之九十。", "家数为九。", "家数为１。", "家数为九\u200b十。"]:
        with pytest.raises(EvidenceError, match="数字"):
            validate_answer({"status": "complete", "findings": [{"text": text, "citations": [eid]}], "gaps": []}, session)
    with pytest.raises(EvidenceError, match="边界"):
        validate_answer({"status": "complete", "findings": [{"text": "建\u200b议买入。", "citations": [eid]}], "gaps": []}, session)
    accepted = validate_answer({"status": "complete", "findings": [{
        "text": "这一观察反映两个时点的差异，三个可比指标不能推断连续趋势。", "citations": [eid]}], "gaps": []}, session)
    assert accepted["findings"][0]["text"].startswith("这一观察")


def test_snapshot_hash_changes_when_source_changes(tmp_path):
    save_review(tmp_path, "2026-09-01", 40)
    before = build_bundle(tmp_path, "2026-09-01")
    save_review(tmp_path, "2026-09-01", 41)
    assert before["revision"] != build_bundle(tmp_path, "2026-09-01")["revision"]


def test_percent_comparison_uses_points_and_preserves_inputs(tmp_path):
    for day, rate in [("2026-09-01", .2), ("2026-09-02", .35)]:
        save_review(tmp_path, day)
        path = tmp_path / f"{day}.json"
        data = json.loads(path.read_text())
        data["market_facts"] = {"seal_quality": {"broken_rate": rate}}
        path.write_text(json.dumps(data))
    session = ToolSession(build_bundle(tmp_path, "2026-09-02"), tmp_path / "tools.jsonl")
    calc = session.compare_metric("broken_rate", "2026-09-01", "2026-09-02")
    assert calc["display"] == "15.00 个百分点"
    replay = ToolSession(session.bundle, session.log)
    replay.replay()
    answer = validate_answer({"status": "complete", "findings": [{"text": "炸板率上升。", "citations": [calc["id"]]}], "gaps": []}, replay)
    assert [e["value"] for e in answer["evidence"]] == [15, 20, 35]


def test_numbers_in_gaps_rejected_but_fixed_metric_names_allowed(tmp_path):
    save_review(tmp_path, "2026-09-01")
    session = ToolSession(build_bundle(tmp_path, "2026-09-01"), tmp_path / "tools.jsonl")
    payload = {"status": "incomplete", "findings": [{"text": "资料不足。", "citations": []}], "gaps": ["跌超5%家数未获取。"]}
    assert validate_answer(payload, session)["status"] == "incomplete"
    payload["gaps"] = ["还差 30 家的数据。"]
    with pytest.raises(EvidenceError, match="数字"):
        validate_answer(payload, session)


def test_store_persistence_frozen_scope_idempotency_and_busy(tmp_path):
    from review_agent.store import Store
    root = tmp_path / "reviews"
    root.mkdir()
    save_review(root, "2026-09-01", 30)
    store = Store(tmp_path / "state")
    source = {"provider": "mimo", "model": "mimo-v2.5"}
    factory = lambda: build_bundle(root, "2026-09-01")
    first, created = store.start("2026-09-01", "公开复盘？", "a" * 32, source, factory)
    assert created
    duplicate, created = store.start("2026-09-01", "公开复盘？", "a" * 32, source, factory)
    assert not created and duplicate == first
    with pytest.raises(EvidenceError, match="已有"):
        store.start("2026-09-01", "另一个问题", "b" * 32, source, factory)
    with pytest.raises(EvidenceError, match="同一"):
        store.start("2026-09-01", "换了问题", "a" * 32, source, factory)
    store.finish(first["id"], error="上游失败")
    save_review(root, "2026-09-01", 99)
    resumed = Store(tmp_path / "state")
    conv = resumed.conversation(first["conversation_id"], internal=True)
    assert conv["bundle"]["evidence"][0]["value"] == 30
    assert conv["turns"][0]["status"] == "failed"
    second, created = resumed.start("2026-09-01", "继续", "c" * 32, source, factory, conv["id"])
    assert created and second["conversation_id"] == conv["id"]
    resumed.cancel(second["id"])
    resumed.finish(second["id"], result={"status": "complete"})
    assert resumed.turn(second["id"])["status"] == "cancelled"


def test_stale_worker_does_not_resume_or_remain_running(tmp_path):
    from review_agent.store import Store
    save_review(tmp_path, "2026-09-01")
    store = Store(tmp_path / "state")
    turn, _ = store.start("2026-09-01", "问题", "a" * 32, {}, lambda: build_bundle(tmp_path, "2026-09-01"))
    with store.connect() as db:
        db.execute("UPDATE turns SET updated=0 WHERE id=?", (turn["id"],))
    recovered = Store(tmp_path / "state").turn(turn["id"])
    assert recovered["status"] == "failed" and "中断" in recovered["error"]


def test_credentials_only_in_engine_env_and_config_blocks_escape(tmp_path, monkeypatch):
    import tomllib
    from review_agent.runtime import connection, config_for, engine_environment, toml
    monkeypatch.setenv("PRIVATE_OTHER_API_KEY", "private-canary")
    source, key = connection({"provider": "mimo", "model": "mimo-v2.5", "apiKey": "ephemeral-canary", "baseURL": "https://token-plan-cn.xiaomimimo.com/v1/"})
    assert key == "ephemeral-canary" and key not in json.dumps(source)
    cfg = config_for(tmp_path, source)
    assert all(v is False for v in cfg["features"].values())
    assert set(cfg["mcp_servers"]) == {"astock"}
    assert cfg["mcp_servers"]["astock"]["env_vars"] == []
    assert "ephemeral-canary" not in toml(cfg)
    assert tomllib.loads("x=" + toml({"quoted": 'a"b\\c', "flags": [True, False]}))["x"]["quoted"] == 'a"b\\c'
    env = engine_environment(tmp_path, key)
    assert env["ASTOCK_MODEL_KEY"] == key and "PRIVATE_OTHER_API_KEY" not in env
    for url in ("http://127.0.0.1/v1", "https://api.openai.com.evil.invalid/v1", "https://user:pass@api.openai.com/v1"):
        with pytest.raises(EvidenceError):
            connection({"model": "x", "apiKey": key, "baseURL": url})


def test_event_channel_requires_completion_and_rejects_uncontrolled_tools(tmp_path):
    import os
    import subprocess
    import sys
    import threading
    from review_agent.runtime import consume_events
    proc = subprocess.Popen([sys.executable, "-c", 'print(\'{"type":"turn.completed"}\')'],
                            stdout=subprocess.PIPE, start_new_session=True, bufsize=0)
    assert consume_events(proc, threading.Event(), lambda m: None, 3) == ""
    scenarios = [
        ([{"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}], "未完成"),
        ([{"type": "item.started", "item": {"type": "command_execution"}}], "未开放"),
        ([{"type": "item.started", "item": {"type": "mcp_tool_call", "server": "foreign", "tool": "read"}}], "未开放"),
        ([{"type": "error", "message": "SECRET_CANARY"}], "AI 请求失败"),
    ]
    for messages, expected in scenarios:
        program = "import json; events=" + repr(messages) + "; [print(json.dumps(e),flush=True) for e in events]"
        proc = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, start_new_session=os.name == "posix", bufsize=0)
        with pytest.raises(EvidenceError, match=expected) as error:
            consume_events(proc, threading.Event(), lambda m: None, 3)
        assert "SECRET_CANARY" not in str(error.value)


def test_engine_cancel_timeout_and_independent_guard(tmp_path):
    import os
    import subprocess
    import sys
    import threading
    import time
    from review_agent.runtime import consume_events, REPO
    for cancel, timeout, expected in [(True, 3, "取消"), (False, .1, "超时")]:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=subprocess.PIPE, start_new_session=True, bufsize=0)
        event = threading.Event()
        if cancel:
            event.set()
        with pytest.raises(EvidenceError, match=expected):
            consume_events(proc, event, lambda m: None, timeout)
        assert proc.poll() is not None
    before = time.monotonic()
    guarded = subprocess.Popen([sys.executable, str(REPO / "review_agent/engine_guard.py"), ".15", str(os.getpid()), sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    assert guarded.wait(timeout=3) != 0
    assert time.monotonic() - before < 3


def test_api_auth_validation_cancellation_and_no_secret_persistence(tmp_path, request):
    import threading
    import time
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from review_agent.api import Manager, create_router
    from review_agent.store import Store

    class WaitingRuntime:
        calls = 0
        def status(self):
            return {"installed": True}
        def run(self, conv, question, key, turn_id, cancel, progress):
            self.calls += 1
            assert key == "SECRET_CANARY"
            progress("读取复盘证据")
            # Wait for the event being tested, not a three-second successful
            # completion that can race slow CI or a busy development machine.
            if not cancel.wait(30):
                raise AssertionError("test worker never received cancellation")
            return {"status": "complete", "findings": [], "gaps": [], "evidence": []}

    save_review(tmp_path, "2026-09-01")
    runtime = WaitingRuntime()
    store = Store(tmp_path / "state")
    manager = Manager(store, tmp_path, runtime)
    request.addfinalizer(manager.shutdown)
    app = FastAPI()
    app.include_router(create_router(manager, "local-access"))
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"authorization": "Bearer local-access"}
    payload = {"anchor": "2026-09-01", "question": "复盘？", "request_id": "a" * 32,
               "llm": {"provider": "mimo", "model": "mimo-v2.5", "baseURL": "https://token-plan-cn.xiaomimimo.com/v1", "apiKey": "SECRET_CANARY"}}
    assert client.get("/api/review-agent/status").status_code == 401
    assert client.get("/api/review-agent/status", headers={**headers, "host": "evil.invalid"}).status_code == 403
    assert client.post("/api/review-agent/turns", json=payload, headers={**headers, "origin": "https://evil.invalid"}).status_code == 403
    assert client.post("/api/review-agent/turns", content="x" * 21000, headers=headers).status_code == 413
    bad = client.post("/api/review-agent/turns", json={**payload, "llm": {"apiKey": "SECRET_CANARY"}}, headers=headers)
    assert bad.status_code == 422 and "SECRET_CANARY" not in bad.text
    first = client.post("/api/review-agent/turns", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    turn = first.json()
    again = client.post("/api/review-agent/turns", json=payload, headers=headers)
    assert again.json()["id"] == turn["id"]
    cancelled = client.post(f"/api/review-agent/turns/{turn['id']}/cancel", json={}, headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    manager.shutdown()
    assert runtime.calls == 1
    assert store.turn(turn["id"])["status"] == "cancelled"
    assert b"SECRET_CANARY" not in store.path.read_bytes()


def test_crashed_backend_releases_lock_and_recovers_without_rebilling(tmp_path):
    import os
    import subprocess
    import sys
    from review_agent.api import Manager
    from review_agent.runtime import Runtime
    from review_agent.store import Store
    save_review(tmp_path, "2026-09-01")
    root = tmp_path / "state"
    program = '''import os,sys
from pathlib import Path
from review_agent.api import Manager
from review_agent.runtime import Runtime
from review_agent.store import Store
from review_agent.evidence import build_bundle
root, reviews = map(Path, sys.argv[1:])
store = Store(root)
manager = Manager(store, reviews, Runtime(root))
turn, _ = store.start('2026-09-01', '问题', 'a'*32, {}, lambda: build_bundle(reviews,'2026-09-01'))
print(turn['id'],flush=True)
sys.stdin.readline()
os._exit(9)
'''
    proc = subprocess.Popen([sys.executable, "-c", program, str(root), str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        turn_id = proc.stdout.readline().strip()
        assert len(turn_id) == 32
        store = Store(root)
        with pytest.raises(EvidenceError, match="另一个"):
            Manager(store, tmp_path, Runtime(root))
        # Crash the actual backend without shutdown/finalizers. On Windows a
        # venv Popen PID may name its redirector, so kill() can leave the holder alive.
        proc.communicate("crash\n", timeout=3)
        assert proc.returncode == 9
        recovered = Manager(store, tmp_path, Runtime(root))
        try:
            assert store.turn(turn_id)["status"] == "failed"
            same, created = store.start("2026-09-01", "问题", "a" * 32, {}, lambda: {})
            assert not created and same["id"] == turn_id and same["status"] == "failed"
        finally:
            recovered.shutdown()
    finally:
        if proc.poll() is None:
            proc.communicate("crash\n", timeout=3)
        for stream in (proc.stdin, proc.stdout):
            stream.close()


def test_format_correction_is_bounded_and_rereads_evidence(tmp_path, monkeypatch):
    import threading
    from review_agent.runtime import Runtime, parse_answer
    assert parse_answer('```json\n{"status":"incomplete"}\n```') == {"status": "incomplete"}
    with pytest.raises(EvidenceError):
        parse_answer('extra prose {"status":"complete"}')
    save_review(tmp_path, "2026-09-01")
    bundle = build_bundle(tmp_path, "2026-09-01")
    runtime = Runtime(tmp_path / "state")
    calls, events = [], []
    def invoke(run, source, key, prompt, cancel, progress, budget):
        calls.append((run, budget, prompt))
        tool = ToolSession(bundle, run / "tools.jsonl")
        eid = bundle["evidence"][0]["id"]
        tool.read_evidence([eid])
        rejected = tool.submit_answer("complete", [{"text": "涨停九百家", "citations": []}], [])
        assert not rejected["accepted"]
        assert tool.submit_answer("complete", [{"text": "快照中有涨停家数。", "citations": [eid]}], [])["accepted"]
        return "分析完成"
    monkeypatch.setattr(runtime, "_invoke", invoke)
    conv = {"bundle": bundle, "source": {}, "turns": []}
    result = runtime.run(conv, "问题", "test", "a" * 32, threading.Event(), events.append)
    assert result["format_corrections"] == 1 and len(calls) == 1
    assert result["elapsed_seconds"] >= 0
    calls.clear()
    monkeypatch.setattr(runtime, "_invoke", lambda *args: calls.append(1) or "invalid")
    with pytest.raises(EvidenceError, match="未提交"):
        runtime.run(conv, "问题", "test", "b" * 32, threading.Event(), events.append)
    assert len(calls) == 1


def test_submission_limit_and_sealed_replay(tmp_path):
    save_review(tmp_path, "2026-09-01")
    bundle = build_bundle(tmp_path, "2026-09-01")
    log = tmp_path / "tools.jsonl"
    session = ToolSession(bundle, log)
    for _ in range(4):
        assert not session.submit_answer("complete", [{"text": "自由数字九", "citations": []}], [])["accepted"]
    with pytest.raises(EvidenceError, match="用完"):
        session.submit_answer("incomplete", [{"text": "资料缺失", "citations": []}], ["未读取资料"])
    with pytest.raises(EvidenceError, match="用完"):
        ToolSession(bundle, log).replay()
    session = ToolSession(bundle, tmp_path / "valid.jsonl")
    assert session.submit_answer("incomplete", [{"text": "资料缺失", "citations": []}], ["未读取资料"])["accepted"]
    with pytest.raises(EvidenceError, match="已确认"):
        session.list_evidence()
    replay = ToolSession(bundle, session.log)
    replay.replay()
    assert replay.answer == session.answer
    with session.log.open("a") as f:
        f.write(json.dumps({"tool": "list_evidence", "args": {}, "ids": []}) + "\n")
    with pytest.raises(EvidenceError, match="额外"):
        ToolSession(bundle, session.log).replay()


def test_provider_failure_is_not_retried(tmp_path, monkeypatch):
    import threading
    from review_agent.runtime import Runtime
    save_review(tmp_path, "2026-09-01")
    runtime = Runtime(tmp_path / "state")
    calls = []
    def failed(*args):
        calls.append(1)
        raise EvidenceError("AI 请求失败")
    monkeypatch.setattr(runtime, "_invoke", failed)
    with pytest.raises(EvidenceError, match="请求失败"):
        runtime.run({"bundle": build_bundle(tmp_path, "2026-09-01"), "source": {}, "turns": []},
                    "问题", "test", "a" * 32, threading.Event(), lambda m: None)
    assert len(calls) == 1


def test_server_agent_is_scoped_to_lifespan(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import server
    root = tmp_path / "product"
    monkeypatch.setattr(server, "_REVIEW_AGENT_ROOT", root)
    monkeypatch.setattr(server, "_start_intraday", lambda: None)
    client = TestClient(server.app, base_url="http://127.0.0.1")
    assert client.get("/api/review-agent/status").status_code == 503
    assert not root.exists()
    # A stopped server releases ownership, allowing another clean startup.
    for _ in range(2):
        with TestClient(server.app, base_url="http://127.0.0.1") as client:
            assert client.get("/api/review-agent/conversations?anchor=2026-09-01").status_code == 200
        assert server.app.state.review_agent is None


def test_followup_retains_query_coordinates_without_old_citable_ids():
    from review_agent.runtime import conversation_history
    turns = [{"status": "complete", "question": "比较涨停家数", "result": {
        "gaps": [],
        "findings": [{"text": "涨停家数增加。", "citations": ["calc-old"]}],
        "evidence": [{"id": "calc-old", "kind": "calculation", "metric": "limit_up_count",
                      "first_date": "2026-09-01", "date": "2026-09-02", "value": 999}],
    }}, {"status": "failed", "question": "FAILED_CANARY", "result": None}]
    history = conversation_history(turns)
    assert history[0]["comparisons_to_recompute_if_referenced"] == [
        {"metric": "limit_up_count", "first_date": "2026-09-01", "last_date": "2026-09-02"}]
    assert history[0]["prior_inferences_not_evidence"] == ["涨停家数增加。"]
    assert not any(s in json.dumps(history) for s in ("calc-old", "999", "FAILED_CANARY"))
    turns[0]["status"] = "incomplete"
    turns[0]["result"]["gaps"] = ["缺少梯队分布", "缺少成交额"]
    projected = conversation_history(turns)[0]
    assert projected["prior_status"] == "incomplete"
    assert projected["prior_gaps_not_evidence"] == ["缺少梯队分布", "缺少成交额"]


def test_native_resource_discovery_is_not_a_foreign_mcp_server():
    import os
    import subprocess
    import sys
    import threading
    from review_agent.runtime import consume_events
    def run(tool, args, server="codex"):
        events = [{"type": "item.started", "item": {"type": "mcp_tool_call", "server": server, "tool": tool, "arguments": args}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
                  {"type": "turn.completed"}]
        program = "import json; events=" + repr(events) + "; [print(json.dumps(e),flush=True) for e in events]"
        proc = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE,
                                start_new_session=os.name == "posix", bufsize=0)
        return consume_events(proc, threading.Event(), lambda m: None, 3)
    assert run("list_mcp_resources", {}) == "answer"
    assert run("list_mcp_resource_templates", {"server": "astock"}) == "answer"
    assert run("read_mcp_resource", {"server": "astock", "uri": "missing"}) == "answer"
    for tool in ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"):
        assert run(tool, {"server": "astock", "uri": "missing"}, "astock") == "answer"
        with pytest.raises(EvidenceError, match="未开放"):
            run(tool, {"server": "foreign", "uri": "secret"}, "astock")
        with pytest.raises(EvidenceError, match="未开放"):
            run(tool, {"server": "astock", "uri": "missing"}, "foreign")
    for tool, args in [("list_mcp_resources", {"server": "foreign"}),
                       ("read_mcp_resource", {"server": "foreign", "uri": "secret"}),
                       ("read_mcp_resource", {}), ("unregistered", {})]:
        with pytest.raises(EvidenceError, match="未开放"):
            run(tool, args)


def test_evaluation_rejects_correct_metric_with_wrong_comparison_dates():
    from scripts.evaluate_review_agent import score
    case={'calculations':['limit_up_count'],'expected_first_date':'2026-07-28','expected_last_date':'2026-07-29'}
    result={'status':'incomplete','gaps':['资料局限'],'evidence':[{'kind':'calculation','metric':'limit_up_count',
            'first_date':'2026-07-27','date':'2026-07-29'}]}
    assert not score(case,result,None)
    result['evidence'][0]['first_date']='2026-07-28'
    assert score(case,result,None)


def test_missing_date_eval_still_requires_available_market_evidence():
    from scripts.evaluate_review_agent import score
    case = {"status": "incomplete", "required_market_evidence": {"metric": "limit_up_count", "date": "2026-07-29"}}
    item = {"kind": "sector", "metric": "sector_limit_up", "date": "2026-07-29", "available": True}
    result = {"status": "incomplete", "evidence": [item]}
    assert not score(case, result, None)
    item.update(kind="metric", metric="limit_up_count")
    assert score(case, result, None)
    item["available"] = False
    assert not score(case, result, None)


def test_evaluation_stops_on_provider_rate_limit(tmp_path):
    from scripts.evaluate_review_agent import evaluate
    class Limited:
        calls = 0
        def run(self, *args):
            self.calls += 1
            raise EvidenceError("AI 请求限流，请稍后重试；此前成功结果已保留")
    runtime = Limited()
    report = {"cases": []}
    evaluate({"cases": [{"name": "first", "question": "a"}, {"name": "next", "question": "b"}]},
             runtime, {"turns": []}, "", report, tmp_path / "report.json")
    assert runtime.calls == 1
    assert report["cases"][1]["status"] == "not_run"

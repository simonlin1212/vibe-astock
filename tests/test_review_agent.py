"""Behavioral contracts for the bounded review Agent (offline)."""
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("provider", ["codebuddy", "claude"])
def test_direct_chat_sends_statistics_contract_to_subscription_bridge(tmp_path, monkeypatch, provider):
    import threading
    from review_agent.runtime import Runtime
    from review_agent import subscription_bridge
    from review_agent.product_policy import ORDINARY_STATISTICS_POLICY
    captured = []
    def invoke(runtime, run, source, prompt, system, cancel, progress, budget, *, tools):
        captured.append((system, tools))
        return "有效样本口径需要披露覆盖率。"
    monkeypatch.setattr(subscription_bridge, "invoke", invoke)
    runtime = Runtime(tmp_path)
    source = {"provider": provider, "model": "default", "baseURL": ""}
    result = runtime.run({"source": source, "bundle": {"context": {"mode": "direct"}}, "turns": []},
                         "没有行情怎么处理？", "", "statistics-chat", threading.Event(), lambda _: None)
    assert result["status"] == "complete"
    assert ORDINARY_STATISTICS_POLICY in captured[0][0]
    assert captured[0][1] == ()

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
    # 本地回环/内网 http 放行(自托管模型网关 cc-switch 127.0.0.1:15721、局域网 vLLM 192.168.x:8000 无 TLS)
    for url in ("http://127.0.0.1/v1", "http://127.0.0.1:15721/v1", "http://192.168.250.10:8000/v1", "http://localhost/v1"):
        src, _ = connection({"model": "x", "apiKey": key, "baseURL": url})
        assert src["baseURL"] == url.rstrip("/"), url
    # 公网地址仍强制 https 标准端口 + 云白名单;带凭据/伪装域名一律拒(SSRF 边界不松)
    for url in ("http://api.openai.com/v1", "http://api.openai.com:8080/v1",
                "https://api.openai.com.evil.invalid/v1", "https://user:pass@api.openai.com/v1",
                "https://example.com:8443/v1"):
        with pytest.raises(EvidenceError):
            connection({"model": "x", "apiKey": key, "baseURL": url})


def test_cloud_metadata_endpoints_stay_blocked_after_allowing_private_http():
    """放行内网 http 之后，云厂商实例元数据地址必须仍然拒绝。

    169.254.169.254 是 AWS/GCP/Azure 的实例元数据服务，读到它等于拿到实例角色凭据。
    坑在于 `ipaddress` 把 169.254.0.0/16 也算作 `is_private`，所以"放行内网"这一步
    会顺手把元数据地址放进来 —— 放行侧必须是显式网段白名单，这条测试钉住白名单的边界。
    同一个地址有多种写法能绕开按属性做的判定，而且不同 CPython 版本漏的写法还不一样：
    3.9.6 上 ::ffff:169.254.169.254 的 `is_link_local` 为 False（漏），
    3.12.13 上 2002:a9fe:a9fe:: 的 `is_private` 为 True、`is_link_local` 为 False（漏）。
    两种写法内嵌的都是同一个元数据地址，所以两种都要钉住。
    阿里云的 100.100.100.200 走 CGNAT(100.64.0.0/10)，同样不在白名单内。
    """
    from review_agent.runtime import EvidenceError, connection
    key = "k" * 32

    blocked = {
        "http://169.254.169.254/latest/meta-data/": "AWS/GCP/Azure 实例元数据",
        "http://169.254.169.254:80/v1": "同上，带显式端口",
        "http://[fe80::1]:8000/v1": "IPv6 链路本地",
        "http://100.100.100.200/latest/meta-data/": "阿里云实例元数据(CGNAT 段)",
        "http://[::ffff:169.254.169.254]/latest/meta-data/": "IPv4-mapped 写法的元数据地址",
        "http://[::ffff:a9fe:a9fe]/v1": "同上，十六进制写法",
        "http://[2002:a9fe:a9fe::1]/v1": "6to4 写法，内嵌 169.254.169.254",
        "http://0.0.0.1:15721/v1": "0.0.0.0/8 里除未指定地址以外的部分",
        "http://240.0.0.1/v1": "240.0.0.0/4 保留段",
        "http://203.0.113.9/v1": "TEST-NET-3 文档段",
        "http://198.18.0.1/v1": "198.18.0.0/15 基准测试段",
    }
    for url, why in blocked.items():
        with pytest.raises(EvidenceError):
            connection({"model": "x", "apiKey": key, "baseURL": url})

    # 阴性对照：正常内网网关必须仍然放行。没有这一半的话，connection() 整体坏掉
    # (任何地址都抛)也会让上面那些全过 —— 那是"命令坏了"，不是"边界守住了"。
    # 白名单里的每一段都要有一条，否则删掉其中一段不会让任何测试变红。
    for url in (
        "http://127.0.0.1:15721/v1",        # 127.0.0.0/8
        "http://127.5.5.5:8000/v1",         # 同上，不是 127.0.0.1 那个字面量
        "http://10.0.0.1:8000/v1",          # 10.0.0.0/8
        "http://172.20.1.1:8000/v1",        # 172.16.0.0/12
        "http://192.168.250.10:8000/v1",    # 192.168.0.0/16
        "http://[::1]:15721/v1",            # ::1/128
        "http://[0:0:0:0:0:0:0:1]:15721/v1",  # 同上，展开写法不在调用处的字面量里
        "http://[fd00::1]:8000/v1",         # fc00::/7 唯一本地地址
    ):
        src, _ = connection({"model": "x", "apiKey": key, "baseURL": url})
        assert src["baseURL"] == url.rstrip("/"), url

    # 未指定地址 0.0.0.0/32 单独一条：放行，但**存下来的是 127.0.0.1**。
    # 存原样会把两个坑留给用户 —— 它不在系统代理的默认排除名单里（实测
    # proxy_bypass("127.0.0.1")=True、("0.0.0.0")=False，设了 HTTP_PROXY 时
    # Authorization 会发往代理），且 Windows 的 connect() 不接受 INADDR_ANY 作为
    # 目的地址。归一化不损失可达目标：本机实测两者作为目的地址等价。
    for url, want in (
        ("http://0.0.0.0:8000/v1", "http://127.0.0.1:8000/v1"),
        ("http://0.0.0.0/v1", "http://127.0.0.1/v1"),
    ):
        src, _ = connection({"model": "x", "apiKey": key, "baseURL": url})
        assert src["baseURL"] == want, (url, src["baseURL"])


def test_malformed_port_is_a_rejection_not_a_crash():
    """端口写错要得到 EvidenceError(前端显示成一句话)，不能抛 ValueError 变成 500。

    `urlparse(...).port` 对越界或非数字端口抛 ValueError，而调用处只捕获 EvidenceError。
    公网与内网两个分支都要覆盖：内网分支的 valid 表达式根本不读 port，端口合法性
    只能靠"读一次 port"这个动作本身来保证，两边因此必须共用同一次读取。
    """
    from review_agent.runtime import EvidenceError, connection
    key = "k" * 32

    # 公网两条必须带 provider="api-compatible"：不带的话 base 不在 allowed 名单里，
    # 无论端口写成什么都会被拒 —— 那样这两条就是空转，端口规则删掉也不会红。
    for url in (
        "https://example.com:99999/v1",   # 公网，端口越界
        "https://example.com:abc/v1",     # 公网，端口非数字
        "http://127.0.0.1:99999/v1",      # 回环，端口越界
        "http://127.0.0.1:abc/v1",        # 回环，端口非数字
        "http://127.0.0.1:169.254.169.254/v1",  # 把元数据地址塞进端口位
    ):
        with pytest.raises(EvidenceError) as caught:
            connection({"model": "x", "apiKey": key, "baseURL": url, "provider": "api-compatible"})
        # 文案必须指向端口。共用那句"地址须为 https(公网)或 http(仅本地回环/内网)"时，
        # 用户改半天协议也改不对，因为错的根本不是协议。
        assert "端口" in str(caught.value), (url, str(caught.value))

    # 阴性对照：合法端口必须仍然放行，否则上面五条可能只是"端口一律拒绝"。
    for url in ("https://example.com:443/v1", "http://127.0.0.1:15721/v1"):
        src, _ = connection({"model": "x", "apiKey": key, "baseURL": url, "provider": "api-compatible"})
        assert src["baseURL"] == url.rstrip("/"), url


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

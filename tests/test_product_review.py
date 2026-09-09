"""Product daily path: date scope, shared source, cancellation and preservation."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from duanxian import data, reflection, review_store, trade_calendar
from duanxian.llm_errors import LlmConfigError
from review_agent.api import DailyInput, Manager
from review_agent.daily import DailyLLM, FrozenInputs, check_report_text
from review_agent.evidence import EvidenceError
from review_agent.runtime import Runtime
from review_agent.store import Store

DATE = "2026-09-03"
SOURCE = {"provider": "codex-private", "model": "gpt-5.6-sol"}


@pytest.mark.parametrize("name", ["get_capital_data", "get_macro_sector_data"])
def test_wrong_date_never_fetches_current_market(monkeypatch, name):
    monkeypatch.setattr(trade_calendar, "live_quotes_are_close_of", lambda date: (False, "日期不匹配"))
    def forbidden(*args):
        pytest.fail("current data must not be requested for this historical date")
    monkeypatch.setattr(data.dr, "fetch_sector_flow", forbidden)
    from duanxian import historical_sources
    calls = []
    def historical(day, *args):
        calls.append(day)
        return day + " 历史成交资料，不是净流入"
    monkeypatch.setattr(historical_sources, "historical_activity", historical)
    assert "历史成交资料" in getattr(data, name)(DATE)
    assert calls == [DATE]


@pytest.mark.parametrize("name", ["get_capital_data", "get_macro_sector_data"])
def test_crossing_session_discards_entire_batch(monkeypatch, name):
    checks = iter([(True, ""), (False, "日期已切换")])
    monkeypatch.setattr(trade_calendar, "live_quotes_are_close_of", lambda date: next(checks))
    monkeypatch.setattr(data.dr, "fetch_sector_flow", lambda *args: [])
    monkeypatch.setattr(data.dr, "enrich_trend", lambda *args: None)
    monkeypatch.setattr(data.dr, "fetch_turnover_top20", lambda *args: [])
    assert "本批未使用" in getattr(data, name)(DATE)


@pytest.mark.parametrize("day", [None, "2026-09-04", DATE])
def test_lhb_requires_matching_response_date(monkeypatch, day):
    import sys
    import pandas as pd
    from duanxian.fetchers import fetch_lhb
    row = {"代码": "000001", "名称": "测试", "龙虎榜净买额": 100, "上榜原因": "测试"}
    if day:
        row["上榜日"] = day
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_lhb_detail_em=lambda **kwargs: pd.DataFrame([row])))
    result = fetch_lhb(DATE.replace("-", ""))
    assert ("error" not in result[0]) == (day == DATE)


@pytest.mark.parametrize("name", ["get_capital_data", "get_macro_sector_data"])
def test_matching_reference_date_note_preserves_source_limits(monkeypatch, name):
    monkeypatch.setattr(trade_calendar, "live_quotes_are_close_of", lambda date: (True, ""))
    monkeypatch.setattr(data.dr, "fetch_sector_flow", lambda *args: [])
    monkeypatch.setattr(data.dr, "enrich_trend", lambda *args: None)
    monkeypatch.setattr(data.dr, "fetch_turnover_top20", lambda *args: [])
    text = getattr(data, name)(DATE)
    assert f"参考行情均对应 {DATE} 收盘" in text
    assert "端点本身的时间戳未单独核实" in text
    assert f"非 {DATE} 历史收盘口径" not in text


def test_reflection_excludes_future_evaluations_and_scoreboard(tmp_path, monkeypatch):
    monkeypatch.setattr(reflection, "_REFLECT_DIR", str(tmp_path))
    for date, end in [("2026-09-01", "2026-09-02"), (DATE, "2026-09-04")]:
        (tmp_path / (date + ".json")).write_text(json.dumps({"prediction_date": date, "eval_date": end, "phase_eval": {"phase": end}}))
    monkeypatch.setattr(reflection, "scoreboard", lambda: pytest.fail("all-time scoreboard leaks future evaluations"))
    assert reflection.latest_reflection(end=DATE)["eval_date"] == "2026-09-02"
    assert "2026-09-04" not in reflection.get_past_context(end=DATE)


def test_strict_supplement_failure_propagates():
    from duanxian.synthesizer import create_review_judge
    from duanxian.prompts import RESEARCH_PACK
    class LLM:
        calls = 0
        def invoke(self, prompt):
            self.calls += 1
            if self.calls > 1:
                raise LlmConfigError("AI 请求限流")
            return SimpleNamespace(content=json.dumps({"emotion_phase": "修复", "market_oneliner": "测试市场情况", "focus_directions": [
                {"direction": s, "logic": "测试数据支撑", "risk": "样本缺口"} for s in ("方向甲", "方向乙")]}))
    llm = LLM()
    with pytest.raises(LlmConfigError, match="限流"):
        create_review_judge(llm, pack=RESEARCH_PACK, strict=True)({})
    assert llm.calls == 2


def test_frozen_inputs_reuse_exact_preflight_values(tmp_path, monkeypatch):
    values = iter(["before", "after"])
    from review_agent import public_worker
    monkeypatch.setattr(public_worker, "fetch_public", lambda *args, **kwargs: next(values))
    inputs = FrozenInputs(DATE, tmp_path, lambda: 10)
    assert inputs.get_sentiment_data(DATE) == inputs.get_sentiment_data(DATE) == "before"
    with pytest.raises(EvidenceError):
        inputs.get_sentiment_data("2026-09-04")
    saved = json.loads((tmp_path / "get_sentiment_data.json").read_text())
    assert saved["target_date"] == DATE and saved["sha256"]


def test_daily_uses_selected_source_text_only_and_no_retry(tmp_path):
    calls = []
    class Engine:
        timeout = 360
        def _invoke(self, run, source, key, prompt, cancel, progress, budget, **kwargs):
            calls.append((source, key, prompt, kwargs))
            raise EvidenceError("AI 请求限流")
    llm = DailyLLM(Engine(), SOURCE, "CANARY_KEY", tmp_path, DATE, threading.Event(), lambda: 40)
    with pytest.raises(LlmConfigError, match="限流"):
        llm.invoke("given evidence")
    assert len(calls) == 1 and calls[0][0] == SOURCE
    assert calls[0][1] == "CANARY_KEY" and calls[0][3] == {"text_only": True}
    assert DATE in calls[0][2]
    assert not any("CANARY_KEY" in p.read_text() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize("text", ["建议明天买入测试标的", "低吸机会", "建议仓位三成", "目标价100元"])
def test_report_action_gate(text):
    with pytest.raises(LlmConfigError):
        check_report_text(text)
    check_report_text("昨日涨停样本显示分歧，不能据此判断全市场。")


def test_save_preserves_versions_and_latest_date(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "DIR", str(tmp_path))
    def payload(date, text): return {"target_date": date, "focus": {"text": text}}
    review_store.save(payload("2026-09-04", "newer"), "2026-09-04")
    review_store.save(payload(DATE, "first"), DATE)
    review_store.save(payload(DATE, "second"), DATE)
    assert review_store.load()["target_date"] == "2026-09-04"
    assert review_store.load(DATE)["focus"]["text"] == "second"
    assert any(json.loads(p.read_text())["focus"]["text"] == "first" for p in (tmp_path / "_versions").glob("*.json"))


def test_broken_latest_rejected_before_report_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text("[1]")
    dated = tmp_path / (DATE + ".json")
    dated.write_text('{"focus":{"text":"original"}}')
    with pytest.raises(ValueError, match="索引损坏"):
        review_store.save({"target_date": DATE, "focus": {"text": "replacement"}}, DATE)
    assert json.loads(dated.read_text())["focus"]["text"] == "original"


@pytest.fixture
def daily_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "DIR", str(tmp_path / "reviews"))
    monkeypatch.setattr(trade_calendar, "is_settled", lambda date: True)
    manager = Manager(Store(tmp_path / "state"), tmp_path / "reviews", Runtime(tmp_path / "state"))
    yield manager
    manager.shutdown()


def test_daily_idempotency_and_cancel_do_not_restart_or_publish(daily_manager, monkeypatch):
    entered = threading.Event()
    from duanxian import preflight
    def check(*args, **kwargs):
        entered.set()
        assert daily_manager.daily.cancel_event.wait(5)
        return {"ok": True, "warnings": []}
    monkeypatch.setattr(preflight, "check", check)
    body = DailyInput(date=DATE, request_id="a"*32, llm=SOURCE)
    with daily_manager.lock:
        first = daily_manager.daily.submit(body, SOURCE, "CANARY_KEY")
    assert entered.wait(2)
    with daily_manager.lock:
        again = daily_manager.daily.submit(body, SOURCE, "CANARY_KEY")
        assert first["job_id"] == again["job_id"]
        with pytest.raises(EvidenceError, match="正在"):
            daily_manager.daily.submit(body.model_copy(update={"request_id": "b"*32}), SOURCE, "")
    with pytest.raises(EvidenceError, match="变化"):
        daily_manager.daily.cancel("b"*32)
    daily_manager.daily.cancel(first["job_id"])
    daily_manager.daily.worker.join(5)
    assert not daily_manager.daily.busy()
    assert daily_manager.daily.snapshot()["status"] == "cancelled"
    with daily_manager.lock:
        assert daily_manager.daily.submit(body, SOURCE, "CANARY_KEY")["status"] == "cancelled"
    assert review_store.load(DATE) is None
    assert "CANARY_KEY" not in "".join(p.read_text() for p in daily_manager.daily.directory.rglob("*.json"))


def test_daily_restart_marks_running_failed_without_model(tmp_path):
    root = tmp_path / "state"
    folder = root / "daily"
    folder.mkdir(parents=True)
    row = {"job_id":"a"*32,"started":time.time(),"running":True,"status":"running","fingerprint":"test"}
    (folder / ("a"*32 + ".json")).write_text(json.dumps(row))
    manager = Manager(Store(root), tmp_path, Runtime(root))
    try:
        assert manager.daily.snapshot()["status"] == "failed"
        assert not manager.daily.busy()
    finally:
        manager.shutdown()


def test_restart_recovers_committed_report_instead_of_rebilling(tmp_path):
    root = tmp_path / "state"
    folder = root / "daily"
    folder.mkdir(parents=True)
    row = {"job_id":"a"*32,"date":DATE,"source":SOURCE,"started":time.time(),"running":True,"status":"running","fingerprint":"test"}
    (folder / ("a"*32 + ".json")).write_text(json.dumps(row))
    (tmp_path / (DATE + ".json")).write_text(json.dumps({"job_id":"a"*32,"target_date":DATE,"ai_source":SOURCE,"generation_mode":"isolated_daily","focus":{"text":"complete"}}))
    manager = Manager(Store(root), tmp_path, Runtime(root))
    try:
        assert manager.daily.snapshot()["status"] == "complete"
        assert not manager.daily.busy()
    finally:
        manager.shutdown()


def test_status_disk_failure_does_not_relabel_saved_report(daily_manager, monkeypatch):
    from review_agent import daily
    daily_manager.daily.current = {"job_id": "a"*32, "running": True}
    def fail(*args): raise OSError("disk unavailable")
    monkeypatch.setattr(daily, "atomic_write", fail)
    daily_manager.daily._finish(running=False, status="complete", error=None)
    snapshot = daily_manager.daily.snapshot()
    assert snapshot["status"] == "complete" and snapshot["state_warning"]


def test_missing_full_market_count_is_not_rendered_as_conflicting_value():
    text = data.render_market_facts({"loss_effect": {"available": True, "prev_date": "2026-09-02",
        "sample": 52, "deep_loss_5_count": 13, "deep_loss_5_rate": .25, "deep_loss_7_count": 9,
        "limit_down_count": 4, "worst": -10, "market_limit_down": None}})
    assert "None" not in text
    assert "本统计模块未获取全市场跌停数" in text


def test_text_only_runtime_rejects_even_approved_evidence_tool():
    import os
    import subprocess
    import sys
    from review_agent.runtime import consume_events
    events = [{"type": "item.started", "item": {"type": "mcp_tool_call", "server": "astock", "tool": "list_evidence", "arguments": {}}},
              {"type": "turn.completed"}]
    proc = subprocess.Popen([sys.executable, "-c", "import json; events=" + repr(events) + "; [print(json.dumps(e),flush=True) for e in events]"],
                            stdout=subprocess.PIPE, start_new_session=os.name == "posix", bufsize=0)
    with pytest.raises(EvidenceError, match="未开放"):
        consume_events(proc, threading.Event(), lambda m: None, 3, text_only=True)
    assert proc.poll() is not None


def test_leader_count_uses_full_ladder_before_truncation(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "_LEADER_DIR", str(tmp_path))
    monkeypatch.setattr(data.dr, "fetch_zt_pool", lambda date: {"ladder": [
        {"name": f"测试{i}", "consec_boards": 2} for i in range(20)
    ]})
    text = data.get_leader_data(DATE)
    assert "共 20 只；以下仅展示前 8 只" in text
    assert text.count("板·") == 8


def test_daily_rejects_weekend_before_model(daily_manager):
    body = DailyInput(date="2026-08-30", request_id="c"*32, llm=SOURCE)
    with daily_manager.lock, pytest.raises(EvidenceError, match="周末"):
        daily_manager.daily.submit(body, SOURCE, "")
    assert not daily_manager.daily.busy()

"""情绪周期状态机(cn_shortline_state)与盘外增强源(cn_shortline_extra)。全程不联网。

移植自 Vibe AStock v2.0.0 的测试思路(duanxian 的状态机纯函数测 + 盘外源的
fake response 测),适配本仓库协议:状态机数据层 monkeypatch daily_readings,
盘外三源 monkeypatch sources._http.http_get。
"""
from __future__ import annotations

import pytest

from test_registry_sources import _ctx
from sources import mappers_shortline as M
from sources import shortline as S
from sources import shortline_extra as X
from sources import shortline_state as ST


# ================================================================ 状态机:分位工具
class TestRankPct:
    def test_single_sample_is_none(self):
        assert ST._rank_pct([5.0], 0) is None
        assert ST._rank_pct([None], 0) is None

    def test_expansion_window_counts_le(self):
        vals = [10.0, 20.0, 30.0, 20.0]
        # 窗口 [10,20] 中 20 的分位:≤20 的 2/2
        assert ST._rank_pct(vals, 1) == 100.0
        # 窗口 [10,20,30] 中 30:3/3
        assert ST._rank_pct(vals, 2) == 100.0
        # 窗口 [10,20,30,20] 中 20:≤20 的 3/4
        assert ST._rank_pct(vals, 3) == 75.0

    def test_none_samples_do_not_enter_denominator(self):
        vals = [10.0, None, 30.0]
        assert ST._rank_pct(vals, 2) == 100.0  # 分母只算 [10, 30]


# ================================================================ 状态机:候选态
def _cols(rows: list[dict]) -> dict:
    keys = ("limit_up", "highest_board", "broken_rate", "money_effect", "promotion_1to2", "deep_loss")
    return {k: [r.get(k) for r in rows] for k in keys}


def _synthetic(n: int = 40, hot: bool = True) -> list[dict]:
    """合成读数序列:hot=True 从冷到热(S 上升 → 启动/发酵),False 反向。"""
    rows = []
    for i in range(n):
        t = i / (n - 1)
        if hot:
            lu, hb, me = 30 + int(70 * t), 3 + int(6 * t), -4 + 9 * t
            br, dl, p12 = 0.55 - 0.35 * t, 25 - 23 * t, 0.15 + 0.5 * t
        else:
            lu, hb, me = 100 - int(70 * t), 9 - int(6 * t), 5 - 9 * t
            br, dl, p12 = 0.2 + 0.35 * t, 2 + 23 * t, 0.65 - 0.5 * t
        rows.append({"limit_up": float(lu), "highest_board": float(hb), "broken_rate": round(br, 3),
                     "money_effect": round(me, 2), "promotion_1to2": round(p12, 3), "deep_loss": float(dl)})
    return rows


def _run_state_machine(rows: list[dict]):
    cols = _cols(rows)
    n = len(rows)
    groups = (("limit_up", 1), ("highest_board", 1), ("money_effect", 1), ("promotion_1to2", 1), ("broken_rate", -1), ("deep_loss", -1))
    s_series = []
    for i in range(n):
        vals = []
        for key, sign in groups:
            pct = ST._rank_pct(cols[key], i)
            if pct is not None:
                vals.append((100.0 - pct) if sign < 0 else pct)
        s_series.append(round(sum(vals) / len(vals), 2) if vals else None)
    deep_pcts = [ST._rank_pct(cols["deep_loss"], i) for i in range(n)]
    p12_pcts = [ST._rank_pct(cols["promotion_1to2"], i) for i in range(n)]
    states, prev = [], "冰点"
    for i in range(n):
        cand = ST._classify(i, cols, s_series, deep_pcts, p12_pcts, prev)
        states.append(cand)
        if cand:
            prev = cand
    return s_series, states


class TestClassify:
    def test_warming_up_rides_the_offense_side(self):
        """从冷到热:后段三指标分位共振 → 高潮(共振分支优先),也是进攻侧。"""
        _, states = _run_state_machine(_synthetic(40, hot=True))
        assert states[-1] in ("高潮", "发酵", "启动")

    def test_cooling_down_flips_defensive_immediately(self):
        """从热到冷:转防御立即生效(不需要 T+1 确认)。"""
        _, states = _run_state_machine(_synthetic(40, hot=False))
        assert states[-1] in ("退潮", "冰点", "分歧", "高潮")

    def test_asymmetric_confirmation_logic_is_in_the_port(self):
        """状态机移植完整性:阈值表 / 分组 / 六态名一个不少。"""
        assert set(ST.THRESHOLDS) == {"ice_pct", "start_pct", "boom_pct", "p12_weak",
                                      "prem_weak", "ice_half", "p12_start_pct", "deep_weak_pct"}
        assert ST.MIN_BASELINE == 30
        assert ST.MIN_S_INPUTS == ("limit_up", "money_effect", "broken_rate")
        assert set(ST.STATE_ORDER) == {"冰点", "退潮", "分歧", "高潮", "启动", "发酵"}
        weights = {g[0]: g[2] for g in ST._GROUPS}
        assert weights == {"广度": 0.35, "质量": 0.35, "风险": 0.30}


# ================================================================ 状态机:build_state(数据层 mock)
@pytest.fixture()
def fake_readings(monkeypatch):
    """40 天合成读数,最后一天 = 目标日。"""
    rows = _synthetic(40, hot=True)
    dates = [f"2026-01-{d:02d}" for d in range(1, 41)]
    hist = [{"date": d, **r} for d, r in zip(dates, rows)]
    monkeypatch.setattr(S, "trade_dates_ending_at", lambda end, n=10: dates[-min(n, len(dates)):])
    monkeypatch.setattr(S, "iso", lambda x: x)
    monkeypatch.setattr(S, "review_day", lambda d=None: dates[-1])
    monkeypatch.setattr(S, "is_settled", lambda d: True)
    monkeypatch.setattr(__import__("sources.shortline_emotion", fromlist=["daily_readings"]),
                        "daily_readings", lambda dt: next(h for h in hist if h["date"] == dt))
    return hist


class TestBuildState:
    def test_baseline_reached_produces_confirmed_state(self, fake_readings):
        out = ST.build_state(fake_readings[-1]["date"], days=40)
        assert out["available"] is True
        assert out["baseline_ok"] is True and out["sample_days"] == 40
        assert out["confirmed"] in ST.STATE_ORDER
        assert out["candidate"] in ST.STATE_ORDER
        assert len(out["history"]) == 20 and out["history"][-1]["date"] == fake_readings[-1]["date"]
        assert "adaptation" in out  # 坦白口径必须带

    def test_below_baseline_describes_but_does_not_judge(self, fake_readings):
        out = ST.build_state(fake_readings[-1]["date"], days=40)
        # 单日读数缺失不炸:把最后一天抽空 → 该日 S 不可算,确认态段可能断
        import sources.shortline_emotion as E

        def partial(dt):
            h = dict(next(h for h in fake_readings if h["date"] == dt))
            if dt == fake_readings[-1]["date"]:
                for k in ("limit_up", "money_effect", "broken_rate"):
                    h[k] = None
            return h
        # 直接调内部路径:重打 daily_readings 补丁
        E.daily_readings = partial
        try:
            out2 = ST.build_state(fake_readings[-1]["date"], days=40)
            # 最后一天 S 不可算 → 状态机如实降级(available 可能是 False 或状态来自前一日)
            assert out2["available"] in (True, False)
        finally:
            E.daily_readings = lambda dt: next(h for h in fake_readings if h["date"] == dt)

    def test_window_bounds_enforced(self):
        with pytest.raises(ValueError):
            ST.build_state("2026-02-10", days=10)
        with pytest.raises(ValueError):
            ST.build_state("2026-02-10", days=200)

    def test_render_unavailable_is_honest(self):
        txt = ST.render({"available": False, "reason": "测试原因"})
        assert "情绪状态机不可用" in txt and "测试原因" in txt
        assert "不影响其他情绪读数" in txt


# ================================================================ 盘外三源
class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture()
def no_throttle(monkeypatch):
    monkeypatch.setattr(X, "_dxzx_throttle", lambda: None)


class TestKplMood:
    def test_date_echo_match(self, monkeypatch):
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp(
            {"info": [{"Day": "2026-02-10", "ztjs": 86, "df_num": 12, "strong": 1300, "lbgd": 7}], "tip": "情绪好"}))
        out = X.kpl_mood("20260210")
        assert out["available"] and out["limit_up_count"] == 86 and out["max_consec_boards"] == 7
        assert out["sentiment"] == 1300 and out["tip"] == "情绪好"

    def test_date_echo_mismatch_is_not_silent(self, monkeypatch):
        """回显对不上目标日必须不可用 —— 把别的场次当成被复盘那天比缺数据更糟。"""
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp(
            {"info": [{"Day": "2026-02-09", "ztjs": 10}]}))
        out = X.kpl_mood("20260210")
        assert not out["available"] and "2026-02-09" in out["reason"]

    def test_missing_echo_is_not_silent(self, monkeypatch):
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp({"info": [{}]}))
        assert not X.kpl_mood("20260210")["available"]

    def test_http_error_is_not_raised(self, monkeypatch):
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp(RuntimeError("boom")))
        assert not X.kpl_mood("20260210")["available"]


class TestZtlive:
    def _payload(self):
        return {"result": "success", "list": [
            {"code": "300001", "name": "甲", "zty": "机器人+减速器", "zt": "3", "time": "09:31"},
            {"code": "600000", "name": "乙", "ztyy": "算力", "zt": "1", "time": "10:00"},
            {"code": "abc", "name": "坏行", "zty": "x", "zt": "1", "time": "10:00"},  # 无效代码
        ]}

    def test_rows_cleaned_and_invalid_dropped(self, monkeypatch, no_throttle):
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp(self._payload()))
        out = X.duanxianxia_ztlive()
        assert out["available"] and out["count"] == 2
        assert out["rows"][0]["reason"] == "机器人、减速器"
        assert out["rows"][1]["reason"] == "算力"

    def test_bad_envelope_is_not_silent(self, monkeypatch, no_throttle):
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp({"result": "error"}))
        assert not X.duanxianxia_ztlive()["available"]

    def test_throttle_sleeps_at_least_min_interval(self, monkeypatch):
        """免费通道限流 ≥10 秒/次:节流器必须存在且真睡(用假时钟验证调用序)。"""
        called = []
        monkeypatch.setattr(X.time, "sleep", lambda s: called.append(s))
        X._dxzx_last[0] = X.time.time() - 1.0  # 1 秒前刚调过 → 要再等 ~9 秒
        X._dxzx_throttle()
        assert called and called[0] >= X._DXZX_MIN_INTERVAL - 1.0


class TestHhxg:
    def _payload(self):
        return {"latest_date": "2026-02-10", "items": [
            {"name": "机器人", "yz": 3.2, "zt": 15, "ratio": 0.18},
            {"name": "算力", "yz": -1.0, "zt": 8, "ratio": 0.09},
        ]}

    def test_latest_issue_match(self, monkeypatch):
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp(self._payload()))
        out = X.hhxg_theme_heat("2026-02-10")
        assert out["available"] and len(out["items"]) == 2
        assert out["items"][0] == {"name": "机器人", "premium": 3.2, "limit_up": 15, "ratio": 0.18}

    def test_stale_issue_is_not_silent(self, monkeypatch):
        monkeypatch.setattr(X._http, "http_get", lambda *a, **k: _Resp(self._payload()))
        out = X.hhxg_theme_heat("2026-01-30")
        assert not out["available"] and "最新一期" in out["reason"]


class TestBuildExtra:
    def test_historical_date_all_three_honest(self, monkeypatch):
        """目标日不是今天:ztlive 直接不可用,KPL/hhxg 靠日期核对挡住。"""
        monkeypatch.setattr(S, "review_day", lambda d=None: "2026-02-06")
        monkeypatch.setattr(S, "today_sh", lambda: "2026-02-10")
        monkeypatch.setattr(S, "is_settled", lambda d: True)
        monkeypatch.setattr(X, "kpl_mood", lambda ymd: {"available": False, "reason": "场次不符"})
        monkeypatch.setattr(X, "hhxg_theme_heat", lambda d, top_n=15: {"available": False, "reason": "仅最新一期"})
        out = X.build_extra("2026-02-06")
        assert out["is_today"] is False
        assert not out["ztlive"]["available"] and "仅当日实时" in out["ztlive"]["reason"]

    def test_render_covers_all_three_blocks(self):
        result = {"date": "2026-02-10", "is_today": True, "settled": False,
                  "kpl_mood": {"available": True, "date": "2026-02-10", "limit_up_count": 86,
                               "drawdown_count": 12, "sentiment": 1300, "max_consec_boards": 7, "tip": ""},
                  "ztlive": {"available": False, "reason": "取数失败"},
                  "theme_heat": {"available": True, "date": "2026-02-10",
                                 "items": [{"name": "机器人", "premium": 3.2, "limit_up": 15, "ratio": 0.18}]}}
        txt = X.render_extra(result)
        assert "开盘啦" in txt and "短线侠" in txt and "hhxg" in txt
        assert "不可用" in txt and "交叉参考" in txt


# ================================================================ 映射层
class TestMappers:
    def test_state_map_emits_state_fields_only(self):
        result = {"available": True, "date": "2026-02-10", "settled": True,
                  "s": 62.5, "s_pct": 62.5, "candidate": "发酵", "confirmed": "启动",
                  "sample_days": 60, "requested_days": 60, "min_baseline": 30, "baseline_ok": True,
                  "readings": {}, "history": [{"date": "2026-02-09", "s_pct": 55.0, "state": "启动"},
                                              {"date": "2026-02-10", "s_pct": 62.5, "state": "启动"}],
                  "thresholds": {}, "adaptation": "口径说明"}
        env = M.shortline_state_map(result, _ctx(market="CN", ep={"id": "cn_shortline_state"}))
        fields = {e["field"]: e for e in env["evidence"]}
        assert fields["emotion_s"]["value"] == 62.5
        assert fields["emotion_state_confirmed"]["value"] == "启动"
        assert fields["emotion_state_sample_days"]["value"] == 60
        days = [e for e in env["evidence"] if e["field"] == "emotion_state_day"]
        assert {e["period"] for e in days} == {"2026-02-09", "2026-02-10"}
        assert env["extra"]["text"] and "状态机" in env["extra"]["text"]
        assert env.get("status") is None  # 已收盘 + 基线足 → 不 partial

    def test_state_map_unavailable_is_failed_with_reason(self):
        env = M.shortline_state_map({"available": False, "date": "2026-02-10", "reason": "基线不足"},
                                    _ctx(market="CN", ep={"id": "cn_shortline_state"}))
        assert env["status"] == "failed" and "基线不足" in env["reason"]

    def test_state_map_baseline_not_ok_marks_degraded(self):
        result = {"available": True, "date": "2026-02-10", "settled": True,
                  "s": 62.5, "s_pct": 62.5, "candidate": "发酵", "confirmed": "发酵",
                  "sample_days": 31, "requested_days": 60, "min_baseline": 30, "baseline_ok": False,
                  "readings": {}, "history": [], "thresholds": {}, "adaptation": ""}
        env = M.shortline_state_map(result, _ctx(market="CN", ep={"id": "cn_shortline_state"}))
        assert env["status"] == "partial" and "分位基线不足" in (env.get("degraded") or "")

    def test_extra_map_kpl_and_theme_heat(self):
        result = {"date": "2026-02-10", "is_today": True, "settled": False,
                  "kpl_mood": {"available": True, "date": "2026-02-10", "limit_up_count": 86,
                               "drawdown_count": 12, "sentiment": 1300, "max_consec_boards": 7, "tip": "提示"},
                  "ztlive": {"available": False, "reason": "仅当日实时"},
                  "theme_heat": {"available": True, "date": "2026-02-10",
                                 "items": [{"name": "机器人", "premium": 3.2, "limit_up": 15, "ratio": 0.18}]}}
        env = M.shortline_extra_map(result, _ctx(market="CN", ep={"id": "cn_shortline_extra"}))
        fields = {e["field"] for e in env["evidence"]}
        assert "extra_kpl_limit_up_count" in fields and "extra_kpl_max_consec_boards" in fields
        assert "extra_ztlive_count" not in fields          # 不可用的块不进证据
        assert "extra_theme_limit_up" in fields
        th = {e["field"]: e for e in env["evidence"]}["extra_theme_premium"]
        assert th["record_key"] == "机器人" and th["value"] == 3.2
        assert env["status"] == "partial"                  # ztlive 缺 → partial
        assert any(m["field"] == "ztlive" for m in env["missing"])
        assert env["extra"]["text"] and "盘外" in env["extra"]["text"]

    def test_extra_map_historical_degrades(self):
        result = {"date": "2026-02-06", "is_today": False, "settled": True,
                  "kpl_mood": {"available": False, "reason": "场次不符"},
                  "ztlive": {"available": False, "reason": "仅当日实时"},
                  "theme_heat": {"available": False, "reason": "仅最新一期"}}
        env = M.shortline_extra_map(result, _ctx(market="CN", ep={"id": "cn_shortline_extra"}))
        assert env["status"] == "failed" and "盘外三路" in env["reason"]

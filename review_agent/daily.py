"""Daily report jobs using the same isolated engine and connection as questions.

Host code freezes dated inputs. Models have no tools. Grounded prose is
qualitative; source quotes and deterministic numerical comparisons are host-owned.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .evidence import EvidenceError, canonical, digest
from .product_policy import has_trade_recommendation


def atomic_write(path, payload):
    target = Path(path)
    tmp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(canonical(payload), encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def check_report_text(text: str) -> None:
    """Reject explicit action/price recommendations; not a semantic proof."""
    from duanxian.llm_errors import LlmConfigError
    if has_trade_recommendation(text):
        raise LlmConfigError("复盘包含交易动作或点位建议，未保存；原报告已保留，请重试")
    if not text.strip():
        raise LlmConfigError("AI 返回空内容，未保存；请重试")


class FrozenInputs:
    def __init__(self, date: str, directory: Path, check, progress=lambda message: None):
        self.date, self.directory, self.check = date, directory, check
        self.values: dict = {}
        self.progress = progress

    def __getattr__(self, name):
        if not name.startswith("get_"):
            raise AttributeError(name)
        def read(date):
            self.check()
            if date != self.date:
                raise EvidenceError("取数日期超出本次复盘范围")
            if name not in self.values:
                from .public_worker import fetch_public
                from .grounding import SOURCES
                self.progress("正在取数：" + SOURCES.get(name, name))
                value = fetch_public(name, [date], self.directory, self.check, timeout=90)
                if name in {"get_emotion_metrics", "get_market_facts"}:
                    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or not isinstance(value[1], dict):
                        raise EvidenceError("复盘取数返回格式错误")
                    value = tuple(value)
                self.check()
                self.values[name] = value
                payload = {"target_date": date, "input": name, "value": value, "fetched_at": time.time()}
                payload["sha256"] = digest(payload)
                atomic_write(str(self.directory / f"{name}.json"), payload)
            return self.values[name]
        return read


def has_unsupported_flow_total(text):
    """Reject numeric flow totals; a caution without an amount is not a total.

    This is a narrow output guard, not proof of all financial reasoning.
    """
    for sentence in re.split(r"[。；;\n]", text):
        if (re.search(r"累计|合计|总计|汇总", sentence)
                and re.search(r"净买|净卖|流入|流出", sentence)
                and re.search(r"[+−-]?\d+(?:\.\d+)?\s*(?:亿|万|元)", sentence)):
            return True
    return False


class DailyLLM:
    def __init__(self, runtime, source, key, directory, date, cancel, check, purpose="daily"):
        self.runtime, self.source, self.key = runtime, source, key
        self.directory, self.date, self.cancel, self.check = directory, date, cancel, check
        self.purpose = purpose

    def invoke(self, prompt, _correction=False):
        from duanxian.llm_errors import LlmConfigError
        remaining = self.check()
        run = self.directory / uuid.uuid4().hex
        try:
            run.mkdir(mode=0o700)
            (run / ".vibe-astock-root").touch()
            prefix = f"复盘目标日：{self.date}。材料中的今日仅指该日。仅使用所给数据，缺口不得推断补齐。\n" if self.purpose == "daily" else "本次明确提供的材料如下；没有日期的材料不得擅自归到今天。\n"
            if self.purpose == "stock":
                prefix += ("后续分析问题只在证据充分时回答。没有覆盖某项数据，只能说明资料缺口，不能推断该现象未发生或风险较高。"
                           "资料不足时风险等级写无法判断，不强行分级；观测次数不等于交易日数。"
                           "龙虎榜单日榜与三日榜区间可能重叠，禁止跨记录累计净买卖额；上榜记录不代表全市场净流入，也不能推断主力意图。"
                           "直接报告事实、依据与缺口，不复述被禁止的交易措辞，也不添加参与倾向。\n")
            options = {} if self.purpose == "daily" else {"task_kind": self.purpose}
            text = self.runtime._invoke(run, self.source, self.key, prefix + prompt,
                self.cancel, lambda message: None, min(remaining, self.runtime.timeout), text_only=True, **options)
            self.check()
            # Daily JSON is checked field-by-field by request_checked, including
            # the action gate, so content errors can use its bounded correction.
            # Other callers consume prose directly and must still gate here.
            self.last_run = run
            if self.purpose != "daily":
                try:
                    check_report_text(text)
                    if self.purpose == "stock" and has_unsupported_flow_total(text):
                        raise LlmConfigError("龙虎榜统计区间可能重叠，不能计算或宣称跨记录资金合计")
                except LlmConfigError as exc:
                    self.record_validation(text, str(exc), self.purpose, int(_correction))
                    if self.purpose == "stock" and not _correction:
                        return self.invoke(prompt + "\n上次输出未通过：" + str(exc) + "。重新整理，逐条保留日期和单日/三日统计区间，只写定性差异和资料局限，省略所有资金金额。", True)
                    raise
            return SimpleNamespace(content=text)
        except EvidenceError as exc:
            # Existing analyst/JSON helpers propagate this class immediately,
            # instead of retrying paid calls or declaring a degraded success.
            raise LlmConfigError(str(exc)) from None
        except LlmConfigError:
            raise
        except Exception:
            raise LlmConfigError("所选 AI 运行异常，已停止；请检查接入与运行环境后重试") from None


    def record_validation(self, raw, reason, stage, attempt):
        """Private diagnostic only; never returned as market evidence."""
        if not hasattr(self, "last_run"):
            return
        reply = raw[:60000] if isinstance(raw, str) else "[non-text response]"
        if self.key:
            reply = reply.replace(self.key, "[redacted]")
        atomic_write(self.last_run / "validation.json", {
            "stage": stage, "attempt": attempt + 1, "reason": reason,
            "rejected_response": reply, "is_evidence": False,
        })


class Daily:
    def __init__(self, manager, name="daily"):
        self.manager = manager
        self.directory = manager.store.root / name
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.worker = None
        self.cancel_event = threading.Event()
        self.state_lock = threading.RLock()
        self.current = None
        # Called only after Manager acquired the process reservation.
        for path in self.directory.glob("*.json"):
            row = self._read(path)
            if row and row.get("running"):
                if self._published(row):
                    row.update(running=False, status="complete", stage="复盘已保存", error=None)
                else:
                    row.update(running=False, status="failed", error="服务已重启，复盘已中断；此前成功版本仍可查看")
                atomic_write(str(path), row)
            if row and (not self.current or row["started"] > self.current["started"]):
                self.current = row

    @staticmethod
    def _read(path):
        try:
            row = json.loads(path.read_text())
            return row if isinstance(row, dict) and isinstance(row.get("started"), (int, float)) else None
        except (OSError, ValueError):
            return None

    def _published(self, row):
        date = row.get("date", "")
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return False
        try:
            payload = json.loads((self.manager.reviews / f"{date}.json").read_text())
            return (isinstance(payload, dict) and payload.get("job_id") == row.get("job_id")
                    and payload.get("generation_mode") == "isolated_daily"
                    and payload.get("target_date") == date and payload.get("ai_source") == row.get("source")
                    and isinstance(payload.get("focus"), dict) and bool(payload["focus"]))
        except (ValueError, OSError):
            return False

    def _finish(self, **values):
        # The report commit and job-status file are separate files. Retain the
        # committed report identity even when writing status hits a disk error.
        with self.state_lock:
            try:
                self._update(**values)
            except OSError:
                self.current.update(values)
                self.current["state_warning"] = "任务状态写入失败；刷新后将按报告标识重新核对，请勿重复生成"

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def snapshot(self):
        with self.state_lock:
            row = dict(self.current or {"running": False, "status": "idle", "elapsed": 0})
            if row.get("running"):
                row["elapsed"] = int(time.time() - row["started"])
            row.pop("fingerprint", None)
            return row

    def _update(self, **values):
        with self.state_lock:
            self.current.update(values)
            atomic_write(str(self.directory / (self.current["job_id"] + ".json")), self.current)

    def submit(self, body, source, key):
        # Caller holds manager.lock across admission and thread publication.
        fingerprint = digest({"date": body.date, "source": source, "force": body.force})
        path = self.directory / (body.request_id + ".json")
        old = self._read(path)
        if path.exists():
            if not old or old.get("fingerprint") != fingerprint:
                raise EvidenceError("请求标识已使用或记录损坏；请重新发起")
            return {k: v for k, v in old.items() if k != "fingerprint"}
        if self.busy() or self.manager.active or self.manager.access.busy() or self.manager.page_chats.busy() or self.manager.deepdive.busy():
            raise EvidenceError("正在复盘、提问或接入 AI，请完成或取消后再试")
        from duanxian import review_store, trade_calendar
        from duanxian.util import validate_trade_date, is_weekend
        try:
            date = validate_trade_date(body.date)
        except ValueError:
            raise EvidenceError("日期无效，请选择已收盘交易日") from None
        if is_weekend(date):
            raise EvidenceError("目标日为周末非交易日，请选择已收盘交易日")
        if not trade_calendar.is_settled(date):
            raise EvidenceError(f"目标日尚未收盘，请选择最近已收盘的交易日：{trade_calendar.latest_session() or '暂未查到'}")
        if not body.force and review_store.usable(review_store.load(date)):
            return {"already_done": True, "date": date, "running": False}
        self.cancel_event = threading.Event()
        self.current = {"job_id": body.request_id, "fingerprint": fingerprint, "date": date,
                        "source": source, "running": True, "status": "running", "stage": "核对输入资料",
                        "started": time.time(), "elapsed": 0, "error": None}
        self._update()
        self.worker = threading.Thread(target=self._work, args=(date, source, key), daemon=True)
        self.worker.start()
        return self.snapshot()

    def cancel(self, job_id=None):
        with self.state_lock:
            if job_id is not None and (not self.current or self.current["job_id"] != job_id):
                raise EvidenceError("任务已变化，请刷新后再取消")
            if self.busy() and self.current.get("running"):
                self.cancel_event.set()
                self._update(stage="正在停止；已完成的报告会保留")
            return self.snapshot()

    def _work(self, date, source, key):
        from duanxian import preflight, review_store
        from duanxian.llm_errors import LlmConfigError
        from .grounding import generate_grounded, SOURCES
        deadline = time.monotonic() + 1200
        def check():
            if self.cancel_event.is_set():
                raise EvidenceError("复盘已取消；原报告已保留")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EvidenceError("复盘超过时限，已停止；原报告已保留")
            return remaining
        try:
            directory = self.directory / self.current["job_id"]
            directory.mkdir(mode=0o700)
            inputs = FrozenInputs(date, directory, check, lambda stage: self._update(stage=stage))
            pre = preflight.check(date, data_source=inputs)
            check()
            if not pre["ok"]:
                raise EvidenceError(preflight.refuse_reason(pre, date))
            llm = DailyLLM(self.manager.runtime, source, key, directory, date, self.cancel_event, check)
            state = generate_grounded(llm, inputs, date, check, lambda stage: self._update(stage=stage))
            check()
            warnings = list(pre["warnings"])
            for name, value in inputs.values.items():
                text = preflight._text_of(value)
                if preflight._looks_degraded(text):
                    warnings.append(text.strip() or f"{SOURCES.get(name, name)}：取数为空，本次复盘少了这一路")
            payload = review_store.serialize(state, date, list(dict.fromkeys(warnings)))
            payload.update(ai_source=source, generation_mode="isolated_daily", job_id=self.current["job_id"],
                           input_revision=digest(inputs.values), report_grounding=state["report_grounding"])
            # Hold the cancellation lock through commit: a cancellation acknowledged
            # before this point cannot subsequently publish a successful report.
            with self.state_lock:
                check()
                result = review_store.save(payload, date)
                if not result.written:
                    raise EvidenceError("复盘未通过保存检查；原报告已保留")
            # Published report remains valid even if a secondary capture fails.
            self._update(stage="复盘已保存，正在归档与核验上期条件")
            from .post_review import capture_bounded
            post = capture_bounded(date, check=check)
            atomic_write(str(directory / "post_review.json"), post)
            incomplete = [name for name, result in post.items() if not result.get("ok")]
            self._finish(running=False, status="complete", stage="复盘已保存", error=None,
                         state_warning=("后续归档/核验未完成：" + "、".join(incomplete)) if incomplete else None)
        except (EvidenceError, LlmConfigError) as exc:
            if self._published(self.current):
                post = {"capture": {"ok": False, "reason": "归档或回评已停止，可单独重试，无需重新生成报告"}}
                self._finish(running=False, status="complete", stage="复盘已保存", error=None,
                             state_warning="报告已保存；后续归档或回评已停止，可单独重试")
                try:
                    if not atomic_write(str(directory / "post_review.json"), post):
                        raise OSError("capture status not saved")
                except Exception:
                    self._finish(state_warning="报告已保存；归档或回评已停止且状态记录未写入，请检查存储后单独重试")
            else:
                self._finish(running=False, status="cancelled" if self.cancel_event.is_set() else "failed", error=str(exc))
        except Exception:
            if self._published(self.current):
                self._finish(running=False, status="complete", stage="复盘已保存", error=None,
                             state_warning="报告已保存，索引或任务状态写入未完成，请按目标日期查看")
            else:
                self._finish(running=False, status="failed", error="复盘运行异常；请检查数据源、AI 接入或存储空间；此前成功版本仍可查看")
        finally:
            key = ""
            self._finish(elapsed=int(time.time() - self.current["started"]))

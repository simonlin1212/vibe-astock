"""Local-only API and a bounded background worker for public review questions."""
from __future__ import annotations

import hmac
import os
import threading
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .evidence import EvidenceError, build_bundle, digest
from .runtime import Runtime, connection
from .store import Store
from .access import Access
from .followup import Followup


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(max_length=80)
    model: str = Field(min_length=1, max_length=100)
    baseURL: str = Field(default="", max_length=200)
    apiKey: SecretStr = Field(default=SecretStr(""))


class DailyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    llm: ModelInput
    force: bool = False


class PageMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


from .backtesting import BacktestSpec


class PageChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[PageMessage] = Field(min_length=1, max_length=12)
    context: str = Field(default="", max_length=8000)
    allow_tools: bool = False
    task_kind: Literal["page", "backtest"] = "page"
    backtest_args: BacktestSpec | None = None
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    llm: ModelInput


class DeepDiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stock: str = Field(min_length=1, max_length=40)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    llm: ModelInput


class DailyCancelInput(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ResearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_network: bool = False
    symbol: str = Field(default="", pattern=r"^(?:\d{6})?$")
    mode: Literal["direct", "agent"] = "agent"
    page: str = Field(default="", max_length=80)


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    question: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    llm: ModelInput
    scope: ResearchScope = Field(default_factory=ResearchScope)


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm: ModelInput
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class ObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    metric: str = Field(max_length=80)
    direction: str = Field(max_length=10)


class CheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            if request.method == "POST":
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > (98304 if request.url.path.endswith("/chat-jobs") else 20000):
                        return JSONResponse({"detail": "请求过大"}, status_code=413)
                request._body = bytes(body)
            try:
                return await original(request)
            except RequestValidationError:
                # Pydantic's default 422 includes rejected input, which may be
                # a whole llm object containing an API key. Never echo it.
                return JSONResponse({"detail": "请求格式无效，请检查日期、问题和 AI 设置"}, status_code=422)

        return handle


class Manager:
    def __init__(self, store: Store, reviews: Path, runtime: Runtime):
        self.store, self.reviews, self.runtime = store, reviews, runtime
        self.lock = threading.Lock()
        self.active: tuple[str, threading.Event, threading.Thread] | None = None
        self._instance_lock = None
        self.access = Access(runtime, store.root)
        self._instance_lock = (store.root / "backend.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._instance_lock.seek(0)
                msvcrt.locking(self._instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._instance_lock.close()
            raise EvidenceError("另一个后端正在使用复盘 Agent 数据目录") from None
        # Only the owner of the kernel lock may mark crashed tasks interrupted.
        with store.connect() as db:
            db.execute("UPDATE turns SET status='failed',error='服务已重启，上次任务已中断；此前成功结果已保留' WHERE status='running'")
        self.followup = Followup(store, reviews)
        from .daily import Daily
        self.daily = Daily(self)
        from .deepdive import DeepDive
        self.deepdive = DeepDive(self)
        from .chat_jobs import ChatJobs
        self.page_chats = ChatJobs(self)

    def submit(self, body: TurnInput) -> dict:
        config = body.llm.model_dump(exclude={"apiKey"})
        config["apiKey"] = body.llm.apiKey.get_secret_value()
        source, key = connection(config)
        with self.lock:
            if self.daily.busy() or self.deepdive.busy() or self.page_chats.busy():
                raise EvidenceError("正在生成复盘，请完成或取消后再提问")
            if self.access.busy():
                raise EvidenceError("正在登录或测试连接，请完成后再提问")
            # DB request idempotency is checked before the in-process worker
            # guard, so a lost HTTP response can always be recovered safely.
            if self.active and self.store.turn(self.active[0])["status"] == "cancelled":
                raise EvidenceError("正在停止上一任务，请稍后再试")
            context = body.scope.model_dump()
            if context["mode"] == "direct" and (context["allow_network"] or context["symbol"]):
                raise EvidenceError("普通对话不能请求联网或标的工具")
            # Preserve identity of existing Agent conversations.
            if context["mode"] == "agent":
                context.pop("mode")
            if not context["page"]:
                context.pop("page")
            if context["symbol"]:
                from .public_data import stock_symbol
                stock_symbol(context["symbol"])
            def bundle_factory():
                if context.get("mode") == "direct":
                    bundle = {"anchor":body.anchor, "dates":[], "evidence":[], "gaps":[], "targets":[], "scope":"普通对话，无本地资料"}
                else:
                    bundle = build_bundle(self.reviews, body.anchor)
                bundle["context"] = context
                if context["allow_network"]:
                    bundle["scope"] += " 用户已启用受控公开日线补充，仅限复盘日及之前。"
                bundle["revision"] = digest({k: v for k, v in bundle.items() if k != "revision"})
                return bundle
            turn, created = self.store.start(body.anchor, body.question, body.request_id, source,
                                             bundle_factory, body.conversation_id, context)
            if created:
                cancel = threading.Event()
                thread = threading.Thread(target=self._work, args=(turn, key, cancel), daemon=True)
                self.active = (turn["id"], cancel, thread)
                thread.start()
        return turn

    def _work(self, turn: dict, key: str, cancel: threading.Event) -> None:
        try:
            conv = self.store.conversation(turn["conversation_id"], internal=True)
            result = self.runtime.run(conv, turn["question"], key, turn["id"], cancel,
                                      lambda message: self.store.event(turn["id"], message))
            self.store.finish(turn["id"], result=result)
        except EvidenceError as exc:
            self.store.finish(turn["id"], error=str(exc))
        except Exception:
            # Raw provider/OS errors may contain credentials or private paths.
            self.store.finish(turn["id"], error="Agent 运行异常，未保存为成功结果；请检查配置后重试")
        finally:
            key = ""
            with self.lock:
                if self.active and self.active[0] == turn["id"]:
                    self.active = None

    def cancel(self, turn_id: str) -> dict:
        with self.lock:
            turn = self.store.turn(turn_id)
            if turn["status"] == "running":
                if not self.active or self.active[0] != turn_id:
                    raise EvidenceError("该任务由另一服务进程持有，请在原进程停止或等待超时")
                cancelled = self.store.cancel(turn_id)
                self.active[1].set()
                return cancelled
            return turn

    def shutdown(self) -> None:
        self.page_chats.cancel()
        if self.page_chats.worker:
            self.page_chats.worker.join(timeout=12)
        self.deepdive.cancel()
        if self.deepdive.worker:
            self.deepdive.worker.join(timeout=12)
        self.daily.cancel()
        if self.daily.worker:
            self.daily.worker.join(timeout=12)
        self.access.stop()
        with self.lock:
            active = self.active
            if active:
                active[1].set()
                self.store.cancel(active[0])
        if active:
            active[2].join(timeout=12)
        # Keep the instance reservation if an unexpected worker failed to stop.
        if self._instance_lock and not self.daily.busy() and not self.deepdive.busy() and not self.page_chats.busy() and not self.access.busy() and (not active or not active[2].is_alive()):
            self._instance_lock.close()
            self._instance_lock = None


def create_router(manager_source: Manager | Callable[[], Manager], access_key: str = "") -> APIRouter:
    def get_manager() -> Manager:
        try:
            return manager_source() if callable(manager_source) else manager_source
        except EvidenceError as exc:
            raise HTTPException(503, str(exc)) from None

    async def guard(request: Request):
        # Do not inherit the older routes' Origin-only guard: validate Host too
        # to reject DNS rebinding, including on GET conversation responses.
        allowed = {"localhost", "127.0.0.1", "::1"}
        if (request.url.hostname or "").lower() not in allowed:
            raise HTTPException(403, "复盘 Agent 仅支持本机访问")
        for header in ("origin", "referer"):
            if request.headers.get(header):
                parsed = urlparse(request.headers[header])
                if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in allowed:
                    raise HTTPException(403, "非法来源")
        if access_key and not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {access_key}"):
            raise HTTPException(401, "请填写后端访问密钥")
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise HTTPException(415, "请求必须使用 JSON")

    router = APIRouter(prefix="/api/review-agent", dependencies=[Depends(guard)], route_class=PrivateRoute)

    def checked(fn, *args):
        try:
            return fn(*args)
        except EvidenceError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/daily")
    def daily_status(manager: Manager = Depends(get_manager)):
        return manager.daily.snapshot()

    @router.post("/daily")
    def daily_submit(body: DailyInput, manager: Manager = Depends(get_manager)):
        config = body.llm.model_dump(exclude={"apiKey"})
        config["apiKey"] = body.llm.apiKey.get_secret_value()
        source, key = checked(connection, config)
        with manager.lock:
            return checked(manager.daily.submit, body, source, key)

    @router.post("/daily/cancel")
    def daily_cancel(body: DailyCancelInput, manager: Manager = Depends(get_manager)):
        return checked(manager.daily.cancel, body.job_id)

    @router.get("/deepdive")
    def deepdive_status(job_id: str = "", manager: Manager = Depends(get_manager)):
        return checked(manager.deepdive.status, job_id)

    @router.post("/deepdive")
    def deepdive_submit(body: DeepDiveInput, manager: Manager = Depends(get_manager)):
        config = body.llm.model_dump(exclude={"apiKey"})
        config["apiKey"] = body.llm.apiKey.get_secret_value()
        source, key = checked(connection, config)
        with manager.lock:
            return checked(manager.deepdive.submit, body, source, key)

    @router.post("/deepdive/cancel")
    def deepdive_cancel(body: DailyCancelInput, manager: Manager = Depends(get_manager)):
        return checked(manager.deepdive.cancel, body.job_id)

    @router.get("/deepdive/reports/{job_id}")
    def deepdive_report(job_id: str, manager: Manager = Depends(get_manager)):
        return checked(manager.deepdive.report, job_id)

    @router.post("/chat-jobs")
    def page_chat_submit(body: PageChatInput, manager: Manager = Depends(get_manager)):
        config = body.llm.model_dump(exclude={"apiKey"})
        config["apiKey"] = body.llm.apiKey.get_secret_value()
        source, key = checked(connection, config)
        with manager.lock:
            return checked(manager.page_chats.submit, body, source, key)

    @router.get("/chat-jobs/{job_id}")
    def page_chat_status(job_id: str, manager: Manager = Depends(get_manager)):
        return checked(manager.page_chats.status, job_id)

    @router.get("/chat-jobs/{job_id}/execution")
    def backtest_execution(job_id: str, manager: Manager = Depends(get_manager)):
        from .backtesting import execution_archive
        path = checked(execution_archive, manager.page_chats, job_id)
        return FileResponse(path, media_type="application/zip", filename=f"backtest-{job_id}-execution.zip")

    @router.post("/chat-jobs/{job_id}/cancel")
    def page_chat_cancel(job_id: str, manager: Manager = Depends(get_manager)):
        with manager.lock:
            return checked(manager.page_chats.cancel, job_id)

    @router.get("/backtest-reports")
    def backtest_reports(manager: Manager = Depends(get_manager)):
        rows = []
        for path in manager.page_chats.directory.glob("*.json"):
            row = manager.page_chats._read(path)
            if row and row.get("backtest_result") and row.get("status") == "complete":
                rows.append({"job_id": row["job_id"], "started": row["started"], "spec": row.get("backtest_spec")})
        return sorted(rows, key=lambda r: r["started"], reverse=True)[:100]

    @router.get("/macro-probability")
    def macro_probability(manager: Manager = Depends(get_manager)):
        from .probability_cache import get_probability
        return checked(get_probability, manager.store.root)

    @router.get("/status")
    def status(manager: Manager = Depends(get_manager)):
        return manager.runtime.status()

    @router.get("/subscriptions/{provider}")
    def subscription(provider: Literal["claude", "codebuddy"], manager: Manager = Depends(get_manager)):
        from .subscription_bridge import subscription_status
        return checked(subscription_status, manager.runtime, provider)

    @router.get("/access")
    def access_status(manager: Manager = Depends(get_manager)):
        return manager.access.snapshot()

    def start_access(manager, kind, source=None, key="", request_id=None):
        with manager.lock:
            if manager.active or manager.daily.busy() or manager.deepdive.busy() or manager.page_chats.busy():
                raise EvidenceError("请先完成或取消正在运行的分析")
            return manager.access.start(kind, source, key, request_id=request_id)

    @router.post("/access/login")
    def login(manager: Manager = Depends(get_manager)):
        return checked(start_access, manager, "login")

    @router.post("/access/probe")
    def probe(body: ProbeInput, manager: Manager = Depends(get_manager)):
        config = body.llm.model_dump(exclude={"apiKey"})
        config["apiKey"] = body.llm.apiKey.get_secret_value()
        source, key = checked(connection, config)
        return checked(start_access, manager, "probe", source, key, body.request_id)

    @router.post("/access/cancel")
    def cancel_access(manager: Manager = Depends(get_manager)):
        return manager.access.stop()

    @router.get("/conversations")
    def conversations(anchor: str = "", mode: Literal["agent", "direct"] = "agent", page: str = "", manager: Manager = Depends(get_manager)):
        return checked(manager.store.list_conversations, anchor, mode, page)

    @router.get("/catalog")
    def catalog(anchor: str, manager: Manager = Depends(get_manager)):
        bundle = checked(build_bundle, manager.reviews, anchor)
        return {"targets": bundle["targets"], "revision": bundle["revision"]}

    @router.get("/observations")
    def observations(conversation_id: str, manager: Manager = Depends(get_manager)):
        return checked(manager.followup.list, conversation_id)

    @router.get("/observations/menu")
    def observation_menu():
        return Followup.menu()

    @router.post("/observations")
    def add_observation(body: ObservationInput, manager: Manager = Depends(get_manager)):
        return checked(manager.followup.add, body.turn_id, body.metric, body.direction)

    @router.post("/observations/{observation_id}/check")
    def check_observation(observation_id: str, body: CheckInput, manager: Manager = Depends(get_manager)):
        return checked(manager.followup.check, observation_id, body.date)

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str, manager: Manager = Depends(get_manager)):
        return checked(manager.store.conversation, conversation_id)

    @router.post("/conversations/{conversation_id}/delete")
    def delete_conversation(conversation_id: str, manager: Manager = Depends(get_manager)):
        return checked(manager.store.delete_conversation, conversation_id)

    @router.get("/turns/{turn_id}")
    def turn(turn_id: str, manager: Manager = Depends(get_manager)):
        return checked(manager.store.turn, turn_id)

    @router.post("/turns")
    def submit(body: TurnInput, manager: Manager = Depends(get_manager)):
        return checked(manager.submit, body)

    @router.post("/turns/{turn_id}/cancel")
    def cancel(turn_id: str, manager: Manager = Depends(get_manager)):
        return checked(manager.cancel, turn_id)

    return router

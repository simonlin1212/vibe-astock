"""Product-owned OAuth and real model probes. No developer credentials are read."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .engine_guard import guard_python
from .evidence import EvidenceError, canonical, digest
from .runtime import REPO, Runtime, engine_command, engine_environment, stop_process


def public_login_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 12000:
        raise EvidenceError("登录地址格式无效")
    url = urlparse(value)
    if url.scheme != "https" or url.hostname not in {"auth.openai.com", "chatgpt.com"} or url.username or url.password or url.port not in (None, 443):
        raise EvidenceError("登录服务返回了非官方地址")
    return value


class Access:
    def __init__(self, runtime: Runtime, root: Path | None = None):
        self.runtime = runtime
        self.lock = threading.Lock()
        self.state = {"status": "idle"}
        self.worker = None
        self.cancel = threading.Event()
        self.journal = (root if root is not None else self.runtime.root) / "access-tests.json"
        self.requests = {}
        if self.journal.is_symlink():
            raise EvidenceError("连接测试记录不能是符号链接")
        if self.journal.exists():
            try:
                self.requests = json.loads(self.journal.read_text())
                if not isinstance(self.requests, dict):
                    raise ValueError()
                for value in self.requests.values():
                    if value["state"]["status"] == "running":
                        value["state"].update(status="failed", error="连接测试随服务中断，未自动重试；请检查后重新发起测试")
                self._persist()
                if self.requests:
                    self.state = dict(max(self.requests.values(), key=lambda value: value["state"]["started"])["state"])
            except (ValueError, KeyError, TypeError):
                raise EvidenceError("连接测试记录损坏，已保留原件，请检查运行目录") from None

    def _persist(self):
        # No credentials or private CLI outputs: only request fingerprint and safe status.
        tmp = self.journal.with_name(".access-" + uuid.uuid4().hex + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(canonical(self.requests))
            os.replace(tmp, self.journal)
        finally:
            tmp.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _update(self, **values) -> None:
        with self.lock:
            self.state.update(values)
            if self.state.get("id") in self.requests:
                self.requests[self.state["id"]]["state"] = dict(self.state)
                self._persist()

    def start(self, kind: str, source: dict | None = None, key: str = "", *, request_id: str | None = None) -> dict:
        with self.lock:
            fingerprint = digest([kind, source, key])
            if request_id in self.requests:
                old = self.requests[request_id]
                if old["fingerprint"] != fingerprint:
                    raise EvidenceError("同一测试编号不能用于不同配置")
                return dict(old["state"])
            if self.busy():
                raise EvidenceError("已有登录或连接测试在进行")
            self.cancel = threading.Event()
            self.state = {"id": request_id or uuid.uuid4().hex, "kind": kind, "status": "running", "started": time.time()}
            if request_id:
                self.requests[request_id] = {"fingerprint": fingerprint, "state": dict(self.state)}
                self._persist()
            target = self._login if kind == "login" else self._probe
            args = () if kind == "login" else (source, key)
            self.worker = threading.Thread(target=self._work, args=(target, args), daemon=True)
            self.worker.start()
            return dict(self.state)

    def _work(self, target, args):
        try:
            target(*args)
        except EvidenceError as exc:
            self._update(status="cancelled" if self.cancel.is_set() else "failed", error=str(exc), auth_url=None)
        except Exception:
            self._update(status="failed", error="连接未完成，请检查网络或接入设置后重试", auth_url=None)

    def stop(self) -> dict:
        self.cancel.set()
        worker = self.worker
        if worker:
            worker.join(timeout=12)
        return self.snapshot()

    def _probe(self, source: dict, key: str) -> None:
        bundle = {"anchor": "2000-01-01", "dates": [], "evidence": [], "gaps": ["连接测试没有市场资料"],
                  "scope": "仅测试连接与工具调用，不分析市场"}
        bundle["revision"] = digest(bundle)
        runtime = Runtime(self.runtime.root, timeout=120)
        result = runtime.run({"source": source, "bundle": bundle, "turns": []},
                             "这是连接测试。调用 list_evidence 后，使用 submit_answer 提交 incomplete，说明没有市场资料。",
                             key, "probe-" + uuid.uuid4().hex, self.cancel,
                             lambda message: self._update(message=message))
        if self.cancel.is_set():
            raise EvidenceError("连接测试已取消")
        self._update(status="complete", source=source, checked_at=time.time(),
                     elapsed_seconds=result["elapsed_seconds"], message="模型与工具调用已通过真实测试")

    def _login(self) -> None:
        staging = Path(tempfile.mkdtemp(prefix="login-", dir=self.runtime.root))
        proc = None
        try:
            # File-only storage belongs to this login attempt. Failed/cancelled
            # attempts cannot replace a previously working product login.
            command = engine_command() + ["app-server", "--stdio", "-c", 'cli_auth_credentials_store="file"',
                                           "-c", "mcp_servers={}", "-c", "features.apps=false", "-c", "features.plugins=false"]
            proc = subprocess.Popen([guard_python(), str(REPO / "review_agent/engine_guard.py"), "605", str(os.getpid()), *command],
                                    cwd=staging, env=engine_environment(staging), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
            messages = queue.Queue(maxsize=32)
            reading = threading.Event()

            def reader():
                while not reading.is_set():
                    line = proc.stdout.readline(65537)
                    if not line:
                        break
                    while not reading.is_set():
                        try:
                            messages.put(line, timeout=.1)
                            break
                        except queue.Full:
                            pass

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()

            def send(method, params=None, request_id=None):
                payload = {"method": method}
                if params is not None:
                    payload["params"] = params
                if request_id is not None:
                    payload["id"] = request_id
                proc.stdin.write((canonical(payload) + "\n").encode())
                proc.stdin.flush()

            send("initialize", {"clientInfo": {"name": "vibe_astock", "version": "0.2.0"}}, 1)
            deadline, size, login_id = time.monotonic() + 600, 0, None
            try:
                while time.monotonic() < deadline:
                    if self.cancel.is_set():
                        raise EvidenceError("登录已取消")
                    if proc.poll() is not None:
                        raise EvidenceError("登录服务已退出，请重试")
                    try:
                        line = messages.get(timeout=.2)
                    except queue.Empty:
                        continue
                    size += len(line)
                    if len(line) > 65536 or size > 2_000_000:
                        raise EvidenceError("登录服务响应异常")
                    event = json.loads(line)
                    if "error" in event:
                        raise EvidenceError("登录请求失败，请检查网络后重试")
                    if event.get("id") == 1:
                        send("initialized")
                        send("account/login/start", {"type": "chatgpt"}, 2)
                    elif event.get("id") == 2:
                        result = event["result"]
                        login_id = result["loginId"]
                        self._update(auth_url=public_login_url(result["authUrl"]), message="请在官方页面完成登录")
                    elif event.get("method") == "account/login/completed":
                        params = event["params"]
                        if params.get("loginId") != login_id:
                            continue
                        if params.get("success") is not True:
                            raise EvidenceError("登录未完成，请重试")
                        send("account/read", {"refreshToken": False}, 3)
                    elif event.get("id") == 3:
                        if event["result"].get("account", {}).get("type") != "chatgpt":
                            raise EvidenceError("尚未验证到 ChatGPT 登录")
                        auth = staging / "auth.json"
                        if auth.is_symlink() or not auth.is_file() or not 0 < auth.stat().st_size < 100000:
                            raise EvidenceError("产品登录文件未生成")
                        if self.cancel.is_set():
                            raise EvidenceError("登录已取消")
                        os.chmod(auth, 0o600)
                        os.replace(auth, self.runtime.home / "auth.json")
                        self._update(status="complete", auth_url=None, message="产品专用 ChatGPT 登录已完成；可继续测试模型")
                        return
                raise EvidenceError("登录已超时，请重新发起")
            finally:
                reading.set()
                stop_process(proc)
                thread.join(timeout=2)
        finally:
            if proc and proc.poll() is None:
                stop_process(proc)
            if proc:
                proc.stdin.close()
                proc.stdout.close()
            shutil.rmtree(staging)

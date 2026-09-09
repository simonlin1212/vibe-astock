"""Local setup, offline diagnostics and foreground launch (Python standard library).

Uses the existing venv, npm lockfiles and Uvicorn service. No global package,
credential or OS service installation. Supports local browser startup on Windows too.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser

PLATFORM = os.name
ROOT = Path(__file__).resolve().parents[1]


class SetupError(RuntimeError):
    pass


def python_at(root: Path) -> Path:
    return root / (".venv/Scripts/python.exe" if PLATFORM == "nt" else ".venv/bin/python")


def npm_command():
    if PLATFORM != "nt":
        return ["npm"]
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm and node:
        # Standard Node/npm installation and nvm-windows layout; no cmd.exe,
        # shell=True, or shell interpolation of a user-chosen checkout path.
        entry = Path(npm).resolve().parent / "node_modules/npm/bin/npm-cli.js"
        if entry.is_file():
            return [node, str(entry)]
    raise SetupError("无法找到 npm 的 Node 入口，请重新安装官方 Node.js 22+（含 npm）。")


def run(argv, root, timeout=900):
    try:
        if str(argv[0]) == "npm":
            argv = npm_command() + list(argv[1:])
        subprocess.run([str(v) for v in argv], cwd=root, check=True, timeout=timeout,
                       env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise SetupError("此步骤未完成，请检查上方提示后重新运行；已有用户数据不会删除。") from None


def fingerprint(root: Path) -> str:
    """Track installation/build inputs, never user data or generated outputs."""
    files = [root / p for p in ("requirements.txt", "runtime/package.json", "runtime/package-lock.json")]
    for directory, dirs, names in os.walk(root / "frontend"):
        dirs[:] = [d for d in dirs if d not in {"node_modules", "dist", ".git"}]
        files += [Path(directory) / n for n in names if not n.endswith(".tsbuildinfo")]
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(str(p.relative_to(root)).encode()); h.update(b"\0"); h.update(p.read_bytes())
    return h.hexdigest()


def probe(argv, root, timeout=30):
    try:
        p = subprocess.run([str(v) for v in argv], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prerequisites(root):
    if sys.version_info < (3, 10):
        raise SetupError("请安装 Python 3.10 或更高版本，推荐 Python 3.12。")
    if not shutil.which("npm") or not probe(["node", "-e", "process.exit(Number(process.versions.node.split('.')[0])>=22?0:1)"], root):
        raise SetupError("请安装 Node.js 22 或更高版本（需包含 npm），然后重新打开启动器。")


def dependency_status(root):
    code = """
import importlib,json
failures=[]
for name in ['fastapi','uvicorn','langgraph','langchain_openai','akshare','requests','dotenv','mcp','py_mini_racer','bs4','numpy','pandas','baostock']:
 try: importlib.import_module(name)
 except Exception as exc: failures.append(name+': '+type(exc).__name__)
print('ASTOCK_IMPORTS='+json.dumps(failures))
"""
    try:
        p = subprocess.run([str(python_at(root)), "-c", code], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60)
    except subprocess.TimeoutExpired:
        return False, "依赖加载超过一分钟；请检查机器负载后重试体检"
    except OSError:
        return False, "Python 环境无法启动；运行 scripts/setup"
    if p.returncode != 0:
        return False, f"依赖检查进程未完成（退出码 {p.returncode}）；请重试体检"
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("ASTOCK_IMPORTS="):
            try:
                failures = json.loads(line.removeprefix("ASTOCK_IMPORTS="))
                if isinstance(failures, list) and all(isinstance(v, str) for v in failures):
                    return not failures, "运行 scripts/setup；依赖加载失败：" + ", ".join(failures)
            except ValueError:
                break
    return False, f"依赖检查进程未完成（退出码 {p.returncode}）；请重试体检"


def doctor(root: Path) -> list[dict]:
    checks = []
    def add(name, ok, action):
        checks.append({"name": name, "ok": bool(ok), "action": "通过" if ok else action})
    add("平台", PLATFORM in ("posix", "nt"), "请使用 macOS、Linux 或 Windows 10/11")
    py = python_at(root)
    add("Python 环境", probe([py, "-c", "import sys;sys.exit(sys.version_info < (3,10))"], root), "运行 scripts/setup")
    add("Node.js / npm", bool(shutil.which("npm")) and probe(["node", "-e", "process.exit(Number(process.versions.node.split('.')[0])>=22?0:1)"], root), "安装 Node.js 22+（包含 npm）")
    dependencies_ok, dependency_action = dependency_status(root)
    add("Python 依赖", dependencies_ok, dependency_action)
    add("依赖安装工具", probe([py, "-m", "pip", "--version"], root), "运行 scripts/setup 自动补齐 pip")
    add("依赖兼容性", probe([py, "-m", "pip", "check"], root), "运行 scripts/setup；如仍失败，检查 Python 依赖冲突")
    add("官方 Agent 引擎", probe(["node", root / "runtime/node_modules/@openai/codex/bin/codex.js", "--version"], root), "运行 scripts/setup 安装本机引擎")
    add("浏览器界面", (root / "frontend/dist/index.html").is_file(), "运行 scripts/setup 构建界面")
    try:
        stamp = json.loads((root / ".local/setup.json").read_text())
        current = isinstance(stamp, dict) and stamp.get("fingerprint") == fingerprint(root)
    except (OSError, ValueError):
        current = False
    add("安装与源码一致", current, "首次使用或源码更新后运行 scripts/setup")
    return checks


def print_checks(checks):
    for c in checks:
        print(f"{'✓' if c['ok'] else '✗'} {c['name']}：{c['action']}", flush=True)
    print("体检不请求模型。AI 登录和连接测试请在浏览器的「接入 AI」完成。", flush=True)


def setup(root: Path):
    prerequisites(root)
    py = python_at(root)
    if not py.exists():
        if (root / ".venv").exists():
            raise SetupError("现有 .venv 缺少解释器。请先将该目录移走保留，再重新安装。")
        print("正在准备独立 Python 环境…", flush=True)
        run([sys.executable, "-m", "venv", root / ".venv"], root)
    if not probe([py, "-c", "import sys;sys.exit(sys.version_info < (3,10))"], root):
        raise SetupError("现有 .venv 无法运行或 Python 版本过低。请先移走保留，再重新安装。")
    if not probe([py, "-m", "pip", "--version"], root):
        run([py, "-m", "ensurepip", "--upgrade"], root)
    print("正在安装 Python 依赖…", flush=True)
    run([py, "-m", "pip", "install", "-r", root / "requirements.txt"], root)
    for directory in ("runtime", "frontend"):
        print(f"正在准备 {directory}…", flush=True)
        run(["npm", "ci", "--no-audit", "--no-fund"], root / directory)
    print("正在构建浏览器界面…", flush=True)
    run(["npm", "run", "build"], root / "frontend")
    checks = doctor(root)
    print_checks(checks[:-1])
    if not all(c["ok"] for c in checks[:-1]):
        raise SetupError("安装后的体检未通过，尚未标记安装完成。")
    local = root / ".local"
    local.mkdir(mode=0o700, exist_ok=True)
    tmp = local / ("setup-" + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps({"fingerprint": fingerprint(root)}, indent=2))
    tmp.replace(local / "setup.json")
    print("安装完成。下次直接打开「启动 Vibe AStock.cmd」。" if PLATFORM == "nt" else "安装完成。下次直接打开「启动 Vibe AStock.command」。", flush=True)


def healthy(url: str, launch_id: str) -> bool:
    # Loopback traffic must never use the user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + "/api/astock/health", timeout=1) as r:
            body = json.loads(r.read(10000))
        return (body.get("service") == "vibe-astock" and body.get("launch_id") == launch_id
                and body.get("ready") is True)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def stop(child):
    if child.poll() is not None:
        return
    if PLATFORM == "nt":
        # child is the Job-owning guard, never the bare Uvicorn process.
        child.kill()
        child.wait(timeout=10)
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def start(root: Path, port: int, browser: bool, timeout=60):
    checks = doctor(root)
    print_checks(checks)
    if not all(c["ok"] for c in checks):
        raise SetupError("尚未就绪。请运行 scripts/setup，或双击启动文件自动准备环境。")
    try:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    except OSError:
        raise SetupError(f"端口 {port} 已被占用。请关闭原服务，或使用 scripts/start --port 8911。") from None
    launch_id = uuid.uuid4().hex
    env = {**os.environ, "ASTOCK_LAUNCH_ID": launch_id, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    log = root / ".local/startup.log"
    log.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a") as output:
        command = [str(python_at(root)), "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
                   "--port", str(port), "--no-access-log"]
        if PLATFORM == "nt":
            # No time limit for the foreground web service; parent death remains
            # guarded, including forcibly closing the Windows terminal window.
            command = [str(python_at(root)), str(root / "review_agent/engine_guard.py"),
                       "0", str(os.getpid()), *command]
        child = subprocess.Popen(command, cwd=root, env=env, stdout=output, stderr=output,
                                 start_new_session=PLATFORM != "nt")
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + timeout
            while child.poll() is None and time.monotonic() < deadline:
                if healthy(url, launch_id):
                    break
                time.sleep(.3)
            else:
                raise SetupError(f"服务未能启动。已有服务占用数据目录或启动失败，请查看 {log}")
            print(f"已启动：{url}\n保留此窗口；按 Control+C 安全停止。日志：{log}", flush=True)
            if browser:
                webbrowser.open(url)
            code = child.wait()
            if code:
                raise SetupError(f"服务意外退出，请查看 {log}")
        finally:
            stop(child)


def main():
    parser = argparse.ArgumentParser(description="Vibe AStock 安装、体检与启动")
    parser.add_argument("command", choices=["setup", "doctor", "start", "auto"])
    parser.add_argument("--port", type=int, default=8910)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口请使用 1024–65535")
    def interrupted(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, interrupted)
    try:
        if args.command == "doctor":
            checks = doctor(ROOT)
            if args.json:
                print(json.dumps({"ok": all(c["ok"] for c in checks), "checks": checks}, ensure_ascii=False))
            else:
                print_checks(checks)
            return 0 if all(c["ok"] for c in checks) else 1
        prerequisites(ROOT)
        if args.command == "setup" or (args.command == "auto" and not all(c["ok"] for c in doctor(ROOT))):
            setup(ROOT)
        if args.command != "setup":
            start(ROOT, args.port, not args.no_browser)
        return 0
    except KeyboardInterrupt:
        print("已停止启动器与本次服务。", flush=True)
        return 130
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

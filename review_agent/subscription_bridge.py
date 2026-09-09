"""Research subscription adapter over private stdin. Parent owns a guarded process tree."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
from .process_io import read_chunk, write_input, GUARD_REAP_EXIT
from .evidence import EvidenceError, canonical


def bridge_environment(home, agent: str) -> dict:
    from .runtime import engine_environment
    env = engine_environment(home)
    # Explicit binary locations, never inherit API routing/credentials from server.
    # Claude's OS credential lookup needs USER/LOGNAME as well as HOME. Let the
    # official CLI read its own login; do not copy any credential files.
    names = ['USER', 'LOGNAME', 'SHELL', 'CLAUDE_BIN', 'CODEBUDDY_BIN',
             'SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS']
    if agent == 'claude':
        names += ['CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN']
    for name in names:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def call_bridge(runtime, agent: str, request: dict, cancel: threading.Event, progress, timeout: float):
    from .runtime import REPO, stop_process
    import shutil
    node = shutil.which('node')
    if not node:
        raise EvidenceError('订阅运行桥需要 Node，请完成运行环境安装')
    env = bridge_environment(runtime.home, agent)
    proc = subprocess.Popen([sys.executable, str(REPO / 'review_agent/engine_guard.py'),
                             str(timeout + 5), str(os.getpid()), "--bridge-reap-group", node, str(REPO / 'runtime/bridge/main.mjs')],
                            cwd=REPO / 'runtime/bridge', env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True, bufsize=0)
    deadline = time.monotonic() + timeout
    pending, size, result, exit_code = b'', 0, None, None
    try:
        write_input(proc, (canonical({'protocol':1, 'agent':agent, **request})+'\n').encode(), cancel, deadline)
        while True:
            if cancel.is_set():
                raise EvidenceError('任务已取消')
            if time.monotonic() > deadline:
                raise EvidenceError('订阅连接超时，已停止')
            chunk = read_chunk(proc.stdout, .1)
            if chunk is None:
                continue
            if not chunk:
                break
            size += len(chunk)
            if size > 5_000_000:
                raise EvidenceError('订阅输出过大，已停止')
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n',1)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    raise EvidenceError('订阅运行桥返回格式无效') from None
                if event.get('type') == 'failed':
                    code = event.get('code')
                    messages = {'agent_not_installed':'所选订阅 CLI 未安装，请先安装对应官方客户端',
                                'agent_not_authenticated':'所选订阅尚未登录或登录失效，请在官方客户端登录',
                                'agent_probe_failed':'所选订阅状态检测未完成，请重试并检查版本或系统访问权限；现有登录未改变',
                                'agent_cli_too_old':'所选订阅 CLI 版本缺少隔离能力，请升级；未复制旧版凭据',
                                'agent_quota':'所选订阅额度或频率受限，请稍后重试',
                                'agent_timeout':'所选订阅响应超时，已停止'}
                    raise EvidenceError(messages.get(code, '所选订阅执行失败，请检查登录、模型或兼容性'))
                if event.get('type') == 'bridge_exit':
                    exit_code = event.get('code')
                elif event.get('type') == 'started':
                    progress('所选订阅正在处理请求')
                elif event.get('type') == 'result':
                    if result is not None:
                        raise EvidenceError('运行桥重复提交最终结果')
                    result = event
        if pending.strip() or proc.wait(timeout=5) != GUARD_REAP_EXIT or exit_code != 0 or result is None:
            raise EvidenceError('订阅运行桥未完整退出，未保存结果')
        return result
    finally:
        # Adapter children deliberately inherit this group, including MCP. Kill
        # and reap after success, timeout, cancellation and abnormal bridge exit.
        stop_process(proc)
        proc.stdin.close()
        proc.stdout.close()


def subscription_status(runtime, agent: str) -> dict:
    return call_bridge(runtime, agent, {'action':'status'}, threading.Event(), lambda _:None, 55)['status']


def invoke(runtime, run, source, prompt, system, cancel, progress, budget, *, tools: tuple[str, ...]):
    from .runtime import REPO
    import sys
    request = {'action':'run', 'model':source['model'], 'prompt':prompt,
               'system':system, 'timeout':budget}
    if tools:
        request['mcp'] = {'serverName':'astock','command':sys.executable,
                          'args':['-m','review_agent.mcp_server'],
                          'env':{'PYTHONPATH':str(REPO),'ASTOCK_AGENT_RUN':str(run),'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'},
                          'allowedTools':['mcp__astock__'+name for name in tools], 'maxTurns':32}
    result = call_bridge(runtime, source['provider'], request, cancel, progress, budget + 10)
    text = result.get('text')
    if not isinstance(text,str) or not text.strip():
        raise EvidenceError('所选订阅没有返回可见回答')
    return text

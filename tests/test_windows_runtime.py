"""Platform-neutral process contracts plus native Windows tests (no vendor quota)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from review_agent import runtime
from review_agent.evidence import EvidenceError


def test_engine_and_status_do_not_reject_windows(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(runtime, 'os', SimpleNamespace(name='nt', environ=os.environ))
    monkeypatch.setattr(runtime, 'CLI', tmp_path / 'codex.js')
    runtime.CLI.write_text('')
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: 'node.exe')
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 1, b'', b''))
    assert runtime.engine_command() == ['node.exe', str(runtime.CLI)]
    assert runtime.Runtime(tmp_path / 'state').status()['installed'] is True


def test_windows_venv_path(tmp_path, monkeypatch):
    from scripts import manage
    monkeypatch.setattr(manage, 'PLATFORM', 'nt')
    assert manage.python_at(tmp_path) == tmp_path / '.venv/Scripts/python.exe'


def spawn_guard(code, timeout=3, bridge=False):
    return subprocess.Popen([sys.executable, str(runtime.REPO / 'review_agent/engine_guard.py'),
        str(timeout), str(os.getpid()), *(['--bridge-reap-group'] if bridge else []),
        sys.executable, '-u', '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=os.name == 'posix', bufsize=0)


def test_pipe_reader_handles_unicode_and_eof():
    from review_agent.process_io import read_chunk
    p = spawn_guard("print('中文✓')")
    try:
        output = b''
        until = time.monotonic() + 5
        while time.monotonic() < until:
            part = read_chunk(p.stdout, .1)
            if part is None: continue
            if not part: break
            output += part
        assert output.decode('utf-8').strip() == '中文✓'
        assert p.wait(timeout=3) == 0
    finally:
        runtime.stop_process(p)
        for stream in (p.stdin, p.stdout, p.stderr): stream.close()


@pytest.mark.parametrize('cancelled', [False, True])
def test_full_pipe_write_can_timeout_or_cancel(cancelled):
    from review_agent.process_io import write_input
    p = spawn_guard('import time;time.sleep(30)', timeout=5)
    cancel = threading.Event()
    timer = threading.Timer(.15, cancel.set) if cancelled else None
    if timer: timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(EvidenceError, match='取消' if cancelled else '超时'):
            write_input(p, b'x' * 2_000_000, cancel, time.monotonic() + .4)
        assert time.monotonic() - start < 3
        assert p.poll() is not None
    finally:
        if timer: timer.join()
        runtime.stop_process(p)
        for stream in (p.stdin, p.stdout, p.stderr): stream.close()


def test_guard_reports_exit_then_reaps_group():
    from review_agent.process_io import GUARD_REAP_EXIT
    p = spawn_guard("print('{\"type\":\"result\",\"value\":1}')", bridge=True)
    out, err = p.communicate(timeout=5)
    assert p.returncode == GUARD_REAP_EXIT, err
    assert [json.loads(line) for line in out.splitlines()] == [
        {'type':'result','value':1}, {'type':'bridge_exit','code':0}]


@pytest.mark.skipif(os.name != 'nt', reason='Requires real Windows kernel Job Objects')
@pytest.mark.parametrize('ending', ['timeout', 'cancel', 'normal'])
def test_windows_guard_kills_detached_descendant(tmp_path, ending):
    from review_agent.windows_process import process_alive
    pidfile = tmp_path / 'descendant.txt'
    code = ("import subprocess,sys,time;from pathlib import Path;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=512);"
            f"Path({str(pidfile)!r}).write_text(str(p.pid));" +
            ("time.sleep(.5)" if ending == 'normal' else "time.sleep(60)"))
    p = spawn_guard(code, timeout=2)
    try:
        until = time.monotonic()+5
        while not pidfile.exists() and time.monotonic()<until: time.sleep(.02)
        assert pidfile.exists(), p.stderr.read() if p.poll() is not None else 'not started'
        pid = int(pidfile.read_text())
        if ending == 'cancel': runtime.stop_process(p)
        else: p.wait(timeout=5)
        until = time.monotonic()+3
        while process_alive(pid) and time.monotonic()<until: time.sleep(.02)
        assert not process_alive(pid)
    finally:
        runtime.stop_process(p)
        for stream in (p.stdin,p.stdout,p.stderr): stream.close()


def test_deepseek_probe_runs_guard_and_validates_evidence_without_network(tmp_path, monkeypatch):
    from review_agent.access import Access
    script = tmp_path / 'fake_engine.py'
    script.write_text('''import os,sys,json
from pathlib import Path
sys.path.insert(0, REPO_PATH)
from review_agent.evidence import ToolSession
assert os.environ['ASTOCK_MODEL_KEY'] == 'synthetic-private-key'
assert 'synthetic-private-key' not in ' '.join(sys.argv)
assert sys.stdin.read()
run=Path.cwd()
session=ToolSession(json.loads((run/'bundle.json').read_text(encoding='utf-8')),run/'tools.jsonl')
session.list_evidence()
session.submit_answer(**{'status':'incomplete','findings':[{'text':'连接测试未提供市场资料。','citations':[]}],'gaps':['未获取市场资料']})
print(json.dumps({'type':'turn.completed'}),flush=True)
'''.replace('REPO_PATH', repr(str(runtime.REPO))), encoding='utf-8')
    monkeypatch.setattr(runtime, 'engine_command', lambda: [sys.executable, str(script)])
    source, key = runtime.connection({'provider':'api-compatible','model':'deepseek-v4-flash',
        'baseURL':'https://api.deepseek.com','apiKey':'synthetic-private-key'})
    access = Access(runtime.Runtime(tmp_path / 'state'))
    access.start('probe', source, key)
    until = time.monotonic()+10
    while access.busy() and time.monotonic()<until: time.sleep(.05)
    assert not access.busy()
    assert access.snapshot()['status'] == 'complete', access.snapshot()
    for p in (tmp_path / 'state').rglob('*'):
        if p.is_file(): assert b'synthetic-private-key' not in p.read_bytes()


def test_real_subscription_transport_uses_cross_platform_node_shim(tmp_path, monkeypatch):
    import shutil
    from review_agent.subscription_bridge import subscription_status, invoke
    node = shutil.which('node')
    if not node: pytest.skip('Node required for subscription transport')
    script = tmp_path / 'fake-cli.cjs'
    script.write_text('''#!NODE_PATH
const a=process.argv.slice(2);
if(a.includes('--version')) console.log('fake test CLI');
else if(a.includes('--help')) console.log('--safe-mode --tools --strict-mcp-config --no-session-persistence --output-format --system-prompt --json-schema');
else if(a[0]==='auth') console.log(JSON.stringify({loggedIn:true,authMethod:'claude.ai',apiProvider:'firstParty'}));
else { let text='';process.stdin.setEncoding('utf8');process.stdin.on('data',x=>text+=x);process.stdin.on('end',()=>{
 if(!text.includes('中文资料')) process.exitCode=1;
 else console.log(JSON.stringify({result:'合成回答'}));
}); }
'''.replace('NODE_PATH', node), encoding='utf-8')
    if os.name == 'nt':
        shim = tmp_path / 'claude.ps1'
        shim.write_text('#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n& "node$exe" "$basedir/fake-cli.cjs" $args\n', encoding='utf-8')
    else:
        shim = script
        script.chmod(0o700)
    monkeypatch.setenv('CLAUDE_BIN', str(shim))
    r = runtime.Runtime(tmp_path / 'state')
    assert subscription_status(r, 'claude')['available'] is True
    assert invoke(r, tmp_path, {'provider':'claude','model':'default'}, '中文资料', 'test',
                  threading.Event(), lambda _:None, 15, tools=()) == '合成回答'


def test_windows_npm_uses_node_without_shell(tmp_path, monkeypatch):
    from scripts import manage
    monkeypatch.setattr(manage, 'PLATFORM', 'nt')
    location = tmp_path / 'Node & 中文'
    entry = location / 'node_modules/npm/bin/npm-cli.js'
    entry.parent.mkdir(parents=True)
    entry.write_text('')
    monkeypatch.setattr(manage.shutil, 'which', lambda name: str(location / (name + ('.cmd' if name=='npm' else '.exe'))))
    assert manage.npm_command() == [str(location/'node.exe'), str(entry)]
    calls = []
    monkeypatch.setattr(manage.subprocess, 'run', lambda args, **kwargs: calls.append((args, kwargs)))
    manage.run(['npm', 'ci'], tmp_path)
    assert calls[0][0] == [str(location/'node.exe'), str(entry), 'ci']
    assert not calls[0][1].get('shell')


@pytest.mark.skipif(os.name != 'nt', reason='Requires real Windows process handles')
def test_windows_guard_survives_parent_only_until_cleanup(tmp_path):
    from review_agent.windows_process import process_alive
    pidfile = tmp_path/'pid.txt'
    code = ("import subprocess,sys,time;from pathlib import Path;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            f"Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(60)")
    guard = str(runtime.REPO/'review_agent/engine_guard.py')
    launcher = f"import subprocess,sys,os,time;subprocess.Popen([sys.executable,{guard!r},'30',str(os.getpid()),sys.executable,'-c',{code!r}]);time.sleep(60)"
    parent = subprocess.Popen([sys.executable,'-c',launcher], stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        until=time.monotonic()+5
        while not pidfile.exists() and time.monotonic()<until: time.sleep(.02)
        assert pidfile.exists()
        pid=int(pidfile.read_text())
        parent.kill();parent.wait(3)
        until=time.monotonic()+5
        while process_alive(pid) and time.monotonic()<until: time.sleep(.02)
        assert not process_alive(pid)
    finally:
        if parent.poll() is None: parent.kill();parent.wait(3)

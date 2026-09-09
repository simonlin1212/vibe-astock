import json
import time
from pathlib import Path

import pytest

from review_agent.access import Access, public_login_url
from review_agent.evidence import EvidenceError
from review_agent.runtime import Runtime


def wait(access):
    deadline = time.monotonic() + 5
    while access.busy() and time.monotonic() < deadline:
        time.sleep(.02)
    assert not access.busy()
    return access.snapshot()


def test_official_login_url_only():
    assert public_login_url('https://auth.openai.com/oauth/authorize?state=abc')
    for url in ['http://auth.openai.com', 'https://auth.openai.com.evil.com', 'https://secret@auth.openai.com', 'file:///tmp/a']:
        with pytest.raises(EvidenceError):
            public_login_url(url)


def test_probe_failure_preserves_previous_auth_and_never_echoes_key(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    auth = runtime.home / 'auth.json'
    auth.write_text('previous-login-canary')
    access = Access(runtime)
    def failing(*args, **kwargs):
        raise RuntimeError('SECRET_CANARY')
    monkeypatch.setattr(Runtime, 'run', failing)
    access.start('probe', {'provider':'openai'}, 'SECRET_CANARY')
    state = wait(access)
    assert state['status'] == 'failed' and 'SECRET_CANARY' not in json.dumps(state)
    assert auth.read_text() == 'previous-login-canary'


def test_only_one_access_operation_and_cancellation(tmp_path, monkeypatch):
    access = Access(Runtime(tmp_path))
    def probe(*args):
        access.cancel.wait(3)
        raise EvidenceError('cancelled')
    monkeypatch.setattr(access, '_probe', probe)
    access.start('probe', {}, '')
    with pytest.raises(EvidenceError, match='已有'):
        access.start('login')
    assert access.stop()['status'] == 'cancelled'


def test_login_protocol_uses_staging_and_installs_only_on_success(tmp_path, monkeypatch):
    import sys
    import review_agent.access as module
    fake = tmp_path / 'engine.py'
    fake.write_text('''import sys,json,os
from pathlib import Path
for line in sys.stdin:
 e=json.loads(line);i=e.get('id')
 if i==1:r={}
 elif i==2:
  r={'loginId':'attempt','authUrl':'https://auth.openai.com/oauth/authorize'}
 elif i==3:
  Path(os.environ['CODEX_HOME'],'auth.json').write_text('product-only-canary')
  r={'account':{'type':'chatgpt'}}
 else:continue
 print(json.dumps({'id':i,'result':r}),flush=True)
 if i==2:print(json.dumps({'method':'account/login/completed','params':{'loginId':'attempt','success':True}}),flush=True)
''')
    monkeypatch.setattr(module, 'engine_command', lambda:[sys.executable,str(fake)])
    runtime=Runtime(tmp_path/'product')
    access=Access(runtime)
    access.start('login')
    assert wait(access)['status']=='complete'
    assert (runtime.home/'auth.json').read_text()=='product-only-canary'
    assert not list(runtime.root.glob('login-*'))


def test_shutdown_retains_instance_lock_while_access_worker_alive(tmp_path, monkeypatch):
    from review_agent.api import Manager
    from review_agent.store import Store
    root=tmp_path/'state'
    manager=Manager(Store(root),tmp_path,Runtime(root))
    monkeypatch.setattr(manager.access,'stop',lambda:None)
    monkeypatch.setattr(manager.access,'busy',lambda:True)
    manager.shutdown()
    with pytest.raises(EvidenceError,match='另一个'):
        Manager(Store(root),tmp_path,Runtime(root))
    monkeypatch.setattr(manager.access,'busy',lambda:False)
    manager.shutdown()
    reopened=Manager(Store(root),tmp_path,Runtime(root))
    reopened.shutdown()


def test_model_catalog_uses_product_home_and_only_public_fields(tmp_path, monkeypatch):
    import sys
    import review_agent.runtime as module
    fake = tmp_path / 'models.py'
    fake.write_text('''import sys,json,os
for line in sys.stdin:
 e=json.loads(line)
 if e['method']=='initialize':
  assert e['id']==1
  print(json.dumps({'id':1,'result':{}}),flush=True)
 elif e['method']=='model/list':
  assert e['params']=={'limit':100}
  assert os.environ['CODEX_HOME'].replace(chr(92), '/').endswith('product/codex-home')
  message=json.dumps({'id':2,'result':{'data':[
   {'model':'account-model','isDefault':True,'private':'SECRET_CANARY'},
   {'model':'other-model','isDefault':False}]}})+'\\n'
  # Stream fragments must be reassembled rather than parsed independently.
  sys.stdout.write(message[:17]);sys.stdout.flush()
  sys.stdout.write(message[17:]);sys.stdout.flush()
''')
    monkeypatch.setattr(module, 'engine_command', lambda: [sys.executable, str(fake)])
    runtime = Runtime(tmp_path / 'product')
    result = module.subscription_models(runtime.home)
    assert result == {'models': [{'model':'account-model','is_default':True},
                                {'model':'other-model','is_default':False}], 'default_model':'account-model'}
    assert 'SECRET_CANARY' not in json.dumps(result)


@pytest.mark.parametrize('data', [[], [{'model':'bad model'}], None])
def test_model_catalog_rejects_invalid_results(tmp_path, monkeypatch, data):
    import sys
    import review_agent.runtime as module
    fake = tmp_path / 'models.py'
    fake.write_text('import sys,json\nfor line in sys.stdin:\n e=json.loads(line)\n'
                    ' if e.get("id"): print(json.dumps({"id":e["id"],"result":{"data":'
                    + repr(data) + '}}),flush=True)\n')
    monkeypatch.setattr(module, 'engine_command', lambda: [sys.executable, str(fake)])
    with pytest.raises(EvidenceError, match='模型列表'):
        module.subscription_models(Runtime(tmp_path / 'product').home)


def test_model_catalog_timeout_stops_process(tmp_path, monkeypatch):
    import os
    import sys
    import review_agent.runtime as module
    fake = tmp_path / 'models.py'
    pid = tmp_path / 'pid'
    fake.write_text(f'import os,time\nopen({str(pid)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
    monkeypatch.setattr(module, 'engine_command', lambda: [sys.executable, str(fake)])
    started = time.monotonic()
    with pytest.raises(EvidenceError, match='超时'):
        module.subscription_models(Runtime(tmp_path / 'product').home, timeout=.5)
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_model_catalog_failure_does_not_clear_logged_in_state(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import review_agent.runtime as module
    monkeypatch.setattr(module, 'CLI', Path(__file__))
    monkeypatch.setattr(module.shutil, 'which', lambda _: '/usr/bin/node')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'Logged in using ChatGPT', stderr=b''))
    def failing(*args):
        raise ValueError('SECRET_CANARY')
    monkeypatch.setattr(module, 'subscription_models', failing)
    result = Runtime(tmp_path / 'product').status()
    assert result['subscription_ready'] is True
    assert result['models'] == [] and result['default_model'] == ''
    assert result['models_error'] and 'SECRET_CANARY' not in json.dumps(result)

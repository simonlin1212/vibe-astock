"""Issue #13 storage relocation and #12 dated lockup facts."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_custom_business_root_reaches_all_writers(tmp_path):
    root = tmp_path / 'D 盘资料'
    env = {**os.environ, 'ASTOCK_DATA_HOME': str(root)}
    code = '''
from pathlib import Path
from duanxian import review_store, reflection, journal, data, breadth, backtest, stats_context
from duanxian.paths import data_path
import os
root=Path(os.environ['ASTOCK_DATA_HOME'])
for path in [review_store.DIR,review_store.REJECT_DIR,reflection._REVIEW_DIR,reflection._REFLECT_DIR,journal._DIR,data._LEADER_DIR,data._PREV_POOL_DIR,breadth._CACHE_DIR,backtest.RESULT_DIR,data_path('cache')]:
 assert Path(path).is_relative_to(root), path
review_store.save({'focus': {'text':'synthetic'}, 'target_date':'2026-09-08'}, '2026-09-08')
assert (root/'reviews/2026-09-08.json').is_file()
assert not (root/'journal/trades.json').exists()
'''
    proc = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert (root / 'reviews/latest.json').is_file()


def test_default_root_unchanged_and_relative_override_rejected(monkeypatch):
    from duanxian.paths import data_path
    monkeypatch.delenv('ASTOCK_DATA_HOME', raising=False)
    assert Path(data_path('reviews')) == Path.home()/'.duanxian-agents/reviews'
    monkeypatch.setenv('ASTOCK_DATA_HOME', '')
    assert Path(data_path()) == Path.home()/'.duanxian-agents'
    monkeypatch.setenv('ASTOCK_DATA_HOME', 'relative-folder')
    with pytest.raises(ValueError, match='绝对路径'):
        data_path()


def test_lockup_history_never_includes_future_and_ratio_preserves_units(monkeypatch):
    from vr import astock
    calls=[]
    def query(*args, **kwargs):
        calls.append(kwargs)
        if "FREE_DATE<'2026-09-09'" in kwargs['filter_str']:
            return [{'FREE_DATE':'2026-09-08 00:00:00','TOTAL_RATIO':0.25, 'FREE_RATIO':0.5}]
        return [{'FREE_DATE':'2026-09-10 00:00:00','TOTAL_RATIO':0.25, 'FREE_RATIO':0.5}]
    monkeypatch.setattr(astock, 'eastmoney_datacenter', query)
    result=astock.lockup_expiry('000001','2026-09-09')
    assert result['history'][0]['date'] == '2026-09-08'
    assert result['upcoming'][0]['date'] == '2026-09-10'
    assert result['upcoming'][0]['ratio'] == 25
    assert result['upcoming'][0]['shares'] is None
    assert all(c.get('strict') for c in calls)


def test_lockup_network_failure_is_not_empty_calendar(monkeypatch):
    from vr import astock
    def fail(*args, **kwargs): raise TimeoutError('network')
    monkeypatch.setattr(astock, 'em_get', fail)
    with pytest.raises(RuntimeError, match='数据中心'):
        astock.lockup_expiry('000001','2026-09-09')


@pytest.mark.parametrize('payload,empty', [
    ({'success':False,'code':9201,'message':'返回数据为空','result':None}, True),
    ({'success':True,'result':{'data':[]}}, True),
    ({'success':False,'code':500,'message':'server error','result':None}, False),
    ({'success':True,'result':None}, False),
])
def test_only_explicit_empty_data_is_no_unlock(monkeypatch,payload,empty):
    from vr import astock
    from types import SimpleNamespace
    monkeypatch.setattr(astock,'em_get',lambda *a,**k:SimpleNamespace(json=lambda:payload))
    if empty:
        assert astock.lockup_expiry('000001','2026-09-09') == {'history':[], 'upcoming':[]}
    else:
        with pytest.raises(RuntimeError): astock.lockup_expiry('000001','2026-09-09')


def test_daily_progress_freezing_tuple_and_cancel(tmp_path, monkeypatch):
    from review_agent import public_worker
    from review_agent.daily import FrozenInputs
    from review_agent.evidence import EvidenceError
    stages=[];calls=[];cancelled=False
    def check():
        if cancelled: raise EvidenceError('cancelled')
        return 50
    def fetch(name,args,directory,check,**kwargs):
        calls.append((name,args,kwargs['timeout']))
        return ['真实日期材料', {'value':1}]
    monkeypatch.setattr(public_worker,'fetch_public',fetch)
    inputs=FrozenInputs('2026-09-08',tmp_path,check,stages.append)
    assert inputs.get_market_facts('2026-09-08') == ('真实日期材料',{'value':1})
    inputs.get_market_facts('2026-09-08')
    assert len(calls)==len(stages)==1 and calls[0][2]==90
    cancelled=True
    with pytest.raises(EvidenceError): inputs.get_sentiment_data('2026-09-08')
    assert len(calls)==1


@pytest.mark.parametrize('window,expected', [('upcoming',('2026-09-09','2026-09-18')),('recent',('2026-08-31','2026-09-09'))])
def test_ten_day_calendar_date_range_units_and_missing_data(monkeypatch,window,expected):
    from vr import astock
    captured=[]
    def fetch(*args,**kwargs):
        captured.append(kwargs)
        return [{'FREE_DATE':'2026-09-09 00:00:00','SECURITY_CODE':'000001','SECURITY_NAME_ABBR':'示例', 'CURRENT_FREE_SHARES':150,'FREE_SHARES':9000,'TOTAL_RATIO':0.015,'FREE_RATIO':0.5}]
    monkeypatch.setattr(astock,'eastmoney_datacenter',fetch)
    result=astock.lockup_calendar(window,'2026-09-09')
    assert (result['start'],result['end'])==expected
    assert result['events'][0]['shares']==150 and result['events'][0]['ratio']==1.5
    assert result['events'][0]['able_shares'] is None
    assert captured[0]['strict'] and captured[0]['page_size']==500
    assert 'SECURITY_CODE=' not in captured[0]['filter_str']


@pytest.mark.parametrize('mode',['timeout','cancel'])
def test_daily_fetch_stuck_network_is_terminated(tmp_path,monkeypatch,mode):
    import time
    from review_agent.public_worker import fetch_public
    from review_agent.evidence import EvidenceError
    original=subprocess.Popen; processes=[]
    fake=tmp_path/'stuck.py';fake.write_text('import time\ntime.sleep(30)\n')
    def launch(argv,**kwargs):
        assert kwargs['env']['ASTOCK_DATA_HOME']==str(tmp_path)
        proc=original([*argv[:5],sys.executable,str(fake)],**kwargs)
        processes.append(proc);return proc
    monkeypatch.setenv('ASTOCK_DATA_HOME',str(tmp_path))
    monkeypatch.setattr('review_agent.public_worker.subprocess.Popen',launch)
    start=time.monotonic()
    def check():
        if mode=='cancel' and time.monotonic()-start>0.3: raise EvidenceError('cancelled')
        return 5
    with pytest.raises(EvidenceError): fetch_public('get_sentiment_data',['2026-09-08'],tmp_path,check,timeout=.5)
    assert time.monotonic()-start < 4 and processes[0].poll() is not None
    assert not list(tmp_path.glob('worker-*.jsonl'))

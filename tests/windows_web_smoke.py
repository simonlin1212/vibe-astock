"""Native CI-only fresh-install web smoke. No user accounts or model requests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('Run only on the disposable Windows Actions runner')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'

    def get(path):
        with opener.open(url + path, timeout=2) as response:
            assert response.status == 200
            return response.read(2_000_000)

    with tempfile.TemporaryDirectory(prefix='astock-web-native-') as tmp:
        env = {**os.environ, 'ASTOCK_AGENT_HOME': str(Path(tmp)/'agent'),
               'VR_DATA_DIR': str(Path(tmp)/'market'), 'VR_REPORTS_DIR': str(Path(tmp)/'reports')}
        for attempt in range(2):
            with (Path(tmp)/f'launcher-{attempt}.log').open('wb') as output:
                p = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT/'scripts/manage.py'),
                    'start', '--no-browser', '--port', str(port)], cwd=ROOT, env=env,
                    stdout=output, stderr=output, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                try:
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        if p.poll() is not None:
                            raise AssertionError(f'Launcher exited early: {p.returncode}')
                        try:
                            health = json.loads(get('/api/astock/health'))
                            if health.get('service') == 'vibe-astock' and health.get('ready') is True:
                                break
                        except (OSError, ValueError, urllib.error.URLError):
                            pass
                        time.sleep(.25)
                    else:
                        raise AssertionError('Web readiness timed out')
                    assert b'<html' in get('/').lower()
                    assert b'<html' in get('/settings').lower()
                    status = json.loads(get('/api/review-agent/status'))
                    assert status['installed'] is True, status
                    assert status['subscription_ready'] is False
                    print(json.dumps({'attempt': attempt+1, 'web_ready': True,
                                      'html_routes': 2, 'engine_installed': True}), flush=True)
                except Exception:
                    output.flush()
                    print((Path(tmp)/f'launcher-{attempt}.log').read_text(encoding='utf-8', errors='replace')[-12000:], flush=True)
                    raise
                finally:
                    if p.poll() is None:
                        p.kill()  # Simulate forced terminal/launcher termination.
                    p.wait(timeout=10)
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                with socket.socket() as probe:
                    probe.settimeout(1)
                    if probe.connect_ex(('127.0.0.1', port)) != 0:
                        break
                time.sleep(.1)
            else:
                raise AssertionError('Backend survived launcher death')
        print('PASS: fresh install, doctor, real HTTP UI/API, parent-death cleanup and same-port restart')


if __name__ == '__main__':
    main()

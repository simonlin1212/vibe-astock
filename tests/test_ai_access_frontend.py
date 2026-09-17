"""前端 AI 接入配置的行为契约。"""

import subprocess
from pathlib import Path


def test_cc_switch_route_has_editable_model_preset():
    script = """
    const m = await import('./frontend/src/lib/ai-access.ts');
    const entry = m.API_PROVIDERS.find(p => p.id === 'cc-switch');
    if (!entry || entry.name !== 'CC Switch 本机路由' || entry.baseURL !== 'http://127.0.0.1:15721/v1') process.exit(2);
    if (entry.models.length !== 0 || !entry.detail.includes('PROXY_MANAGED')) process.exit(3);
    if (m.draftFor('cc-switch').apiKey !== 'PROXY_MANAGED') process.exit(4);
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=Path(__file__).parents[1])
    assert result.returncode == 0


def test_frontend_only_allows_exact_cc_switch_http_route():
    script = """
    const m = await import('./frontend/src/lib/ai-access.ts');
    const valid = [
      {provider: 'api-compatible', model: 'glm-5.3', baseURL: 'http://127.0.0.1:15721/v1', apiKey: 'PROXY_MANAGED'},
      {provider: 'api-compatible', model: 'm', baseURL: 'https://example.com/v1', apiKey: 'k'},
    ];
    const invalid = [
      {provider: 'api-compatible', model: 'glm-5.3', baseURL: 'http://127.0.0.1:15722/v1', apiKey: 'PROXY_MANAGED'},
      {provider: 'api-compatible', model: 'm', baseURL: 'http://localhost:15721/v1', apiKey: 'k'},
      {provider: 'api-compatible', model: 'm', baseURL: 'https://user@example.com/v1', apiKey: 'k'},
      {provider: 'api-compatible', model: 'm', baseURL: 'https://example.com/v1?x=1', apiKey: 'k'},
    ];
    if (valid.some(x => m.draftError(x) !== '')) process.exit(2);
    if (invalid.some(x => m.draftError(x) === '')) process.exit(3);
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=Path(__file__).parents[1])
    assert result.returncode == 0


def test_switching_provider_keeps_preset_placeholder_key():
    src = Path('frontend/src/components/AgentAccess.tsx').read_text(encoding='utf-8')
    assert 'setApiKey(next.apiKey);' in src

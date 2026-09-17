import pytest
from review_agent.runtime import connection, config_for
from review_agent.evidence import EvidenceError

@pytest.mark.parametrize('url', [
    'https://api.deepseek.com', 'https://openrouter.ai/api/v1',
    'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
    'https://api.siliconflow.cn/v1', 'https://api.minimaxi.com/v1',
    'https://api.groq.com/openai/v1', 'https://api.together.xyz/v1',
])
def test_api_preset_reaches_responses_engine_unchanged(tmp_path, url):
    source, key = connection({'provider':'api-compatible','baseURL':url,'model':'custom/model','apiKey':'TEST_ONLY_KEY'})
    config = config_for(tmp_path, source)
    assert config['model_providers.astock_api']['base_url'] == url
    assert config['model_providers.astock_api']['wire_api'] == 'responses'
    assert source['model'] == 'custom/model'
    assert key == 'TEST_ONLY_KEY' and key not in repr(config)


def test_cc_switch_local_route_reaches_responses_engine(tmp_path):
    source, key = connection({
        'provider': 'api-compatible', 'baseURL': 'http://127.0.0.1:15721/v1/',
        'model': 'glm-5.3-flash', 'apiKey': 'PROXY_MANAGED',
    })
    config = config_for(tmp_path, source)
    assert source == {'provider': 'api-compatible', 'model': 'glm-5.3-flash',
                      'baseURL': 'http://127.0.0.1:15721/v1'}
    assert config['model_providers.astock_api']['wire_api'] == 'responses'
    assert key == 'PROXY_MANAGED'


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:15721',
    'http://127.0.0.1:15721/v2',
    'http://127.0.0.1:15722/v1',
    'http://localhost:15721/v1',
    'http://192.168.1.8:15721/v1',
])
def test_other_local_http_routes_remain_rejected(url):
    with pytest.raises(EvidenceError):
        connection({'provider': 'api-compatible', 'baseURL': url,
                    'model': 'glm-5.3', 'apiKey': 'PROXY_MANAGED'})


@pytest.mark.parametrize('url', ['https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', 'https://host/<workspace>/v1'])
def test_unfilled_endpoint_placeholder_is_rejected(url):
    with pytest.raises(EvidenceError):
        connection({'provider':'api-compatible','baseURL':url,'model':'qwen','apiKey':'TEST_ONLY_KEY'})

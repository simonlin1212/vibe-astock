import test from 'node:test';
import assert from 'node:assert/strict';
import { API_PROVIDERS, draftFor, connectionFor, providerFor, sourceLabel, draftError } from '../src/lib/ai-access.ts';

test('Research API choices retain their endpoint and model through submission', () => {
  assert.deepEqual(API_PROVIDERS.map(p=>p.id), ['deepseek','mimo','glm','kimi','qwen','openai','silicon','minimax','openrouter','groq','together','api-compatible']);
  for (const preset of API_PROVIDERS.filter(p=>p.id!=='api-compatible')) {
    const draft=draftFor(preset.id);
    const base=draft.baseURL.replace('{WorkspaceId}','test-workspace');
    const connection=connectionFor({...draft,baseURL:base,apiKey:'TEST_ONLY_KEY'});
    assert.equal(connection.baseURL,base);
    assert.equal(connection.model,preset.models[0].id);
    assert.equal(connection.apiKey,'TEST_ONLY_KEY');
    assert.equal(connection.provider, ['openai','mimo'].includes(preset.id)?preset.id:'api-compatible');
    assert.equal(providerFor(connection),preset.id);
    assert.equal(sourceLabel(connection),preset.name+' API');
  }
});

test('switching source never carries another providers key or endpoint', () => {
  const saved={provider:'mimo',model:'my-model',baseURL:'https://token-plan-cn.xiaomimimo.com/v1',apiKey:'PRIVATE_TEST_KEY'};
  const deepseek=draftFor('deepseek',saved);
  assert.equal(deepseek.apiKey,'');assert.equal(deepseek.baseURL,'https://api.deepseek.com');
  assert.equal(draftFor('mimo',saved).apiKey,'PRIVATE_TEST_KEY');
  assert.equal(draftFor('mimo',saved).model,'my-model');
  for (const id of ['claude','codebuddy','codex-private']) {
    const c=connectionFor(draftFor(id,saved));assert.equal(c.apiKey,'');assert.equal(c.baseURL,'');
  }
});

test('custom addresses and custom models survive without misleading source labels', () => {
  const saved={provider:'api-compatible',model:'custom/model',baseURL:'https://gateway.example/v1',apiKey:'TEST_KEY'};
  assert.equal(providerFor(saved),'api-compatible');
  assert.deepEqual(connectionFor(draftFor('api-compatible',saved)),saved);
  assert.equal(sourceLabel(saved),'自定义 API');
  assert.equal(providerFor({...saved,baseURL:'https://api.deepseek.com.evil.test'}),'api-compatible');
  assert.equal(providerFor({...saved,baseURL:'https://user:pass@api.deepseek.com'}),'api-compatible');
});

test('incomplete or unsafe API fields cannot start a paid probe', () => {
  const d={...draftFor('qwen'),apiKey:'TEST_KEY'};
  assert.match(draftError(d),/WorkspaceId/);
  for(const baseURL of ['https:example.com','https:/example.com','http://host/v1','https://user:pass@host/v1','https://host/v1?key=x','https://host/v1#x','https://host:444/v1']) {
    assert.ok(draftError({...d,baseURL}));
  }
  assert.equal(draftError({...d,baseURL:'https://mine.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'}),'');
  // 本地回环/内网 http 放行(自托管模型网关 cc-switch 127.0.0.1:15721、局域网 vLLM 192.168.x:8000、localhost)
  assert.equal(draftError({...d,baseURL:'http://127.0.0.1:15721/v1'}),'');
  assert.equal(draftError({...d,baseURL:'http://192.168.250.10:8000/v1'}),'');
  assert.equal(draftError({...d,baseURL:'http://localhost/v1'}),'');
  assert.ok(draftError({...draftFor('deepseek'),apiKey:''}));
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { API_PROVIDERS, draftFor, connectionFor, providerFor, sourceLabel, draftError } from '../src/lib/ai-access.ts';

test('Research API choices retain their endpoint and model through submission', () => {
  assert.deepEqual(API_PROVIDERS.map(p=>p.id), ['deepseek','mimo','cc-switch','glm','kimi','qwen','openai','silicon','minimax','openrouter','groq','together','api-compatible']);
  // 判据是「带预设模型的项」而不是「除了 api-compatible」—— cc-switch 同样没有预设模型
  // (模型取决于用户本机路由怎么配)，下面 preset.models[0].id 对它会直接抛。
  for (const preset of API_PROVIDERS.filter(p=>p.models.length>0)) {
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

test('CC Switch 预设带占位密钥、留空模型，并能按地址认回自己', () => {
  const draft = draftFor('cc-switch');
  assert.equal(draft.baseURL, 'http://127.0.0.1:15721/v1');
  // 本机代理持有真实密钥，客户端填占位符；不预填的话用户会撞上「请填写有效 API 密钥」。
  assert.equal(draft.apiKey, 'PROXY_MANAGED');
  // 模型必须留空：路由后面接什么模型只有用户知道，预填一个会诱导用户直接保存。
  assert.equal(draft.model, '');
  assert.match(draftError(draft), /模型/, '模型留空时必须挡住保存');

  const connection = connectionFor({...draft, model: 'glm-5.3-flash'});
  assert.equal(draftError({...draft, model: 'glm-5.3-flash'}), '');
  // 后端按 api-compatible 收，才能跳过云端点白名单走本机地址。
  assert.equal(connection.provider, 'api-compatible');
  // 存盘后重开设置页要能认回 CC Switch，而不是掉回「自定义」。
  assert.equal(providerFor(connection), 'cc-switch');
  assert.equal(sourceLabel(connection), 'CC Switch 本机路由 API');

  // 其它预设不受影响：只有 cc-switch 预填密钥。
  for (const id of ['deepseek','mimo','glm','api-compatible']) assert.equal(draftFor(id).apiKey, '', id);
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
  // 未指定地址必须能存:后端 v1.1.3 起放行它,这里判窄了「测试连接并保存」按钮就是灰的,
  // 而网关监听所有网卡时启动日志打印的正是 http://0.0.0.0:<端口>。
  assert.equal(draftError({...d,baseURL:'http://0.0.0.0:8000/v1'}),'');
  // 阴性对照:只放 0.0.0.0 这一个,0.0.0.0/8 的其余部分仍须挡住(与后端白名单同口径)。
  assert.ok(draftError({...d,baseURL:'http://0.0.0.1:8000/v1'}),'0.0.0.1');
  // 内网前缀打头的**域名**不是内网:这些 host 会解析到公网,http 必须挡住。
  // 判据挂在"整串是不是那几段 IPv4",不是"以 192.168. 开头"。
  for (const baseURL of ['http://192.168.1.1.evil.com/v1','http://10.foo.example.com/v1','http://127.0.0.1.evil.com/v1'])
    assert.ok(draftError({...d,baseURL}), baseURL);
  assert.ok(draftError({...draftFor('deepseek'),apiKey:''}));
});

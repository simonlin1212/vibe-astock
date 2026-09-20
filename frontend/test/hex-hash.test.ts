import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { hexHash } from '../src/lib/random-id.ts';

// 「问 AI」用 hexHash 给每次请求做幂等指纹，存进 sessionStorage 的 key。
// 原本用 await crypto.subtle.digest，而非安全上下文（http://<LAN-IP>:8910，
// http 只对 localhost 算安全）下 crypto.subtle 是 undefined，直接抛 TypeError，
// 于是经局域网 IP 打开时任何提问都报「对话失败」且请求根本发不出去。

test('hexHash 在没有 WebCrypto 的环境里也能算出指纹', () => {
  // 模拟非安全上下文：crypto 存在但 subtle 不可用。
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  Object.defineProperty(globalThis, 'crypto', { value: {}, configurable: true });
  try {
    assert.equal(hexHash('{"question":"茅台怎么样"}').length, 32);
  } finally {
    if (original) Object.defineProperty(globalThis, 'crypto', original);
    else delete (globalThis as Record<string, unknown>).crypto;
  }
});

test('同样的请求身份得到同样的指纹，不同的身份必须分开', () => {
  const identity = (over: Record<string, unknown> = {}) => JSON.stringify({
    session: 'single-question', question: '茅台怎么样', context: '页面上下文',
    allow_tools: true, llm: { model: 'glm-5.3' }, ...over,
  });

  assert.equal(hexHash(identity()), hexHash(identity()), '同一请求两次指纹必须一致，否则幂等复用失效');

  // 这几个维度任意一个变了都必须换指纹——否则换了问题却复用上一问的待定结果。
  const variants: Record<string, Record<string, unknown>> = {
    问题: { question: '宁德时代怎么样' },
    上下文: { context: '另一个页面' },
    模型: { llm: { model: 'glm-5.3-flash' } },
    会话: { session: 'other-session' },
    工具开关: { allow_tools: false },
  };
  const base = hexHash(identity());
  for (const [label, over] of Object.entries(variants)) {
    assert.notEqual(hexHash(identity(over)), base, `换了${label}却得到相同指纹，会误复用上一次的请求`);
  }
});

test('指纹恒为 32 位十六进制，长文本与非 ASCII 都不例外', () => {
  for (const input of ['', 'a', '中'.repeat(8000), JSON.stringify({ q: '带 emoji 🚀 的问题' })]) {
    assert.match(hexHash(input), /^[0-9a-f]{32}$/, `输入长度 ${input.length} 时指纹格式不对`);
  }
});

test('chatStream 不得回退到 crypto.subtle', () => {
  // 直接钉住源码：这个回归不会让任何行为测试变红（有 WebCrypto 的环境里
  // crypto.subtle 工作正常），只在用户用局域网 IP 打开时才炸。
  const source = readFileSync(new URL('../src/lib/llm.ts', import.meta.url), 'utf8');
  assert.ok(source.includes('hexHash'), 'llm.ts 应当用 hexHash 生成幂等指纹');
  assert.doesNotMatch(source, /crypto\.subtle/, 'llm.ts 不能依赖 crypto.subtle：非安全上下文下它是 undefined');
});

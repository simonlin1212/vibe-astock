/** Request identity only; never used as an authentication secret. */
export function randomId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let i=0; i<bytes.length; i++) bytes[i] = Math.floor(Math.random()*256);
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

/** 同步 32-hex 内容指纹。只用 JS 字符串运算 —— 非安全上下文（http://IP:port 访问）
 *  下 crypto.subtle 是 undefined，await subtle.digest 会直接抛 TypeError，
 *  曾导致「问 AI」在 LAN IP 上必然"对话失败"且请求发不出去。 */
export function hexHash(input: string): string {
  let h1 = 0x811c9dc5, h2 = 0xcbf29ce4;
  for (let i = 0; i < input.length; i++) {
    const c = input.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193);
    h2 = Math.imul(h2 ^ Math.imul(c, 0x85ebca6b), 0xc2b2ae35);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 0x27d4eb2f); h1 ^= Math.imul(h2 ^ (h2 >>> 15), 0x165667b1);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 0x27d4eb2f); h2 ^= Math.imul(h1 ^ (h1 >>> 15), 0x165667b1);
  const a = (h1 >>> 0).toString(16).padStart(8, '0');
  const b = (h2 >>> 0).toString(16).padStart(8, '0');
  const c = (Math.imul(h1, h2) >>> 0).toString(16).padStart(8, '0');
  const d = (Math.imul(h1 ^ 0x9e3779b9, h2 ^ 0x85ebca77) >>> 0).toString(16).padStart(8, '0');
  return a + b + c + d;
}

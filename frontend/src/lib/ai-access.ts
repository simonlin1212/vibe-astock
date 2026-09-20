// UI presets share the existing Responses adapter; saved backend identities stay compatible.
type Connection = { provider: string; model: string; baseURL: string; apiKey: string };
export type AccessDraft = Connection;
const bailian = 'https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1';
const hosted = '通过阿里云百炼接入，请使用百炼工作空间的地址和密钥。';
const ccSwitch = 'http://127.0.0.1:15721/v1';
export const SUBSCRIPTION_PROVIDERS = [
  { id: 'codex-private', name: 'Codex 订阅', detail: '使用 ChatGPT 账户登录产品专用空间' },
  { id: 'claude', name: 'Claude 订阅', detail: '使用本机 Claude Code 登录' },
  { id: 'codebuddy', name: 'WorkBuddy 订阅', detail: '使用 WorkBuddy 内置 CodeBuddy 登录；macOS 已完成单日复盘流程实测' },
];
const preset = (id: string, name: string, baseURL: string, models: string[], detail = '填写该服务商的 API 密钥，可按账户权限修改模型。') =>
  ({ id, name, baseURL, models: models.map(id => ({ id, name: id })), detail });
// Same starting choices as Research. A preset is not a successful connection test.
export const API_PROVIDERS = [
  preset('deepseek', 'DeepSeek', 'https://api.deepseek.com', ['deepseek-v4-flash', 'deepseek-v4-pro']),
  preset('mimo', 'MiMo', 'https://token-plan-cn.xiaomimimo.com/v1', ['mimo-v2.5', 'mimo-v2.5-pro']),
  preset('cc-switch', 'CC Switch 本机路由', ccSwitch, [],
    'CC Switch 需在本机开启 Responses 路由；API 密钥通常填 PROXY_MANAGED，模型按路由实际配置自行填写。'),
  preset('glm', '智谱 GLM', bailian, ['glm-5.2'], hosted),
  preset('kimi', 'Kimi', bailian, ['kimi-k2.7-code'], hosted),
  preset('qwen', '通义千问', bailian, ['qwen3.8-max'], hosted),
  preset('openai', 'OpenAI', 'https://api.openai.com/v1', ['gpt-4o']),
  preset('silicon', '硅基流动', 'https://api.siliconflow.cn/v1', ['deepseek-ai/DeepSeek-V3']),
  preset('minimax', 'MiniMax', 'https://api.minimaxi.com/v1', ['MiniMax-M2']),
  preset('openrouter', 'OpenRouter', 'https://openrouter.ai/api/v1', ['openai/gpt-4o']),
  preset('groq', 'Groq', 'https://api.groq.com/openai/v1', ['llama-3.3-70b-versatile']),
  preset('together', 'Together', 'https://api.together.xyz/v1', ['meta-llama/Llama-3.3-70B-Instruct-Turbo']),
  preset('api-compatible', '自定义', '', [], '填写支持 Responses 与工具调用的服务地址和模型标识。'),
];
export function isSubscription(provider: string): boolean {
  return SUBSCRIPTION_PROVIDERS.some(p => p.id === provider);
}
function cleanURL(base: string): string {
  return base.trim().replace(/\/+$/, '');
}
export function providerFor(connection: Connection): string {
  if (isSubscription(connection.provider)) return connection.provider;
  const base = cleanURL(connection.baseURL);
  const exact = API_PROVIDERS.find(p => p.baseURL && p.baseURL !== bailian && base === p.baseURL);
  if (exact) return exact.id;
  if (/^https:\/\/[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com\/compatible-mode\/v1$/.test(base)) {
    if (connection.model.startsWith('glm-')) return 'glm';
    if (connection.model.startsWith('kimi-')) return 'kimi';
    if (connection.model.startsWith('qwen')) return 'qwen';
  }
  return 'api-compatible';
}
export function draftFor(provider: string, saved?: Connection | null): AccessDraft {
  if (saved && providerFor(saved) === provider) return { provider, model: saved.model, baseURL: saved.baseURL, apiKey: isSubscription(provider) ? '' : saved.apiKey };
  const entry = API_PROVIDERS.find(p => p.id === provider);
  // cc-switch 由本机代理持有真实密钥，客户端这一侧填占位符即可；后端只要求非空。
  return { provider, model: entry?.models[0]?.id ?? (['claude', 'codebuddy'].includes(provider) ? 'default' : ''),
           baseURL: entry?.baseURL ?? '', apiKey: entry?.id === 'cc-switch' ? 'PROXY_MANAGED' : '' };
}
export function connectionFor(draft: AccessDraft): Connection {
  if (isSubscription(draft.provider)) return { provider: draft.provider, model: draft.model.trim(), baseURL: '', apiKey: '' };
  const baseURL = cleanURL(draft.baseURL);
  const provider = baseURL === 'https://api.openai.com/v1' ? 'openai' : baseURL === 'https://token-plan-cn.xiaomimimo.com/v1' ? 'mimo' : 'api-compatible';
  return { provider, model: draft.model.trim(), baseURL, apiKey: draft.apiKey.trim() };
}
export function sourceLabel(connection: Connection): string {
  const provider = providerFor(connection);
  return SUBSCRIPTION_PROVIDERS.find(p => p.id === provider)?.name ?? `${API_PROVIDERS.find(p => p.id === provider)?.name ?? '自定义'} API`;
}
// 本地回环 / 内网(RFC1918)host 判定 —— 只为在设置页即时给提示,**真正的边界在后端**
// runtime.connection(),那边是权威,这里判错不影响安全。
// 两边不是同一口径,也做不到同一口径:这里拿到的是 new URL 规范化后的 hostname,
// 后端 urlparse 看到的是原串(例如 "127.1" 在这里已经变成 127.0.0.1、后端则不认),
// 所以少数地址会在这里过、被后端拒,用户看到的是后端那句话。反过来后端比这里宽
// (IPv6 唯一本地地址 fd00::/8 后端收、这里不收),那一侧只是提示保守,不会放行更多。
// 自托管模型网关(cc-switch 127.0.0.1:15721)、局域网 vLLM(192.168.x:8000)无 TLS,
// 对这些 host 放行 http 任意端口;公网 host 仍强制 https 标准端口。
const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
function isLocalOrPrivateHost(hostname: string | null): boolean {
  if (!hostname) return false;
  const h = hostname.toLowerCase();
  if (h === 'localhost' || h === '::1' || h === '[::1]') return true;
  // 必须整串匹配:前缀匹配会把 192.168.1.1.evil.com 这种公网域名当成内网。
  const parts = IPV4.exec(h)?.slice(1).map(Number);
  if (!parts || parts.some(n => n > 255)) return false;
  const [a, b] = parts;
  if (a === 127) return true;                   // 127.0.0.0/8 loopback
  if (a === 10) return true;                    // 10.0.0.0/8
  if (a === 192 && b === 168) return true;      // 192.168.0.0/16
  return a === 172 && b >= 16 && b <= 31;       // 172.16.0.0/12
}
export function draftError(draft: AccessDraft): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$/.test(draft.model.trim())) return '请填写有效的模型标识';
  if (isSubscription(draft.provider)) return '';
  const base = draft.baseURL.trim();
  if (/[{}<>]|%7b|%7d|%3c|%3e/i.test(base)) return '请将地址中的 WorkspaceId 等占位符替换为实际工作空间信息';
  if (/\s|\\/.test(base)) return 'API 地址不能包含空白或反斜杠';
  let url: URL;
  try { url = new URL(base); } catch { return '请填写有效的 API 基础地址(含协议)'; }
  if (url.username || url.password || url.search || url.hash) return 'API 地址不能含账号或查询参数';
  // 强制原文以 http:// 或 https:// 开头 —— 拦截 new URL 会洗白的畸形(https:/x、https:x)
  if (!base.startsWith('https://') && !base.startsWith('http://')) return 'API 地址需以 http:// 或 https:// 开头';
  if (isLocalOrPrivateHost(url.hostname)) {
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return '本地/内网地址请使用 http 或 https';
  } else {
    if (url.protocol !== 'https:' || url.port) return '公网 API 地址须为 HTTPS 标准端口;本地/内网模型网关可用 http';
  }
  const key = draft.apiKey.trim();
  if (!key || key.length > 1024 || /\s/.test(key)) return '请填写该服务商的有效 API 密钥';
  return '';
}

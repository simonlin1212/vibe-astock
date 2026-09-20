// UI presets share the existing Responses adapter; saved backend identities stay compatible.
type Connection = { provider: string; model: string; baseURL: string; apiKey: string };
export type AccessDraft = Connection;
const bailian = 'https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1';
const hosted = '通过阿里云百炼接入，请使用百炼工作空间的地址和密钥。';
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
  return { provider, model: entry?.models[0]?.id ?? (['claude', 'codebuddy'].includes(provider) ? 'default' : ''), baseURL: entry?.baseURL ?? '', apiKey: '' };
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
// runtime.connection()。这里刻意比后端窄(只认这几段 IPv4 字面量),窄的一侧是安全的:
// 前端放行的后端一定也放行,反之不然(例如 IPv6 唯一本地地址后端收、这里不收)。
// 自托管模型网关(cc-switch 127.0.0.1:15721)、局域网 vLLM(192.168.x:8000)无 TLS,
// 对这些 host 放行 http 任意端口;公网 host 仍强制 https 标准端口,SSRF 边界不松。
function isLocalOrPrivateHost(hostname: string | null): boolean {
  if (!hostname) return false;
  const h = hostname.toLowerCase();
  if (h === 'localhost' || h === '::1' || h === '[::1]') return true;
  if (/^127\./.test(h)) return true;                // 127.0.0.0/8 loopback
  if (/^10\./.test(h)) return true;                 // 10.0.0.0/8
  if (/^192\.168\./.test(h)) return true;           // 192.168.0.0/16
  if (/^172\.(1[6-9]|2\d|3[01])\./.test(h)) return true; // 172.16.0.0/12
  return false;
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

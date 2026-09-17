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
    'CC Switch 需在本机开启 Responses 路由；API 密钥通常填 PROXY_MANAGED，模型可按路由自行填写。'),
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
export function draftError(draft: AccessDraft): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$/.test(draft.model.trim())) return '请填写有效的模型标识';
  if (isSubscription(draft.provider)) return '';
  const base = draft.baseURL.trim();
  if (/[{}<>]|%7b|%7d|%3c|%3e/i.test(base)) return '请将地址中的 WorkspaceId 等占位符替换为实际工作空间信息';
  try {
    const url = new URL(base);
    const localResponsesRoute = base === ccSwitch;
    const secureResponsesRoute = /^https:\/\//.test(base) && url.protocol === 'https:' && !url.port;
    if (url.username || url.password || url.search || url.hash || /\s|\\/.test(base) ||
        (!localResponsesRoute && !secureResponsesRoute)) throw new Error();
  } catch { return '请填写 HTTPS API 基础地址，或本机 CC Switch Responses 路由'; }
  const key = draft.apiKey.trim();
  if (!key || key.length > 1024 || /\s/.test(key)) return '请填写该服务商的有效 API 密钥';
  return '';
}

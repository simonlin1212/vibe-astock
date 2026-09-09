let monitorWarning: string | undefined;
import { apiUrl } from "./base";
import { randomId } from "./random-id";
let monitorId: string | undefined;
let registeredWatch = "";
let registeredAt = 0;
// 后端 API 客户端。/api → vite 代理到本仓库的 FastAPI（server.py，默认 8910）。
// 后端未启动或数据源异常时抛 ApiError，页面据此优雅降级。

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

// 后端访问密钥（对应后端部署时的 VR_API_KEY，公网部署防蹭用）。只存本地浏览器。
const ACCESS_KEY = "vr-access-key";

export function loadAccessKey(): string {
  try {
    return localStorage.getItem(ACCESS_KEY) || "";
  } catch {
    return "";
  }
}

export function saveAccessKey(key: string) {
  try {
    if (key) localStorage.setItem(ACCESS_KEY, key);
    else localStorage.removeItem(ACCESS_KEY);
  } catch {
    throw new Error("未保存：浏览器禁止存储或空间不足，请检查权限后重试");
  }
}

export function authHeaders(): Record<string, string> {
  const k = loadAccessKey();
  return k ? { Authorization: `Bearer ${k}` } : {};
}

export interface MyReport {
  id: string; name: string; industry: string; size: number; ext: string; ts: number;
}

// 下载/预览研报：带鉴权头 fetch → blob → 触发浏览器下载（<a download> 无法带 Authorization，故走 blob）。
export async function downloadReport(id: string, name: string): Promise<void> {
  const resp = await fetch(apiUrl(`/api/myreports/file/${id}`), { headers: authHeaders() });
  if (!resp.ok) throw new ApiError(`下载失败 HTTP ${resp.status}`, resp.status);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function request<T>(path: string, method: "GET" | "POST" | "DELETE" = "GET", body?: unknown): Promise<T> {
  let resp: Response;
  const headers: Record<string, string> = { ...authHeaders() };
  const opts: RequestInit = { method };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (Object.keys(headers).length > 0) opts.headers = headers;
  try {
    resp = await fetch(apiUrl(`/api${path}`), opts);
  } catch {
    throw new ApiError("连接不到后端，请先在项目根目录启动：.venv/bin/python server.py（默认 8910）", 0);
  }
  let payload: any = null;
  try {
    payload = await resp.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      throw new ApiError("后端开启了访问鉴权（VR_API_KEY）：请在「接入 AI」页底部填写后端访问密钥", 401);
    }
    throw new ApiError(payload?.detail || payload?.error || `HTTP ${resp.status}`, resp.status);
  }
  return (payload?.data ?? payload) as T;
}

const get = <T>(path: string) => request<T>(path, "GET");

export interface Quote {
  quote_time?: string | null; amount_wan?: number | null;
  name: string; price: number; last_close: number; change_pct: number;
  pe_ttm: number; pb: number; mcap_yi: number; turnover_pct: number;
  limit_up: number; limit_down: number;
}

export interface Valuation {
  quote_time?: string | null;
  name: string; code: string; price: number; mcap_yi: number;
  pe_ttm: number; pb: number;
  eps_26e: number | null; eps_27e: number | null; pe_26e: number | null;
  cagr_pct: number | null; peg: number | null; digest_years: number | null;
  analyst_count: number; forecast_note?: string;
}

export interface Report {
  title: string; publishDate: string; orgSName: string;
  emRatingName?: string; indvInduName?: string; pdfUrl?: string | null;
}

export interface ValMetric {
  as_of?: string;
  current: number; percentile: number; min: number; max: number;
  p20: number; p50: number; p80: number; n: number;
}
export interface ValPercentile {
  gaps?: string[];
  period: string; metrics: { pe_ttm?: ValMetric; pb?: ValMetric };
}

export interface Announcement {
  date: string; title: string; type: string; url: string;
}

export interface Financials {
  period: string | null;
  revenue: string | null; revenue_yoy: string | null;
  net_profit: string | null; net_profit_yoy: string | null;
  eps: string | null; bvps: string | null; roe: string | null;
  gross_margin: string | null; net_margin: string | null; op_cf_ps: string | null;
}

export interface NewsItem {
  新闻标题?: string; 发布时间?: string; 文章来源?: string; 新闻链接?: string;
}

export interface IndexQuote {
  name: string; price: number; change_pct: number; change_amt: number;
}

export interface MarketSentiment {
  up: number; down: number; flat: number; zt: number; zt_real: number; dt: number; dt_real: number;
  active: string; breadth: string; speculation: string; date: string;
}
export interface SectorFlow {
  name: string; pct: number; net: number; inflow: number; outflow: number; firms: number;
}
export interface MarketOverview {
  sentiment: MarketSentiment; sectors: SectorFlow[]; updated: string;
}

// 短线情绪：连板梯队 / 最高连板 / 炸板率 / 封板率 / 晋级率 / 涨跌停家数 + 连板股清单（客观公开榜单）
export interface EmotionTier { boards: number; count: number; plus: boolean }
export interface LianbanStock {
  code: string; name: string; boards: number;
  price: number; pct: number; amount: number | null; float_cap: number | null; industry: string;
  reason: string;  // 涨停原因题材串（问财；缺 key/失败为空串）
}
export interface ShortTermEmotion {
  date: string;
  zt_count: number; dt_count: number; zb_count: number;
  max_boards: number; lianban_count: number;
  ladder: EmotionTier[];
  lianban_stocks: LianbanStock[];
  seal_rate: number | null; break_rate: number | null; promotion_rate: number | null;
  yzt_count: number;
}

// 每日盯盘：3 秒实时快照（持仓/自选/500亿大票/三板+/昨日成交前十 + 异动流）
export interface WatchRow {
  code: string; name: string;
  price: number | null; pct: number | null; amount: number | null;
  cost?: number; shares?: number; pnl_pct?: number;   // 持仓行
  boards?: number; is_limit?: boolean;                 // 三板+行
}
export interface MonitorAlert {
  change_pct?: number | null;
  ts: string; code: string; name: string; kind: string; msg: string; sources: string[];
}
export interface YesterdayStock extends WatchRow { sector?: string; status: string; quote_ok: boolean; quote_time?: string | null; }
export interface YesterdayLadder {
  available: boolean; reason?: string; warning?: string; sample_date?: string | null; quote_date?: string | null;
  covered: number; stocks: YesterdayStock[];
}
export interface MonitorSnapshot {
  watch_warning?: string;
  warming_up?: boolean;
  ts: string; phase: "open" | "break" | "closed"; poll_seconds: number;
  holdings: WatchRow[]; holdings_error?: string; watchlist: WatchRow[];
  bigcap: { total: number; top: WatchRow[] };
  lianban3: WatchRow[];
  yesterday_ladder?: YesterdayLadder;
  turnover: { label: string; stocks: WatchRow[] };
  alerts: MonitorAlert[];
}

// 首板分析：今日首板涨停股（连板数=1）+ 涨停原因题材串（客观公开榜单）
export interface FirstBoardStock {
  code: string; name: string;
  price: number; pct: number; amount: number | null; float_cap: number | null;
  industry: string; seal_time: string; break_count: number; reason: string;
}
export interface FirstBoardData {
  date: string; total_zt: number; first_count: number;
  reason_note: string | null;
  stocks: FirstBoardStock[];
}

// 全市场成交额榜（客观公开榜单）
export interface TurnoverStock {
  code: string; name: string;
  price: number | null; pct: number | null;
  amount: number | null; mcap: number | null; float_cap: number | null; industry: string;
}
export interface TurnoverTop { stocks: TurnoverStock[]; updated: string; quote_date?: string; reason?: string }

export interface RadarItem {
  title: string; url: string; time: string; source: string; summary?: string; zh?: string;
}
export interface Industry {
  key: string; name: string; accent: string; total: number; items: RadarItem[];
}
export interface RadarData {
  generated_at: string | null; recent_days: number; industries: Industry[];
  stats: { industries: number; total_sources: number; failed_sources?: number };
}

export interface Holding {
  code: string; name: string; price: number; shares: number; cost: number;
  market_value: number; pnl: number; pnl_pct: number;
}
export interface ClosedPosition {
  code: string; name: string; date: string; price: number; shares: number; cost: number;
  pnl: number; pnl_pct: number;
}
export interface PortfolioData {
  holdings: Holding[];
  totals: { market_value: number; cost: number; pnl: number; pnl_pct: number };
  closed: ClosedPosition[];
  realized_pnl: number;
  updated: string;
}

// 资金面 / 筹码 / 信号（v3.3 并入，均为「用户查的那只股」的公开数据）
export interface MarginRow { date: string; rzye: number; rzmre: number; rzche: number; rqye: number; rqmcl: number; rzrqye: number }
export interface BlockTradeRow { date: string; price: number; close: number; premium_pct: number; vol: number; amount: number; buyer: string; seller: string }
export interface HolderRow { date: string; holder_num: number; change_ratio: number; avg_shares: number }
export interface DividendRow { date: string; bonus_rmb: number; transfer_ratio: number; bonus_ratio: number | null; plan: string }
export interface FundFlowRow { date: string; main_net: number; small_net: number; mid_net: number; large_net: number; super_net: number }
export interface DtSeat { name: string; buy_amt: number; sell_amt: number; net: number }
export interface DragonTiger {
  records: { date: string; reason: string; net_buy: number; turnover: number }[];
  seats: { buy: DtSeat[]; sell: DtSeat[] };
  institution: { buy_amt: number; sell_amt: number; net_amt: number };
}
export interface LockupRow { date: string; type: string; shares: number | null; able_shares: number | null; ratio: number | null }
export interface UnlockCalendar { start: string; end: string; fetched_at: string; truncated: boolean; events: (LockupRow & { code: string; name: string })[] }
export interface Lockup { history: LockupRow[]; upcoming: LockupRow[] }
export interface Board { name: string; code: string; change_pct: number | string; lead_stock: string }
export interface Blocks { total: number; boards: Board[]; concept_tags: string[] }
export interface HotConcept { concept: string; bk: string; hit: number }
export interface QaRow { company: string; question: string; answer: string | null; answerer: string; ask_time: string }
export interface IndustryRow { rank: number; name: string; change_pct: number; code: string; up_count: number; down_count: number }
export interface IndustryData { top: IndustryRow[]; bottom: IndustryRow[]; total: number }

// 全球市场（美股 / 港股，移植自 global-stock-data · 东财域内源）
export interface GlobalIndex {
  key: string; name: string; region: string;
  price: number | null; change_pct: number | null;
}
export interface GlobalQuote {
  code: string; name: string;
  price: number | null; open: number | null; high: number | null; low: number | null;
  prev_close: number | null; amount: number | null; mcap: number | null; change_pct: number | null;
}
export interface GlobalMetrics {
  report_date: string;
  revenue: number | null; revenue_yoy: number | null; net_profit: number | null;
  eps: number | null; roe: number | null; gross_margin: number | null;
  net_margin: number | null; debt_ratio: number | null;
}
export interface GlobalStock {
  code: string; name: string; market: string;
  quote: GlobalQuote; metrics: GlobalMetrics | null;
}

/** 此刻的「实时行情」属于哪一场 —— 盘前行情返回的是上一场收盘，UI 要如实标注 */
export interface MarketSession {
  now: string;
  today: string;
  /** 实时行情代表的交易日；取不到时为 null */
  quotes_of: string | null;
  is_today: boolean;
  poll?: boolean;
  phase_key?: string;
  phase: string;
  /** 直接可展示的一句话，如「盘前 · 显示 2026-07-29 收盘」 */
  label: string;
}

/** 隔夜外围快照：指数 + 美股七姐妹，各自带「属于哪一场」 */
export interface OverseasRow {
  name: string;
  price: number;
  change_pct: number;
  /** 该行行情所属交易日；取不到为 null（**不拿今天顶替**） */
  session: string | null;
  region?: string;
  ticker?: string;
}
export interface OverseasSnapshot {
  available: boolean;
  reason?: string;
  indices?: OverseasRow[];
  mag7?: OverseasRow[];
  us_session?: string | null;
  hk_session?: string | null;
  /** 可直接展示的一句话，如「美股 2026-07-29 收盘」「港股 2026-07-30 盘前」。
   *  别用 us_session 自己拼「XX 收盘」—— 港股在北京白天可能正在交易。 */
  us_label?: string | null;
  hk_label?: string | null;
}

/** 今日实时打板情绪（盘面数据页）—— 与 ShortTermEmotion（已收盘那一场）分开 */
export interface LiveEmotion {
  available: boolean;
  reason?: string;
  date?: string;
  /** 快照时刻 HH:MM */
  as_of?: string;
  phase?: string;
  zt_count?: number;
  dt_count?: number | null;
  zb_count?: number | null;
  max_boards?: number;
  lianban_count?: number;
  seal_rate?: number | null;
  break_rate?: number | null;
  promotion_rate?: number | null;
  /** 晋级率的分母：上一场的涨停家数 */
  promotion_base?: number | null;
  /** 分母是哪一场。两张卡都叫「晋级率」，而各自的「昨」不是同一天，所以把日期给出来写死 */
  promotion_base_date?: string | null;
}

export const api = {
  health: () => get<{ ok: boolean }>("/health"),
  indices: () => get<IndexQuote[]>("/indices"),
  marketSession: () => get<MarketSession>("/market/session"),
  overseas: () => get<OverseasSnapshot>("/market/overseas"),
  liveEmotion: () => get<LiveEmotion>("/market/live-emotion"),
  marketOverview: () => get<MarketOverview>("/market/overview"),
  emotion: () => get<ShortTermEmotion>("/market/emotion"),
  monitorSnapshot: async (watch: string) => {
    const codes = watch.split(',').filter(Boolean);
    const monitored = codes.slice(0, 100).join(',');
    if (!monitorId || monitored !== registeredWatch || Date.now() - registeredAt > 60000) {
      monitorId ??= randomId().replace(/-/g, "");
      registeredWatch = monitored; registeredAt = Date.now();
      try {
        await request('/monitor/watch', 'POST', {client_id: monitorId, codes: codes.slice(0, 100)});
        monitorWarning = undefined;
      } catch (e) {
        monitorWarning = `自选监控名单未更新：${e instanceof Error ? e.message : '注册失败'}；一分钟后重试，其余行情继续展示`;
      }
    }
    const snapshot = await get<MonitorSnapshot>(`/monitor/snapshot?watch=${encodeURIComponent(monitored)}`);
    return {...snapshot, watch_warning: monitorWarning || (codes.length > 100 ? '已有自选超过100只，本页仅监控前100只；原列表保留，可在持仓自选中整理' : undefined)};
  },
  firstBoard: () => get<FirstBoardData>("/market/first-board"),
  turnoverTop: () => get<TurnoverTop>("/market/turnover-top"),
  globalIndices: () => get<GlobalIndex[]>("/global/indices"),
  globalStock: (symbol: string) => get<GlobalStock>(`/global/stock?symbol=${encodeURIComponent(symbol)}`),
  radar: () => get<RadarData>("/radar"),
  radarRefresh: () => request<RadarData>("/radar/refresh", "POST"),
  portfolio: () => get<PortfolioData>("/portfolio"),
  addHolding: (code: string, shares: number, cost: number) => request<PortfolioData>("/portfolio/holding", "POST", { code, shares, cost }),
  removeHolding: (code: string) => request<PortfolioData>(`/portfolio/holding?code=${code}`, "DELETE"),
  refreshPortfolio: () => request<PortfolioData>("/portfolio/refresh", "POST"),
  closePosition: (code: string, date: string, price: number, shares: number, cost: number) =>
    request<PortfolioData>("/portfolio/close", "POST", { code, date, price, shares, cost }),
  removeClosed: (index: number) => request<PortfolioData>(`/portfolio/close?index=${index}`, "DELETE"),
  valuation: (code: string) => get<Valuation>(`/valuation?code=${code}`),
  percentile: (code: string) => get<ValPercentile>(`/valuation/percentile?code=${code}`),
  financials: (code: string) => get<Financials>(`/financials?code=${code}`),
  announcements: (code: string) => get<Announcement[]>(`/announcements?code=${code}`),
  quote: (codes: string) => get<Record<string, Quote>>(`/quote?codes=${codes}`),
  reports: (code: string) => get<Report[]>(`/reports?code=${code}`),
  news: (code: string) => get<NewsItem[]>(`/news?code=${code}`),
  margin: (code: string) => get<MarginRow[]>(`/margin?code=${code}`),
  blockTrade: (code: string) => get<BlockTradeRow[]>(`/block-trade?code=${code}`),
  holders: (code: string) => get<HolderRow[]>(`/holders?code=${code}`),
  dividend: (code: string) => get<DividendRow[]>(`/dividend?code=${code}`),
  fundFlow: (code: string) => get<FundFlowRow[]>(`/fund-flow?code=${code}`),
  dragonTiger: (code: string) => get<DragonTiger>(`/dragon-tiger?code=${code}`),
  unlockCalendar: (window: "upcoming" | "recent") => get<UnlockCalendar>(`/lockup-calendar?window=${window}`),
  lockup: (code: string) => get<Lockup>(`/lockup?code=${code}`),
  blocks: (code: string) => get<Blocks>(`/blocks?code=${code}`),
  hotConcepts: (code: string) => get<HotConcept[]>(`/hot-concepts?code=${code}`),
  investorQa: (code: string) => get<QaRow[]>(`/investor-qa?code=${code}`),
  industry: (top = 20) => get<IndustryData>(`/industry?top=${top}`),
  myReports: () => get<MyReport[]>("/myreports"),
  uploadReport: (name: string, contentB64: string) =>
    request<MyReport>("/myreports", "POST", { name, content_b64: contentB64 }),
  deleteReport: (id: string) => request<{ ok: boolean }>(`/myreports/${id}`, "DELETE"),
};

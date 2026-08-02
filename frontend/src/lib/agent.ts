import { apiUrl } from "./base";
// 复盘 agent 的前端类型 + 助手（后端默认 8910，vite 把全部 /api 代理过去）。

export async function agentFetch<T>(path: string, method: "GET" | "POST" = "GET"): Promise<T> {
  const r = await fetch(apiUrl(path), { method });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as T;
}

export async function agentPost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as T;
}

export interface ChatMsg { role: "user" | "assistant"; content: string; }


export function safeArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

/** 只取普通对象；数组和 null 都不算，避免 Object.entries 崩或产出怪结果 */
export function safeRecord<T>(v: unknown): Record<string, T> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, T>) : {};
}

/** 有限数才算数：NaN / Infinity 会溜过 `v == null`，最后显示成 "NaN%" 或灌进 CSS 宽高 */
export function finite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function localDate(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());
  return parts;   // en-CA 的格式就是 YYYY-MM-DD
}

// 情绪档位 → 语义色（用 VR 主题 token）
export function phaseTone(phase?: string): string {
  if (!phase) return "text-muted-foreground bg-muted";
  if (phase.includes("亢奋")) return "text-danger bg-danger/10";
  if (phase.includes("发酵")) return "text-primary bg-primary/10";
  if (phase.includes("修复")) return "text-warning bg-warning/10";
  if (phase.includes("冰点")) return "text-info bg-info/10";
  if (phase.includes("退潮")) return "text-muted-foreground bg-muted";
  return "text-primary bg-primary/10";
}

export function plain(v: string | null | undefined): string {
  return (v ?? "").replace(/\*\*/g, "");
}

export function ratioPct(v: number | null | undefined, signed = false): string {
  const n = finite(v);
  if (n === null) return "—";
  const out = `${Math.round(n * 100)}%`;
  return signed && n > 0 ? `+${out}` : out;
}

export function pct(v: number | null | undefined): string {
  const n = finite(v);
  if (n === null) return "—";
  return `${n > 0 ? "+" : ""}${n}%`;
}

// ---------- 每日复盘 ----------
export interface FocusDirection {
  direction: string;
  logic: string;
  /** 仅部分 prompt 包会产出（自带的 research 包不点名个股）——渲染时必须判空 */
  leader_candidates?: string[];
  risk: string;
}
/** 明日验证条件：指标 + 预期方向。不是"明天买什么"，是"明天用哪些读数检验今晚判断" */
export interface VerificationItem {
  metric: string; direction: string; reason: string;
  /** 今晚这个读数是多少 —— 明天要跟它比。取不到为 null（不是 0）*/
  base_value?: number | null;
  /** 变了才算变的阈值。没有它，涨停 40→41 也会被当成"上升" */
  eps?: number | null;
  label?: string; unit?: string; higher_is_hotter?: boolean;
}
/** 次日核验结果。verified=null 表示数据不足以判定，**不是判错** */
export interface VerificationResult extends VerificationItem {
  label: string; unit: string; expect: string;
  prev_value: number | null; cur_value: number | null;
  actual: string | null; verified: boolean | null;
  /** 这个方向历史上多久发生一次 —— 命中率唯一的参照物。null = 没有历史序列 */
  baseline?: number | null; baseline_samples?: number | null;
  baseline_reason?: string | null; baseline_note?: string | null;
  /** 命中 − 基准。≈0 = 判的是本来就会发生的事 */
  edge?: number | null; eps?: number | null;
}
export interface VerificationSummary {
  total: number; decided: number; hit: number; rate: number | null; undecidable: number;
  /** 有基准的条目数；expected_rate = 这批条目按历史常态本该中的比例 */
  baseline_covered?: number; expected_rate?: number | null;
  edge?: number | null; edge_note?: string;
}

export interface TomorrowFocus {
  emotion_phase: string;
  market_oneliner: string;
  focus_directions: FocusDirection[];
  risk_alerts: string[];
  verification_items?: VerificationItem[];
}
export interface ReflectionLeader { name: string; code: string; ret: number; }
export interface ReflectionDir {
  direction: string;
  avg_next_ret: number | null;
  hit: boolean;
  leaders: ReflectionLeader[];
}
/** 情绪档位验证：市场层面靶子，不依赖个股预测，任何 prompt 包都有 */
export interface PhaseEval {
  phase: string;
  expected_direction: "up" | "down";
  actual_direction: "up" | "down" | "flat";
  /** 持平未分胜负时为 null */
  hit: boolean | null;
  signals: Record<string, number | null>;
  detail: Record<string, { prev: number | null; cur: number | null }>;
}
export interface Reflection {
  prediction_date: string;
  eval_date: string;
  /** 仅当 prompt 包产出个股预测时才有 */
  direction_hit_rate: number | null;
  overall_next_ret: number | null;
  directions: ReflectionDir[];
  phase_eval?: PhaseEval | null;
  verification?: VerificationResult[];
  verification_summary?: VerificationSummary;
}

// ---------- 派生情绪指标 ----------
interface MetricBase { available: boolean; reason?: string; prev_date?: string; }
export interface MoneyEffect extends MetricBase {
  sample?: number; avg?: number; median?: number;
  positive_rate?: number; limit_up_again_rate?: number;
}
export interface PromotionTier { base: number; promoted: number; rate: number | null; }
export interface Promotion extends MetricBase {
  tiers?: Record<string, PromotionTier>;
  overall?: PromotionTier;
  limit_up_count?: number;
  prev_limit_up_count?: number;
}
export interface ConsecPremium extends MetricBase {
  sample?: number; avg?: number; median?: number; positive_rate?: number;
}
/** 连板梯队断层：高标与低位之间有没有空档 */
export interface LadderGap extends MetricBase {
  highest?: number;
  /** { "5": 1, "4": 2, ... } 各档家数 */
  tiers?: Record<string, number>;
  /** 缺失的档位，如 [3, 4] */
  gaps?: number[];
  continuous?: boolean;
  note?: string;
}
export interface CycleDay {
  date: string; limit_up: number; highest_consec: number;
  broken_rate: number | null; score: number;
}
/** 情绪周期位置：这波从哪天启动、今天第几天 */
export interface EmotionCycle extends MetricBase {
  window?: number;
  trough_date?: string;
  trough_score?: number;
  current_score?: number;
  day_n?: number;
  
  rising?: boolean;
  /** 最近走向：连续两日走强 / 今日转弱 / 基本走平 … */
  trend?: string;
  /** 当前值在十日区间的分位（0~1） */
  pctile?: number | null;
  series?: CycleDay[];
}
export interface EmotionMetrics {
  date?: string;
  prev_date?: string | null;
  money_effect?: MoneyEffect;
  promotion?: Promotion;
  consec_premium?: ConsecPremium;
  ladder_gap?: LadderGap;
  cycle?: EmotionCycle;
}
// ---------- 客观事实表（market_facts）----------
// 事实层回答"今天发生了什么"，指标层（EmotionMetrics）回答"什么温度"。两者互补。
interface FactBase { available: boolean; reason?: string; prev_date?: string; }

export interface SealQuality extends FactBase {
  total?: number;
  /** 全天一次没炸过的家数 —— 板的成色 */
  never_broken?: number; never_broken_rate?: number;
  opening_seconds?: number;   // 开盘 5 分钟内首封
  late_seal?: number;         // 14:30 后才最终封住
  reopened?: number;          // 炸过又回封
  avg_broken_times?: number;
  /** 真炸板率 = 炸板未回封 ÷（涨停 + 炸板未回封）。不是「1 - 从未炸板占比」 */
  broken_rate?: number | null;
  broken_pool?: number;
  /** 最终封住的票里曾开过板的比例 —— 衡量板的成色，别当炸板率用 */
  ever_opened_rate?: number;
}
export interface LossEffect extends FactBase {
  sample?: number;
  deep_loss_5_count?: number; deep_loss_5_rate?: number; deep_loss_7_count?: number;
  limit_down_count?: number; worst?: number; market_limit_down?: number;
  /** 昨日炸板股今天修复了没 —— 试错代价的直接读数 */
  prev_broken_recovery?: { sample: number; avg: number; positive_rate: number } | null;
}
export interface FeedbackCell {
  晋级涨停: number; 收红: number; 小跌: number; "跌超5%": number; 跌停: number;
  合计: number; 晋级率: number | null;
}
export interface FeedbackDetail {
  code: string; name: string; board: string; prev_tier: string;
  ret: number; result: string; sector: string;
}
export interface FeedbackMatrix extends FactBase {
  result_order?: string[];
  matrix?: Record<string, FeedbackCell>;
  details?: FeedbackDetail[];
}
export interface ThemeRow {
  sector: string; limit_up: number; first_boards: number; consec_boards: number;
  highest: number; broken: number; broken_rate: number | null;
  first_seal: string | null; last_seal: string | null;
  names: { code: string; name: string; boards: number; first_seal: string; broken_times: number }[];
}
export interface ThemeStructure extends FactBase {
  themes?: ThemeRow[]; theme_count?: number; concentration?: number | null;
  timeline?: { slot: string; count: number }[];
}
export interface LedgerEvent {
  tag: string; code: string; name: string; board: string; boards: number;
  sector: string; ret: number; first_seal: string | null; last_seal: string | null;
  broken_times: number; turnover?: number | null; note: string;
}
export interface EventLedger extends FactBase {
  events?: LedgerEvent[]; count?: number;
  loss_events?: LedgerEvent[];    // 断板 / 炸板 / 跌停 —— 今天钱亏在哪
  gain_events?: LedgerEvent[];    // 最高标 / 题材首封 —— 今天钱赚在哪
  split_events?: LedgerEvent[];   // 反复开板 —— 分歧，两头都不算
}
export interface BoardStat {
  limit_up: number; highest: number; broken?: number; limit_down?: number;
  prev_base?: number; promoted?: number; promotion_rate: number | null;
}
export interface ByBoard extends FactBase { boards?: Record<string, BoardStat>; }

/** 题材事件树：按**真实事件题材**（问财涨停原因）聚合，不是行业分类 */
export interface ThemeNode {
  tag: string; state: string;
  limit_up: number; first_boards: number; consec_boards: number; highest: number;
  broken: number; limit_down: number; broken_rate: number | null;
  first_seal: string | null; last_seal: string | null;
  /** 昨日该题材涨停股今天还剩多少仍涨停 */
  prev_members: number; prev_still_up: number; prev_broken_or_down: number;
  continuation_rate: number | null;
  members: { code: string; name: string; boards: number; label?: string; zt_stat?: string | null; first_seal: string | null; broken_times: number }[];
}
export interface ThemeTree extends FactBase {
  date?: string; prev_date?: string; tag_count?: number;
  themes?: ThemeNode[]; concentration?: number | null;
  covered?: number; total_limit_up?: number; coverage_rate?: number | null;
}

/** 一个读数的历史统计位置。数字谁都能看，位置才是信号 */
export interface StatItem {
  key: string; label: string; unit: string; kind: string;
  value: number | null; value_text: string;
  /** 近 N 日分位（0~1）。样本不足时为 null —— 不拿十几天的数据冒充"历史规律" */
  percentile: number | null;
  /** 处在两头才标：偏热 / 偏冷（语义已按 higher_is_hotter 翻译过）*/
  extreme: string | null;
  higher_is_hotter: boolean;
  hist_min: number | null; hist_max: number | null; hist_median: number | null;
}
export interface StatsContext extends FactBase {
  date?: string; sample_days?: number; min_samples?: number;
  items?: StatItem[]; extreme_count?: number;
}
export interface DayChange {
  key: string; label: string; unit: string; kind: string;
  prev: number; cur: number; delta: number;
  prev_text: string; cur_text: string;
  /** 这个变化是让情绪转热还是转冷 */
  hotter: boolean;
}
export interface DayDiff extends FactBase {
  date?: string; prev_date?: string;
  changes?: DayChange[]; unchanged_count?: number;
}

/** 全市场涨跌宽度 —— 涨停池那些数字的分母。 */
export interface Breadth extends FactBase {
  date?: string;
  up?: number; down?: number; flat?: number;
  up_down_scope?: string;          // 家数口径（沪深两市，不含北交所）
  amount_yi?: number;              // 两市成交额（亿）
  universe?: number;               // 分布统计覆盖的只数
  deep_up_5_incl?: number | null;
  
  deep_up_5?: number | null;
  deep_down_5?: number | null;
  dist_scope?: string;
  dist_available?: boolean;
  dist_partial?: boolean;   // 三项里有没有单独失败的
}

export interface TrendMetric {
  key: string; label: string; unit?: string; kind?: string;
  higher_is_hotter?: boolean;
  values: (number | null)[];
}
export interface Trend extends FactBase {
  days?: string[];
  metrics?: TrendMetric[];
  sample_days?: number;
}

export interface MarketFacts {
  breadth?: Breadth;
  trend?: Trend;
  stats_context?: StatsContext;
  day_diff?: DayDiff;
  theme_tree?: ThemeTree;
  seal_quality?: SealQuality;
  loss_effect?: LossEffect;
  feedback_matrix?: FeedbackMatrix;
  theme_structure?: ThemeStructure;
  event_ledger?: EventLedger;
  by_board?: ByBoard;
}

/** AI 判断的累计战绩：判断 → 次日验证 */
export interface Scoreboard {
  phase: {
    /** 能分胜负的样本数（次日持平的不计入分母）*/
    decided: number;
    hits: number;
    flat: number;
    
    next_day_direction_rate: number | null;
    /** 样本够不够撑一个百分比（<30 天时前端只显示 x/y） */
    enough_samples: boolean;
    min_samples: number;
    by_phase: Record<string, { n: number; hit: number; hit_rate: number | null }>;
  };
  /** 个股靶子：hit_rate 按**方向样本**加权（samples=累计方向数，不是天数） */
  stock: { days: number; samples: number; hits: number; hit_rate: number | null };
  recent: { prediction_date: string; eval_date: string; phase: string | null; hit: boolean | null }[];
}

export interface AnalystReport { key: string; title: string; tag: string; html: string; }
export interface ReviewData {
  target_date?: string;
  trade_date?: string;
  generated_at?: string;
  warnings?: string[];
  focus: TomorrowFocus | null;
  focus_md?: string;
  emotion_metrics?: EmotionMetrics;
  market_facts?: MarketFacts;
  macro_sector?: string;
  reflection?: Reflection | null;
  scoreboard?: Scoreboard | null;
  analysts?: AnalystReport[];
}

// ---------- 近5天热度 ----------
export interface HeatDay {
  date: string;
  limit_up: number | null;
  broken_rate: number | null;
  highest_consec: number | null;
  leader: { code: string; name: string; boards: number; sector: string } | null;
  unavailable?: boolean;
}
export interface LineageLeader {
  code: string;
  name: string;
  sector: string;
  appear_date: string;
  boards_then: number;
  cum_return_since: number | null;
  series: { date: string; cum_ret: number }[];
  peak_cum_ret?: number | null;        // 区间最高累计收益（收盘价口径）
  drawdown_from_peak?: number | null;  // 现价距该最高点跌了多少（≤0；不知道时 null）
  is_current_top?: boolean;            // 是不是**最近一个交易日**的最高标
}
export interface WeeklyData {
  generated_at?: string;
  error?: string;
  stale?: boolean;
  days: HeatDay[];
  leader_lineage: LineageLeader[];
}

export interface JobStatus { running: boolean; elapsed?: number; error?: string | null; stock?: string; busy?: boolean; }


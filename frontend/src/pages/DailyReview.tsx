import { UnlockCalendarPanel } from "@/components/UnlockCalendarPanel";
import { pageMaterials } from "@/lib/page-materials";
import { useState, useEffect, useRef, Fragment } from "react";
import { pctColor } from "@/lib/colors";
import { Sparkles, Loader2, RefreshCw, Gauge, ArrowDownUp, TrendingUp, TrendingDown, Plus, X, Flame, BarChart3, Globe } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Caliber } from "@/components/ui/Caliber";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { Disclaimer } from "@/components/ui/Disclaimer";
import { finite } from "@/lib/agent";
import { api, type IndexQuote, type Quote, type MarketOverview, type ShortTermEmotion, type LianbanStock, type TurnoverTop, type GlobalIndex, type MarketSession, type OverseasSnapshot, type OverseasRow, type LiveEmotion } from "@/lib/api";
import { useDeepDive, DeepDivePanel, RunAllButton, type DiveItem } from "@/components/ui/DeepDive";
import { loadWatch, saveWatch, addCodes } from "@/lib/watchlist";
import { cn } from "@/lib/utils";

// A股红涨绿跌。全球市场（美股/港股指数）**也沿用红涨**——与整个看板及东财等中国平台一致，
// A 股惯例：红涨绿跌。与国际绿涨惯例相反，是有意选择，全站必须一致，勿改。

const AUTO_KEY = "vibe-astock-auto-refresh";
const LIVE_MS = 5_000;      // 轻量行情：腾讯批量，5 秒
const HEAVY_MS = 60_000;    // 板块资金 / 成交额榜：东财+akshare，60 秒

const fmt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const yi = (v: number | null) => (v == null ? "—" : `${fmt(v / 1e8)} 亿`); // 元 → 亿

export function DailyReview() {
  const [indices, setIndices] = useState<IndexQuote[]>([]);
  const [idxErr, setIdxErr] = useState(false);
  const [overview, setOverview] = useState<MarketOverview | null>(null);
  const [emotion, setEmotion] = useState<ShortTermEmotion | null>(null);
  const [turnover, setTurnover] = useState<TurnoverTop | null>(null);
  const [globalIdx, setGlobalIdx] = useState<GlobalIndex[]>([]);
  // 实时行情属于哪一场：盘前接口返回的是上一场收盘，不标出来会被当成今天的
  const [session, setSession] = useState<MarketSession | null>(null);
  // 隔夜外围：指数 + 七姐妹。走自己的接口是为了拿到**行情所属交易日**
  // （vr 的 /api/global/indices 不回时间，界面就只能按本机今天贴日期）
  const [oversea, setOversea] = useState<OverseasSnapshot | null>(null);
  // 东财实时池 · 盘中读数。与下面那块「昨日短线情绪」是两回事：
  // 这块随盘变，那块是已收盘那一场的东财池快照。
  const [liveEmo, setLiveEmo] = useState<LiveEmotion | null>(null);
  // 昨日连板股的**今日**实时行情。昨日那份的「涨停%」全是 +10%（都涨停了，
  // 没信息量），换成今天的实时涨跌才回答「昨天的高标今天怎么样」。
  const [lianbanQuotes, setLianbanQuotes] = useState<Record<string, Quote>>({});
  // 自动刷新开关：**默认关**（别替用户决定要不要一直打请求），选择记在本地
  const [autoRefresh, setAutoRefresh] = useState<boolean>(
    () => { try { return localStorage.getItem(AUTO_KEY) === "1"; } catch { return false; } });
  // 关注股票（自选，存本地）
  const [watchCodes, setWatchCodes] = useState<string[]>(loadWatch);
  const [watchQuotes, setWatchQuotes] = useState<Record<string, Quote>>({});
  const [watchInput, setWatchInput] = useState("");
  const [watchLoading, setWatchLoading] = useState(false);

  // 各数据块请求是否已结束：区分「加载中」与「数据源暂不可用」（非交易时段/被限流时后端返回空）
  const [ovDone, setOvDone] = useState(false);
  const [emoDone, setEmoDone] = useState(false);
  const [toDone, setToDone] = useState(false);

  /** 拉这批代码的实时行情（昨日连板股用）。空名单直接返回，别打空请求。
   *  ⚠️ 必须定义在 `loadLive` **之前**：`const` 箭头函数没有提升，
   *  写在后面 `loadLive()` 首次调用就会 ReferenceError（TDZ）。 */
  const refreshLianban = (codes: string[]) => {
    if (!codes.length) return;
    api.quote(codes.join(",")).then(setLianbanQuotes).catch(() => {});
  };

  // 轻量实时组：全是腾讯批量行情，一次一个请求，5 秒一刷不吃力
  const loadLive = () => {
    api.indices().then(setIndices).catch(() => setIdxErr(true));
    api.overseas().then(setOversea).catch(() => {});
    api.marketSession().then(setSession).catch(() => {});
    api.liveEmotion().then(setLiveEmo).catch(() => {});
    // 连板股名单来自「昨日」那份，这里只刷它们的实时价
    refreshLianban((emotion?.lianban_stocks ?? []).map((s) => s.code));
  };

  // 重量组：板块资金走 akshare + JS 引擎（90 个行业）、成交额榜走东财 clist。
  // **不能跟着 5 秒刷** —— 会撞限流，而且这两样本身变化慢。
  const loadHeavy = () => {
    api.globalIndices().then(setGlobalIdx).catch(() => {});
    api.marketOverview().then(setOverview).catch(() => {}).finally(() => setOvDone(true));
    api.turnoverTop().then(setTurnover).catch(() => {}).finally(() => setToDone(true));
  };

  // 短线情绪锚在已收盘那一场，只在进页面时取一次，不参与任何轮询
  const loadSettled = () => {
    api.emotion().then(setEmotion).catch(() => {}).finally(() => setEmoDone(true));
  };

  const loadIndices = () => { loadLive(); loadHeavy(); loadSettled(); };

  // 数据块占位：请求没回来 = 加载中；回来了但为空 = 数据源暂不可用（别让用户干等）
  const pending = (done: boolean) => (
    <p className="py-4 text-center text-sm text-muted-foreground/60">
      {done ? "暂无数据：可能是非交易时段或数据源暂时不可用，可点「大盘指数」旁的刷新重试" : "加载中…"}
    </p>
  );

  const refreshWatch = (codes: string[]) => {
    if (!codes.length) { setWatchQuotes({}); return; }
    setWatchLoading(true);
    api.quote(codes.join(",")).then(setWatchQuotes).catch(() => {}).finally(() => setWatchLoading(false));
  };

  useEffect(() => {
    loadIndices();
    refreshWatch(loadWatch());
  }, []);

  // 时段探测独立于行情轮询，盘前/午休打开后才能自动发现开盘。
  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(() => api.marketSession().then(setSession).catch(() => {}), 15000);
    return () => clearInterval(timer);
  }, [autoRefresh]);

  // 收盘和15:05定稿标签变化时补拉数据，避免停轮询后仍展示尾盘旧值。
  const observedSessionLabel = useRef<string | null>(null);
  useEffect(() => {
    if (!session?.label) return;
    const previous = observedSessionLabel.current;
    observedSessionLabel.current = session.label;
    if (previous === null || previous === session.label || !autoRefresh || session.phase_key !== 'closed') return;
    loadLive();
    loadHeavy();
    refreshWatch(watchCodes);
  }, [autoRefresh, session?.label, session?.phase_key]);

  // 连板股名单是异步来的，首次 loadLive 时它还没回来 —— 名单一到补拉一次
  useEffect(() => {
    refreshLianban((emotion?.lianban_stocks ?? []).map((s) => s.code));
  }, [emotion?.date, emotion?.lianban_stocks?.length]);

  // ⭐ 自动刷新。三条不能写错的地方：
  //  ① **交易时段用后端给的 `session.phase` 判断**，不要用 `new Date().getHours()`
  //     —— 那是本机时区，人在海外会盘中不刷、半夜狂刷。后端算的是北京时间。
  //  ② 收盘 / 非交易日**不刷** —— 数字不会动，白打请求还可能撞限流。
  //  ③ cleanup 必须把两个句柄都清掉，且**句柄只属于这一次 effect**。
  //     共享句柄或忘了清，旧的定时器会活下来跟新的并行 → 实际频率翻倍。
  useEffect(() => {
    const live = Boolean(session?.poll);
    if (!autoRefresh || !live) return;

    const liveTimer = setInterval(() => {
      loadLive();
      if (watchCodes.length) refreshWatch(watchCodes);
    }, LIVE_MS);
    const heavyTimer = setInterval(loadHeavy, HEAVY_MS);
    return () => { clearInterval(liveTimer); clearInterval(heavyTimer); };
  }, [autoRefresh, session?.phase, watchCodes]);

  const toggleAuto = () => {
    const next = !autoRefresh;
    try { localStorage.setItem(AUTO_KEY, next ? "1" : "0"); setAutoRefresh(next); setWatchError(null); }
    catch { setWatchError("自动刷新设置未保存：请检查浏览器存储空间与权限"); }
  };

  const [watchError, setWatchError] = useState<string | null>(null);
  const addWatch = () => {
    try {
    // 支持一次粘贴多只（逗号 / 空格分隔）；全部无效或重复则清空输入、无副作用。
    const { next, added } = addCodes(watchCodes, watchInput);
    setWatchInput("");
    if (!added) return;
    saveWatch(next); setWatchCodes(next); refreshWatch(next); setWatchError(null);
    } catch(e) { setWatchError(e instanceof Error ? e.message : "自选保存失败"); }
  };

  const removeWatch = (c: string) => {
    try {
    const next = watchCodes.filter((x) => x !== c);
    saveWatch(next); setWatchCodes(next); refreshWatch(next); setWatchError(null);
    } catch(e) { setWatchError(e instanceof Error ? e.message : "自选保存失败"); }
  };

  const today = new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
  // 是否处在会动的时段（后端判定，避免本机时区坑）
  const liveNow = Boolean(session?.poll);

  const dataSummary = pageMaterials({
    "当前场次": session, "指数": indices, "市场概览": overview,
    "盘中情绪": liveEmo, "已收盘情绪及梯队": emotion,
    "成交额榜": turnover, "隔夜外围": oversea,
    "自选行情": watchQuotes, "昨日连板股当前行情": lianbanQuotes,
  });

  // 连板股「AI 深入分析」（与首板分析页共用 DeepDive 单元；结果本地存档，跨加载复用）
  const dd = useDeepDive("lianban", emotion?.date || "");
  const lianbanPrompt = (s: LianbanStock) =>
    `${emotion?.date || ""}（已收盘）A 股连板股「${s.name}（${s.code}）」的客观数据：\n` +
    `该日收盘 ${s.price} 元、涨停 +${s.pct}%，已连续涨停 ${s.boards} 天（${s.boards} 连板），` +
    (finite(lianbanQuotes[s.code]?.change_pct) !== null
      ? `另附行情：${lianbanQuotes[s.code].price} 元（${lianbanQuotes[s.code].change_pct > 0 ? "+" : ""}${lianbanQuotes[s.code].change_pct}%），报价时点 ${lianbanQuotes[s.code].quote_time || "未提供"}；按报价自身日期解释，不冒充研究日收盘或今日盘中值。`
      : "") +
    `成交额 ${yi(s.amount)}，流通市值 ${yi(s.float_cap)}，所属概念/行业 ${s.industry || "未知"}，` +
    `涨停原因题材：${s.reason || "尚未核实"}。\n\n` +
    "仅根据材料分析这个交易日的连板表现。若本次允许工具，可查询该日及之前的新闻研报；否则直接说明新闻研报尚未核实，不要求取新资料。" +
    "按「已知事实」「尚未核实」「后续核验条件」三段，每段最多三条，全文约300字。" +
    "题材串是来源归因，不是公司确认事实。只有单股与单日数据时，不能判断板块强度、扩散、阶段，也不能判断放量缩量、承接或持续性。" +
    "只列需要补查的公开资料及可观察变化，不给交易动作、点位、仓位或涨跌预测。不在答案中复述工具权限与这些约束。" +
    "个股层面只陈述已经发生的客观数据与事实，方向与强弱判断做到题材板块层面为止，且必须有这个题材板块整体的证据；不要由此推断这只个股接下来会怎样。不预测个股涨跌，不给个股参与倾向，不推荐任何标的，不构成投资建议。" +
    "输出纯 Markdown，不使用 HTML 标签。";
  const lianbanCtx = (s: LianbanStock) => `连板股 ${s.name}(${s.code}) ${s.boards}连板 深入分析`;
  const lianbanItem = (s: LianbanStock): DiveItem => ({ key: s.code, prompt: lianbanPrompt(s), context: lianbanCtx(s) });

  const sentiment = overview?.sentiment;
  const sectors = overview?.sectors || [];
  const sentCells = sentiment ? [
    { k: "上涨家数", v: sentiment.up, up: true },
    { k: "下跌家数", v: sentiment.down, up: false },
    { k: "平盘", v: sentiment.flat, up: null },
    { k: "涨停", v: sentiment.zt, up: true },
    { k: "真实涨停", v: sentiment.zt_real, up: true },
    { k: "跌停", v: sentiment.dt, up: false },
    { k: "真实跌停", v: sentiment.dt_real, up: false },
    { k: "活跃度", v: sentiment.active, up: null },
  ] : [];

  return (
    <div>
      {watchError && <p role="alert" className="text-danger">{watchError}</p>}
      <PageHeader
        title="盘面数据"
        subtitle={`${session?.label ?? today} · 指数 / 自选股 / 外围 / 板块资金一屏看全（复盘统计在「复盘看板」）`}
        actions={
          <div className="flex items-center gap-2">
            {/* 开着但不在交易时段时要说清「为什么不动」——否则会被当成坏了 */}
            <button
              onClick={toggleAuto}
              title={autoRefresh
                ? `已开：指数/外围/自选每 ${LIVE_MS / 1000} 秒、板块资金与成交额榜每 ${HEAVY_MS / 1000} 秒。只在盘中生效`
                : "开启后在交易时段自动刷新行情"}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm transition-colors",
                autoRefresh
                  ? "bg-primary/15 text-primary hover:bg-primary/25"
                  : "text-muted-foreground hover:text-primary",
              )}
            >
              <RefreshCw className={cn("h-3.5 w-3.5", autoRefresh && liveNow && "animate-spin")} />
              {autoRefresh ? (liveNow ? `每 ${LIVE_MS / 1000} 秒自动刷新` : "自动刷新（非交易时段暂停）") : "自动刷新"}
            </button>
            <AskAiButton
              context={dataSummary}
              label="问 AI"
              suggestions={["根据已有材料解释这场盘面", "哪些指数领涨领跌", "哪些判断还缺少证据"]}
            />
          </div>
        }
      />

      {session && <p role="status" className="mb-4 text-sm text-warning">{session.label}。{["auction", "closing"].includes(session.phase_key || "") ? "以下个股为试撮合报价，不代表已成交或封板；成交榜与异动在连续交易时段确认。" : "各表按来源时点更新，未覆盖不等于零。"}</p>}
      {/* 1. 大盘指数（实时） */}
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-baseline gap-2">
          <h3 className="text-sm font-semibold text-muted-foreground">大盘指数</h3>
          <Caliber text={
            "涨跌幅对比前一交易日收盘。\n" +
        "「实时」是延时行情，页面上没标截至几点。"
          } />
          {session && (
            <span className={cn("text-[11px]", session.is_today ? "text-muted-foreground/50" : "text-warning")}>
              {session.label}
            </span>
          )}
          {/* 集合竞价（09:15-09:25）还没成交，指数等于昨收、涨跌幅是 0 */}
          {session?.phase === "集合竞价" && (
            <span className="text-[11px] text-muted-foreground/50">还没成交，涨跌幅为 0 是正常的</span>
          )}
        </div>
        <button onClick={loadIndices} className="text-muted-foreground hover:text-primary" title="刷新"><RefreshCw className="h-3.5 w-3.5" /></button>
      </div>
      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {indices.length === 0
          ? [1, 2, 3, 4].map((i) => (
              <GlassCard key={i} className="p-3">
                <p className="text-xs text-muted-foreground">{idxErr ? "行情未接通" : "加载中…"}</p>
                <p className="mt-1 font-mono text-lg font-bold text-muted-foreground/40">—</p>
              </GlassCard>
            ))
          : indices.map((i) => (
              <GlassCard key={i.name} className="p-3">
                <p className="truncate text-xs text-muted-foreground">{i.name}</p>
                <p className={cn("mt-1 font-mono text-lg font-bold", pctColor(i.change_pct))}>{i.price}</p>
                <p className={cn("text-xs", pctColor(i.change_pct))}>{i.change_pct > 0 ? "+" : ""}{i.change_pct}%</p>
              </GlassCard>
            ))}
      </div>

      {/* 1b. 隔夜外围：美股 / 港股指数 + 美股七姐妹 */}
      {(oversea?.available || globalIdx.length > 0) && (() => {
        // 优先用带交易日的新接口；它挂了才退回 vr 那份（没有日期，只能不标）
        const rows: OverseasRow[] = oversea?.available && oversea.indices?.length
          ? oversea.indices
          : globalIdx.map((g) => ({ name: g.name, price: g.price ?? 0, change_pct: g.change_pct ?? 0,
                                    session: null, region: g.region }));
        const us = rows.filter((r) => r.region === "美股");
        const hk = rows.filter((r) => r.region !== "美股");
        const mag7 = oversea?.available ? (oversea.mag7 ?? []) : [];
        const cell = (r: OverseasRow, sub?: string) => (
          <GlassCard key={r.name} className="p-3">
            <p className="truncate text-xs text-muted-foreground">
              {r.name}{sub && <span className="ml-1 font-mono text-[10px] text-muted-foreground/40">{sub}</span>}
            </p>
            <p className={cn("mt-1 font-mono text-lg font-bold", pctColor(r.change_pct))}>{r.price}</p>
            <p className={cn("text-xs", pctColor(r.change_pct))}>
              {r.change_pct > 0 ? "+" : ""}{r.change_pct}%
            </p>
          </GlassCard>
        );
        return (
          <>
            <div className="mb-3 flex flex-wrap items-baseline gap-2">
              <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><Globe className="h-4 w-4" /> 隔夜外围</h3>
              <Caliber text={
                "美股港股的涨跌幅都是对比它们各自的前一交易日收盘。\n" +
            "港股在北京时间白天可能正在交易，所以会标「盘中」——那是抓取那一刻的延时行情，没标具体几点。"
              } />
              <span className="text-[11px] text-muted-foreground/50">A 股常看美股 / 港股脸色</span>
              {/* 标签由后端给（含盘前/盘中/收盘）—— 前端别自己拼"收盘"，
                  港股在北京白天可能正在交易，那时候标"收盘"就是错的 */}
              {oversea?.us_label && <span className="text-[11px] text-warning">{oversea.us_label}</span>}
              {oversea?.hk_label && <span className="text-[11px] text-warning">{oversea.hk_label}</span>}
            </div>

            {us.length > 0 && (
              <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-3">{us.map((r) => cell(r))}</div>
            )}

            {mag7.length > 0 && (
              <>
                <p className="mb-2 text-[11px] text-muted-foreground/60">
                  美股七姐妹 · 权重股带指数走，看它们比看指数细
                </p>
                <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
                  {mag7.map((r) => cell(r, r.ticker))}
                </div>
              </>
            )}

            {hk.length > 0 && (
              <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3">{hk.map((r) => cell(r))}</div>
            )}
          </>
        );
      })()}

      {/* 2. 关注股票（自选） */}
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-muted-foreground">自选股 · 实时行情</h3>
        {watchCodes.length > 0 && (
          <button onClick={() => refreshWatch(watchCodes)} className="text-muted-foreground hover:text-primary" title="刷新价格">
            {watchLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          </button>
        )}
      </div>
      <GlassCard className="mb-6">
        <div className="mb-3 flex gap-2">
          <input
            value={watchInput}
            onChange={(e) => setWatchInput(e.target.value.replace(/[^\d,\s]/g, "").slice(0, 80))}
            onKeyDown={(e) => e.key === "Enter" && addWatch()}
            placeholder="加自选：可批量，如 600519 000858"
            className="w-60 rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
          <button onClick={addWatch}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25">
            <Plus className="h-4 w-4" /> 增加
          </button>
        </div>
        {watchCodes.length === 0 ? (
          <p className="text-sm text-muted-foreground/60">加上你关注的股票，随时看它们的实时价格与涨跌。数据存本地，不上传。</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {watchCodes.map((c) => {
              const q = watchQuotes[c];
              return (
                <div key={c} className="group relative rounded-lg bg-muted/25 p-3">
                  <button onClick={() => removeWatch(c)} title="移除"
                    className="absolute right-1.5 top-1.5 text-muted-foreground/40 opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100">
                    <X className="h-3.5 w-3.5" />
                  </button>
                  {}
                  {(() => {
                    const ok = q != null && Number.isFinite(q.price) && q.price > 0;
                    return (
                      <>
                        <p className="truncate text-xs text-muted-foreground">{q?.name || c}</p>
                        <p className={cn("mt-1 font-mono text-lg font-bold",
                          ok ? pctColor(q!.change_pct) : "text-muted-foreground/40")}>
                          {ok ? q!.price : "—"}
                        </p>
                        <p className={cn("text-xs", ok ? pctColor(q!.change_pct) : "text-muted-foreground/40")}>
                          {ok ? `${q!.change_pct > 0 ? "+" : ""}${q!.change_pct}%` : "行情不可用"}
                        </p>
                      </>
                    );
                  })()}
                </div>
              );
            })}
          </div>
        )}
      </GlassCard>

      {/* 4. 市场情绪 */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><Gauge className="h-4 w-4" /> 市场情绪 · 乐咕乐股口径</h3>
        <Caliber text={
          "涨停 / 真实涨停 / 跌停 / 真实跌停 / 活跃度这几个数取自乐咕乐股，不是我们算的。\n" +
          "「真实」与普通涨跌停的差额由它自己的口径决定，具体算法未公开。\n" +
          "活跃度同样是数据源自有算法，**不能按上涨家数占比来理解**；请分别查看资料期和统计口径。\n" +
          "大盘宽度按涨跌家数机械分档：上涨不足 600 家为冰点，其余看 上涨÷下跌 的比值\n" +
          "（<0.7 偏弱 / 0.7-1.2 中性 / 1.2-2.5 偏强 / ≥2.5 普涨）。\n" +
          "题材投机只按真实涨停家数分档：<30 冰点 / 30-59 普通 / 60-99 活跃 / ≥100 亢奋。"
        } />
        {sentiment?.date && <span className="text-[11px] text-muted-foreground/50">{sentiment.date}</span>}
      </div>
      <GlassCard className="mb-6">
        <p className="mb-3 text-xs text-muted-foreground">本组与下方东财收盘池分别统计；计数不一致时保留来源差异，不互相替换。</p>
        {!sentiment?.breadth ? (
          pending(ovDone)
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                { k: "大盘宽度", v: sentiment.breadth, hint: "冰点 / 偏弱 / 中性 / 偏强 / 普涨" },
                { k: "题材投机", v: sentiment.speculation, hint: "冰点 / 普通 / 活跃 / 亢奋" },
              ].map((m) => (
                <div key={m.k} className="rounded-lg bg-muted/25 p-4">
                  <p className="text-xs text-muted-foreground">{m.k}</p>
                  <p className="mt-1 text-2xl font-bold text-primary">{m.v}</p>
                  <p className="mt-1 text-[11px] text-muted-foreground/60">{m.hint}</p>
                </div>
              ))}
            </div>
            <div className="mt-3 grid grid-cols-4 gap-2">
              {sentCells.map((c) => (
                <div key={c.k} className="rounded-lg bg-muted/20 p-2 text-center">
                  <p className="truncate text-[11px] text-muted-foreground">{c.k}</p>
                  <p className={cn("mt-0.5 font-mono text-sm font-bold", c.up === null ? "text-foreground" : c.up ? "text-danger" : "text-success")}>{c.v}</p>
                </div>
              ))}
            </div>
          </>
        )}
      </GlassCard>

      {/* 4a. 东财实时池 · 盘中读数 —— 这页是盘面数据，得有今天的 */}
      {liveEmo && (
        <details open={session?.phase_key === "open" || session?.phase_key === "break"} className="mb-5"><summary className="mb-3 cursor-pointer text-sm text-muted-foreground">实时池对照（与收盘快照可能处于不同更新时间）</summary>
          <div className="mb-3 flex flex-wrap items-baseline gap-2">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground">
              <Flame className="h-4 w-4" /> 东财实时池 · 盘中读数
              <Caliber text={
                "封板率 = 最终封住家数 ÷ 摸板家数；炸板率 = 炸板未回封家数 ÷ 摸板家数。\n" +
                "摸板家数 = 涨停 + 炸板，**按家数算，不按炸板次数算**。\n" +
                "最高连板只给板数，具体是哪只票看下面那张连板股表。"
              } />
            </h3>
            {liveEmo.available ? (
              <span className="text-[11px] text-warning">
                {liveEmo.date} {liveEmo.as_of} · {liveEmo.phase}（随盘变化）
              </span>
            ) : (
              <span className="text-[11px] text-muted-foreground/50">{liveEmo.reason}</span>
            )}
          </div>
          <GlassCard className="mb-6">
            {!liveEmo.available ? (
              <p className="py-4 text-center text-sm text-muted-foreground/60">
                {liveEmo.reason || "今日暂无数据"}
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {[
                  { k: "涨停", v: liveEmo.zt_count, unit: "", up: true },
                  { k: "跌停", v: liveEmo.dt_count, unit: "", up: false },
                  { k: "最高连板", v: liveEmo.max_boards, unit: " 板", up: true },
                  { k: "连板（2板+）", v: liveEmo.lianban_count, unit: " 家", up: true },
                ].map((c) => (
                  <div key={c.k} className="rounded-lg bg-muted/30 p-3 text-center">
                    <p className="text-xs text-muted-foreground">{c.k}</p>
                    <p className={cn("mt-1 font-mono text-xl font-bold",
                                     c.v == null ? "text-muted-foreground/40"
                                                 : c.up ? "text-danger" : "text-success")}>
                      {c.v ?? "—"}{c.v != null && c.unit}
                    </p>
                  </div>
                ))}
                {[
                  { k: "封板率", v: liveEmo.seal_rate, sub: "封住 / 尝试涨停", up: true },
                  { k: "炸板率", v: liveEmo.break_rate, sub: "炸板 / 尝试涨停", up: false },
                  {
                    k: "晋级率", v: liveEmo.promotion_rate, up: true,
                    sub: liveEmo.promotion_base != null
                      ? `${liveEmo.promotion_base_date || "上一场"} 涨停 ${liveEmo.promotion_base} 家，今天又封住`
                      : "上一场涨停的票，今天又封住",
                  },
                  { k: "炸板家数", v: null as number | null, raw: liveEmo.zb_count, sub: "炸板未回封", up: false },
                ].map((c) => (
                  <div key={c.k} className="rounded-lg bg-muted/30 p-3 text-center">
                    <p className="text-xs text-muted-foreground">{c.k}</p>
                    <p className={cn("mt-1 font-mono text-xl font-bold",
                                     (c.raw ?? c.v) == null ? "text-muted-foreground/40"
                                                            : c.up ? "text-danger" : "text-success")}>
                      {c.raw != null ? c.raw
                        : c.v != null ? `${(c.v * 100).toFixed(1)}%` : "—"}
                    </p>
                    <p className="mt-0.5 text-[10px] text-muted-foreground/50">{c.sub}</p>
                  </div>
                ))}
              </div>
            )}
          </GlassCard>
        </details>
      )}

      {/* 4b. 短线情绪（连板梯队 / 打板情绪，聚合口径零个股名） */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><Flame className="h-4 w-4" /> 短线情绪 · {emotion?.date || "资料日待确认"}</h3>
        <Caliber text={
          "封板率 / 炸板率与上面那张实时卡同一个算法：分母是摸板家数（涨停 + 炸板），按家数不按次数。\n" +
          "表里的「行业 / 概念」经常只有四个字（像「互联网电」「汽车零部」）——是上游把名字截到四字，\n" +
          "不是这里显示不全；怕猜错所以不替它补全称。"
        } />
        <span className="text-[11px] text-muted-foreground/50">已收盘那一场的东财池快照 · 连板股 · 客观公开榜单</span>
        {/* 这块锚在**已收盘那一场**（复盘口径），不跟上面的实时行情一起刷 ——
            不标出来会和上面的实时块混在一起，让人以为它也在跳 */}
        {emotion?.date && (
          <span className="ml-auto text-[11px] text-muted-foreground/50">
            {emotion.date} 收盘快照 · 只有表格里标（实时）的两列随盘刷新
          </span>
        )}
      </div>
      <GlassCard className="mb-6">
        {!emotion || emotion.zt_count === undefined ? (
          pending(emoDone)
        ) : (
          <>
            {/* 关键计数 */}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[
                { k: "涨停", v: `${emotion.zt_count}`, cls: "text-danger" },
                { k: "跌停", v: `${emotion.dt_count}`, cls: "text-success" },
                { k: "最高连板", v: `${emotion.max_boards} 板`, cls: "text-primary" },
                { k: "连板（2板+）", v: `${emotion.lianban_count} 家`, cls: "text-primary" },
              ].map((c) => (
                <div key={c.k} className="rounded-lg bg-muted/25 p-3 text-center">
                  <p className="text-[11px] text-muted-foreground">{c.k}</p>
                  <p className={cn("mt-0.5 font-mono text-xl font-bold", c.cls)}>{c.v}</p>
                </div>
              ))}
            </div>
            {/* 打板情绪比率 */}
            <div className="mt-2 grid grid-cols-3 gap-2">
              {[
                { k: "封板率", v: emotion.seal_rate, hint: "封住 / 尝试涨停", strong: true },
                { k: "炸板率", v: emotion.break_rate, hint: "炸板 / 尝试涨停", strong: false },
                { k: "晋级率", v: emotion.promotion_rate,
                  hint: `前一交易日涨停的票，${emotion.date} 又封住`, strong: true },
              ].map((c) => (
                <div key={c.k} className="rounded-lg bg-muted/20 p-2.5 text-center">
                  <p className="text-[11px] text-muted-foreground">{c.k}</p>
                  <p className={cn("mt-0.5 font-mono text-sm font-bold", c.strong ? "text-danger" : "text-success")}>
                    {c.v == null ? "—" : `${(c.v * 100).toFixed(1)}%`}
                  </p>
                  <p className="mt-0.5 text-[10px] text-muted-foreground/50">{c.hint}</p>
                </div>
              ))}
            </div>
            {/* 连板股清单（2 板以上，客观公开榜单） */}
            <div className="mt-3">
              <div className="mb-1.5 flex flex-wrap items-center gap-2">
                <p className="text-[11px] text-muted-foreground">连板股（2 板以上连续涨停）· 客观公开榜单，非推荐 / 非预测</p>
                <span className="ml-auto">
                  <RunAllButton
                    dd={dd}
                    items={emotion.lianban_stocks.map(lianbanItem)}
                    nameOf={(k) => emotion.lianban_stocks.find((s) => s.code === k)?.name || k}
                  />
                </span>
              </div>
              {emotion.lianban_stocks.length === 0 ? (
                <p className="text-xs text-muted-foreground/50">今日无 2 板以上个股</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                        {["名称", "连板", "现价（实时）", "今日涨跌（实时）", "样本日成交额", "流通市值", "涨停原因", "概念", ""].map((h) => (
                          <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {emotion.lianban_stocks.map((s) => (
                        <Fragment key={s.code}>
                          <tr className="border-b border-border/30">
                            <td className="px-2 py-2"><span className="font-medium">{s.name}</span> <span className="text-xs text-muted-foreground/50">{s.code}</span></td>
                            <td className="whitespace-nowrap px-2 py-2 font-mono font-bold text-primary">{s.boards} 板</td>
                            <td className="px-2 py-2 font-mono">
                              {lianbanQuotes[s.code]?.price ?? (
                                <span className="text-muted-foreground/50" title="实时行情未取到，显示昨收">
                                  {s.price}
                                </span>
                              )}
                            </td>
                            <td className={cn("px-2 py-2 font-mono",
                                              pctColor(finite(lianbanQuotes[s.code]?.change_pct)))}>
                              {finite(lianbanQuotes[s.code]?.change_pct) !== null
                                ? `${lianbanQuotes[s.code].change_pct > 0 ? "+" : ""}${lianbanQuotes[s.code].change_pct}%`
                                : <span className="text-muted-foreground/50" title="实时行情未取到">—</span>}
                            </td>
                            <td className="whitespace-nowrap px-2 py-2 font-mono text-muted-foreground">{yi(s.amount)}</td>
                            <td className="whitespace-nowrap px-2 py-2 font-mono text-muted-foreground">{yi(s.float_cap)}</td>
                            <td className="max-w-56 px-2 py-2 text-xs">
                              {s.reason ? <span className="text-foreground">{s.reason}</span> : <span className="text-muted-foreground/50">—</span>}
                            </td>
                            <td className="whitespace-nowrap px-2 py-2 text-xs text-muted-foreground">{s.industry}</td>
                            <td className="whitespace-nowrap px-2 py-2 text-right">
                              <button
                                onClick={() => dd.toggle(lianbanItem(s))}
                                className="inline-flex items-center gap-1 rounded-lg border border-primary/50 bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/20"
                              >
                                {dd.running === s.code ? <Loader2 className="h-3 w-3 animate-spin" /> : dd.open === s.code ? <X className="h-3 w-3" /> : <Sparkles className="h-3 w-3" />}
                                {dd.open === s.code ? "收起" : dd.analysis[s.code] ? "展开" : "深入分析"}
                              </button>
                            </td>
                          </tr>
                          {dd.open === s.code && (
                            <DeepDivePanel
                              dd={dd}
                              stockKey={s.code}
                              colSpan={9}
                              noteTitle={`连板深析 · ${s.name} ${s.boards}板`}
                              onRerun={() => dd.rerun(lianbanItem(s))}
                            />
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </GlassCard>

      {}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><BarChart3 className="h-4 w-4" /> 全市场成交额 TOP20</h3>
        <Caliber text={
          "沪深京 A 股按当日累计成交额从大到小排。\n" +
          "盘中看到的成交额是「到刷新那一刻为止」的累计值，不是收盘值；总市值按当前价算。\n" +
          "名字前面带 C 的是上市不满一年的次新股标记，不是名字的一部分。"
        } />
        <span className="text-[11px] text-muted-foreground/50">客观公开榜单，非推荐 / 非预测 / 不构成投资建议</span>
        {turnover?.updated && <span className="ml-auto text-[11px] text-muted-foreground/50">参考行情日 {turnover.quote_date || "未确认"} · 抓取于 {turnover.updated}</span>}
      </div>
      <GlassCard className="mb-6">
        {!turnover || turnover.stocks.length === 0 ? (
          turnover?.reason ? <p className="p-4 text-sm text-muted-foreground">{turnover.reason}</p> : pending(toDone)
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {["#", "名称", "现价", "涨跌%", "成交额", "总市值", "行业"].map((h) => (
                    <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {turnover.stocks.map((s, i) => (
                  <tr key={s.code} className="border-b border-border/30">
                    <td className="px-2 py-2 font-mono text-xs text-muted-foreground/50">{i + 1}</td>
                    <td className="px-2 py-2"><span className="font-medium">{s.name}</span> <span className="text-xs text-muted-foreground/50">{s.code}</span></td>
                    <td className="px-2 py-2 font-mono">{s.price ?? "—"}</td>
                    <td className={cn("px-2 py-2 font-mono", s.pct == null ? "text-muted-foreground" : pctColor(s.pct))}>
                      {s.pct == null ? "—" : `${s.pct > 0 ? "+" : ""}${s.pct}%`}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 font-mono">{yi(s.amount)}</td>
                    <td className="whitespace-nowrap px-2 py-2 font-mono text-muted-foreground">{yi(s.mcap)}</td>
                    <td className="whitespace-nowrap px-2 py-2 text-xs text-muted-foreground">{s.industry}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>

      {/* 5. 板块资金趋势榜（行业） */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><TrendingUp className="h-4 w-4" /> 板块资金趋势榜</h3>
        <Caliber text={
          "净流入 / 流入 / 流出取自同花顺行业资金流的**盘中即时值**，单位亿元，净流入 = 流入 − 流出。\n" +
          "⚠️ 那边没说明这是主力资金还是全部成交资金，所以**不能当作主力净流入**来读。\n" +
          "涨跌% 是行业整体涨幅；成分股数是这个行业的公司总数，不是上涨家数、也不是涨停家数。"
        } />
        <span className="text-[11px] text-muted-foreground/50">行业 · 按今日净流入排序</span>
      </div>
      <GlassCard className="mb-6">
        {sectors.length === 0 ? (
          pending(ovDone)
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {["行业", "涨跌%", "今日净流入", "流入(亿)", "流出(亿)", "成分股数"].map((h) => (
                    <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sectors.slice(0, 15).map((s) => (
                  <tr key={s.name} className="border-b border-border/30">
                    <td className="px-2 py-2 font-medium">{s.name}</td>
                    <td className={cn("px-2 py-2 font-mono", pctColor(s.pct))}>{s.pct > 0 ? "+" : ""}{s.pct}%</td>
                    <td className={cn("px-2 py-2 font-mono", pctColor(s.net))}>{s.net > 0 ? "+" : ""}{fmt(s.net)} 亿</td>
                    <td className="px-2 py-2 font-mono text-muted-foreground">{fmt(s.inflow)}</td>
                    <td className="px-2 py-2 font-mono text-muted-foreground">{fmt(s.outflow)}</td>
                    <td className="px-2 py-2 font-mono text-muted-foreground">{s.firms}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>

      {/* 6. 资金轮动 */}
      <div className="mb-3 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-muted-foreground"><ArrowDownUp className="h-4 w-4" /> 资金轮动</h3>
        <Caliber text={
          "就是上面那张榜的两头：流入榜只放净流入为正的、流出榜只放为负的，各取前六。\n" +
          "某一边不足六个就只列够格的那几个 —— 普跌日往往只有两三个行业真净流入。\n" +
          "口径同上：同花顺行业资金流盘中即时值，不能当主力净流入读。"
        } />
        <span className="text-[11px] text-muted-foreground/50">板块级净流入 / 流出</span>
      </div>
      <div className="mb-2 grid gap-4 md:grid-cols-2">
        {[
          {
            title: "流入 Top", icon: TrendingUp, color: "text-danger",
            rows: sectors.filter((s) => s.net > 0).slice(0, 6),
            empty: "今日没有行业净流入",
          },
          {
            title: "流出 Top", icon: TrendingDown, color: "text-success",
            rows: sectors.filter((s) => s.net < 0)
              .sort((a, b) => a.net - b.net).slice(0, 6),
            empty: "今日没有行业净流出",
          },
        ].map((col) => (
          <GlassCard key={col.title}>
            <h4 className={cn("mb-3 flex items-center gap-1.5 text-sm font-semibold", col.color)}><col.icon className="h-4 w-4" /> {col.title}</h4>
            {col.rows.length === 0 ? (
              ovDone && sectors.length > 0
                ? <p className="py-4 text-center text-sm text-muted-foreground/60">{col.empty}</p>
                : pending(ovDone)
            ) : (
              <div className="space-y-1.5">
                {col.rows.map((s, i) => (
                  <div key={s.name} className="flex items-center gap-3 border-b border-border/30 pb-1.5 text-sm last:border-0">
                    <span className="w-5 text-xs text-muted-foreground/50">{i + 1}</span>
                    <span className="flex-1 truncate">{s.name}</span>
                    <span className={cn("font-mono text-xs", pctColor(s.pct))}>{s.pct > 0 ? "+" : ""}{s.pct}%</span>
                    <span className={cn("w-20 text-right font-mono text-xs", pctColor(s.net))}>{s.net > 0 ? "+" : ""}{fmt(s.net)} 亿</span>
                  </div>
                ))}
              </div>
            )}
          </GlassCard>
        ))}
      </div>

      <UnlockCalendarPanel watchCodes={watchCodes} />
      <Disclaimer />
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { FlaskConical, Play, FileText, Loader2, CandlestickChart, Share2, Swords, Star, Check } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageTabs } from "@/components/ui/PageTabs";
import { SegmentedToggle } from "@/components/ui/SegmentedToggle";
import { GlassCard } from "@/components/ui/GlassCard";
import { Disclaimer } from "@/components/ui/Disclaimer";
import { useAiPage } from "../../../core/ai/pageContext";
import { backend, ApiError, type RunListItem, type ResearchStatus } from "@/lib/backend";
import { api, type Quote } from "@/lib/api";
import { addWatch as addToWatch, loadWatch } from "@/lib/watchlist";
import { ResearchReport } from "../components/ResearchReport";
import { KlineCard } from "../components/KlineCard";
import { EarningsBriefCard } from "../components/EarningsBriefCard";
import { EventStudyCard, type EventStudyData } from "../components/EventStudyCard";
import { eventContextText } from "@/lib/eventStudyView";
import { earningsSummary, type EarningsBriefResult } from "@/lib/earningsBrief";
import { SymbolSearch } from "../components/SymbolSearch";
import { marketOfSymbol, normalizeMarketSymbol, type MarketCode } from "@/lib/marketSymbol";
import { ResearchRunItem, ResearchFailureNotice } from "../components/ResearchRunItem";
import { ReportCompare } from "../components/ReportCompare";
import { RunCompareCard } from "../components/RunCompareCard";
import { WeeklyCompareButton } from "../components/WeeklyCompareButton";
import { subscriptionConfig } from "@/lib/aiConnection";
import { ShareCardPanel } from "../components/ShareCardPanel";
import { ThesisPanel } from "../components/ui/ThesisPanel";

/**
 * 「个股深挖」（研究工具下）—— 选一只股票，走势、财报、事件回看直接看（概览）；要完整研究再切分页。
 *
 * 🔴 2026-10 改成以股票为中心：原来走势和财报藏在「个股研究 → 看走势」按钮后面，没人找得到。
 *    现在股票只在页头选一次，两个分页共用。正在看哪只记在地址里（?symbol=），自选、持仓、候选池点过来都带它；刷新、后退都还在。
 *    多空辩论 10-09 起是研究工具下单独一页（Simon 定），页头有按钮带着这只过去；旧的 ?tab=debate 跳过去。
 *
 * 六阶段研究引擎（深度研究分页）：
 * 🔴 为什么新建这一页：这条链路**后端一直都有**（公司画像 → 财务 → 一致预期 → 估值 →
 *    风险 → 成稿，强制季度拆分 / TTM / 预测分歧 / 四情景估值 / 反证 / 裁决点），
 *    `backend.ts` 里 `runs()`、`report()` 也早就写好，但**没有任何路由指向它** ——
 *    用户完全不知道产品能生成带证据链与裁决点的正式研究。
 *    「我的研报」是上传外部文件的归档柜，不是这个。
 *
 * ⚠️ 完整研究会消耗模型额度，耗时受来源、取数与校验重试影响，所以：
 *    ① 必须用户显式点，页面打开不自动跑；
 *    ② 阶段逐个显示，不是一个转圈 —— 用户要知道它在哪一步、卡在哪一步。
 */

/** 阶段的中文名。⚠️ 阶段由垂类包定义，这里只做显示；认不出的**原样显示**，不猜 */
const STAGE_CN: Record<string, string> = {
  profile: "公司画像",
  financials: "财务",
  estimates: "一致预期",
  valuation: "估值",
  risk: "风险与卡口",
  report: "成稿",
};

const STATUS_CN: Record<string, string> = {
  complete: "完成",
  running: "进行中",
  cancelling: "正在取消",
  finalizing: "正在归档收尾（已不能取消）",
  cancelled: "已取消",
  failed: "失败",
  pending: "待跑",
  incomplete: "资料不完整",
  unvalidated: "未通过校验",
};

type Tab = "overview" | "research";
const isTab = (v: string | null): v is Tab => v === "overview" || v === "research";
const MARKET_CN: Record<MarketCode, string> = { CN: "A 股", HK: "港股", US: "美股" };
const RUNNING = ["running", "cancelling", "finalizing"];

export function Research() {
  const [params, setParams] = useSearchParams();
  /** 正在看哪只（规范代码）。只认地址里的 ?symbol= —— 选股、从别的页点过来、后退都走这一条 */
  const chartSymbol = normalizeMarketSymbol(params.get("symbol")) ?? "";
  const tabParam = params.get("tab");
  const tab: Tab = isTab(tabParam) ? tabParam : "overview";
  const setQuery = (patch: Record<string, string | null>, replace: boolean) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    for (const [k, v] of Object.entries(patch)) { if (v === null) next.delete(k); else next.set(k, v); }
    return next;
  }, { replace });
  const pickTab = (t: Tab) => setQuery({ tab: t === "overview" ? null : t }, true);

  const [runs, setRuns] = useState<RunListItem[]>([]);
  /** 页头搜索框里的字（还没选定的输入）；选定以后换成规范代码 */
  const [symbol, setSymbol] = useState(chartSymbol);
  useEffect(() => { setSymbol(chartSymbol); }, [chartSymbol]);
  const [pickErr, setPickErr] = useState("");
  /** 每换一次股票加一：打开归档报告的途中人又换了股票，以人最后选的为准（见 openReport） */
  const pickSeq = useRef(0);
  /** 选定一只：只换正在看的股票，**不开始研究**（研究要到深度研究分页里点「开始」） */
  const choose = (raw: string) => {
    const c = normalizeMarketSymbol(raw);
    if (!c) { setPickErr("请输入 A 股 6 位代码、港股代码（如 00700）或美股代码（如 AAPL），或在下拉里选一个"); return; }
    setPickErr("");
    if (c !== chartSymbol) { pickSeq.current += 1; setQuery({ symbol: c }, false); }
  };
  /** 下拉里选中的那只(规范代码)。手敲的字母代码要先报出公司名、再按一次才开始 */
  const pickedCode = useRef<string | null>(null);
  const [confirm, setConfirm] = useState<{ code: string; text: string } | null>(null);
  /** 这只的名称和最新价（批量行情，三个市场都走它） */
  const [quote, setQuote] = useState<{ code: string; q: Quote | null } | null>(null);
  useEffect(() => {
    if (!chartSymbol) return;
    let off = false;
    api.quote(chartSymbol).then(
      (m) => { if (!off) setQuote({ code: chartSymbol, q: m[chartSymbol] ?? null }); },
      () => { if (!off) setQuote({ code: chartSymbol, q: null }); });
    return () => { off = true; };
  }, [chartSymbol]);
  const q = quote?.code === chartSymbol ? quote.q : undefined;   // undefined = 还在取
  const stockName = q?.name ?? "";
  const market = marketOfSymbol(chartSymbol);
  /** 自选：页头一键加；没选股票时，概览里列出来给人点 */
  const [watchCodes, setWatchCodes] = useState<string[]>(loadWatch);
  const inWatch = !!chartSymbol && watchCodes.includes(chartSymbol);
  const addWatch = () => {
    if (!chartSymbol) return;
    // 增量加一只：排队写，不拿缓存拼整张列表（连着加两只时会把上一只删掉）
    const p = addToWatch(chartSymbol);
    setWatchCodes(loadWatch());
    void p.catch((e) => { setWatchCodes(loadWatch()); setPickErr(`没加进自选：${e instanceof Error ? e.message : String(e)}`); });
  };
  const [watchQuotes, setWatchQuotes] = useState<Record<string, Quote>>({});
  useEffect(() => {
    if (chartSymbol || !watchCodes.length) return;
    let off = false;
    api.quote(watchCodes.slice(0, 12).join(",")).then((m) => { if (!off) setWatchQuotes(m); }, () => { /* 只是补名称，取不到就显示代码 */ });
    return () => { off = true; };
  }, [chartSymbol, watchCodes]);
  const [archiveAll, setArchiveAll] = useState(false);
  /** 财报前瞻与复盘(卡片自己取,交给这里拼「问 Agent」的上下文) */
  const [earn, setEarn] = useState<EarningsBriefResult | null>(null);
  /** 事件回看(A 股;卡片点了才跑,结果交给这里拼上下文) */
  const [events, setEvents] = useState<EventStudyData | null>(null);
  const [sharing, setSharing] = useState(false);
  const [scope, setScope] = useState<"core" | "full">("core");
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [err, setErr] = useState("");
  const [active, setActive] = useState<ResearchStatus | null>(null);
  /** active 那次运行是哪只（规范代码）；查不到时为 null */
  const [activeCode, setActiveCode] = useState<string | null>(null);
  /** 🔴 `null` = 还没取回；`[]` = 真的一次都没跑过。**两者不能混** ——
   *  接口挂了却渲染成"还没有研究运行",就是把故障说成了事实。 */
  const [runsErr, setRunsErr] = useState<string | null>(null);
  /** 归档删除:正在删的 run_id / 删除错误(不写成空,避免把故障说成"删掉了") */
  const [deleting, setDeleting] = useState<string | null>(null);
  const [delErr, setDelErr] = useState("");
  /** 轮询中断的原因。⚠️ 停掉轮询而不说话 = 页面永远停在"进行中"，开始按钮也一直禁用 */
  const [watchErr, setWatchErr] = useState<string | null>(null);
  /** 只认最后一次点击的运行：慢请求回来时不许覆盖已经切走的那一个 */
  const wantRun = useRef<string | null>(null);
  const watchGeneration = useRef(0);
  const [report, setReport] = useState<Awaited<ReturnType<typeof backend.report>> | null>(null);
  const timer = useRef<number | null>(null);
  // 换了另一只股票：上一只的报告收起来（它的归档还在下面，点开会把股票一起切回去）；
  // 上一只的报错不留；上一只已经结束的研究也收起来，还在跑的留着、卡片上写明是哪只
  const reportSymbol = report ? normalizeMarketSymbol(runs.find((r) => r.run_id === report.run_id)?.symbol) : null;
  useEffect(() => {
    pickSeq.current += 1;
    if (reportSymbol && reportSymbol !== chartSymbol) { setReport(null); setSharing(false); }
    setErr("");
    if (active?.finished_at != null && activeCode !== chartSymbol) setActive(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartSymbol]);

  const loadRuns = () =>
    backend.runs(30)
      .then((r) => { setRuns(r); setRunsErr(null); })
      // 🔴 不写成空数组 —— 空数组会被读成"确实一次都没跑过"
      .catch((e) => setRunsErr(e instanceof ApiError ? e.message : String(e)));
  useEffect(() => {
    void loadRuns();
    // 组件卸载要停掉轮询，否则它会在后台一直打接口
    return () => { wantRun.current = null; watchGeneration.current += 1; if (timer.current) window.clearInterval(timer.current); };
  }, []);

  /** 删除一次研究运行(归档清理)。进行中的 run 由后端拒(run_in_progress),这里乐观刷新列表。
   *  🔴 删除不可逆:确认框带标的,二次确认后才调后端(后端另有 manifest/control 双防线)。 */
  const removeRun = async (id: string) => {
    if (deleting) return;
    const target = runs.find((r) => r.run_id === id);
    const label = target?.name ?? target?.symbol ?? id;
    if (!window.confirm(`删除「${label}」的这次研究归档？\n\n其原始数据 / 计算 / 报告会一并删除，不可恢复。`)) return;
    setDeleting(id); setDelErr("");
    try {
      await backend.deleteRun(id);
      await loadRuns();
    } catch (e) {
      setDelErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setDeleting(null);
    }
  };

  /** 轮询一次运行的状态，直到它不再是 running */
  const watch = (id: string) => {
    wantRun.current = id;
    const generation = ++watchGeneration.current;
    if (timer.current) window.clearInterval(timer.current);
    setWatchErr(null);
    // 🔴 一次网络抖动不该让整块永久停住(页面会一直显示"进行中"、开始按钮一直禁用)。
    //    连续失败到上限才停,并且**把原因说出来** —— 停掉却不说话是静默失败。
    let fails = 0;
    let pending = false;
    const MAX_FAILS = 3;
    timer.current = window.setInterval(() => {
      if (pending || generation !== watchGeneration.current) return;
      pending = true;
      backend
        .researchStatus(id)
        .then((st) => {
          if (wantRun.current !== id || generation !== watchGeneration.current) return;
          fails = 0; setWatchErr(null);
          setActive(st);
          if (st.finished_at !== null) {
            watchGeneration.current += 1;
            if (timer.current) window.clearInterval(timer.current);
            void loadRuns();
          }
        })
        .catch((e) => {
          if (wantRun.current !== id || generation !== watchGeneration.current) return;
          fails += 1;
          const msg = e instanceof ApiError ? e.message : String(e);
          if (fails >= MAX_FAILS) {
            watchGeneration.current += 1;
            if (timer.current) window.clearInterval(timer.current);
            setWatchErr(`连着 ${MAX_FAILS} 次没问到进度，已停止跟踪：${msg}。研究**可能仍在后台跑**，稍后回到这一页从下面的归档里打开它。`);
          } else {
            setWatchErr(`第 ${fails} 次没问到进度（还在重试）：${msg}`);
          }
        }).finally(() => { pending = false; });
    }, 4000);
  };

  const start = async () => {
    setErr(""); setReport(null);
    const pending = confirm; setConfirm(null);
    const code = chartSymbol;
    const market = marketOfSymbol(code);
    if (!market) { setErr("先在页头选一只股票（A 股、港股、美股都行）"); return; }
    // 研究用的代码不带后缀:港股 00700.HK → 00700 + 市场 HK;美股原样 + 市场 US;A 股不传市场(后台按代码判断沪深北)
    const runSymbol = market === "HK" ? code.slice(0, 5) : code;
    setStarting(true);
    try {
      const quote = await backend.fetch(market === "CN" ? "tx_quote" : "global_quote_ref", { symbol: runSymbol }).catch(() => null);
      const companyName = quote?.envelope.evidence.find((e) => e.field === "security_name")?.value;
      // 🔴 港美股:字母既可能是代码也可能是拼音(md / ba / ms 都是真实美股代码)。查不到公司名不开跑;
      //    不是从下拉选的,先报出要研究的是哪家公司,再按一次才开始(阶段 D 审查 P2)
      if (market !== "CN") {
        const name = typeof companyName === "string" ? companyName.trim() : "";
        const label = market === "HK" ? "港股" : "美股";
        if (!name) { setErr(`没查到${label} ${runSymbol} 的报价和公司名。输入的是名称或拼音的话，请在下拉里选一个；是代码的话请核对后再试`); return; }
        if (pickedCode.current !== code && pending?.code !== code) {
          setConfirm({ code, text: `将研究${label} ${runSymbol}（${name}）。确认是这家再按一次「开始」；输入的是拼音的话，请在下拉里选` });
          return;
        }
      }
      const r = await backend.startResearch({
        symbol: runSymbol,
        ...(market === "CN" ? {} : { market }),
        ...(typeof companyName === "string" && companyName.trim() ? { company_name: companyName.trim() } : {}),
        endpoints: scope,
        knowledge: "on",
      });
      setActive({
        run_id: r.run_id, exists: true, status: "running", exit_code: null,
        stages: [], evidence_count: null, calculation_count: null, finished_at: null,
      });
      setActiveCode(code);
      watch(r.run_id);
      void loadRuns();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  /** 研究对比里「请另一家 Agent 也研究这只」：对象、取数范围沿用这次，只这一次用那家的订阅。
   *  报告留着不收（对比卡就在它旁边）；进度照常显示在上方，错误交回对比卡显示 */
  const runWith = async (run: RunListItem, provider: string, endpoints: "core" | "full") => {
    const code = normalizeMarketSymbol(run.symbol);
    const market = marketOfSymbol(code);
    if (!code || !market) throw new Error("这次研究没记下是哪只股票，没法再研究一次");
    if (RUNNING.includes(active?.status ?? "")) throw new Error("页面上方还有一次研究在跑，跑完才能再开");
    const r = await backend.startResearch({
      symbol: market === "HK" ? code.slice(0, 5) : code,
      ...(market === "CN" ? {} : { market }),
      ...(run.name?.trim() ? { company_name: run.name.trim() } : {}),
      endpoints,
      knowledge: "on",
    }, subscriptionConfig(provider));
    setErr("");
    setActive({
      run_id: r.run_id, exists: true, status: "running", exit_code: null,
      stages: [], evidence_count: null, calculation_count: null, finished_at: null,
    });
    setActiveCode(code);
    watch(r.run_id);
    void loadRuns();
  };

  const cancel = async () => {
    if (!active || cancelling) return;
    const id = active.run_id;
    const generation = ++watchGeneration.current;
    if (timer.current) window.clearInterval(timer.current);
    setCancelling(true); setErr("");
    try {
      const st = await backend.cancelResearch(id);
      if (wantRun.current === id && generation === watchGeneration.current) { setActive(st); if (!st.finished_at) watch(id); }
    } catch (e) {
      if (wantRun.current === id && generation === watchGeneration.current) {
        setErr(e instanceof ApiError ? e.message : String(e)); watch(id);
      }
    }
    finally { setCancelling(false); }
  };

  const openReport = async (id: string) => {
    setErr("");
    const generation = ++watchGeneration.current;
    if (timer.current) window.clearInterval(timer.current);
    // 🔴 连点两个运行时,慢的那个后回来会盖掉快的 —— 报告是 A 的、变化列表是 B 的,
    //    而页面上完全看不出这是两次运行拼起来的。⇒ 只认最后一次点击。
    wantRun.current = id;
    // 打开途中人在页头换了股票：这次打开作废，不然报告会先挂在新股票下面、地址再被跳回报告那只
    const pick = pickSeq.current;
    const stale = () => wantRun.current !== id || generation !== watchGeneration.current || pick !== pickSeq.current;
    try {
      const r = await backend.report(id);
      if (stale()) return;
      const st = await backend.researchStatus(id).catch(() => null);
      if (stale()) return;
      const c = normalizeMarketSymbol(runs.find((x) => x.run_id === id)?.symbol);
      setReport(r);
      if (st) { setActive(st); setActiveCode(c); if (!st.finished_at) watch(id); }
      if (c && c !== chartSymbol) setQuery({ symbol: c }, false);
    } catch (e) {
      if (wantRun.current !== id) return;
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };

  useAiPage({
    key: `research:${chartSymbol || "none"}`,
    title: chartSymbol ? `个股深挖 · ${stockName || chartSymbol}` : "个股深挖",
    context: (chartSymbol
      ? `正在看：${stockName ? `${stockName}（${chartSymbol}）` : chartSymbol}`
        + (q?.price != null ? `，现价 ${q.price}${q.change_pct != null ? `（${q.change_pct > 0 ? "+" : ""}${q.change_pct}%）` : ""}` : "") + "\n\n"
      : "还没选股票。\n\n")
      + (active && activeCode === chartSymbol
      ? `当前研究运行 ${active.run_id}：状态 ${active.status}｜证据 ${active.evidence_count ?? "—"} 条｜计算 ${active.calculation_count ?? "—"} 项｜` +
        `阶段 ${active.stages.map((s) => `${STAGE_CN[s.stage] ?? s.stage}=${STATUS_CN[s.status] ?? s.status}`).join("、")}` +
        (report?.availability === "ready" && report.run_id === active.run_id && report.report ? `\n\n报告全文：\n${report.report.slice(0, 6000)}` : "")
      : (active ? `另有一次研究运行 ${active.run_id}（${activeCode ?? "另一只股票"}，状态 ${active.status}），不是正在看的这只。\n` : "")
        + `研究归档共 ${runs.length} 次运行。六阶段研究支持 A 股、港股、美股。`)
      // 🔴 只拼**正在看的这只**的财报卡结果:慢请求回来时可能已经换了票
      + (earn && earn.symbol === chartSymbol.toUpperCase() ? `\n\n个股财报(${earn.symbol}):\n${earningsSummary(earn).join("\n")}` : "")
      + (events && events.symbol === chartSymbol ? `\n\n${eventContextText(events.result)}` : ""),
    suggestions: tab === "overview"
      ? (earn ? ["过去几次财报后的表现", "下一份财报要看什么", "这只最近的走势怎么样"] : ["这只最近的走势怎么样", "下一份财报要看什么", "有哪些值得留意的事件"])
      : ["这份研究的裁决点是什么", "哪些数据有缺口", "反证部分说了什么"],
  }, tabParam !== "debate");

  const currentRun = report ? runs.find((r) => r.run_id === report.run_id) ?? null : null;
  const stagesToShow = active
    ? (active.stages.length ? active.stages : Object.keys(STAGE_CN).map((s) => ({ stage: s, status: "pending" })))
    : [];

  const researchBusy = RUNNING.includes(active?.status ?? "");
  const mineRuns = chartSymbol ? runs.filter((r) => normalizeMarketSymbol(r.symbol) === chartSymbol) : runs;
  const shownRuns = chartSymbol && !archiveAll ? mineRuns : runs;
  const busyDot = <Loader2 className="h-3.5 w-3.5 animate-spin" aria-label="进行中" />;

  // 旧链接里的多空辩论分页：带着这只股票去「多空辩论」页（hook 都在上面，这里提前返回不打乱 hook 顺序）
  if (tabParam === "debate") {
    const q = new URLSearchParams(params);
    q.delete("tab");
    const s = q.toString();
    return <Navigate replace to={`/debate${s ? `?${s}` : ""}`} />;
  }

  return (
    <div>
      <PageHeader
        title="个股深挖"
        subtitle="选一只股票：走势、财报、事件回看直接看；要完整研究再切到「深度研究」。A 股、港股、美股都支持"
      />

      {/* 选股：只在这里选一次，两个分页共用 */}
      <GlassCard className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <span className="block text-xs text-muted-foreground">代码 / 名称 / 拼音</span>
            <SymbolSearch
              value={symbol}
              onChange={(v) => { setSymbol(v); setConfirm(null); }}
              onPick={(m) => { pickedCode.current = normalizeMarketSymbol(m.symbol); choose(m.symbol); }}
              onEnter={() => choose(symbol)}
              placeholder="600519 / 腾讯 / AAPL"
              title="A 股、港股（00700）和美股（AAPL）都可以"
              ariaLabel="股票代码"
              className="w-56"
            />
          </div>
          <button onClick={() => choose(symbol)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-4 py-2 text-sm hover:bg-muted/40">
            <CandlestickChart className="h-4 w-4" /> 看这只
          </button>
          {chartSymbol && (
            <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 pb-1.5 text-sm">
              <span className="font-semibold">{stockName || chartSymbol}</span>
              {stockName && <span className="font-mono text-xs text-muted-foreground">{chartSymbol}</span>}
              {market && <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">{MARKET_CN[market]}</span>}
              {q?.price != null && (
                <span className={cn("font-mono text-xs", q.change_pct == null ? "text-muted-foreground" : q.change_pct > 0 ? "text-danger" : q.change_pct < 0 ? "text-success" : "text-muted-foreground")}>
                  {q.price}{q.change_pct != null && ` ${q.change_pct > 0 ? "+" : ""}${q.change_pct}%`}
                </span>
              )}
              <button onClick={addWatch} disabled={inWatch}
                className="inline-flex items-center gap-1 rounded-md border border-border/60 px-2 py-0.5 text-xs text-muted-foreground hover:text-primary disabled:hover:text-muted-foreground">
                {inWatch ? <><Check className="h-3 w-3" /> 已在自选</> : <><Star className="h-3 w-3" /> 加入自选</>}
              </button>
              {market === "CN" && (
                <Link to={`/debate?symbol=${encodeURIComponent(chartSymbol)}`} title="同一份数据，多方与空方各自立论、互相质疑"
                  className="inline-flex items-center gap-1 rounded-md border border-border/60 px-2 py-0.5 text-xs text-muted-foreground hover:text-primary">
                  <Swords className="h-3 w-3" /> 多空辩论
                </Link>
              )}
            </div>
          )}
        </div>
        {pickErr && <p role="alert" className="mt-2 text-xs text-destructive">{pickErr}</p>}
        {/* 字母既可能是美股代码也可能是拼音（gzmt）：查不到这只的行情时说出来，别让下面一排卡片各报各的错 */}
        {chartSymbol && q === null && market !== "CN" && (
          <p className="mt-2 text-xs text-warning">没查到 {chartSymbol} 的行情。输入的是名称或拼音的话，请在下拉里选一个；是代码的话请核对。</p>
        )}
      </GlassCard>

      <PageTabs label="个股分页" value={tab} onChange={pickTab} options={[
        { key: "overview", label: "概览", icon: CandlestickChart },
        { key: "research", label: "深度研究", icon: FlaskConical, badge: researchBusy && tab !== "research" ? busyDot : undefined },
      ]} />

      {/* 概览：切走只藏起来，图和事件回看的结果都还在（美股事件回看要跑半分钟，别让人重跑） */}
      <div hidden={tab !== "overview"}>
        {chartSymbol ? <>
          <KlineCard symbol={chartSymbol} />
          {/* 财报前瞻与复盘(#18):下一份财报日、预期与预告,过去几次财报后的当天涨跌;数字全由 calc 算 */}
          <EarningsBriefCard symbol={chartSymbol} onData={setEarn} />
          {/* 事件回看：A 股看业绩预告和重要股东增减持，港股、美股看业绩公布 */}
          <EventStudyCard symbol={chartSymbol} onData={setEvents} />
          {/* 我的论点与判据：先看事实，再对照自己写下的判断；事件日历指引用户来这里写（审计第三轮 C7） */}
          <ThesisPanel key={chartSymbol} symbol={chartSymbol} name={stockName || undefined} />
        </> : (
          <GlassCard className="mb-4">
            <h3 className="mb-2 font-semibold">先在上面选一只股票</h3>
            <p className="text-sm leading-6 text-muted-foreground">
              选好就能直接看：<b className="text-foreground">K 线走势</b>（日线 / 周线 / 月线和常用指标）、
              <b className="text-foreground">财报前瞻与复盘</b>（下一份财报日、预期、过去几次财报后的涨跌）、
              <b className="text-foreground">事件回看</b>（过去同类事件前后的平均走势）。不用接 AI，A 股、港股、美股都行。
            </p>
            {watchCodes.length > 0 && (
              <div className="mt-3">
                <p className="mb-1.5 text-xs text-muted-foreground">从自选里挑一只：</p>
                <div className="flex flex-wrap gap-1.5">
                  {watchCodes.slice(0, 12).map((c) => (
                    <button key={c} onClick={() => choose(c)}
                      className="rounded-md border border-border px-2 py-1 text-xs hover:border-primary/50 hover:text-primary">
                      {watchQuotes[c]?.name || c}{watchQuotes[c]?.name && <span className="ml-1 font-mono text-muted-foreground">{c}</span>}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </GlassCard>
        )}
      </div>

      <div hidden={tab !== "research"}>
      <GlassCard className="mb-4">
        <div className="mb-3 flex items-center gap-2">
          <FlaskConical className="h-4 w-4 text-primary" />
          <h3 className="font-semibold">跑一次研究{chartSymbol && <span className="ml-1.5 font-normal text-muted-foreground">· {stockName ? `${stockName}（${chartSymbol}）` : chartSymbol}</span>}</h3>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="space-y-1.5">
            <span className="block text-xs text-muted-foreground">取数范围</span>
            <select
              value={scope}
              onChange={(e) => setScope(e.target.value as "core" | "full")}
              className="rounded-lg border border-border bg-background/60 px-3 py-2 text-sm"
            >
              <option value="core">核心（快，够形成判断）</option>
              <option value="full">完整（含适用的扩展数据，较慢）</option>
            </select>
          </label>
          <button
            onClick={() => void start()}
            disabled={!chartSymbol || starting || active?.status === "running" || active?.status === "cancelling" || active?.status === "finalizing"}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary ring-1 ring-primary/30 hover:bg-primary/25 disabled:opacity-50"
          >
            {starting || active?.status === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            开始
          </button>
          {(active?.status === "running" || active?.status === "cancelling") && (
            <button onClick={() => void cancel()} disabled={cancelling || active.status === "cancelling"}
              className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-50">
              {cancelling || active.status === "cancelling" ? "正在取消，等待后台停止…" : "取消研究"}
            </button>
          )}
        </div>
        {!chartSymbol && <p className="mt-2.5 text-xs text-primary">先在页头选一只股票。</p>}
        {researchBusy && activeCode !== chartSymbol && (
          <p className="mt-2.5 text-xs text-warning">下面还在跑的是 {activeCode ?? "另一只股票"} 的研究；它结束后才能为当前这只开始。</p>
        )}
        {/* 不用固定时长承诺掩盖不同来源和校验重试的差异。 */}
        <p className="mt-2.5 text-xs leading-relaxed text-muted-foreground">
          六阶段：公司画像 → 财务 → 一致预期 → 估值 → 风险 → 成稿。一次完整研究会多轮调用你配置的模型，并按标的获取适用数据。取数、模型响应和校验重试会影响耗时，<b className="text-foreground">可能需要数十分钟或更久</b>，
          消耗你自己的模型额度。产出是一份带证据 id、确定性计算、数据缺口与裁决点的报告 ——
          每个数字都能追回它的来源。
        </p>
        {err && <p role="alert" className="mt-2 text-xs text-destructive">{err}</p>}
        {confirm && confirm.code === chartSymbol && <p role="status" className="mt-2 text-xs text-primary">{confirm.text}</p>}
        {/* 🔴 轮询断了要说话:停掉却不说,页面会一直显示"进行中"、开始按钮一直禁用,
            而研究其实可能已经跑完了 —— 用户只会以为它卡住了。 */}
        {watchErr && <p className="mt-2 text-xs text-warning">{watchErr}</p>}
      </GlassCard>

      {active && (
        <GlassCard className="mb-4">
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
            <h3 className="font-semibold">{active.run_id}</h3>
            <span className="rounded-full bg-muted/50 px-2 py-0.5 text-[11px]">{STATUS_CN[active.status] ?? active.status}</span>
            {active.evidence_count !== null && <span className="text-xs text-muted-foreground">证据 {active.evidence_count} 条</span>}
            {active.calculation_count !== null && <span className="text-xs text-muted-foreground">计算 {active.calculation_count} 项</span>}
            {/* 跑完了就在这里给入口(#30):以前要到下面「研究归档」里点这次运行才看得到报告。
                资料不完整、失败的也给：点开等于从归档打开，报告能不能看由报告卡自己说明 */}
            {active.finished_at !== null && report?.run_id !== active.run_id && (
              <button type="button" onClick={() => void openReport(active.run_id)}
                className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-primary/40 px-2.5 py-1 text-xs text-primary hover:bg-primary/10">
                <FileText className="h-3.5 w-3.5" /> 查看报告
              </button>
            )}
          </div>
          {/* 阶段逐个显示 —— 用户要知道它在哪一步，而不是看一个转圈 */}
          <div className="research-stages" aria-label="研究阶段索引">
            {stagesToShow.map((s, index) => (
              <span
                key={s.stage}
                data-status={s.status}
                className="research-stage min-w-0 rounded-b text-xs"
              >
                <span className="mb-2 block font-mono text-[10px] text-primary">{String(index + 1).padStart(2, "0")}</span>
                <span className="block font-medium">{STAGE_CN[s.stage] ?? s.stage}</span>
                <span className="mt-1 block text-[10px] text-muted-foreground">{STATUS_CN[s.status] ?? s.status}</span>
              </span>
            ))}
          </div>
          {active.failure && <ResearchFailureNotice failure={active.failure} />}
        </GlassCard>
      )}

      {/* 「和上次比变了什么」:同一家公司任选两次研究,关键数据逐条对齐、报告逐节比 */}
      {report && currentRun && <ReportCompare key={report.run_id} current={currentRun} runs={runs} report={report} />}
      {/* 「各次研究过程对比」:换一家 Agent 再研究同一只,并排看取数、引用、数字核对、缺口(不比结论和收益) */}
      {report && currentRun && <RunCompareCard key={`cmp-${report.run_id}`} current={currentRun} researchBusy={researchBusy || starting}
        onRunWith={(provider, endpoints) => runWith(currentRun, provider, endpoints)} />}

      {report && (
        <GlassCard className="mb-4">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <FileText className="h-4 w-4 text-primary" />
            <h3 className="font-semibold">{report.run_id} 的报告</h3>
            {/* 分享卡只给通过最终校验的报告(后端同一个判据,也会拒) */}
            {report.availability === "ready" && <button type="button" onClick={() => setSharing((v) => !v)} aria-expanded={sharing}
              className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-border/60 px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground">
              <Share2 className="h-3.5 w-3.5" /> {sharing ? "收起分享卡" : "分享卡"}
            </button>}
          </div>
          {sharing && report.availability === "ready" && <ShareCardPanel runId={report.run_id} />}
          <ResearchReport result={report} />
        </GlassCard>
      )}

      <GlassCard className="mb-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-semibold">研究归档</h3>
          <div className="flex flex-wrap items-baseline gap-2">
            {chartSymbol && runs.length > 0 && (
              <SegmentedToggle label="归档范围" value={archiveAll ? "all" : "mine"} onChange={(v) => setArchiveAll(v === "all")}
                options={[{ key: "mine", label: `只看这只（${mineRuns.length}）` }, { key: "all", label: `全部（${runs.length}）` }]} />
            )}
            {runs.length > 1 && <WeeklyCompareButton />}
          </div>
        </div>
        {runsErr ? (
          // 🔴 取不到 ≠ 没有:接口挂了要说是挂了,不能显示成"还没有研究运行"
          <p className="text-sm text-destructive">归档列表没取到：{runsErr}</p>
        ) : runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">还没有研究运行。选好股票点「开始」就能跑第一次。</p>
        ) : shownRuns.length === 0 ? (
          <p className="text-sm text-muted-foreground">这只还没跑过研究。<button onClick={() => setArchiveAll(true)} className="text-primary hover:underline">看全部归档</button></p>
        ) : (
          <div className="space-y-1.5">
            {shownRuns.map((r) => (
              <ResearchRunItem key={r.run_id} run={r} onOpen={(id) => void openReport(id)}
                onDelete={(id) => void removeRun(id)} deleting={deleting === r.run_id} />
            ))}
            {delErr && <p className="pt-1 text-xs text-destructive">删除失败：{delErr}</p>}
          </div>
        )}
      </GlassCard>
      </div>

      <Disclaimer />
    </div>
  );
}

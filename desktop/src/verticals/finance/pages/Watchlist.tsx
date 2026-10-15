import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Loader2, Plus, X, RefreshCw, Star } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { useAiPage } from "../../../core/ai/pageContext";
import { GlassCard } from "@/components/ui/GlassCard";
import { Disclaimer } from "@/components/ui/Disclaimer";
import { loadWatch, saveWatch, watchNameOf } from "@/lib/watchlist";
import { currencyOfSymbol, normalizeMarketSymbol } from "@/lib/marketSymbol";
import { backend } from "@/lib/backend";
import {
  BATCH_SEARCH_MAX, codeFallback, collectPicks, matchTag, offlineItems, parseBatch, planBatch, type BatchItem,
} from "@/lib/symbolSearch";
import { useLiveQuotes, isTradingHours } from "@/hooks/useLiveQuotes";
import { AbnormalCard } from "@/components/AbnormalCard";
import { KlineCard } from "@/components/KlineCard";
import { SortTh } from "@/components/ui/SortTh";
import { useTableSort } from "@/hooks/useTableSort";
import { currencyRank, sortRows, type SortDir, type SortValue } from "@/lib/tableSort";
import { abnormalContext, type AbnormalResult } from "@/lib/abnormal";
import { cn } from "@/lib/utils";

// A 股红涨绿跌（与整个看板一致）。
const color = (v: number | null | undefined) =>
  v == null ? "text-muted-foreground" : v > 0 ? "text-danger" : v < 0 ? "text-success" : "text-muted-foreground";
const pct = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v}%`);

const LIVE_KEY = "vr-watchlist-live";

type WatchQuote = ReturnType<typeof useLiveQuotes>["quotes"][string] | undefined;
/** 自选总览的列：现价不同币种分开排（100 港元和 100 元不比大小），其余直接比 */
const WATCH_COLS: { key: string; label: string; firstDir?: SortDir; right?: boolean; byCurrency?: boolean; value: (c: string, q: WatchQuote) => SortValue }[] = [
  { key: "name", label: "名称", firstDir: "asc", value: (c, q) => q?.name || watchNameOf(c) || c },
  { key: "code", label: "代码", firstDir: "asc", value: (c) => c },
  { key: "price", label: "现价", right: true, byCurrency: true, value: (_c, q) => q?.price },
  { key: "change_pct", label: "涨跌%", right: true, value: (_c, q) => q?.change_pct },
  { key: "pe", label: "PE(TTM)", right: true, value: (_c, q) => q?.pe_ttm },
  { key: "pb", label: "PB", right: true, value: (_c, q) => q?.pb },
  { key: "turnover", label: "换手%", right: true, value: (_c, q) => q?.turnover_pct },
];
const WATCH_KEYS = WATCH_COLS.map((c) => c.key);
const NUM = "whitespace-nowrap px-2 py-2.5 text-right font-mono tabular-nums";

// localStorage 在隐私模式 / 嵌入式浏览器里可能直接抛异常。读写都要兜底，
// 否则初始化时一抛整个自选股页就白屏（与 lib/watchlist.ts 的处理保持一致）。
const loadLive = (): boolean => {
  try {
    return localStorage.getItem(LIVE_KEY) === "on";
  } catch {
    return false;
  }
};
const saveLive = (on: boolean) => {
  try {
    localStorage.setItem(LIVE_KEY, on ? "on" : "off");
  } catch {
    /* 存储不可用：开关本次会话内仍生效，只是不被记住 */
  }
};

/** `tabs`：放在页头下面的「自选 / 持仓」切换（这一页挂在「自选与持仓」里）。 */
export function Watchlist({ tabs }: { tabs?: ReactNode } = {}) {
  const [codes, setCodes] = useState<string[]>(loadWatch);
  // 自选里哪些也在持仓里：只是个标记，取不到不影响自选本身
  const [held, setHeld] = useState<ReadonlySet<string> | null>(null);
  const [heldErr, setHeldErr] = useState(false);
  useEffect(() => {
    let off = false;
    backend.holdings("average").then(
      (v) => { if (!off) setHeld(new Set(v.holdings.filter((h) => h.shares > 0).map((h) => normalizeMarketSymbol(h.symbol) ?? h.symbol))); },
      () => { if (!off) setHeldErr(true); },
    );
    return () => { off = true; };
  }, []);
  const [input, setInput] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [pending, setPending] = useState<BatchItem[]>([]);
  const [busy, setBusy] = useState(0);   // 正在按名称搜的个数
  const ctl = useRef<AbortController | null>(null);
  // 搜索要等好几秒，回来时按「那一刻」的列表合并，不用发起时的旧列表
  const codesRef = useRef(codes);
  useEffect(() => () => ctl.current?.abort(), []);
  // 实时行情默认**关闭**——开着会持续请求，让用户自己决定要不要开。
  const [live, setLive] = useState(loadLive);
  const [abnormal, setAbnormal] = useState<AbnormalResult | null>(null);

  const { quotes, loading, updatedAt, polling, error, settled, refresh } = useLiveQuotes(codes, live);

  // 点表头排序（只改显示的先后，不动自选本身）
  const { sort, onSort } = useTableSort("vr-sort-watchlist", WATCH_KEYS);
  const shown = useMemo(() => {
    const col = (k: string) => WATCH_COLS.find((x) => x.key === k);
    return sortRows(codes, sort, (c, k) => col(k)?.value(c, quotes[c]),
      (c, k) => (col(k)?.byCurrency ? currencyRank(quotes[c]?.currency ?? currencyOfSymbol(c)) : null));
  }, [codes, sort, quotes]);
  // 点哪行，下面的 K 线就画哪只；没点过默认第一行。
  // 行情完整取过一次（成功失败都算）以后定住，免得按涨跌排着时每次刷新都换图；
  // 按行情排着时，行情回来之前先不画（不然先画原顺序第一只、行情一到又换一只）
  const [picked, setPicked] = useState<string | null>(null);
  const quotesSettled = settled !== null;
  const current = picked && codes.includes(picked) ? picked : quotesSettled || !sort ? shown[0] ?? null : null;
  useEffect(() => { if (quotesSettled && current && current !== picked) setPicked(current); }, [quotesSettled, current, picked]);

  const toggleLive = () => {
    setLive((on) => {
      const next = !on;
      saveLive(next);
      return next;
    });
  };

  const put = (next: string[]) => { codesRef.current = next; setCodes(next); };

  /** 并入自选并保存，返回实际新增几只。写失败就把列表退回台账里的真实状态，别让界面停在"看着加上了" */
  const commit = (incoming: string[]): number => {
    const cur = codesRef.current;
    const fresh = incoming.filter((c, i) => !cur.includes(c) && incoming.indexOf(c) === i);
    if (!fresh.length) return 0;
    const next = [...cur, ...fresh];
    put(next);
    void saveWatch(next).catch((e) => { put(loadWatch()); setHint(`没保存上：${e instanceof Error ? e.message : String(e)}`); });
    return fresh.length;
  };

  const add = async () => {
    if (busy) return;
    const plan = planBatch(input);
    setInput(plan.rest.join(" "));
    if (!plan.direct.length && !plan.search.length) {
      setNotes([]);
      setHint(plan.skipped.length ? `跳过 ${plan.skipped.length} 段太长的文字（不像代码也不像名称）` : null);
      return;
    }
    let items: BatchItem[] = [];
    let offline = "";
    if (plan.search.length) {
      const c = new AbortController();
      ctl.current = c;
      setBusy(plan.search.length);
      try {
        const r = parseBatch(await backend.runTool<unknown>("search", { queries: plan.search }, c.signal));
        if (c.signal.aborted) return;
        if (r.ok) items = r.items;
        else { offline = r.error; items = offlineItems(plan.search, "名称搜索不通"); }
      } catch (e) {
        if (c.signal.aborted) return;
        offline = `名称搜索暂时不通（${e instanceof Error ? e.message : String(e)}）`;
        items = offlineItems(plan.search, "名称搜索不通");
      } finally {
        if (!c.signal.aborted) setBusy(0);
      }
    }
    const { picks, notes: seen, pending: unsure } = collectPicks(plan.direct, items);
    const added = commit(picks);
    // 同一个词再粘一次：用新结果替换旧的待确认条目
    setPending((old) => [...unsure, ...old.filter((o) => !unsure.some((u) => u.query === o.query) && !items.some((i) => i.pick && i.query === o.query))]);
    setNotes(seen);
    const parts: string[] = [];
    if (added) parts.push(`已添加 ${added} 只`);
    if (picks.length > added) parts.push(`${picks.length - added} 只已在自选里`);
    if (unsure.length) parts.push(`${unsure.length} 个需要你点选（见下方）`);
    if (plan.rest.length) parts.push(`一次最多按名称查 ${BATCH_SEARCH_MAX} 个，剩下 ${plan.rest.length} 个留在输入框里，再点一次添加`);
    if (plan.skipped.length) parts.push(`跳过 ${plan.skipped.length} 段太长的文字`);
    if (offline) parts.push(`${offline}；能直接认出的代码已加上`);
    setHint(parts.length ? parts.join("；") : "没识别到新的标的");
  };

  const choose = (it: BatchItem, symbol: string) => {
    const added = commit([symbol]);
    setPending((old) => old.filter((o) => o.query !== it.query));
    setHint(added ? `已添加 ${symbol}` : `${symbol} 已在自选里`);
  };
  const dismiss = (query: string) => setPending((old) => old.filter((o) => o.query !== query));

  const remove = (c: string) => {
    const next = codesRef.current.filter((x) => x !== c);
    put(next);
    void saveWatch(next).catch(() => put(loadWatch()));
  };

  const aiContext = useMemo(
    () =>
      codes.length
        ? "我的自选股（本地）：\n" +
          codes
            .map((c) => {
              const q = quotes[c];
              return q
                ? `${q.name}(${c},${q.currency}) 现价${q.price ?? "未取到"} ${pct(q.change_pct)} PE(TTM)${q.pe_ttm ?? "—"} 换手${q.turnover_pct ?? "—"}%`
                : `${c}（行情未取到）`;
            })
            .join("\n") +
          (abnormalContext(abnormal) ? `\n\n${abnormalContext(abnormal)}` : "")
        : "还没有自选股。",
    [codes, quotes, abnormal],
  );

  useAiPage({
    key: "watchlist",
    title: "自选股",
    context: aiContext,
    suggestions: ["这几只里哪些估值偏高", "帮我按赛道分组看看", "各自最大的风险点是什么"],
  });

  return (
    <div>
      <PageHeader
        title="自选股"
        subtitle="批量添加、一屏总览你关注的标的。数据只存本地、不上传。"
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={toggleLive}
              title={live ? "关闭自动取数" : "开启自动取数（交易时段每次请求结束后等 3 秒；上游可能延迟）"}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs transition-colors",
                live
                  ? "border-primary/50 bg-primary/10 text-primary"
                  : "border-border/60 text-muted-foreground hover:text-foreground",
              )}
            >
              <span className="relative flex h-2 w-2">
                {polling && (
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/70" />
                )}
                <span
                  className={cn(
                    "relative inline-flex h-2 w-2 rounded-full",
                    live ? "bg-primary" : "bg-muted-foreground/40",
                  )}
                />
              </span>
              自动取数
            </button>
          </div>
        }
      />
      {tabs}

      <GlassCard className="mb-4">
        <label className="mb-1.5 block text-xs text-muted-foreground">
          批量添加 —— 代码、中文名、拼音首字母都行，支持 A 股、港股、美股（逗号 / 空格 / 换行分隔）
        </label>
        <div className="flex gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void add();
            }}
            rows={2}
            placeholder={"如：600519 茅台 AAPL 腾讯\n000858, gzmt, 09988.HK"}
            className="flex-1 resize-y rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
          <button
            onClick={() => void add()}
            disabled={busy > 0}
            className="inline-flex h-9 shrink-0 items-center gap-1.5 self-start rounded-lg bg-primary/15 px-4 text-sm font-medium text-primary shadow-glow hover:bg-primary/25 disabled:opacity-60"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} 添加
          </button>
        </div>
        {busy > 0 && <p className="mt-2 text-xs text-muted-foreground">正在按名称查 {busy} 个（东财限流，每个约 1 秒）…</p>}
        {!busy && hint && <p className="mt-2 text-xs text-muted-foreground/70">{hint}</p>}
        {!busy && notes.length > 0 && (
          <p className="mt-1 text-[11px] leading-5 text-muted-foreground/70">识别为：{notes.join(" · ")}</p>
        )}
        {pending.length > 0 && (
          <div className="mt-3 space-y-1.5">
            <p className="text-xs text-muted-foreground">下面这些没法唯一确定，点一下要加的那个：</p>
            {pending.map((it) => {
              const code = codeFallback(it);
              return (
                <div key={it.query} className="flex flex-wrap items-center gap-1.5 rounded-lg border border-border/50 bg-black/10 px-2.5 py-1.5 text-xs">
                  <span className="font-medium">{it.query}</span>
                  <span className="text-muted-foreground/70">{it.reason}</span>
                  {it.matches.map((m) => (
                    <button key={m.symbol} onClick={() => choose(it, m.symbol)}
                      className="rounded-md border border-border px-2 py-0.5 hover:border-primary/50 hover:text-primary">
                      {m.name} <span className="font-mono text-muted-foreground">{m.symbol}</span>{" "}
                      <span className="text-[10px] text-muted-foreground/70">{matchTag(m)}</span>
                    </button>
                  ))}
                  {code && (
                    <button onClick={() => choose(it, code)} title="搜索里没有这个代码，请自己核对"
                      className="rounded-md border border-dashed border-border px-2 py-0.5 hover:border-primary/50 hover:text-primary">
                      按{/^[A-Z]/.test(code) ? "美股" : ""}代码 <span className="font-mono">{code}</span> 加
                    </button>
                  )}
                  <button onClick={() => dismiss(it.query)} title="不加了" aria-label={`不加 ${it.query}`}
                    className="ml-auto text-muted-foreground/50 hover:text-foreground">
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </GlassCard>

      <GlassCard glow className="mb-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="flex items-center gap-1.5 font-semibold">
            <Star className="h-4 w-4 text-primary" /> 自选总览
            <span className="text-xs font-normal text-muted-foreground">（{codes.length}）</span>
          </h3>
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground/70">
            {error ? (
              <span className="text-warning">{error}</span>
            ) : (
              <>
                {/* 把「开着却没在刷」的原因说清楚，否则用户会以为坏了 */}
                {live && !polling && codes.length > 0 && (
                  <span>{isTradingHours(codes) ? "已暂停（页面未激活）" : "当前关注市场均为非交易时段"}</span>
                )}
                {polling && <span className="text-primary/80">自动取数 · 间隔 3 秒</span>}
                {updatedAt && (
                  <span className="font-mono" title="所显示价格中最早的源取数时刻；不是成交时间，上游可能延迟">
                    最早快照取数 {new Date(updatedAt).toLocaleString("zh-CN", { hour12: false })}
                  </span>
                )}
              </>
            )}
            <button
              onClick={refresh}
              disabled={loading}
              className="text-muted-foreground hover:text-primary"
              title="立即刷新"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            </button>
          </div>
        </div>
        {codes.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground/60">
            还没有自选股，用上面的框粘贴一串代码批量添加。
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {WATCH_COLS.map((x) => (
                    <SortTh key={x.key} label={x.label} k={x.key} firstDir={x.firstDir} right={x.right} sort={sort} onSort={onSort}
                      title={x.byCurrency ? `按${x.label}排序，不同币种分开排` : undefined} className="px-2 py-2 font-medium" />
                  ))}
                  <th className="px-2 py-2"><span className="sr-only">移除</span></th>
                </tr>
              </thead>
              <tbody>
                {shown.map((c) => {
                  const q = quotes[c];
                  const sel = c === current;
                  // 整行可点：点哪行，下面的 K 线换成哪只。名称是个真按钮，键盘和读屏都能用（读屏会念出「已按下」）
                  return (
                    <tr key={c} aria-current={sel ? "true" : undefined} title="点一下，在下面看这只的 K 线"
                      onClick={() => setPicked(c)}
                      className={cn("cursor-pointer border-b border-border/30 transition-colors hover:bg-muted/40",
                        sel && "bg-primary/10 shadow-[inset_2px_0_0_hsl(var(--primary))] hover:bg-primary/15")}>
                      <td className="px-2 py-2.5 font-medium">
                        <button type="button" aria-pressed={sel} onClick={(e) => { e.stopPropagation(); setPicked(c); }}
                          title={`在下面看 ${q?.name || c} 的 K 线`}
                          className={cn("rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-primary/60", sel && "text-primary")}>{q?.name || c}</button>
                        {held?.has(c) && <span className="ml-1.5 rounded bg-primary/15 px-1 py-0.5 text-[10px] font-normal text-primary">持有</span>}
                      </td>
                      <td className="whitespace-nowrap px-2 py-2.5 font-mono text-xs text-muted-foreground">
                        {c}
                        {q && <span className="ml-1.5 rounded border border-border px-1 py-0.5 text-[10px]">{q.currency}</span>}
                      </td>
                      <td className={cn(NUM, color(q?.change_pct))}>{q?.price ?? "—"}</td>
                      <td className={cn(NUM, color(q?.change_pct))}>{q ? pct(q.change_pct) : "—"}</td>
                      <td className={cn(NUM, "text-muted-foreground")}>{q?.pe_ttm ?? "—"}</td>
                      <td className={cn(NUM, "text-muted-foreground")}>{q?.pb ?? "—"}</td>
                      <td className={cn(NUM, "text-muted-foreground")}>{q?.turnover_pct ?? "—"}</td>
                      <td className="px-2 py-2.5 text-right">
                        <button
                          onClick={(e) => { e.stopPropagation(); remove(c); }}
                          className="text-muted-foreground/50 hover:text-destructive"
                          title="移除"
                          aria-label={`从自选移除 ${q?.name || c}`}
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {codes.length > 0 && <p className="mt-2 text-[11px] text-muted-foreground/70">点一行，在下面看它的 K 线；点表头排序（再点反过来，第三下恢复）。</p>}
        {heldErr && codes.length > 0 && <p className="mt-2 text-[11px] text-muted-foreground/70">没取到持仓，「持有」标记暂缺。</p>}
      </GlassCard>

      {current && (
        <KlineCard symbol={normalizeMarketSymbol(current) ?? current} title={quotes[current]?.name}
          action={<Link to={`/research?symbol=${encodeURIComponent(current)}`} title="在个股页看财报、事件回看和研究"
            className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-primary hover:underline">个股页<ArrowRight className="h-3.5 w-3.5" /></Link>} />
      )}

      <AbnormalCard codes={codes} onData={setAbnormal} />

      <Disclaimer />
    </div>
  );
}

import { useState } from "react";
import {
  AlertTriangle, ArrowLeftRight, ChevronDown, ChevronRight, Gauge, Layers3,
  ListTree, Lock, Network, Rows3,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Caliber } from "@/components/ui/Caliber";
import { pctColor, countColor, strengthColor } from "@/lib/colors";
import { finite, safeArray, safeRecord } from "@/lib/agent";
import type {
  BoardStat, ByBoard, EventLedger, FeedbackCell, FeedbackMatrix,
  DayChange, DayDiff, FeedbackDetail, LossEffect, SealQuality,
  StatItem, StatsContext, ThemeNode, ThemeStructure, ThemeTree,
} from "@/lib/agent";

function rate(v?: number | null): string {
  return v == null ? "—" : `${Math.round(v * 100)}%`;
}
function signed(v?: number | null): string {
  return v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}
function hhmm(t?: string | null): string {
  if (!t || t.length < 4) return "—";
  return `${t.slice(0, 2)}:${t.slice(2, 4)}`;
}

/** 统计读数按 kind 格式化。裸值直接印会让同一列里"家数=61"和"炸板率=0.23"并排，
 *  读者分不清哪个是百分比 —— 后端喂 AI 的文本一直是格式化过的，界面也该一致。
 *  kind/unit 每份复盘快照里都有，所以历史复盘也能一并显示对。 */
function statVal(it: { kind?: string; unit?: string }, v?: number | null): string {
  if (v == null) return "—";
  if (it.kind === "rate") return rate(v);
  if (it.kind === "pct") return signed(v);
  return `${v}${it.unit ?? ""}`;
}

/** 只数 → 占样本的比例。原来这张卡三个数只有第一个给了占比，另两个只给只数，
 *  同一行里一半带比例一半不带，读者会以为它们不是同一个分母。 */
function ratioOf(count?: number | null, sample?: number | null): number | null {
  const c = finite(count), n = finite(sample);
  return c == null || n == null || n <= 0 ? null : c / n;
}

export function Section({
  icon: Icon, title, hint, caliber, available, reason, children,
}: {
  icon: typeof Gauge; title: string; hint?: string; caliber?: string;
  available?: boolean; reason?: string; children: React.ReactNode;
}) {
  return (
    <div className="glass rounded-2xl p-5">
      <div className="mb-1 flex items-center gap-1.5">
        <Icon className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-bold">{title}</h3>
        {caliber && <Caliber text={caliber} />}
      </div>
      {hint && <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">{hint}</p>}
      {available === false
        ? <p className="text-[13px] text-muted-foreground/70">暂不可用：{reason || "未知"}</p>
        : children}
    </div>
  );
}

/** 昨日强势股反馈矩阵：按昨日板位分组，看今天各落到什么结果 */
export function Matrix({ fm }: { fm?: FeedbackMatrix }) {
  const matrix = safeRecord<FeedbackCell>(fm?.matrix);
  // 板位写具体高度（首板/2板/3板/…/9板），档位动态从 matrix 键生成：
  // 首板固定最前、「昨日炸板」固定最后，中间按数字升序
  const tierOrder = (t: string) =>
    t === "首板" ? 1 : t === "昨日炸板" ? Number.MAX_SAFE_INTEGER : (parseInt(t, 10) || Number.MAX_SAFE_INTEGER - 1);
  const tiers = Object.keys(matrix).sort((a, b) => tierOrder(a) - tierOrder(b));
  const cols = ["晋级涨停", "收红", "小跌", "跌超5%", "跌停"] as const;
  return (
    <Section
      icon={Rows3}
      title="昨日强势股反馈"
      hint="昨天涨停/炸板的票，今天分别落到哪一档。均值只说冷热，这张表告诉你亏钱集中在哪个板位。"
      available={fm?.available}
      reason={fm?.reason}
    >
      {tiers.length === 0 ? (
        <p className="text-[13px] text-muted-foreground/70">无可统计分档</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px] tabular-nums">
            <thead>
              <tr className="border-b border-border text-[11px] text-muted-foreground">
                <th className="py-1.5 text-left font-semibold">昨日身份</th>
                {cols.map((c) => <th key={c} className="px-1.5 py-1.5 text-right font-semibold">{c}</th>)}
                <th className="px-1.5 py-1.5 text-right font-semibold">晋级率</th>
              </tr>
            </thead>
            <tbody>
              {tiers.map((t) => {
                const c = matrix[t];
                return (
                  <tr key={t} className="border-b border-border/50 last:border-0">
                    <td className="py-1.5 font-semibold">{t}<span className="ml-1 text-muted-foreground">({c.合计})</span></td>
                    <td className={cn("px-1.5 py-1.5 text-right", countColor("up"))}>{c.晋级涨停}</td>
                    <td className="px-1.5 py-1.5 text-right">{c.收红}</td>
                    <td className="px-1.5 py-1.5 text-right text-muted-foreground">{c.小跌}</td>
                    <td className={cn("px-1.5 py-1.5 text-right", countColor("down"))}>{c["跌超5%"]}</td>
                    <td className={cn("px-1.5 py-1.5 text-right font-bold", countColor("down"))}>{c.跌停}</td>
                    <td className="px-1.5 py-1.5 text-right font-bold">{rate(c.晋级率)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {/* 明细：后端已产出每只票的结果，之前完全没展示（半截功能）。
          默认折叠 —— 复盘先看矩阵，要追到具体哪只票再展开。按今日结果从差到好排。*/}
      {safeArray<FeedbackDetail>(fm?.details).length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-[11px] text-muted-foreground hover:text-foreground">
            展开明细（{safeArray<FeedbackDetail>(fm?.details).length} 只，按今日表现从差到好）
          </summary>
          <div className="mt-2 max-h-64 overflow-y-auto">
            <table className="w-full text-[11px] tabular-nums">
              <tbody>
                {safeArray<FeedbackDetail>(fm?.details).map((d) => (
                  <tr key={d.code} className="border-b border-border/30 last:border-0">
                    <td className="py-1 pr-2 font-semibold">{d.name}</td>
                    <td className="pr-2 text-muted-foreground">{d.code}</td>
                    <td className="pr-2 text-muted-foreground">{d.prev_tier}</td>
                    <td className={cn("pr-2 font-bold",
                      pctColor(d.ret))}>
                      {d.ret > 0 ? "+" : ""}{d.ret}%
                    </td>
                    <td className="pr-2">{d.result}</td>
                    <td className="truncate text-muted-foreground">{d.sector}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </Section>
  );
}

/** 题材结构 + 涨停时间分布 */
export function Themes({ ts }: { ts?: ThemeStructure }) {
  const themes = safeArray<ThemeStructure["themes"] extends (infer T)[] | undefined ? T : never>(ts?.themes);
  const timeline = safeArray<{ slot: string; count: number }>(ts?.timeline);
  const maxSlot = Math.max(1, ...timeline.map((x) => x.count));
  return (
    <Section
      icon={Layers3}
      title="题材结构 · 发酵节奏"
      hint="⚠️ 这是「行业分类」视角（兜底）——真正的题材看上面的事件树。行业≠题材：同一行业可能装着完全不同的炒作原因。这里的价值主要在「涨停时间分布」。"
      available={ts?.available}
      reason={ts?.reason}
    >
      <div className="mb-3 space-y-1">
        {themes.map((t) => (
          <div key={t.sector} className="flex items-center gap-2 text-[12px] tabular-nums">
            <span className="w-20 shrink-0 truncate font-semibold" title={t.sector}>{t.sector}</span>
            <span className="flex gap-0.5">
              {Array.from({ length: Math.min(t.limit_up, 12) }).map((_, i) => (
                <span key={i} className="h-3 w-1.5 rounded-sm bg-primary/70" />
              ))}
            </span>
            <span className="text-muted-foreground">
              {t.limit_up}只 · 最高{t.highest}板
              {t.broken > 0 && <span className="text-danger"> · 炸{t.broken}</span>}
              {t.first_seal && ` · 首封 ${hhmm(t.first_seal)}`}
            </span>
            {/* 成分票：后端产出了 names（含板位/首封/炸板次数），之前没展示 */}
            {safeArray<{ code: string; name: string; boards: number }>(t.names).length > 0 && (
              <span className="truncate text-[10px] text-muted-foreground/60" title={
                safeArray<{ name: string; boards: number }>(t.names)
                  .map((x) => `${x.name}(${x.boards}板)`).join("、")
              }>
                {safeArray<{ name: string; boards: number }>(t.names).slice(0, 3)
                  .map((x) => `${x.name}${x.boards}板`).join(" ")}
              </span>
            )}
          </div>
        ))}
      </div>
      {timeline.length > 0 && (
        <div className="border-t border-border pt-2.5">
          <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">涨停时间分布（按首次封板）</div>
          <div className="flex items-end gap-1.5">
            {timeline.map((x) => (
              <div key={x.slot} className="flex-1 text-center">
                <div className="mx-auto w-full rounded-t bg-primary/60"
                  style={{ height: `${Math.max(3, (x.count / maxSlot) * 44)}px` }} />
                <div className="mt-1 text-[10px] tabular-nums text-foreground">{x.count}</div>
                <div className="text-[9px] leading-tight text-muted-foreground">{x.slot}</div>
              </div>
            ))}
          </div>
        </div>
      )}
      {ts?.concentration != null && (
        <p className="mt-2 text-[11px] text-muted-foreground">
          共 {ts.theme_count} 个方向，头部占全市场涨停 <b className="text-foreground">{rate(ts.concentration)}</b>
          {ts.concentration >= 0.3 ? "（一枝独秀）" : ts.concentration <= 0.15 ? "（高度分散）" : ""}
        </p>
      )}
    </Section>
  );
}

/** 历史统计位置 —— 每个读数在近 N 日里排第几 */
export function StatsView({ c }: { c?: StatsContext }) {
  const items = safeArray<StatItem>(c?.items).filter((it) => it.value !== null);
  return (
    <Section
      icon={Gauge}
      title="历史统计位置"
      hint={"「涨停 40 家」是多还是少？光看绝对值判断不了 —— 要看它在历史上排第几。"
            + "每行第二个百分数是**历史分位**（77% = 比 77% 的日子高），**不是胜率**；两头（≥85% 或 ≤15%）才额外标偏热/偏冷。"
            + "⚠️ 全部从已落盘的涨停池缓存算，用池子里的全部票；「赚钱效应中位数」因此可能与上面那张卡差个零头"
            + "（那张卡只计入实时行情取得到的票）——两个都对，分母不同。"}
      available={c?.available}
      reason={c?.reason}
    >
      <div className="space-y-1.5">
        {items.map((it) => (
          <div key={it.key} className="flex flex-wrap items-center gap-2 text-[12px] tabular-nums">
            <span className="w-24 shrink-0 text-muted-foreground">{it.label}</span>
            <span className="w-16 font-bold">{it.value_text}</span>
            {it.percentile != null ? (
              <>
                {/* 分位条：位置比数字直观 */}
                <span className="relative h-1.5 w-24 overflow-hidden rounded-full bg-muted">
                  <span className={cn("absolute inset-y-0 left-0 rounded-full",
                    it.extreme ? (it.extreme === "偏热" ? "bg-danger" : "bg-info") : "bg-primary/60")}
                    style={{ width: `${Math.max(2, it.percentile * 100)}%` }} />
                </span>
                <span className="w-10 text-muted-foreground">{Math.round(it.percentile * 100)}%</span>
                {it.extreme && (
                  <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-bold",
                    it.extreme === "偏热" ? "bg-danger/15 text-danger" : "bg-info/15 text-info")}>
                    {it.extreme}
                  </span>
                )}
                {it.hist_median != null && (
                  <span className="text-[10px] text-muted-foreground/60">历史中位 {statVal(it, it.hist_median)}</span>
                )}
              </>
            ) : (
              <span className="text-[11px] text-muted-foreground/60">
                样本不足 {c?.min_samples ?? 10} 天，暂不给分位
              </span>
            )}
          </div>
        ))}
      </div>
      <p className="mt-2 border-t border-border pt-2 text-[11px] text-muted-foreground">
        对比近 <b className="text-foreground">{c?.sample_days ?? "—"}</b> 个交易日
        {(c?.extreme_count ?? 0) > 0
          ? ` · ${c?.extreme_count} 项处在两头`
          : " · 各项都不极端（今天是平淡的一天，这本身也是信息）"}
        {(c?.sample_days ?? 0) < 30 && "　⚠️ 样本随每天复盘自动变长，现在的分位仅供粗判"}
      </p>
    </Section>
  );
}

/** 今日 vs 昨日：只列非平凡变化 */
export function DiffView({ d }: { d?: DayDiff }) {
  const changes = safeArray<DayChange>(d?.changes);
  return (
    <Section
      icon={ArrowLeftRight}
      title={`今日 vs ${d?.prev_date || "昨日"}`}
      hint="复盘最该先回答的是「今天和昨天有什么不同」。只列超过噪声阈值的变化 —— 涨停 40→43 不算变化。"
      available={d?.available}
      reason={d?.reason}
    >
      {changes.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          关键读数全部在噪声阈值内 —— 今天与昨天没有实质变化。
        </p>
      ) : (
        <div className="space-y-1.5">
          {changes.map((c) => (
            <div key={c.key} className="flex flex-wrap items-center gap-2 text-[12px] tabular-nums">
              <span className="w-24 shrink-0 text-muted-foreground">{c.label}</span>
              <span className="text-muted-foreground/70">{d?.prev_date || "昨日"} {c.prev_text}</span>
              <span className={cn("font-bold", c.hotter ? "text-danger" : "text-info")}>
                {c.delta > 0 ? "↑" : "↓"}
              </span>
              <span className="font-bold">{d?.date || "今日"} {c.cur_text}</span>
              <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-bold",
                c.hotter ? "bg-danger/15 text-danger" : "bg-info/15 text-info")}>
                {c.hotter ? "转热" : "转冷"}
              </span>
            </div>
          ))}
        </div>
      )}
      {(d?.unchanged_count ?? 0) > 0 && changes.length > 0 && (
        <p className="mt-2 text-[11px] text-muted-foreground/70">
          另有 {d?.unchanged_count} 项变化在阈值内，未列出
        </p>
      )}
    </Section>
  );
}

const STATE_TONE: Record<string, string> = {
  今日新出现: "bg-info/15 text-info",
  扩散中: "bg-success/15 text-success",
  延续: "bg-success/10 text-success",
  维持: "bg-muted text-muted-foreground",
  分歧加大: "bg-warning/15 text-warning",
  高标独活: "bg-warning/15 text-warning",
  接力断档: "bg-danger/15 text-danger",
  无昨日题材数据: "bg-muted text-muted-foreground/70",
};

/** 题材事件树 —— 按真实事件题材（问财涨停原因）而不是行业分类 */
export function ThemeTreeView({ t }: { t?: ThemeTree }) {
  const themes = safeArray<ThemeNode>(t?.themes);
  return (
    <Section
      icon={Network}
      title="题材事件树"
      hint={
        "按「真实事件题材」聚合（问财涨停原因），不是行业分类 —— 「兵装重组」「间接投资长鑫科技」"
        + "这类事件在行业分类里找不到。每行按复盘顺序摆：昨日反馈 → 今天几点首封 → 扩散几只 → 梯队多高 → 炸了几个。"
      }
      available={t?.available}
      reason={t?.reason}
    >
      {themes.length === 0 ? (
        <p className="text-[13px] text-muted-foreground/70">无可聚合题材</p>
      ) : (
        <>
          <div className="space-y-2">
            {themes.map((n) => (
              <div key={n.tag} className="border-b border-border/40 pb-2 last:border-0 last:pb-0">
                <div className="flex flex-wrap items-center gap-2 text-[13px]">
                  <span className="font-bold">{n.tag}</span>
                  <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-bold",
                    STATE_TONE[n.state] || "bg-muted text-muted-foreground")}>{n.state}</span>
                  <span className="tabular-nums text-muted-foreground">
                    涨停 <b className="text-foreground">{n.limit_up}</b>
                    （首板{n.first_boards}/连板{n.consec_boards}）· 最高 <b className="text-foreground">{n.highest}</b> 板
                  </span>
                  {n.broken > 0 && <span className="text-[11px] text-warning">炸{n.broken}</span>}
                  {n.limit_down > 0 && <span className={cn("text-[11px]", countColor("down"))}>跌停{n.limit_down}</span>}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11px] tabular-nums text-muted-foreground">
                  {n.first_seal && (
                    <span>
                      首封 {hhmm(n.first_seal)}
                      {n.last_seal && n.last_seal !== n.first_seal && ` → 末封 ${hhmm(n.last_seal)}`}
                    </span>
                  )}
                  {n.prev_members > 0 && (
                    <span>
                      昨日 {n.prev_members} 只 → 今仍涨停 <b className={
                        strengthColor((n.continuation_rate ?? 0) >= 0.5)
                      }>{n.prev_still_up}</b>
                      {n.continuation_rate != null && `（延续 ${rate(n.continuation_rate)}）`}
                    </span>
                  )}
                  {safeArray<{ name: string; boards: number; label?: string }>(n.members).length > 0 && (
                    <span className="truncate opacity-70">
                      {safeArray<{ name: string; boards: number; label?: string }>(n.members).slice(0, 4)
                        .map((m) => `${m.name}${m.label ?? `${m.boards}板`}`).join("、")}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
          <p className="mt-2 border-t border-border pt-2 text-[11px] text-muted-foreground">
            共 {t?.tag_count} 个事件题材 · 题材串覆盖 {t?.covered}/{t?.total_limit_up} 只涨停
            {t?.coverage_rate != null && `（${rate(t.coverage_rate)}）`}
            {t?.concentration != null && ` · 头部占全市场涨停 ${rate(t.concentration)}`}
          </p>
        </>
      )}
    </Section>
  );
}

/** 关键事件账本 */
const TAG_TONE: Record<string, string> = {
  最高标: "bg-primary/15 text-primary",
  反复开板: "bg-warning/15 text-warning",
  题材首封: "bg-info/15 text-info",
  炸板: "bg-danger/15 text-danger",
  // 跌停走「跌」色（绿）—— 全站红涨绿跌，见 lib/colors.ts。
  // 「炸板」上面那条保持 danger 不动：它是"由强转弱"的**事件警示**，不是涨跌方向。
  跌停: "bg-success/20 text-success",
};

export function Ledger({ el }: { el?: EventLedger }) {
  type Ev = NonNullable<EventLedger["events"]>[number];
  const all = safeArray<Ev>(el?.events);
  const LOSS = ["高位断板", "连板断板", "炸板", "高标跌停", "跌停"];
  const GAIN = ["最高标", "题材首封"];
  const loss = el?.loss_events ?? all.filter((e) => LOSS.includes(e.tag));
  const gain = el?.gain_events ?? all.filter((e) => GAIN.includes(e.tag));
  const split = el?.split_events ?? all.filter((e) => !LOSS.includes(e.tag) && !GAIN.includes(e.tag));

  const Row = ({ e }: { e: Ev }) => (
    <div className="flex items-center gap-2 border-b border-border/40 py-1 text-[12px] last:border-0">
      <span className={cn("shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold",
        TAG_TONE[e.tag] || "bg-muted text-muted-foreground")}>{e.tag}</span>
      <span className="font-semibold">{e.name}</span>
      <span className="shrink-0 text-[10px] text-muted-foreground">{e.code}</span>
      {e.boards > 0 && <span className="shrink-0 text-[11px] text-primary">{e.boards}板</span>}
      <span className={cn("shrink-0 tabular-nums", pctColor(e.ret))}>{signed(e.ret)}</span>
      <span className="truncate text-[11px] text-muted-foreground">{e.sector} · {e.note}</span>
    </div>
  );

  const Group = ({ title, note, list }: {
    title: string; note: string; list: Ev[];
  }) => (
    <div>
      <div className="mb-1 flex items-baseline gap-2">
        <span className="text-[12px] font-bold text-foreground">{title}</span>
        <span className="text-[11px] text-muted-foreground">{note}</span>
        <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">{list.length} 起</span>
      </div>
      <div className="max-h-56 space-y-1 overflow-y-auto">
        {list.length ? list.map((e, i) => <Row key={`${e.code}-${e.tag}-${i}`} e={e} />)
          : <p className="text-[12px] text-muted-foreground/70">无</p>}
      </div>
    </div>
  );

  return (
    <Section
      icon={ListTree}
      title="关键事件账本"
      hint="今天值得看的那十几只票自动挑出来。⚠️ 只陈述发生了什么，不排序打分、不给参与倾向。"
      caliber={"事件是按规则自动挑的：今日最高板、炸板≥2次又回封、各题材最早封板的那只、\n" +
        "昨日板位最高的断板股（最多 6 只）、跌得最狠的（最多 5 只）。\n" +
        "「今天钱亏在哪」记的是**打板资金吃面的地方**，不是这些票都收跌 ——\n" +
        "炸板的票当天照样可能收红（冲板又掉下来、收盘还涨 7%），埋的是打板那一下。\n" +
        "「N 起」= 这一堆里有 N 只票。\n" +
        "⚠️ **按方向分三堆，堆内不排序。**\n" +
        "⚠️ 断板按**昨日板位**排，不按炸板次数：一只普通首板炸十次，信息量远不如「昨日 5 板断了」。"}
      available={el?.available}
      reason={el?.reason}
    >
      {}
      <div className="space-y-3">
        <Group title="今天钱亏在哪" note="断板 · 炸板 · 跌停" list={loss} />
        {split.length > 0 && (
          <Group title="分歧最大的" note="反复开板后回封" list={split} />
        )}
        <Group title="今天钱赚在哪" note="最高标 · 题材首封" list={gain} />
      </div>
    </Section>
  );
}

/** 亏钱效应 —— 昨日涨停股今天有多疼。 */
export function LossEffectSection({ le }: { le?: LossEffect }) {
  return (
    <Section
      icon={AlertTriangle}
      title="亏钱效应 · 昨日涨停股今天吃了多少面"
      hint="⚠️ 这一栏只统计【昨日涨停的那批票】今天的表现，不是全市场。判退潮先看昨天的强势股今天有多疼——炸板率只说明板没封住，说明不了封不住之后亏多少。"
      caliber={"样本 = 昨日涨停池全部个股，收盘对收盘。\n" +
        "「跌超5%/7%」按今日收盘涨跌幅算，不看盘中最低；**恰好跌 5.00% / 7.00% 都算进去**。\n" +
        "跌停按每只票自己的制度判（主板 10%、创业科创 20%、ST 主板 5%、北交所 30%），不是统一 10%。\n" +
        "⚠️ 与第一屏「全市场跌超5%」不是同一个分母：这里的分母只有昨日涨停那批（见下方样本数），\n" +
        "那里的分母是全 A 五千多只。两个数不能直接比大小。"}
      available={le?.available}
      reason={le?.reason}
    >
      <div className="grid grid-cols-3 gap-3 text-center">
        <div>
          <div className={cn("text-xl font-extrabold tabular-nums", countColor("down"))}>{le?.deep_loss_5_count ?? "—"}</div>
          <div className="text-[11px] text-muted-foreground">昨日涨停股跌超5% ({rate(le?.deep_loss_5_rate)})</div>
        </div>
        <div>
          <div className={cn("text-xl font-extrabold tabular-nums", countColor("down"))}>{le?.deep_loss_7_count ?? "—"}</div>
          <div className="text-[11px] text-muted-foreground">
            昨日涨停股跌超7% ({rate(ratioOf(le?.deep_loss_7_count, le?.sample))})
          </div>
        </div>
        <div>
          <div className={cn("text-xl font-extrabold tabular-nums", countColor("down"))}>{le?.limit_down_count ?? "—"}</div>
          <div className="text-[11px] text-muted-foreground">
            昨日涨停股今日跌停 ({rate(ratioOf(le?.limit_down_count, le?.sample))})
          </div>
        </div>
      </div>
      <div className="mt-3 space-y-0.5 border-t border-border pt-2.5 text-[11px] text-muted-foreground">
        <div>
          样本 = 昨日涨停 <b className="text-foreground">{le?.sample ?? "—"}</b> 只 ·
          其中最差 <b className={countColor("down")}>{signed(le?.worst)}</b>
        </div>
        {le?.market_limit_down != null && (
          <div>作为对照，今日<b className="text-foreground">全市场</b>跌停 <b className={countColor("down")}>{le.market_limit_down}</b> 家</div>
        )}
        {le?.prev_broken_recovery && (
          <div>
            昨日炸板股今日均 <b className={pctColor(le.prev_broken_recovery.avg)}>{signed(le.prev_broken_recovery.avg)}</b>、
            翻红 <b className="text-foreground">{rate(le.prev_broken_recovery.positive_rate)}</b>
            <span className="ml-1 text-muted-foreground/70">← 试错代价高</span>
          </div>
        )}
      </div>
    </Section>
  );
}

export function SealQualitySection({ sq }: { sq?: SealQuality }) {
  const nb = sq?.never_broken, ro = sq?.reopened, tot = sq?.total;
  return (
    <Section
      icon={Lock}
      title="封板质量"
      hint="同样是涨停，开盘秒板不炸、和炸六次尾盘回封完全是两回事。"
      caliber={"分母 = 今日**最终涨停**的个股。\n" +
        "「没炸过」+「炸后回封」= 最终涨停家数（互补的两半）。\n" +
        "「开盘秒板」是**首封时间**维度（9:35 前首封），和上面两个重叠，所以单列在下面。\n" +
        "🔴 「炸板率」的分母**不是**这里的涨停家数，而是「最终涨停 + 炸板未回封」——\n" +
        "它和「涨停里曾经开过板的比例」是两个概念，后者衡量板的成色、前者才是复盘说的炸板率。"}
      available={sq?.available}
      reason={sq?.reason}
    >
      <div className="grid grid-cols-2 gap-3 text-center">
        <div>
          <div className={cn("text-xl font-extrabold tabular-nums", strengthColor(true))}>{nb ?? "—"}</div>
          <div className="text-[11px] text-muted-foreground">没炸过 ({rate(sq?.never_broken_rate)})</div>
        </div>
        <div>
          <div className="text-xl font-extrabold tabular-nums text-warning">{ro ?? "—"}</div>
          <div className="text-[11px] text-muted-foreground">炸后回封</div>
        </div>
      </div>
      {/* 显式写出这两个加起来就是全部 —— 省得用户自己去加、加错了怀疑数据 */}
      {nb != null && ro != null && tot != null && (
        <p className="mt-1.5 text-center text-[11px] text-muted-foreground/70">
          {nb} + {ro} = 今日涨停 <b className="text-foreground">{tot}</b> 家（这两半互补）
        </p>
      )}
      <div className="mt-3 border-t border-border pt-2.5 text-[11px] text-muted-foreground">
        其中<b className="text-foreground"> 开盘秒板 {sq?.opening_seconds ?? "—"} </b>家、
        <b className="text-foreground">14:30 后才封住 {sq?.late_seal ?? "—"}</b> 家
        <span className="ml-1 text-muted-foreground/60">（首封时间口径，与上面两半交叉）</span>
        <div className="mt-0.5">
          <b className="text-foreground">炸板率 {rate(sq?.broken_rate)}</b>
          （炸板未回封 {sq?.broken_pool ?? "—"} 家 ÷（炸板未回封 {sq?.broken_pool ?? "—"} + 涨停 {tot ?? "—"}）家）·
          平均炸板 <b className="text-foreground">{sq?.avg_broken_times ?? "—"}</b> 次
        </div>
      </div>
    </Section>
  );
}

export function BoardSystemSection({ bb }: { bb?: ByBoard }) {
  const boards = safeRecord<BoardStat>(bb?.boards);
  const keys = Object.keys(boards).filter(
    (k) => boards[k] && (boards[k].limit_up > 0 || (boards[k].prev_base ?? 0) > 0),
  );
  const extinct = keys.filter((k) => boards[k].limit_up === 0 && (boards[k].prev_base ?? 0) > 0);
  const [open, setOpen] = useState(extinct.length > 0);
  if (!keys.length) return null;

  return (
    <div className="glass rounded-2xl px-5 py-3">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 text-left">
        {open ? <ChevronDown className="h-4 w-4 text-primary" /> : <ChevronRight className="h-4 w-4 text-primary" />}
        <span className="text-sm font-bold">分涨跌幅制度</span>
        <span className="text-[11px] text-muted-foreground">
          今日有涨停的 {keys.length} 类 · 10cm / 20cm / 北交所 / ST 的晋级生态不同，混算对不上任何一种打法
        </span>
        {extinct.length > 0 && (
          <span className="ml-auto rounded bg-danger/15 px-1.5 py-0.5 text-[10px] font-bold text-danger">
            {extinct.join("、")} 今日零涨停
          </span>
        )}
      </button>
      {open && (
        <div className="mt-3 flex flex-wrap gap-3 border-t border-border pt-3">
          {keys.map((k) => {
            const g = boards[k];
            return (
              <div key={k} className="min-w-[128px] flex-1 rounded-lg border border-border px-3 py-2">
                <div className="text-[12px] font-bold">{k}</div>
                <div className="mt-0.5 text-[11px] tabular-nums text-muted-foreground">
                  涨停 <b className="text-foreground">{g.limit_up}</b> · 最高 <b className="text-foreground">{g.highest}</b> 板
                </div>
                <div className="text-[11px] tabular-nums text-muted-foreground">
                  晋级率 <b className="text-foreground">{rate(g.promotion_rate)}</b>
                  {(g.broken ?? 0) > 0 && <span className="text-warning"> · 炸{g.broken}</span>}
                  {(g.limit_down ?? 0) > 0 && <span className={countColor("down")}> · 跌停{g.limit_down}</span>}
                </div>
                {g.limit_up === 0 && (g.prev_base ?? 0) > 0 && (
                  <div className="mt-0.5 text-[10px] font-bold text-danger">
                    ⚠️ 今日零涨停（昨日 {g.prev_base} 家）
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

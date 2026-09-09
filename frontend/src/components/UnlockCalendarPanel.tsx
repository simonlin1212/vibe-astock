import { useEffect, useState } from "react";
import { api, type UnlockCalendar } from "@/lib/api";
import { GlassCard } from "@/components/ui/GlassCard";

export function UnlockCalendarPanel({ watchCodes }: { watchCodes: string[] }) {
  const [window, setWindow] = useState<"upcoming" | "recent">("upcoming");
  const [onlyWatch, setOnlyWatch] = useState(false);
  const [data, setData] = useState<UnlockCalendar | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    setData(null); setError("");
    api.unlockCalendar(window).then(value => { if (active) setData(value); })
      .catch(() => { if (active) setError("解禁日历取数失败，暂不能判断有无事件。"); });
    return () => { active = false; };
  }, [window, reload]);
  const events = data?.events.filter(row => !onlyWatch || watchCodes.includes(row.code)) ?? [];
  const number = (value: number | null) => value == null ? "—" : value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return <GlassCard className="mb-4">
    <div className="mb-3 flex flex-wrap items-center gap-3">
      <h3 className="text-sm font-semibold">限售解禁日历</h3>
      <select aria-label="解禁日期范围" value={window} onChange={event => setWindow(event.target.value as typeof window)} className="rounded border border-border bg-background px-2 py-1 text-sm">
        <option value="upcoming">未来十天（含今天）</option><option value="recent">最近十天（含今天）</option>
      </select>
      <label className="flex items-center gap-1 text-sm"><input type="checkbox" checked={onlyWatch} onChange={event => setOnlyWatch(event.target.checked)} />只看自选</label>
      {error && <button type="button" onClick={() => setReload(n => n + 1)} className="text-sm text-primary">重试</button>}
    </div>
    <p className="mb-3 text-xs text-muted-foreground">来源：东方财富公开解禁计划；解禁不等于实际减持，以公司公告为准。按日历日统计，自选仅在本浏览器匹配。</p>
    {error ? <p role="alert" className="text-sm text-warning">{error}</p> : !data ? <p role="status" className="text-sm text-muted-foreground">正在读取解禁日历…</p> : <>
      <p className="mb-2 text-xs text-muted-foreground">{data.start} 至 {data.end} · {events.length} 条{data.truncated ? "（仅覆盖前 500 条，无法据此排除其他事件）" : ""}</p>
      {events.length === 0 ? <p className="text-sm text-muted-foreground">{onlyWatch && watchCodes.length === 0 ? "尚未添加自选，可在上方添加。" : data.truncated ? "已返回的部分记录中未匹配事件。" : "数据源在该范围内未返回匹配的解禁记录。"}</p> :
        <div className="max-h-80 overflow-auto"><table className="w-full text-left text-xs">
          <caption className="sr-only">限售解禁计划；数量为万股，占比为占总股本百分比</caption>
          <thead><tr>{["日期", "标的", "类型", "本次解禁（万股）", "占总股本"].map(text => <th scope="col" key={text} className="whitespace-nowrap px-2 py-2">{text}</th>)}</tr></thead>
          <tbody>{events.map((row, i) => <tr key={`${row.code}-${row.date}-${i}`} className="border-t border-border/50">
            <td className="whitespace-nowrap px-2 py-2">{row.date}</td><td className="whitespace-nowrap px-2 py-2">{row.name} {row.code}{watchCodes.includes(row.code) && <span className="ml-1 text-primary">自选</span>}</td>
            <td className="px-2 py-2">{row.type}</td><td className="px-2 py-2">{number(row.shares)}</td><td className="px-2 py-2">{row.ratio == null ? "—" : `${number(row.ratio)}%`}</td>
          </tr>)}</tbody>
        </table></div>}
    </>}
  </GlassCard>;
}

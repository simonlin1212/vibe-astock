import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Bell, BellOff, CheckCheck, Clock, Inbox as InboxIcon, Play, Plus, Send, Trash2 } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { backend, ApiError } from "@/lib/backend";
import { NOTIFY_CHANGED, WEBHOOK_KINDS, formatInboxTime, jobStatusLabel, type NotifyJob, type NotifyOverview, type WebhookKind } from "@/lib/notify";
import { useAiPage } from "../../../core/ai/pageContext";
import { cn } from "@/lib/utils";

const INPUT = "rounded-lg border border-border bg-background/60 px-3 py-2 text-sm";
const BTN = "inline-flex items-center gap-1.5 rounded-lg border border-border/60 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50";
// 任务状态不用红 / 绿:A 股里红绿是涨跌,「已运行」显示成绿色、「失败」显示成红色容易看反
const TONE = { ok: "text-foreground/80", warn: "text-warning", bad: "text-warning font-medium", muted: "text-muted-foreground" } as const;

const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

function Switch({ on, label, disabled, onChange }: { on: boolean; label: string; disabled?: boolean; onChange: (v: boolean) => void }) {
  return <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled} onClick={() => onChange(!on)}
    className="inline-flex shrink-0 items-center disabled:cursor-not-allowed disabled:opacity-50">
    <span aria-hidden="true" className={`relative inline-flex h-5 w-9 items-center rounded-full border transition-colors ${on ? "border-primary bg-primary" : "border-muted-foreground/40 bg-muted"}`}>
      <span className={`h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform ${on ? "translate-x-[18px]" : "translate-x-[3px]"}`} />
    </span>
  </button>;
}

function Section({ title, icon, actions, children }: { title: string; icon: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return <GlassCard>
    <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
      <h2 className="flex items-center gap-2 text-sm font-semibold">{icon}{title}</h2>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
    {children}
  </GlassCard>;
}

function JobRow({ job, busy, onToggle, onTime, onRun }: { job: NotifyJob; busy: boolean; onToggle: (v: boolean) => void; onTime: (at: string) => void; onRun: () => void }) {
  const [at, setAt] = useState(job.at);
  useEffect(() => setAt(job.at), [job.at]);
  const st = jobStatusLabel(job);
  return <div className="rounded-xl border border-border/60 p-3">
    <div className="flex flex-wrap items-center gap-3">
      <Switch on={job.enabled} label={`${job.enabled ? "关闭" : "开启"}「${job.label}」`} onChange={onToggle} />
      <span className="text-sm font-medium">{job.label}</span>
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        每天
        <input type="time" value={at} aria-label={`「${job.label}」运行时刻`} onChange={(e) => setAt(e.target.value)}
          onBlur={() => { if (at && at !== job.at) onTime(at); }} className={cn(INPUT, "w-[8.5rem] py-1")} />
      </label>
      <span className={cn("text-xs", TONE[st.tone])}>{st.text}{job.last_run_at ? ` · ${formatInboxTime(job.last_run_at)}` : ""}</span>
      <button type="button" onClick={onRun} disabled={busy || job.running} className={cn(BTN, "ml-auto")}>
        <Play className="h-3.5 w-3.5" />{busy || job.running ? "运行中…" : "立即运行"}
      </button>
    </div>
    <p className="mt-2 text-xs leading-5 text-muted-foreground">{job.description}</p>
    {job.note && <p className="mt-1 text-xs leading-5 text-muted-foreground/80">上次:{job.note}</p>}
  </div>;
}

function AddWebhook({ onAdd }: { onAdd: (b: { kind: WebhookKind; url: string; secret: string; uid: string; label: string }) => Promise<boolean> }) {
  const [kind, setKind] = useState<WebhookKind>("feishu");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [uid, setUid] = useState("");
  const [label, setLabel] = useState("");
  const [saving, setSaving] = useState(false);
  const meta = WEBHOOK_KINDS.find((k) => k.kind === kind)!;
  return <form className="mt-3 space-y-2 rounded-xl border border-dashed border-border/70 p-3" onSubmit={async (e) => {
    e.preventDefault();
    setSaving(true);
    // 地址只在这次请求里带上;加成功就清空输入框,页面上不留原文
    if (await onAdd({ kind, url, secret, uid, label })) { setUrl(""); setSecret(""); setUid(""); setLabel(""); }
    setSaving(false);
  }}>
    <div className="flex flex-wrap gap-2">
      <select aria-label="机器人类型" value={kind} onChange={(e) => { setKind(e.target.value as WebhookKind); setSecret(""); setUid(""); }} className={cn(INPUT, "py-1.5")}>
        {WEBHOOK_KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
      </select>
      <input aria-label="备注名" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="备注名(可不填,如:投研群)" maxLength={40} className={cn(INPUT, "min-w-40 flex-1 py-1.5")} />
    </div>
    {kind === "wxpusher"
      ? <input aria-label="API 地址" type="url" autoComplete="off" spellCheck={false} value={url} onChange={(e) => setUrl(e.target.value)}
          placeholder={secret.trim().startsWith("SPT_") ? "https://wxpusher.zjiecode.com/api/send/message/simple-push" : "https://wxpusher.zjiecode.com/api/send/message"} className={cn(INPUT, "w-full py-1.5 font-mono")} />
      : <input aria-label="Webhook 地址" type="password" autoComplete="off" spellCheck={false} value={url} onChange={(e) => setUrl(e.target.value)}
          placeholder={`${meta.label}机器人的 Webhook 地址`} className={cn(INPUT, "w-full py-1.5 font-mono")} />}
    {meta.signLabel && <input aria-label={meta.signLabel} type="password" autoComplete="off" spellCheck={false} value={secret} onChange={(e) => setSecret(e.target.value)}
      placeholder={kind === "wxpusher" ? meta.signLabel : `${meta.signLabel}(可不填)`} className={cn(INPUT, "w-full py-1.5 font-mono")} />}
    {meta.uidLabel && <input aria-label={meta.uidLabel} autoComplete="off" spellCheck={false} value={uid} onChange={(e) => setUid(e.target.value)}
      placeholder={kind === "wxpusher" ? meta.uidLabel : `${meta.uidLabel}(必填)`} className={cn(INPUT, "w-full py-1.5 font-mono")} />}
    <p className="text-[11px] leading-5 text-muted-foreground">{meta.hint} 只用「自定义关键词」校验时,消息可能被平台拒收。</p>
    <button type="submit" disabled={saving || !url.trim()} className={BTN}><Plus className="h-3.5 w-3.5" />{saving ? "添加中…" : "添加"}</button>
  </form>;
}

export function Inbox() {
  const [data, setData] = useState<NotifyOverview | null>(null);
  // 读取失败和操作失败分开记:操作失败后会自动重读一次,重读成功不能把操作失败的原因冲掉(第四轮审计 C7)
  const [loadError, setLoadError] = useState("");
  const [actError, setActError] = useState("");
  const error = actError || loadError;
  const [busy, setBusy] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try { setData(await backend.notify()); setLoadError(""); } catch (e) { setLoadError(errText(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const act = async (key: string, action: string, body: Record<string, unknown>): Promise<boolean> => {
    setBusy(key);
    try {
      setData(await backend.notifyAction(action, body));
      setActError(""); setLoadError("");
      window.dispatchEvent(new Event(NOTIFY_CHANGED));
      return true;
    } catch (e) {
      setActError(errText(e));
      // 失败时也刷新一次,比如「立即运行」失败后任务状态已经更新(只更新数据,不清操作失败的原因)
      void load();
      return false;
    } finally { setBusy(null); }
  };

  const test = async (channel: string) => {
    setBusy(`test:${channel}`);
    setTestResult((r) => ({ ...r, [channel]: "发送中…" }));
    try {
      const r = await backend.notifyTest(channel);
      setTestResult((x) => ({ ...x, [channel]: r.ok ? "已发出,去看看有没有收到" : `没发出去:${r.error ?? "原因不明"}` }));
    } catch (e) { setTestResult((x) => ({ ...x, [channel]: `没发出去:${errText(e)}` })); } finally { setBusy(null); }
  };

  const inbox = data?.inbox;
  useAiPage({
    key: "inbox",
    title: "提醒",
    context: inbox?.items.length
      ? `提醒收件箱(最近 ${Math.min(inbox.items.length, 20)} 条):\n` + inbox.items.slice(0, 20).map((x) => `- ${x.title}\n  ${x.lines.join("\n  ")}`).join("\n")
      : "提醒收件箱:还没有消息。",
    suggestions: ["今天的盘前提醒和收盘摘要说了什么", "自选里谁的公告值得我回去看原文"],
  });

  const s = data?.settings;
  const sched = data?.scheduler;
  return <div>
    <PageHeader title="提醒"
      subtitle="交易日开盘前自动整理「盘前提醒」、收盘后整理「收盘摘要」和「自选变化」放进这里,也可以推到系统通知或飞书 / 企业微信 / 钉钉群。只列事实,不给买卖建议。"
      actions={inbox && inbox.total > 0 && <>
        <button type="button" onClick={() => void act("read", "inbox/read", { all: true })} disabled={!inbox.unread || busy !== null} className={BTN}><CheckCheck className="h-3.5 w-3.5" />全部标为已读</button>
        <button type="button" onClick={() => { if (confirm("清空收件箱?")) void act("clear", "inbox/clear", {}); }} disabled={busy !== null} className={cn(BTN, "hover:text-destructive")}><Trash2 className="h-3.5 w-3.5" />清空</button>
      </>} />

    <div role="note" className="mb-5 rounded-xl border border-warning/30 bg-warning/[0.06] px-4 py-3 text-xs leading-6 text-muted-foreground">
      <p><b className="text-foreground">只在本程序运行时生效:</b>电脑关机或程序没开时不会运行;当天再打开时,过了时刻还没跑的任务会补跑一次。</p>
      {sched?.gate_note && <p>{sched.gate_note}</p>}
      {sched && !sched.running && <p className="text-warning">后台的定时任务现在没有启动(可能设置了 VRA_SCHEDULER=off),下面的任务不会按点运行,只能手动运行。</p>}
      {sched?.gate?.status === "unavailable" && <p className="text-warning">今天({sched.gate.day})还没判断出该不该运行:{sched.gate.reason}</p>}
      <p>推送里的链接是 {sched?.ui_base ?? "本机地址"},只在运行本程序的电脑上能打开。</p>
    </div>

    {error && <p role="alert" className="mb-4 rounded-lg border border-destructive/30 bg-destructive/[0.06] px-3 py-2 text-xs text-destructive">{error}</p>}
    {!data && !error && <p role="status" className="text-sm text-muted-foreground">正在读取…</p>}

    {data && <div className="grid gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Section title={`收件箱${inbox!.unread ? ` · ${inbox!.unread} 条未读` : ""}`} icon={<InboxIcon className="h-4 w-4 text-primary" />}>
        {inbox!.items.length === 0
          ? <p className="py-8 text-center text-sm text-muted-foreground">还没有消息。到点运行后会出现在这里;也可以在右边点「立即运行」先看一眼。</p>
          : <ul className="space-y-2">
            {inbox!.items.map((x) => <li key={x.id} className={cn("rounded-xl border p-3", x.read ? "border-border/50" : "border-primary/40 bg-primary/[0.04]", x.kind === "notice" && "border-warning/40")}>
              <div className="flex flex-wrap items-center gap-2">
                {!x.read && <span aria-label="未读" className="h-2 w-2 rounded-full bg-primary" />}
                <span className="text-sm font-medium">{x.title}</span>
                <span className="font-mono text-[11px] text-muted-foreground/70">{formatInboxTime(x.at)}</span>
                <span className="ml-auto flex items-center gap-2">
                  {x.link && <Link to={x.link} onClick={() => { if (!x.read) void act(`read:${x.id}`, "inbox/read", { ids: [x.id] }); }} className="text-xs text-primary hover:underline">打开</Link>}
                  {!x.read && <button type="button" onClick={() => void act(`read:${x.id}`, "inbox/read", { ids: [x.id] })} className="text-xs text-muted-foreground hover:text-foreground">标为已读</button>}
                </span>
              </div>
              {x.lines.length > 0 && <ul className="mt-2 space-y-0.5 text-xs leading-5 text-muted-foreground">
                {x.lines.map((l, i) => <li key={i} className="tabular-nums">{l}</li>)}
              </ul>}
            </li>)}
          </ul>}
      </Section>

      <div className="space-y-5">
        <Section title="定时任务" icon={<Clock className="h-4 w-4 text-primary" />}>
          {data.jobs.length === 0 ? <p className="text-sm text-muted-foreground">没有可用的定时任务。</p>
            : <div className="space-y-3">{data.jobs.map((j) => <JobRow key={j.id} job={j} busy={busy === `run:${j.id}`}
              onToggle={(v) => void act(`job:${j.id}`, "settings", { jobs: { [j.id]: { enabled: v } } })}
              onTime={(at) => void act(`job:${j.id}`, "settings", { jobs: { [j.id]: { at } } })}
              onRun={() => void act(`run:${j.id}`, "jobs/run", { id: j.id })} />)}</div>}
        </Section>

        <Section title="推送渠道" icon={<Send className="h-4 w-4 text-primary" />}>
          <p className="mb-3 text-xs leading-5 text-muted-foreground">不开任何渠道也没关系,消息照样进收件箱。推送内容和收件箱里一样,外加一个本机链接。</p>
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-border/60 p-3">
              <Switch on={s!.system} label="系统通知" disabled={!s!.system_supported} onChange={(v) => void act("system", "settings", { system: v })} />
              <span className="text-sm">系统通知</span>
              <button type="button" onClick={() => void test("system")} disabled={!s!.system_supported || busy !== null} className={cn(BTN, "ml-auto")}><Send className="h-3.5 w-3.5" />发送测试</button>
              <p className="w-full text-[11px] leading-5 text-muted-foreground">
                {s!.system_supported ? "macOS 第一次使用时,如果没弹出来,到「系统设置 → 通知」里允许「脚本编辑器」。" : "系统通知目前只支持 macOS 和 Linux;Windows 请用群机器人推送。"}
                {testResult.system && <span className="ml-1 text-foreground">{testResult.system}</span>}
              </p>
            </div>
            {s!.webhooks.map((h) => <div key={h.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-border/60 p-3">
              <Switch on={h.enabled} label={`${h.enabled ? "停用" : "启用"}${h.label || h.kind_label}`} onChange={(v) => void act(`hook:${h.id}`, "webhooks/update", { id: h.id, enabled: v })} />
              <span className="text-sm">{h.label || h.kind_label}</span>
              <span className="text-[11px] text-muted-foreground">{h.kind_label} · <span className="font-mono">{h.url}</span>{h.uid ? ` · UID ${h.uid}` : ""}{h.signed ? " · 已签名" : ""}</span>
              <span className="ml-auto flex gap-2">
                <button type="button" onClick={() => void test(h.id)} disabled={busy !== null} className={BTN}><Send className="h-3.5 w-3.5" />发送测试</button>
                <button type="button" aria-label={`删除${h.label || h.kind_label}`} onClick={() => { if (confirm(`删除「${h.label || h.kind_label}」?`)) void act(`hook:${h.id}`, "webhooks/remove", { id: h.id }); }} disabled={busy !== null} className={cn(BTN, "hover:text-destructive")}><Trash2 className="h-3.5 w-3.5" /></button>
              </span>
              {testResult[h.id] && <p className="w-full text-[11px] text-foreground">{testResult[h.id]}</p>}
            </div>)}
          </div>
          <AddWebhook onAdd={(b) => act("hook:add", "webhooks/add", { kind: b.kind, url: b.url.trim(), ...(b.secret.trim() ? { secret: b.secret.trim() } : {}), ...(b.uid.trim() ? { uid: b.uid.trim() } : {}), ...(b.label.trim() ? { label: b.label.trim() } : {}) })} />
        </Section>

        <Section title="免打扰" icon={s!.quiet.enabled ? <BellOff className="h-4 w-4 text-primary" /> : <Bell className="h-4 w-4 text-primary" />}>
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <Switch on={s!.quiet.enabled} label="静默时段" onChange={(v) => void act("quiet", "settings", { quiet: { enabled: v } })} />
            <span className="text-sm text-foreground">静默时段</span>
            <input type="time" aria-label="静默开始" defaultValue={s!.quiet.from} key={`f${s!.quiet.from}`} onBlur={(e) => { if (e.target.value && e.target.value !== s!.quiet.from) void act("quiet", "settings", { quiet: { from: e.target.value } }); }} className={cn(INPUT, "w-[8.5rem] py-1")} />
            到
            <input type="time" aria-label="静默结束" defaultValue={s!.quiet.to} key={`t${s!.quiet.to}`} onBlur={(e) => { if (e.target.value && e.target.value !== s!.quiet.to) void act("quiet", "settings", { quiet: { to: e.target.value } }); }} className={cn(INPUT, "w-[8.5rem] py-1")} />
          </div>
          <p className="mt-2 text-[11px] leading-5 text-muted-foreground">静默时段里只进收件箱、不推送。同一条内容 24 小时内只推一次;同一个任务两次推送至少间隔
            <input type="number" min={0} max={1440} aria-label="冷却分钟" defaultValue={s!.cooldown_min} key={`c${s!.cooldown_min}`}
              onBlur={(e) => { const n = Number(e.target.value); if (Number.isInteger(n) && n >= 0 && n <= 1440 && n !== s!.cooldown_min) void act("cooldown", "settings", { cooldown_min: n }); }}
              className={cn(INPUT, "mx-1 w-16 px-2 py-0.5 text-xs")} />分钟。手动运行和发送测试不受这些限制。</p>
        </Section>
      </div>
    </div>}
  </div>;
}

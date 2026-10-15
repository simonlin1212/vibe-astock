/**
 * 定时提醒与收件箱的界面数据(后端见 orchestrator/src/notify.ts + finance/schedule.ts)。
 * 群机器人地址后端只回显打码后的样子;这里也不缓存用户填的原始地址。
 */
export interface NotifyJob {
  id: string;
  label: string;
  description: string;
  default_at: string;
  at: string;
  enabled: boolean;
  last_day: string | null;
  last_run_at: string | null;
  status: "ok" | "failed" | "blocked" | "skipped" | "running" | null;
  note: string;
  running: boolean;
}

export type WebhookKind = "feishu" | "wecom" | "dingtalk" | "wxpusher";

export interface NotifyWebhook { id: string; kind: WebhookKind; kind_label: string; label: string; enabled: boolean; url: string; signed: boolean; uid: string | null }

export interface InboxItem { id: string; at: string; job: string; job_label: string; title: string; lines: string[]; link?: string; read: boolean; kind: "message" | "notice" }

export interface NotifyOverview {
  scheduler: {
    running: boolean;
    registered: boolean;
    time_zone: string | null;
    gate_note: string;
    gate: { day: string; status: "open" | "closed" | "unavailable"; reason?: string; checked_at: string } | null;
    ui_base: string;
  };
  jobs: NotifyJob[];
  settings: {
    system: boolean;
    system_supported: boolean;
    quiet: { enabled: boolean; from: string; to: string };
    cooldown_min: number;
    webhooks: NotifyWebhook[];
  };
  inbox: { items: InboxItem[]; unread: number; total: number };
}

/** 收件箱有变化(标已读、清空、手动运行)时广播,侧栏的未读数跟着刷新 */
export const NOTIFY_CHANGED = "vr-notify-changed";

export function jobStatusLabel(job: Pick<NotifyJob, "status" | "running">): { text: string; tone: "ok" | "warn" | "bad" | "muted" } {
  if (job.running || job.status === "running") return { text: "运行中", tone: "muted" };
  switch (job.status) {
    case "ok": return { text: "已运行", tone: "ok" };
    case "failed": return { text: "失败", tone: "bad" };
    case "blocked": return { text: "等待中", tone: "warn" };
    case "skipped": return { text: "今天不运行", tone: "muted" };
    default: return { text: "还没运行过", tone: "muted" };
  }
}

export const WEBHOOK_KINDS: { kind: WebhookKind; label: string; signLabel: string | null; uidLabel?: string; hint: string }[] = [
  { kind: "feishu", label: "飞书", signLabel: "签名校验密钥",
    hint: "群设置 → 群机器人 → 添加机器人 → 自定义机器人,复制 Webhook 地址。建议在「安全设置」里开「签名校验」,把密钥一起填上。" },
  { kind: "wecom", label: "企业微信", signLabel: null,
    hint: "群聊右上角 → 消息推送(群机器人)→ 添加,复制 Webhook 地址。企业微信没有签名,地址本身就能发消息,别发给别人。" },
  { kind: "dingtalk", label: "钉钉", signLabel: "加签密钥(SEC 开头)",
    hint: "群设置 → 机器人 → 添加机器人 → 自定义,复制 Webhook 地址。建议安全设置选「加签」,把 SEC 开头的密钥一起填上。" },
  { kind: "wxpusher", label: "WxPusher", signLabel: "appToken 或 SPT 个人令牌", uidLabel: "UID(标准方式必填)",
    hint: "微信公众号推送,两种凭据二选一:① 最简 —— 微信关注「wxpusher」公众号,菜单「个人推送」页复制 SPT 令牌,地址填 https://wxpusher.zjiecode.com/api/send/message/simple-push;② 标准 —— 官网后台创建应用拿 appToken(AT_ 开头),地址填 https://wxpusher.zjiecode.com/api/send/message,并填上公众号「我的→我的UID」里的 UID。" },
];

/** 本地时间显示(收件箱时间是 UTC ISO) */
export function formatInboxTime(iso: string, now = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameDay = d.toDateString() === now.toDateString();
  return d.toLocaleString("zh-CN", sameDay ? { hour: "2-digit", minute: "2-digit" } : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

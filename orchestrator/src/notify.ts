/**
 * **定时任务与提醒**(通用机制):到点跑垂类声明的任务 → 结果进本机收件箱 → 按用户设置推送到系统通知或群机器人。
 *
 * Core 只管:什么时候跑(时区 + 时刻 + 当天补跑)、能不能跑(垂类给的「今天开不开门」判定)、
 * 跑出来的消息往哪送(收件箱 / 系统通知 / 飞书 · 企业微信 · 钉钉群机器人)、
 * 怎么不吵人(静默时段、同一条 24 小时内不重复推、同一任务冷却)。
 * **任务做什么、门怎么判**全在垂类(`registerSchedule`),Core 不认识它们。
 *
 * 🔴 只在本程序运行时生效:电脑关机、程序没开就不会跑;当天再打开时,过了时刻还没跑的任务补跑一次。
 *    这句话界面上要原样说给用户 —— 不能让人以为装了就会准点推。
 * 🔴 门判定**取不到**(网络断、上游挂)时**不跑**,并在收件箱说明一次;不当成「开门」去跑。
 *    宁可少推一天,也不在本该休息的日子推一份看着正常的东西。
 * 🔴 群机器人地址本身就是凭据(拿到地址就能往群里发消息):只存本机 `notify/settings.json`(仅当前用户可读),
 *    接口只回显打码后的地址;错误信息里一律去掉地址、key 和签名密钥。
 *
 * 数据落在 `<dataRoot>/notify/`:settings.json(设置,0600)/ state.json(各任务上次运行)/ inbox.json(收件箱,最多 300 条)。
 */
import { execFile } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { UI_PORT } from "./brand.ts";
import { NOFOLLOW_FLAG, restrictPrivateFileAsync, writeJson } from "./fsutil.ts";
import { ServiceError, type ServiceContext } from "./service.ts";

// ---------------------------------------------------------------- 契约

export interface NotifyMessage {
  title: string;
  lines: string[];
  /** 界面内路径(如 `/my-stocks?tab=watch`);推送时前面拼上本机地址 */
  link?: string;
  /** 同一条内容的稳定键:24 小时内同键只推一次(收件箱照写) */
  dedupeKey: string;
}

export interface JobContext {
  ctx: ServiceContext;
  /** 计划时区下的日期 YYYY-MM-DD */
  day: string;
  now: Date;
  signal: AbortSignal;
  /** 这个任务上次留下的状态(由任务自己定义形状,比如已经见过的条目);首次为 undefined */
  state: unknown;
  /** 手动「立即运行」 */
  manual: boolean;
  /** 这个任务设定的运行时刻 HH:MM(用户改过就是改后的) */
  at?: string;
}

export interface JobResult {
  messages: NotifyMessage[];
  /** 要留给下次的状态(不返回 = 保持原样) */
  state?: unknown;
  /** 给界面看的一句话(比如「自选为空,没有可看的」) */
  note?: string;
}

export interface ScheduledJobDef {
  id: string;
  label: string;
  description: string;
  /** 默认时刻 HH:MM(计划时区),用户可改 */
  at: string;
  defaultEnabled: boolean;
  run(job: JobContext): Promise<JobResult>;
}

export type GateResult =
  | { status: "open"; reason?: string }
  | { status: "closed"; reason: string }
  | { status: "unavailable"; reason: string };

export interface ScheduleDef {
  /** IANA 时区名,所有时刻按它算 */
  timeZone: string;
  /** 界面上解释「哪些天会跑」的一句话 */
  gateNote: string;
  /** 今天开不开门。必须自己处理超时 / 取消;拿不准就返回 unavailable,不要猜 */
  gate(g: { ctx: ServiceContext; day: string; now: Date; signal: AbortSignal }): Promise<GateResult>;
  jobs: readonly ScheduledJobDef[];
  /**
   * 每一拍(约每分钟)都调一次的钩子,给「一天在几个固定时点各做一次」的事用(比如盘中快照)。
   * 在调度锁里运行、有超时;这一拍要不要干活由它自己判断,不干活时别联网。
   * `gate()` 是同一份当日开门判断(按天缓存),要用时再调。返回的一句话和出错都只进本拍报告,不影响定时任务。
   */
  beat?(b: { ctx: ServiceContext; day: string; hm: string; now: Date; signal: AbortSignal; gate: () => Promise<GateResult> }): Promise<string | void>;
}

const JOB_ID = /^[a-z][a-z0-9_]{0,39}$/;
const HM = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

let registered: ScheduleDef | null = null;

/** 垂类入口注册一次;Core 不 import 垂类(与 deep_target_registry 同一个注册槽写法) */
export function registerSchedule(def: ScheduleDef): void {
  if (registered && registered !== def) throw new Error("定时任务已经注册");
  validateSchedule(def);
  registered = def;
}

export function currentSchedule(): ScheduleDef | null {
  return registered;
}

export function validateSchedule(def: ScheduleDef): void {
  try { new Intl.DateTimeFormat("en-US", { timeZone: def.timeZone }); } catch { throw new Error(`定时任务的时区不认识:${def.timeZone}`); }
  if (typeof def.gate !== "function") throw new Error("定时任务缺少 gate 判定");
  if (def.beat !== undefined && typeof def.beat !== "function") throw new Error("定时任务的每拍钩子不是函数");
  const seen = new Set<string>();
  for (const j of def.jobs) {
    if (!JOB_ID.test(j.id)) throw new Error(`定时任务 id 不合法:${j.id}`);
    if (seen.has(j.id)) throw new Error(`定时任务 id 重复:${j.id}`);
    seen.add(j.id);
    if (!HM.test(j.at)) throw new Error(`定时任务 ${j.id} 的时刻不是 HH:MM:${j.at}`);
    if (typeof j.run !== "function") throw new Error(`定时任务 ${j.id} 缺少 run`);
  }
}

// ---------------------------------------------------------------- 存储

export type WebhookKind = "feishu" | "wecom" | "dingtalk" | "wxpusher";

interface Webhook { id: string; kind: WebhookKind; url: string; secret?: string; label: string; enabled: boolean; uid?: string }

interface Settings {
  version: 1;
  system: boolean;
  quiet: { enabled: boolean; from: string; to: string };
  cooldown_min: number;
  webhooks: Webhook[];
  jobs: Record<string, { enabled?: boolean; at?: string }>;
}

interface JobState {
  last_day?: string;
  last_run_at?: string;
  status?: "ok" | "failed" | "blocked" | "skipped" | "running";
  note?: string;
  attempt?: { day: string; n: number; retry_after?: string };
  data?: unknown;
}

interface State {
  version: 1;
  gate?: { day: string; status: GateResult["status"]; reason?: string; checked_at: string };
  gate_notice_day?: string;
  jobs: Record<string, JobState>;
  /** dedupeKey → 最近一次成功推送时间 */
  sent: Record<string, string>;
  /** 任务 id → 最近一次成功推送时间(冷却用) */
  pushed: Record<string, string>;
}

export interface InboxItem { id: string; at: string; job: string; job_label: string; title: string; lines: string[]; link?: string; read: boolean; kind: "message" | "notice" }

const INBOX_MAX = 300;
const DEFAULT_SETTINGS: Settings = {
  version: 1, system: false, quiet: { enabled: false, from: "22:00", to: "08:00" }, cooldown_min: 30, webhooks: [], jobs: {},
};

/**
 * notify 目录下的文件路径。目录与文件**逐级拒绝符号链接**:不能借一条链接把设置 / 收件箱读写到数据目录以外
 * (Codex 审计 2026-10-09 A2)。
 */
function notifyPath(dataRoot: string, name: string): string {
  const base = fs.existsSync(dataRoot) ? fs.realpathSync(dataRoot) : path.resolve(dataRoot);
  const dir = path.join(base, "notify");
  for (const p of [dir, path.join(dir, name)]) {
    let st: fs.Stats | null = null;
    try { st = fs.lstatSync(p); } catch (e) { if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e; }
    if (st?.isSymbolicLink()) throw new ServiceError("notify_path_symlink", "通知目录里有符号链接，已拒绝读写（不能借链接读写数据目录以外的文件）");
  }
  return path.join(dir, name);
}
const fileOf = (dataRoot: string, name: "settings" | "state" | "inbox") => notifyPath(dataRoot, `${name}.json`);

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** 通知文件读坏了:报错,**不能**当成默认值 —— 之后任何一次保存都会用默认值把原文件覆盖掉(A4) */
const corrupt = (name: string): never => {
  throw new ServiceError("notify_file_corrupt", `通知的 ${name}.json 读不出来（内容损坏或不是普通文件），为免覆盖原文件已停止读写；请修复或删除数据目录里的 notify/${name}.json 后重试`);
};

/** 读 notify 下的 JSON。不存在 = null;存在却读不出 / 解析不了 → 报错(不打开符号链接) */
function readNotifyJson(dataRoot: string, name: "settings" | "state" | "inbox"): unknown {
  const file = fileOf(dataRoot, name);
  let fd: number;
  try { fd = fs.openSync(file, fs.constants.O_RDONLY | NOFOLLOW_FLAG); } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return null;
    return corrupt(name);
  }
  try {
    if (!fs.fstatSync(fd).isFile()) return corrupt(name);
    const parsed: unknown = JSON.parse(fs.readFileSync(fd, "utf8"));
    // 文件里写着 null 不等于「没有文件」:当成没有,下一次保存就用默认值把它覆盖了(审计第二轮 A4)
    return parsed === null ? corrupt(name) : parsed;
  } catch (e) {
    if (e instanceof ServiceError) throw e;
    return corrupt(name);
  } finally { fs.closeSync(fd); }
}

/** 读设置。没有文件 = 默认值;文件坏了**报错**(原文件里可能还有能救回来的机器人地址,不能让下一次保存覆盖掉) */
export function loadSettings(dataRoot: string): Settings {
  const raw = readNotifyJson(dataRoot, "settings");
  if (raw === null) return structuredClone(DEFAULT_SETTINGS);
  if (!isObj(raw)) return corrupt("settings");
  const quiet = isObj(raw.quiet) ? raw.quiet : {};
  // 🔴 机器人记录坏了(字段缺 / 类型错 / 地址不合规)也按文件损坏处理:以前悄悄滤掉,
  //    下一次随便改个开关就把这条连同它的地址从磁盘上删了(审计第二轮 A4)
  const hooks = raw.webhooks === undefined ? [] : Array.isArray(raw.webhooks) ? raw.webhooks : corrupt("settings");
  return {
    version: 1,
    system: raw.system === true,
    quiet: {
      enabled: quiet.enabled === true,
      from: typeof quiet.from === "string" && HM.test(quiet.from) ? quiet.from : DEFAULT_SETTINGS.quiet.from,
      to: typeof quiet.to === "string" && HM.test(quiet.to) ? quiet.to : DEFAULT_SETTINGS.quiet.to,
    },
    cooldown_min: Number.isInteger(raw.cooldown_min) && (raw.cooldown_min as number) >= 0 && (raw.cooldown_min as number) <= 1440 ? raw.cooldown_min as number : DEFAULT_SETTINGS.cooldown_min,
    webhooks: hooks.map((h): Webhook => {
      if (!isObj(h) || typeof h.id !== "string" || typeof h.url !== "string" || !isKind(h.kind)) return corrupt("settings");
      if (checkWebhookUrl(h.kind, h.url) === null) return corrupt("settings");
      const secret = typeof h.secret === "string" && h.secret ? h.secret : undefined;
      const uid = typeof h.uid === "string" && h.uid ? h.uid : undefined;
      // WxPusher 两种凭据二选一、地址必须配对(混着配发不出去,按损坏处理):
      //   SPT 个人令牌(SPT_ 开头)→ simple-push 地址,不用 uid(最简,推荐)
      //   标准方式 appToken → message 地址 + 必填 uid
      if (h.kind === "wxpusher") {
        if (!secret) return corrupt("settings");
        const spt = secret.startsWith("SPT_");
        if (spt !== h.url.endsWith("/message/simple-push")) return corrupt("settings");
        if (!spt && (!uid || !WXPUSHER_UID_RE.test(uid))) return corrupt("settings");
      } else if (uid !== undefined) {
        return corrupt("settings");
      }
      return { id: h.id, kind: h.kind, url: h.url, ...(secret ? { secret } : {}), ...(uid ? { uid } : {}),
        label: typeof h.label === "string" ? h.label.slice(0, 40) : "", enabled: h.enabled !== false };
    }),
    jobs: isObj(raw.jobs) ? Object.fromEntries(Object.entries(raw.jobs).flatMap(([k, v]) => (JOB_ID.test(k) && isObj(v)
      ? [[k, { ...(typeof v.enabled === "boolean" ? { enabled: v.enabled } : {}), ...(typeof v.at === "string" && HM.test(v.at) ? { at: v.at } : {}) }]]
      : []))) : {},
  };
}

/** 设置里有机器人地址 / 签名密钥:临时文件一创建就是仅当前用户可读,收紧后再换名,中间没有「谁都能读」的窗口 */
async function saveSettings(dataRoot: string, s: Settings): Promise<void> {
  const file = fileOf(dataRoot, "settings");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`;
  try {
    fs.writeFileSync(tmp, JSON.stringify(s, null, 2) + "\n", { flag: "wx", mode: 0o600 });
    await restrictPrivateFileAsync(tmp);
    fs.renameSync(tmp, file);
  } catch (e) {
    fs.rmSync(tmp, { force: true });
    throw e;
  }
}

/** 运行状态只是去重 / 冷却用的记账:坏了按空状态重来(最多重推一次),不挡住调度。符号链接照样拒绝 */
function loadState(dataRoot: string): State {
  let raw: unknown = null;
  try { raw = readNotifyJson(dataRoot, "state"); } catch (e) { if (!(e instanceof ServiceError && e.code === "notify_file_corrupt")) throw e; }
  const strMap = (v: unknown): Record<string, string> => (isObj(v) ? Object.fromEntries(Object.entries(v).filter(([, x]) => typeof x === "string")) as Record<string, string> : {});
  if (!isObj(raw)) return { version: 1, jobs: {}, sent: {}, pushed: {} };
  const gate = isObj(raw.gate) && typeof raw.gate.day === "string" && typeof raw.gate.checked_at === "string"
    && ["open", "closed", "unavailable"].includes(String(raw.gate.status)) ? raw.gate as State["gate"] : undefined;
  return {
    version: 1,
    ...(gate ? { gate } : {}),
    ...(typeof raw.gate_notice_day === "string" ? { gate_notice_day: raw.gate_notice_day } : {}),
    jobs: isObj(raw.jobs) ? Object.fromEntries(Object.entries(raw.jobs).filter(([, v]) => isObj(v))) as Record<string, JobState> : {},
    sent: strMap(raw.sent),
    pushed: strMap(raw.pushed),
  };
}

function loadInbox(dataRoot: string): InboxItem[] {
  const raw = readNotifyJson(dataRoot, "inbox");
  if (raw === null) return [];
  const items = isObj(raw) && Array.isArray(raw.items) ? raw.items : corrupt("inbox");
  // 某一条结构坏了(比如 lines 被写成字符串,正文其实还在)也按损坏报错,不悄悄滤掉 ——
  // 滤掉之后「全部标已读」一写回,这条就永久没了(审计第二轮 A4)
  if (!items.every((x) => isObj(x) && typeof x.id === "string" && typeof x.title === "string" && Array.isArray(x.lines))) return corrupt("inbox");
  return items as InboxItem[];
}

/** 同一进程内的读改写一律排队,避免收件箱「标已读」和任务写入互相覆盖 */
let chain: Promise<unknown> = Promise.resolve();
function serial<T>(fn: () => T | Promise<T>): Promise<T> {
  const p = chain.then(fn, fn);
  chain = p.catch(() => undefined);
  return p;
}

function mutateState(dataRoot: string, fn: (s: State) => void): Promise<State> {
  return serial(() => {
    const s = loadState(dataRoot);
    fn(s);
    writeJson(fileOf(dataRoot, "state"), s);
    return s;
  });
}

function addInbox(dataRoot: string, items: InboxItem[]): Promise<void> {
  if (!items.length) return Promise.resolve();
  return serial(() => {
    const all = [...items, ...loadInbox(dataRoot)].slice(0, INBOX_MAX);
    writeJson(fileOf(dataRoot, "inbox"), { version: 1, items: all });
  });
}

const newId = () => crypto.randomBytes(6).toString("hex");

// ---------------------------------------------------------------- 时钟

/** 计划时区下的日期与时刻(不信任本机时区设置) */
export function localClock(now: Date, timeZone: string): { day: string; hm: string } {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(now).map((p) => [p.type, p.value]));
  return { day: `${parts.year}-${parts.month}-${parts.day}`, hm: `${parts.hour}:${parts.minute}` };
}

/** 静默时段:from > to 表示跨午夜(22:00–08:00) */
export function inQuietHours(q: Settings["quiet"], hm: string): boolean {
  if (!q.enabled || q.from === q.to) return false;
  return q.from < q.to ? hm >= q.from && hm < q.to : hm >= q.from || hm < q.to;
}

// ---------------------------------------------------------------- 推送渠道

const WEBHOOK_RULES: Record<WebhookKind, { label: string; hosts: string[]; path: RegExp; query?: string }> = {
  feishu: { label: "飞书", hosts: ["open.feishu.cn", "open.larksuite.com"], path: /^\/open-apis\/bot\/v2\/hook\/[A-Za-z0-9-]{8,80}$/ },
  wecom: { label: "企业微信", hosts: ["qyapi.weixin.qq.com"], path: /^\/cgi-bin\/webhook\/send$/, query: "key" },
  dingtalk: { label: "钉钉", hosts: ["oapi.dingtalk.com"], path: /^\/robot\/send$/, query: "access_token" },
  // WxPusher(微信公众号推送):凭据是 body 里的 appToken(SPT 个人令牌走 simple-push),订阅者用 uid 指定 —— 地址本身不含凭据
  wxpusher: { label: "WxPusher", hosts: ["wxpusher.zjiecode.com"], path: /^\/api\/send\/message(?:\/simple-push)?$/ },
};

const isKind = (v: unknown): v is WebhookKind => v === "feishu" || v === "wecom" || v === "dingtalk" || v === "wxpusher";

/** WxPusher 订阅者 UID(官方 app 里「UID」一段字母数字) */
export const WXPUSHER_UID_RE = /^[A-Za-z0-9_-]{4,64}$/;

/**
 * 只接受三家官方机器人地址(https + 固定域名 + 固定路径)。
 * 🔴 这是**白名单**,不是「不是内网就行」:任意地址都能填的话,这个功能就成了让本机替人往任意网址发请求的口子。
 */
export function checkWebhookUrl(kind: WebhookKind, raw: string): string | null {
  let u: URL;
  try { u = new URL(raw.trim()); } catch { return null; }
  const rule = WEBHOOK_RULES[kind];
  if (u.protocol !== "https:" || u.username || u.password || u.port || !rule.hosts.includes(u.hostname) || !rule.path.test(u.pathname)) return null;
  if (rule.query && !/^[A-Za-z0-9_-]{8,128}$/.test(u.searchParams.get(rule.query) ?? "")) return null;
  if ((kind === "feishu" || kind === "wxpusher") && u.search) return null;
  return u.toString();
}

/** 打码:只露域名和末尾 4 位,够用户认出是哪一个 */
export function maskWebhookUrl(url: string): string {
  try {
    const u = new URL(url);
    const tail = (u.searchParams.get("key") ?? u.searchParams.get("access_token") ?? u.pathname.split("/").pop() ?? "").slice(-4);
    return `${u.hostname}/…${tail}`;
  } catch { return "…"; }
}

/** 错误文字里去掉地址、key、签名密钥 —— 平台回包、网络错误都可能把它们带出来 */
export function scrubSecrets(text: string, hook?: Pick<Webhook, "url" | "secret" | "uid">): string {
  let out = String(text);
  const secrets: string[] = [];
  if (hook) {
    secrets.push(hook.url);
    try {
      const u = new URL(hook.url);
      for (const v of u.searchParams.values()) secrets.push(v);
      secrets.push(u.pathname.split("/").pop() ?? "");
    } catch { /* 已校验过的地址不会走到这里 */ }
    if (hook.secret) secrets.push(hook.secret);
  }
  for (const s of secrets.filter((x) => x.length >= 6).sort((a, b) => b.length - a.length)) out = out.split(s).join("***");
  // WxPusher 的 UID 单独抹:它常是 4–6 位短串,进上面的长串过滤会被漏掉(不是机密,但也没必要带出去)
  if (hook && "uid" in hook && typeof hook.uid === "string" && hook.uid.length >= 4) out = out.split(hook.uid).join("***");
  return out.replace(/https?:\/\/\S+/gi, "<地址>").slice(0, 200);
}

/** 飞书:以 `timestamp\nsecret` 为 HMAC-SHA256 密钥、对空串签名,base64;timestamp 单位秒,放进请求体 */
export function feishuSign(secret: string, tsSec: number): string {
  return crypto.createHmac("sha256", `${tsSec}\n${secret}`).update("").digest("base64");
}

/** 钉钉:以 secret 为密钥、对 `timestamp\nsecret` 签名,base64 后再 URL 编码;timestamp 单位毫秒,拼在地址上 */
export function dingtalkSign(secret: string, tsMs: number): string {
  return encodeURIComponent(crypto.createHmac("sha256", secret).update(`${tsMs}\n${secret}`).digest("base64"));
}

/** 企业微信文本消息上限 2048 字节,三家取最小的那个;中文 3 字节一个,按字节截 */
const TEXT_MAX_BYTES = 2000;
function clipBytes(text: string, max = TEXT_MAX_BYTES): string {
  if (Buffer.byteLength(text, "utf8") <= max) return text;
  const tail = "\n…(完整内容见收件箱)";
  let out = "";
  for (const ch of text) {
    if (Buffer.byteLength(out + ch + tail, "utf8") > max) break;
    out += ch;
  }
  return out + tail;
}

/** WxPusher 文本 content 上限 500 字(按 Unicode 码点数,不按字节),summary 上限 100 —— 超了直接拒绝请求 */
const WXPUSHER_CONTENT_MAX = 500;
function clipChars(text: string, max: number): string {
  if ([...text].length <= max) return text;
  const chars = [...text];
  const tail = "\n…(完整内容见收件箱)";
  return chars.slice(0, max - [...tail].length).join("") + tail;
}

export function messageText(m: Pick<NotifyMessage, "title" | "lines" | "link">, base = uiBase()): string {
  const local = /^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$/i.test(base);
  const link = m.link ? `\n${local ? "在本机打开" : "打开"}:${base}${m.link}` : "";
  return clipBytes(`【${m.title}】\n${m.lines.join("\n")}${link}`);
}

/**
 * 推送里的链接:本机模式只在这台电脑上能打开;远程模式用对外地址(启动器经 VRA_PUSH_LINK_BASE 传给 API)。
 * 本机页面端口默认见 brand.ts;被占用时启动器换了端口,经 VRA_UI_PORT 告诉 API。
 */
export function uiBase(env: NodeJS.ProcessEnv = process.env): string {
  const port = /^\d{1,5}$/.test(env.VRA_UI_PORT ?? "") ? env.VRA_UI_PORT : String(UI_PORT);
  return (env.VRA_PUSH_LINK_BASE || env.VRA_PUBLIC_URL || `http://127.0.0.1:${port}`).replace(/\/+$/, "");
}

export interface Deps {
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  system?: (title: string, body: string) => Promise<void>;
  platform?: NodeJS.Platform;
}

class PushError extends Error {
  readonly retry: boolean;
  constructor(message: string, retry: boolean) { super(message); this.retry = retry; }
}

export function webhookRequest(hook: Webhook, text: string, now: Date): { url: string; body: Record<string, unknown> } {
  if (hook.kind === "feishu") {
    const ts = Math.floor(now.getTime() / 1000);
    return { url: hook.url, body: { ...(hook.secret ? { timestamp: String(ts), sign: feishuSign(hook.secret, ts) } : {}), msg_type: "text", content: { text } } };
  }
  if (hook.kind === "dingtalk") {
    const ts = now.getTime();
    return { url: hook.secret ? `${hook.url}&timestamp=${ts}&sign=${dingtalkSign(hook.secret, ts)}` : hook.url, body: { msgtype: "text", text: { content: text } } };
  }
  if (hook.kind === "wxpusher") {
    // WxPusher:凭据在 body,订阅者用 uids;summary 是通知栏预览,取首行去掉多余空白
    const content = clipChars(text, WXPUSHER_CONTENT_MAX);
    const summary = clipChars(content.split("\n").filter(Boolean)[0] ?? content, 100);
    // SPT 个人令牌走 simple-push,不用 uids;标准 appToken + uids
    if (hook.secret?.startsWith("SPT_")) {
      return { url: hook.url, body: { content, spt: hook.secret, summary, contentType: 1 } };
    }
    return { url: hook.url, body: { appToken: hook.secret ?? "", uids: hook.uid ? [hook.uid] : [], content, summary, contentType: 1 } };
  }
  return { url: hook.url, body: { msgtype: "text", text: { content: text } } };
}

async function postWebhook(hook: Webhook, text: string, deps: Deps): Promise<void> {
  const doFetch = deps.fetch ?? fetch;
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  let last: PushError | null = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt) await sleep(attempt === 1 ? 1000 : 3000);
    // 每次重试重新签名:签名带时间戳,平台只认一小时内的
    const { url, body } = webhookRequest(hook, text, new Date());
    try {
      const res = await doFetch(url, {
        method: "POST", headers: { "Content-Type": "application/json; charset=utf-8" }, body: JSON.stringify(body),
        redirect: "error", signal: AbortSignal.timeout(10_000),
      });
      const raw = await res.text();
      if (res.status === 429 || res.status >= 500) { last = new PushError(`平台暂时不可用(HTTP ${res.status})`, true); continue; }
      if (!res.ok) throw new PushError(`平台拒绝(HTTP ${res.status})`, false);
      let j: Record<string, unknown> = {};
      try { j = JSON.parse(raw) as Record<string, unknown>; } catch { throw new PushError("平台返回的不是 JSON", false); }
      // WxPusher 成功码是 1000(其余三家都是 0);错误文字里同样可能带凭据,走统一的 scrub
      const code = hook.kind === "wxpusher" ? j.code : hook.kind === "feishu" ? (j.code ?? j.StatusCode) : j.errcode;
      const okCode = hook.kind === "wxpusher" ? 1000 : 0;
      if (code === okCode) return;
      const msg = String(j.msg ?? j.errmsg ?? j.StatusMessage ?? "");
      throw new PushError(`平台返回错误 ${String(code)}${msg ? `:${msg}` : ""}`, false);
    } catch (e) {
      if (e instanceof PushError && !e.retry) throw new PushError(scrubSecrets(e.message, hook), false);
      last = new PushError(scrubSecrets(e instanceof Error ? e.message : String(e), hook), true);
    }
  }
  throw last ?? new PushError("发送失败", false);
}

export function systemSupported(platform: NodeJS.Platform = process.platform): boolean {
  return platform === "darwin" || platform === "linux";
}

/**
 * 系统通知。参数**按 argv 传**,不拼进脚本字符串 —— 消息里有引号、反斜杠也不会变成脚本。
 * ⚠️ Windows 暂不支持(没有不装第三方模块就稳定可用的写法),界面上会说明并建议用群机器人。
 */
function systemNotify(title: string, body: string, platform: NodeJS.Platform): Promise<void> {
  const run = (cmd: string, args: string[]) => new Promise<void>((resolve, reject) => {
    execFile(cmd, args, { timeout: 10_000, windowsHide: true }, (err) => (err ? reject(new PushError(`系统通知没发出去(${(err as NodeJS.ErrnoException).code ?? "失败"})`, false)) : resolve()));
  });
  if (platform === "darwin") {
    return run("osascript", ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run", title, body]);
  }
  if (platform === "linux") return run("notify-send", ["--", title, body]);
  return Promise.reject(new PushError("系统通知目前只支持 macOS 和 Linux;Windows 请用群机器人推送", false));
}

interface PushOutcome { channel: string; ok: boolean; error?: string }

async function pushAll(settings: Settings, m: NotifyMessage, deps: Deps): Promise<PushOutcome[]> {
  const out: PushOutcome[] = [];
  if (settings.system) {
    const body = clipBytes(m.lines.slice(0, 3).join(" · "), 240);
    try {
      await (deps.system ?? ((t, b) => systemNotify(t, b, deps.platform ?? process.platform)))(m.title, body);
      out.push({ channel: "系统通知", ok: true });
    } catch (e) { out.push({ channel: "系统通知", ok: false, error: e instanceof Error ? e.message : String(e) }); }
  }
  for (const h of settings.webhooks.filter((x) => x.enabled)) {
    const name = h.label || WEBHOOK_RULES[h.kind].label;
    try { await postWebhook(h, messageText(m), deps); out.push({ channel: name, ok: true }); } catch (e) {
      out.push({ channel: name, ok: false, error: scrubSecrets(e instanceof Error ? e.message : String(e), h) });
    }
  }
  return out;
}

// ---------------------------------------------------------------- 运行

const JOB_TIMEOUT_MS = 5 * 60_000;
const GATE_TIMEOUT_MS = 90_000;
const GATE_RETRY_MS = 15 * 60_000;
const BEAT_TIMEOUT_MS = 60_000;
const RETRY_AFTER_MS = 10 * 60_000;
const MAX_ATTEMPTS = 3;
const DEDUPE_MS = 24 * 3600_000;

const running = new Set<string>();
const ticking = new Set<string>();

function withTimeout<T>(ms: number, fn: (signal: AbortSignal) => Promise<T>, what: string): Promise<T> {
  const ac = new AbortController();
  let timer: NodeJS.Timeout | undefined;
  return Promise.race([
    fn(ac.signal),
    new Promise<never>((_, reject) => { timer = setTimeout(() => { ac.abort(); reject(new Error(`${what}超时(${Math.round(ms / 1000)} 秒)`)); }, ms); }),
  ]).finally(() => clearTimeout(timer));
}

function checkResult(r: unknown): JobResult {
  if (!isObj(r) || !Array.isArray(r.messages)) throw new Error("任务没有返回消息列表");
  for (const m of r.messages) {
    if (!isObj(m) || typeof m.title !== "string" || !m.title.trim() || !Array.isArray(m.lines) || m.lines.some((l) => typeof l !== "string")
      || typeof m.dedupeKey !== "string" || !m.dedupeKey || (m.link !== undefined && (typeof m.link !== "string" || !m.link.startsWith("/")))) {
      throw new Error("任务返回的消息格式不对");
    }
  }
  return r as unknown as JobResult;
}

const jobAt = (s: Settings, j: ScheduledJobDef) => s.jobs[j.id]?.at ?? j.at;
const jobEnabled = (s: Settings, j: ScheduledJobDef) => s.jobs[j.id]?.enabled ?? j.defaultEnabled;

/** 投递:收件箱必写;推送按静默 / 去重 / 冷却过滤(手动运行和测试不过滤,也不记推送时间 —— 否则当天定时那次会被去重 / 冷却吞掉) */
async function deliver(ctx: ServiceContext, job: ScheduledJobDef, messages: NotifyMessage[], now: Date, hm: string, manual: boolean, deps: Deps): Promise<string> {
  if (!messages.length) return "";
  await addInbox(ctx.dataRoot, messages.map((m) => ({
    id: newId(), at: now.toISOString(), job: job.id, job_label: job.label, title: m.title, lines: m.lines, ...(m.link ? { link: m.link } : {}), read: false, kind: "message" as const,
  })));
  const settings = loadSettings(ctx.dataRoot);
  const hasChannel = settings.system || settings.webhooks.some((h) => h.enabled);
  if (!hasChannel) return "已放进收件箱(没开推送)";
  if (!manual && inQuietHours(settings.quiet, hm)) return "已放进收件箱(静默时段不推送)";
  const state = loadState(ctx.dataRoot);
  const notes: string[] = [];
  const sentKeys: string[] = [];
  let pushedAny = false;
  for (const m of messages) {
    if (!manual) {
      const last = Date.parse(state.sent[m.dedupeKey] ?? "");
      if (Number.isFinite(last) && now.getTime() - last < DEDUPE_MS) { notes.push(`「${m.title}」24 小时内推过,这次不重复推`); continue; }
      const lastJob = Date.parse(state.pushed[job.id] ?? "");
      if (pushedAny === false && Number.isFinite(lastJob) && now.getTime() - lastJob < settings.cooldown_min * 60_000) {
        notes.push(`距上次推送不到 ${settings.cooldown_min} 分钟,这次只进收件箱`); break;
      }
    }
    const outs = await pushAll(settings, m, deps);
    const ok = outs.filter((o) => o.ok).map((o) => o.channel);
    const bad = outs.filter((o) => !o.ok).map((o) => `${o.channel}:${o.error}`);
    if (ok.length) { sentKeys.push(m.dedupeKey); pushedAny = true; }
    notes.push([ok.length ? `已推送到 ${ok.join("、")}` : "", bad.length ? `没推出去 —— ${bad.join(";")}` : ""].filter(Boolean).join(";"));
  }
  if (sentKeys.length && !manual) {
    await mutateState(ctx.dataRoot, (s) => {
      for (const k of sentKeys) s.sent[k] = now.toISOString();
      s.pushed[job.id] = now.toISOString();
      for (const [k, t] of Object.entries(s.sent)) if (now.getTime() - Date.parse(t) > 2 * DEDUPE_MS) delete s.sent[k];
    });
  }
  return [...new Set(notes.filter(Boolean))].join(";");
}

async function runJob(ctx: ServiceContext, def: ScheduleDef, job: ScheduledJobDef, now: Date, manual: boolean, deps: Deps): Promise<JobState> {
  const key = `${ctx.dataRoot}\0${job.id}`;
  if (running.has(key)) throw new ServiceError("notify_busy", `「${job.label}」正在运行,等它跑完再试`);
  running.add(key);
  const { day, hm } = localClock(now, def.timeZone);
  try {
    const prev = loadState(ctx.dataRoot).jobs[job.id] ?? {};
    await mutateState(ctx.dataRoot, (s) => { s.jobs[job.id] = { ...prev, status: "running", note: "正在运行" }; });
    try {
      const r = checkResult(await withTimeout(JOB_TIMEOUT_MS, (signal) => job.run({ ctx, day, now, signal, state: prev.data, manual, at: jobAt(loadSettings(ctx.dataRoot), job) }), `「${job.label}」`));
      const pushNote = await deliver(ctx, job, r.messages, now, hm, manual, deps);
      const note = [r.note ?? (r.messages.length ? `生成 ${r.messages.length} 条` : "没有要提醒的"), pushNote].filter(Boolean).join(";");
      const next: JobState = {
        ...(manual ? { ...(prev.last_day ? { last_day: prev.last_day } : {}) } : { last_day: day }),
        last_run_at: now.toISOString(), status: "ok", note: `${manual ? "手动运行:" : ""}${note}`,
        // 手动运行只是看一眼:不改任务记下的进度(看过的公告、提醒过的判据),否则当天定时那次就没东西可推了
        ...(r.state !== undefined && !manual ? { data: r.state } : prev.data !== undefined ? { data: prev.data } : {}),
      };
      await mutateState(ctx.dataRoot, (s) => { s.jobs[job.id] = next; });
      return next;
    } catch (e) {
      const reason = e instanceof Error ? e.message : String(e);
      const n = !manual && prev.attempt?.day === day ? prev.attempt.n + 1 : 1;
      const giveUp = manual || n >= MAX_ATTEMPTS;
      const next: JobState = {
        ...prev, last_run_at: now.toISOString(), status: "failed",
        note: manual ? `手动运行失败:${reason}` : giveUp ? `今天试了 ${n} 次都失败:${reason}` : `第 ${n} 次失败,10 分钟后重试:${reason}`,
        ...(manual ? {} : giveUp ? { last_day: day, attempt: { day, n } } : { attempt: { day, n, retry_after: new Date(now.getTime() + RETRY_AFTER_MS).toISOString() } }),
      };
      await mutateState(ctx.dataRoot, (s) => { s.jobs[job.id] = next; });
      if (!manual && giveUp) {
        await addInbox(ctx.dataRoot, [{ id: newId(), at: now.toISOString(), job: job.id, job_label: job.label, title: `「${job.label}」今天没跑成`, lines: [reason], read: false, kind: "notice" }]);
      }
      if (manual) throw new ServiceError("job_failed", next.note!);
      return next;
    }
  } finally {
    running.delete(key);
  }
}

/**
 * 同一数据目录、同一天的门判断同时只发一个。每拍钩子 60 秒超时时,它要的门判断可能还在路上(门判断自己 90 秒才超时);
 * 任务那边或下一拍再要就接着等这一个:不并发再查一遍,也不会让晚到的「没判断出来」盖掉刚判好的结果。
 */
const gateInflight = new Map<string, Promise<GateResult>>();

function gateFor(ctx: ServiceContext, def: ScheduleDef, day: string, now: Date): Promise<GateResult> {
  const cached = loadState(ctx.dataRoot).gate;
  if (cached && cached.day === day && (cached.status !== "unavailable" || now.getTime() - Date.parse(cached.checked_at) < GATE_RETRY_MS)) {
    return Promise.resolve({ status: cached.status, reason: cached.reason ?? "" } as GateResult);
  }
  const key = `${ctx.dataRoot}\n${day}`;
  const pending = gateInflight.get(key);
  if (pending) return pending;
  const p = (async () => {
    let g: GateResult;
    try {
      g = await withTimeout(GATE_TIMEOUT_MS, (signal) => def.gate({ ctx, day, now, signal }), "判断今天是否运行");
      if (!isObj(g) || !["open", "closed", "unavailable"].includes(String(g.status))) g = { status: "unavailable", reason: "判断结果格式不对" };
    } catch (e) {
      g = { status: "unavailable", reason: e instanceof Error ? e.message : String(e) };
    }
    await mutateState(ctx.dataRoot, (s) => { s.gate = { day, status: g.status, ...(g.reason ? { reason: g.reason } : {}), checked_at: now.toISOString() }; });
    return g;
  })().finally(() => gateInflight.delete(key));
  gateInflight.set(key, p);
  return p;
}

/**
 * 跨进程只让一个调度在跑(同一数据目录开了两个后台时,不推两遍)。
 * 每跑一个任务前 `touch` 一下:一拍里排了几个慢任务时,锁文件不会因为太久没动被另一个进程当成死锁清掉。
 */
function tryLock(dataRoot: string, now: Date): { unlock: () => void; touch: () => void } | null {
  const file = notifyPath(dataRoot, "tick.lock");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const token = `${process.pid}:${newId()}`;
  for (let i = 0; i < 2; i++) {
    try {
      fs.writeFileSync(file, token, { flag: "wx", mode: 0o600 });
      const mine = () => { try { return fs.readFileSync(file, "utf8") === token; } catch { return false; } };
      return {
        unlock: () => { if (mine()) fs.rmSync(file, { force: true }); },
        touch: () => { if (mine()) { try { const t = new Date(); fs.utimesSync(file, t, t); } catch { /* 已被清理 */ } } },
      };
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
      // 上次进程异常退出留下的锁:超过一个任务超时 + 门判定超时的时长还在,就当它死了
      const age = now.getTime() - fs.statSync(file).mtimeMs;
      if (age < JOB_TIMEOUT_MS * 2 + GATE_TIMEOUT_MS) return null;
      fs.rmSync(file, { force: true });
    }
  }
  return null;
}

export interface TickReport { day: string; hm: string; due: string[]; gate?: GateResult; ran: { id: string; status: string; note?: string }[]; skipped?: string; beat?: string }

/**
 * 调度一拍:把到点且今天还没跑的任务跑掉。可测 —— 传入 now 和 def 即可,不碰真时钟;
 * 要模拟队列执行中时间往前走,传 clock(每个任务开始前读一次)。
 */
export async function tick(ctx: ServiceContext, opts: { now?: Date; clock?: () => Date; def?: ScheduleDef | null; deps?: Deps } = {}): Promise<TickReport> {
  const def = opts.def === undefined ? currentSchedule() : opts.def;
  const clock = opts.clock ?? (() => opts.now ?? new Date());
  const now = opts.now ?? clock();
  const { day, hm } = localClock(now, def?.timeZone ?? "UTC");
  const report: TickReport = { day, hm, due: [], ran: [] };
  if (!def) return { ...report, skipped: "没有注册定时任务" };
  if (ticking.has(ctx.dataRoot)) return { ...report, skipped: "上一拍还没结束" };
  ticking.add(ctx.dataRoot);
  let lock: ReturnType<typeof tryLock> = null;
  try {
    lock = tryLock(ctx.dataRoot, now);
    if (!lock) return { ...report, skipped: "另一个后台进程正在调度" };
    if (def.beat) {
      try {
        const note = await withTimeout(BEAT_TIMEOUT_MS, (signal) => def.beat!({ ctx, day, hm, now, signal, gate: () => gateFor(ctx, def, day, now) }), "每拍钩子");
        if (note) report.beat = note;
      } catch (e) {
        report.beat = `出错:${e instanceof Error ? e.message : String(e)}`;
      }
      lock.touch();
    }
    const settings = loadSettings(ctx.dataRoot);
    const state = loadState(ctx.dataRoot);
    const due = def.jobs.filter((j) => {
      const st = state.jobs[j.id];
      if (!jobEnabled(settings, j) || jobAt(settings, j) > hm || st?.last_day === day) return false;
      const retry = st?.attempt?.day === day ? Date.parse(st.attempt.retry_after ?? "") : Number.NaN;
      return !(Number.isFinite(retry) && retry > now.getTime());
    });
    report.due = due.map((j) => j.id);
    if (!due.length) return report;
    const gate = await gateFor(ctx, def, day, now);
    report.gate = gate;
    if (gate.status === "unavailable") {
      const fresh = loadState(ctx.dataRoot);
      await mutateState(ctx.dataRoot, (s) => {
        for (const j of due) s.jobs[j.id] = { ...(s.jobs[j.id] ?? {}), status: "blocked", note: `今天还没运行:${gate.reason}(15 分钟后再试)` };
        s.gate_notice_day = day;
      });
      if (fresh.gate_notice_day !== day) {
        await addInbox(ctx.dataRoot, [{ id: newId(), at: now.toISOString(), job: "", job_label: "定时任务", title: "今天的定时任务暂时没运行",
          lines: [gate.reason, "判断不了今天该不该运行时不运行,15 分钟后会再试一次;一直取不到就整天不推。"], read: false, kind: "notice" }]);
      }
      for (const j of due) report.ran.push({ id: j.id, status: "blocked", note: gate.reason });
      return report;
    }
    if (gate.status === "closed") {
      await mutateState(ctx.dataRoot, (s) => {
        for (const j of due) s.jobs[j.id] = { ...(s.jobs[j.id] ?? {}), last_day: day, status: "skipped", note: `今天不运行:${gate.reason}` };
      });
      for (const j of due) report.ran.push({ id: j.id, status: "skipped", note: gate.reason });
      return report;
    }
    for (const [i, j] of due.entries()) {
      lock.touch();
      // 前面的任务可能跑了几分钟:每个任务按它开始的时刻算(测试传了 now 就用 now)。
      // 🔴 开市判定和到点条件都是按开拍那一刻算的:队列跑过了午夜(或时钟跳到别的日期)就停,
      //    剩下的留给下一拍按新的一天重新判断 —— 否则周五的「开市」会带到周六,周六被记成跑过(2026-10-11 审计)
      const at = clock();
      const c = localClock(at, def.timeZone);
      if (c.day !== day) {
        for (const rest of due.slice(i)) report.ran.push({ id: rest.id, status: "deferred", note: `已经到了 ${c.day},留给下一拍按新的一天重新判断` });
        break;
      }
      if (jobAt(settings, j) > c.hm) { report.ran.push({ id: j.id, status: "deferred", note: "时钟往回调了,还没到点" }); continue; }
      const st = await runJob(ctx, def, j, at, false, opts.deps ?? {}).catch((e): JobState => ({ status: "failed", note: e instanceof Error ? e.message : String(e) }));
      report.ran.push({ id: j.id, status: st.status ?? "ok", ...(st.note ? { note: st.note } : {}) });
    }
    return report;
  } finally {
    lock?.unlock();
    ticking.delete(ctx.dataRoot);
  }
}

let schedulerTimer: NodeJS.Timeout | null = null;

/** 后台每分钟一拍。只在 API 入口(main)启动;`VRA_SCHEDULER=off` 关闭 */
export function startScheduler(ctx: ServiceContext, opts: { intervalMs?: number; log?: (line: string) => void } = {}): () => void {
  if (schedulerTimer) return () => undefined;
  const log = opts.log ?? ((line: string) => console.error(line));
  const beat = () => {
    tick(ctx).then((r) => {
      if (r.ran.length) log(`[notify] ${r.day} ${r.hm} ${r.ran.map((x) => `${x.id}=${x.status}`).join(" ")}`);
      if (r.beat) log(`[notify] ${r.day} ${r.hm} ${scrubSecrets(r.beat)}`);
    }).catch((e) => log(`[notify] 调度出错:${scrubSecrets(e instanceof Error ? e.message : String(e))}`));
  };
  const first = setTimeout(beat, 5_000);
  schedulerTimer = setInterval(beat, opts.intervalMs ?? 60_000);
  first.unref();
  schedulerTimer.unref();
  return () => { clearTimeout(first); if (schedulerTimer) clearInterval(schedulerTimer); schedulerTimer = null; };
}

// ---------------------------------------------------------------- 接口

export function notifyOverview(ctx: ServiceContext, opts: { def?: ScheduleDef | null; platform?: NodeJS.Platform } = {}) {
  const def = opts.def === undefined ? currentSchedule() : opts.def;
  const settings = loadSettings(ctx.dataRoot);
  const state = loadState(ctx.dataRoot);
  const inbox = loadInbox(ctx.dataRoot);
  return {
    scheduler: {
      running: schedulerTimer !== null,
      registered: def !== null,
      time_zone: def?.timeZone ?? null,
      gate_note: def?.gateNote ?? "",
      gate: state.gate ?? null,
      ui_base: uiBase(),
    },
    jobs: (def?.jobs ?? []).map((j) => {
      const st = state.jobs[j.id] ?? {};
      const busy = running.has(`${ctx.dataRoot}\0${j.id}`);
      // 记着「运行中」、这个进程里却没在跑:上次跑到一半程序被关掉或重启了,别一直显示运行中
      const cut = st.status === "running" && !busy;
      return {
        id: j.id, label: j.label, description: j.description, default_at: j.at, at: jobAt(settings, j), enabled: jobEnabled(settings, j),
        last_day: st.last_day ?? null, last_run_at: st.last_run_at ?? null,
        status: cut ? "failed" : st.status ?? null, note: cut ? "上次运行没有正常结束(程序中途被关掉或重启);到点会再跑,也可以手动运行" : st.note ?? "",
        running: busy,
      };
    }),
    settings: {
      system: settings.system,
      system_supported: systemSupported(opts.platform),
      quiet: settings.quiet,
      cooldown_min: settings.cooldown_min,
      webhooks: settings.webhooks.map((h) => ({ id: h.id, kind: h.kind, kind_label: WEBHOOK_RULES[h.kind].label, label: h.label, enabled: h.enabled, url: maskWebhookUrl(h.url),
        uid: h.uid ?? null, signed: (h.kind === "feishu" || h.kind === "dingtalk") && !!h.secret })),
    },
    inbox: { items: inbox.slice(0, 100), unread: inbox.filter((x) => !x.read).length, total: inbox.length },
  };
}

const bad = (msg: string): never => { throw new ServiceError("bad_request", msg); };
const onlyKeys = (b: Record<string, unknown>, keys: string[]) => {
  const extra = Object.keys(b).filter((k) => !keys.includes(k));
  if (extra.length) bad(`不认识的字段:${extra.join(" / ")}`);
};

/** 所有写操作的单一入口(POST /notify/<action>) */
export async function notifyAction(ctx: ServiceContext, action: string, body: unknown, opts: { def?: ScheduleDef | null; deps?: Deps; now?: Date } = {}): Promise<unknown> {
  const def = opts.def === undefined ? currentSchedule() : opts.def;
  const deps = opts.deps ?? {};
  const b = isObj(body) ? body : bad("请求体必须是对象");
  switch (action) {
    case "settings": {
      onlyKeys(b, ["system", "quiet", "cooldown_min", "jobs"]);
      await serial(async () => {
        const s = loadSettings(ctx.dataRoot);
        if (b.system !== undefined) { if (typeof b.system !== "boolean") bad("system 必须是 true / false"); s.system = b.system as boolean; }
        if (b.quiet !== undefined) {
          const q = isObj(b.quiet) ? b.quiet : bad("quiet 必须是对象");
          onlyKeys(q, ["enabled", "from", "to"]);
          if (q.enabled !== undefined && typeof q.enabled !== "boolean") bad("quiet.enabled 必须是 true / false");
          for (const k of ["from", "to"] as const) if (q[k] !== undefined && !(typeof q[k] === "string" && HM.test(q[k] as string))) bad(`quiet.${k} 必须是 HH:MM`);
          s.quiet = { ...s.quiet, ...(q as Partial<Settings["quiet"]>) };
        }
        if (b.cooldown_min !== undefined) {
          if (!Number.isInteger(b.cooldown_min) || (b.cooldown_min as number) < 0 || (b.cooldown_min as number) > 1440) bad("cooldown_min 必须是 0–1440 的整数");
          s.cooldown_min = b.cooldown_min as number;
        }
        if (b.jobs !== undefined) {
          const jobs = isObj(b.jobs) ? b.jobs : bad("jobs 必须是对象");
          for (const [id, v] of Object.entries(jobs)) {
            if (!def?.jobs.some((j) => j.id === id)) bad(`没有这个任务:${id}`);
            const o = isObj(v) ? v : bad(`jobs.${id} 必须是对象`);
            onlyKeys(o, ["enabled", "at"]);
            if (o.enabled !== undefined && typeof o.enabled !== "boolean") bad(`jobs.${id}.enabled 必须是 true / false`);
            if (o.at !== undefined && !(typeof o.at === "string" && HM.test(o.at))) bad(`jobs.${id}.at 必须是 HH:MM`);
            s.jobs[id] = { ...(s.jobs[id] ?? {}), ...(o as { enabled?: boolean; at?: string }) };
          }
        }
        await saveSettings(ctx.dataRoot, s);
      });
      return notifyOverview(ctx, { def });
    }
    case "webhooks/add": {
      onlyKeys(b, ["kind", "url", "secret", "label", "uid"]);
      if (!isKind(b.kind)) bad("kind 只能是 feishu / wecom / dingtalk / wxpusher");
      const kind = b.kind as WebhookKind;
      const url = typeof b.url === "string" ? checkWebhookUrl(kind, b.url) : null;
      if (!url) bad(`这不是${WEBHOOK_RULES[kind].label}群机器人的地址(只接受 ${WEBHOOK_RULES[kind].hosts.join(" / ")} 的官方地址)`);
      // 企业微信群机器人没有签名密钥这一项:填了也用不上,别存下来让页面显示成「已签名」
      if (kind === "wecom" && b.secret !== undefined && b.secret !== "") bad("企业微信群机器人没有签名密钥,不用填");
      if (kind !== "wxpusher" && b.uid !== undefined && b.uid !== "") bad(`${WEBHOOK_RULES[kind].label}不用填 UID`);
      // WxPusher 两种凭据二选一、地址必须配对:SPT 个人令牌(SPT_ 开头)→ simple-push 地址;标准 appToken → message 地址 + 必填 uid
      if (kind === "wxpusher") {
        const spt = typeof b.secret === "string" && b.secret.trim().startsWith("SPT_");
        if (!(typeof b.secret === "string" && b.secret.trim())) bad("WxPusher 需要填 appToken(或 SPT 个人令牌,公众号「个人推送」页复制)");
        else if (spt !== url!.endsWith("/message/simple-push")) {
          bad(spt ? "SPT 令牌要把地址填成 https://wxpusher.zjiecode.com/api/send/message/simple-push"
                  : "appToken 要把地址填成 https://wxpusher.zjiecode.com/api/send/message");
        } else if (!spt && !(typeof b.uid === "string" && b.uid.trim())) {
          bad("标准方式(appToken)需要填 UID;图省事可以在公众号「个人推送」页拿 SPT 令牌,那样不用 UID");
        }
      }
      const secret = b.secret === undefined || b.secret === "" ? undefined : typeof b.secret === "string" && /^[\x21-\x7e]{8,128}$/.test(b.secret.trim()) ? b.secret.trim() : bad("签名密钥格式不对");
      const uid = b.uid === undefined || b.uid === "" ? undefined : typeof b.uid === "string" && WXPUSHER_UID_RE.test(b.uid.trim()) ? b.uid.trim() : bad("UID 格式不对(字母数字,4–64 位)");
      const label = b.label === undefined ? "" : typeof b.label === "string" ? b.label.trim().slice(0, 40) : bad("label 必须是文字");
      await serial(async () => {
        const s = loadSettings(ctx.dataRoot);
        if (s.webhooks.length >= 10) bad("最多 10 个群机器人");
        if (s.webhooks.some((h) => h.url === url && h.kind === kind && (kind !== "wxpusher" || h.uid === uid))) bad("这个机器人已经加过了");
        s.webhooks.push({ id: newId(), kind, url: url!, ...(secret ? { secret } : {}), ...(uid ? { uid } : {}), label, enabled: true });
        await saveSettings(ctx.dataRoot, s);
      });
      return notifyOverview(ctx, { def });
    }
    case "webhooks/update":
    case "webhooks/remove": {
      onlyKeys(b, action === "webhooks/remove" ? ["id"] : ["id", "enabled", "label"]);
      if (typeof b.id !== "string") bad("缺少 id");
      if (b.enabled !== undefined && typeof b.enabled !== "boolean") bad("enabled 必须是 true / false");
      if (b.label !== undefined && typeof b.label !== "string") bad("label 必须是文字");
      await serial(async () => {
        const s = loadSettings(ctx.dataRoot);
        const i = s.webhooks.findIndex((h) => h.id === b.id);
        if (i < 0) throw new ServiceError("not_found", "没有这个群机器人(可能已经删掉了)");
        if (action === "webhooks/remove") s.webhooks.splice(i, 1);
        else s.webhooks[i] = { ...s.webhooks[i]!, ...(b.enabled !== undefined ? { enabled: b.enabled as boolean } : {}), ...(b.label !== undefined ? { label: (b.label as string).trim().slice(0, 40) } : {}) };
        await saveSettings(ctx.dataRoot, s);
      });
      return notifyOverview(ctx, { def });
    }
    case "test": {
      onlyKeys(b, ["channel"]);
      const settings = loadSettings(ctx.dataRoot);
      const now = opts.now ?? new Date();
      const stamp = def ? `${localClock(now, def.timeZone).day} ${localClock(now, def.timeZone).hm}` : `${now.toISOString().slice(0, 16).replace("T", " ")}(UTC)`;
      const msg: NotifyMessage = { title: "测试消息", lines: [`这是一条测试消息,${stamp} 发出。收到说明这个渠道能用。`], link: "/inbox", dedupeKey: `test:${now.getTime()}` };
      if (b.channel === "system") {
        if (!systemSupported(deps.platform)) bad("系统通知目前只支持 macOS 和 Linux;Windows 请用群机器人推送");
        const outs = await pushAll({ ...settings, system: true, webhooks: [] }, msg, deps);
        return outs[0]!;
      }
      const hook = settings.webhooks.find((h) => h.id === b.channel);
      if (!hook) throw new ServiceError("not_found", "没有这个渠道");
      const outs = await pushAll({ ...settings, system: false, webhooks: [{ ...hook, enabled: true }] }, msg, deps);
      return outs[0]!;
    }
    case "jobs/run": {
      onlyKeys(b, ["id"]);
      const job = def?.jobs.find((j) => j.id === b.id);
      if (!job) throw new ServiceError("not_found", "没有这个任务");
      await runJob(ctx, def!, job, opts.now ?? new Date(), true, deps);
      return notifyOverview(ctx, { def });
    }
    case "inbox/read": {
      onlyKeys(b, ["ids", "all"]);
      const ids = b.all === true ? null : Array.isArray(b.ids) && b.ids.every((x) => typeof x === "string") ? new Set(b.ids as string[]) : bad("要么 all:true,要么给 ids");
      await serial(() => {
        const items = loadInbox(ctx.dataRoot).map((x) => (ids === null || ids.has(x.id) ? { ...x, read: true } : x));
        writeJson(fileOf(ctx.dataRoot, "inbox"), { version: 1, items });
      });
      return notifyOverview(ctx, { def });
    }
    case "inbox/clear": {
      onlyKeys(b, []);
      await serial(() => writeJson(fileOf(ctx.dataRoot, "inbox"), { version: 1, items: [] }));
      return notifyOverview(ctx, { def });
    }
    default:
      throw new ServiceError("not_found", `没有这个操作:${action.slice(0, 40)}`);
  }
}

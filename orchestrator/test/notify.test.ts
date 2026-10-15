import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  checkWebhookUrl, dingtalkSign, feishuSign, inQuietHours, localClock, maskWebhookUrl, messageText, notifyAction, notifyOverview,
  scrubSecrets, tick, uiBase, validateSchedule, webhookRequest, type GateResult, type JobContext, type JobResult, type ScheduleDef,
} from "../src/notify.ts";
import { ServiceError, type ServiceContext } from "../src/service.ts";
import { UI_PORT } from "../src/brand.ts";

const FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/0123abcd-4567-89ef-0123-456789abcdef";
const WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7xxx-4bc4-97a0-0ec2sifa5aaa";
const DING = "https://oapi.dingtalk.com/robot/send?access_token=abcdef0123456789abcdef";

function tmpCtx(): ServiceContext {
  return { repoRoot: process.cwd(), dataRoot: fs.mkdtempSync(path.join(os.tmpdir(), "vra-notify-")), python: "python3", node: process.execPath };
}
const clean = (ctx: ServiceContext) => fs.rmSync(ctx.dataRoot, { recursive: true, force: true });
/** 北京时间 → Date(UTC+8,无夏令时) */
const sh = (s: string) => new Date(`${s}:00+08:00`);

interface Harness { def: ScheduleDef; gateCalls: number; runs: JobContext[]; setGate(g: GateResult | Error): void; setRun(fn: (j: JobContext) => Promise<JobResult>): void }

function harness(at = "15:40"): Harness {
  let gate: GateResult | Error = { status: "open" };
  let run: (j: JobContext) => Promise<JobResult> = async (j) => ({ messages: [{ title: `摘要 ${j.day}`, lines: ["一行事实"], link: "/x", dedupeKey: `d:${j.day}` }] });
  const h: Harness = {
    gateCalls: 0, runs: [],
    setGate: (g) => { gate = g; },
    setRun: (fn) => { run = fn; },
    def: {
      timeZone: "Asia/Shanghai", gateNote: "测试",
      gate: async () => { h.gateCalls++; if (gate instanceof Error) throw gate; return gate; },
      jobs: [{ id: "digest", label: "摘要", description: "测试任务", at, defaultEnabled: true, run: (j) => { h.runs.push(j); return run(j); } }],
    },
  };
  return h;
}

const inbox = (ctx: ServiceContext) => notifyOverview(ctx, { def: null }).inbox;
const jobState = (ctx: ServiceContext, h: Harness) => notifyOverview(ctx, { def: h.def }).jobs[0]!;

test("时钟按计划时区算,不看本机时区", () => {
  assert.deepEqual(localClock(new Date("2026-10-08T07:40:00Z"), "Asia/Shanghai"), { day: "2026-10-08", hm: "15:40" });
  assert.deepEqual(localClock(new Date("2026-10-08T16:30:00Z"), "Asia/Shanghai"), { day: "2026-10-09", hm: "00:30" });
});

test("静默时段:同日区间与跨午夜区间", () => {
  const q = { enabled: true, from: "22:00", to: "08:00" };
  for (const hm of ["22:00", "23:59", "00:00", "07:59"]) assert.equal(inQuietHours(q, hm), true, hm);
  for (const hm of ["08:00", "12:00", "21:59"]) assert.equal(inQuietHours(q, hm), false, hm);
  assert.equal(inQuietHours({ enabled: true, from: "12:00", to: "13:00" }, "12:30"), true);
  assert.equal(inQuietHours({ enabled: true, from: "12:00", to: "13:00" }, "13:00"), false);
  assert.equal(inQuietHours({ ...q, enabled: false }, "23:00"), false);
});

test("签名与官方文档的参考实现一致(Python hmac 独立算出的值)", () => {
  // 飞书:hmac.new(f"{ts}\n{secret}".encode(), digestmod=sha256) —— 拼出来的串是密钥、消息为空
  assert.equal(feishuSign("SECtestsecret123", 1599360473), "J9McLHdI2Ik/t6sn5GWBWiFoU7xWQA6wzeN5ShiXCSw=");
  // 钉钉:hmac.new(secret, f"{ts}\n{secret}", sha256) → base64 → quote_plus;ts 是毫秒
  assert.equal(dingtalkSign("SECtestsecret123", 1599360473000), "qGd%2Fu1gXDnWhzwD5Xgq0Qom2s8yAA3zjASeEGxMF3nk%3D");
});

test("机器人地址只认三家官方域名和路径", () => {
  assert.equal(checkWebhookUrl("feishu", FEISHU), FEISHU);
  assert.equal(checkWebhookUrl("wecom", WECOM), WECOM);
  assert.equal(checkWebhookUrl("dingtalk", DING), DING);
  for (const [kind, url] of [
    ["feishu", FEISHU.replace("https:", "http:")],
    ["feishu", FEISHU.replace("open.feishu.cn", "open.feishu.cn.evil.example")],
    ["feishu", FEISHU.replace("open.feishu.cn", "user:pw@open.feishu.cn")],
    ["feishu", FEISHU.replace("open.feishu.cn", "open.feishu.cn:8443")],
    ["feishu", FEISHU + "?next=http://169.254.169.254/"],
    ["feishu", "https://open.feishu.cn/open-apis/other/v2/hook/0123abcd-4567"],
    ["wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"],
    ["wecom", FEISHU],
    ["dingtalk", "https://oapi.dingtalk.com/robot/send?access_token="],
    ["dingtalk", "https://127.0.0.1/robot/send?access_token=abcdef0123456789"],
  ] as const) assert.equal(checkWebhookUrl(kind, url), null, url);
});

test("打码与去密:错误文字里不留地址、key、签名密钥", () => {
  assert.equal(maskWebhookUrl(WECOM), "qyapi.weixin.qq.com/…5aaa");
  const hook = { url: WECOM, secret: "SECsupersecret999" };
  const out = scrubSecrets(`fetch failed ${WECOM} key=693a91f6-7xxx-4bc4-97a0-0ec2sifa5aaa sign with SECsupersecret999`, hook);
  assert.doesNotMatch(out, /693a91f6|SECsupersecret|qyapi/);
});

test("推送正文:标题 + 事实行 + 本机链接,超长按字节截断", () => {
  const t = messageText({ title: "收盘摘要", lines: ["a", "b"], link: "/daily-review" }, "http://127.0.0.1:5930");
  assert.equal(t, "【收盘摘要】\na\nb\n在本机打开:http://127.0.0.1:5930/daily-review");
  assert.equal(messageText({ title: "t", lines: [], link: "/x" }, "https://vr.example.app"), "【t】\n\n打开:https://vr.example.app/x", "远程模式的对外地址不写「在本机」");
  assert.equal(uiBase({ VRA_PUSH_LINK_BASE: "https://vr.example.app/" }), "https://vr.example.app");
  assert.equal(uiBase({}), `http://127.0.0.1:${UI_PORT}`);
  assert.equal(uiBase({ VRA_UI_PORT: "5931" }), "http://127.0.0.1:5931", "默认端口被占时启动器换了端口,推送链接跟着换");
  assert.equal(uiBase({ VRA_UI_PORT: "abc" }), `http://127.0.0.1:${UI_PORT}`);
  assert.equal(uiBase({ VRA_UI_PORT: "5931", VRA_PUSH_LINK_BASE: "https://a.example/" }), "https://a.example", "远程模式仍用对外地址");
  const long = messageText({ title: "长", lines: ["汉".repeat(3000)] }, "http://x");
  assert.ok(Buffer.byteLength(long, "utf8") <= 2000);
  assert.match(long, /完整内容见收件箱/);
});

test("注册校验:id / 时刻 / 时区", () => {
  const h = harness();
  validateSchedule(h.def);
  assert.throws(() => validateSchedule({ ...h.def, timeZone: "Mars/Base" }), /时区/);
  assert.throws(() => validateSchedule({ ...h.def, jobs: [{ ...h.def.jobs[0]!, at: "25:00" }] }), /HH:MM/);
  assert.throws(() => validateSchedule({ ...h.def, jobs: [h.def.jobs[0]!, h.def.jobs[0]!] }), /重复/);
});

test("到点前不跑、不查门;到点后跑一次,当天不再跑,第二天再跑", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    let r = await tick(ctx, { now: sh("2026-10-08T15:39"), def: h.def });
    assert.deepEqual(r.due, []);
    assert.equal(h.gateCalls, 0);
    r = await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    assert.deepEqual(r.ran.map((x) => x.status), ["ok"]);
    assert.equal(h.runs[0]!.day, "2026-10-08");
    assert.equal(inbox(ctx).unread, 1);
    r = await tick(ctx, { now: sh("2026-10-08T20:00"), def: h.def });
    assert.deepEqual(r.due, []);
    assert.equal(h.runs.length, 1);
    // 当天开机晚了也补跑一次(第二天 21:00 才第一次打开)
    r = await tick(ctx, { now: sh("2026-10-09T21:00"), def: h.def });
    assert.deepEqual(r.ran.map((x) => x.status), ["ok"]);
    assert.equal(h.runs[1]!.day, "2026-10-09");
  } finally { clean(ctx); }
});

test("门关着:标成今天不运行,不跑任务,当天不再查", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    h.setGate({ status: "closed", reason: "今天休息" });
    const r = await tick(ctx, { now: sh("2026-10-10T16:00"), def: h.def });
    assert.deepEqual(r.ran, [{ id: "digest", status: "skipped", note: "今天休息" }]);
    assert.equal(h.runs.length, 0);
    await tick(ctx, { now: sh("2026-10-10T17:00"), def: h.def });
    assert.equal(h.gateCalls, 1);
    assert.equal(inbox(ctx).total, 0, "休息日不往收件箱塞东西");
  } finally { clean(ctx); }
});

test("门判定取不到:当天不跑、只说明一次,15 分钟后再试,恢复后照常跑", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    h.setGate(new Error("日历连不上"));
    let r = await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    assert.equal(r.ran[0]!.status, "blocked");
    assert.equal(h.runs.length, 0, "取不到就不跑,不当成开门");
    assert.equal(jobState(ctx, h).status, "blocked");
    await tick(ctx, { now: sh("2026-10-08T15:50"), def: h.def });
    assert.equal(h.gateCalls, 1, "15 分钟内不重复查");
    await tick(ctx, { now: sh("2026-10-08T15:56"), def: h.def });
    assert.equal(h.gateCalls, 2);
    const notices = inbox(ctx).items.filter((x) => x.kind === "notice");
    assert.equal(notices.length, 1, "同一天只说明一次");
    assert.match(notices[0]!.lines[0]!, /日历连不上/);
    h.setGate({ status: "open" });
    r = await tick(ctx, { now: sh("2026-10-08T16:12"), def: h.def });
    assert.deepEqual(r.ran.map((x) => x.status), ["ok"]);
  } finally { clean(ctx); }
});

test("任务失败:10 分钟后重试,第三次还失败就放弃并在收件箱说明", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    h.setRun(async () => { throw new Error("上游 502"); });
    await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    assert.match(jobState(ctx, h).note, /第 1 次失败,10 分钟后重试:上游 502/);
    await tick(ctx, { now: sh("2026-10-08T15:45"), def: h.def });
    assert.equal(h.runs.length, 1, "重试间隔没到不跑");
    await tick(ctx, { now: sh("2026-10-08T15:50"), def: h.def });
    await tick(ctx, { now: sh("2026-10-08T16:00"), def: h.def });
    assert.equal(h.runs.length, 3);
    assert.match(jobState(ctx, h).note, /今天试了 3 次都失败/);
    await tick(ctx, { now: sh("2026-10-08T17:00"), def: h.def });
    assert.equal(h.runs.length, 3, "放弃后当天不再试");
    assert.equal(inbox(ctx).items.filter((x) => x.kind === "notice").length, 1);
  } finally { clean(ctx); }
});

test("任务返回的消息格式不对按失败处理,不往收件箱写半截东西", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    h.setRun(async () => ({ messages: [{ title: "x", lines: [1 as unknown as string], dedupeKey: "k" }] }));
    await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    assert.equal(jobState(ctx, h).status, "failed");
    assert.equal(inbox(ctx).total, 0);
  } finally { clean(ctx); }
});

function fakeFetch(calls: { url: string; body: Record<string, unknown> }[], reply: (url: string) => { status: number; body: unknown } = () => ({ status: 200, body: { code: 0, errcode: 0 } })) {
  return (async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    const r = reply(String(url));
    return new Response(JSON.stringify(r.body), { status: r.status });
  }) as typeof fetch;
}

test("设置文件仅当前用户可读;接口只回显打码地址,不回显签名密钥", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    const ov = await notifyAction(ctx, "webhooks/add", { kind: "feishu", url: FEISHU, secret: "SECsupersecret999", label: "研究群" }, { def: h.def }) as ReturnType<typeof notifyOverview>;
    const text = JSON.stringify(ov);
    assert.doesNotMatch(text, /0123abcd-4567|SECsupersecret/);
    assert.equal(ov.settings.webhooks[0]!.signed, true);
    if (process.platform !== "win32") assert.equal(fs.statSync(path.join(ctx.dataRoot, "notify", "settings.json")).mode & 0o777, 0o600);
    await assert.rejects(notifyAction(ctx, "webhooks/add", { kind: "feishu", url: "https://example.com/hook" }, { def: h.def }), (e: unknown) => e instanceof ServiceError && e.code === "bad_request");
    await assert.rejects(notifyAction(ctx, "webhooks/add", { kind: "feishu", url: FEISHU }, { def: h.def }), /已经加过/);
    await assert.rejects(notifyAction(ctx, "settings", { quiet: { from: "7:00" } }, { def: h.def }), /HH:MM/);
    await assert.rejects(notifyAction(ctx, "settings", { jobs: { nope: { enabled: false } } }, { def: h.def }), /没有这个任务/);
    await assert.rejects(notifyAction(ctx, "settings", { extra: 1 }, { def: h.def }), /不认识的字段/);
  } finally { clean(ctx); }
});

test("发送测试:飞书带秒级时间戳与签名;钉钉签名拼在地址上;失败原因去掉地址", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    await notifyAction(ctx, "webhooks/add", { kind: "feishu", url: FEISHU, secret: "SECsupersecret999" }, { def: h.def });
    await notifyAction(ctx, "webhooks/add", { kind: "dingtalk", url: DING, secret: "SECdingsecret0001" }, { def: h.def });
    const hooks = notifyOverview(ctx, { def: h.def }).settings.webhooks;
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    const deps = { fetch: fakeFetch(calls), sleep: async () => undefined };
    assert.deepEqual(await notifyAction(ctx, "test", { channel: hooks[0]!.id }, { def: h.def, deps }), { channel: "飞书", ok: true });
    const fs1 = calls[0]!;
    assert.equal(fs1.url, FEISHU);
    assert.match(String(fs1.body.timestamp), /^\d{10}$/);
    assert.equal(fs1.body.sign, feishuSign("SECsupersecret999", Number(fs1.body.timestamp)));
    assert.equal(fs1.body.msg_type, "text");
    await notifyAction(ctx, "test", { channel: hooks[1]!.id }, { def: h.def, deps });
    const u = new URL(calls[1]!.url);
    assert.equal(u.searchParams.get("access_token"), "abcdef0123456789abcdef");
    assert.match(u.searchParams.get("timestamp")!, /^\d{13}$/);
    assert.equal(encodeURIComponent(u.searchParams.get("sign")!), dingtalkSign("SECdingsecret0001", Number(u.searchParams.get("timestamp"))));
    // 平台回错误码:不重试,原因里没有地址和密钥
    const bad = await notifyAction(ctx, "test", { channel: hooks[0]!.id }, {
      def: h.def, deps: { fetch: fakeFetch(calls, () => ({ status: 200, body: { code: 19021, msg: `sign match fail ${FEISHU}` } })), sleep: async () => undefined },
    }) as { ok: boolean; error: string };
    assert.equal(bad.ok, false);
    assert.match(bad.error, /19021/);
    assert.doesNotMatch(bad.error, /0123abcd|feishu\.cn/);
    // 网络错误 / 5xx 重试 3 次
    const before = calls.length;
    await notifyAction(ctx, "test", { channel: hooks[0]!.id }, { def: h.def, deps: { fetch: fakeFetch(calls, () => ({ status: 502, body: {} })), sleep: async () => undefined } });
    assert.equal(calls.length - before, 3);
  } finally { clean(ctx); }
});

test("推送过滤:静默时段只进收件箱;24 小时内同一条不重复推;手动运行照推", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    h.setRun(async () => ({ messages: [{ title: "同一条", lines: ["x"], dedupeKey: "same" }] }));
    await notifyAction(ctx, "webhooks/add", { kind: "wecom", url: WECOM }, { def: h.def });
    await notifyAction(ctx, "settings", { quiet: { enabled: true, from: "15:00", to: "16:00" }, cooldown_min: 0 }, { def: h.def });
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    const deps = { fetch: fakeFetch(calls), sleep: async () => undefined };
    await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def, deps });
    assert.equal(calls.length, 0);
    assert.match(jobState(ctx, h).note, /静默时段/);
    assert.equal(inbox(ctx).unread, 1);
    await notifyAction(ctx, "settings", { quiet: { enabled: false } }, { def: h.def });
    await tick(ctx, { now: sh("2026-10-09T16:30"), def: h.def, deps });
    assert.equal(calls.length, 1);
    assert.equal((calls[0]!.body.text as { content: string }).content, "【同一条】\nx");
    await tick(ctx, { now: sh("2026-10-10T16:00"), def: h.def, deps });
    assert.equal(calls.length, 1, "不到 24 小时,同一条不再推");
    assert.match(jobState(ctx, h).note, /24 小时内推过/);
    await notifyAction(ctx, "jobs/run", { id: "digest" }, { def: h.def, deps, now: sh("2026-10-10T16:05") });
    assert.equal(calls.length, 2, "手动运行是用户要看,照推");
    assert.equal(jobState(ctx, h).last_day, "2026-10-10", "手动运行不改「今天已跑」的记录");
  } finally { clean(ctx); }
});

test("手动运行不会让当天的定时运行被跳过", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    await notifyAction(ctx, "jobs/run", { id: "digest" }, { def: h.def, now: sh("2026-10-08T10:00") });
    assert.equal(h.runs[0]!.manual, true);
    await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    assert.equal(h.runs.length, 2);
  } finally { clean(ctx); }
});

test("手动运行照推,但不记推送时间、不改任务进度:当天定时那次照常推、照常看到同样的新条目", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    h.setRun(async (j) => ({ messages: [{ title: `摘要 ${j.day}`, lines: ["x"], dedupeKey: `d:${j.day}` }], state: { seen: ((j.state as { seen?: number } | undefined)?.seen ?? 0) + 1 } }));
    await notifyAction(ctx, "webhooks/add", { kind: "wecom", url: WECOM }, { def: h.def });
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    const deps = { fetch: fakeFetch(calls), sleep: async () => undefined };
    await notifyAction(ctx, "jobs/run", { id: "digest" }, { def: h.def, deps, now: sh("2026-10-08T10:00") });
    assert.equal(calls.length, 1);
    await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def, deps });
    assert.equal(calls.length, 2, "同一天的定时推送没被手动那次去重 / 冷却吞掉");
    assert.deepEqual(h.runs.map((j) => j.state), [undefined, undefined], "手动那次的进度没存下来");
    await tick(ctx, { now: sh("2026-10-09T15:40"), def: h.def, deps });
    assert.deepEqual(h.runs[2]!.state, { seen: 1 }, "定时那次的进度照常保存");
  } finally { clean(ctx); }
});

test("企业微信不收签名密钥;记着「运行中」却没在跑的任务显示为没正常结束", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    await assert.rejects(notifyAction(ctx, "webhooks/add", { kind: "wecom", url: WECOM, secret: "SECsupersecret999" }, { def: h.def }), /没有签名密钥/);
    await notifyAction(ctx, "webhooks/add", { kind: "wecom", url: WECOM, secret: "" }, { def: h.def });
    assert.equal(notifyOverview(ctx, { def: h.def }).settings.webhooks[0]!.signed, false);
    // 老版本存下的企业微信密钥:页面也不能显示成「已签名」
    const sp = path.join(ctx.dataRoot, "notify", "settings.json");
    const saved = JSON.parse(fs.readFileSync(sp, "utf8")) as { webhooks: Record<string, unknown>[] };
    saved.webhooks[0]!.secret = "SEColdsecret0001";
    fs.writeFileSync(sp, JSON.stringify(saved));
    assert.equal(notifyOverview(ctx, { def: h.def }).settings.webhooks[0]!.signed, false);
    fs.writeFileSync(path.join(ctx.dataRoot, "notify", "state.json"), JSON.stringify({ version: 1, jobs: { digest: { status: "running", note: "正在运行" } }, sent: {}, pushed: {} }));
    const j = jobState(ctx, h);
    assert.deepEqual([j.status, j.running], ["failed", false]);
    assert.match(j.note, /没有正常结束/);
  } finally { clean(ctx); }
});

test("一拍里排了几个任务:每个任务开始前刷新调度锁,慢任务不会让锁被当成死锁", async () => {
  const ctx = tmpCtx();
  try {
    const lockFile = path.join(ctx.dataRoot, "notify", "tick.lock");
    const seen: number[] = [];
    const old = new Date(Date.now() - 3600_000);
    const job = (id: string, fn: () => void) => ({ id, label: id, description: "", at: "15:40", defaultEnabled: true, run: async () => { fn(); return { messages: [] }; } });
    const def: ScheduleDef = { timeZone: "Asia/Shanghai", gateNote: "", gate: async () => ({ status: "open" }), jobs: [
      job("slow", () => fs.utimesSync(lockFile, old, old)),   // 模拟跑了很久:锁的修改时间停在一小时前
      job("next", () => seen.push(fs.statSync(lockFile).mtimeMs)),
    ] };
    await tick(ctx, { now: sh("2026-10-08T15:40"), def });
    assert.equal(seen.length, 1);
    assert.ok(Date.now() - seen[0]! < 60_000, "第二个任务开始前锁被刷新过");
    assert.ok(!fs.existsSync(lockFile), "一拍结束锁被释放");
  } finally { clean(ctx); }
});

test("任务自己的状态跨次保留(比如已经见过的条目)", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    h.setRun(async (j) => ({ messages: [], state: { n: ((j.state as { n?: number } | undefined)?.n ?? 0) + 1 } }));
    await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    await tick(ctx, { now: sh("2026-10-09T15:40"), def: h.def });
    assert.deepEqual(h.runs.map((j) => j.state), [undefined, { n: 1 }]);
  } finally { clean(ctx); }
});

test("关掉的任务不跑;改时刻生效", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    await notifyAction(ctx, "settings", { jobs: { digest: { enabled: false } } }, { def: h.def });
    await tick(ctx, { now: sh("2026-10-08T16:00"), def: h.def });
    assert.equal(h.runs.length, 0);
    await notifyAction(ctx, "settings", { jobs: { digest: { enabled: true, at: "17:00" } } }, { def: h.def });
    await tick(ctx, { now: sh("2026-10-08T16:30"), def: h.def });
    assert.equal(h.runs.length, 0);
    await tick(ctx, { now: sh("2026-10-08T17:00"), def: h.def });
    assert.equal(h.runs.length, 1);
    assert.equal(h.runs[0]!.at, "17:00", "任务拿得到改后的时刻");
  } finally { clean(ctx); }
});

test("收件箱:标已读、清空、最多留 300 条", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    let k = 0;
    h.setRun(async () => ({ messages: Array.from({ length: 160 }, () => ({ title: `m${k}`, lines: [], dedupeKey: `k${k++}` })) }));
    await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    await tick(ctx, { now: sh("2026-10-09T15:40"), def: h.def });
    assert.equal(inbox(ctx).total, 300);
    const first = inbox(ctx).items[0]!.id;
    await notifyAction(ctx, "inbox/read", { ids: [first] }, { def: h.def });
    assert.equal(inbox(ctx).unread, 299);
    await notifyAction(ctx, "inbox/read", { all: true }, { def: h.def });
    assert.equal(inbox(ctx).unread, 0);
    await notifyAction(ctx, "inbox/clear", {}, { def: h.def });
    assert.equal(inbox(ctx).total, 0);
  } finally { clean(ctx); }
});

test("两个后台进程共用一个数据目录时,只有一个在调度", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    fs.mkdirSync(path.join(ctx.dataRoot, "notify"), { recursive: true });
    fs.writeFileSync(path.join(ctx.dataRoot, "notify", "tick.lock"), "999:other");
    const r = await tick(ctx, { now: sh("2026-10-08T15:40"), def: h.def });
    assert.match(r.skipped ?? "", /另一个后台进程/);
    assert.equal(h.runs.length, 0);
  } finally { clean(ctx); }
});

test("🔴 notify 目录或文件是符号链接 → 读写一律拒绝,数据目录外的文件一个字节不变(审计 A2)", async () => {
  const ctx = tmpCtx();
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "vra-notify-out-"));
  try {
    const victim = path.join(outside, "settings.json");
    fs.writeFileSync(victim, "外部文件\n");
    fs.symlinkSync(outside, path.join(ctx.dataRoot, "notify"), "dir");
    await assert.rejects(notifyAction(ctx, "settings", { system: true }, { def: null }), (e: unknown) => e instanceof ServiceError && e.code === "notify_path_symlink");
    assert.throws(() => notifyOverview(ctx, { def: null }), (e: unknown) => e instanceof ServiceError && e.code === "notify_path_symlink");
    assert.equal(fs.readFileSync(victim, "utf8"), "外部文件\n");
    assert.deepEqual(fs.readdirSync(outside), ["settings.json"], "外部目录里不能多出临时文件");

    fs.unlinkSync(path.join(ctx.dataRoot, "notify"));
    fs.mkdirSync(path.join(ctx.dataRoot, "notify"));
    fs.symlinkSync(victim, path.join(ctx.dataRoot, "notify", "inbox.json"));
    await assert.rejects(notifyAction(ctx, "inbox/read", { all: true }, { def: null }), (e: unknown) => e instanceof ServiceError && e.code === "notify_path_symlink");
    assert.equal(fs.readFileSync(victim, "utf8"), "外部文件\n");
  } finally { clean(ctx); fs.rmSync(outside, { recursive: true, force: true }); }
});

test("🔴 设置 / 收件箱文件坏了 → 明确报错,写操作不能拿默认值把原文件覆盖掉(审计 A4)", async () => {
  const ctx = tmpCtx();
  try {
    const dir = path.join(ctx.dataRoot, "notify");
    fs.mkdirSync(dir);
    const settings = path.join(dir, "settings.json");
    const broken = '{"version":1,"webhooks":[{"id":"a","kind":"feishu","url":"' + FEISHU + '"';   // 截断
    fs.writeFileSync(settings, broken);
    assert.throws(() => notifyOverview(ctx, { def: null }), (e: unknown) => e instanceof ServiceError && e.code === "notify_file_corrupt");
    await assert.rejects(notifyAction(ctx, "settings", { system: true }, { def: null }), (e: unknown) => e instanceof ServiceError && e.code === "notify_file_corrupt");
    assert.equal(fs.readFileSync(settings, "utf8"), broken, "原文件被覆盖了");
    fs.writeFileSync(settings, "[1,2]");
    assert.throws(() => notifyOverview(ctx, { def: null }), (e: unknown) => e instanceof ServiceError && e.code === "notify_file_corrupt", "结构不对也不能当默认值");
    fs.rmSync(settings);

    const inboxFile = path.join(dir, "inbox.json");
    fs.writeFileSync(inboxFile, '{"version":1,"items":[{"id":"x"');
    await assert.rejects(notifyAction(ctx, "inbox/read", { all: true }, { def: null }), (e: unknown) => e instanceof ServiceError && e.code === "notify_file_corrupt");
    assert.equal(fs.readFileSync(inboxFile, "utf8"), '{"version":1,"items":[{"id":"x"');
    fs.rmSync(inboxFile);
    assert.equal(inbox(ctx).total, 0, "文件不存在才是空收件箱");
  } finally { clean(ctx); }
});

test("🔴 JSON 能解析、里面某一条坏了:同样报损坏,改开关 / 全部标已读都不能把那条从磁盘上删掉(审计第二轮 A4)", async () => {
  const ctx = tmpCtx();
  try {
    const dir = path.join(ctx.dataRoot, "notify");
    fs.mkdirSync(dir);
    const settings = path.join(dir, "settings.json");
    const isCorrupt = (e: unknown) => e instanceof ServiceError && e.code === "notify_file_corrupt";
    for (const body of [
      { version: 1, webhooks: [{ id: "a", kind: "feishu?", url: FEISHU }] },          // kind 坏了,地址还在
      { version: 1, webhooks: [{ id: "a", kind: "feishu" }] },                         // 缺地址
      { version: 1, webhooks: [{ id: "a", kind: "feishu", url: "https://evil.example/hook" }] },
      { version: 1, webhooks: "oops" },
    ]) {
      const text = JSON.stringify(body);
      fs.writeFileSync(settings, text);
      assert.throws(() => notifyOverview(ctx, { def: null }), isCorrupt, text);
      await assert.rejects(notifyAction(ctx, "settings", { system: true }, { def: null }), isCorrupt, text);
      assert.equal(fs.readFileSync(settings, "utf8"), text, "原字节保留");
    }
    fs.writeFileSync(settings, "null");
    await assert.rejects(notifyAction(ctx, "settings", { system: true }, { def: null }), isCorrupt, "内容是 null 不等于没有文件");
    assert.equal(fs.readFileSync(settings, "utf8"), "null");
    fs.rmSync(settings);

    const inboxFile = path.join(dir, "inbox.json");
    const text = JSON.stringify({ version: 1, items: [{ id: "x", title: "日报", lines: "正文还在", read: false }] });
    fs.writeFileSync(inboxFile, text);
    await assert.rejects(notifyAction(ctx, "inbox/read", { all: true }, { def: null }), isCorrupt);
    assert.equal(fs.readFileSync(inboxFile, "utf8"), text);
  } finally { clean(ctx); }
});

test("每拍钩子:每拍都调(没到点的任务也一样),门判断共用按天缓存;钩子出错不耽误定时任务", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness("15:40");
    const seen: string[] = [];
    let fail = false;
    const def: ScheduleDef = {
      ...h.def,
      beat: async (b) => {
        seen.push(b.hm);
        if (fail) throw new Error("取数超时");
        if (b.hm === "09:25") { const g = await b.gate(); return `门 ${g.status}`; }
      },
    };
    validateSchedule(def);
    let r = await tick(ctx, { now: sh("2026-10-08T09:24"), def });
    assert.equal(r.beat, undefined);
    assert.equal(h.gateCalls, 0, "钩子没要门判断时不查门");
    r = await tick(ctx, { now: sh("2026-10-08T09:25"), def });
    assert.equal(r.beat, "门 open");
    assert.equal(h.gateCalls, 1);
    fail = true;
    r = await tick(ctx, { now: sh("2026-10-08T15:40"), def });
    assert.match(r.beat ?? "", /^出错:取数超时/);
    assert.deepEqual(r.ran.map((x) => x.status), ["ok"], "钩子出错,到点的任务照跑");
    assert.equal(h.gateCalls, 1, "任务和钩子共用当天的门判断");
    assert.deepEqual(seen, ["09:24", "09:25", "15:40"]);
    assert.throws(() => validateSchedule({ ...h.def, beat: "x" as never }), /每拍钩子/);
  } finally { clean(ctx); }
});

test("🔴 每拍钩子要的门判断还在路上(钩子超时先返回了),任务再要门判断时接着等这一个,不并发再查(Opus 审计待核实项)", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness("15:40");
    let release!: (g: GateResult) => void;
    let calls = 0;
    const def: ScheduleDef = {
      ...h.def,
      gate: () => { calls++; return new Promise<GateResult>((res) => { release = res; }); },
      // 模拟钩子在门判断回来之前就结束了(真实情况是 60 秒超时,门判断要 90 秒才超时)
      beat: async (b) => { void b.gate(); return "钩子先走了"; },
    };
    const pending = tick(ctx, { now: sh("2026-10-08T15:40"), def });
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(calls, 1, "钩子发出的那一个");
    release({ status: "open" });
    const r = await pending;
    assert.equal(calls, 1, "任务没有另发一个门判断");
    assert.equal(r.gate?.status, "open");
    assert.deepEqual(r.ran.map((x) => x.status), ["ok"]);
    // 晚到的请求结束后清掉:第二天照常重新判断
    def.gate = async () => { calls++; return { status: "closed", reason: "周六" }; };
    const sat = await tick(ctx, { now: sh("2026-10-10T15:40"), def });
    assert.equal(calls, 2);
    assert.equal(sat.gate?.status, "closed");
  } finally { clean(ctx); }
});

test("🔴 队列跑过午夜就停:周五的开市判定不带到周六,剩下的任务下一拍按新的一天重新判断", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness("15:40");
    let t = sh("2026-10-09T23:59");  // 周五
    const ran: { id: string; day: string }[] = [];
    const job = (id: string, at: string) => ({ id, label: id, description: "测试", at, defaultEnabled: true,
      run: async (j: JobContext): Promise<JobResult> => {
        ran.push({ id, day: j.day });
        if (id === "first") t = sh("2026-10-10T00:01");  // 第一个任务跑了两分钟,跨进周六
        return { messages: [] };
      } });
    const def: ScheduleDef = { ...h.def, jobs: [job("first", "15:40"), job("second", "15:40")] };
    const r = await tick(ctx, { clock: () => t, def });
    assert.deepEqual(ran, [{ id: "first", day: "2026-10-09" }], "第二个任务没在周六按周五的判定跑");
    assert.equal(r.ran[1]?.status, "deferred");
    assert.match(r.ran[1]?.note ?? "", /2026-10-10/);
    const st = notifyOverview(ctx, { def }).jobs.find((x) => x.id === "second")!;
    assert.notEqual(st.last_day, "2026-10-10", "周六不能被记成已运行");
    // 周六到点那一拍按周六重新查门:休市 → 两个任务都记为今天不运行,一个都不跑
    h.setGate({ status: "closed", reason: "周末休市" });
    const before = h.gateCalls;
    assert.deepEqual((await tick(ctx, { clock: () => sh("2026-10-10T00:02"), def })).due, [], "周六还没到点");
    const r2 = await tick(ctx, { clock: () => sh("2026-10-10T15:40"), def });
    assert.equal(h.gateCalls, before + 1);
    assert.deepEqual(r2.ran.map((x) => x.status), ["skipped", "skipped"]);
    assert.equal(ran.length, 1);
    // 同一天里时钟往回调到点前:不跑,下一拍到点再跑
    t = sh("2026-10-12T15:40");
    h.setGate({ status: "open" });
    const back = { ...def, jobs: [job("third", "15:40"), job("fourth", "15:41")] };
    let calls = 0;
    const r3 = await tick(ctx, { now: sh("2026-10-12T15:41"), clock: () => (calls++ ? sh("2026-10-12T15:40") : sh("2026-10-12T15:41")), def: back });
    assert.deepEqual(r3.ran.map((x) => [x.id, x.status]), [["third", "ok"], ["fourth", "deferred"]]);
  } finally {
    clean(ctx);
  }
});

// ---------------------------------------------------------------- WxPusher 渠道
const WXP_URL = "https://wxpusher.zjiecode.com/api/send/message";
const WXP_TOKEN = "AT_qhwTF1LMQPeSGCjXPsLLBhhhRaN2";
const WXP_UID = "73642";

test("WxPusher 只认官方 API 地址,凭据必须走 appToken + UID", () => {
  assert.equal(checkWebhookUrl("wxpusher", WXP_URL), WXP_URL);
  // 地址上带任何参数都不收:凭据不能放进 URL(它会被回显 / 打码 / 记日志)
  assert.equal(checkWebhookUrl("wxpusher", `${WXP_URL}?appToken=x`), null);
  assert.equal(checkWebhookUrl("wxpusher", "https://wxpusher.zjiecode.com/api/send/message/simple"), null);
  assert.equal(checkWebhookUrl("wxpusher", WXP_URL.replace("https:", "http:")), null);
  assert.equal(checkWebhookUrl("wxpusher", "https://evil.example/api/send/message"), null);
});

test("WxPusher 请求体:appToken + uids + content,超长按 500 字截断", () => {
  const hook = { id: "t", kind: "wxpusher" as const, url: WXP_URL, secret: WXP_TOKEN, uid: WXP_UID, label: "", enabled: true };
  const now = new Date("2026-10-14T08:00:00Z");
  const body = webhookRequest(hook, messageText({ title: "测试", lines: ["一行"] }), now).body;
  assert.equal(body.appToken, WXP_TOKEN);
  assert.deepEqual(body.uids, [WXP_UID]);
  assert.equal(body.contentType, 1);
  assert.match(String(body.content), /^【测试】/);
  assert.ok(String(body.summary).length <= 100);
  // 600 个汉字 → 截到 500 字以内并带「见收件箱」尾巴
  const long = "汉".repeat(600);
  const clipped = webhookRequest(hook, long, now).body;
  assert.ok([...String(clipped.content)].length <= 500);
  assert.match(String(clipped.content), /完整内容见收件箱/);
  assert.equal(String(clipped.summary).length <= 100, true);
});

test("WxPusher 添加:appToken 与 UID 必填,回显打码地址与 UID 但不回显 appToken", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    const WXP = `${WXP_URL}`;
    await assert.rejects(
      notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: WXP }, { def: h.def }),
      (e: unknown) => e instanceof ServiceError && e.code === "bad_request" && /appToken/.test(e.message),
    );
    await assert.rejects(
      notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: WXP, secret: WXP_TOKEN }, { def: h.def }),
      (e: unknown) => e instanceof ServiceError && /UID/.test(e.message),
    );
    await assert.rejects(
      notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: WXP, secret: WXP_TOKEN, uid: "不是合法UID!" }, { def: h.def }),
      (e: unknown) => e instanceof ServiceError && /UID/.test(e.message),
    );
    const ov = await notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: WXP, secret: WXP_TOKEN, uid: WXP_UID, label: "微信推送" }, { def: h.def }) as ReturnType<typeof notifyOverview>;
    const hook = ov.settings.webhooks[0]!;
    assert.equal(hook.kind, "wxpusher");
    assert.equal(hook.kind_label, "WxPusher");
    assert.equal(hook.uid, WXP_UID);            // UID 不是凭据,明面显示方便辨认
    assert.doesNotMatch(JSON.stringify(ov), /AT_qhwTF1LMQPeSGCjXPsLLBhhhRaN2/); // appToken 绝不回显
    assert.equal(hook.signed, false, "WxPusher 不是签名,别显示成已签名");
    // 别的渠道带 UID → 拒绝
    await assert.rejects(
      notifyAction(ctx, "webhooks/add", { kind: "wecom", url: WECOM, uid: WXP_UID }, { def: h.def }),
      (e: unknown) => e instanceof ServiceError && /UID/.test(e.message),
    );
  } finally { clean(ctx); }
});

test("WxPusher 发送:code 1000 才算成功;错误信息里抹掉 appToken 和 UID", async () => {
  const ctx = tmpCtx();
  try {
    const h = harness();
    const ov = await notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: WXP_URL, secret: WXP_TOKEN, uid: WXP_UID }, { def: h.def }) as ReturnType<typeof notifyOverview>;
    const hookId = ov.settings.webhooks[0]!.id;
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    const deps = { fetch: fakeFetch(calls, () => ({ status: 200, body: { code: 1000, msg: "处理成功", data: [] } })), sleep: async () => undefined };
    const out = await notifyAction(ctx, "test", { channel: hookId }, { def: h.def, deps }) as { channel: string; ok: boolean };
    assert.equal(out.ok, true);
    assert.equal(calls[0]!.url, WXP_URL);
    assert.equal(calls[0]!.body.appToken, WXP_TOKEN);
    assert.deepEqual(calls[0]!.body.uids, [WXP_UID]);
    // 平台拒绝(错误的 appToken):错误文字里不能带凭据
    const calls2: { url: string; body: Record<string, unknown> }[] = [];
    const bad = await notifyAction(ctx, "test", { channel: hookId }, { def: h.def, deps: { fetch: fakeFetch(calls2, () => ({ status: 200, body: { code: 2000, msg: `appToken ${WXP_TOKEN} 无效, uid ${WXP_UID}` } })), sleep: async () => undefined } }) as { ok: boolean; error: string };
    assert.equal(bad.ok, false);
    assert.doesNotMatch(bad.error, /AT_qhwTF1LMQPeSGCjXPsLLBhhhRaN2/);
    assert.doesNotMatch(bad.error, new RegExp(WXP_UID));
  } finally { clean(ctx); }
});

test("WxPusher SPT 模式:simple-push 地址配 SPT 令牌,不用 UID", async () => {
  const SPT = "SPT_zcrp4hdu4Bo2yzMab7rtpqvbXjf1";
  const SIMPLE = "https://wxpusher.zjiecode.com/api/send/message/simple-push";
  assert.equal(checkWebhookUrl("wxpusher", SIMPLE), SIMPLE);
  // 请求体:spt 字段,没有 uids
  const hook = { id: "t", kind: "wxpusher" as const, url: SIMPLE, secret: SPT, label: "", enabled: true };
  const body = webhookRequest(hook, "【测试】一行", new Date("2026-10-14T08:00:00Z")).body;
  assert.equal(body.spt, SPT);
  assert.equal("uids" in body, false);
  assert.equal(body.contentType, 1);
  // 添加:混着配(地址/凭据不成对)必须拒绝
  const ctx = tmpCtx();
  try {
    const h = harness();
    await assert.rejects(
      notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: WXP_URL, secret: SPT }, { def: h.def }),
      (e: unknown) => e instanceof ServiceError && /simple-push/.test(e.message),
    );
    await assert.rejects(
      notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: SIMPLE, secret: WXP_TOKEN }, { def: h.def }),
      (e: unknown) => e instanceof ServiceError && /appToken 要把地址填成/.test(e.message),
    );
    const ov = await notifyAction(ctx, "webhooks/add", { kind: "wxpusher", url: SIMPLE, secret: SPT, label: "SPT 推送" }, { def: h.def }) as ReturnType<typeof notifyOverview>;
    assert.equal(ov.settings.webhooks[0]!.kind, "wxpusher");
    assert.doesNotMatch(JSON.stringify(ov), /SPT_zcrp4hdu4Bo2yzMab7rtpqvbXjf1/); // SPT 令牌不回显
    // 发送成功
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    const out = await notifyAction(ctx, "test", { channel: ov.settings.webhooks[0]!.id }, { def: h.def, deps: { fetch: fakeFetch(calls, () => ({ status: 200, body: { code: 1000, msg: "处理成功" } })), sleep: async () => undefined } }) as { ok: boolean };
    assert.equal(out.ok, true);
    assert.equal(calls[0]!.url, SIMPLE);
    assert.equal(calls[0]!.body.spt, SPT);
  } finally { clean(ctx); }
});

import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import type { Socket } from "node:net";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import "../src/finance/register.ts";
import { DirectStageAgent, writtenPathsOf } from "../src/engines/direct_stage_agent.ts";
import type { Stage } from "../src/config.ts";
import { classifyResearchFailure } from "../src/research_failure.ts";

/**
 * **直连阶段执行器的故障矩阵**(双引擎方案 v2 第 5 步)。
 *
 * 按 v2 的要求:**先跑单阶段故障矩阵,不直接跑六阶段 happy path** ——
 * happy path 只能证明"顺利时能跑",而这一层真正的风险全在不顺利的时候:
 * 模型把参数写坏、工具报错、结果太大、轮数用尽、端点抽风。这些每一条处理错了,
 * 表现都是"研究莫名其妙没产出",而不是一条清楚的错误。
 *
 * 假的只有**模型端点**;工具是真的、文件系统是真的 —— 否则测不出接线问题。
 */

const STAGE = "profile" as Stage;

/** 能过收工检查的最小阶段产物(取数全失败 → incomplete + 三条 source_failed 缺口) */
const PASSING_PROFILE = {
  stage: STAGE, status: "incomplete", summary: "x", evidence_ids: [], calculation_ids: [],
  gaps: ["fetch_quote", "fetch_profile", "fetch_trade_calendar"].map((operation) => ({ operation, reason_code: "source_failed", detail: "x" })),
  quote_decision: "unknown_unverified", quote_decision_reason: "x", moat_tag: "待补",
};

/**
 * 建一个最小但真实的运行目录:受控工具靠 .vibe/hook-context.json 判当前阶段。
 * 默认**预置合格的阶段产物** —— 这组测试测的是工具循环本身;模型一收尾,产品内的收工检查就会查产物,
 * 不预置的话每条用例都会被拦下续问,请求次数全变。收工检查与循环的接线单独测(stageDone: false)。
 */
function makeRunDir({ stageDone = true }: { stageDone?: boolean } = {}): string {
  const runDir = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), "vra-direct-run-"));
  fs.mkdirSync(path.join(runDir, ".vibe"), { recursive: true });
  fs.mkdirSync(path.join(runDir, "fetch"), { recursive: true });
  fs.writeFileSync(path.join(runDir, ".vibe", "hook-context.json"), JSON.stringify({
    stage: STAGE, attempt: 1, run_id: "t1", repo_root: runDir, data_root: runDir, run_dir: runDir,
    python: "python3", scripts_rel: "scripts", forbidden_path_patterns: [], allowed_path_prefixes: [],
    written_at: new Date().toISOString(),
  }));
  fs.writeFileSync(path.join(runDir, "fetch", "quote.json"), JSON.stringify({ hello: "world" }));
  if (stageDone) {
    fs.mkdirSync(path.join(runDir, "stages"), { recursive: true });
    fs.writeFileSync(path.join(runDir, "stages", `${STAGE}.json`), JSON.stringify(PASSING_PROFILE));
  }
  return runDir;
}

/** 按脚本逐轮返回预设回复的假模型端点;记录每次收到的请求体 */
async function scriptedEndpoint(replies: unknown[]) {
  const seen: Record<string, unknown>[] = [];
  let i = 0;
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("end", () => {
      seen.push(JSON.parse(raw || "{}"));
      const body = replies[Math.min(i, replies.length - 1)];
      i += 1;
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as { port: number }).port;
  return { baseURL: `http://127.0.0.1:${port}/v1`, seen, close: () => new Promise<void>((r) => { server.close(() => r()); }) };
}

const toolCallReply = (name: string, args: string, id = "call_1") => ({
  choices: [{ message: { role: "assistant", content: null, tool_calls: [{ id, type: "function", function: { name, arguments: args } }] }, finish_reason: "tool_calls" }],
  usage: { total_tokens: 10 },
});
const textReply = (content: string) => ({ choices: [{ message: { role: "assistant", content }, finish_reason: "stop" }], usage: { total_tokens: 5 } });

function makeAgent(runDir: string, baseURL: string, extra: Record<string, unknown> = {}) {
  return new DirectStageAgent({
    runId: "t1",
    toolCtx: { runDir, repoRoot: runDir, python: "python3" },
    api: { baseURL, model: "m", structuredOutput: "prompt", verified: true },
    apiKey: "sk-test-abcdefghijklmnop",
    eventsPath: path.join(runDir, "events.jsonl"),
    requestTimeoutMs: 5_000,
    ...extra,
  });
}

test("正常一轮:调工具 → 拿结果 → 给最终回复", async () => {
  const runDir = makeRunDir();
  const ep = await scriptedEndpoint([toolCallReply("list_run_files", "{}"), textReply("共 1 个文件")]);
  try {
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "看看有什么文件");
    assert.equal(out.failed, null);
    assert.equal(out.finalResponse, "共 1 个文件");
    assert.equal(out.itemCount, 2, "两轮模型往返");
    assert.deepEqual(out.commands, [], "直连没有 shell,commands 必须是空的(塞工具名进去是假的审计等价)");
    assert.deepEqual(out.fileChanges, [], "读类工具不产生文件变更");
    assert.equal(out.threadId, null, "per_stage_session:没有跨阶段线程");
    // 第二次请求里必须带上 tool 角色的结果
    const second = ep.seen[1].messages as { role: string; content?: string }[];
    assert.equal(second.at(-1)?.role, "tool");
    assert.match(String(second.at(-1)?.content), /quote\.json/);
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("🔴 思考模式:每一轮的 reasoning_content 原样回传(DeepSeek 带 tools 时漏传直接 400)", async () => {
  const runDir = makeRunDir();
  const thinking = (reply: ReturnType<typeof toolCallReply>, text: string) => {
    const message = { ...reply.choices[0]!.message, reasoning_content: text };
    return { ...reply, choices: [{ ...reply.choices[0]!, message }] };
  };
  const ep = await scriptedEndpoint([
    thinking(toolCallReply("list_run_files", "{}"), "先看看有哪些文件"),
    thinking(toolCallReply("read_run_file", JSON.stringify({ path: "fetch/quote.json" }), "call_2"), "再读行情"),
    textReply("读完了"),
  ]);
  try {
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "看看文件");
    assert.equal(out.failed, null);
    const third = ep.seen[2]!.messages as { role: string; reasoning_content?: string }[];
    assert.deepEqual(third.filter((m) => m.role === "assistant").map((m) => m.reasoning_content), ["先看看有哪些文件", "再读行情"]);
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("🔴 模型把 arguments 写成非法 JSON:回喂 invalid_arguments,**不静默换成 {}**", async () => {
  const runDir = makeRunDir();
  const ep = await scriptedEndpoint([toolCallReply("list_run_files", "{不是JSON"), textReply("好的")]);
  try {
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "p");
    assert.equal(out.failed, null, "参数写坏不该让整轮失败 —— 要给模型改的机会");
    const second = ep.seen[1].messages as { role: string; content?: string }[];
    const toolMsg = second.at(-1);
    assert.equal(toolMsg?.role, "tool");
    assert.match(String(toolMsg?.content), /invalid_arguments/,
      "静默换成 {} 会让'模型传错参数'变成'工具行为异常',而且模型永远学不到自己错在哪");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("工具报错:回喂结构化错误,循环继续(不是整轮崩掉)", async () => {
  const runDir = makeRunDir();
  // 读一个不在白名单里的路径 → 实现层拒
  const ep = await scriptedEndpoint([toolCallReply("read_run_file", JSON.stringify({ path: "../../etc/passwd" })), textReply("换个方式")]);
  try {
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "p");
    assert.equal(out.failed, null);
    assert.equal(out.finalResponse, "换个方式");
    const toolMsg = (ep.seen[1].messages as { role: string; content?: string }[]).at(-1);
    assert.match(String(toolMsg?.content), /path_not_allowed/, "工具的失败原因要原样告诉模型");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("未知工具名:按 registry 拒绝,并把可用工具告诉模型", async () => {
  const runDir = makeRunDir();
  const ep = await scriptedEndpoint([toolCallReply("rm_rf", "{}"), textReply("抱歉")]);
  try {
    await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "p");
    const toolMsg = (ep.seen[1].messages as { role: string; content?: string }[]).at(-1);
    assert.match(String(toolMsg?.content), /unknown_tool/);
    assert.match(String(toolMsg?.content), /list_run_files/, "要列出可用工具,否则模型只能瞎猜");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("轮数用尽:最后一次请求**不带工具**,逼模型用现有材料收尾", async () => {
  const runDir = makeRunDir();
  // 每轮都要工具 → 撞上限
  const ep = await scriptedEndpoint([toolCallReply("list_run_files", "{}")]);
  try {
    const out = await makeAgent(runDir, ep.baseURL, { maxToolRounds: 2 }).runTurn(STAGE, 1, "p");
    // 2 轮工具 + 1 轮收尾
    assert.equal(ep.seen.length, 3);
    assert.ok(ep.seen[0].tools, "前两轮要带工具");
    assert.equal(ep.seen[2].tools, undefined, "最后一轮必须不带工具,否则它会继续要工具、永远收不了尾");
    assert.equal(out.failed, "模型给出了空回复", "收尾轮仍只给 tool_calls 而无内容 → 如实判失败");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("🔴 服务端 schema 模式:干活的几轮**不许带 response_format**,只在收尾那轮带", async () => {
  const runDir = makeRunDir();
  const ep = await scriptedEndpoint([
    toolCallReply("list_run_files", "{}"),
    textReply("材料够了"),                                   // 模型自然收尾
    textReply('{"stage_file_written":true,"status":"complete","notes":"ok"}'),   // 收尾轮的格式化汇报
  ]);
  try {
    const agent = new DirectStageAgent({
      runId: "t1", toolCtx: { runDir, repoRoot: runDir, python: "python3" },
      api: { baseURL: ep.baseURL, model: "m", structuredOutput: "server_schema", verified: true },
      apiKey: "sk-test-abcdefghijklmnop",
      eventsPath: path.join(runDir, "events.jsonl"), requestTimeoutMs: 5_000,
    });
    const out = await agent.runTurn(STAGE, 1, "p", { type: "object", properties: { status: { type: "string" } } });

    // 实测过的坑:response_format 与 tools 同时给,模型会直接吐 JSON、一个工具都不调,
    // 5 秒收工、阶段产物没写,而 finish_reason 还是 "stop"(看起来一切正常)。
    assert.equal(ep.seen[0].response_format, undefined, "第 1 轮(要调工具)不许带 response_format");
    assert.ok(ep.seen[0].tools, "第 1 轮必须带工具");
    assert.equal(ep.seen[1].response_format, undefined, "工具循环中的每一轮都不许带");
    // 收尾轮:带 schema、不带工具
    assert.ok(ep.seen[2].response_format, "收尾轮必须带 response_format,否则汇报格式没有任何约束");
    assert.equal(ep.seen[2].tools, undefined, "收尾轮不许再带工具");
    assert.equal(out.failed, null);
    assert.match(out.finalResponse, /stage_file_written/, "最终回复应当是收尾轮那份格式化汇报");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("提示词模式下模型已自然收尾:不再多花一次调用(schema 本就写在提示词里)", async () => {
  const runDir = makeRunDir();
  const ep = await scriptedEndpoint([textReply('{"stage_file_written":false,"status":"skipped","notes":"n"}')]);
  try {
    // api.structuredOutput = "prompt" → withOutputSchema 把 schema 写进提示词
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "p", { type: "object" });
    assert.equal(ep.seen.length, 1, "提示词模式 + 自然收尾 = 一次调用就够,不该再补一轮");
    assert.match(String((ep.seen[0].messages as { content: string }[])[0].content), /JSON Schema/, "schema 应当已写进提示词");
    assert.equal(out.failed, null);
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("端点失败:turn 判失败并带上原因,而不是抛异常炸穿编排器", async () => {
  const runDir = makeRunDir();
  const server = http.createServer((_q, s) => { s.writeHead(500); s.end('{"error":"boom"}'); });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as { port: number }).port;
  try {
    const out = await makeAgent(runDir, `http://127.0.0.1:${port}/v1`).runTurn(STAGE, 1, "p");
    assert.match(String(out.failed), /http_error/);
    assert.equal(out.finalResponse, "", "失败时不许编一个回复出来");
  } finally { await new Promise<void>((r) => { server.close(() => r()); }); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("用户取消:在途的模型请求立刻中止,本轮如实记为取消,不误报成超时", async () => {
  const runDir = makeRunDir();
  let received!: () => void;
  const arrived = new Promise<void>((r) => { received = r; });
  const sockets = new Set<Socket>();
  const server = http.createServer(() => { received(); });   // 收到请求后故意不回,模拟模型还在想
  server.on("connection", (s) => { sockets.add(s); s.on("close", () => sockets.delete(s)); });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as { port: number }).port;
  const ac = new AbortController();
  try {
    const turn = makeAgent(runDir, `http://127.0.0.1:${port}/v1`, { requestTimeoutMs: 60_000 }).runTurn(STAGE, 1, "p", undefined, ac.signal);
    await arrived;
    const t0 = Date.now();
    ac.abort(new Error("用户取消研究"));
    const out = await turn;
    // 信号没接到请求上的话,要等满请求超时(真实配置是 30 分钟)才停 —— 期间照样计费
    assert.ok(Date.now() - t0 < 2_000, "取消必须立刻中止在途请求");
    assert.match(String(out.failed), /^cancelled:/);
    assert.equal(classifyResearchFailure(String(out.failed)), null, "取消不是执行故障,不许被归成超时 / 限流");
    assert.equal(out.finalResponse, "");
  } finally {
    for (const s of sockets) s.destroy();
    await new Promise<void>((r) => { server.close(() => r()); });
    fs.rmSync(runDir, { recursive: true, force: true });
  }
});

test("空回复要判失败(空字符串不是产出)", async () => {
  const runDir = makeRunDir();
  const ep = await scriptedEndpoint([textReply("   ")]);
  try {
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "p");
    assert.equal(out.failed, "模型给出了空回复");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("收工检查:缺产物时模型想收工 → 问题清单在同一段对话里交回;拦下后补上产物 → 放行", async () => {
  const runDir = makeRunDir({ stageDone: false });
  const ep = await scriptedEndpoint([
    textReply("我做完了"),                                                                 // 想收工,但没写产物
    toolCallReply("write_stage", JSON.stringify({ stage_output: PASSING_PROFILE })),       // 被拦后补写
    textReply("补好了"),
  ]);
  try {
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "p");
    assert.equal(ep.seen.length, 3);
    const afterBlock = ep.seen[1].messages as { role: string; content?: string }[];
    assert.equal(afterBlock.at(-1)?.role, "user", "拦截意见要作为下一条用户消息交回,而不是悄悄结束");
    assert.match(String(afterBlock.at(-1)?.content), /【收工检查】[\s\S]*缺产物:stages\/profile\.json/);
    assert.equal(out.failed, null);
    assert.equal(out.finalResponse, "补好了");
    assert.deepEqual(out.stopGuard, { blocks: 1, terminated: null });
    assert.ok(fs.existsSync(path.join(runDir, "stages", "profile.json")), "补写必须真的落盘(受控工具是真的)");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("收工检查:一直不补产物 → 按上限终止本轮,原因带统一前缀,不再要汇报", async () => {
  const runDir = makeRunDir({ stageDone: false });
  const ep = await scriptedEndpoint([textReply("我做完了")]);   // 每轮都只想收工
  try {
    const out = await makeAgent(runDir, ep.baseURL, { stopLimits: { maxIdle: 2, maxTotal: 5 } })
      .runTurn(STAGE, 1, "p", { type: "object" });
    assert.equal(ep.seen.length, 3, "1 次想收工 + 2 次被拦后的续问;第 3 次空转即终止");
    assert.equal(out.stopGuard?.blocks, 2);
    assert.match(String(out.stopGuard?.terminated), /^收工检查终止本轮/, "编排器按这个前缀把本轮判为失败并补跑");
    const types = fs.readFileSync(path.join(runDir, "events.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l).type);
    assert.equal(types.filter((t) => t === "stop_guard.block").length, 2);
    assert.ok(types.includes("stop_guard.terminate"));
    assert.ok(!types.includes("direct.finalized"), "终止后不该再花一次调用要格式化汇报");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("事件流:关键节点都要落盘,且摘要随之变化(审计账本)", async () => {
  const runDir = makeRunDir();
  const ep = await scriptedEndpoint([toolCallReply("list_run_files", "{}"), textReply("done")]);
  try {
    const agent = makeAgent(runDir, ep.baseURL);
    const before = agent.eventsDigest();
    await agent.runTurn(STAGE, 1, "p");
    assert.notEqual(agent.eventsDigest(), before, "写了事件,摘要必须变");
    const lines = fs.readFileSync(path.join(runDir, "events.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));
    const types = lines.map((l) => l.type);
    for (const t of ["direct.turn_start", "direct.model_reply", "direct.tool_ok", "direct.turn_end"]) {
      assert.ok(types.includes(t), `事件流里缺 ${t}:${types.join(", ")}`);
    }
    assert.ok(lines.every((l) => typeof l.seq === "number" && l.run_id === "t1"), "每条事件都要带 seq 与 run_id");
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

test("🔴 fileChanges 必须是绝对路径,且落在运行目录内(这是与 validator 的契约)", () => {
  const runDir = "/tmp/some-run";
  // validator 用 path.resolve(f) 判越界,而 path.resolve 是**相对当前工作目录**解析的。
  // 给相对路径的话,`stages/profile.json` 会被解析成 <cwd>/stages/profile.json,
  // 于是合法产物被判成"写入了运行目录之外的文件",阶段直接失败。
  // 🔴 2026-09-04 真踩:9 条单元测试全绿,只有真跑一次才暴露 —— 当时单测只断言了本函数的
  //    返回值长什么样,没断言它与 validator 之间的这条契约。
  const cases = [
    writtenPathsOf(runDir, "calculate", { output_file: "01_x.json" }, {}),
    writtenPathsOf(runDir, "write_stage", {}, { written: "stages/profile.json" }),
    writtenPathsOf(runDir, "write_report", {}, { written: ["report.md", "stages/report.json"] }),
  ].flat();
  assert.ok(cases.length >= 4, "样例太少,下面的断言会变成空转");
  for (const p of cases) {
    assert.ok(path.isAbsolute(p), `不是绝对路径:${p}`);
    assert.ok(p === runDir || p.startsWith(runDir + path.sep), `落在运行目录之外:${p}`);
  }
  assert.deepEqual(writtenPathsOf(runDir, "calculate", { output_file: "01_x.json" }, {}), [path.join(runDir, "calcs", "01_x.json")]);
  assert.deepEqual(writtenPathsOf(runDir, "list_run_files", {}, {}), [], "读类工具不产生变更");
  // calculate 内部还会写 .vibe/calc-owners.json —— 那是簿记不是 agent 产物。
  // 一旦混进来,validator 会判"agent 改写了受保护的编排产物",于是每次计算都变成违规。
  assert.ok(!cases.some((p) => p.includes(".vibe")), "内部簿记混进了 fileChanges");
});

test("🔴 真跑一次工具:fileChanges 里的路径要能直接过 validator 的越界判定", async () => {
  const runDir = makeRunDir();
  // 让模型调一次真实的写类工具,再拿实际产出的 fileChanges 做 validator 同款判定。
  // 上一条测的是纯函数,这一条测的是**接线** —— 真 bug 就出在这两者之间。
  const stageOutput = { stage: STAGE, evidence_ids: [], calculation_ids: [], gaps: [], findings: [] };
  const ep = await scriptedEndpoint([
    toolCallReply("write_stage", JSON.stringify({ stage_output: stageOutput })),
    textReply("写好了"),
  ]);
  try {
    const out = await makeAgent(runDir, ep.baseURL).runTurn(STAGE, 1, "p");
    // write_stage 可能因 schema 不合被拒——那没关系,这条测的是"若写成了,路径形态对不对"
    for (const f of out.fileChanges) {
      const resolved = path.resolve(f);   // ← validator 就是这么做的
      assert.ok(resolved === path.resolve(runDir) || resolved.startsWith(path.resolve(runDir) + path.sep),
        `validator 会把它判成运行目录之外的文件:${f}`);
    }
  } finally { await ep.close(); fs.rmSync(runDir, { recursive: true, force: true }); }
});

// 不量墙钟(Codex 复审 R4-4):若先读响应体,要等 10 秒请求超时、失败原因变成 timeout,下面的 redirect 断言会红。
test("端点返回跳转(响应体还挂着):本轮立刻失败,带上结构化故障码交给编排,不重发、不跟随", { timeout: 60_000 }, async (t) => {
  const runDir = makeRunDir();
  let hits = 0;
  const sockets = new Set<Socket>();
  const server = http.createServer((_req, res) => {
    hits += 1;
    res.writeHead(307, { location: "http://127.0.0.1:9/v1/chat/completions", "content-length": "1000" });
    res.write("x");   // 故意不写完
  });
  server.on("connection", (s) => { sockets.add(s); s.on("close", () => sockets.delete(s)); });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as { port: number }).port;
  // 清理挂在 t.after、不放 finally:整体超时只把测试判失败,不会打断下面的 await,finally 根本不执行,
  // 还在监听的 server 会让测试进程一直挂着(Codex 复审 R5-1,Node 26 实测)
  t.after(async () => {
    for (const s of sockets) s.destroy();
    await new Promise<void>((r) => { server.close(() => r()); });
    fs.rmSync(runDir, { recursive: true, force: true });
  });
  const out = await makeAgent(runDir, `http://127.0.0.1:${port}/v1`, { requestTimeoutMs: 10_000 }).runTurn(STAGE, 1, "p");
  assert.match(String(out.failed), /^redirect:/);
  assert.equal(out.failureCode, "endpoint_redirect", "编排靠这个码整次停下;只留文字的话分类器认不出来,会一直补跑");
  assert.equal(hits, 1);
  assert.equal(out.finalResponse, "");
});

test("vibe-astock #30:risk 阶段没取筹码时,提示词要求写成取数范围不含,而不是缺数据", () => {
  const stages = fs.readFileSync(new URL("../src/finance/stages.ts", import.meta.url), "utf8");
  const line = stages.split("\n").find((l) => l.includes("chip_distribution") && l.includes("bs_kline_qfq.json")) ?? "";
  assert.ok(line.includes("本次取数范围不含筹码数据"), "给出固定说法");
  assert.ok(line.includes("不是取数失败"), "说明文件不存在不等于取数失败");
});

import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import "../src/finance/register.ts";
import { detectPython } from "../src/init.ts";
import { addReport } from "../src/report_library.ts";
import { resumeUnifiedTask, runUnifiedTask } from "../src/task_service.ts";
import type { DeepResearchBackend } from "../src/engines/deep_engine.ts";
import { FinanceDeepTargetResolver } from "../src/finance/deep_target.ts";
import { ServiceError, type ServiceContext } from "../src/service.ts";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "vra-task-service-"));
const PYTHON = process.env.VRA_PYTHON?.trim() || detectPython(REPO) || detectPython(path.join(REPO, ".."))
  || (process.platform === "win32" ? "python" : "python3");
const ctx = (dataRoot: string): ServiceContext => ({ repoRoot: REPO, dataRoot,
  python: PYTHON, node: process.execPath });
const task = (id: string, requestedMode: "auto" | "quick" | "deep" = "auto") => ({
  schemaVersion: 1, id: "task-api-1", kind: "locate_passages", requestedMode,
  objective: "定位收入变化的原文", evidenceScope: "existing", workflow: "single_step",
  inputRefs: [{ kind: "report", id }], outputFormat: "text", operation: null,
});
/** v2 没有默认大脑:要用模型的路线显式带一份合成来源(地址不可达,本文件不会真的发请求) */
const SYNTH_LLM = { provider: "custom", apiKey: "secret-test-key", baseURL: "https://example.invalid/v1", model: "fixture" };

test("统一任务入口用真实研报适配器路由并执行 Quick，响应不回显密钥", async () => {
  const dataRoot = tmp();
  const rec = await addReport(dataRoot, { name: "收入.md", content: Buffer.from("收入同比增长。", "utf8").toString("base64") });
  const routed = await runUnifiedTask(ctx(dataRoot), { task: task(rec.id), execute: false, llm: SYNTH_LLM });
  const result = await runUnifiedTask(ctx(dataRoot), {
    task: task(rec.id), execute: true, expectedRouteFingerprint: routed.route.routeFingerprint, llm: SYNTH_LLM,
  }, undefined, {
    quickProvider: { name: "fixture", baseURL: "https://example.invalid/v1", apiKey: "secret-test-key", model: "fixture", structuredOutput: "prompt" },
    complete: async (request) => {
      const payload = JSON.parse(String(request.messages[1]?.content));
      const excerpt = payload.materials[0].excerpts[0];
      return { message: { role: "assistant", content: JSON.stringify({ selections: [{
        kind: payload.materials[0].kind, id: payload.materials[0].id, revision: payload.materials[0].revision,
        excerptId: excerpt.excerptId,
      }] }) }, finishReason: "stop", usage: { total_tokens: 12 }, durationMs: 3 };
    },
  });
  assert.equal(result.status, "completed");
  assert.equal(result.route.target, "quick");
  assert.deepEqual(result.events.map((event) => event.type), ["started", "artifact", "completed"]);
  assert.equal(JSON.stringify(result).includes("secret-test-key"), false);
});

test("Agent 模式的 Quick 走模型 API:端点返回跳转时提示改 Base URL,不笼统报「Quick 执行失败」", async () => {
  // 不注入 complete:要走真实的 Agent 适配器 → 对话层 → 传输层,跳转原因才会在中间被吞掉(Codex 复审 R4-2)
  const dataRoot = tmp();
  const rec = await addReport(dataRoot, { name: "收入.md", content: Buffer.from("收入同比增长。", "utf8").toString("base64") });
  let hits = 0;
  const server = http.createServer((_req, res) => { hits += 1; res.writeHead(302, { location: "/elsewhere/chat/completions" }); res.end(); });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const llm = { ...SYNTH_LLM, baseURL: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1` };
  try {
    const routed = await runUnifiedTask(ctx(dataRoot), { task: task(rec.id, "quick"), execute: false, executionMode: "agent", llm });
    assert.equal(routed.route.target, "quick");
    const result = await runUnifiedTask(ctx(dataRoot), { task: task(rec.id, "quick"), execute: true, executionMode: "agent",
      expectedRouteFingerprint: routed.route.routeFingerprint, llm });
    assert.equal(result.status, "failed");
    const failed = result.events.at(-1)?.payload;
    assert.equal(failed?.code, "redirect");
    assert.match(String(failed?.message), /跳转/);
    assert.match(String(failed?.message), /Base URL/);
    assert.equal(hits, 1, "跳转不跟随、不重发");
    assert.equal(JSON.stringify(result).includes("secret-test-key"), false);
  } finally { await new Promise<void>((r) => { server.close(() => r()); }); }
});

test("Deep 在 M3 通过统一入口启动现有六阶段流程，并可按指纹恢复状态", async () => {
  const dataRoot = tmp();
  const rec = await addReport(dataRoot, { name: "300308-深研.md", content: Buffer.from("公司代码 300308。正文。", "utf8").toString("base64") });
  const deepTask = { ...task(rec.id, "deep"), kind: "deep_research", evidenceScope: "open_discovery",
    workflow: "multi_step", outputFormat: "document", inputRefs: [{ kind: "report", id: rec.id }, { kind: "entity", id: "300308" }] };
  let runId = "";
  let terminal = false;
  const deepBackend: DeepResearchBackend = {
    start(request) { runId = request.run_id!; return { run_id: runId, run_dir: `runs/${runId}`, log: `logs/${runId}.log`, pid: 3 }; },
    status() { return { run_id: runId, exists: true, status: terminal ? "complete" : "running", exit_code: terminal ? 0 : null,
      stages: terminal ? [{ stage: "profile", status: "complete", attempts: 1 }] : [], evidence_count: terminal ? 9 : 0,
      calculation_count: terminal ? 2 : 0, finished_at: terminal ? "2026-09-04T12:00:00Z" : null,
      last_events: [], report: terminal, viewer: terminal ? `runs/${runId}/viewer.html` : null }; },
    report() { return { run_id: runId, report: "# 完成", appendix: null, availability: "ready", run_status: "complete" }; },
  };
  const deepDeps = { deepBackend, deepTargetResolver: new FinanceDeepTargetResolver(dataRoot) };
  // 请求不带 llm:用后台默认(数据根 config.json 选中的本机 Claude 订阅)
  fs.writeFileSync(path.join(dataRoot, "config.json"), JSON.stringify({ provider: { profile: "cli-claude" } }));
  const routed = await runUnifiedTask(ctx(dataRoot), { task: deepTask, execute: false }, undefined, deepDeps);
  assert.equal(routed.status, "routed");
  assert.equal(routed.route.engineFamily, "local_agent", "后台默认是本机订阅,路由要如实标出");
  assert.equal(routed.executionAvailable, true);
  const started = await runUnifiedTask(ctx(dataRoot), { task: deepTask, execute: true,
    expectedRouteFingerprint: routed.route.routeFingerprint }, undefined, deepDeps);
  assert.equal(started.status, "running");
  assert.equal(started.events.at(-1)?.type, "started");
  terminal = true;
  const resumed = await resumeUnifiedTask(ctx(dataRoot), {
    runId, routeFingerprint: routed.route.routeFingerprint,
  }, undefined, deepDeps);
  assert.equal(resumed.status, "completed");
  assert.deepEqual(resumed.events.map((event) => event.type), ["progress", "artifact", "completed"]);
});

test("接口传 direct 时可以先看见 Deep 路由，但不能执行，明确报 agent_required", async () => {
  const dataRoot = tmp();
  const rec = await addReport(dataRoot, { name: "300308-深研.md", content: Buffer.from("公司代码 300308。", "utf8").toString("base64") });
  const deepTask = { ...task(rec.id, "deep"), kind: "deep_research", evidenceScope: "open_discovery",
    workflow: "multi_step", outputFormat: "document", inputRefs: [{ kind: "report", id: rec.id }, { kind: "entity", id: "300308" }] };
  const deps = { deepTargetResolver: new FinanceDeepTargetResolver(dataRoot) };
  const llm = { provider: "deepseek", apiKey: "test-key", model: "deepseek-v4" };
  const routed = await runUnifiedTask(ctx(dataRoot), {
    task: deepTask, execute: false, executionMode: "direct", llm,
  }, undefined, deps);
  assert.equal(routed.route.target, "deep");
  assert.equal(routed.executionAvailable, false);
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), {
    task: deepTask, execute: true, executionMode: "direct", expectedRouteFingerprint: routed.route.routeFingerprint,
    llm,
  }, undefined, deps), (error: unknown) => error instanceof ServiceError && error.code === "agent_required");
});

test("任务路由绑定 AI 来源，Claude / WorkBuddy 订阅可启动六阶段", async () => {
  const dataRoot = tmp();
  const rec = await addReport(dataRoot, { name: "300308-深研.md", content: Buffer.from("公司代码 300308。", "utf8").toString("base64") });
  const deepTask = { ...task(rec.id, "deep"), kind: "deep_research", evidenceScope: "open_discovery",
    workflow: "multi_step", outputFormat: "document" };
  const deps = { deepTargetResolver: new FinanceDeepTargetResolver(dataRoot) };
  const claude = { provider: "cli-claude", model: "claude-subscription" };
  const routed = await runUnifiedTask(ctx(dataRoot), {
    task: deepTask, execute: false, executionMode: "agent", llm: claude,
  }, undefined, deps);
  assert.equal(routed.route.target, "deep");
  assert.equal(routed.executionAvailable, true, "Claude 已有受控 MCP 六阶段适配器");
  assert.equal(routed.route.engineFamily, "local_agent", "Claude 订阅如实标成本机订阅 Agent,不能冒充模型 API 工具循环");

  const codebuddyRouted = await runUnifiedTask(ctx(dataRoot), {
    task: deepTask, execute: false, executionMode: "agent", llm: { provider: "cli-codebuddy" },
  }, undefined, deps);
  assert.equal(codebuddyRouted.executionAvailable, true, "CodeBuddy 已有受控 MCP 六阶段适配器");
  assert.equal(codebuddyRouted.route.engineFamily, "local_agent", "WorkBuddy 订阅如实标成本机订阅 Agent,不能冒充模型 API 工具循环");

  let startedRuntime: unknown = null;
  const backend: DeepResearchBackend = {
    start(_request, internal) { startedRuntime = internal?.runtimeLlm; return { run_id: "task-local-agent", run_dir: "runs/task-local-agent", log: "logs/task-local-agent.log", pid: 7 }; },
    status() { throw new Error("not used"); }, report() { throw new Error("not used"); },
  };
  const started = await runUnifiedTask(ctx(dataRoot), {
    task: deepTask, execute: true, executionMode: "agent", llm: { provider: "cli-codebuddy" },
    expectedRouteFingerprint: codebuddyRouted.route.routeFingerprint,
  }, undefined, { ...deps, deepBackend: backend });
  assert.equal(started.events[0]?.payload?.executor, "local-agent-deep-v1");
  assert.deepEqual(startedRuntime, { provider: "cli-codebuddy" });

  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), {
    task: deepTask, execute: true, executionMode: "agent", llm: { provider: "cli-codex", model: "codex-subscription" },
    expectedRouteFingerprint: routed.route.routeFingerprint,
  }, undefined, deps), (error: unknown) => error instanceof ServiceError && error.code === "route_changed");
});

test("确定性计算由统一任务入口真实执行，不把成功路由冒充成已处理", async () => {
  const dataRoot = tmp();
  const calcTask = {
    schemaVersion: 1, id: "task-calc-1", kind: "calculate", requestedMode: "auto",
    objective: "计算前瞻倍数", evidenceScope: "existing", workflow: "single_step", inputRefs: [],
    outputFormat: "data", operation: { kind: "calculate", functionId: "forward_pe",
      args: { price: 100, eps_forecast: 5 } },
  };
  const routed = await runUnifiedTask(ctx(dataRoot), { task: calcTask, execute: false });
  const result = await runUnifiedTask(ctx(dataRoot), {
    task: calcTask, execute: true, expectedRouteFingerprint: routed.route.routeFingerprint,
  });
  assert.equal(result.status, "completed");
  assert.equal(result.executionAvailable, true);
  assert.equal(result.route.target, "deterministic");
  assert.deepEqual(result.events.map((event) => event.type), ["started", "artifact", "completed"]);
  const artifact = result.events.find((event) => event.type === "artifact");
  assert.equal(((artifact?.payload?.result as { output?: { value?: unknown } })?.output?.value), 20);
});

test("execute=false 只完成确定性路由；已取消的请求不会进入计算", async () => {
  const dataRoot = tmp();
  const calcTask = {
    schemaVersion: 1, id: "task-calc-route", kind: "calculate", requestedMode: "auto",
    objective: "只判断路径", evidenceScope: "existing", workflow: "single_step", inputRefs: [],
    outputFormat: "data", operation: { kind: "calculate", functionId: "forward_pe",
      args: { price: 100, eps_forecast: 5 } },
  };
  const routed = await runUnifiedTask(ctx(dataRoot), { task: calcTask, execute: false });
  assert.equal(routed.status, "routed");
  assert.deepEqual(routed.events, []);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), {
    task: calcTask, execute: true, expectedRouteFingerprint: routed.route.routeFingerprint,
  }, controller.signal),
    (error: unknown) => error instanceof ServiceError && error.code === "cancelled");
});

test("两段式执行必须与第一次路由指纹一致，不一致时拒绝而不是换路线", async () => {
  const dataRoot = tmp();
  const rec = await addReport(dataRoot, { name: "会变化.md", content: Buffer.from("原始正文。", "utf8").toString("base64") });
  const routed = await runUnifiedTask(ctx(dataRoot), { task: task(rec.id), execute: false, llm: SYNTH_LLM });
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), {
    task: task(rec.id), execute: true, expectedRouteFingerprint: "f".repeat(64),
    llm: { provider: "cli-claude" },
  }), (error: unknown) => error instanceof ServiceError && error.code === "route_changed");
  assert.match(routed.route.routeFingerprint, /^[a-f0-9]{64}$/);
});

test("统一任务请求拒绝注入依赖、非法 llm 与契约外字段", async () => {
  const dataRoot = tmp();
  for (const request of [
    { task: {}, materials: {} },
    { task: {}, execute: "yes" },
    { task: {}, expectedRouteFingerprint: "short" },
    { task: task("0".repeat(32)), execute: false, llm: { provider: "mimo", apiKey: 42 } },
    { task: task("0".repeat(32)), llm: { provider: "mimo", apiKey: "x", resolver: "evil" } },
  ]) {
    await assert.rejects(() => runUnifiedTask(ctx(dataRoot), request),
      (error: unknown) => error instanceof ServiceError && error.code === "invalid_task_request");
  }
  const rec = await addReport(dataRoot, { name: "两阶段.md", content: Buffer.from("正文。", "utf8").toString("base64") });
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), { task: task(rec.id), execute: true }),
    (error: unknown) => error instanceof ServiceError && error.code === "invalid_task_request");
});

test("没选 AI 来源:确定性任务照常路由执行;要用模型的路线如实报来源错误,不替用户编一个默认大脑", async () => {
  const dataRoot = tmp();
  const calcTask = {
    schemaVersion: 1, id: "task-calc-nosrc", kind: "calculate", requestedMode: "auto",
    objective: "计算前瞻倍数", evidenceScope: "existing", workflow: "single_step", inputRefs: [],
    outputFormat: "data", operation: { kind: "calculate", functionId: "forward_pe", args: { price: 100, eps_forecast: 5 } },
  };
  const rec = await addReport(dataRoot, { name: "无来源.md", content: Buffer.from("收入同比增长。", "utf8").toString("base64") });
  const runCalc = async () => {
    const routed = await runUnifiedTask(ctx(dataRoot), { task: calcTask, execute: false });
    assert.equal(routed.route.target, "deterministic");
    const done = await runUnifiedTask(ctx(dataRoot), { task: calcTask, execute: true, expectedRouteFingerprint: routed.route.routeFingerprint });
    assert.equal(done.status, "completed");
    return routed.route.routeFingerprint;
  };
  const isCode = (code: string) => (error: unknown) => error instanceof ServiceError && error.code === code;

  // ① 什么都没配
  const fpNone = await runCalc();
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), { task: task(rec.id), execute: false }), isCode("ai_source_not_configured"));

  // ② 后台默认配了但配坏了(模板要的环境变量没设):确定性任务不受影响,要用模型的路线报真实原因
  delete process.env.VRA_TEST_TASK_UNSET_KEY;
  fs.mkdirSync(path.join(dataRoot, "providers"), { recursive: true });
  fs.writeFileSync(path.join(dataRoot, "providers", "fixture-broken.json"), JSON.stringify({ id: "fixture-broken", name: "fixture", base_url: "https://example.invalid/v1", env_key: "VRA_TEST_TASK_UNSET_KEY", default_model: "fixture" }));
  fs.writeFileSync(path.join(dataRoot, "config.json"), JSON.stringify({ provider: { profile: "fixture-broken" } }));
  assert.equal(await runCalc(), fpNone, "来源都不可用时占位指纹一致,两段式照常衔接");
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), { task: task(rec.id), execute: false }), isCode("missing_key"));

  // ③ 请求里显式带的来源解析失败 → 照常报错,哪怕任务本身是确定性的(不吞掉调用方给错的配置)
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), { task: calcTask, execute: false, llm: { provider: "cli-unknown" } }), isCode("unsupported_cli"));

  // ④ 来源从无到有:路由指纹随之改变 ⇒ 旧路由不能拿来执行
  fs.writeFileSync(path.join(dataRoot, "config.json"), JSON.stringify({ provider: { profile: "cli-claude" } }));
  const routedWithSource = await runUnifiedTask(ctx(dataRoot), { task: calcTask, execute: false });
  assert.notEqual(routedWithSource.route.routeFingerprint, fpNone);
  await assert.rejects(() => runUnifiedTask(ctx(dataRoot), { task: calcTask, execute: true, expectedRouteFingerprint: fpNone }), isCode("route_changed"));
});

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import "../src/finance/register.ts";
import { detectPython } from "../src/init.ts";
import { currentPlugin } from "../src/plugin.ts";
import { ServiceError, assertArgs, blockStatusFromEnvelope, pageQuery, type ServiceContext } from "../src/service.ts";
import type { EndpointDef } from "../src/registry.ts";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
/** 解释器:VRA_PYTHON → 仓库 .venv → 上一级 .venv(开发布局)→ PATH 上的 python3;不写死任何机器的绝对路径 */
const PY = process.env.VRA_PYTHON?.trim() || detectPython(REPO) || detectPython(path.join(REPO, ".."))
  || (process.platform === "win32" ? "python" : "python3");
/**
 * 上下文真正会注入哪些键 —— **从垂类的 pageContext.resolve 真实产出里取**,
 * 不在测试里手写一份(手写的那份迟早与实现漂移,而漂移的表现是"棘轮全绿但没查到东西")。
 */
const INJECTED_KEYS: string[] = (() => {
  const pc = currentPlugin().pageContext;
  if (!pc) return [];
  // 用一个形状合法的假信封走一遍真实的 resolve
  const probe = pc.resolve({
    status: "ok",
    evidence: [
      { id: "ev-1", field: "last_trading_day", value: "2026-08-27", unit: "日期", period: "2026-08-27", raw_ref: null, source: "test" },
      { id: "ev-2", field: "previous_trading_day", value: "2026-08-26", unit: "日期", period: "2026-08-26", raw_ref: null, source: "test" },
      { id: "ev-3", field: "is_today_trading_day", value: 1, unit: "布尔", period: "2026-08-27", raw_ref: null, source: "test" },
      { id: "ev-4", field: "session_phase", value: "post_close", unit: "枚举", period: "2026-08-27", raw_ref: null, source: "test" },
    ],
  } as never);
  return probe ? Object.keys(probe.inject) : [];
})();

const ctx = (): ServiceContext =>
  ({ repoRoot: REPO, dataRoot: fs.mkdtempSync(path.join(os.tmpdir(), "vra-page-")), python: process.env.VRA_PYTHON ?? "python3", node: process.execPath }) as ServiceContext;

test("🔴 页面按名字要数据,端点 id 只活在垂类声明里(界面上不该印出端点名)", () => {
  const qs = currentPlugin().pageQueries ?? {};
  assert.ok(Object.keys(qs).length >= 3, "至少声明了几屏");
  for (const [name, def] of Object.entries(qs)) {
    assert.ok(def.title && def.intent, `${name} 要有标题与"在回答什么"`);
    assert.ok(def.blocks.length > 0, name);
    for (const b of def.blocks) {
      assert.match(b.id, /^[a-z][a-z0-9_]*$/, `${name}.${b.id} 块 id 要是稳定标识`);
      assert.ok(b.title, `${name}.${b.id} 要有给人看的标题`);
      assert.ok(b.endpoint, `${name}.${b.id} 要指明端点`);
    }
    // 块 id 在一屏内唯一 —— 撞了前端会拿错块
    const ids = def.blocks.map((b) => b.id);
    assert.equal(new Set(ids).size, ids.length, `${name} 的块 id 有重复:${ids.join(",")}`);
  }
});

test("🔴 声明里引用的端点必须真的存在于注册表(否则整块永远 missing 且只有跑起来才知道)", () => {
  const reg = JSON.parse(fs.readFileSync(path.join(REPO, "datasources", "registry.json"), "utf8")) as { endpoints: { id: string }[] };
  const known = new Set(reg.endpoints.map((e) => e.id));
  for (const [name, def] of Object.entries(currentPlugin().pageQueries ?? {})) {
    for (const b of def.blocks) assert.ok(known.has(b.endpoint), `${name}.${b.id} 引用了不存在的端点 ${b.endpoint}`);
  }
  const pc = currentPlugin().pageContext;
  if (pc) assert.ok(known.has(pc.endpoint), `pageContext 引用了不存在的端点 ${pc.endpoint}`);
});

test("页面查询声明不能暗中绑定某一只示例标的", () => {
  // 例外只有**指数层**端点:一屏市场概览要的就是某条指数(如沪深300 的估值),那是市场本身,不是示例标的
  const reg = JSON.parse(fs.readFileSync(path.join(REPO, "datasources", "registry.json"), "utf8")) as { endpoints: { id: string; layer?: string }[] };
  const layerOf = new Map(reg.endpoints.map((e) => [e.id, String(e.layer ?? "")]));
  for (const [name, def] of Object.entries(currentPlugin().pageQueries ?? {})) {
    for (const block of def.blocks) {
      if (block.symbol !== undefined && /指数/.test(layerOf.get(block.endpoint) ?? "")) continue;
      assert.equal(block.symbol, undefined, `${name}.${block.id} 不应硬编码示例标的 ${block.symbol}`);
    }
  }
});

test("备用端点也必须存在于注册表,且不吃上下文(否则备源按自己的默认值给数,页面看不出不是那一天)", () => {
  const reg = JSON.parse(fs.readFileSync(path.join(REPO, "datasources", "registry.json"), "utf8")) as { endpoints: { id: string; symbol_kind?: string }[] };
  const byId = new Map(reg.endpoints.map((e) => [e.id, e]));
  let n = 0;
  for (const [name, def] of Object.entries(currentPlugin().pageQueries ?? {})) {
    for (const b of def.blocks) {
      if (!b.fallback) continue;
      n += 1;
      const fb = byId.get(b.fallback.endpoint);
      assert.ok(fb, `${name}.${b.id} 的备用端点 ${b.fallback.endpoint} 不在注册表`);
      assert.equal(fb.symbol_kind, "none", `${name}.${b.id} 的备用端点要是全市场端点(页面不给它主体)`);
      assert.ok(!b.injectContext && !b.userArgs?.length, `${name}.${b.id} 有备源就不能吃上下文 / 用户参数`);
      assert.ok(b.fallback.note.length >= 10, `${name}.${b.id} 备源要写清口径差在哪`);
    }
  }
  assert.ok(n > 0, "一个备源都没核到");
});

test("🔴 上下文注入是**按块**的:不吃那个参数的端点不许被硬塞(第一版就是这么把一屏全弄 missing 的)", () => {
  // 🔴 用**代码真正用的那把尺子**(assertArgs),不要自己按注册表的 `args` 推 ——
  //    那个字段是**默认值**不是**允许集**(允许集还含 GLOBAL_ARG_KEYS)。
  //    我第一版就是拿默认值当允许集,于是测试报了一条根本不存在的错。同一个不变量只能有一种判法。
  const reg = JSON.parse(fs.readFileSync(path.join(REPO, "datasources", "registry.json"), "utf8")) as { endpoints: EndpointDef[] };
  const byId = new Map(reg.endpoints.map((e) => [e.id, e]));
  // 🔴 要按块的 injectAs **改名之后**再过白名单:曾经这里直接塞 `date`,于是趋势块改名成 `end` 后
  //    注册表没声明 end、运行时整块 bad_args,这条测试却照样绿。
  for (const [name, def] of Object.entries(currentPlugin().pageQueries ?? {})) {
    for (const b of def.blocks) {
      if (!b.injectContext) continue;
      const ep = byId.get(b.endpoint);
      assert.ok(ep, `${name}.${b.id}:注册表里没有 ${b.endpoint}`);
      const injected = Object.fromEntries(Object.values(b.injectAs ?? {}).map((to) => [to, "2026-08-25"]));
      assert.ok(Object.keys(injected).length > 0, `${name}.${b.id}:吃上下文却没声明 injectAs`);
      assert.doesNotThrow(
        () => assertArgs(ep, { ...(b.args ?? {}), ...injected }),
        `${name}.${b.id} 声明了 injectContext,但 ${b.endpoint} 不接受注入的参数`,
      );
    }
  }
});

test("未知查询名当场报错,并列出可用的", async () => {
  await assert.rejects(
    () => pageQuery(ctx(), { query: "nosuch" }),
    (e: unknown) => e instanceof ServiceError && e.code === "unknown_query" && /可用:/.test(e.message),
  );
});

test("🔴 块状态跟着信封走:信封 failed / partial 不许被记成 ok(它曾一律记 ok —— 一个证据 0 条、带 traceback 的块在界面上显示成正常,而按 status 做的缺口保护永远不触发)", () => {
  const cases: [unknown, string][] = [
    ["ok", "ok"],
    ["partial", "partial"],
    ["failed", "failed"],
    [undefined, "failed"],   // 信封没说 → 保守当失败,不许当正常
    ["weird", "failed"],     // 没见过的值同理
    [null, "failed"],
  ];
  for (const [es, want] of cases)
    assert.equal(blockStatusFromEnvelope({ status: es }), want, `信封 ${String(es)} → 应为 ${want}`);
  assert.equal(blockStatusFromEnvelope(null), "failed", "连信封都没有 → failed");
  assert.equal(blockStatusFromEnvelope(undefined), "failed");
});

test("🔴 传给端点的参数键必须**函数真的收得下** —— 白名单放行 ≠ 收得下(全局键 limit/date 对任何端点都放行,而全市场龙虎榜收的是 trade_date:整包注入进去 TypeError,信封 failed、证据 0 条,界面上曾一直显示正常)", () => {
  const py = PY;
  const helper = path.join(path.dirname(fileURLToPath(import.meta.url)), "helpers", "endpoint_signatures.py");
  const r = spawnSync(py, [helper], { encoding: "utf8", cwd: REPO });
  // 🔴 取不到签名就**判红**,不许跳过 —— 跳过的棘轮等于没有棘轮
  assert.equal(r.status, 0, `签名导出失败(退出码 ${r.status}):${(r.stderr || "").slice(-400)}`);
  const sigs = JSON.parse(r.stdout) as Record<string, { module: string; params?: string[]; var_kw?: boolean; error?: string }>;

  const reg = JSON.parse(fs.readFileSync(path.join(REPO, "datasources", "registry.json"), "utf8")) as { endpoints: { id: string; module: string; args?: Record<string, unknown> }[] };
  const problems: string[] = [];
  const unresolved: string[] = [];
  for (const e of reg.endpoints) {
    const s0 = sigs[e.id];
    assert.ok(s0, `${e.id} 没出现在签名导出里`);
    // module:"legacy" 是独立脚本不是可导入函数 —— 机制不同,不适用
    if (e.module === "legacy") continue;
    if (s0.error) { unresolved.push(`${e.id}(${e.module}):${s0.error}`); continue; }
    if (s0.var_kw) continue;                       // 收 **kwargs 的什么都吃得下
    const ps = new Set(s0.params ?? []);
    for (const k of Object.keys(e.args ?? {}))
      if (!ps.has(k)) problems.push(`${e.id}:注册表声明了 ${k},但 ${e.module}.${e.id} 的函数只收 [${[...ps].join(", ")}]`);
  }
  assert.deepEqual(unresolved, [], `这些端点的函数导入不了 —— 参数对不对根本没人核过:\n${unresolved.join("\n")}`);
  assert.deepEqual(problems, [], `注册表声明的参数,函数收不下(会 TypeError,且只在信封里可见):\n${problems.join("\n")}`);
});

test("🔴 页面注入的上下文参数,改名后必须是该端点函数真收的参数名", () => {
  const py = PY;
  const helper = path.join(path.dirname(fileURLToPath(import.meta.url)), "helpers", "endpoint_signatures.py");
  const r = spawnSync(py, [helper], { encoding: "utf8", cwd: REPO });
  assert.equal(r.status, 0, `签名导出失败:${(r.stderr || "").slice(-300)}`);
  const sigs = JSON.parse(r.stdout) as Record<string, { module: string; params?: string[]; var_kw?: boolean; error?: string }>;
  const ctxDef = currentPlugin().pageContext;
  assert.ok(ctxDef, "垂类要声明 pageContext");

  // 🔴 先断言探针真解析出了键 —— 空数组会让下面的循环空转,棘轮"全绿"却什么都没查
  assert.ok(INJECTED_KEYS.length > 0, "pageContext.resolve 没产出任何注入键(探针信封的形状可能与实现漂移了)");
  let checked = 0;

  const problems: string[] = [];
  for (const [name, def] of Object.entries(currentPlugin().pageQueries ?? {})) {
    for (const b of def.blocks) {
      if (!b.injectContext) continue;
      const s0 = sigs[b.endpoint];
      if (!s0 || s0.error || s0.var_kw || s0.module === "legacy") continue;
      const ps = new Set(s0.params ?? []);
      // 只核这一块**自己声明要**的键(注册期已强制吃上下文就得声明 injectAs)
      for (const [from, to] of Object.entries(b.injectAs ?? {})) {
        checked += 1;
        // ① 源键必须真的是上下文会产出的 —— 拼错了就永远注入不到,且悄无声息
        assert.ok(INJECTED_KEYS.includes(from),
          `${name}.${b.id}:injectAs 的源键 ${from} 不在上下文产出的键里(有:${INJECTED_KEYS.join(", ")}) —— 拼错了会静默注入不到`);
        // ② 目标名必须是该端点函数真收的参数名
        if (!ps.has(to))
          problems.push(`${name}.${b.id}:注入键 ${from} 改名后是 ${to},而 ${b.endpoint} 的函数只收 [${[...ps].join(", ")}]`);
      }
    }
  }
  assert.ok(checked > 0, "一个吃上下文的块都没核到 —— 这条棘轮什么都没查");
  assert.deepEqual(problems, [], problems.join("\n"));
});

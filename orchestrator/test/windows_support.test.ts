import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import "../src/finance/register.ts";
import { fetchEnv, interpreterRoot, makeConfig, normalizeInterpreter } from "../src/config.ts";
import { privateFilePermissions, restrictPrivateFile, restrictPrivateFileAsync, sha256File, writeJson } from "../src/fsutil.ts";
import { executableInvocation, findExecutable, LocalAgentError, localSubscriptionEnv, runLocalAgent } from "../src/local_agent_runtime.ts";
import { RunToolsError, listRunFiles, readRunFile, runCalculation, writeStageOutput } from "../src/finance/run_tools_mcp.ts";
import { writeTurnContext } from "../src/turn_context.ts";
import { loadRun, validateFetchIntegrity } from "../src/validator.ts";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const temp = (prefix = "vra-win-") => fs.mkdtempSync(path.join(os.tmpdir(), prefix));

test("含空格的安装路径照常可用:受控工具不拼命令行,路径里有空格也能读写", () => {
  const dataRoot = temp("vra spaced data ");
  try {
    const cfg = makeConfig({ symbol: "300308", repoRoot: REPO, dataRoot, runId: "spaced" });
    assert.ok(cfg.runDir.includes(" "));
    fs.mkdirSync(path.join(cfg.runDir, "fetch"), { recursive: true });
    fs.writeFileSync(path.join(cfg.runDir, "fetch", "sample.json"), '{"status":"ok"}\n');
    writeTurnContext(cfg, "profile", 0);
    const ctx = { runDir: cfg.runDir, repoRoot: cfg.repoRoot, python: cfg.python };
    assert.ok(listRunFiles(ctx).files.some((x) => x.path === "fetch/sample.json"));
    assert.equal(readRunFile(ctx, "fetch/sample.json").text, '{"status":"ok"}\n');
  } finally { fs.rmSync(dataRoot, { recursive: true, force: true }); }
});

test("Windows 安装脚本:优先 3.12 但允许其他受支持 Python 3；doctor 警告不伪装成安装失败", () => {
  const setup = fs.readFileSync(path.join(REPO, "scripts", "setup-windows.ps1"), "utf8");
  assert.match(setup, /py -3\.12 -c/);
  assert.match(setup, /else \{ & py -3 -m venv/);
  assert.match(setup, /sys\.version_info >= \(3, 11\)/);
  assert.match(setup, /\$doctorExit -notin @\(0, 2\)/);
  assert.doesNotMatch(setup, /Assert-NativeSuccess "运行产品体检"/);
});

test("Windows 路径与环境:Scripts venv、反斜杠、用户目录和 PATHEXT 都被保留", async () => {
  assert.equal(normalizeInterpreter(" C:\\Users\\Simon\\VRA\\.venv\\Scripts\\python.exe "), "C:\\Users\\Simon\\VRA\\.venv\\Scripts\\python.exe");
  assert.equal(interpreterRoot("C:\\Users\\Simon\\VRA\\.venv\\Scripts\\python.exe"), "C:\\Users\\Simon\\VRA\\.venv");
  // 本机订阅 Agent 与取数子进程的环境白名单都要保住 Windows 必需变量(缺了 CLI 找不到用户目录 / 解释器起不来)
  const winBase = { PATH: "X", USERPROFILE: "C:\\Users\\Simon", PATHEXT: ".EXE;.CMD", SYSTEMROOT: "C:\\Windows", APPDATA: "C:\\Users\\Simon\\AppData\\Roaming", OPENAI_API_KEY: "leak" };
  for (const env of [localSubscriptionEnv(winBase, "claude"), localSubscriptionEnv(winBase, "codex"), localSubscriptionEnv(winBase, "codebuddy"), fetchEnv({}, winBase)]) {
    assert.equal(env.USERPROFILE, "C:\\Users\\Simon");
    assert.equal(env.PATHEXT, ".EXE;.CMD");
    assert.equal(env.SYSTEMROOT, "C:\\Windows");
    assert.equal(env.APPDATA, "C:\\Users\\Simon\\AppData\\Roaming");
    assert.ok(!("OPENAI_API_KEY" in env), "密钥类变量不进子进程");
  }
  assert.doesNotThrow(() => makeConfig({ symbol: "300308", repoRoot: REPO, dataRoot: temp("vra more spaces ") }));
  const npmBin = temp();
  const cmd = path.join(npmBin, "claude.cmd");
  const ps1 = path.join(npmBin, "claude.ps1");
  fs.writeFileSync(cmd, "@echo off\r\n");
  fs.writeFileSync(ps1, '#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n& "node$exe" "$basedir/cli.cjs" $args\nexit $LASTEXITCODE\n');
  const cli = path.join(npmBin, "cli.cjs");
  fs.writeFileSync(cli, '#!/usr/bin/env node\nconsole.log(JSON.stringify(process.argv.slice(2)));\n');
  assert.equal(findExecutable("claude", { PATH: npmBin, PATHEXT: ".EXE;.CMD" }, "win32"), ps1);
  const launch = executableInvocation(ps1, ["--system-prompt", "A&B%PATH%"], { SYSTEMROOT: "C:\\Windows" }, "win32");
  assert.equal(launch.file, process.execPath);
  assert.deepEqual(launch.args.slice(-2), ["--system-prompt", "A&B%PATH%"], "提示词必须作为独立 argv，不经 cmd.exe 拼接展开");
  const values = ["--tools", "", "--mcp-config", '{"name":"测试","path":"C:\\\\a\\\\b"}', 'a "quoted" value', "A&B%PATH%", "line1\nline2", "trailing\\"];
  const exact = executableInvocation(ps1, values, process.env, "win32");
  const result = spawnSync(exact.file, exact.args, { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), values, "真实子进程必须逐字收到空参数、JSON、中文与元字符");
  fs.writeFileSync(ps1, '& node "custom.js" @args\n');
  assert.throws(() => executableInvocation(ps1, values, process.env, "win32"), /标准 npm|原生/,
    "未知 PowerShell 包装器不得悄悄回退到会吞参数的执行路径");
  if (process.platform === "win32") {
    for (let i = 0; i < 5; i += 1) await assert.rejects(runLocalAgent("claude", {
      systemPrompt: "规则", userPrompt: "测试", env: { ...process.env, CLAUDE_BIN: ps1 },
    }), (e: unknown) => e instanceof LocalAgentError && e.code === "agent_windows_wrapper_unsupported",
    "拒绝未知启动器不能泄漏并发槽位并在第五次变成 busy");
  }
});

test("Windows npm shim 指向原生 exe 时(Claude Code 形态)直接 spawn 该 exe", () => {
  const npmBin = temp();
  const ps1 = path.join(npmBin, "claude.ps1");
  const exe = path.join(npmBin, "node_modules/@anthropic-ai/claude-code/bin/claude.exe");
  fs.mkdirSync(path.dirname(exe), { recursive: true });
  fs.writeFileSync(exe, "MZ fake exe");
  // 与本机 claude.ps1 同构：shebang + basedir + 直调 $basedir 下的 .exe
  fs.writeFileSync(ps1, '#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n\n$exe=""\nif ($PSVersionTable.PSVersion -lt "6.0" -or $IsWindows) {\n  $exe=".exe"\n}\nif ($MyInvocation.ExpectingInput) {\n  $input | & "$basedir/node_modules/@anthropic-ai/claude-code/bin/claude.exe"   $args\n} else {\n  & "$basedir/node_modules/@anthropic-ai/claude-code/bin/claude.exe"   $args\n}\nexit $LASTEXITCODE\n');
  const launch = executableInvocation(ps1, ["--version"], {}, "win32");
  assert.equal(launch.file, exe, "exe 型 shim 必须解析出真实 exe 路径并直接 spawn（不经 shell）");
  assert.deepEqual(launch.args, ["--version"]);
  // 目标文件不存在 / 含插值字符 / 非 exe 目标 → 一律仍按不支持拒绝
  fs.rmSync(exe);
  assert.throws(() => executableInvocation(ps1, ["--version"], {}, "win32"), /标准 npm|原生/);
  fs.writeFileSync(exe, "MZ fake exe");
  fs.writeFileSync(ps1, '#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n& "$basedir/node_modules/x/bin/$($env:EVIL).exe" $args\n');
  assert.throws(() => executableInvocation(ps1, ["--version"], {}, "win32"), /标准 npm|原生/,
    "目标带插值字符的 shim 不得被执行");
});

test("Windows npm shim 指向无扩展名 node 入口时(CodeBuddy 形态)用 node 运行该入口", () => {
  const npmBin = temp();
  const ps1 = path.join(npmBin, "codebuddy.ps1");
  const entry = path.join(npmBin, "node_modules/@tencent-ai/codebuddy-code/bin/codebuddy");
  fs.mkdirSync(path.dirname(entry), { recursive: true });
  fs.writeFileSync(entry, "#!/usr/bin/env node\nconsole.log(JSON.stringify(process.argv.slice(2)));\n");
  // 与本机 codebuddy.ps1 同构：Test-Path 分支 + 无扩展名入口目标
  fs.writeFileSync(ps1, '#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n\n$exe=""\nif ($PSVersionTable.PSVersion -lt "6.0" -or $IsWindows) {\n  $exe=".exe"\n}\n$ret=0\nif (Test-Path "$basedir/node$exe") {\n  if ($MyInvocation.ExpectingInput) {\n    $input | & "$basedir/node$exe"  "$basedir/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy" $args\n  } else {\n    & "$basedir/node$exe"  "$basedir/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy" $args\n  }\n  $ret=$LASTEXITCODE\n} else {\n  & "node$exe"  "$basedir/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy" $args\n}\nexit $ret\n');
  const launch = executableInvocation(ps1, ["--version"], {}, "win32");
  assert.equal(launch.file, process.execPath, "无扩展名 node 入口必须用 node 运行");
  assert.equal(launch.args[0], entry);
  // 入口内容不是 node shebang（比如随手一个文本文件）→ 仍然拒绝
  fs.writeFileSync(entry, "not a node script\n");
  assert.throws(() => executableInvocation(ps1, ["--version"], {}, "win32"), /标准 npm|原生/);
  // shebang 收紧：node 前必须是 / 或空格（"xx-node-thing" 不算指向 node）
  fs.writeFileSync(entry, "#!/usr/bin/env xx-node-thing\nconsole.log(1);\n");
  assert.throws(() => executableInvocation(ps1, ["--version"], {}, "win32"), /标准 npm|原生/,
    "伪 node shebang（xx-node-thing）不得放行");
  // 入口带 UTF-8 BOM 不应误拒
  fs.writeFileSync(entry, "\uFEFF#!/usr/bin/env node\nconsole.log(1);\n");
  assert.doesNotThrow(() => executableInvocation(ps1, ["--version"], {}, "win32"));
  // shim 自身带 BOM 也不应误拒
  fs.writeFileSync(ps1, "\uFEFF" + '#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n& "node$exe"  "$basedir/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy" $args\nexit $ret\n');
  assert.doesNotThrow(() => executableInvocation(ps1, ["--version"], {}, "win32"));
});

// v1 的「Codex config.toml 隔离投影」与「研究线程关 Shell / 只读沙箱」两条已移到 local_agent_runtime.test.ts:
// v2 的 Codex 用 --ignore-user-config 根本不读用户配置,参数在各平台一致,不再需要按平台单独验证。

test("受控 MCP:只能读净化产物、计算用 argv 调 Python、只能写当前阶段", () => {
  const dataRoot = temp();
  const cfg = makeConfig({ symbol: "300308", repoRoot: REPO, dataRoot, runId: "tools", python: process.platform === "win32" ? "python" : "python3" });
  fs.mkdirSync(path.join(cfg.runDir, "fetch"), { recursive: true });
  fs.mkdirSync(path.join(cfg.runDir, "raw"), { recursive: true });
  fs.writeFileSync(path.join(cfg.runDir, "fetch", "sample.json"), '{"status":"ok"}\n');
  fs.writeFileSync(path.join(cfg.runDir, "raw", "secret.json"), "{}\n");
  writeTurnContext(cfg, "profile", 0);
  const ctx = { runDir: cfg.runDir, repoRoot: cfg.repoRoot, python: cfg.python };

  assert.ok(listRunFiles(ctx).files.some((x) => x.path === "fetch/sample.json"));
  assert.equal(readRunFile(ctx, "fetch/sample.json").text, '{"status":"ok"}\n');
  assert.throws(() => readRunFile(ctx, "raw/secret.json"), (e: unknown) => e instanceof RunToolsError && e.code === "path_not_allowed");

  const record = runCalculation(ctx, { function: "ratio", args: { numerator: 1, denominator: 4 }, output_file: "01_ratio.json" });
  assert.equal((record.output as Record<string, unknown>).status, "ok");
  assert.equal((record.output as Record<string, unknown>).value, 0.25);
  assert.ok(fs.existsSync(path.join(cfg.runDir, "calcs", "01_ratio.json")));
  assert.equal((runCalculation(ctx, { function: "ratio", args: { numerator: 2, denominator: 4 }, output_file: "01_ratio.json" }).output as Record<string, unknown>).value, 0.5, "同阶段补跑可覆盖自己的计算");

  writeTurnContext(cfg, "financials", 1);
  assert.throws(
    () => runCalculation(ctx, { function: "ratio", args: { numerator: 3, denominator: 4 }, output_file: "01_ratio.json" }),
    (e: unknown) => e instanceof RunToolsError && e.code === "calc_owned_by_other_stage",
  );

  writeTurnContext(cfg, "profile", 2);
  assert.throws(() => writeStageOutput(ctx, { stage: "financials" }), (e: unknown) => e instanceof RunToolsError && e.code === "wrong_stage");
  assert.throws(() => writeStageOutput(ctx, { stage: "profile" }), (e: unknown) => e instanceof RunToolsError && e.code === "stage_schema_invalid");
});

test("Windows 账本路径按 JSON 契约使用正斜杠，不随宿主路径分隔符漂移", () => {
  const runDir = temp();
  for (const dir of ["raw", "fetch", "calcs", "stages"]) fs.mkdirSync(path.join(runDir, dir), { recursive: true });
  const file = path.join(runDir, "fetch", "fetch_quote.json");
  writeJson(file, {
    script: "fetch_quote", symbol: "300308", market: "SZ", status: "ok", fetched_at: "2026-08-30T00:00:00Z",
    primary_source: "fixture", used_sources: ["fixture"], evidence: [], extra: {}, errors: [], missing: [],
  });
  const ledger = {
    fetch_quote: {
      script: "fetch_quote", argv: [], exit_code: 0, duration_ms: 1, status: "ok" as const,
      file: "fetch/fetch_quote.json", sha256: sha256File(file), raw_files: {},
      started_at: "2026-08-30T00:00:00Z", finished_at: "2026-08-30T00:00:01Z", stage: "profile",
    },
  };
  assert.deepEqual(validateFetchIntegrity(loadRun(runDir, ledger)).errors, []);
});

test("Windows 私密文件权限不用 Unix mode 冒充 ACL 验证", { skip: process.platform !== "win32" }, async () => {
  const dir = temp();
  const file = path.join(dir, "private.txt");
  fs.writeFileSync(file, "secret", { mode: 0o600 });
  restrictPrivateFile(file);
  assert.equal(privateFilePermissions(file).secure, true, "收紧后必须断开继承且只留当前用户 SID");
  const asyncFile = path.join(dir, "private-async.txt");
  fs.writeFileSync(asyncFile, "");
  let eventLoopAdvanced = false;
  const timer = setTimeout(() => { eventLoopAdvanced = true; }, 0);
  try {
    await restrictPrivateFileAsync(asyncFile);
    assert.equal(eventLoopAdvanced, true, "等待 PowerShell 期间事件循环必须继续处理请求");
    assert.equal(privateFilePermissions(asyncFile).secure, true);
  } finally { clearTimeout(timer); }
});

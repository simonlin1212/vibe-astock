/**
 * **用户自有台账**(Core 侧):记录的存储、校验与增删改查。
 *
 * 边界:Core 只知道"有若干**种类**的记录,每种有一组字段"。
 * **种类叫什么、字段是什么,全部由 Plugin 声明**(`Plugin.ledger.kinds`)——
 * 换个垂类换一套种类,这个文件一行都不用改。
 *
 * 与研究运行的区别:运行产物是**取来的事实**(带 raw_ref,可复算);
 * 台账是**用户自己写下的东西**(计划、判据、记录)。两者永不混在一起:
 * 台账不进证据账本,也不参与数字绑定。
 *
 * 落盘:`<dataRoot>/ledger/<kind>.json`。写入走 `atomicWrite`(临时文件 → fsync → 替换),
 * 并在读-改-写全程持排他锁 —— 这是用户手写的数据,丢一条就是真丢。
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import AjvModule from "ajv";

import { applyCoreFormats } from "./formats.ts";
import { atomicWrite, nowIso } from "./fsutil.ts";
import { LEDGER_ENVELOPE_KEYS, currentPlugin, hasPlugin, type LedgerKindDef } from "./plugin.ts";

export type { LedgerKindDef };

const AjvCtor = ((AjvModule as unknown as { default?: unknown }).default ?? AjvModule) as new (o: object) => {
  compile: (s: object) => ((d: unknown) => boolean) & { errors?: { instancePath?: string; message?: string }[] | null };
  addFormat: (name: string, def: { type: "string"; validate: (s: string) => boolean }) => unknown;
};

function newAjv(): InstanceType<typeof AjvCtor> {
  return applyCoreFormats(new AjvCtor({ allErrors: true, strict: false }));
}

/** Core 拥有的信封字段;其余字段由垂类声明。**这四个键垂类不许重名**(注册期校验) */
export interface LedgerRecord {
  id: string;
  kind: string;
  created_at: string;
  updated_at: string;
  [field: string]: unknown;
}

/** 与契约同一份定义,别在这里再写一遍(两处各写一份 = 迟早漂移) */
export const ENVELOPE_KEYS = LEDGER_ENVELOPE_KEYS;

interface LedgerFile {
  schema_version: number;
  kind: string;
  records: LedgerRecord[];
}

export class LedgerError extends Error {
  // 不用参数属性语法:本仓库开了 erasableSyntaxOnly(Node 直接跑 .ts,不允许会产生运行时代码的 TS 语法)
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "LedgerError";
    this.code = code;
  }
}

/** 声明的种类表;插件没声明台账就是空表(合法 —— 不是每个垂类都需要台账) */
export function kinds(): Readonly<Record<string, LedgerKindDef>> {
  if (!hasPlugin()) return {};
  return currentPlugin().ledger?.kinds ?? {};
}

/**
 * 字段 / 枚举的显示名。**Core 不解释这些字符串**,只负责把它们送到界面。
 * 没声明就是空表 —— 界面退回原键名显示,而不是整块不显示。
 */
export function labels(): { fields: Readonly<Record<string, string>>; enums: Readonly<Record<string, string>> } {
  const l = hasPlugin() ? currentPlugin().ledger : undefined;
  return { fields: l?.fieldLabels ?? {}, enums: l?.enumLabels ?? {} };
}

/**
 * 种类名 → 文件路径。
 * 🔴 **kind 必须是已声明的种类**,不是"看着像个安全的字符串就行" ——
 *    它会被拼进文件路径,白名单是这里唯一可靠的防线(`..` / 绝对路径 / 大小写变体一概挡在外面)。
 */
function fileOf(dataRoot: string, kind: string): string {
  if (!Object.prototype.hasOwnProperty.call(kinds(), kind)) {
    throw new LedgerError("unknown_kind", `台账没有这个种类:${JSON.stringify(kind)}`);
  }
  const dir = path.resolve(dataRoot, "ledger");
  const abs = path.resolve(dir, `${kind}.json`);
  // 🔴 第二道:上面那句只证明"这个字符串是插件对象的键",**不证明它是安全文件名**。
  // ⚠️ **当前不可达,变异测试测不出来** —— 注册期已按 `^[a-z][a-z0-9_]{0,31}$` 挡住了全部穿越
  //    (实测:`../../config` / `..` / `a/b` / `a\b` / `__proto__` 一律拒),所以能走到这里的 kind
  //    一定是安全的。留着是为了**第一道被改松时还有第二道**,别因为"测试是绿的"就以为它被覆盖了。
  if (abs !== path.join(dir, `${kind}.json`) || path.dirname(abs) !== dir) {
    throw new LedgerError("unknown_kind", `种类名不是安全文件名:${JSON.stringify(kind)}`);
  }
  return abs;
}

const validators = new Map<string, (d: unknown) => boolean>();

/** 校验器按种类缓存;键带上插件 id,免得同进程换过插件后拿到上一套的校验器 */
function validatorFor(
  kind: string,
): ((d: unknown) => boolean) & { errors?: { instancePath?: string; message?: string }[] | null } {
  const table = kinds();
  // 🔴 与 fileOf 用**同一把尺子**(hasOwnProperty),不要用"取值判真" ——
  //    `({})["constructor"]` 返回构造函数、是 truthy,取值判真的守卫会被它绕过,
  //    于是两处守卫对同一个 kind 给出不同答案。同一个不变量只能有一种判法。
  // ⚠️ **当前不可达**:公开入口都先过 fileOf,坏 kind 到不了这里(变异测试因此测不红)。
  //    改它纯粹是为了两处口径一致 —— 口径不一致本身就是下一个 bug 的温床。
  if (!Object.prototype.hasOwnProperty.call(table, kind)) {
    throw new LedgerError("unknown_kind", `台账没有这个种类:${JSON.stringify(kind)}`);
  }
  const def = table[kind]!;
  // 🔴 同 ingest:只按 id::kind 索引,换一个 id 相同、字段不同的插件会命中旧校验器。
  //    两处是同一个不变量,用同一种做法(键里带契约哈希)。
  const cacheKey = `${hasPlugin() ? currentPlugin().id : "-"}::${kind}::${crypto.createHash("sha256").update(JSON.stringify(def)).digest("hex").slice(0, 12)}`;
  const hit = validators.get(cacheKey);
  if (hit) return hit as never;
  const schema = {
    type: "object",
    additionalProperties: false, // 多写一个字段 = 拼错了名字,当场说,别静默丢掉
    required: [...def.required],
    properties: { ...def.properties },
  };
  const fn = newAjv().compile(schema);
  validators.set(cacheKey, fn as never);
  return fn;
}

function explain(fn: { errors?: { instancePath?: string; message?: string }[] | null }): string {
  return (
    (fn.errors ?? []).map((e) => `${e.instancePath || "(顶层)"} ${e.message ?? ""}`.trim()).join(";") || "字段不合法"
  );
}

/* ---------------- 排他锁 ---------------- */

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    // EPERM = 进程在,只是不属于当前用户 ⇒ 仍算活着,不能抢
    return (e as NodeJS.ErrnoException).code === "EPERM";
  }
}

/** 同步等待。sync 路径没有 sleep,用 Atomics.wait 而不是空转烧 CPU */
function pause(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

const LOCK_WAIT_MS = 5_000;
/** 锁文件里没有可用 pid 时(创建后、写 pid 前崩溃)的兜底回收年龄 */
const ORPHAN_LOCK_MS = 60_000;

interface Holder {
  pid: number;
  token: string;
}

function readHolder(lock: string): Holder | null {
  try {
    const [pidStr, token] = fs.readFileSync(lock, "utf8").trim().split(":");
    const pid = Number(pidStr);
    return Number.isInteger(pid) && pid > 0 && token ? { pid, token } : null;
  } catch {
    return null;
  }
}

function sameFile(a: string, b: string): boolean {
  try {
    const x = fs.statSync(a);
    const y = fs.statSync(b);
    return x.ino === y.ino && x.dev === y.dev;
  } catch {
    return false;
  }
}

/**
 * 读-改-写全程持锁。
 *
 * 互斥靠 **`linkSync` 的原子性**(把自己那个唯一文件硬链成锁名):即便多个进程同时判定旧锁失效
 * 并把它删掉,后续的 link 也只有一个能成功 —— 这是 `O_CREAT|O_EXCL` 之外同等强度的原语。
 *
 * 🔴 **回收判据是持有者进程还活不活,不是锁文件多久没动** ——
 *    按 mtime 回收会把一个正在慢慢干活的持有者的锁抢走。
 * ⚠️ 例外:锁里读不出持有者(建好之后、写内容之前崩了),没有 pid 可查,才退回按年龄回收并出声。
 *
 * 🔴🔴 **这不是跨进程互斥保证,别当它是**(Codex 复审 r3-P1,如实记下):
 *    "读到锁失效 → 删除它"是两步,中间永远有窗口 —— A 判定旧锁失效的那一瞬,
 *    B 可能已经删掉旧锁并 link 了新锁,于是 A 删掉的是 B 正在持有的锁。
 *    连续两次确认 + 随机退避 + 释放前比 inode 只**降低概率**,数学上消不掉。
 *    零依赖前提下没有正确解:要真互斥得用 `flock`(内核级、进程死自动释放),Node 无内置。
 *
 *    ⇒ **真正的互斥来自产品形态**:一个垂类一个本地服务进程,API 占用固定端口,
 *      第二个实例根本起不来;同进程内这些调用全是同步的,天然串行。
 *      这层文件锁的定位是**崩溃残留的自愈**(硬杀 / 断电后不用人工删文件),不是并发控制。
 *    ⚠️ 哪天要做多进程 / 多租户,**必须换成内核锁**,不要在这里继续加确认次数。
 */
export function withLock<T>(target: string, fn: () => T): T {
  const lock = `${target}.lock`;
  const token = crypto.randomBytes(8).toString("hex");
  const mine = `${lock}.${process.pid}.${token}`;
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(mine, `${process.pid}:${token}`, { mode: 0o600 });

  try {
    const deadline = Date.now() + LOCK_WAIT_MS;
    let staleSeen: string | null = null;
    for (;;) {
      try {
        fs.linkSync(mine, lock); // 原子:并发时只有一个能成功
        break;
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
      }
      const holder = readHolder(lock);
      if (holder && !alive(holder.pid)) {
        // 连续两次看到同一个死持有者才回收 —— 一次就动手时,两个进程更容易撞在一起
        if (staleSeen === holder.token) {
          if (readHolder(lock)?.token === holder.token) fs.rmSync(lock, { force: true });
          staleSeen = null;
        } else {
          staleSeen = holder.token;
        }
        pause(10 + Math.floor(Math.random() * 20));
        continue;
      }
      staleSeen = null;
      if (!holder) {
        let ageMs = 0;
        try {
          ageMs = Date.now() - fs.statSync(lock).mtimeMs;
        } catch {
          continue; // 锁刚好被释放了
        }
        if (ageMs > ORPHAN_LOCK_MS) {
          console.error(`[ledger] 锁 ${path.basename(lock)} 读不出持有者且已存在 ${Math.round(ageMs / 1000)} 秒,判为残留并回收`);
          fs.rmSync(lock, { force: true });
          continue;
        }
      }
      if (Date.now() > deadline) {
        throw new LedgerError(
          "locked",
          `台账被占用超过 ${LOCK_WAIT_MS / 1000} 秒(持有者进程 ${holder?.pid ?? "未知"})。`
            + `若确认没有别的实例在写,可删除锁文件后重试:${lock}`,
        );
      }
      pause(20 + Math.floor(Math.random() * 30));
    }

    try {
      return fn();
    } finally {
      // 🔴 只删**自己的**锁:比 inode,不比路径。
      //    无条件 rm 会在自己的锁被误回收后,顺手删掉接替者正在持有的锁。
      if (sameFile(lock, mine)) fs.rmSync(lock, { force: true });
      else console.error(`[ledger] 释放时发现锁 ${path.basename(lock)} 已不属于本次持有 —— 可能被误回收过,本次写入可能与他人交错`);
    }
  } finally {
    fs.rmSync(mine, { force: true });
  }
}

/* ---------------- 读写 ---------------- */

function loadFile(file: string, kind: string): LedgerFile {
  // 🔴 **"文件不存在"和"文件在但读不动"必须分开**。
  //    原来这里用 `readJsonIfExists`,它把 JSON 解析失败也吞成 `null` ⇒ 损坏的台账被当成"还没写过":
  //    读接口 200 返回空列表(伪装成"你本来就没记过东西"),而**下一次新增会把只含新记录的文件原子写上去,
  //    原文件永久没了** —— 回读复核还会确认新记录在,一路报成功。
  //    这与本函数下面那句"已保留原文件,请人工处理"直接矛盾:那条路它根本走不到。
  if (!fs.existsSync(file)) return { schema_version: 1, kind, records: [] };
  let raw: LedgerFile;
  try {
    raw = JSON.parse(fs.readFileSync(file, "utf8")) as LedgerFile;
  } catch (e) {
    throw new LedgerError(
      "corrupt",
      `台账文件不是合法 JSON:${path.basename(file)}(${e instanceof Error ? e.message : String(e)});已保留原文件,请人工处理`,
    );
  }
  // 🔴 文件在用户数据区,当**不可信输入**处理:形状不对就报错,不要"尽力解析"后
  //    悄悄丢掉一半记录 —— 那是拿用户手写的数据做赌注。
  const bad = (why: string): never => {
    throw new LedgerError("corrupt", `台账文件损坏:${path.basename(file)}(${why});已保留原文件,请人工处理`);
  };
  if (!raw || typeof raw !== "object" || !Array.isArray(raw.records)) bad("records 不是数组");
  // 🔴 只查"records 是不是数组"不够:元素若是 null / 字符串,后面 `r.id` 会抛 TypeError(变成 500);
  //    id 重复时**更新只改第一条、删除却删掉全部**,两个操作对同一份数据给出不同答案。
  //    用户数据区是不可信输入 —— 形状不对就当场拒,不要带着坏数据继续跑。
  const seen = new Set<string>();
  for (const [i, r] of raw.records.entries()) {
    if (!r || typeof r !== "object" || Array.isArray(r)) bad(`第 ${i + 1} 条不是对象`);
    const rec = r as Record<string, unknown>;
    for (const k of ENVELOPE_KEYS) {
      if (typeof rec[k] !== "string" || !rec[k]) bad(`第 ${i + 1} 条缺少或非法的 ${k}`);
    }
    if (rec.kind !== kind) bad(`第 ${i + 1} 条的 kind 是 ${JSON.stringify(rec.kind)},与文件不符`);
    if (seen.has(rec.id as string)) bad(`id 重复:${String(rec.id)}`);
    seen.add(rec.id as string);
  }
  return { schema_version: Number(raw.schema_version) || 1, kind, records: raw.records };
}

function saveFile(file: string, data: LedgerFile): void {
  atomicWrite(file, `${JSON.stringify(data, null, 2)}\n`);
}

/**
 * 写完立刻回读复核。
 *
 * 🔴 这**不是**在修上面那个锁的竞态(那个数学上消不掉,见 `withLock` 注释),
 *    修的是它最坏的后果:**用户新增/修改了一条,接口返回 200,记录却不在文件里**。
 *    静默丢用户手写的数据,是这个模块能犯的最严重的错;宁可如实报"这次没写进去"。
 *
 * ⚠️ 诚实的边界:复核之后到下一次写之间仍可能被别人覆盖 —— 窗口被压到很小,但不是零。
 */
function sameRecord(a: LedgerRecord | undefined, b: LedgerRecord): boolean {
  // 逐字段比,不比引用。台账记录是纯 JSON(无 undefined / 循环),序列化比较够用且不会漏字段
  return a !== undefined && JSON.stringify(a) === JSON.stringify(b);
}

function saveVerified(file: string, kind: string, data: LedgerFile, expect: (rs: LedgerRecord[]) => boolean): void {
  saveFile(file, data);
  let ok = false;
  try {
    ok = expect(loadFile(file, kind).records);
  } catch {
    ok = false; // 回读都读不出来 = 更糟,同样按没写成处理
  }
  if (!ok) {
    throw new LedgerError(
      "write_lost",
      `写入后回读没找到这次的改动(${path.basename(file)})。多半是另一个进程同时在写同一份台账;请确认没有第二个实例在跑,然后重试。`,
    );
  }
}

/**
 * 生成 id:种类前缀 + UUID。
 * 🔴 不用 `Date.now()+Math.random()`:同毫秒可撞,而撞了之后**更新只改第一条、删除删掉全部** ——
 *    是会丢用户数据的那种不一致。调用处还会在持锁后再查一次重(万一 UUID 也撞)。
 */
function newId(kind: string): string {
  return `${kind}-${crypto.randomUUID()}`;
}

/** 一条不符合垂类契约的磁盘记录。**不删不改**,只是把问题说出来 */
export interface LedgerIssue {
  id: string;
  why: string;
}

/**
 * 读一种记录,并按**垂类契约**逐条复核。
 *
 * 🔴 为什么读也要校验:写入路径过 `validatorFor`,读取路径此前只查信封形状 ——
 *    台账文件是**用户能直接编辑的磁盘文件**,手改一条 `shares: "很多"` 就能绕开全部字段约束,
 *    然后以 200 成功返回给界面和下游。**同一个不变量在写入和读取两侧口径不同,本身就是 bug。**
 *
 * ⚠️ 复核**不抛错、不丢记录**,只回报问题。理由:垂类日后新增一个必填字段,
 *    老记录会全部"不合契约" —— 若在这里抛错,用户的台账会在一次升级后整份打不开,
 *    而且他连进界面把字段补上的机会都没有。比原来的 bug 更糟。
 */
export function listRecordsChecked(dataRoot: string, kind: string): { records: LedgerRecord[]; issues: LedgerIssue[] } {
  const file = fileOf(dataRoot, kind);
  const records = loadFile(file, kind).records;
  const fn = validatorFor(kind);
  const issues: LedgerIssue[] = [];
  for (const r of records) {
    const rest: Record<string, unknown> = { ...r };
    for (const k of ENVELOPE_KEYS) delete rest[k];
    if (!fn(rest)) issues.push({ id: r.id, why: explain(fn) });
  }
  return { records, issues };
}

export function listRecords(dataRoot: string, kind: string): LedgerRecord[] {
  return listRecordsChecked(dataRoot, kind).records;
}

/**
 * 已有时间比当前时刻最多晚这么多,才接着它往后排。
 * 🔴 超过的当成系统时间曾被调快时写下的未来时间,不跟着走:否则以后每条都排到那个未来时刻之后,
 *    删掉那条也恢复不了,按录入 / 修改时间判断先后的地方会把早写的记录判成晚写的。
 */
const MAX_AHEAD_MS = 1_000;

/**
 * 更新时刻:晚于上一版的 `updated_at`(上一版是未来时间的除外,见 MAX_AHEAD_MS)。
 * 🔴 写完马上改会落在同一毫秒;整批录入还把 `created_at` 往后排了几毫秒 —— 直接取当前时刻,
 *    改过的记录会和 `created_at` 相等甚至更早,靠 `updated_at === created_at` 判断"没改过"的地方就会认错。
 * 🔴 未来时间上限对照真实当前时刻,不是按录入顺序推进后的 `at`;后者可能让超出上限的上一版继续往后排。
 */
function updatedAfter(prev: string, at: string, now: number): string {
  const p = Date.parse(prev);
  const a = Date.parse(at);
  return Number.isFinite(p) && a <= p && p - now <= MAX_AHEAD_MS ? nowIso(new Date(p + 1)) : at;
}

/**
 * 新记录的录入时刻(毫秒):晚于这个种类里已有的每一条(未来时间的除外,见 MAX_AHEAD_MS)。
 * 🔴 录入先后要能从记录本身还原:同一毫秒里接连新增,或紧跟在整批录入(往后排了几毫秒)后面新增,
 *    直接取当前时刻会和前面的相等甚至更早,先后就乱了。
 */
function createdAfter(records: readonly LedgerRecord[], at: number): number {
  let last = -Infinity;
  for (const r of records) {
    const t = Date.parse(r.created_at);
    if (Number.isFinite(t) && t > last && t - at <= MAX_AHEAD_MS) last = t;
  }
  return Math.max(at, last + 1);
}

/**
 * 新增或更新一条。带 `id` 且能找到 = 更新(**整条替换,不做字段级合并**);否则新增。
 * 不合并是刻意的:合并会让"我把某个字段清空了"和"我这次没提这个字段"变得无法区分。
 */
export function upsertRecord(dataRoot: string, kind: string, input: Record<string, unknown>): LedgerRecord {
  const file = fileOf(dataRoot, kind);
  const rest: Record<string, unknown> = { ...input };
  const id = rest.id;
  for (const k of ENVELOPE_KEYS) delete rest[k];
  const fn = validatorFor(kind);
  if (!fn(rest)) throw new LedgerError("bad_record", explain(fn));

  return withLock(file, () => {
    const data = loadFile(file, kind);
    const now = nowIso();
    if (id !== undefined) {
      if (typeof id !== "string" || !id) throw new LedgerError("bad_id", "id 必须是非空字符串");
      const i = data.records.findIndex((r) => r.id === id);
      if (i < 0) throw new LedgerError("not_found", `台账里没有这条记录:${id}`);
      const prev = data.records[i]!;
      const next: LedgerRecord = { ...rest, id, kind, created_at: prev.created_at, updated_at: updatedAfter(prev.updated_at, now, Date.parse(now)) };
      data.records[i] = next;
      // 🔴 比**整条内容**,不只比 id 与时间戳:并发覆盖可以保留同一个 id、
      //    同一毫秒里 `updated_at` 还会一模一样 ⇒ 只比标识的话,字段已经是别人的版本也照样报成功。
      saveVerified(file, kind, data, (rs) => sameRecord(rs.find((r) => r.id === id), next));
      return next;
    }
    let fresh = newId(kind);
    // 便宜的兜底:UUID 撞的概率可以忽略,但"忽略"和"检查过"在数据丢失面前不是一回事
    for (let i = 0; data.records.some((r) => r.id === fresh) && i < 5; i++) fresh = newId(kind);
    if (data.records.some((r) => r.id === fresh)) throw new LedgerError("id_collision", "生成 id 连续撞车,请重试");
    const at = nowIso(new Date(createdAfter(data.records, Date.parse(now))));
    const next: LedgerRecord = { ...rest, id: fresh, kind, created_at: at, updated_at: at };
    data.records.push(next);
    saveVerified(file, kind, data, (rs) => sameRecord(rs.find((r) => r.id === fresh), next));
    return next;
  });
}

/** 只校验、不写:这条按垂类契约能不能存。能存返回 null,不能返回原因(给预览用,与真正写入是同一套检查) */
export function checkRecord(kind: string, input: Record<string, unknown>): string | null {
  const rest: Record<string, unknown> = { ...input };
  for (const k of ENVELOPE_KEYS) delete rest[k];
  const fn = validatorFor(kind);
  return fn(rest) ? null : explain(fn);
}

/**
 * 一次新增多条(批量导入用)。**要么全写进去,要么一条都不写**:先逐条校验,有一条不合契约就整批拒,报出第几条、为什么。
 *
 * 🔴 只新增:带 id 的整批拒 —— 批量覆盖已有记录太容易误伤,改已有记录走 `upsertRecord` 一条条来。
 * 🔴 同一批的 `created_at` 按传入顺序逐条加 1 毫秒:按录入先后排序的地方(同一天的几条谁先谁后)要能从记录本身还原,
 *    全写成同一个时刻的话,先后就只剩随机 id 决定了。
 */
export function insertRecords(dataRoot: string, kind: string, inputs: readonly Record<string, unknown>[]): LedgerRecord[] {
  const file = fileOf(dataRoot, kind);
  const fn = validatorFor(kind);
  const rests = inputs.map((input, i) => {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new LedgerError("bad_record", `第 ${i + 1} 条不是对象`);
    if (input.id !== undefined) throw new LedgerError("bad_record", `第 ${i + 1} 条带了 id:批量只新增,改已有记录请一条条改`);
    const rest: Record<string, unknown> = { ...input };
    for (const k of ENVELOPE_KEYS) delete rest[k];
    if (!fn(rest)) throw new LedgerError("bad_record", `第 ${i + 1} 条:${explain(fn)}`);
    return rest;
  });
  if (!rests.length) return [];
  return withLock(file, () => {
    const data = loadFile(file, kind);
    const base = createdAfter(data.records, Date.now());
    const taken = new Set(data.records.map((r) => r.id));
    const added = rests.map((rest, i) => {
      let fresh = newId(kind);
      for (let n = 0; taken.has(fresh) && n < 5; n++) fresh = newId(kind);
      if (taken.has(fresh)) throw new LedgerError("id_collision", "生成 id 连续撞车,请重试");
      taken.add(fresh);
      // 🔴 与 upsertRecord 同一种时间写法(nowIso,+08:00):录入先后按字符串比,混用 `Z` 写法会把同一天的先后排反
      const at = nowIso(new Date(base + i));
      return { ...rest, id: fresh, kind, created_at: at, updated_at: at } as LedgerRecord;
    });
    data.records.push(...added);
    saveVerified(file, kind, data, (rs) => added.every((a) => sameRecord(rs.find((r) => r.id === a.id), a)));
    return added;
  });
}

/** 记录去掉信封字段后的正文;键按字母排序后序列化,比内容时不受字段先后影响 */
function bodyKey(r: Record<string, unknown>): string {
  const rest: Record<string, unknown> = { ...r };
  for (const k of ENVELOPE_KEYS) delete rest[k];
  return JSON.stringify(rest, (_k, v: unknown) =>
    v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) : v);
}

/**
 * 整组替换:把满足 `match` 的那一组记录换成 `inputs`,**只落一次盘**。
 * 带 id 的 = 改组里那条(正文没变就原样留下,updated_at 不动);不带 id 的 = 新增;组里没被提到的 = 删掉。
 *
 * 🔴 为什么要有它:逐条删、逐条加时,删完、加之前进程退出或磁盘写失败,这一组就永久残缺(旧的没了、新的没进去)。
 *    这里先整批校验,再在同一把锁里算出新文件、只写一次 —— 要么全换,要么原样不动。
 * 🔴 带的 id 必须在这一组里:不然一个调用就能顺手改掉组外的记录。
 */
export function replaceRecords(
  dataRoot: string,
  kind: string,
  match: (r: LedgerRecord) => boolean,
  inputs: readonly Record<string, unknown>[],
): { records: LedgerRecord[]; saved: LedgerRecord[]; removed: string[] } {
  const file = fileOf(dataRoot, kind);
  const fn = validatorFor(kind);
  const items = inputs.map((input, i) => {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new LedgerError("bad_record", `第 ${i + 1} 条不是对象`);
    const id = input.id;
    if (id !== undefined && (typeof id !== "string" || !id)) throw new LedgerError("bad_id", `第 ${i + 1} 条:id 必须是非空字符串`);
    const rest: Record<string, unknown> = { ...input };
    for (const k of ENVELOPE_KEYS) delete rest[k];
    if (!fn(rest)) throw new LedgerError("bad_record", `第 ${i + 1} 条:${explain(fn)}`);
    return { id: id as string | undefined, rest };
  });
  const ids = items.flatMap((x) => (x.id ? [x.id] : []));
  if (new Set(ids).size !== ids.length) throw new LedgerError("bad_record", "同一条记录在这一批里出现了两次");
  return withLock(file, () => {
    const data = loadFile(file, kind);
    const group = new Map(data.records.filter(match).map((r) => [r.id, r]));
    const taken = new Set(data.records.map((r) => r.id));
    const now = Date.now();
    const base = createdAfter(data.records, now);
    const saved: LedgerRecord[] = [];
    const next = items.map(({ id, rest }, i) => {
      const at = nowIso(new Date(base + i));
      if (id !== undefined) {
        const prev = group.get(id);
        if (!prev) throw new LedgerError("not_found", `这一组里没有这条记录:${id}`);
        if (bodyKey(prev) === bodyKey(rest)) return prev;
        const r = { ...rest, id, kind, created_at: prev.created_at, updated_at: updatedAfter(prev.updated_at, at, now) } as LedgerRecord;
        saved.push(r);
        return r;
      }
      let fresh = newId(kind);
      for (let n = 0; taken.has(fresh) && n < 5; n++) fresh = newId(kind);
      if (taken.has(fresh)) throw new LedgerError("id_collision", "生成 id 连续撞车,请重试");
      taken.add(fresh);
      const r = { ...rest, id: fresh, kind, created_at: at, updated_at: at } as LedgerRecord;
      saved.push(r);
      return r;
    });
    const byId = new Map(next.map((r) => [r.id, r]));
    const removed = [...group.keys()].filter((id) => !byId.has(id));
    if (!saved.length && !removed.length) return { records: next, saved, removed };
    // 组外的、留下的原位不动,新增的接在末尾
    data.records = data.records.flatMap((r) => (!group.has(r.id) ? [r] : byId.has(r.id) ? [byId.get(r.id)!] : []));
    data.records.push(...next.filter((r) => !group.has(r.id)));
    saveVerified(file, kind, data, (rs) => next.every((a) => sameRecord(rs.find((r) => r.id === a.id), a)) && !removed.some((id) => rs.some((r) => r.id === id)));
    return { records: next, saved, removed };
  });
}

/** 删除一条。返回是否真的删掉了 —— 调用方要能区分"删了"和"本来就没有" */
export function removeRecord(dataRoot: string, kind: string, id: string): boolean {
  if (typeof id !== "string" || !id) throw new LedgerError("bad_id", "id 必须是非空字符串");
  const file = fileOf(dataRoot, kind);
  return withLock(file, () => {
    const data = loadFile(file, kind);
    const before = data.records.length;
    data.records = data.records.filter((r) => r.id !== id);
    if (data.records.length === before) return false;
    saveVerified(file, kind, data, (rs) => !rs.some((r) => r.id === id));
    return true;
  });
}

/** 全部种类的记录(界面一次性拉取用) */
export function listAll(dataRoot: string): Record<string, LedgerRecord[]> {
  // 用无原型对象:种类名由插件给,普通 `{}` 上写 `constructor` / `__proto__` 这类键会碰到原型
  // (前者虽能建自有属性但读起来歧义,后者会触发 setter)。注册期已挡住多数,这里再断一次根。
  const out = Object.create(null) as Record<string, LedgerRecord[]>;
  for (const k of Object.keys(kinds())) out[k] = listRecords(dataRoot, k);
  return out;
}

/** 全部种类里不合契约的记录(界面据此提示"这几条要修",而不是装作没看见) */
export function listAllIssues(dataRoot: string): Record<string, LedgerIssue[]> {
  const out = Object.create(null) as Record<string, LedgerIssue[]>;
  for (const k of Object.keys(kinds())) {
    const issues = listRecordsChecked(dataRoot, k).issues;
    if (issues.length) out[k] = issues;
  }
  return out;
}

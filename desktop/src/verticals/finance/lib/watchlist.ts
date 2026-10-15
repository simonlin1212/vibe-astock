/**
 * 自选股 —— **存在用户自有台账里**（`watch` 种类），不是浏览器缓存。
 * 理由与研究记录相同：清缓存不该让自选消失，而且 agent 要读得到。
 *
 * ⚠️ 读同步 / 写异步，缓存在 React 挂载前灌好（见 `main.tsx`）。
 */
import { backend, type LedgerRecord } from "./backend.ts";
import { normalizeMarketSymbol } from "./marketSymbol.ts";

let cache: string[] = [];
/** 台账里记住的 名称 → 行情没回来时兜底显示（用户导入过名称的股票不该显示成代码）。
 *  只在 hydrateWatch 提交，与 cache 同步刷新；其他路径不许顺手改它。 */
let names = new Map<string, string>();
/** 已经排进队、还没写进台账的「加一只」：重读台账时补回缓存，别的页面这时读到的列表里也有它 */
const pending = new Set<string>();

/** 台账里当前的 代码 → 记录 id / 名称。同一代码有多条时保留第一条，其余当重复处理。
 *  纯查询：不碰任何模块级状态（names 的提交只发生在 hydrateWatch）。 */
async function currentIds(): Promise<{ ids: Map<string, string>; dupes: string[]; names: Map<string, string> }> {
  const led = await backend.ledger();
  const rows: LedgerRecord[] = led.records.watch ?? [];
  const ids = new Map<string, string>();
  const dupes: string[] = [];
  const fresh = new Map<string, string>();
  for (const r of rows) {
    const c = normalizeMarketSymbol(r.symbol);
    if (!c) continue;
    const nm = typeof r.name === "string" ? r.name : null;
    if (nm && !fresh.has(c)) fresh.set(c, nm);
    if (ids.has(c)) dupes.push(r.id);
    else ids.set(c, r.id);
  }
  return { ids, dupes, names: fresh };
}

/** 台账里该代码的名称；没有则 null（调用方再往代码兜底） */
export function watchNameOf(code: string): string | null {
  return names.get(code) ?? null;
}

/**
 * 🔴 慢快照不许覆盖新缓存:hydrate 在途时若有写入完成,先发的那次 hydrate 会带回
 *    **不含新记录的旧快照**,落地后表现为"刚存的东西不见了"。用序号丢弃过期结果。
 */
let seq = 0;
export async function hydrateWatch(): Promise<void> {
  const mine = ++seq;
  const { ids, names: fresh } = await currentIds();
  if (mine !== seq) return;   // 期间又发生了一次 hydrate,以更晚那次为准
  cache = [...ids.keys(), ...[...pending].filter((c) => !ids.has(c))];
  names = fresh;
}

/** ⚠️ 返回**副本**:直接交出内部数组的话,调用方一个 `sort()` / `push()` 就改了缓存而没写台账 */
export function loadWatch(): string[] {
  return [...cache];
}

/**
 * 把列表写回台账（差集增删）。
 *
 * 🔴 **每次都以台账的真实状态算差集，不信任内存里的映射。**
 *    信内存的话，只要那份映射不是最新的（模块被重新加载过、另一个标签页写过），
 *    "已经有了"就会判成"还没有" ⇒ **静默写出重复记录**。
 *
 * 同一页面里的保存会排队，避免「先加 AAPL、紧接着加港股」时旧保存反过来删掉新代码。
 * ⚠️ 不同浏览器标签页仍是两个 JS 进程；真正的跨标签唯一性要在台账侧加唯一约束。
 * ⚠️ 中途失败时台账已经被改了一半 ⇒ **缓存一律以重读结果为准**，
 *    不能停在"写之前"或"想写成"的状态：那两种都会让界面显示一个从未存在过的列表。
 */
let saveQueue: Promise<void> = Promise.resolve();

async function writeWatch(want: string[]): Promise<void> {
  try {
    const { ids, dupes } = await currentIds();
    for (const id of dupes) await backend.ledgerDelete("watch", id); // 顺手清掉历史重复
    for (const c of want) {
      if (!ids.has(c)) await backend.ledgerSave("watch", { symbol: c });
    }
    for (const [c, id] of ids) {
      if (!want.includes(c)) await backend.ledgerDelete("watch", id);
    }
  } finally {
    // 成功也好、写到一半炸了也好,缓存都刷成台账此刻的真实内容
    await hydrateWatch().catch(() => { /* 连重读都失败:保留旧缓存,错误由上面抛出去 */ });
  }
}

/**
 * 加一只（排队、增量）。轮到它时读台账的最新内容再追加，**不拿缓存拼整张列表去 saveWatch**：
 * 缓存要等前一次写完、重读回来才更新，连着加两只时第二次拼出的列表里没有第一只，
 * 而 saveWatch 会删掉列表外的记录 ⇒ 刚加的上一只被删（Codex 审计 2026-10-09 C3）。
 */
export function addWatch(code: string): Promise<void> {
  const c = normalizeMarketSymbol(code);
  if (!c) return Promise.reject(new Error(`代码不对：${code}`));
  const before = cache.includes(c);
  pending.add(c);
  if (!before) cache = [...cache, c];
  const work = saveQueue.catch(() => undefined).then(async () => {
    let confirmed = false;
    try {
      const { ids } = await currentIds();
      if (!ids.has(c)) await backend.ledgerSave("watch", { symbol: c });
      confirmed = true;
    } finally {
      pending.delete(c);
      await hydrateWatch().catch(() => {
        // 连重读都失败：这一只没确认写进去，就从缓存撤掉（加之前就在的不动）。
        // 不撤的话页面按缓存显示「已在自选」、按钮锁死，恢复连接后也没法重试（审计第二轮，C3 修复引入）。
        // 写了一半才断（其实存上了）也没关系：重试时先读台账，已经有了就不再写
        if (!confirmed && !before) cache = cache.filter((x) => x !== c);
      });
    }
  });
  saveQueue = work.catch(() => undefined);
  return work;
}

export function saveWatch(codes: string[]): Promise<void> {
  const want = [...new Set(codes.map(normalizeMarketSymbol).filter((c): c is string => c !== null))];
  const work = saveQueue.catch(() => undefined).then(() => writeWatch(want));
  // 队列自身吞掉失败以便后续保存继续；本次调用仍拿到 work 的原始成功 / 失败。
  saveQueue = work.catch(() => undefined);
  return work;
}


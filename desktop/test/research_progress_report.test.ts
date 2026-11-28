import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// vibe-astock #30:研究跑完,进度卡上直接给「查看报告」,不用再去归档里找
const src = readFileSync(new URL("../src/verticals/finance/pages/Research.tsx", import.meta.url), "utf8");

test("#30 进度卡:跑完且还没打开这次的报告时给「查看报告」,点了走 openReport(和归档同一条路)", () => {
  const card = src.slice(src.indexOf("{active && ("), src.indexOf("研究阶段索引"));
  assert.ok(card.includes("查看报告"), "进度卡上要有入口");
  assert.match(card, /active\.finished_at !== null && report\?\.run_id !== active\.run_id/, "跑完才给、已打开就不重复给");
  assert.match(card, /onClick=\{\(\) => void openReport\(active\.run_id\)\}/, "点了等于从归档打开");
});

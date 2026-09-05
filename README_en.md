<p align="center"><a href="README.md">简体中文</a> | <b>English</b></p>

<h1 align="center">Vibe-Astock</h1>

<p align="center">
  <b>A daily review dashboard for A-share short-term traders — open it and you can see today's sentiment</b><br>
  Derived sentiment metrics · Runs entirely on your machine · Works with a local CLI subscription, no API key needed
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/react-19-61DAFB.svg?logo=react&logoColor=white" alt="React">
  <img src="https://img.shields.io/badge/tests-533%20passing-brightgreen.svg" alt="Tests">
  <img src="https://img.shields.io/badge/version-v0.2.1-orange.svg" alt="Version">
</p>

<p align="center">
  <a href="#what-it-is">What it is</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#whats-in-this-version">This version</a> ·
  <a href="#the-core-derived-sentiment-metrics">Derived metrics</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#custom-analysis-style-prompt-packs">Custom style</a> ·
  <a href="#quick-start">Quick start</a> ·
</p>

<p align="center">
  <b>⚠️ This project only organises, aggregates and comments on public market data at the market and
  sector level. It does not recommend individual stocks, predict prices, or tell you when to buy or sell.<br>
  It is not investment advice, and it provides no investment service.</b>
</p>

---

## Open to AI Roles in Shenzhen

The author is open to AI roles in Shenzhen, particularly in **AI-powered investment research products, Forward Deployed Engineering (FDE), and AI consulting or solutions** at Tencent, other leading technology companies, and financial institutions.

He combines experience in financial institutions with hands-on AI product development, building open-source market data tools and multi-agent systems with **17K+ GitHub stars**.

Contact: [simonlin0423@gmail.com](mailto:simonlin0423@gmail.com)

---

## What it is

**It finishes today's review for you.**

A short-term daily review means going through the limit-up pool, the consecutive-board ladder,
the Dragon-Tiger list, sector money flow and theme attribution — and then working out whether
yesterday's limit-up stocks made money today. That's an hour of the same work every day.
Vibe-Astock automates it: pull the data → five analysts each cover one aspect of the market →
converge into one market read you can actually sit down with.

A daily review is **factual work** (getting straight what happened today), so it never needed
recommendations in the first place. That is why nine tenths of the screen is the **hard-metric layer** —
money effect / promotion rate / ladder structure / sentiment cycle / loss effect / seal quality /
theme event tree / historical percentile (all defined in the glossary below). **Every one of those is
pure computation straight from the data source; no AI touches them, and they are on screen the
moment the page loads.** The AI's job is to turn seven or eight data
sources into one readable story, not to pick stocks.

**What it does not do:** no stock recommendations, no "should you join in", no entry or exit levels.
Individual stocks are only ever stated as facts (which theme it belongs to, how many consecutive
boards, which brokerage desks showed up on the Dragon-Tiger list, where it sits on the chart). Directional and
sentiment calls stop at the sector level.

### A quick glossary

A-share short-term trading has its own vocabulary and most of it has no English equivalent,
so here is the whole set up front. Chinese terms are given so you can match them to the UI.

| Term used here | 中文 | What it means |
|---|---|---|
| **limit up** | 涨停 | The daily upper price limit (+10% on the main boards, +20% on ChiNext/STAR, +5% for ST names). A stock that closes there is "at limit up" |
| **sealed** | 封板 | Buy orders are stacked at the limit and the price stays locked there. "Sealed at 09:31" = locked from 09:31 on |
| **broken board** | 炸板 | It was sealed at the limit during the day, then the seal broke and it traded below |
| **board / N boards** | 板 / N连板 | One "board" = one limit-up close. "3 boards" = three consecutive limit-up closes. A "first board" is day one of a streak |
| **promotion** | 晋级 | A stock on an N-board streak hits limit up again the next day, so it moves to N+1. The **promotion rate** is what share of a tier managed that. Again a literal translation — read it as "continuation rate" |
| **money effect** | 赚钱效应 | How yesterday's limit-up stocks did today. There is no standard English name for it — this is the Chinese term translated literally. It is the single best read on whether chasing strength is currently paying |
| **loss effect** | 亏钱效应 | The same batch, measured by damage: how many fell more than 5% / 7%, how many hit limit down |
| **leader** | 龙头 | In this dashboard: the stock with the highest board count that day (ties are broken by source order, not by judgement). Traders use 龙头 more loosely than that. Shown as a sentiment gauge, not a buy list |
| **settled record** | 定稿记录 | End-of-day data for a session that has already closed, cached to disk. Facts that will not change again, so past sessions never depend on live quotes |

---


## Screenshots

### Reading the market

**Review dashboard** — the hard-metric layer, there the moment you open it. Market breadth as the
denominator, where yesterday's strong names landed today, money effect and loss effect side by side.

<img src="assets/screenshots/01-review-metrics.png" alt="Review dashboard: market breadth, yesterday's strong-stock feedback, money effect, loss effect">

**Tomorrow's verification conditions** — turns tonight's read into something you can actually check
tomorrow: every line carries today's baseline and the threshold a move has to clear to count.

<img src="assets/screenshots/02-verification.png" alt="Tomorrow's verification conditions, each with today's baseline and its threshold">

**Limit-up sample stats** — among first-time limit-ups, *when* the seal finally held changes next-day
expectancy by a wide margin. ⚠️ The sample bias sits pinned at the top of the page: this is a list
compiled with hindsight about which names sealed, so real expectancy is necessarily lower.

<img src="assets/screenshots/07-backtest.png" alt="Limit-up sample stats: seal-time curve and the sample-bias disclosure">

**Market data** — indices, overnight markets (including the Magnificent Seven), sector money flow,
turnover ranking, and today's live limit-up sentiment. Auto-refresh optional.

<img src="assets/screenshots/03-market-data.png" alt="Market data: indices and overnight markets">

**5-day heat + leader lineage** — how sentiment moved over five sessions, and where the leaders from
a few days ago have retraced to.

<img src="assets/screenshots/04-heat.png" alt="5-day sentiment heat and leader lineage">

### Reading yourself

> The trades in the next two shots are **sample data**, there only to show the interface.
> Your own ledger never leaves your machine.

**Trade journal** — every trade is pinned to the market conditions of that day, then grouped for
self-review by sentiment phase, playbook, whether you followed your plan, board count and holding
period. Small samples are labelled as such.

<img src="assets/screenshots/05-journal.png" alt="Trade journal: entry form and five-dimension self review">

**Capital at risk** — computed from the stops *you* wrote down, and summed rather than read per
trade. Positions with no stop are listed separately instead of counted as zero, which is why the
total is labelled a floor.

<img src="assets/screenshots/06-at-risk.png" alt="Capital at risk: bounded total and positions with no stop">

### On-demand analysis

**Stock deep-dive** — four analysts, a two-sided debate, then a judge. Description and risk
disclosure only; it never takes a stance on participating.

<img src="assets/screenshots/08-deepdive.png" alt="Stock deep-dive: theme / money flow / technical columns with risk disclosure">

---

## What's in this version

**v0.2.0** — fourteen entries in the sidebar:

| Entry | What it is |
|---|---|
| **Review dashboard** | The main page and the default landing page. AI narrative on top, hard metrics below |
| Market data | Indices / sector money flow / money rotation / active stocks |
| First-board analysis | Today's first-board names — when each one first sealed, and how many times the seal broke |
| 5-day heat | How theme heat moved over the last 5 trading days |
| **Watch** | A live event stream during market hours: sharp moves, seals and breaks — objective events only |
| **Holdings** | **A view over the trade journal** (not a second ledger); an unfetchable quote is labelled, never shown as zero |
| **Watchlist** | Names you follow, sharing the same local data as the watch page |
| **Stock data** | Quotes, valuation, earnings snapshot and research list for a single name |
| **News radar** | Multi-source news aggregation |
| **Intraday check** | 09:25 auction check plus an intraday sentiment path across six time slots, captured automatically |
| **Stock deep-dive** | An on-demand single-stock agent: four analysts, a two-sided debate, then a judge — description only |
| **Limit-up sample stats** | How yesterday's limit-up names performed the next day, split by sentiment regime; with raw-data archiving and drift detection |
| **Trade journal** | Log your own trades, pinned to that day's market conditions; grouped self-review plus account risk and execution drift |
| Connect AI | Set an API key or point it at a local CLI (see below) |

### Watch and watchlist (new)

- **Watch** — refreshes every 3 seconds during market hours (the practical ceiling for L1
  snapshots) and turns sharp moves, seals and breaks into a stream of **objective events**; the
  same event on the same name is reported at most once per 5 minutes. It stops outside market hours.
- **Holdings / Watchlist** — your own entries, stored only on your machine. ⚠️ When a quote can't
  be fetched the backend returns `price=0`, which would render as "the whole position went to zero,
  down 100%" — **entirely false**. So availability is checked per row and labelled honestly
  ("quote unavailable — not actually zero"), the totals are marked incomplete, and **the context
  sent to the AI goes through the same check** — otherwise asking the AI would return an analysis
  built on "you lost everything".
- **Stock data / News radar** — quotes, valuation and earnings for a single name, plus aggregated news.

The rankings (consecutive limit-ups, turnover, …) are objective public data. The page states
facts; it does not recommend names, predict direction, or suggest timing.

### Intraday check (new)

A tool that only does post-close reviews loses you by the next morning: last night it called a
cooling phase — did that hold up at the open? This page adds two things:

- **09:25 auction check** — whether yesterday's limit-up names opened higher or lower overall,
  auction strength by board count, what the highest-streak name is doing, and whether last night's
  verification conditions are showing early signs.
- **Intraday sentiment path** — one snapshot each at 09:25 / 09:35 / 10:00 / 11:30 / 14:00 / 15:00.
  Closing with 40 limit-ups after starting at 60 and breaking down is the opposite of closing with
  40 after starting at 15 and spreading — only the path shows the difference.

⚠️ **A snapshot missed is gone for good** (it reads live quotes), so a background thread captures
them on schedule rather than waiting for someone to click. ⚠️ More importantly: once the moment
has passed, the system **will not pass off the current quote as that moment's snapshot** — it says
plainly that the 09:25 snapshot is missing and that you are now more than 8 minutes past it,
rather than handing you a plausible-looking fake. Scheduling uses Shanghai time, so the machine
can sit in any timezone.

The page only produces market-level aggregates (how many opened up or down, averages by board
count, overall strength of the leading themes). Individual names are listed as objective readings
only — never ranked, scored, or given a stance.

### Stock deep-dive (new)

The other entries look at the whole market; this one takes **a single stock** and runs on demand
(about 2-3 minutes): four analysts — theme, money flow, technicals, risk — then a for-and-against
debate, then a judge that pulls it together. You can keep asking follow-up questions underneath.

⚠️ **The same boundary as the review, applied more strictly**: description and risk disclosure
only — no stance on whether to participate ("worth watching" / "avoid"), no entry or exit levels,
no timing. That wording lives in the prompt pack; swap in your own and it is still the pack that
constrains the engine, never hard-coded logic.

Two guardrails: if even the quote can't be fetched (halted or invalid code) it **returns an error
instead of burning seven LLM calls**, and any text pulled in from outside is marked untrusted all
the way through to the judge.

### Limit-up sample stats (new)

Runs a few classic short-term setups over the last 20 / 30 / 60 / 90 trading days: win rate,
expectancy, distribution, daily curve, and performance **split by sentiment regime** — the same
setup often behaves completely differently in a hot phase versus a cooling one.

⚠️ **This is "market phenomenon statistics", not a strategy backtest, and certainly not
"the returns of trading limit-ups".** The three are different:

| Layer | What it is | This project |
|---|---|---|
| ① Market phenomenon stats | How a class of stocks behaved historically | **this layer** |
| ② Rule replay | Your rules replayed with fill probability and slippage | not built |
| ③ Your actual fills | What you actually got filled at | see the trade journal |

The sample comes from "stocks still in the limit-up pool at the previous close" — a list compiled
**with hindsight about which ones sealed**. It structurally excludes names that hit the limit but
failed to hold, orders that queued without filling, and gap-locked names you could never buy. Real
expectancy is necessarily lower than what you see here. This disclosure sits at the top of the
page and is not collapsible.

Two foundations live on the same page:

- **Raw data archiving** — every day the source's **verbatim response** is stored and never
  deleted (a few hundred KB per day). Caches hold derived results and can be dropped and rebuilt;
  the archive is the only thing that lets you recompute history after a definition changes.
  ⚠️ It must store the raw rows: normalised rows can neither detect field drift nor give back
  columns that were dropped.
- **Structural drift detection** — keeps three things apart: **the source changed** (field list),
  **the market changed** (window comparison of sector mix and limit-up counts), and **the rules
  changed** (price limits, the 920 board, how consecutive limit-ups are counted). Conflating the
  first two with the third means adjusting your playbook on a phantom signal.
  ⚠️ Regime events are **entered by hand only, never inferred from data** — inference is guessing,
  and a wrong guess turns market noise into a rule change.

### Trade journal (new in v0.2.0)

The four entries above are all about what the market did. This one is about what *you* did.

- **Every trade is pinned to the market conditions of the day it happened** — the sentiment phase
  and money effect at the time, plus what that stock was doing: which consecutive limit-up day it
  was on, when it first sealed, how many times the seal broke, which theme it belonged to. The buy
  day and the sell day each get their own snapshot.
- **Fills** (optional) — enter them and weighted cost, realised P&L and holding days are computed
  for you. ⚠️ Settlement walks the fills **in order**: selling more than you held at that moment
  is rejected outright rather than settled out of thin air, and buying back after a full exit
  starts a new holding cycle — **P&L that already happened is never rewritten by a later buy**.
  Cost uses a moving weighted average, matching how brokers reconcile.
- **P&L is net of fees** — commission, stamp duty and transfer fees are deducted. You can enter the
  actual fee per fill (the number on your statement) or configure your own rates and let it
  estimate; the page says which one a figure is based on. ⚠️ The default rates are a starting
  point, not a recommendation — for a high-turnover style, a pile of thin winners can land near
  break-even or below once fees are counted, and win rate and expectancy will read too high.
- **You can append fills to an open position** — write the stop down on the day you buy, come back
  the next day and add the sell. No deleting and re-entering: the original entry time and the
  evidence that "the stop was written at order time" both survive. Editing the stop afterwards is
  allowed but leaves a timestamp.
- **Planned exits** — the stop and target you wrote down *when you placed the order*. ⚠️ It only
  means anything if it was written at the time; filling it in afterwards makes the field useless.
- **Grouped self-review** — by sentiment phase, by playbook, by whether you followed your plan, by
  the stock's board count, and by holding period. Every group shows its sample size and says
  plainly when the sample is too small to conclude anything.
- **Playbook cards** — write your own trading rules down, **with versions**. Change a rule and a
  new version opens; performance is then segmented by version, so you can answer "did that last
  change actually help?". Editing a card never rewrites which version past trades belong to, and
  trades that happened before the card existed are listed separately and counted in no version.

Further down the same page there is a block on **account risk and execution drift**:

- **Capital at risk** — computed from the stops *you* wrote down: `(cost − stop) × shares`, and
  summed rather than read per trade. ⚠️ Positions with no stop go into an "unbounded" bucket and
  are reported separately — they are **never counted as zero**, which would understate the total
  while looking perfectly normal. The percentage is only shown if you entered your account size.
- **The shape of the equity curve** — not cumulative profit, but how deep and how long the
  drawdown is, how many trades since the last high, and **what is left after removing your best
  1 and best 3 trades**.
- **Rolling windows** — last 10 / 20 / 50 trades next to lifetime. When there aren't enough
  trades for a window it is greyed out and labelled, so "last 50" on a 12-trade history never
  reads as if the playbook had been validated at that scale.
- **MFE / MAE and give-back** — how far a trade went in your favour, how far against, and how
  much of the move you actually captured. ⚠️ Daily bars carry no intraday ordering, so MFE is an
  **upper bound** and capture rate is systematically understated; that caveat sits next to the number.
- **Judgement vs execution** — crosses "did last night's call hold up" with "did you make money
  that day". Right call but losing money is an execution problem; wrong call but making money is
  luck, and it gets called out on its own.
- **Anomaly inbox** — surfaces the few trades worth a second look. ⚠️ Every threshold comes from
  **your own history** (your median position size, your median holding period, the limits you
  wrote); with too small a sample those checks are skipped outright. It only says what is
  unusual, never what to do about it.
- **Risk constitution** — the limits you write yourself (max loss per trade / per day, max open
  positions, cool-off after consecutive losses…). The system only checks whether you broke
  **your own** rules; the defaults are a starting point, not a recommendation.
  ⚠️ Every rule reports whether it was actually checked: when it can't be computed (no account
  size means no denominator for daily loss) it is marked `unavailable` rather than reported as
  "0 violations" — **a rule that is configured but never checked is worse than no rule at all**.

Scope: it **only aggregates the trades you entered yourself**. No stock picking, no entry/exit
timing, no suggestion about your next trade — a ledger and a health check, not an advisor. The data
stays on your machine and ⛔ **never reaches any AI prompt** (enforced by tests).

## The core: derived sentiment metrics

The number of limit-ups is only raw material. What actually tells you the state of the market is
this handful of **derived** readings:

| Metric | What it is | Why it matters |
|---|---|---|
| **Money effect** | Yesterday's limit-up stocks today: mean / **median** / share that closed up / share that hit limit up again | 40 limit-ups but yesterday's batch is at a median of −1.8% today is an ebbing market; 25 limit-ups with a median of +4% is a healthy one |
| **Promotion rate** | Share of each board tier that hit limit up again today (1→2 / 2→3 / 3+) | **1→2 is the most sensitive**: a clear drop means the tide is going out, a rebound means sentiment is recovering |
| **Consecutive-board premium** | How yesterday's 2-board-and-above names did today | Whether buyers are still stepping in for the high-streak names, or leaving them to fall |
| **Ladder structure** | Count at each board tier + gap detection | 5-board and 2-board names but nothing at 3–4 leaves the top name isolated: when its streak ends there is no tier just below it ready to become the new leader |
| **Sentiment cycle** | A sentiment score over the last 10 trading days, locating where this round started and what day we are on | So you know where in the cycle you are standing |

> ⚠️ **The mean and the median often disagree** — a few big gainers pull the mean up.
> For "what most people are feeling", go by the median.

---

## Architecture

```
limit-up pool / Dragon-Tiger / sector money flow / theme attribution / Tencent quotes
        ↓
derived sentiment metrics + objective fact tables
        (pure computation, no AI — nine tenths of the screen)
        ↓
five short-term analysts
        (sentiment · money flow · themes · Dragon-Tiger desks · leader tracking)
        ↓
review judge → structured market read
        (sentiment phase / active directions + evidence + what would falsify them / risk notes)
```


---

## Custom analysis style (prompt packs)

The engine — data pipeline, multi-agent orchestration, reflection loop — is generic; **what gets
said, and how far it goes**, is decided by the prompt pack. Everyone's short-term framework is
different: some watch the sentiment cycle, some only trade first boards, some just follow the money.
So this layer is replaceable.

The repo ships with `RESEARCH_PACK` (observation and analysis at the market and sector level).
To swap in your own:

The easiest option is to change only the wording and keep the built-in output schema.
You can copy this and run it as is:

```python
# ~/.vibe-astock/prompts_local.py
from duanxian.prompts import PromptPack, RESEARCH_PACK

PACK = PromptPack(
    name="my-style",
    analyst_style="只讲情绪周期位置和晋级率，别的少说。",   # tone and scope for the five analysts
    analyst_len="控制在 250 字内。",
    judge_requirements="""1. 判断当前市场情绪档位（冰点/修复/发酵/亢奋/退潮）。
2. 说清这个档位的依据是哪几个读数。
3. 列出需警惕的风险信号。""",                              # what the judge must produce
    # These three go together — either use all the built-ins, or write a full set of your own
    focus_model=RESEARCH_PACK.focus_model,      # = schemas.TomorrowFocus
    focus_skeleton=RESEARCH_PACK.focus_skeleton,
    render_focus=RESEARCH_PACK.render_focus,
)
```

To replace the shape of the conclusion as well, define your own `focus_model` (a pydantic model),
`focus_skeleton` (the English-key skeleton handed to JSON mode) and `render_focus`
(model → markdown). Those three must match each other. The full field list is `PromptPack`
in `duanxian/prompts.py`; `TomorrowFocus` in `duanxian/schemas.py` is a working example to copy.

The engine finds that file on its own (or point `VIBE_ASTOCK_PROMPTS` at any path). If it fails to
load, the engine prints why and falls back to the default pack. The file lives outside this repo and
is not distributed with the code; what you write in it, how you use it, and the consequences are
yours — please also confirm what your own jurisdiction requires for this kind of activity.

---

## Quick start

```bash
# Python >= 3.10 (required by recent akshare)
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Build the frontend first (build output is not in git; the backend serves it directly):

```bash
cd frontend && npm install && npm run build
```

Configure the LLM — **two backends, use whichever you have**:

**① You have an OpenAI-compatible API key** (the default; the example here is MiMo):

```bash
# ~/.config/mimo/mimo.env
MIMO_API_KEY=...
MIMO_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5-pro
# Optional: model for the quick tier (analysts and other high-frequency nodes); defaults to mimo-v2.5
# MIMO_QUICK_MODEL=mimo-v2.5
```

It does not have to be MiMo: any OpenAI-compatible endpoint works. For DeepSeek, point `MIMO_BASE_URL`
at `https://api.deepseek.com/v1` and set `MIMO_MODEL=deepseek-reasoner`, `MIMO_QUICK_MODEL=deepseek-chat`
(fill in both tiers with that vendor's model names, otherwise the quick tier still asks for the default
`mimo-v2.5` and gets a 404).

**② You only have a Claude / Codex subscription and no API key**: use the CLI you are already
logged into on this machine. No key needed.

```bash
VIBE_LLM_CLI=claude .venv/bin/python server.py
```

> ⚠️ To run a CLI **other than `claude`** on the server, you also have to set
> `VIBE_ALLOW_UNSAFE_CLI=<the same one>` explicitly. Not opening that automatically is deliberate:
> it would also open up "ask the AI" in the browser, and that path puts fetched news text straight
> into the prompt, which is a far larger injection surface than the review itself. The `claude`
> branch runs with `--disallowedTools`, which is enough for one-off questions.
> Running the review on its own with `python main.py` does not need the second switch.

<details>
<summary><b>On Windows (the commands above are macOS / Linux)</b></summary>

Two things differ on Windows: the interpreter lives at `.venv\Scripts\python`
(not `.venv/bin/python`), and environment variables cannot be prefixed onto the
command — set them separately first.

**PowerShell** (recommended):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# The frontend must be built first, or the page returns 503 (frontend/dist is gitignored)
cd frontend; npm install; npm run build; cd ..

# (1) With an API key: write mimo.env as above, at $env:USERPROFILE\.config\mimo\mimo.env
# (2) With a local CLI subscription:
$env:VIBE_LLM_CLI = "claude"
.venv\Scripts\python server.py
```

**CMD**:

```bat
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt

rem The frontend must be built first, or the page returns 503
cd frontend && npm install && npm run build && cd ..

set VIBE_LLM_CLI=claude
.venv\Scripts\python server.py
```

For a CLI other than `claude` you still need the second switch (PowerShell:
`$env:VIBE_ALLOW_UNSAFE_CLI = "codex"`; CMD: `set VIBE_ALLOW_UNSAFE_CLI=codex`).

This assumes the CLI is already installed and logged in — typing `claude` in the
same terminal should drop you into it. The backend only looks it up on PATH and
reuses that session; it will not log in for you. If `claude` runs fine on its own
but the server reports "not detected" or fails to start, please paste the full
error into an [issue](https://github.com/simonlin1212/vibe-astock/issues) along
with your Windows version and how the CLI was installed (npm / installer).

</details>

Run it:

```bash
.venv/bin/python server.py          # one process, one port :8910, all fourteen entries
```
```bash
.venv/bin/python main.py            # or run today's review straight from the CLI
```

⚠️ **A review can only ever be run on a session that has already closed.** With no date given it
picks the most recent closed trading day; give it a day that has not closed yet and it will refuse,
and tell you to use that most recent closed day instead (the limit-up pool and the Dragon-Tiger list are not final
intraday, and feeding them in only makes the AI improvise). If that session has already been run,
you just read it — it is not re-run; add `?force=1` if you want it re-run.

**Past sessions are always readable**: money effect / loss effect / consecutive-board premium /
yesterday's strong-stock feedback all prefer the **settled record** — end-of-day data cached to disk once a session has closed,
falling back to Eastmoney's previous-day limit-up pool — rather than live quotes — so you can pull
up last Wednesday's review in the middle of a trading session.

Data lives in `~/.duanxian-agents/` (reviews / heat / cache / trade journal / playbook cards); the
market-data tabs use `~/.vibe-research/`. The journal itself is a single local file at
`~/.duanxian-agents/journal/trades.json` and is never uploaded.

### Environment variables

| Variable | Default | What it does |
|---|---|---|
| `VIBE_PORT` | `8910` | Backend port |
| `VIBE_LLM_CLI` | unset | Use a local CLI as the LLM (`claude` / `codex` …); set this and you need no API key |
| `MIMO_QUICK_MODEL` | `mimo-v2.5` | Model name for the quick tier in API mode; required when pointing at DeepSeek or another vendor (can also live in `mimo.env`) |
| `VIBE_ALLOW_UNSAFE_CLI` | unset | Allow CLIs other than `claude`, comma-separated (see the note above) |
| `VIBE_ASTOCK_PROMPTS` | `~/.vibe-astock/prompts_local.py` | Swap in another analysis style (see "Custom analysis style") |
| `VIBE_ALLOW_HOSTS` | unset | Add your domain here when serving under one, otherwise write requests get a 403 |
| `VIBE_MARKET_PROXY` | unset | Set to `1` when Eastmoney is reachable **only** through your proxy. Same as `VR_DATA_PROXY=1`; either works |
| `VIBE_MARKET_DIRECT` | unset | The other direction: set to `1` to force a direct connection when your proxy breaks Eastmoney and even the limit-up pool fails. ⚠️ This is **process-wide** and also turns off the proxy fallback for Eastmoney requests |
| `VR_API_KEY` | unset | Put a key check in front of the market-data endpoints |

---

## Data sources

| Source | What it provides | Key needed? |
|---|---|---|
| akshare (Eastmoney limit-up pool / Dragon-Tiger) | limit-ups · broken boards · limit-downs · board ladder · Dragon-Tiger desks | No |
| Eastmoney `push2delay` clist | sector / single-stock money flow, turnover ranking | No |
| akshare previous-day limit-up pool | **The settled record**: how yesterday's limit-ups did on the target day (the main source for money effect / loss effect / board premium / the feedback matrix) | No |
| Tencent Finance `qt.gtimg.cn` | batched live quotes (watchlist, today's live limit-up sentiment; also the fallback for the items above) | No |
| Tencent hist `stock_zh_a_hist_tx` | candles and the trading calendar (Eastmoney `push2his` is blocked on some networks, hence Tencent) | No |
| Tonghuashun iWenCai | limit-up reason themes (→ theme event tree) | **Yes** |

**Every source above is free and connects directly; the theme strings are the only one that needs a key.** The theme strings
come from Tonghuashun iWenCai and need `IWENCAI_API_KEY`; without it the app still runs, the
"theme event tree" block just says it is unavailable (the rest of the review is unaffected).
To set it, create a `.env` in the repo root with one line:

```
IWENCAI_API_KEY=your-key
```

Limit-up pool summaries for past trading days are cached to disk (`~/.duanxian-agents/cache/`) —
they are facts that will never change again, so from the second time on they cost almost nothing.

---

## Tests

```bash
.venv/bin/python -m pytest -q
```

533 cases, covering metric calculation, boundary conditions and degradation paths — with the weight
on the places where **being wrong looks exactly like being right**: the page renders as usual, the
numbers look plausible, and the conclusion is false.

One group of them guards the boundary around your personal trade data: a corrupted ledger must
raise rather than return an empty list, concurrent writes must not drop records, migrating an old
ledger must never back-fill a stop you did not write, the add endpoint must forward every field,
and **no module that feeds a prompt may import the personal-data modules**.

---


## Disclaimer

> - Everything this system produces is generated by AI and may contain errors or bias
> - This project is not investment advice; consult a properly licensed professional before making decisions
> - The author accepts no liability for any loss arising from use of this tool
> - Markets carry risk; invest with care

## Support

If you find it useful, you can buy me a coffee ☕

<p align="center">
  <a href="https://buymeacoffee.com/simonlin1212"><img src="./assets/bmc-qr.png" width="180" alt="Buy Me a Coffee"></a>
</p>

## License

Apache-2.0, see [LICENSE](LICENSE).

**Author:** Simon Lin · X [@linsizhen](https://x.com/linsizhen) · Email: [simonlin0423@gmail.com](mailto:simonlin0423@gmail.com)

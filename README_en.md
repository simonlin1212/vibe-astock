<p align="center"><a href="README.md">简体中文</a> | <b>English</b></p>

<h1 align="center">Vibe-Astock</h1>

<p align="center">
  <b>A local workspace for short-term A-share market review and tracking</b><br>
  Market observation · Evidence-based reviews · Bull–bear discussion · Historical backtesting · Local web UI
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/react-19-61DAFB.svg?logo=react&logoColor=white" alt="React">
  <img src="https://img.shields.io/badge/version-v0.2.1-orange.svg" alt="Previous public version baseline">
  <img src="https://img.shields.io/badge/status-unreleased%20web%20development-orange.svg" alt="Unreleased web development build">
</p>

<p align="center">
  <a href="#purpose">Purpose</a> · <a href="#seven-work-modules">Features</a> ·
  <a href="#quick-start">Quick start</a> · <a href="#connect-ai">Connect AI</a> ·
  <a href="#data-and-privacy">Data and privacy</a> · <a href="#validation-scope">Validation</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

This README describes the **September 9, 2026 development version, which has not been released**. The v0.2.1 badge identifies the previous public version. The new features below require the current development source.

---

## The Author Is Open to Opportunities

The author is open to AI roles at Tencent and other leading technology companies in Shenzhen, and hopes to join a team passionate about AI development. Areas of interest include AI / Agent product development, real-world deployment, and AI consulting.

Contact: [simonlin0423@gmail.com](mailto:simonlin0423@gmail.com)

## Purpose

Vibe-Astock brings recurring short-term A-share research tasks into one local web workspace: inspect the market and yesterday's limit-up cohorts, organize review evidence, check subsequent changes, and revisit personal records and historical rules.

Source data, program calculations, manual records, and AI interpretation are presented separately. Quotes and statistics do not depend on AI generation. AI explains materials, compares interpretations, and organizes conditions for later verification. Market-sentiment classifications are analytical frameworks, not objective facts; a single indicator cannot establish a trading conclusion.

Run the local web launcher to view quotes, reviews, market events, and task progress in your browser.

### Interface preview

Dark mode is the default; light mode remains available. The sidebar groups pages by workflow. Submenus start expanded and can be collapsed individually using their triangles.

![Dark web market-review interface](assets/screenshots/09-web-review-dark-20260909.png)

Captured on September 9, 2026, this screenshot shows a review of public market data for September 8 and its sample-coverage notes. It is neither a live quote display nor a performance case study.

## Seven work modules

| Primary module | Pages and main content |
|---|---|
| Home | Chat, categorized feature shortcuts, and recent reports |
| Market watch | Market data including watchlist quotes, live events, yesterday's cohorts, and intraday verification |
| Market review | Review reports, first-limit-up analysis, five-session heat, and historical statistics |
| News radar | News aggregation and event probabilities, with sources, timestamps, and coverage status |
| Stock research | Quotes, valuation, financial and research-report materials; bull–bear discussion |
| Backtesting | AI-assisted rule clarification, confirmation, program calculation, and report archiving |
| My stocks | Holdings and watchlist, trading journal, and research notes |

Connect AI is at the bottom of the sidebar. Holdings and the watchlist appear in two vertically stacked sections on one page. Holdings are derived from the trading journal rather than maintained in a second, potentially conflicting ledger.

### Market watch: today's events and yesterday's cohorts

- **Market data:** indices, breadth, fund-flow and sector readings, and quotes for the shared watchlist. Read each source's market universe, date, and units separately.
- **Live events:** observed changes such as sharp moves and limit-up breaks or reseals, grouped from quote snapshots. Page controls filter scope and thresholds. Snapshot polling is not tick-by-tick transaction data and cannot guarantee capture of every intraday change.
- **Yesterday's cohorts:** a fixed sample of the preceding trading session's limit-up stocks, with filters for all, two or more consecutive limit-up sessions, or three or more. Stocks that decline today or lack current quotes remain visible, preserving continuity and coverage gaps.
- **Intraday verification:** compares conditions recorded in a review with valid snapshots from the current session. It asks whether earlier conditions occurred; live events show what just happened. Historical sessions remain viewable through saved snapshots and finalized records. Current quotes cannot reconstruct missed historical intraday observations.

Collection requires the local service to be running and data sources to be available. Overnight, lunch-break, or stale quotes cannot stand in for valid snapshots of an active session.

### Market review: establish the date, sample, and evidence first

Choose a target trading date before generating a review, including dates without an existing report. The page distinguishes the selected date, the displayed report's trading date, and its generation time. It does not substitute an existing report's date for a new selection. Tasks support progress, cancellation, and status recovery after a refresh. Failed generation preserves the old report; regeneration saves versions, and filling in an earlier date does not move the latest-report pointer backwards.

Five review sections cover sentiment, funds, themes, exchange trading disclosures, and leading-stock developments, followed by a synthesis of evidence and conditions to verify. New reports expose their supporting materials. Numerical presentation and supported comparisons are handled by the program; AI explanations are subject to citation checks. Existing reports are retained and do not automatically acquire new validation results after an upgrade.

| Observation | What it covers | Reading boundary |
|---|---|---|
| Market breadth | Advancers, decliners, unchanged stocks, and turnover | Do not mix denominators across market universes |
| Yesterday's strong-stock feedback | Mean, median, positive-return share, and repeat limit-up share | Describes a fixed cohort, not whole-market returns |
| Continuation and cohort structure | Continuation, breaks, and distributions by yesterday's streak length | Disclose missing quotes; one reading cannot establish a sentiment cycle |
| Loss distribution and limit-up structure | Pullbacks, breaks, reseals, and sealing quality | Interpret only the samples actually covered by the source |
| Themes and cross-session comparison | Reported catalysts, theme changes, and earlier observation conditions | Attribution is an interpretation to check, not established causality |

First-limit-up analysis and five-session heat sit within Market review, providing daily structure and context across trading sessions. Missing data, source revisions, and structural changes retain explicit notices. Available raw archives support tracing, but external sources are not guaranteed to remain retrievable indefinitely.

### Stock research and bull–bear discussion

Stock research brings quotes, valuation, financial information, and research-report materials together. Bull–bear discussion organizes different analytical perspectives on the same stock, checks positive interpretations, risk interpretations, and counterevidence, then summarizes unresolved differences. It does not force a winner or provide participation ratings, entry or exit levels, or position-sizing recommendations.

Action-oriented stance and price-level fields in older records are no longer displayed as operating guidance. Old report bodies have not been automatically revalidated; the page identifies that boundary. Duration depends on sources, material volume, and the selected model, with no fixed completion-time promise.

### Historical statistics versus backtesting

**Historical statistics within Market review** describe subsequent-session outcomes and grouping differences, such as sealing time, using available limit-up samples. The lists were assembled after the event and carry selection bias. Their statistics cannot be treated as achievable trading returns.

**The separate Backtesting module** evaluates explicit historical rules: enter conditions → AI clarifies supported execution details → user confirms → deterministic engine calculates → local report. AI does not decide what the user should trade now. Within its supported scope, the report discloses market rules, the data period, costs, trades, and reasons for unfilled orders. Zero-trade results are shown honestly. Missing data and unsupported conditions are explained rather than replaced with invented test results.

Historical performance does not guarantee future outcomes. Older Hong Kong reports missing current market-rule fields should be rerun with their original conditions; their old results do not establish validation under the updated rules.

### My stocks: preserve the research process

Holdings, realized results, and costs are based on the trading journal; the watchlist is a manually maintained observation list. Missing quotes are marked unavailable and aggregates indicate incomplete coverage, rather than treating absent prices as worthless assets.

The journal supports grouped self-review using market context, recorded methods, and execution records. Account-risk calculations use conditions entered by the user. Holdings without a specified risk boundary are listed separately, so the known portion is not presented as complete account risk. Research notes can be saved, searched, and exported. Associations in these records do not establish the causes of profits or losses.

## Quick start

These steps apply to a directory **already containing this development snapshot**. They are not installation instructions for a released major version.

Requirements: Python 3.10+ (3.12 recommended), Node.js 22+ including npm. Initial setup requires internet access to download dependencies.

On macOS, double-click [启动 Vibe AStock.command](启动%20Vibe%20AStock.command), follow the setup prompts, and open the local browser UI. Keep the terminal window open; press Control+C to stop the service.

On macOS or Linux, run these steps from the repository root:

```bash
sh scripts/setup
sh scripts/doctor
sh scripts/start
```

On Windows 10/11, double-click [启动 Vibe AStock.cmd](启动%20Vibe%20AStock.cmd), or run these commands from the repository root in PowerShell or CMD:

```powershell
py -3 -X utf8 scripts/manage.py setup
.venv\Scripts\python.exe -X utf8 scripts/manage.py doctor
.venv\Scripts\python.exe -X utf8 scripts/manage.py start
```

Windows uses the same local browser interface. Install Python (including the `py` launcher) and Node.js first. Keep the launch window open and press Ctrl+C to stop. Add `--port 8911` after `start` to change ports. Rerun `setup` and `doctor` after updating the source.

The default address is `http://127.0.0.1:8910`, bound to the local loopback interface. If the port is occupied, use `sh scripts/start --port 8911`. Development previews may use other ports; the interface does not depend on a particular development port.

Open Connect AI to sign in or enter your own API configuration, test it, and save it. Viewing quotes and existing records does not require generating an AI report first. Starting the service does not automatically generate a review. Connection tests and AI tasks may consume the selected service's quota.

Before updating source code, stop the service and back up the data listed below. Preserve local modifications; after updating, rerun `sh scripts/setup` and `sh scripts/doctor`. Startup logs are in `.local/startup.log`; check for personal information before sharing logs.

The local backend has startup paths for macOS, Linux, and Windows; AI access no longer rejects Windows by platform. Local regression tests have been run on macOS. Native Windows automated tests are included in the workflow but still await execution and acceptance; Linux also lacks full physical-machine acceptance. Each provider still requires a connection test using your own account or key.

## Connect AI

Web AI entry points share the source and model tested and saved in Connect AI. A displayed source label does not establish that the account can still make requests; use connection-test and task results to check that.

| Connection | Implementation and verification boundary |
|---|---|
| Codex subscription | Product-specific ChatGPT authorization and isolated engine; real login, connection, and business runs have been recorded. Model availability depends on account quota |
| Claude subscription | Official local CLI integration, with real review and discussion runs recorded. Requires the user's installation, login, and valid subscription; past success does not establish current account status |
| WorkBuddy / CodeBuddy | Adapter available; **real business validation is not complete**. |
| OpenAI API | Uses the user's own configuration. Subscription tests do not substitute for API validation; the full compatibility assessment is incomplete |
| DeepSeek / MiMo | Editable model and endpoint presets; use your own key and pass the connection test before saving |
| GLM / Kimi / Qwen | Alibaba Cloud Bailian presets require the workspace endpoint and a Bailian key; availability must be tested with your account |
| SiliconFlow / MiniMax / OpenRouter / Groq / Together / custom | Editable endpoint and model settings; presets do not establish verified compatibility. Endpoints must support the Responses API and tool calling |

Agent mode defaults to off: ordinary chat does not enable tools or network access. When enabled, applicable entry points use only their permitted evidence or public-data tools. Existing review evidence on Home, page materials, and explicitly allowed historical daily prices have distinct scopes; this is not unrestricted access to the internet or local files. Reviews, discussions, and backtests still require explicit initiation rather than being triggered by the switch.

Every web AI source receives the research constraints. Ordinary chat and report paths check for explicit trading recommendations; evidence answers also undergo citation and numerical checks. These checks reduce violations and citation errors but **do not guarantee complete semantic correctness**. Product instructions prohibit investment decisions, trading recommendations, and position-sizing directives. Buy and sell descriptions in historical rules are for simulation, not present-day operating advice.

The legacy `main.py` CLI and executable Python prompt packs remain for development maintenance; see [PromptPack](duanxian/prompts.py). Their separate configuration is not the web setup procedure, and custom code is outside the official web constraints guarantee.

## Data and privacy

Quotes, limit-up pools, exchange disclosures, sectors, news, and event probabilities come from public interfaces or user-configured sources. An iWencai key is an optional supplement: its absence should not imply that the entire review is unavailable. Public catalyst sources may also omit markets or historical dates. Event probabilities from sources including Kalshi and Polymarket carry retrieval times, cache ages, and partial-coverage status. They neither establish that an event has occurred nor represent a stock-price forecast by the product.

The local web app still uses network connections to retrieve data. When remote AI is used, questions, selected materials, and necessary context are sent to the chosen supplier. Local storage does not mean all processing happens offline.

| Data | Default location and considerations |
|---|---|
| Agent conversations, tasks, evidence ledger, and product login | `~/.vibe-astock-agent/`; `ASTOCK_AGENT_HOME` changes this root |
| Reviews, versions, trading journal, and raw archives | `~/.duanxian-agents/`, including `reviews/`, `journal/`, `archive/`, and related directories |
| Market materials and watch caches | `~/.vibe-astock-agent/market-data/`; configurable through `VR_DATA_DIR` |
| Research-report materials | The market-data directory's `myreports/` by default; independently configurable through `VR_REPORTS_DIR` |
| Watchlist, research notes, UI state, and API configuration | Local storage for the current browser origin; changing browser or port, or clearing site data, affects access |
| Development logs and runtime diagnostics | Repository `.local/`, excluded from version control by default |

These locations are not migrated by one setting: changing `ASTOCK_AGENT_HOME` does not automatically move the journal, market materials, or browser data. Backups should cover the actual directories in use and records exportable from the UI. Protect any login data and keys they contain. Do not commit private records, API keys, or login files, or copy a development assistant's login file as a substitute for product authorization.

## Architecture and development

```text
Public data → Raw archives and normalized definitions → Deterministic statistics / simulation
                                      ↓
                         Constrained AI interpretation and evidence tools
                                      ↓
                         Citation validation → Report versions and follow-up
Manual journal and research notes → Local records and self-review views
```

| Directory | Responsibility |
|---|---|
| `frontend/` | React web UI, seven-module navigation, themes, and interactions |
| `server.py`, `vr/` | Local HTTP service and market-page data interfaces |
| `review_agent/`, `runtime/` | Shared model access, isolated engine, tasks, evidence tools, and research constraints |
| `duanxian/` | Short-term indicators, reviews, verification, archives, journal, and bull–bear discussion |
| `backtest/`, `research_data/` | Historical simulation engine, supporting data retrieval, and event probabilities |
| `scripts/`, `tests/`, `frontend/test/` | Setup, diagnostics, startup, and regression validation |

Provenance and licensing for the event-probability and backtesting code are recorded in the [backtesting NOTICE](backtest/NOTICE.md) and [data NOTICE](research_data/NOTICE.md).

## Validation scope

Validation of the September 9, 2026 development version passed **1,237 backend tests** across three directories, **36 frontend tests**, type checking, and a production build. Dependency-deprecation and build-size warnings remain.

Evidence includes selected real-date reviews, date switching, page questions, and A-share, Hong Kong, and US historical backtests with independent recalculation. **Continuous observation over a complete live trading day, compatibility acceptance for all providers and platforms, and formal release checks remain incomplete.** Offline regression tests, individual real runs, and screenshots cannot substitute for those checks.

With the repository environment prepared, run:

```bash
.venv/bin/python -m pytest tests backtest/tests research_data/tests -q
node --test frontend/test/*.test.ts
npm --prefix frontend run build
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for product updates.

## Disclaimer

This tool is for organizing data, research, and historical review, not investment decisions or trading instructions. Public data, calculations, manual records, and AI interpretations may contain errors. A citation does not establish a conclusion, and historical results do not establish future performance. Investing carries risk; verify materials independently and exercise your own judgment.

## Support

If this project is useful, you can buy me a coffee ☕

<p align="center">
  <a href="https://buymeacoffee.com/simonlin1212"><img src="./assets/bmc-qr.png" width="180" alt="Buy Me a Coffee"></a>
</p>

## License

Apache-2.0. See [LICENSE](LICENSE).

**Author:** Simon Lin · X [@linsizhen](https://x.com/linsizhen) · Email: [simonlin0423@gmail.com](mailto:simonlin0423@gmail.com)

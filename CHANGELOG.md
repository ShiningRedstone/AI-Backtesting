# Changelog

Status labels: **IMPLEMENTED** (code exists) · **TESTED** (covered by automated tests) ·
**NOT IMPLEMENTED** (deliberately absent) · **REQUIRES REAL DATA** (cannot be validated on synthetic data).

## Web UI visual redesign (presentation only, version unchanged 0.2.0)

| Item | Status |
|---|---|
| Design tokens moved from navy/mint to near-black charcoal with hairline borders; silver primary pill; warm accent decorative only (one featured panel); `--ok` separated from the action colour; status badges, banners and toasts carry icons | IMPLEMENTED, TESTED (browser e2e) |
| Self-hosted Inter (variable, opsz) and JetBrains Mono with OFL texts (previously named but never loaded); `build.mjs` copies `web/src/fonts/` to `static/fonts/` | IMPLEMENTED, TESTED |
| Home hero (display type, static glow/grid), +/− accordions, empty-state icon tile, full-value hover titles on truncated hashes, `TableWrap className`, `Mono title` | IMPLEMENTED |
| Backend, API, services, calculations, identities, datasets, labels and disclaimers | UNCHANGED |

## Research-terminal UI + Windows auto-updater, version 0.2.0 (ADR-58, ADR-59)

| Item | Status |
|---|---|
| Dark navy/mint design system, grouped terminal navigation, SVG chart kit (CVD-validated palette), KPI tiles, scope/basis tags, drawer/modal/pager/sortable-resizable columns | IMPLEMENTED, TESTED (browser e2e) |
| Home overview: system facts, ACTIVE protocol budgets, dataset identity (Dukascopy CFD, not CME NQ), execution sides + cost scenario, latest results, experiments, candidates, warnings | IMPLEMENTED, TESTED |
| Research dashboard: breakdowns by market/TF/session/entry/stop/target/direction/family/source, distributions, calendar effects, cost share (scope- and basis-labelled) | IMPLEMENTED, TESTED |
| Strategy explorer: server-side filter/sort/paging, URL-persistent filters, detail drawer (rules in plain English, performance, equity, trades, robustness, pipeline) | IMPLEMENTED, TESTED |
| Run page: integrity/provenance section and full per-run analytics; Experiments protocol budget; Controls page (holdout controls with their stored exact MC p-value; ad-hoc controls descriptive); candidate pipeline; prop simulated paths and summary; Settings & About | IMPLEMENTED, TESTED |
| Read-only read models + routes (`research/overview.py`); holdout-evaluation runs always labelled Holdout, never counted as OOS | IMPLEMENTED, TESTED |
| One authoritative version (`edgelab.__version__` = 0.2.0) enforced across UI bundle, build manifest, exe metadata and release | IMPLEMENTED, TESTED |
| Updater: GitHub Releases manifest, check / Later / persistent Skip / Update now, staged download, size + SHA-256 verification, safe extraction, helper-process swap with rollback, update log, offline-safe | IMPLEMENTED, TESTED (fixtures; Linux) |
| `packaging/release.py` (artifact + manifest + checksums; never publishes) | IMPLEMENTED, TESTED |
| Fix: launcher loopback calls bypass any `HTTP(S)_PROXY` (start-up failed behind an environment proxy) | IMPLEMENTED, TESTED |
| Helper relaunches the unchanged previous version when the swap fails (e.g. a locked file on Windows) | IMPLEMENTED, TESTED (unit); packaged check Windows-only |
| Fix: the helper waits for the relaunched version to REPORT READY (`EDGELAB_UPDATE_READY_FILE`, written after its /api/health answered) instead of "still alive after 8 s", so a start-up error dialog is no longer mistaken for success; not ready -> stopped, kept as `.failed-*`, previous restored and relaunched, `update_failed` | IMPLEMENTED, TESTED (unit; packaged Linux build) |
| `packaging/windows_validation.py` (Windows validation orchestrator: real-data manifests, git-status preservation, statuses, verdict) | IMPLEMENTED, TESTED (Linux dry run) |
| Validation fixes from the first Windows run: Ctrl+Break no longer broadcast to the console (own process group); `smoke_packaged.py` ends and reaps every process it starts (tree kill) even when it crashes; the validator records child failures/timeouts/exceptions, requires a unittest `OK` summary, and ignores build output inside a repository-rooted saved workspace while still protecting `data/`, `strategy_library/`, `configs/` | IMPLEMENTED, TESTED (Linux); Windows rerun pending |
| `packaging/smoke_update.py` (packaged update E2E: check, prompt, verify, swap, relaunch, corrupted artifact, new build that cannot start, locked folder [Windows], offline) and scratch isolation of the packaged smoke and window tests | IMPLEMENTED, TESTED (Linux build) |
| Packaged Windows build + update on Windows | NOT VERIFIED HERE (must be built and exercised on Windows) |
| Publishing a GitHub Release | NOT DONE (maintainer action) |
| Code signing, delta updates, installer | NOT IMPLEMENTED |
## Research browser, clean names, ETA, discovery-window contract (ADR-70)

| Item | Status |
|---|---|
| Folder-tree research browser (All strategies / families / strategies, tri-state selection, counts, manifest order), selection by frozen strategy ids, persisted run scope, read-only data line (no dataset picker) | IMPLEMENTED, TESTED |
| Deterministic display names, explanations and key parameters (`strategy/presentation.py`) | IMPLEMENTED, TESTED |
| Per-cell timing persisted in `search_cells`; data-based ETA with "Estimating..." fallback; preflight timing | IMPLEMENTED, TESTED |
| Live progress from persisted cells (display name, family, timeframe, elapsed, errors, ETA); restart reconstruction | IMPLEMENTED, TESTED |
| Holdout error: ad-hoc paths now request the protocol's discovery window explicitly (`/api/backtests` period, Experiments default); governance unchanged and strict | IMPLEMENTED, TESTED |

## Desktop Research Runs (ADR-69)

| Item | Status |
|---|---|
| Research runs page: frozen campaigns, governance header, family scope selector (catalog order, Select all / Clear all, counts), run / resume, live progress, cancel, run history, stored results by family and strategy (links to the existing run report) | IMPLEMENTED, TESTED (synthetic workspace; browser smoke) |
| `run_search(include=, on_cell=)` run scope and progress hook (sequential; defaults unchanged) | IMPLEMENTED, TESTED |
| Campaign background jobs in the existing JobManager; lock-free status; restart reconciliation of run records | IMPLEMENTED, TESTED |
| Durable run records `<data>/campaigns/<CMP>/runs/` | IMPLEMENTED, TESTED |

## Frozen-manifest discovery campaign launcher (ADR-68)

| Item | Status |
|---|---|
| `research campaign-freeze / campaign-check / campaign-run / campaign-status`: frozen campaign spec, materialization with exact identity and factory lineage, read-only preflight, protocol-gated resumable run (workers 1, one trial per strategy, discovery only) | IMPLEMENTED, TESTED (synthetic workspace, 30-strategy manifest; 10,000-row pure checks) |
| The real campaign over FM_3B0B01CFC81AB15E under RP_D8EAE439C41A | NOT RUN (user workspace) |
| Library round trip of `canonical_definition` for trailing without ATR / no_progress | KNOWN DEFECT (campaign stores the exact manifest definition; DSL unchanged) |

## Pre-campaign governance (ADR-67)

| Item | Status |
|---|---|
| Research protocol version 3: 10,000 unique trials, Bonferroni family = declared budget (alpha 5e-6), bootstrap replicates 5,000,000, independent holdout-look budget | IMPLEMENTED, TESTED (synthetic ledger) |
| `create_protocol(supersedes=)` for an unused protocol + `scripts/protocol_supersede.py` (check / `--apply`) | IMPLEMENTED, TESTED |
| Supersede the user's ACTIVE `RP_C7E98B2A03BD` (v2, 2,000-trial default) in the user workspace | PENDING (Windows command; zero trials / looks required) |
| Prop profiles v4: metadata-only correction (VERIFIED vs ASSUMED_DEFAULT) | IMPLEMENTED, TESTED |
| Hand-computed successful end-to-end lifecycle fixtures for LucidFlex, Growth, Select Flex, Select Daily; profile independence | TESTED |
| Manifest FM_3B0B01CFC81AB15E re-verified (10,000 unique, 30 quotas, all MNQ whole contracts, account-free) | VERIFIED (not regenerated) |

## Prop rule-basis model (ADR-66)

| Item | Status |
|---|---|
| Schema-3 profiles: every rule `{value, status: VERIFIED / ASSUMED_DEFAULT / CUSTOM, basis}`, no nulls; v3 defaults for LucidFlex 50K, Tradeify Growth 50K, Select 50K -> Flex / Daily | IMPLEMENTED, TESTED |
| ASSUMED_DEFAULT rules are active (locks, day boundary 18:00 NY per stage, DLL measurement, intratrade drawdown measurement, Select Daily soft DLL, Growth payout formula / count / scaling, LucidFlex cushion 0) | IMPLEMENTED, TESTED |
| States PASS / FAIL / INCOMPATIBLE / NOT_APPLICABLE, RULE_ASSUMED basis, rule-basis counts and "UNDER DEFAULT ASSUMED RULES" labels on every result; base result fields extended | IMPLEMENTED, TESTED |
| `profiles.customize` (CUSTOM rules as a new version) | IMPLEMENTED, TESTED |
| Checking any rule against provider documentation | NOT DONE (by instruction) |

## Configurable prop rulebook, user-supplied defaults (ADR-65)

| Item | Status |
|---|---|
| Profile schema 2: every prop rule a variable (see ADR-65); append-only v2 profiles registered, v1 drafts kept | IMPLEMENTED, TESTED |
| LucidFlex 50K, Tradeify Growth 50K, Select 50K -> Flex, Select 50K -> Daily defaults as supplied by the user (2026-09-30), `scripts/prop_default_profiles.py` | IMPLEMENTED, TESTED (known-answer) |
| Chronological lifecycle v2: drawdown lock, soft/hard DLL, consistency + cushion, min days, scaling at session end, INCOMPATIBLE on oversize, payout formulas / caps / buffer / split, live-transition eligibility | IMPLEMENTED, TESTED |
| Base result + per-profile summary with every backtest (`run["prop"]`, `backtest_strategy()["prop"]`) | IMPLEMENTED, TESTED |
| Independent verification against provider documents | NOT DONE (basis user_specified) |
| Values not supplied (Tradeify lock level, Select Daily DLL hard/soft, Growth payout formula, trading-day reset) | EdgeLab modelling choices, listed in each profile; CONFIRM |

## $50,000 research account + automatic prop lifecycle (ADR-64)

| Item | Status |
|---|---|
| Default research account 50,000 USD as a RUN parameter (`account=`, `DEFAULT_RESEARCH_ACCOUNT`); not in strategy identity; `starting_equity` in a definition refused | IMPLEMENTED, TESTED |
| Factory `edgelab-strategy-factory/5`, space `edgelab-dt-space/5` (fixed 1/5/10; risk 125/250/500 and equity 0.25/0.5/1.0 % capped at 40; all `contract: MNQ`); manifest regenerated and verified reproducible (10,000) | IMPLEMENTED, TESTED |
| Versioned, hashed, append-only prop rule profiles (`edgelab/prop/profiles.py`, `configs/prop/profiles/`): LucidFlex 50K, Tradeify Growth 50K, Select Flex 50K, Select Daily 50K. Status derives from per-rule evidence; none verified | IMPLEMENTED, TESTED |
| Lifecycle simulator evaluation -> funded -> payouts -> live-transition (`edgelab/prop/lifecycle.py`); runs automatically with every backtest, stored in `run["prop"]`; read-only `/api/prop/profiles`, `/api/prop/lifecycle/<run>`; CLI `prop profiles|lifecycle` | IMPLEMENTED, TESTED (known-answer, synthetic) |
| Official LucidFlex / Tradeify rule values | NOT VERIFIED: official hosts were unreachable (egress blocked). Every profile reports "RULES NOT VERIFIED"; no pass/fail/payout is claimed |
| Tradeify rule values, Select Daily payout formula | NOT IMPLEMENTED (no verified source) |
| Quantity cap 40 for risk-based sizing | UNVERIFIED research cap, not a firm rule |

## MNQ whole-contract execution model (ADR-63)

| Item | Status |
|---|---|
| One authoritative MNQ spec (configs/instruments.yaml, unchanged); dataset identity stays the Dukascopy index-CFD proxy; `execution_view`, `contract_for` | IMPLEMENTED, TESTED |
| `sizing.contract` in the DSL (identity only when present); every factory strategy trades MNQ; factory/space `/4` | IMPLEMENTED, TESTED |
| One exact, floor-only whole-contract conversion for `risk` and `equity_risk`; integer guards; zero contracts reject; hard cap | IMPLEMENTED, TESTED (property sweep of 4,000 budgets, mutation-checked) |
| Quote-aware long/short execution and R-invariance under the contract model | TESTED |
| An instrument-level quantity cap; MNQ on other futures series | NOT IMPLEMENTED / refused by name |
| Numerical evaluation | NOT DONE: no trials, no holdout looks |

## Capability audit (ADR-62)

| Item | Status |
|---|---|
| Capability matrix as data, evidence resolved by test, FACTORY_CAPABILITIES.md generated and drift-tested | IMPLEMENTED, TESTED |
| Equity-based risk sizing (`equity_risk`): realised-equity at the signal, no future leakage, deterministic cap | IMPLEMENTED, TESTED (hand-computed sizes, leakage proofs) |
| No-progress exit, per-strategy trade cap, exit-based re-entry (`entry.reentry`) | IMPLEMENTED, TESTED |
| Feature engine refuses volume-weighted features on provider-defined volume (Dukascopy) | IMPLEMENTED, TESTED |
| Factory `/3`: new real dimensions (equity sizing, no-progress, cap, re-entry, `htf_breakout`, `prior_day_nr`); `atr_normalized` label removed | IMPLEMENTED, TESTED |
| VWAP/volume levels, event/news filters | EXCLUDED (canonical data cannot support them) |
| One-direction-per-session, partial exits, scaling, target trailing, tier tables | NOT IMPLEMENTED (declared, refused by name) |
| Numerical evaluation | NOT DONE: no trials, no holdout looks |

## Trailing stops and 30-family catalog completion (ADR-61)

| Item | Status |
|---|---|
| Real trailing / breakeven stops in the execution kernel (`simulate_exit_trailing`), DSL `exit.trailing`, compiler, backtester; ratchet-only, bar-close decisions effective next bar, exit-side quote extremes, reasons `TRAIL_STOP`/`TRAIL_STOP_GAP` | IMPLEMENTED, TESTED (hand-computed bars; differential test vs the legacy kernel; directional quotes; causality; day-trading invariants) |
| Phase 1 demo output unchanged; logic hashes of strategies without trailing unchanged | TESTED |
| New causal features `order_block` (+ breaker), `daily_nr` | IMPLEMENTED, TESTED (known answers + truncation) |
| ICT family 30: eight setups incl. Order Block, Breaker Block, Opening Range / Initial Balance sweep; NR7 bar + day scope; ROC ATR + percent units | IMPLEMENTED, TESTED |
| Factory `/2`: trailing is a real variation dimension; `zone` stop; family/catalog hashes in lineage | IMPLEMENTED, TESTED |
| Partial exits, target trailing | NOT IMPLEMENTED. Equity sizing, no-progress, caps, re-entry: implemented in ADR-62; VWAP/volume and events excluded for data reasons |
| Numerical evaluation of the generated universe | NOT DONE: no trials, no holdout looks |

## Day-trading strategy factory (ADR-60, generation only)

| Item | Status |
|---|---|
| 30-family universe (`strategy/factory_space.py`), frozen allocation summing to exactly 10,000 (min 200/family), deterministic hash-based sampling | IMPLEMENTED, TESTED |
| 12-stage validation with machine-readable rejection codes; global logic-hash de-duplication; loud failure on an under-filled family | IMPLEMENTED, TESTED |
| Central day-trading policy (`strategy/daytrading.py`): strategy flat window + <23 h max-hold guard + engine-config check; DST-probed NY trading-date check | IMPLEMENTED, TESTED (synthetic backtest with engine flatten disabled) |
| Manifest (`strategies/rejections/duplicates.jsonl`, `manifest.json`, content-derived `FM_` id), `verify` regeneration, Explorer query/summary via Services, CLI `factory`, read-only `/api/factory/*` | IMPLEMENTED, TESTED |
| New causal features `macd`, `adx`, `stoch`, `bollinger`, `donchian`, `narrow_range`, `atr_regime` | IMPLEMENTED, TESTED (registry-wide truncation test) |
| Equity-based sizing, VWAP/volume levels, event filters, no-progress exits, per-strategy trade caps, exit-based re-entry rules | NOT IMPLEMENTED (declared, refused by name). Trailing/breakeven: implemented later in ADR-61 |
| Explorer web page | NOT IMPLEMENTED (backend only) |
| Evaluation of the 10,000-variant universe | NOT IMPLEMENTED - needs a dedicated research protocol; no trials or holdout looks consumed |

## Robust acceptance statistics, protocol version 2 (ADR-57)

| Item | Status |
|---|---|
| OOS confidence = `min(normal, bootstrap-t)` lower bound at the Bonferroni per-test alpha; B = 1,000,000, derived seed, deterministic, monotone in trial count; degenerate/small samples never met | IMPLEMENTED, TESTED (synthetic) |
| Random-entry control = exact Monte-Carlo p-value (<= 0.05 with 100 controls), documented as a robustness filter, not a familywise test | IMPLEMENTED, TESTED |
| Version-1 protocol records keep their own rules; `save_protocol` verifies record identity | IMPLEMENTED, TESTED |
| Retire the zero-trial v1 protocol `RP_257969CFAFFD` and create the v2 protocol in the user workspace | PENDING (Windows command; requires this code) |
| Synthetic end-to-end preflight fix: a changed stored definition is refused (`HOLDOUT_DEFINITION_CHANGED`) before a holdout look is spent | IMPLEMENTED, TESTED |
| Serial dependence (block bootstrap) | NOT IMPLEMENTED (documented limitation) |

## Locked research protocol and ledgers (ADR-56, pre-AI gate)

| Item | Status |
|---|---|
| Immutable, content-addressed research protocol (trading-date discovery/holdout windows + exact bars, dataset, execution, config hash, budgets, acceptance criteria, Bonferroni rule, pre-protocol exposure); ACTIVE -> RETIRED only | IMPLEMENTED, TESTED (synthetic) |
| Holdout lock at the Services boundary (`_run_cell` gate, `plan_search`, OOS/walk-forward pre-check, AI `date_scope`), machine-readable `ProtocolRefusal` codes, HTTP 409 | IMPLEMENTED, TESTED |
| Program-level trial ledger (all discovery entry points, dedup by logic/evaluated-bars/config, budget) and separate proposal-attempt ledger; search identity includes the protocol | IMPLEMENTED, TESTED |
| `Services.evaluate_holdout`: one look per shortlisted frozen candidate within the look budget; random-entry control, cost stress, Bonferroni-adjusted lower bound; never "accepted" | IMPLEMENTED, TESTED (synthetic) |
| Real protocol on the canonical Dukascopy dataset | NOT DONE (created in the user's workspace) |
| Direct library calls (`run_backtest`, `run_across_datasets`, scripts) governed by the protocol | NOT IMPLEMENTED (documented limitation) |

## Canonical directional Dukascopy execution (ADR-55 follow-up)

| Item | Status |
|---|---|
| `NQ_DUKASCOPY@DUKASCOPY` scenario `dukascopy_directional_cost_assumption_v1`, `spread_source: quotes`; commission/fees/slippage/financing numbers unchanged; status `assumed` | CONFIGURED, TESTED |
| Eligibility: only `has_ask_ohlc` datasets; BID-only and BID+spread (incl. frozen `NQ_DUKASCOPY_BIDASK_2021_2026_*`) refused with `reason_codes: [ASK_OHLC_REQUIRED]`; rows gain `has_ask_ohlc`, `cost.spread_source`, `cost.quote_model` | IMPLEMENTED, TESTED |
| Legacy single-series profile (`spread_source: dataset`) keeps its old eligibility when configured | TESTED |
| Real ASK-OHLC datasets `NQ_DUKASCOPY_BIDASK_OHLC_2021_2026_1M_6B0A245100` / `_5M_96699F7568`; real comparison RUN_2026_00027 vs RUN_2026_00028 reconciled | REAL DATA (user workspace; not in the repository) |
| Preferred Research Dataset set to the 5m ASK-OHLC dataset; one canonical EMA baseline run | REAL DATA (user workspace) |
| Historical runs and frozen datasets | UNCHANGED (legacy evidence, never recomputed) |

## Directional BID/ASK quote execution (after Phase 9; ADR-55)

| Item | Status |
|---|---|
| Optional observed ASK OHLC on `BarArrays` (all-or-none), `has_ask_ohlc` manifest flag, content hash includes ASK only when present (hashes/manifests without ASK unchanged) | IMPLEMENTED, TESTED |
| Importer `ask_open/ask_high/ask_low_column` (+ `ask_close_column`); requires all four, `price_basis: bid`, `bid_close_column` = close; values verbatim, never inferred | IMPLEMENTED, TESTED (synthetic) |
| Validation: ASK columns complete, finite, internally consistent, ASK >= BID on O/H/L/C (FAIL, never repaired) | IMPLEMENTED, TESTED |
| 1m -> 5m: ASK first/max/min/last over the same present sub-bars; any missing ASK in a present sub-bar blanks that bucket's ASK (then FAILs); BID and spread aggregation unchanged | IMPLEMENTED, TESTED |
| SQLite ASK columns + migration of existing databases; round trip exact | IMPLEMENTED, TESTED |
| Cost `spread_source: quotes`: buys on ASK, sells on BID for every entry/exit type, gaps, signal and close exits, excursions; separate spread exactly 0; commission on quote-side fills; slippage once per fill | IMPLEMENTED, TESTED (synthetic) |
| Two-sided entry-bar certainty rule; intrabar replay needs and checks both sides (entry side for the fill, exit side for stops/targets) | IMPLEMENTED, TESTED |
| Refusal without complete, finite ASK OHLC (backtester + eligibility) | IMPLEMENTED, TESTED |
| Provenance: `quote_model`, `execution_sides`, `spread_treatment`, `dataset_has_ask_ohlc`, `gross_pnl`, side-naming fill rules; trades `entry_quote_side`/`exit_quote_side`; `trades_hash` definition unchanged | IMPLEMENTED, TESTED |
| ASK == BID reproduces single-series trades hash exactly (market, stop, limit entries) | TESTED |
| Real Dukascopy re-import with ASK OHLC; switching the Dukascopy profile to `quotes` | DONE later (see "Canonical directional Dukascopy execution") |
| CLI/web import form options for ASK OHLC columns | NOT IMPLEMENTED (`Services.import_file` accepts them) |

## Dukascopy-style cost plumbing (after Phase 9)

| Item | Status |
|---|---|
| `commission_mode: notional` + `commission_per_million` (USD per USD 1M traded notional per side, from theoretical fill prices); default `per_unit` unchanged | IMPLEMENTED, TESTED |
| `financing_mode: not_modeled` (charges nothing, disclosed as not modelled; distinct from `none`) | IMPLEMENTED, TESTED |
| Dukascopy profile reshaped to notional commission / dataset spread / financing not modelled, every number empty, still `unconfigured` | CONFIGURED (still refuses) |
| Per-bar spread from bid/ask closes (existing importer + engine) | UNCHANGED; needs a historical ASK series (the imported datasets are BID-only) |
| Time-varying financing rates, tiered commission by cumulative volume | NOT IMPLEMENTED |
| `scripts/dukascopy_bid_ask_check.py`: read-only BID/ASK alignment + spread report through the production parser; combined file only on exact alignment, never overwriting; no import | IMPLEMENTED, TESTED (synthetic) |
| Notional commission relies on `NQ_DUKASCOPY`'s provisional research-unit `point_value` (not a broker-verified contract mapping) | DOCUMENTED CAVEAT |
| `NQ_DUKASCOPY` economics evidence recorded (Dukascopy official: index CFD, 1 CFD, USD 0.01 per 0.01-price point = USD 1 per 1.0 move, matching `point_value` 1) plus the explicit assumption "1 EdgeLab research unit = 1 Dukascopy USATECH.IDX/USD CFD - not yet broker-verified"; points-vs-ticks conversion documented; values unchanged, still `research_units` | DOCUMENTED, TESTED |
| Named research-cost scenario (`scenario` + `basis` on a cost profile): declaring it makes a complete, sourced, notional-commission, points-slippage scenario mandatory; recorded in every run's cost assumptions and shown in Strategy Lab run tables; Dukascopy template declares it (null) and stays `unconfigured` | IMPLEMENTED, TESTED |
| BID/ASK check `--intersection`: combined file = exact overlap when one-sided timestamps are boundary-only (interior gaps refuse); `--out` provenance required (source SHA-256s, row/one-sided counts and ranges, overlap range, output rows/SHA-256) + `import_notes` for the manifest's provider_notes | IMPLEMENTED, TESTED (synthetic) |

## Dukascopy research eligibility: partial calendar verification (after Phase 9)

| Item | Status |
|---|---|
| New `calendar_status: regular_hours_verified` (needs `calendar_evidence` + `calendar_unverified_scope`): allows research; the unverified scope is surfaced as a dataset limitation (`calendar_caveat`); `provisional_unverified` still refuses, `verified` still needs evidence | IMPLEMENTED, TESTED |
| `NQ_DUKASCOPY` set to `regular_hours_verified`: evidence = official Dukascopy USATECH.IDX/USD hours + real-file validation (0 outside-session, 0 grid failures, Sun 18:00 / Fri 16:14) + post-import audit; unverified scope = holidays / early closes (14 whole missing days, early-close-shaped gaps) | CONFIGURED |
| No holidays / early closes added; validation thresholds and data-quality warnings unchanged | UNCHANGED |
| Dukascopy cost profile | UNCHANGED: `unconfigured` (no Dukascopy cost figures in the project records; the only remaining research refusal) |

## Dukascopy gap attribution (after Phase 9)

| Item | Status |
|---|---|
| Gap analysis fix: gap runs split at trading-date boundaries. An early-close tail on D and a wholly missing D+1 were merged into one `session_close` gap, so the real-file report showed 14 missing days but 0 `whole_trading_day` gaps and distorted position counts. | FIXED, TESTED (regression reproduces the old symptom) |
| Gaps carry `minutes_after_session_open` / `minutes_before_session_close`; the summary adds missing-bar totals per position and length; the inspector's JSON lists every gap | IMPLEMENTED, TESTED |
| Official regular USATECH.IDX/USD hours (user-supplied from Dukascopy's range-of-markets page) match `DUKASCOPY_USATECH_OBSERVED` | DOCUMENTED |
| Holidays / early closes for the 14 missing days and early-close-shaped gaps | NOT ENTERED: Dukascopy's Trading Breaks Calendar was not retrievable (egress blocked); no dates inferred from US holidays |

## Dukascopy calendar from the real-file inspection (after Phase 9)

| Item | Status |
|---|---|
| Real-file read-only inspection (user's machine; sha256 d92f25fc…c1d9, 1,709,068 rows): Sun 18:00 first bar, Fri 16:14 last bar, 16:15-18:00 NY empty, no Saturday bars | EVIDENCE (inspector output) |
| `DUKASCOPY_USATECH_OBSERVED` (America/New_York 18:00 → 16:15, declarative, existing single-session model) replaces the CME-style provisional calendar; explains 58,680 of the 90,453 "missing" bars as the recurring 16:15-17:00 closure; remaining ≈31,773 (1.825%, WARN) stay visible | IMPLEMENTED, TESTED (synthetic fixture of the observed schedule; count computed offline from the real range) |
| Missing-bar / outside-session thresholds unchanged; genuine in-session gaps and whole missing days still reported | TESTED |
| 14 whole missing trading days, early closes, 141 range-spike warnings | UNRESOLVED (dates not in this environment; no holidays entered; spikes stay warning-only) |
| `calendar_status` remains `provisional_unverified` (research refused) until the inspector is re-run under the new calendar | UNCHANGED |

## Dukascopy identity and real-import preparation (after Phase 9)

| Item | Status |
|---|---|
| `NQ_DUKASCOPY` identity from the user's download call: Dukascopy `USATECH.IDX/USD` (feed `E_NQ-100`, verified in dukascopy-python 4.0.1), asset class `cfd` (index CFD, not CME NQ), BID, UTC, source volume not exchange volume, research proxy, research-unit economics; `identity_status: user_specified` with evidence | IMPLEMENTED, TESTED |
| Calendar gate: `calendar_status: provisional_unverified` refuses research (same single gate in `_run_cell`, `409 instrument_identity`) until the calendar is verified against the real file (`calendar_status: verified` + `calendar_evidence`) | IMPLEMENTED, TESTED |
| Dukascopy cost profile stays separate and `unconfigured` (no costs invented) | UNCHANGED, TESTED |
| `scripts/dukascopy_inspect.py`: read-only real-file inspection (production inspect + in-memory validation gate + gap analysis; stores nothing) | IMPLEMENTED, TESTED (synthetic fixture) |
| Datasets UI shows symbol, feed code, price basis, volume semantics, calendar status | IMPLEMENTED |
| Inspection, calendar verification, import, preferred dataset and smoke tests on the real file | REQUIRES REAL DATA (user's machine; commands in DATA_IMPORT.md) |

## Phase 9 (as requested): Dukascopy primary research source + AI-assisted strategy discovery

| Item | Status |
|---|---|
| Dukascopy 1m CSV through the existing pipeline: profile `dukascopy_utc_csv` (explicit UTC offsets converted exactly; mixed/naive and `+Nh` refused), source SHA-256, provider, filename, convention, volume `unknown` (not exchange volume), symbol `UNSTATED` (not invented), 1m → 5m derivation with lineage | IMPLEMENTED, TESTED (synthetic Dukascopy-shaped fixture) |
| Provisional source identity `NQ_DUKASCOPY` (not CME NQ; research units); every backtest/search/validation/control refused until identity is stated (`user_specified`) or verified (`source_verified` + evidence); visible in Datasets, pickers and API (`409 instrument_identity`) | IMPLEMENTED, TESTED |
| Separate cost profile `NQ_DUKASCOPY` / `providers.DUKASCOPY` (unconfigured → import allowed, research refused); HistData costs unchanged and never applied | IMPLEMENTED, TESTED |
| Provisional calendar `DUKASCOPY_NQ_PROVISIONAL` (UTC source vs New York session); wrong hours surface as `bars_outside_session` / missing bars (refused, never relaxed) | IMPLEMENTED, TESTED; calendar REQUIRES REAL DATA (inspect evidence) |
| Gap classification + coverage (read-only; nothing excluded) — API, CLI `dataset ID --quality`, Datasets page | IMPLEMENTED, TESTED |
| Preferred Research Dataset (workspace-level, persisted, validated datasets only; Strategy Lab + AI Discovery preselect it for new research; stored runs unchanged) — API, CLI `prefer-dataset`, Datasets page | IMPLEMENTED, TESTED (API + browser) |
| Datasets page: provider, instrument identity/proxy, TF, range, validation, source hash, price/volume semantics, cost status, caveats, preferred flag; import panel gains dataset name, symbol, calendar, derived timeframes | IMPLEMENTED, TESTED |
| AI Discovery: versioned request/proposal schemas; `AIProvider` abstraction; deterministic mock (valid / malformed / causality-invalid / parameter-invalid); optional env-configured external provider (no default model; key never stored) | IMPLEMENTED, TESTED (no network) |
| Strict gate: schema → DSL → features → causality → parameter domain → request constraints → compile → identity, exact reasons, no repair; blind versioned context (no results; hash stable across runs) | IMPLEMENTED, TESTED |
| Human review (inspect / accept / reject / save / send to backtest), no ranking; saved proposals are library strategies with provenance; modifications are new versions with parent + stated and computed changes; lineage request → proposal → strategy → runs | IMPLEMENTED, TESTED (API + browser) |
| Import of the user's real Dukascopy CSV | NOT RUN HERE — the file is not in the repository; commands in DATA_IMPORT.md; REQUIRES REAL DATA |
| Automated AI optimization loops, live trading, broker integration | NOT IMPLEMENTED (by design) |

## Research workspace selection (desktop)

| Item | Status |
|---|---|
| Root cause of the empty Datasets page: a packaged launch without `--data-root` silently created/used an empty `%LOCALAPPDATA%\EdgeLab` workspace | FIXED |
| One workspace authority `runtime.resolve_workspace` (`--data-root` → `EDGELAB_DATA_ROOT` → saved selection → first run); selection saved in the app settings file outside every workspace (ADR-53) | IMPLEMENTED, TESTED |
| Read-only workspace validation (configs, SQLite `mode=ro`, tables, counts, writable, not demo) before switching; create only in empty folders; missing saved workspace → chooser with notice (never silently created) | IMPLEMENTED, TESTED |
| Settings → Research Workspace (current location, validity, SQLite, read/write, counts; check / use / create / default; Browse… in the window); Welcome screen on first run; workspace chip in the top bar; workspace-aware empty states (Datasets, Strategy Lab, Results, Prop) | IMPLEMENTED, TESTED (API + browser) |
| Switching: cancel job, close store, swap app, move lock and runtime.json; both workspaces unchanged (store byte-identical) | IMPLEMENTED, TESTED |
| `packaging/workspace_snapshot.py` read-only before/after check | IMPLEMENTED |
| Real Windows check with `C:\Users\<you>\Documents\AI-Backtesting` | REQUIRES THE USER'S MACHINE (not run here) |

## Native desktop window (after Phase 8)

| Item | Status |
|---|---|
| `EdgeLab.exe` opens its own EdgeLab window (pywebview + Microsoft Edge WebView2) on the existing loopback app; closing it shuts the backend down cleanly (ADR-52) | IMPLEMENTED, TESTED (deterministic; frozen Linux build) |
| WebView2 detection before start with a clear message; `--ui browser` / `--ui none` explicit alternatives; no silent fallback | IMPLEMENTED, TESTED |
| Second launch focuses the running window (token-protected loopback control channel); never a second server | IMPLEMENTED, TESTED |
| `EdgeLabConsole.exe` (console: CLI, logs, headless smoke) next to the windowed `EdgeLab.exe`; windowed output to `logs/console.log` | IMPLEMENTED, BUILT (Linux) |
| Windows real-window integration test `packaging/window_test_windows.py` (runs with `build_windows.ps1 -Smoke`) | IMPLEMENTED; REQUIRES WINDOWS (not run here) |
| Fixed-version WebView2 bundling, tray icon, installer | NOT IMPLEMENTED |

## Phase 8 (as requested): Strategy Lab and GUI-first strategy research

| Item | Status |
|---|---|
| Strategy page as a Strategy Lab: Research hub (version, provenance, workflow, stored runs with scope, batch-on-datasets), Validate tab (OOS, walk-forward, random control on the whole dataset or OOS window), existing Backtest / Variations / Lineage tabs | IMPLEMENTED, TESTED (API + browser e2e) |
| Compare page: unranked side-by-side metrics (trades, gross/net/cost R, expectancy, PF, max DD, breakeven cost multiple, parameters, scope, costs, prop count) from a lineage / version / batch / search; sort and filter as views; selection → validate / prop | IMPLEMENTED, TESTED |
| Run page: scope badge, equity + drawdown curve from stored trades, session / entry-hour / cost-sensitivity breakdowns | IMPLEMENTED, TESTED |
| Dataset pickers with provider/instrument/timeframe filters, caveats, and eligibility enforced (ineligible not selectable) | IMPLEMENTED, TESTED |
| Variation preview lists the exact combinations before generation; batch → "run on datasets" link | IMPLEMENTED, TESTED |
| Machine-readable provenance `GET /api/strategies/<id>/research` (id, hashes, parent id/hash, batch spec, parameters, runs, validation state) | IMPLEMENTED, TESTED |
| HTTP for existing validation services and the Mode B gate; AI Proposals page (check, then save accepted proposals as strategies) | IMPLEMENTED, TESTED; no LLM connected |
| Real 2024 GUI-path smoke test (`lab_smoke_real.py`) | REQUIRES REAL DATA (local store; verified on a synthetic stand-in) |
| Scoring, ranking, "best" labels, automatic optimization, LLM calls, live/paper trading | NOT IMPLEMENTED (deliberately) |

## Phase 7 (as requested): Windows desktop / exe hardening

| Item | Status |
|---|---|
| `edgelab/runtime.py`: the one dev-vs-packaged resolver (bundled resources, persistent workspace `%LOCALAPPDATA%\EdgeLab` / `--data-root` / `EDGELAB_DATA_ROOT`); default configs copied once, never overwritten; config differences reported (ADR-50) | IMPLEMENTED, TESTED |
| Build manifest `edgelab_build.json` from the real sources (source, compiler and feature impl hashes, git commit, build id); frozen code reads it and refuses without it; development `code_version()` unchanged | IMPLEMENTED, TESTED (dev + simulated frozen) |
| Launcher `edgelab/desktop.py`: freeze_support, single instance per workspace, 127.0.0.1 on a free port, readiness wait, browser, clean shutdown, logged/visible errors, `cli` pass-through | IMPLEMENTED, TESTED |
| Repository-relative paths (config defaults, static bundle, demo fixtures, test status) resolved via `edgelab.runtime` | IMPLEMENTED, TESTED |
| PyInstaller spec, `packaging/build.py`, `build_windows.ps1`, build requirements (no DuckDB, tzdata) | IMPLEMENTED |
| Folder-mode build + packaged smoke test (launch, UI, API, static, config, SQLite, build id, data root outside the bundle, datasets, strategy edit/save/lineage, backtest, prop, single instance, clean shutdown + integrity, restart, CLI search with 2 worker processes) | BUILT AND PASSED on Linux (`dist/EdgeLab/EdgeLab`) |
| Frozen vs development research identity (trades hash, config hash, strategy id, feature-cache keys) | VERIFIED identical |
| `EdgeLab.exe` (Windows) | NOT PRODUCED here (no Windows toolchain; PyInstaller does not cross-compile); build with `build_windows.ps1` |
| Installer, signing, auto-update, tray icon, migration command | NOT IMPLEMENTED (deliberately) |

## Phase 6 (as requested): Prop-firm simulation layer

Roadmap row 7 ("Prop-Firm Simulation"), evaluation rules only. It sits above the engine and reads stored runs.

| Item | Status |
|---|---|
| Versioned prop rule sets (`kind: edgelab.prop_rules`, `schema_version: 1`), with strict validation listing every error, a rules-only hash, and refusal by name of rules the trade records cannot support (trailing from intra-trade highs, payouts, news, weekend holding) (ADR-49) | IMPLEMENTED, TESTED |
| Rules: target, minimum trading days, static drawdown, trailing drawdown (closed-balance or end-of-day reference, optional lock), daily loss (terminate or pause the day) with an explicit reset timezone and time, maximum position units with scaling tiers (violations recorded, never clipped), consistency (best-day share), evaluation deadline, session entry window and flat-by time, payout eligibility (reported only) | IMPLEMENTED, TESTED (synthetic trades) |
| Account replay: chronological (exit, entry, trade_no) order; overlapping positions refused; P&L booked on the exit's trading day; `AccountStatus` enum; per-trade progression (balance, peak, drawdown, day P&L, headrooms, target progress, trading days) and per-day table | IMPLEMENTED, TESTED |
| Detection `end_of_trade`, and `intratrade_bound` (conservative bar-resolution MAE bound; refused for trades crossing the reset); same-trade conflicts (a breach beats the target; drawdown takes precedence when both breach) | IMPLEMENTED, TESTED |
| Multiple independent accounts per simulation (same or different rule sets, optional start); no broker routing | IMPLEMENTED, TESTED |
| Lineage (run, strategy, stored definition/logic hash, dataset, period, cost profile/status, verified trades hash, rule-set ids/hashes, ordering, simulator/code version); deterministic `PROP_` ids; optional storage in `<data>/prop_simulations/`; source run verified unchanged after every simulation | IMPLEMENTED, TESTED |
| Report with the strategy result (the source run's metrics, unchanged) kept separate from the prop-account results; no composite score; labels (not evidence of profitability; SYNTHETIC rules; data and cost caveats) | IMPLEMENTED, TESTED |
| `Services.prop_configs / validate_prop_config / prop_simulate / list_prop_simulations / get_prop_simulation`, CLI `prop ...`, `/api/prop/*` | IMPLEMENTED, TESTED |
| Web: Prop Simulation page; Datasets page gains an Eligible column (reasons from `backtest_readiness`) | IMPLEMENTED, TESTED (browser e2e) |
| Strategy workspace (list, open, edit, validate, save a new version or duplicate, lineage) | VERIFIED (existing Phase 3.5 tests; definition-hash preservation assertions added) |
| Synthetic test-only example rule sets `configs/prop/synthetic_{static,trailing}_eval.yaml` | IMPLEMENTED; no real firm's rules shipped |
| Real stored run through the simulator (`prop_smoke_real.py`) | REQUIRES REAL DATA (local store; the script was verified on a synthetic store) |
| Funded phases, payouts, resets; firm-specific commissions; tick-level intra-trade paths | NOT IMPLEMENTED (deliberately) |
| Windows `.exe` | NOT IMPLEMENTED; architecture and blockers in DESKTOP_PACKAGING.md |

## Real-data import fix (after Phase 4, before Phase 5)

| Item | Status |
|---|---|
| Finding: the first real CFD file (`time,open,high,low,close,volume`; stamps such as `2025-10-02T13:14:00Z` and `2026-04-03T09:10:00-04:00`) failed inspection with "Mixed timezones detected" | FOUND (real data) |
| **Phase 2 importer change:** timestamps that all carry an explicit offset (`Z`, `+/-HH:MM`) are converted to UTC exactly even when offsets differ; naive stamps still require `source_timezone`; explicit + naive mixes and malformed stamps are refused (ADR-42) | IMPLEMENTED, TESTED |
| Whether the file's stamps mark bar open or close, its price basis, volume meaning and broker costs | REQUIRES REAL DATA (user must state them; not inferred) |

## HistData NSXUSD research-proxy configuration (after Phase 4, before Phase 5)

| Item | Status |
|---|---|
| Instrument `NAS100_HISTDATA` (tick 0.001, research proxy, no broker figures) + `unconfigured` cost profile; `NAS100_CFD` unchanged (ADR-43) | IMPLEMENTED, TESTED |
| Calendars `HISTDATA_NSX_R1` (Sun 18:00 - Fri 17:00 NY) and `HISTDATA_NSX_R2` (Sun 18:00 - Fri 16:15 NY); existing calendars unchanged | IMPLEMENTED, TESTED (semantics on synthetic timestamps) |
| Timestamps read as `America/New_York`: supported by FOMC events, contradicts HistData's fixed-EST documentation | EVIDENCE (local real-data diagnostics), unresolved conflict recorded |
| Exclusion of the 17:00-17:59 NY DST-transition source anomaly | superseded by ADR-44 below |
| 2017 and 2023 as full-year datasets | REJECTED (missing bars > 5% under their own-schedule calendars) |
| Any HistData year passing validation with the new calendars | REQUIRES REAL DATA (not run in this environment) |
| 2018 real file (local run): `HISTDATA_NSX_R1`, 348,607 rows, 2.873% missing, 131 outside-session bars (WARN), gate accepts with WARN, no exclusions | MEASURED LOCALLY (in memory, not stored) |
| **Phase 2 importer change:** audited source-quality exclusion windows (`source_exclusions.<NAME>` in `configs/data.yaml`, `--source-exclusions`); refuses malformed/overlapping/empty windows and any window touching an in-session bar; full record in the manifest; thresholds and calendars unchanged (ADR-44) | IMPLEMENTED, TESTED (synthetic) |
| Set `HISTDATA_NSXUSD_2019` (20 windows, 17:00-18:00 EDT) | VERIFIED LOCALLY (real file: 1,197 bars excluded, 0 outside-session left, 6,961 missing / 2.005% WARN, gate accepts in memory) |
| Sets `HISTDATA_NSXUSD_2020` / `_2021` / `_2022` / `_2024` (20 / 15 / 15 / 17 windows, 17:00-18:00 EDT, dates from the local trace; none for 2024-10-28/29) | CONFIGURED, TESTED (config); REQUIRES REAL DATA (local run must confirm every window matches bars and the gate accepts) |
| Exclusion set for 2023 | NOT IMPLEMENTED (deliberately; 2023 stays coverage-rejected) |

## Phase 5: Research Analytics (in progress)

| Item | Status |
|---|---|
| `analytics/research.py`: pooled (entry-time ordered, not averaged) and per-dataset metrics, stability counts, canonical-session and entry-hour breakdowns, exact cost sensitivity at the configured multipliers, break-even cost multiple, caveat labels (ADR-46) | IMPLEMENTED, TESTED |
| `Services.research_report(run_ids)`, CLI `report RUN_ID ...`, `GET /api/results/report?run_ids=...` (one fixed strategy; mixed strategies and repeated datasets refused) | IMPLEMENTED, TESTED |
| Research page UI, Monte Carlo, walk-forward/OOS, distributions, weekday/month breakdowns | NOT IMPLEMENTED (deferred) |
| Real five-year EMA pipeline baseline analysed through the report | REQUIRES the local store (runs are local) |
| Validation of fixed strategies (ADR-47): OOS split (`IN_SAMPLE` / `OUT_OF_SAMPLE` runs), rolling/anchored walk-forward (`WALK_FORWARD` runs), frozen definition checks, seeded bootstrap/shuffle Monte Carlo of observed trades; `Services.evaluate_oos`, `Services.walk_forward`, CLI `validate`; `research_report` gains `monte_carlo` | IMPLEMENTED, TESTED |
| Matched random-entry control (ADR-48): candidate's own eligibility/levels/cooldown/exits/sizing/costs, entry timing and direction randomized per bar (seeded, prefix-stable), calibrated to the candidate's pre-cooldown entry count and long share (method v2; v1 under-fired cooldown strategies); N realizations, candidate-vs-control distribution summary; `Services.random_entry_control`, CLI `validate control`; control results never stored as runs | IMPLEMENTED, TESTED |
| **Phase 3 compiler refactor:** `signals_from_features` split into shared helpers, behaviour-preserving (8/8 fixtures identical) | IMPLEMENTED, TESTED |
| Walk-forward across several datasets; validation HTTP/UI | NOT IMPLEMENTED (deferred) |

## HistData research cost baseline (after HistData import, before Phase 5)

| Item | Status |
|---|---|
| `costs.symbols.NAS100_HISTDATA.providers.HISTDATA`: approved MNQ-equivalent research ASSUMPTIONS (`status: assumed`; commission 0.50/unit/side, slippage 0.25 pts market/stop, 0 limit, fixed spread 0.50 pts, financing none). Symbol level and other feeds stay unconfigured; `NAS100_CFD`/`US100_CFD`/`NQ_CFD` unchanged | CONFIGURED, TESTED |
| These values as a broker's actual costs | NOT CLAIMED (assumed research baseline; replace per broker when verified) |

## Dataset reload fix (after HistData import, before Phase 5)

| Item | Status |
|---|---|
| **Phase 2 fix:** `load_validated` kept overwriting import-time cleaning facts (`source_detail.cleaning`, `raw_duplicate_bars`, `duplicate_bars`) on reload, so datasets whose source needed cleaning reloaded with a different manifest hash; it now keeps the stored facts (ADR-45) | IMPLEMENTED, TESTED |
| Real HistData 2019-2022, 2024 imported locally (`NAS100_HISTDATA_<YEAR>`, WARN, exclusions 1197/1106/900/900/898, 60-row rollback cleaned); only the reload manifest-hash check failed before this fix | MEASURED LOCALLY; re-check after the fix REQUIRES the local run |

## Phase 4: Batch Research & Search

| Item | Status |
|---|---|
| Search spec (`research/search.py`): sources = strategy ids, Mode A `VB_` batches, Mode B `PB_` batches, families; datasets; period (full / common / explicit with timezone); ranking settings; `max_cells`; seed; workers. Strict validation (unknown keys, malformed values, invalid references) with every issue reported; example `configs/search.example.yaml` (outside the config hash) | IMPLEMENTED, TESTED |
| Deterministic planner: strategy x dataset cells in plan order; duplicate strategies collapse; archived batch/family members excluded and reported; eligibility = the exact `backtest_readiness` reasons (unconfigured CFD costs stay ineligible) plus "outside the period"; eligible cells above `max_cells` refused before anything runs, never truncated | IMPLEMENTED, TESTED |
| Identity: `search_hash` = canonical sources, datasets, period, seed, spec version + execution `config_hash` (ranking, `max_cells`, workers excluded); `cell_id` = strategy_id, dataset_id, dataset content hash, config_hash, search_hash (ADR-32) | IMPLEMENTED, TESTED |
| One cell pipeline: `Services._run_cell` / `_record_cell` extracted from `backtest_strategy` (unchanged output); `_dataset_eligibility` from `backtest_readiness` (ADR-33) | IMPLEMENTED, TESTED |
| Durable SQLite storage: `search_batches`, `search_cells` (additive, ADR-34); the `runs` table stays authoritative; every planned cell stored (completed incl. zero-trade / failed with error / ineligible with reasons / cancelled) | IMPLEMENTED, TESTED |
| `run_search`: synchronous, plan order, lineage (`parent_strategy_id`, `mutation`) and search/cell id on each run, status `IN_SAMPLE`; resume skips completed cells whose run exists; failed/pending/cancelled cells re-run | IMPLEMENTED, TESTED |
| Trial accounting per invocation and cumulative over the CURRENT plan; cells of an earlier plan kept as historical, never counted (ADR-35) | IMPLEMENTED, TESTED |
| In-sample ranking (`research/ranking.py`): expectancy_r (default) / profit_factor / net_r, win rate refused, minimum sample label, deterministic ties, exclusions counted by reason, +inf profit factor recognised from stored fields and flagged (never a number); every output labelled in-sample / NOT VALIDATED with its trial count; shortlist = a tag on the batch, no run status changed (ADR-36) | IMPLEMENTED, TESTED |
| Mode B proposal-batch records (`kind: proposal`) saved by `ingest_proposals(save=True)`; `list_variation_batches(kind=)` with unchanged default; derived, verified strategy-library index (ADR-37) | IMPLEMENTED, TESTED |
| Background jobs (`research/jobs.py`): one worker thread, one active job (409 otherwise), queued -> running -> completed / failed / cancelled, polling progress from stored cells, cooperative cancellation between cells, restart reconciliation (`running` -> `interrupted`, never auto-resumed); the service lock is shared and never held during a backtest (ADR-38) | IMPLEMENTED, TESTED |
| `FeatureCache` thread and process safety: private lock for the memory LRU and stats; unique temp files for metadata and arrays, metadata written first (ADR-39) | IMPLEMENTED, TESTED (thread + multi-process stress) |
| Process-parallel search (`workers > 1`, spawned processes): workers compute only, the parent writes everything in plan order; `workers=1` and `workers=N` give identical cell ids, statuses, trades hashes and errors (ADR-40) | IMPLEMENTED, TESTED |
| `scripts/benchmark_search.py` (synthetic, informational): measured 32 cells, 17.97 s sequential vs 6.78 s with 4 workers on a 4-CPU container, results identical; tiny searches are slower in parallel (process start-up) | IMPLEMENTED, TESTED (runs in a test) |
| `research_config_options` exposes the stored strategies and the search spec options (the Phase 2 "NOT IMPLEMENTED" placeholder is gone) | IMPLEMENTED, TESTED |
| Research CLI: `research validate / plan / run [--workers N] / rank / job` | IMPLEMENTED, TESTED |
| Research HTTP API: `/api/research/validate`, `plan`, `jobs`, `jobs/<id>`, `jobs/<id>/cancel`, `searches`, `searches/<id>`, `searches/<id>/ranking`, `searches/<id>/shortlist`; 422 search spec / ranking, 409 job conflict / unsupported storage, 404 unknown job or search | IMPLEMENTED, TESTED |
| Research web page: setup, spec check, plan preview, background job with polling and cancel, searches list, current and historical cells, in-sample ranking, shortlist; committed bundle rebuilt | IMPLEMENTED, TESTED (browser) |
| `research/compare.py`: cross-dataset warnings extracted into `comparison_warnings` (same text, ADR-41) | IMPLEMENTED, TESTED |
| Analytics breakdowns, OOS, walk-forward, Monte Carlo, significance testing, null-control services, prop simulation, reports, paper/live trading, AI model calls | NOT IMPLEMENTED (Phase 5+) |
| Research on real market or CFD data; broker CFD costs | REQUIRES REAL DATA (none imported; all results so far are synthetic) |

Tests: full regression 416 passed, 4 skipped (DuckDB x3, slow opt-in); the Phase 4 audit run had 415 before the example-config test was added. Phase 4 added 98 tests (318 after Phase 3.5), including 9 browser end-to-end tests of which 4 cover the Research page. Phase 1 demo: identical to the pre-Phase-4 output apart from the source hash and timings.

## Phase 3.5: Strategy Builder & research UI

| Item | Status |
|---|---|
| Web app: `python -m edgelab.web` (Flask API over `services`, committed React + TypeScript bundle); `--demo` separate synthetic workspace | IMPLEMENTED, TESTED |
| App shell: sidebar, top bar with strategy search, backend status and Settings; mobile drawer; demo banner | IMPLEMENTED, TESTED (browser) |
| Dashboard: status, version, frontend build, last test run, counts, quick actions gated on prerequisites | IMPLEMENTED, TESTED |
| Strategy Builder: general/family, timeframe, trading window, weekdays, local sessions, 5 parameter types, ALL/ANY/NOT condition trees with all DSL operators, operands (constant/parameter/bar/feature/arithmetic), registry-driven feature picker, higher-timeframe operands, market/stop/limit entries with expiry, stops, targets, time and signal exits, sizing | IMPLEMENTED, TESTED (browser) |
| Live backend validation with section badges; Validate; Compile & Explain; backend DSL preview (draft/canonical/JSON), copy, download, load | IMPLEMENTED, TESTED |
| Save / Save As New / Duplicate / Edit with lineage methods; unchanged logic saves nothing; invalid strategies refused | IMPLEMENTED, TESTED |
| Library (filters, archive/restore with confirmation), strategy page, families with lineage tree and accessible table, variation batches | IMPLEMENTED, TESTED |
| Mode A in the UI: grid / one-at-a-time / seeded random, backend combination preview and cap, results with duplicates, batch ID and compare | IMPLEMENTED, TESTED (browser) |
| Datasets: library, metadata and validation report, import over the Phase 2 pipeline (import folders only) | IMPLEMENTED, TESTED (API) |
| Single backtest: readiness per dataset (validation, timeframe, cost reasons), explicit selection, run recorded in the Phase 1 run registry; synthetic runs labelled and listed separately; CFD refused while costs are unconfigured | IMPLEMENTED, TESTED (browser + API) |
| Results page (single runs only), Research and AI Discovery placeholders, read-only Settings | IMPLEMENTED (the Research placeholder was replaced by the Phase 4 Research page) |
| Service additions (system_status, builder_options, render_strategy, variation_preview, archive/restore, batches, family_detail, backtest_readiness, list_runs, get_run); `backtest_strategy(record=)` | IMPLEMENTED, TESTED |
| **Phase 1 store change:** SQLite `check_same_thread=False` (ADR-30); Phase 1 demo identical | IMPLEMENTED, TESTED |
| Lineage library: reversible archive, richer listing, batch listing; variation summaries include overrides; feature `describe()` exposes session parameters | IMPLEMENTED, TESTED |
| Optional `configs/web.yaml`, outside the research config hash | IMPLEMENTED, TESTED |
| `scripts/run_tests.py` (dashboard test status); stale-bundle test; TypeScript type-check test | IMPLEMENTED, TESTED |
| Batch research, analytics, OOS, walk-forward, Monte Carlo, prop simulation, reports, paper/live trading, AI model calls | NOT IMPLEMENTED (Phase 4+); batch research was implemented in Phase 4 |

Tests: 318 (20 new: 15 API, 5 browser end-to-end); 4 skipped (DuckDB x3, slow opt-in).

## Phase 3: Strategy DSL, compiler, lineage, controlled variations, proposal interface

### DSL and validation (`edgelab/strategy/dsl.py`, STRATEGY_DSL.md)
| Item | Status |
|---|---|
| Versioned DSL v1 (YAML/JSON/dict): family, timeframe, local sessions, parameters, entry, exit, sizing | IMPLEMENTED, TESTED |
| Operands: constants, `$param`, bar fields with lag, feature outputs (params/output/HTF/lag/pinned version), add/sub/mul/div | IMPLEMENTED, TESTED |
| Conditions: all/any/not, `> >= < <= == !=`, crosses_above/below, per-condition `enabled` and `label` | IMPLEMENTED, TESTED |
| Typed parameters (integer/float/boolean/choice/timeframe) with strict domain and grid checks | IMPLEMENTED, TESTED |
| Validator: schema, parameter, feature, logic, architecture and causality rules; all issues reported with paths and suggestions; CLI exit code 2 | IMPLEMENTED, TESTED |
| Unsupported concepts refused by name: trailing stop, breakeven, partial exits, pyramiding, exit-based cooldown, lead/future | IMPLEMENTED, TESTED |
| Canonical form; `logic_hash` (strategy_id) vs `definition_hash`; session definitions part of identity | IMPLEMENTED, TESTED |

### Compiler (`edgelab/strategy/compiler.py`)
| Item | Status |
|---|---|
| Deterministic compile into `OrderSpec` + sizing config + de-duplicated `FeatureSpec`s -> `DSLStrategy` (FeatureStrategy / Strategy) | IMPLEMENTED, TESTED |
| Entries: market / stop / limit with expiry; long / short / both; trading window; trading-date weekdays; signal cooldown | IMPLEMENTED, TESTED |
| Exits: points / ATR / price stops; none / points / ATR / price / R-multiple targets; time stop; max hold; signal exits | IMPLEMENTED, TESTED |
| Sizing: fixed quantity (checked against instrument size rules at bind) and risk-based | IMPLEMENTED, TESTED |
| Kleene three-valued evaluation; ambiguous both-direction bars dropped; invalid stop/target sides dropped and counted | IMPLEMENTED, TESTED |
| Causality: static rules + Phase 1 truncation check (incl. exit arrays) on every run; MTF cut inside unfinished HTF bars | IMPLEMENTED, TESTED |
| Provenance (DSL/compiler versions, compiler source hash, feature spec ids + impl hashes, sessions, config hash); `explain()` | IMPLEMENTED, TESTED |
| Same definition on futures and CFD datasets; CFD runs refused while broker costs are unconfigured | IMPLEMENTED, TESTED (synthetic) |

### Engine and features (Phase 1/2 modules changed)
| Item | Status |
|---|---|
| **Engine change:** optional `SignalSet.exit_long/exit_short`; exit at next open, reason `SIGNAL` (market costs); earlier exits win; gap at that open uses the existing gap policy. Absent arrays = unchanged behaviour (219 prior tests + Phase 1 demo identical) | IMPLEMENTED, TESTED |
| New feature `time_of_day` v1 (weekday, trading_weekday, hour, minute_of_day; known at bar open); FEATURES.md regenerated | IMPLEMENTED, TESTED |

### Lineage, Mode A, Mode B, services
| Item | Status |
|---|---|
| Families vs instances; `LineageRecord` (method, parent, exact changes, batch, timestamp, versions); file-based `StrategyLibrary` (atomic, idempotent, multi-parent, ancestry/children) | IMPLEMENTED, TESTED |
| Mode A: grid / one_at_a_time / seeded random_sample; declared-domain enforcement; cap before generation; invalid child fails batch; logic dedupe reported; reproducible batch ids and records | IMPLEMENTED, TESTED |
| Mode B: capability menu, `StrategyProposer` protocol, `StaticProposer`, ingestion gate (strict schema, claim-language rejection, same validator/compiler, duplicate/structure checks, lineage) | IMPLEMENTED, TESTED |
| Services (strict JSON) + CLI `strategy validate/compile/explain/save/variations/proposals/list/show/lineage/menu/backtest` | IMPLEMENTED, TESTED |
| 8 strategy fixtures, variation spec, proposal batch (test fixtures, not claims of edge) | IMPLEMENTED, TESTED |

### Not implemented (deliberately)
| Item | Status |
|---|---|
| Trailing stops, breakeven, partial exits, pyramiding, exit-based cooldown (engine does not support them) | NOT IMPLEMENTED (refused by the validator) |
| Batch execution / ranking of variations and proposals | NOT IMPLEMENTED in Phase 3; IMPLEMENTED in Phase 4 |
| Any AI model call | NOT IMPLEMENTED (interface only) |
| UI, live trading, broker execution | NOT IMPLEMENTED (out of scope) |
| Performance of any fixture strategy on real data | REQUIRES REAL DATA |

Tests: 298 (4 skipped: DuckDB unavailable, slow tests opt-in); Phase 3 added 79.

## Phase 2: Features + CFD-ready data foundation

### Data foundation
| Item | Status |
|---|---|
| Generic import pipeline: inspect, normalize, validate, manifest, hash, store, derive timeframes, build features (`data/importer.py`, CLI, services) | IMPLEMENTED, TESTED |
| Layout presets `generic_csv`, `mt5_export`, `dukascopy_csv`; column mapping; date+time columns; epoch timestamps | IMPLEMENTED, TESTED (on synthetic files in those layouts) |
| Timezones: IANA zones and broker-server clocks (`America/New_York+7h`); close to open conversion; DST gap / repeated hour refused | IMPLEMENTED, TESTED |
| Manifest provenance (dataset name, asset type, symbol, source tz/convention, file sha256, volume type, price basis, spread source, bid/ask, calendar + fingerprint, import version, parent/derivation) and deterministic `manifest_hash()` | IMPLEMENTED, TESTED |
| Content-addressed dataset ids; idempotent re-import; no overwrite; identical content flagged | IMPLEMENTED, TESTED |
| Optional per-bar spread (part of the content hash when present; absent -> Phase 1 hashes unchanged) | IMPLEMENTED, TESTED |
| Validation: `spread_non_negative` (FAIL), `spread_availability` (WARN), `volume_availability` | IMPLEMENTED, TESTED |
| Store: validation reports, dataset listing, spread column migration, schema-evolving result tables | IMPLEMENTED, TESTED (SQLite); DuckDB paths IMPLEMENTED, untested (duckdb unavailable) |
| `load_validated`: re-runs the gate, checks content hash and calendar fingerprint | IMPLEMENTED, TESTED |
| Real CFD data quality, broker server clocks, broker trading hours, spread units, MT5 export details | REQUIRES REAL DATA |

### Sessions and features
| Item | Status |
|---|---|
| Configurable DST-safe session windows (NY 09:00-10:00, 10:00-11:00, 09:30-10:30, RTH, AM/PM, London, Tokyo, NY-evening Asia with midnight wrap) | IMPLEMENTED, TESTED (US/UK DST, mismatch weeks, fall-back, gaps, ambiguity) |
| 14 features: candle, atr, ema, sma, rsi, roc, vwap, volume_stats, rvol_tod, session, daily_levels, swings (BOS, sweeps, trend), range_stats, fvg | IMPLEMENTED, TESTED (hand-computed known answers) |
| Machine-readable feature definitions and generated `FEATURES.md` (drift-tested) | IMPLEMENTED, TESTED |
| Causality: truncation checker; every feature and HTF variant passes; checker proven to catch 5 leak classes | IMPLEMENTED, TESTED |
| Multi-timeframe: HTF exposed only at effective close (partial session-end bucket at session close) | IMPLEMENTED, TESTED |
| Persistent feature cache: full-input keys incl. implementation hash and dependency keys, checksum-verified, corruption-safe, memory LRU, per-engine memo | IMPLEMENTED, TESTED |
| Volume features refuse no-volume datasets; tick volume recorded in result metadata | IMPLEMENTED, TESTED |
| `FeatureStrategy` / `FeatureContext`: dataset-agnostic strategy consumption; Phase 1 causality check passes end to end | IMPLEMENTED, TESTED |
| Whether any feature is predictive on real markets | REQUIRES REAL DATA (and Phase 6 validation) |

### Costs, comparison, interfaces
| Item | Status |
|---|---|
| Cost profiles with status; per-provider overrides; CFD profiles ship unconfigured and refuse | IMPLEMENTED, TESTED |
| Dataset spread mode, point-based slippage, overnight financing (rollover instants, triple day, credits) | IMPLEMENTED, TESTED (exact post-hoc cost sensitivity preserved) |
| Fractional CFD unit sizing (`min_size`, `size_step`) | IMPLEMENTED, TESTED |
| Broker-specific CFD numbers | NOT IMPLEMENTED (by design: user must enter them); REQUIRES REAL DATA / broker schedules |
| Bid/ask trigger asymmetry for stops on bid-based feeds | NOT IMPLEMENTED (documented limitation) |
| Dataset comparison: per-dataset independent runs with provenance, feed comparison, period restriction; no merged or averaged dataset | IMPLEMENTED, TESTED |
| Service layer (strict-JSON contracts for Data Center / Feature Lab / Research Configuration) and CLI | IMPLEMENTED, TESTED |
| Web UI | NOT IMPLEMENTED (contracts ready; no placeholder UI was built) |
| Benchmark script and results (`reports/phase2_benchmark.txt`) | IMPLEMENTED (synthetic data) |
| Strategy DSL, variation generation (Mode A), AI strategy families (Mode B), batch search | NOT IMPLEMENTED in Phase 2; IMPLEMENTED in Phases 3-4 |

### Changes to Phase 1 code (all Phase 1 tests still pass)
- **Bug fix - resampling fabricated volume.** `resample_bars` summed volume with pandas `sum()`, so a
  bucket with no volume became 0.0 and a partially missing bucket was under-counted. A bucket's
  volume is now NaN unless every sub-bar has volume. (Never triggered in Phase 1, which had no
  missing volume; would have corrupted any no-volume CFD feed.)
- **Correctness fix - invented CFD cost numbers removed.** `costs.yaml` contained a CFD spread of
  1.0 and 2-tick slippage that were not from any broker. CFD profiles are now `unconfigured`.
  Consequently `tests/test_costs_sizing.py::test_cfd_spread` now builds an explicit
  `CostModel(spread_points=1.0)` (same arithmetic assertion) and additionally asserts that the
  unconfigured profile refuses.
- Additive, backward-compatible: `BarArrays.spread` (optional), new manifest fields with defaults
  and a tolerant `DatasetManifest.from_dict`, `SessionCalendar.fingerprint()`,
  `validate_and_freeze(**manifest_fields)`, instrument `min_size`/`size_step`/`underlying`,
  sizing floors to the size step (integer contracts unchanged for futures), backtester size check
  `<= 0` (identical for integer sizes), new trade column `financing_usd`, assumption `cost_status`,
  `CostModel` new fields and `round_trip_base(..., spread_points=None)`.
- CFD instrument templates changed to a 0.01 price grid with explicit unit sizing (were 0.1 ticks).

### Test counts
Phase 1: 104 tests (unchanged except `test_cfd_spread`, above). Phase 2: 115 new tests in
`test_features_numeric`, `test_sessions_dst`, `test_mtf_causality`, `test_feature_cache`,
`test_cfd_data`, `test_cfd_costs`, `test_dataset_compare`, `test_services_cli`.

## Phase 1: Foundation
Configuration, JSON logging, data schema and mandatory validation gate, synthetic data, calendars,
session-anchored resampling, fill model, costs, sizing, backtester with causality enforcement,
metrics with sample labels, run records, SQLite store (DuckDB backend untested), 104 tests.

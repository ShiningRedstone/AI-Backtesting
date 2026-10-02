"""EdgeLab command line (thin wrapper over edgelab.services - the same calls a web UI makes).

    python -m edgelab.cli inspect FILE [layout options] --source-timezone TZ
    python -m edgelab.cli import  FILE --instrument NAS100_CFD --provider X --asset-type CFD \\
                                        --timeframe 1m --source-timezone TZ [options]
    python -m edgelab.cli datasets
    python -m edgelab.cli dataset DATASET_ID
    python -m edgelab.cli features list | docs | build DATASET_ID | cache DATASET_ID
    python -m edgelab.cli compare-feeds DATASET_A DATASET_B
    python -m edgelab.cli sessions
    python -m edgelab.cli strategy validate|compile|explain|save FILE
    python -m edgelab.cli strategy variations BASE_FILE SPEC_FILE [--no-save]
    python -m edgelab.cli strategy proposals BATCH_FILE [--no-save]
    python -m edgelab.cli strategy list [--family ID] | show ID | lineage ID | menu
    python -m edgelab.cli strategy backtest FILE_OR_ID DATASET_ID
    python -m edgelab.cli research validate|plan SEARCH_SPEC_FILE
    python -m edgelab.cli research run SEARCH_SPEC_FILE [--workers N]
    python -m edgelab.cli research rank SEARCH_ID [--metric M] [--min-sample-label L]
    python -m edgelab.cli research job SEARCH_SPEC_FILE     (background job; progress; Ctrl-C cancels)
    python -m edgelab.cli report RUN_ID [RUN_ID ...]       (Phase 5: descriptive report, one strategy)
    python -m edgelab.cli validate oos|walkforward STRATEGY DATASET_ID [--split DATE | --train-months N --test-months M]
    python -m edgelab.cli validate control STRATEGY DATASET_ID [--controls N --seed S]   (random-entry control)
    python -m edgelab.cli prop configs | list | validate FILE_OR_ID | show PROP_ID
    python -m edgelab.cli prop simulate RUN_ID --config FILE_OR_ID [--config ...] [--accounts N] [--record]

Add --json to any command for machine-readable output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _layout_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file")
    p.add_argument("--profile", help="preset from configs/import_profiles.yaml (generic_csv, mt5_export, ...)")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--source-timezone", help="IANA zone (UTC, Europe/London) or broker server time "
                                             "like America/New_York+7h. REQUIRED for import.")
    p.add_argument("--timestamp-convention", default="open", choices=("open", "close"))
    p.add_argument("--delimiter", default=",")
    p.add_argument("--columns", default=None,
                   help="canonical=file mapping, e.g. ts=Gmt time,open=Open,high=High,low=Low,close=Close,volume=Volume")
    p.add_argument("--date-column")
    p.add_argument("--time-column")
    p.add_argument("--datetime-format")
    p.add_argument("--epoch-unit", choices=("s", "ms", "us", "ns"))
    p.add_argument("--spread-column")
    p.add_argument("--spread-multiplier", type=float)
    p.add_argument("--bid-close-column")
    p.add_argument("--ask-close-column")
    p.add_argument("--volume-type", choices=("exchange", "tick", "none"))


def _options(a, for_import: bool) -> dict:
    cols = {}
    if a.columns:
        for pair in a.columns.split(","):
            k, v = pair.split("=", 1)
            cols[k.strip()] = v.strip()
    d = dict(file=a.file, instrument=getattr(a, "instrument", None) or "UNSPECIFIED",
             provider=getattr(a, "provider", None) or "UNSPECIFIED",
             asset_type=getattr(a, "asset_type", None) or "unspecified", timeframe=a.timeframe,
             source_timezone=a.source_timezone, timestamp_convention=a.timestamp_convention,
             delimiter=a.delimiter.encode().decode("unicode_escape"), columns=cols,
             date_column=a.date_column, time_column=a.time_column, datetime_format=a.datetime_format,
             epoch_unit=a.epoch_unit, spread_column=a.spread_column, spread_multiplier=a.spread_multiplier,
             bid_close_column=a.bid_close_column, ask_close_column=a.ask_close_column,
             volume_type=a.volume_type, profile=a.profile)
    if for_import:
        d.update(symbol=a.symbol or "", dataset_name=a.dataset_name, calendar=a.calendar,
                 price_basis=a.price_basis, notes=a.notes or "",
                 derive_timeframes=[x for x in (a.derive or "").split(",") if x],
                 build_features=not a.no_features, source_exclusions=a.source_exclusions)
    return d


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=1, default=str))
        return
    if isinstance(obj, list):
        for row in obj:
            print("  ".join(f"{k}={v}" for k, v in row.items()))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and len(json.dumps(v, default=str)) > 100:
                print(f"{k}:")
                print("  " + json.dumps(v, indent=1, default=str).replace("\n", "\n  "))
            else:
                print(f"{k}: {v}")
    else:
        print(obj)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="edgelab", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="project root (contains configs/)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="dry-run: show how a raw file would be read (stores nothing)")
    _layout_args(p)
    p.add_argument("--view-timezone", default="America/New_York")

    p = sub.add_parser("import", help="validate + store an immutable dataset, derive timeframes, cache features")
    _layout_args(p)
    p.add_argument("--instrument", required=True, help="key in configs/instruments.yaml, e.g. NAS100_CFD")
    p.add_argument("--provider", required=True)
    p.add_argument("--asset-type", required=True, help="CFD | FUTURE | ...")
    p.add_argument("--symbol", help="provider's own symbol")
    p.add_argument("--dataset-name", help="default <INSTRUMENT>_<PROVIDER>")
    p.add_argument("--calendar", help="default: the instrument's calendar")
    p.add_argument("--price-basis", default="unknown", choices=("bid", "ask", "mid", "last", "unknown"))
    p.add_argument("--notes")
    p.add_argument("--derive", help="comma-separated higher timeframes to derive, e.g. 5m,15m,60m")
    p.add_argument("--no-features", action="store_true")
    p.add_argument("--source-exclusions", help="audited exclusion set from configs/data.yaml "
                                                "(source_exclusions.<NAME>); opt-in, see DATA_IMPORT.md")

    sub.add_parser("datasets", help="list stored datasets")
    p = sub.add_parser("dataset", help="manifest + validation report of one dataset")
    p.add_argument("dataset_id")
    p.add_argument("--quality", action="store_true", help="gap classification + coverage (read-only)")
    p = sub.add_parser("prefer-dataset", help="show / set / clear the workspace's Preferred Research Dataset "
                                              "(a default for NEW research; stored runs never change)")
    p.add_argument("dataset_id", nargs="?")
    p.add_argument("--clear", action="store_true")

    p = sub.add_parser("features", help="feature catalog / docs / build / cache status")
    p.add_argument("action", choices=("list", "docs", "build", "cache"))
    p.add_argument("dataset_id", nargs="?")

    p = sub.add_parser("compare-feeds", help="bar-level agreement of two datasets (no merging)")
    p.add_argument("a")
    p.add_argument("b")
    sub.add_parser("sessions", help="configured session windows")

    p = sub.add_parser("strategy", help="Strategy DSL: validate, compile, variations, proposals, lineage")
    p.add_argument("action", choices=("validate", "compile", "explain", "save", "variations", "proposals",
                                      "list", "show", "lineage", "menu", "backtest"))
    p.add_argument("args", nargs="*", help="files / ids for the action")
    p.add_argument("--family")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--n-families", type=int, default=20)

    p = sub.add_parser("report", help="Phase 5: descriptive research report over stored runs of one strategy")
    p.add_argument("run_ids", nargs="+", help="RUN_ ids (one fixed strategy, one run per dataset)")

    p = sub.add_parser("validate", help="fixed-strategy OOS split or walk-forward (+ seeded Monte Carlo)")
    p.add_argument("kind", choices=("oos", "walkforward", "control"))
    p.add_argument("strategy", help="strategy file or STR_ id (used unchanged, frozen)")
    p.add_argument("dataset_id")
    p.add_argument("--split", help="oos: first instant of the out-of-sample window (UTC if no offset)")
    p.add_argument("--train-months", type=int, help="walkforward: train window length")
    p.add_argument("--test-months", type=int, help="walkforward: test window length (= step)")
    p.add_argument("--anchored", action="store_true", help="walkforward: train from the dataset start")
    p.add_argument("--record", action="store_true", help="store each window as a run record")
    p.add_argument("--sims", type=int, default=1000, help="Monte Carlo simulations (default 1000)")
    p.add_argument("--seed", type=int, default=0, help="Monte Carlo / control base seed (default 0)")
    p.add_argument("--controls", type=int, default=20, help="control: random-entry realizations (default 20)")

    p = sub.add_parser("prop", help="Phase 6 prop-account simulation over a stored run (read-only)")
    p.add_argument("action", choices=("configs", "list", "validate", "simulate", "show", "profiles", "lifecycle"))
    p.add_argument("target", nargs="?", help="validate: rule-set file or id; simulate: RUN_ id; show: PROP_ id")
    p.add_argument("--config", action="append", default=[],
                   help="simulate: rule-set file or id from configs/prop (repeat: one account per config)")
    p.add_argument("--accounts", type=int, default=1, help="simulate: identical accounts per --config (default 1)")
    p.add_argument("--record", action="store_true", help="simulate: store the simulation under <data>/prop_simulations")

    p = sub.add_parser("factory", help="day-trading strategy factory: generate / list / summary / query / verify "
                                       "a strategy-universe manifest (generation only: no data, no trials)")
    p.add_argument("action", choices=("generate", "generate-pool2", "list", "summary", "query", "verify", "capabilities"))
    p.add_argument("manifest_id", nargs="?", help="FM_ id (summary / query / verify)")
    p.add_argument("--seed", type=int, help="generate: seed (default: the factory default seed)")
    p.add_argument("--filter", action="append", default=[], help="query: key=value (repeatable)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)

    p = sub.add_parser("research", help="Phase 4 batch search: validate, plan, run, rank, background job")
    p.add_argument("action", choices=("validate", "plan", "run", "rank", "job", "campaign-freeze", "campaign-check",
                                      "campaign-run", "campaign-status"))
    p.add_argument("target", help="search spec file (YAML/JSON), a SRCH_ id for rank, an FM_ manifest id for "
                                   "campaign-freeze, or a CMP_ campaign id for campaign-check / -run / -status")
    p.add_argument("--dataset", action="append", default=[], metavar="TF=DATASET_ID",
                   help="campaign-freeze: explicit dataset for a timeframe (must be the protocol source or derived from it)")
    p.add_argument("--protocol", help="campaign-freeze: the ACTIVE protocol id (default: the only ACTIVE protocol)")
    p.add_argument("--max-failures", type=int, default=0,
                   help="campaign-run: stop after this many failed cells (default 0 = stop at the first failure)")
    p.add_argument("--workers", type=int, help="run: worker processes (default: the spec's workers, 1)")
    p.add_argument("--processes", type=int, default=1,
                   help="campaign-run: CPU cores computing cells at once (default 1; results are identical)")
    p.add_argument("--metric", help="rank: expectancy_r | profit_factor | net_r (default: the search spec's)")
    p.add_argument("--min-sample-label", help="rank: LOW SAMPLE SIZE | MODERATE SAMPLE | ADEQUATE SAMPLE")

    a = ap.parse_args(argv)
    from edgelab.data.importer import ImportFailed
    from edgelab.data.store import SearchStorageUnsupported
    from edgelab.engine.costs import CostConfigError
    from edgelab.research.ranking import RankingError
    from edgelab.research.search import SearchSpecError
    from edgelab.services import Services
    from edgelab.strategy.compiler import StrategyCompileError
    from edgelab.strategy.dsl import StrategyValidationError
    from edgelab.strategy.variations import VariationError
    svc = Services(root=a.root)
    try:
        if a.cmd == "inspect":
            from edgelab.data.importer import ImportOptions, inspect_file
            out = inspect_file(ImportOptions(**_options(a, False)), svc.cfg, a.view_timezone)
            prof = out.pop("session_profile", None)
            _print(out, a.json)
            if prof and not a.json:
                import pandas as pd
                print(f"\nbars per local hour ({out['session_profile_tz']}), use to choose/verify the calendar:")
                print(pd.DataFrame(prof).T.to_string())
        elif a.cmd == "import":
            r = svc.import_file(_options(a, True))
            _print(r, a.json)
        elif a.cmd == "datasets":
            _print(svc.list_datasets(), a.json)
        elif a.cmd == "dataset":
            _print(svc.dataset_quality(a.dataset_id) if a.quality else svc.dataset_detail(a.dataset_id), a.json)
        elif a.cmd == "prefer-dataset":
            if a.clear:
                _print(svc.clear_preferred_dataset(), a.json)
            elif a.dataset_id:
                _print(svc.set_preferred_dataset(a.dataset_id), a.json)
            else:
                _print(svc.preferred_dataset(), a.json)
        elif a.cmd == "features":
            if a.action == "list":
                rows = [{"id": d["id"], "v": d["version"], "category": d["category"],
                         "requires": ",".join(d["requires"]) or "-", "known_at": d["known_at"],
                         "summary": d["summary"]} for d in svc.feature_catalog()]
                _print(rows if not a.json else svc.feature_catalog(), a.json)
            elif a.action == "docs":
                from edgelab.features.docs import render_markdown
                sys.stdout.write(render_markdown())
            elif not a.dataset_id:
                ap.error("features build/cache need a DATASET_ID")
            elif a.action == "build":
                _print(svc.build_features(a.dataset_id), a.json)
            else:
                _print(svc.feature_cache_status(a.dataset_id), a.json)
        elif a.cmd == "compare-feeds":
            _print(svc.compare_feeds(a.a, a.b), a.json)
        elif a.cmd == "sessions":
            _print([v.definition() for v in svc.sessions.values()], a.json)
        elif a.cmd == "strategy":
            return _strategy(svc, a, ap)
        elif a.cmd == "research":
            return _research(svc, a)
        elif a.cmd == "validate":
            try:
                if a.kind == "oos":
                    if not a.split:
                        raise ValueError("oos needs --split")
                    r = svc.evaluate_oos(a.strategy, a.dataset_id, a.split, a.record, a.sims, a.seed)
                elif a.kind == "control":
                    r = svc.random_entry_control(a.strategy, a.dataset_id, a.controls, a.seed)
                else:
                    if not (a.train_months and a.test_months):
                        raise ValueError("walkforward needs --train-months and --test-months")
                    r = svc.walk_forward(a.strategy, a.dataset_id, a.train_months, a.test_months,
                                         a.anchored, a.record, a.sims, a.seed)
                _print(r, True)
            except ValueError as exc:
                print(f"validation refused: {exc}", file=sys.stderr)
                return 2
        elif a.cmd == "prop":
            return _prop(svc, a)
        elif a.cmd == "factory":
            return _factory(svc, a, ap)
        elif a.cmd == "report":
            try:
                _print(svc.research_report(a.run_ids), True)
            except ValueError as exc:
                print(f"report refused: {exc}", file=sys.stderr)
                return 2
    except SearchSpecError as exc:
        print(f"search refused: {exc}", file=sys.stderr)
        return 2
    except (RankingError, SearchStorageUnsupported) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except StrategyValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (VariationError, StrategyCompileError, CostConfigError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except ImportFailed as exc:
        print(f"IMPORT FAILED {exc}", file=sys.stderr)
        if exc.report is not None:
            print(exc.report.to_text(), file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 3
    return 0


def _factory(svc, a, ap) -> int:
    if a.action == "generate":
        r = svc.factory_generate(a.seed)
        if a.json:
            _print(r, True)
        else:
            c = r["counts"]
            print(f"manifest {r['manifest_id']} -> {r['path']}\n  candidates {c['candidates_generated']}, valid unique "
                  f"{c['valid_unique']}, rejected {c['rejected']}, duplicates {c['duplicates']}\n"
                  "  (generation only: no market data read, no numerical trials, no holdout looks)")
        return 0
    if a.action == "generate-pool2":              # ADR-87: needs the workspace's pool-1 campaign manifest
        from edgelab.research import pool2
        r = pool2.generate(svc, seed=a.seed)
        _print(r, a.json)
        return 0
    if a.action == "list":
        _print(svc.factory_manifests(), a.json)
        return 0
    if a.action == "capabilities":
        from edgelab.strategy import capabilities
        print(capabilities.render_markdown(), end="")
        return 0
    if not a.manifest_id:
        ap.error(f"factory {a.action} needs a manifest id (FM_...)")
    if a.action == "summary":
        _print(svc.factory_summary(a.manifest_id), True)
    elif a.action == "query":
        filters = dict(f.split("=", 1) for f in a.filter)
        _print(svc.factory_query(a.manifest_id, filters, a.limit, a.offset), True)
    else:
        r = svc.factory_verify(a.manifest_id)
        _print(r, a.json)
        return 0 if r["reproducible"] else 2
    return 0


def _strategy(svc, a, ap) -> int:
    need = {"validate": 1, "compile": 1, "explain": 1, "save": 1, "variations": 2, "proposals": 1,
            "list": 0, "show": 1, "lineage": 1, "menu": 0, "backtest": 2}[a.action]
    if len(a.args) != need:
        ap.error(f"strategy {a.action} takes {need} argument(s)")
    x = a.args
    if a.action == "validate":
        r = svc.validate_strategy(x[0])
        if a.json:
            _print(r, True)
        else:
            print(r["report"])
            if r["valid"]:
                print(f"\nstrategy_id: {r['identity']['strategy_id']}\nlogic_hash: {r['identity']['logic_hash']}"
                      f"\ndefinition_hash: {r['identity']['definition_hash']}")
        return 0 if r["valid"] else 2
    if a.action == "explain":
        r = svc.preview_strategy(x[0])
        print(r["explain"]) if not a.json else _print(r, True)
    elif a.action == "compile":
        r = svc.compile_strategy(x[0])
        if a.json:
            _print(r, True)
        else:
            print(r["explain"])
            print(f"\norder: {r['order']}\ncompiler: {r['provenance']['compiler_version']}  "
                  f"dsl: v{r['provenance']['dsl_version']}")
    elif a.action == "save":
        _print(svc.save_strategy(x[0]), a.json)
    elif a.action == "variations":
        r = svc.generate_variations(x[0], x[1], save=not a.no_save)
        if a.json:
            _print(r, True)
        else:
            print(f"batch {r['batch_id']}: {r['combinations']} combinations -> {r['generated']} variants "
                  f"({r['duplicates_removed']} logic duplicates removed, {r['same_as_base']} identical to base)"
                  f"{'; saved to the strategy library' if r['saved'] else '; not saved'}")
            for v in r["variants"]:
                ch = ", ".join(f"{c['parameter']}: {c['old']} -> {c['new']}" for c in v["changes"])
                print(f"  {v['strategy_id']}  {ch}")
    elif a.action == "proposals":
        r = svc.ingest_proposals(x[0], save=not a.no_save)
        if a.json:
            _print(r, True)
        else:
            print(f"proposal batch {r['batch_id']}: {r['n_accepted']} accepted, {r['n_rejected']} rejected")
            for acc in r["accepted"]:
                print(f"  ACCEPTED [{acc['index']}] {acc['family_id']} -> {acc['strategy_id']}")
            for rej in r["rejected"]:
                print(f"  REJECTED [{rej['index']}] {rej['family_id']}: " + "; ".join(rej["errors"]))
            for w in r["warnings"]:
                print(f"  WARNING {w}")
    elif a.action == "list":
        _print(svc.list_strategies(a.family), a.json)
    elif a.action == "show":
        _print(svc.load_strategy(x[0]), a.json)
    elif a.action == "lineage":
        _print(svc.strategy_lineage(x[0]), a.json)
    elif a.action == "menu":
        _print(svc.proposal_menu(a.n_families), True)
    elif a.action == "backtest":
        _print(svc.backtest_strategy(x[0], x[1]), a.json)
    return 0


def _research(svc, a) -> int:
    """Thin wrappers over the Phase 4 research services (the same calls /api/research makes)."""
    if a.action.startswith("campaign-"):
        return _campaign(svc, a)
    if a.action == "validate":
        r = svc.validate_search(a.target)
        if a.json:
            _print(r, True)
        else:
            print("search spec is valid" if r["valid"] else f"search spec is invalid ({len(r['errors'])} error(s)):")
            for i in r["errors"]:
                print(f"  {i['path']}: {i['message']}" + (f" ({i['hint']})" if i.get("hint") else ""))
            if r["valid"]:
                print(f"search_hash: {r['search_hash']}")
        return 0 if r["valid"] else 2
    if a.action == "plan":
        r = svc.plan_search(a.target)
        if a.json:
            _print(r, True)
        else:
            print(f"{r['search_id']}: {r['counts']}")
            for w in r["warnings"]:
                print(f"  WARNING {w}")
            for c in r["cells"]:
                print(f"  [{c['plan_index']}] {c['cell_id']} {c['strategy_id']} x {c['dataset_id']} "
                      + ("eligible" if c["eligible"] else "INELIGIBLE: " + "; ".join(c["reasons"])))
        return 0
    if a.action == "run":
        r = svc.run_search(a.target, workers=a.workers)
        _print(r if a.json else {k: r[k] for k in ("search_id", "status", "n_planned", "n_eligible", "n_ineligible",
                                                   "n_evaluated", "n_skipped_resume", "n_failed", "n_trials",
                                                   "cumulative", "execution", "note")}, a.json)
        return 0
    if a.action == "rank":
        r = svc.rank_search(a.target, a.metric, a.min_sample_label)
        if a.json:
            _print(r, True)
        else:
            print(r["label"])
            print(f"metric={r['metric']} min_sample_label={r['min_sample_label']} excluded={r['excluded']}")
            for x in r["ranked"]:
                val = "+inf" if x.get("value_infinite") else x["value"]
                print(f"  {x['rank']:>3}  {x['strategy_id']} x {x['dataset_id']}  {r['metric']}={val}  "
                      f"trades={x['metrics'].get('trade_count')} [{x['metrics'].get('sample_label')}]")
            print(r["note"])
        return 0
    return _research_job(svc, a)


def _campaign(svc, a) -> int:
    """Frozen-manifest discovery campaign (ADR-68): freeze -> check (read-only) -> run (workers 1, resumable)."""
    from edgelab.research.campaign import CampaignError
    try:
        if a.action == "campaign-freeze":
            ds = dict(x.split("=", 1) for x in a.dataset)
            r = svc.campaign_freeze(a.target, protocol_id=a.protocol, datasets=ds or None)
            _print(r, a.json)
            return 0
        if a.action == "campaign-check":
            r = svc.campaign_check(a.target)
            if a.json:
                _print(r, True)
            else:
                for c in r["checks"]:
                    print(f"  [{'OK  ' if c['ok'] else 'FAIL'}] {c['check']}" + ("" if c["ok"] else f": {c['detail']}"))
                print(("READY" if r["ready"] else "NOT READY") + f" - {r['campaign_id']} ({r['note']})")
            return 0 if r["ready"] else 2
        if a.action == "campaign-run":
            r = svc.campaign_run(a.target, workers=a.workers if a.workers is not None else 1, max_failures=a.max_failures,
                                 processes=a.processes)
            _print(r, a.json)
            return 0 if r["complete"] else 1
        _print(svc.campaign_status(a.target), a.json)
        return 0
    except CampaignError as exc:
        print(json.dumps(exc.to_dict(), indent=1, default=str), file=sys.stderr)
        return 2


def _research_job(svc, a) -> int:
    """Start a background search job and follow it until it ends. Ctrl-C requests cooperative
    cancellation (the running cell finishes). Job ids live in this process only; the stored search
    (SRCH_ id) persists and can be resumed with `research run`."""
    st = svc.start_search_job(a.target)
    jid = st["job_id"]
    if not a.json:
        print(f"job {jid} started for {st['search_id']} (Ctrl-C cancels)")
    try:
        while st["state"] not in ("completed", "failed", "cancelled"):
            svc.jobs.join(timeout=1.0)                     # progress interval only
            st = svc.job_status(jid)
            if not a.json:
                p = st["progress"]
                if p.get("stored"):
                    print(f"  {st['state']}: evaluated {p['evaluated']}/{p['eligible']} eligible, "
                          f"failed {p['failed']}, skipped {p['skipped_resume']}")
    except KeyboardInterrupt:
        svc.cancel_job(jid)
        print(f"cancellation requested for {jid}; the running cell will finish", file=sys.stderr)
        svc.jobs.join()
    st = svc.job_status(jid)
    _print(st if a.json else {k: st[k] for k in ("job_id", "search_id", "state", "error", "progress")}, a.json)
    return {"completed": 0, "cancelled": 130}.get(st["state"], 1)



def _prop_source(x: str):
    from pathlib import Path
    return Path(x).read_text() if Path(x).is_file() else x


def _prop(svc, a) -> int:
    from edgelab.prop.rules import PropConfigError
    from edgelab.prop.simulator import PropDataError
    try:
        if a.action == "profiles":
            r = svc.prop_profiles()
            _print(r, True)
            return 0 if not r["registry_problems"] else 2
        if a.action == "lifecycle":
            if not a.target:
                raise ValueError("prop lifecycle needs a RUN_ id")
            r = svc.prop_lifecycle(a.target)
            if a.json:
                _print(r, True)
            else:
                for pr in r["profiles"]:
                    print("\n".join(pr.get("headline", [pr.get("final_status", "")])))
                    print(f"  rule profile: {pr['profile']['profile_id']} v{pr['profile']['version']} "
                          f"hash {pr['profile'].get('profile_hash', '')[:12]}  status: {pr['profile'].get('verification_status')}")
            return 0
        if a.action == "configs":
            _print([{k: c[k] for k in ("file", "id", "name", "valid", "synthetic_test_only", "config_hash", "errors")}
                    for c in svc.prop_configs()], a.json)
        elif a.action == "list":
            _print(svc.list_prop_simulations(), a.json)
        elif not a.target:
            raise ValueError(f"prop {a.action} needs a target")
        elif a.action == "validate":
            r = svc.validate_prop_config(_prop_source(a.target))
            _print(r, True)
            return 0 if r["valid"] else 2
        elif a.action == "show":
            _print(svc.get_prop_simulation(a.target), True)
        else:
            if not a.config or a.accounts < 1:
                raise ValueError("simulate needs at least one --config and --accounts >= 1")
            accounts = [{"account_id": f"A{i + 1}", "config": _prop_source(c)}
                        for i, c in enumerate(c for c in a.config for _ in range(a.accounts))]
            r = svc.prop_simulate(a.target, accounts, a.record)
            if a.json:
                _print(r, True)
            else:
                _print({"simulation_id": r["simulation_id"], "recorded": r["recorded"], "labels": r["labels"],
                        "lineage": r["lineage"], "strategy_result": r["strategy_result"],
                        "accounts": [x["summary"] for x in r["accounts"]]}, True)
    except PropConfigError as exc:
        print("prop rule set refused:\n  " + "\n  ".join(exc.errors), file=sys.stderr)
        return 2
    except (PropDataError, ValueError, KeyError) as exc:
        print(f"prop simulation refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

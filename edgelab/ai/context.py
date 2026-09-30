"""The context an AI provider may see (versioned, BLIND to results).

Contains only what defines the research space: dataset identities and coverage (no bars), the
preferred dataset, instrument identity metadata, the DSL capability menu (features, constructs,
sessions, parameter domains), cost-profile STATUS caveats and the resolved request scope. It is
built without touching the run registry, searches, validations, controls or prop simulations, so
no result of any kind (in-sample, OOS, walk-forward, control, prop) can reach a provider, and
there is no path from results back into generation. `context_hash` identifies it exactly.
"""
from __future__ import annotations

from typing import Any, Mapping

from edgelab.core.identity import hash_obj

CONTEXT_VERSION = 1
DATASET_FIELDS = ("dataset_id", "dataset_name", "provider", "asset_type", "instrument", "symbol", "timeframe",
                  "start", "end", "n_bars", "quality_status", "content_hash", "volume_type", "price_basis",
                  "parent_dataset_id")
# Keys that would indicate a result leaking into the context; tests assert none appear.
FORBIDDEN_KEYS = {"metrics", "run_id", "runs", "trades", "trades_hash", "profit_factor", "expectancy", "win_rate",
                  "net_r", "drawdown", "equity", "oos", "walk_forward", "control", "prop", "sharpe", "pnl"}


class DiscoveryScopeError(ValueError):
    pass


def resolve_scope(svc, scope: Mapping) -> dict:
    """Fill the scope from the dataset (default: the workspace's preferred research dataset).
    A stated instrument/timeframe must agree with the dataset; nothing is guessed."""
    rows = {d["dataset_id"]: d for d in svc.list_datasets()}
    did = scope.get("dataset_id") or svc.preferred_dataset_id()
    if not did:
        raise DiscoveryScopeError("no research dataset: choose one in the scope or set a Preferred Research "
                                  "Dataset (Datasets page)")
    if did not in rows:
        raise DiscoveryScopeError(f"dataset {did} is not stored in this workspace")
    d = rows[did]
    if d.get("quality_status") == "FAIL":
        raise DiscoveryScopeError(f"dataset {did} failed validation")
    for k in ("instrument", "timeframe"):
        if scope.get(k) and scope[k] != d[k]:
            raise DiscoveryScopeError(f"scope.{k} {scope[k]!r} does not match dataset {did} ({d[k]})")
    ds_ = scope.get("date_scope")
    if ds_:
        lo, hi = str(d["start"])[:10], str(d["end"])[:10]
        for x in ("start", "end"):
            if ds_.get(x) and not lo <= ds_[x] <= hi:
                raise DiscoveryScopeError(f"scope.date_scope.{x} {ds_[x]} is outside the dataset ({lo} .. {hi})")
    out = {"dataset_id": did, "instrument": d["instrument"], "timeframe": d["timeframe"],
           "session": scope.get("session"), "direction": scope.get("direction"),
           "date_scope": ds_, "dataset_is_preferred": did == svc.preferred_dataset_id()}
    gp = getattr(svc, "_governing_protocol", None)
    p = gp(d["instrument"], d["provider"]) if gp is not None else None
    if p is not None:                            # ADR-56: discovery may never be scoped onto the locked holdout
        from edgelab.research.protocol import ProtocolRefusal
        disc = p["material"]["windows"]["discovery"]["trading_dates"]
        if not (ds_ and ds_.get("start") and ds_.get("end")):
            raise ProtocolRefusal("AI_SCOPE_DATES_REQUIRED", "under an active research protocol the request must state "
                                  "scope.date_scope inside the discovery window", protocol_id=p["protocol_id"],
                                  discovery_trading_dates=disc)
        if ds_["start"] < disc[0] or ds_["end"] > disc[1]:
            raise ProtocolRefusal("AI_SCOPE_HOLDOUT_OVERLAP", "scope.date_scope reaches outside the discovery window "
                                  "(the holdout is locked)", protocol_id=p["protocol_id"], date_scope=ds_,
                                  discovery_trading_dates=disc)
        out.update(protocol_id=p["protocol_id"], discovery_trading_dates=list(disc))
    return out


def build_context(svc, request: Mapping, scope: Mapping, base_definition: Mapping | None = None) -> dict:
    from edgelab.instruments import identity_info, load_instruments
    menu = svc.proposal_menu(n_families=request["n_proposals"], instructions="")
    instruments = load_instruments(svc.cfg)
    datasets = []
    for d in svc.list_datasets():
        row = {k: d.get(k) for k in DATASET_FIELDS}
        datasets.append(row)
    inst = instruments.get(scope["instrument"])
    ident = identity_info(inst) if inst is not None else {"identity_status": "unknown"}
    elig = svc._dataset_eligibility(next(d for d in svc.list_datasets() if d["dataset_id"] == scope["dataset_id"]))
    caveats = []
    if elig["cost"]["status"] == "unconfigured" or elig["cost"].get("incomplete"):
        caveats.append("the research dataset's cost profile is UNCONFIGURED or INCOMPLETE: backtests are refused "
                       "until costs are entered; do not assume any cost")
    if ident.get("identity_status") == "provisional":
        caveats.append("the instrument's source identity is PROVISIONAL (not CME NQ; economics unknown): "
                       "express stops/targets in points or ATR, never in currency")
    caveats += list(elig["limitations"])
    ctx = {"context_version": CONTEXT_VERSION,
           "blind": "no backtest, OOS, walk-forward, control or prop results are included, by construction",
           "scope": dict(scope),
           "preferred_dataset_id": svc.preferred_dataset_id(),
           "datasets": sorted(datasets, key=lambda r: r["dataset_id"]),
           "instrument": {"symbol": scope["instrument"], **{k: ident.get(k) for k in (
               "identity_status", "research_proxy", "source_provider", "source_symbol", "asset_class",
               "price_source", "tick_size", "point_value", "description")}},
           "cost_status": elig["cost"]["status"],
           "caveats": caveats,
           "dsl": {k: menu[k] for k in ("dsl_version", "operators", "bar_fields", "arithmetic", "entry_orders",
                                        "directions", "stop_types", "target_types", "sizing_modes",
                                        "unsupported", "sessions", "features")},
           "rules": ["return data only (the proposal schema); never code",
                     "state hypotheses; never state or estimate performance",
                     "use only the listed features, constructs and sessions",
                     "declare every tunable number as a DSL parameter with min/max/step"],
           "base_strategy": dict(base_definition) if base_definition is not None else None}
    leaks = sorted(_keys(ctx) & FORBIDDEN_KEYS)
    if leaks:                                    # defence in depth: never send a result-shaped key
        raise AssertionError(f"discovery context would leak result keys {leaks}")
    ctx["context_hash"] = hash_obj({k: v for k, v in ctx.items()})
    return ctx


def _keys(node: Any) -> set:
    out: set = set()
    if isinstance(node, Mapping):
        for k, v in node.items():
            out.add(str(k))
            out |= _keys(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            out |= _keys(v)
    return out

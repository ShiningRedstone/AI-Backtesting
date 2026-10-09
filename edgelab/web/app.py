"""HTTP API over ``edgelab.services`` (Phase 3.5).

Rules
  * Routing and input checking only. Every capability is a ``Services`` method; nothing
    here validates, canonicalizes, compiles, sizes or backtests anything itself.
  * The browser sends JSON data only. A strategy source is a JSON object (a DSL document)
    or a stored id ``STR_<12 hex>``. The service layer can also read file paths and YAML
    text; that is deliberately NOT reachable from HTTP.
  * Imports read files only from the configured ``import_dirs``.
  * Errors come back as ``{"error": {kind, message, issues?, details}}``. ``message`` is for
    people; ``details`` (exception type / traceback) is for the "technical details" panel.
  * No credentials, no broker access, no order execution, no code execution.
"""
from __future__ import annotations

import dataclasses
import re
import sqlite3
import threading
import traceback
from pathlib import Path
from typing import Mapping, Any, Callable

from flask import Flask, jsonify, request, send_from_directory

from edgelab.web.config import WebConfig, load_web_config

from edgelab.runtime import resource_dir, runtime_info, static_dir

STATIC = static_dir()
STRATEGY_ID = re.compile(r"^STR_[0-9A-F]{12}$")
BATCH_ID = re.compile(r"^VB_[0-9A-F]{12}$")
RUN_ID = re.compile(r"^RUN_\d{4}_\d{5}$")
SEARCH_ID = re.compile(r"^SRCH_[0-9A-F]{12}$")
JOB_ID = re.compile(r"^JOB_[0-9A-F]{12}$")
PROP_SIM_ID = re.compile(r"^PROP_[0-9A-F]{16}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_\-.]{1,120}$")
AI_GEN_ID = re.compile(r"^AIG_[0-9A-F]{12}$")
AI_PROP_ID = re.compile(r"^AIP_[0-9A-F]{12}$")
PROTOCOL_ID = re.compile(r"^RP_[0-9A-F]{12}$")
CAMPAIGN_ID = re.compile(r"^CMP_[0-9A-F]{12}$")
RUN_RECORD_ID = re.compile(r"^CR_\d{8}_\d{6}_[0-9A-F]{6}$")
CONTROL_ID = re.compile(r"^CTRL_[0-9A-F]{12}$")
BT_JOB_ID = re.compile(r"^BTJ_[0-9A-F]{12}$")
FAMILY_ID = re.compile(r"^[a-z0-9_]{1,64}$")
FACTORY_ID = re.compile(r"^FM_[0-9A-F]{16}$")
MY_REPORT_ID = re.compile(r"^(BT|HO|HD|PL)_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$")       # ADR-93
MY_EXPORT_ID = re.compile(r"^(BT|HO|HD|PL|SR)_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$")      # ADR-96: + setup reviews
MY_SETUP_ID = re.compile(r"^SR_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$")
MY_BT_ID = re.compile(r"^BT_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$")
MY_JOB_ID = re.compile(r"^MSJ_[0-9a-f]{12}$")
MY_OPT_ID = re.compile(r"^OPT_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$")                   # ADR-101: autotuner runs


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str, **extra):
        super().__init__(message)
        self.status, self.kind, self.message, self.extra = status, kind, message, extra


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def strategy_source(x: Any) -> Any:
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and STRATEGY_ID.match(x):
        return x
    raise _bad("a strategy must be a JSON object (DSL document) or a strategy id like STR_0123456789AB")


CHART_SYMBOL = re.compile(r"\A(MNQ|NQ|ES|MES)\Z")                 # ADR-111
SIM_ID = re.compile(r"\ASIM[0-9A-F]{10}\Z")                        # ADR-112
SIM_ACTION = re.compile(r"\A(order|modify|cancel|flatten|reverse|reset|rename|delete)\Z")
CHART_TF = re.compile(r"\A([0-9]{1,4}[mh]?|1D|1W|1M)\Z")
MARKET_SECTION = re.compile(r"\A[a-z_]{1,32}\Z")                 # ADR-106
MARKET_DAY = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
EDGE_SOURCE = re.compile(r"\A[A-Za-z0-9_.:\-]{1,200}\Z")      # ADR-105: "discovery" or a dataset id


def _id(x: Any, pattern: re.Pattern, what: str) -> str:
    if not isinstance(x, str) or not pattern.match(x):
        raise _bad(f"invalid {what}")
    return x


def warm_caches(svc) -> None:
    """Fill the read-only view caches (strategy facets, run rows, prop profiles, campaign views) in the background right
    after start, so the first page does not pay for them. Under the service lock like every request; failures are
    ignored (the pages compute the same values on first use). Only the launchers turn it on (``create_app(warm=True)``):
    their shutdown closes the store under the same lock, so the warm-up never races a closing database."""
    def work():
        from edgelab.prop.service import default_profiles
        from edgelab.research import campaign as C
        from edgelab.research import overview as ov
        steps = [lambda: default_profiles(svc.root), lambda: ov.library_facets(svc), lambda: ov.run_records(svc),
                 lambda: svc.library._all_lineage()]
        root = C.campaigns_dir(svc)
        for d in sorted(root.glob("CMP_*")) if root.is_dir() else []:
            steps += [lambda cid=d.name: svc.campaign_detail(cid), lambda cid=d.name: svc.campaign_tree(cid)]
        for step in steps:                                   # one step at a time: requests can run in between
            try:
                with svc.lock:
                    step()
            except Exception:                                # noqa: BLE001 - a warm-up never affects the app
                pass
    threading.Thread(target=work, daemon=True, name="munyun-cache-warmup").start()
    if (Path(svc.data_root) / "charts" / "sim").is_dir():              # ADR-112: working simulated orders keep working
        try:
            svc._sim()._kick()
        except Exception:                                    # noqa: BLE001 - shown on the Charts tab instead
            pass


def create_app(root: str | Path = ".", demo: bool = False, web: WebConfig | None = None, warm: bool = False) -> Flask:
    from edgelab.services import Services

    root = Path(root).resolve()
    web = web or load_web_config(root)
    svc = Services(root=root)
    lock = svc.lock                  # the one service lock (shared with the Phase 4 job manager)
    if svc.store.backend == "sqlite":
        svc.jobs                     # start the job manager: searches a dead process left `running` -> interrupted
    if warm:
        warm_caches(svc)
        svc.paper.start()            # ADR-81: daily paper updates (launchers only, like the warm-up)
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = int(web.max_request_mb * 1024 * 1024)
    app.config["EDGELAB"] = {"root": root, "demo": demo, "services": svc, "web": web}

    def call(fn: Callable, *a, **kw):
        # ADR-78: page reads (GET) run on a pooled read-only connection WITHOUT the service lock, so pages load
        # while a research run holds it. A GET that turns out to write is retried once under the lock on the writer.
        if request.method == "GET":
            with svc.read_context() as ok:
                if ok:
                    try:
                        return fn(*a, **kw)
                    except sqlite3.OperationalError as exc:
                        if "readonly" not in str(exc):
                            raise
        with lock:
            return fn(*a, **kw)

    def body() -> dict:
        if not request.is_json:
            raise _bad("request body must be JSON (Content-Type: application/json)")
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise _bad("request body must be a JSON object")
        return data

    # ------------------------------------------------------------------ errors
    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        return jsonify({"error": {"kind": e.kind, "message": e.message, **e.extra}}), e.status

    @app.errorhandler(Exception)
    def _any_error(e: Exception):
        from werkzeug.exceptions import HTTPException

        from edgelab.data.importer import ImportFailed
        from edgelab.data.store import SearchStorageUnsupported
        from edgelab.engine.backtester import BacktestError
        from edgelab.engine.costs import CostConfigError
        from edgelab.ai.context import DiscoveryScopeError
        from edgelab.ai.providers import ProviderError
        from edgelab.ai.schema import DiscoveryRequestError
        from edgelab.instruments import InstrumentIdentityError
        from edgelab.prop.rules import PropConfigError
        from edgelab.prop.simulator import PropDataError
        from edgelab.research.jobs import JobConflict
        from edgelab.research.campaign import CampaignError
        from edgelab.research.flips import FlipError
        from edgelab.research.ranking import RankingError
        from edgelab.research.search import SearchSpecError
        from edgelab.strategy.compiler import StrategyCompileError
        from edgelab.strategy.dsl import StrategyValidationError
        from edgelab.strategy.variations import VariationError
        details = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        if isinstance(e, HTTPException):
            kind = "not_found" if e.code == 404 else "http"
            return jsonify({"error": {"kind": kind, "message": e.description}}), e.code
        from edgelab.research.protocol import ProtocolRefusal
        if isinstance(e, ProtocolRefusal):                   # ADR-56: machine-readable refusal code
            return jsonify({"error": {"kind": "protocol_refusal", "code": e.code, "message": e.message,
                                      "refusal": e.to_dict()}}), 409
        if isinstance(e, FlipError):                         # ADR-88: flip scan refusals (machine-readable code)
            return jsonify({"error": {"kind": "flip_refusal", "code": e.code, "message": e.message,
                                      "refusal": e.to_dict()}}), 409 if e.code in ("FLIP_EXISTS", "PROTOCOL_NOT_ACTIVE") else 422
        from edgelab.market.data import MarketDataError
        from edgelab.market.news import NewsError
        from edgelab.market.newdays import NewDaysError
        from edgelab.market.holdout import HoldoutTestError
        if isinstance(e, (MarketDataError, NewsError, NewDaysError, HoldoutTestError)):      # ADR-106 / ADR-107
            return jsonify({"error": {"kind": "market", "code": e.code, "message": e.message}}), \
                409 if e.code == "HOLDOUT_LOOK_USED" else 422
        from edgelab.charts.feed import ChartError
        if isinstance(e, ChartError):                        # ADR-111: live charts
            return jsonify({"error": {"kind": "charts", "code": e.code, "message": e.message}}), \
                503 if e.code == "DOWNLOAD_FAILED" else 422
        from edgelab.charts.sim import SimError
        if isinstance(e, SimError):                          # ADR-112: simulated accounts (orders refused with a reason)
            return jsonify({"error": {"kind": "sim", "code": e.code, "message": e.message}}), \
                404 if e.code == "NO_ACCOUNT" else 409 if e.code in ("ACCOUNT_CLOSED", "NO_PRICES") else 422
        from edgelab.mystrategy.params import SettingsError
        from edgelab.mystrategy.runner import MyStrategyError
        if isinstance(e, SettingsError):                     # ADR-93
            return jsonify({"error": {"kind": "my_strategy_settings", "message": "Some settings are not valid.",
                                      "issues": [{"severity": "error", "message": m} for m in e.issues]}}), 422
        if isinstance(e, MyStrategyError):
            return jsonify({"error": {"kind": "my_strategy", "code": e.code, "message": e.message}}), \
                409 if e.code in ("JOB_RUNNING", "REVIEW_OPEN", "HOLDOUT_LOOKS_USED", "NOT_CURRENT",
                              "SETUP_REVIEW_OPEN", "AUTOTUNE_RUNNING") else 422
        if isinstance(e, SearchSpecError):
            return jsonify({"error": {"kind": "search_spec", "message": "The search was refused.",
                                      "reason": str(e), "issues": [i.to_dict() for i in e.issues],
                                      "details": details}}), 422
        if isinstance(e, PropConfigError):
            return jsonify({"error": {"kind": "prop_config", "message": "The prop rule set is not valid.",
                                      "reason": "; ".join(e.errors),
                                      "issues": [{"severity": "error", "message": m} for m in e.errors],
                                      "details": details}}), 422
        if isinstance(e, DiscoveryRequestError):
            return jsonify({"error": {"kind": "ai_request", "message": "The discovery request was refused.",
                                      "reason": "; ".join(e.errors),
                                      "issues": [{"severity": "error", "message": m} for m in e.errors]}}), 422
        if isinstance(e, StrategyValidationError):
            return jsonify({"error": {"kind": "validation", "message": "The strategy is not valid.",
                                      "issues": [i.to_dict() for i in e.result.issues],
                                      "details": str(e)}}), 422
        table = [(InstrumentIdentityError, 409, "instrument_identity",
                  "Research unavailable: this instrument's source identity or session calendar is not yet "
                  "established (see the reason)."),
                 (DiscoveryScopeError, 422, "ai_scope", "The discovery scope was refused."),
                 (ProviderError, 503, "ai_provider", "The AI provider is unavailable."),
                 (VariationError, 422, "variation", "Variation generation was refused."),
                 (StrategyCompileError, 422, "compile", "The strategy cannot run on this dataset."),
                 (CostConfigError, 409, "cost_unconfigured",
                  "Backtest unavailable: the broker/provider cost profile is unconfigured. "
                  "Configure verified costs before running research."),
                 (BacktestError, 422, "backtest", "The backtest was stopped."),
                 (PropDataError, 422, "prop_data",
                  "The stored trades cannot support these account rules honestly; nothing was simulated."),
                 (RankingError, 422, "ranking", "The ranking request was refused."),
                 (JobConflict, 409, "job_conflict", "Another research job is still active; one runs at a time."),
                 (CampaignError, 422, "campaign", "The campaign request was refused."),
                 (SearchStorageUnsupported, 409, "search_storage_unsupported",
                  "Research searches need the SQLite result store."),
                 (ImportFailed, 422, "import_failed", "The import was refused."),
                 (KeyError, 404, "not_found", "Not found."),
                 (FileNotFoundError, 404, "not_found", "File not found.")]
        for cls, status, kind, headline in table:
            if isinstance(e, cls):
                reason = str(e.args[0]) if e.args else str(e)
                return jsonify({"error": {"kind": kind, "message": headline, "reason": reason,
                                          "details": details}}), status
        if isinstance(e, ValueError):
            return jsonify({"error": {"kind": "invalid_request", "message": str(e), "details": details}}), 400
        app.logger.exception("unhandled error")
        return jsonify({"error": {"kind": "internal", "message": "Unexpected server error.",
                                  "details": details}}), 500

    # ------------------------------------------------------------------ system
    @app.get("/api/health")
    def health():
        return jsonify({"backend": "ok", "demo": demo})

    @app.get("/api/status")
    def status():
        s = call(svc.system_status)
        tests = root / "reports" / "last_test_run.txt"
        from edgelab.web.bundle import bundle_status
        repo_tests = resource_dir() / "reports" / "last_test_run.txt"
        tests = tests if tests.exists() else repo_tests
        return jsonify({**s, "demo": demo, "root": str(root), "frontend": bundle_status(), "runtime": runtime_info(),
                        "test_status": tests.read_text().strip() if tests.exists() else None})

    @app.get("/api/workspace")
    def workspace():
        """Read-only here: `python -m edgelab.web` serves one fixed --root. The desktop app's workspace
        host answers this route itself (with switching)."""
        from edgelab import runtime
        return jsonify({"current": {**runtime.inspect_workspace(root), "source": "--root (development server)"},
                        "switchable": False, "notice": None, "default": None, "browse_available": False,
                        "settings_path": None})

    @app.get("/api/options")
    def options():
        return jsonify(call(svc.builder_options, web.builder_timeframes))

    @app.get("/api/features")
    def features():
        return jsonify(call(svc.feature_catalog))

    @app.get("/api/config")
    def config():
        from edgelab.engine.costs import CostConfigError, cost_model_from_config
        from edgelab.instruments import load_instruments
        from edgelab.runtime import legacy_hidden
        cfg = svc.cfg
        costs = {}
        for sym in load_instruments(cfg):
            if legacy_hidden(sym):                     # kept only for the settings fingerprint (ADR-89)
                continue
            try:
                cm = cost_model_from_config(cfg, sym)
                costs[sym] = {"status": cm.status, "profile": getattr(cm, "profile", None)}
            except CostConfigError as exc:
                costs[sym] = {"status": "unconfigured", "reason": str(exc)}
        return jsonify({"demo": demo, "root": str(root), "config_hash": svc._config_hash(),
                        "backtest": cfg["backtest"], "sessions": {k: v.definition() for k, v in svc.sessions.items()},
                        "instruments": {k: {"asset_class": v.asset_class, "tick_size": v.tick_size,
                                            "point_value": v.point_value, "calendar": v.calendar}
                                        for k, v in load_instruments(cfg).items() if not legacy_hidden(k)},
                        "cost_profiles": costs, "sample_size": cfg.get("sample_size"),
                        "import_profiles": sorted((cfg.get("import_profiles") or {}).keys()),
                        "web": {"host": web.host, "port": web.port, "import_dirs": web.import_dirs,
                                "builder_timeframes": web.builder_timeframes},
                        "credentials": "none stored; credentials only ever come from environment variables"})

    # ------------------------------------------------------------------ strategies
    @app.post("/api/strategies/render")
    def render():
        return jsonify(call(svc.render_strategy, strategy_source(body().get("definition"))))

    @app.post("/api/strategies/validate")
    def validate():
        return jsonify(call(svc.validate_strategy, strategy_source(body().get("definition"))))

    @app.post("/api/strategies/compile")
    def compile_():
        return jsonify(call(svc.compile_strategy, strategy_source(body().get("definition"))))

    @app.post("/api/strategies/explain")
    def explain_draft():
        return jsonify(call(svc.preview_strategy, strategy_source(body().get("definition"))))

    @app.post("/api/dsl/parse")
    def dsl_parse():
        """YAML/JSON text -> DSL object for the builder. Parsing only (yaml.safe_load)."""
        import yaml
        text = body().get("text")
        if not isinstance(text, str) or len(text) > 1_000_000:
            raise _bad("text must be a string (max 1 MB)")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ApiError(422, "parse", "The text is not valid YAML/JSON.", reason=str(exc)) from None
        if not isinstance(data, dict):
            raise ApiError(422, "parse", "A strategy document must be a mapping at the top level.")
        return jsonify({"definition": data})

    @app.get("/api/strategies")
    def strategies():
        fam = request.args.get("family")
        if fam is not None:
            _id(fam, SAFE_ID, "family id")
        archived = request.args.get("archived") == "1"
        return jsonify(call(svc.list_strategies, fam, archived))

    @app.get("/api/strategies/<sid>")
    def strategy(sid):
        return jsonify(call(svc.load_strategy, _id(sid, STRATEGY_ID, "strategy id")))

    @app.get("/api/strategies/<sid>/explain")
    def explain(sid):
        return jsonify(call(svc.preview_strategy, _id(sid, STRATEGY_ID, "strategy id")))

    @app.get("/api/strategies/<sid>/lineage")
    def lineage(sid):
        return jsonify(call(svc.strategy_lineage, _id(sid, STRATEGY_ID, "strategy id")))

    @app.post("/api/strategies/save")
    def save():
        b = body()
        d = strategy_source(b.get("definition"))
        if not isinstance(d, dict):
            raise _bad("save needs a definition object")
        parent = b.get("parent_strategy_id")
        if not parent:
            return jsonify(call(svc.save_strategy, d))
        parent = _id(parent, STRATEGY_ID, "parent strategy id")
        method = b.get("method", "manual_edit")
        if method not in ("manual_edit", "duplicate"):
            raise _bad("method must be manual_edit or duplicate")
        call(svc.load_strategy, parent)                                  # parent must exist
        v = call(svc.validate_strategy, d)
        if v["valid"] and v["identity"]["strategy_id"] == parent:
            # same logic as the parent: saving would add a self-referencing lineage record
            return jsonify({**v["identity"], "created": False,
                            "note": f"No logic change relative to {parent}; nothing was saved. A strategy is "
                                    "identified by its logic, so name/description edits alone do not create "
                                    "a new instance (use Save As New with changed rules)."})
        return jsonify(call(svc.save_strategy, d, method, parent))

    @app.post("/api/strategies/<sid>/duplicate")
    def duplicate(sid):
        name = body().get("name")
        if not isinstance(name, str) or not name.strip():
            raise _bad("duplicate needs a new name")
        return jsonify(call(svc.duplicate_strategy, _id(sid, STRATEGY_ID, "strategy id"), name.strip()))

    @app.post("/api/strategies/<sid>/archive")
    def archive(sid):
        return jsonify(call(svc.archive_strategy, _id(sid, STRATEGY_ID, "strategy id")))

    @app.post("/api/strategies/<sid>/restore")
    def restore(sid):
        return jsonify(call(svc.restore_strategy, _id(sid, STRATEGY_ID, "strategy id")))

    @app.get("/api/families")
    def families():
        return jsonify(call(svc.strategy_families))

    @app.get("/api/families/<fid>")
    def family(fid):
        return jsonify(call(svc.family_detail, _id(fid, SAFE_ID, "family id")))

    # ------------------------------------------------------------------ strategy factory (read-only)
    @app.get("/api/factory/manifests")
    def factory_manifests():
        return jsonify(call(svc.factory_manifests))

    @app.get("/api/factory/<mid>")
    def factory_summary(mid):
        return jsonify(call(svc.factory_summary, _id(mid, FACTORY_ID, "manifest id")))

    @app.get("/api/factory/<mid>/strategies")
    def factory_strategies(mid):
        args = dict(request.args)
        try:
            limit, offset = int(args.pop("limit", 100)), int(args.pop("offset", 0))
        except ValueError:
            raise _bad("limit/offset must be integers")
        for k, v in args.items():
            _id(k, SAFE_ID, "filter name")
            _id(v, SAFE_ID, "filter value")
        return jsonify(call(svc.factory_query, _id(mid, FACTORY_ID, "manifest id"), args, limit, offset))

    # ------------------------------------------------------------------ variations
    def _spec(b: dict) -> dict:
        spec = b.get("spec")
        if not isinstance(spec, dict):
            raise _bad("a variation spec must be a JSON object")
        return spec

    @app.post("/api/variations/preview")
    def variation_preview():
        b = body()
        return jsonify(call(svc.variation_preview, strategy_source(b.get("base")), _spec(b)))

    @app.post("/api/variations")
    def variations():
        b = body()
        return jsonify(call(svc.generate_variations, strategy_source(b.get("base")), _spec(b),
                            bool(b.get("save", True))))

    @app.get("/api/variation-batches")
    def batches():
        return jsonify(call(svc.list_variation_batches))

    @app.get("/api/variation-batches/<bid>")
    def batch(bid):
        return jsonify(call(svc.get_variation_batch, _id(bid, BATCH_ID, "batch id")))

    # ------------------------------------------------------------------ datasets / import
    @app.get("/api/datasets")
    def datasets():
        return jsonify(call(svc.backtest_readiness)["datasets"])

    @app.get("/api/preferences/risk-per-trade")
    def risk_per_trade():
        return jsonify(call(svc.risk_per_trade))

    @app.post("/api/preferences/risk-per-trade")
    def set_risk_per_trade():
        return jsonify(call(svc.set_risk_per_trade, body().get("risk_per_trade_usd")))

    @app.get("/api/preferences/ui")
    def ui_preferences():
        return jsonify({**call(svc.ui_preferences), "profile_choices": call(svc.prop_profile_choices)})

    @app.post("/api/preferences/ui")
    def set_ui_preferences():
        return jsonify(call(svc.set_ui_preferences, body()))

    # ------------------------------------------------------------------ paper trading (ADR-81)
    PAPER_ID = re.compile(r"^PA_[0-9A-F]{12}$")
    PROFILE_ID = re.compile(r"^[A-Z0-9_]{1,64}$")

    @app.get("/api/paper/candidates")
    def paper_candidates():
        pid = _id(request.args.get("profile_id") or "", PROFILE_ID, "prop rule profile id")
        view = request.args.get("view") or None
        if view is not None and view not in ("holdout", "survivors", "all"):
            raise _bad("view must be holdout, survivors or all")
        return jsonify(call(svc.paper_candidates, pid, request.args.get("show_all") in ("1", "true"), view))

    @app.get("/api/paper/accounts")
    def paper_accounts():
        return jsonify(call(svc.paper_accounts))

    @app.get("/api/paper/accounts/<aid>")
    def paper_account(aid):
        return jsonify(call(svc.paper_account, _id(aid, PAPER_ID, "paper account id")))

    @app.post("/api/paper/batches")
    def paper_batch():
        b = body()
        sids = b.get("strategy_ids")
        if not isinstance(sids, list) or not sids or not all(isinstance(x, str) and STRATEGY_ID.match(x) for x in sids):
            raise _bad("strategy_ids must be a non-empty list of strategy ids")
        pid = _id(b.get("profile_id") or "", PROFILE_ID, "prop rule profile id")
        return jsonify(call(svc.paper_start, sids, pid)), 201

    @app.post("/api/paper/accounts/<aid>/<action>")
    def paper_action(aid, action):
        if action not in ("stop", "resume", "delete"):
            raise _bad("action must be stop, resume or delete")
        return jsonify(call(svc.paper_set_status, _id(aid, PAPER_ID, "paper account id"), action))

    @app.get("/api/paper/feed")
    def paper_feed():                               # lock-free: files only
        return jsonify(svc.paper_feed_status())

    @app.post("/api/paper/feed/update")
    def paper_feed_update():
        return jsonify(svc.paper_update_now()), 202

    @app.post("/api/paper/feed/check")               # ADR-83: compare downloaded days with the research data
    def paper_feed_check():
        return jsonify(svc.paper_check_source()), 202

    @app.post("/api/paper/feed/continue-anyway")
    def paper_feed_continue():
        return jsonify(call(svc.paper_continue_anyway))

    @app.get("/api/preferences/research-processes")
    def research_processes():                       # ADR-77: CPU cores research runs use (choice, default, max)
        return jsonify(call(svc.research_processes))

    @app.get("/api/favorites")
    def favorites():
        return jsonify(call(svc.favorites_info))

    @app.post("/api/favorites/<sid>")
    def set_favorite(sid):
        return jsonify(call(svc.set_favorite, _id(sid, STRATEGY_ID, "strategy id"), body().get("favorite")))

    @app.post("/api/workspace/reset")
    def reset_workspace():
        return jsonify(call(svc.reset_workspace, body().get("confirm")))

    @app.get("/api/preferences/research-dataset")
    def preferred_dataset():
        return jsonify(call(svc.preferred_dataset))

    @app.post("/api/preferences/research-dataset")
    def set_preferred_dataset():
        did = body().get("dataset_id")
        if did is None:
            return jsonify(call(svc.clear_preferred_dataset))
        return jsonify(call(svc.set_preferred_dataset, _id(did, SAFE_ID, "dataset id")))

    @app.get("/api/datasets/<did>")
    def dataset(did):
        return jsonify(call(svc.dataset_detail, _id(did, SAFE_ID, "dataset id")))

    @app.get("/api/datasets/<did>/quality")
    def dataset_quality(did):
        return jsonify(call(svc.dataset_quality, _id(did, SAFE_ID, "dataset id")))

    def _import_options(b: dict) -> dict:
        from edgelab.data.importer import ImportOptions
        path = b.get("path")
        if not isinstance(path, str):
            raise _bad("choose a file")
        full = (root / path).resolve()
        allowed = [(root / d).resolve() for d in web.import_dirs]
        if not any(full.is_relative_to(a) for a in allowed):
            raise ApiError(403, "forbidden", "Files can only be imported from the configured import folders: "
                           + ", ".join(web.import_dirs))
        opts = b.get("options") or {}
        fields = {f.name for f in dataclasses.fields(ImportOptions)} - {"file"}
        unknown = set(opts) - fields
        if unknown:
            raise _bad(f"unknown import options: {sorted(unknown)}")
        return {**opts, "file": str(full)}

    @app.get("/api/import/files")
    def import_files():
        return jsonify({"import_dirs": web.import_dirs, "files": call(svc.list_import_files, web.import_dirs)})

    @app.post("/api/import/inspect")
    def import_inspect():
        return jsonify(call(svc.inspect_file, _import_options(body())))

    @app.post("/api/import")
    def import_():
        return jsonify(call(svc.import_file, _import_options(body())))

    # ------------------------------------------------------------------ backtests / results
    @app.post("/api/backtests/readiness")
    def readiness():
        return jsonify(call(svc.backtest_readiness, strategy_source(body().get("strategy"))))

    @app.post("/api/backtests")
    def backtest():
        b = body()
        did = _id(b.get("dataset_id"), SAFE_ID, "dataset id")
        per = b.get("period")                        # ADR-70: optional explicit {start, end}; the protocol gate still decides
        period = None
        if per is not None:
            if not (isinstance(per, Mapping) and isinstance(per.get("start"), str) and isinstance(per.get("end"), str)):
                raise _bad("period must be {start, end} ISO timestamps")
            period = (per["start"], per["end"])
        return jsonify(call(svc.backtest_strategy, strategy_source(b.get("strategy")), did, True, period))

    def _backtest_args(b: Mapping) -> tuple:
        did = _id(b.get("dataset_id"), SAFE_ID, "dataset id")
        per = b.get("period")
        period = None
        if per is not None:
            if not (isinstance(per, Mapping) and isinstance(per.get("start"), str) and isinstance(per.get("end"), str)):
                raise _bad("period must be {start, end} ISO timestamps")
            period = (per["start"], per["end"])
        return strategy_source(b.get("strategy")), did, period

    @app.post("/api/backtests/jobs")
    def backtest_job_start():
        """ADR-76: the same backtest in the background; returns at once (the service lock is never held while the
        engine computes, so every other page keeps loading)."""
        src, did, period = _backtest_args(body())
        return jsonify(call(svc.start_backtest_job, src, did, period))

    @app.get("/api/backtests/jobs/<jid>")
    def backtest_job_status(jid):
        return jsonify(svc.backtest_job(_id(jid, BT_JOB_ID, "backtest job id")))

    @app.get("/api/results")
    def results():
        return jsonify(call(svc.list_runs))

    @app.get("/api/results/report")
    def results_report():
        """Phase 5: /api/results/report?run_ids=RUN_2026_00001,RUN_2026_00002 (one fixed strategy)."""
        raw = (request.args.get("run_ids") or "").split(",")
        ids = [_id(x.strip(), RUN_ID, "run id") for x in raw if x.strip()]
        if not ids:
            raise _bad("run_ids is required")
        return jsonify(call(svc.research_report, ids))

    @app.get("/api/results/<rid>")
    def result(rid):
        return jsonify(call(svc.get_run, _id(rid, RUN_ID, "run id")))

    # ------------------------------------------------------------------ strategy lab (Phase 8)
    @app.get("/api/strategies/<sid>/research")
    def strategy_research(sid):
        return jsonify(call(svc.strategy_research, _id(sid, STRATEGY_ID, "strategy id")))

    @app.get("/api/compare")
    def compare():
        """?source=runs|strategy|lineage|batch|search&id=... (runs: comma-separated RUN_ ids)."""
        src, ident = request.args.get("source", ""), (request.args.get("id") or "").strip()
        if not ident:
            raise _bad("id is required")
        if src == "runs":
            ids = [_id(x.strip(), RUN_ID, "run id") for x in ident.split(",") if x.strip()]
            return jsonify(call(svc.compare_runs, run_ids=ids))
        pattern = {"strategy": STRATEGY_ID, "lineage": STRATEGY_ID, "batch": BATCH_ID, "search": SEARCH_ID}.get(src)
        if pattern is None:
            raise _bad("source must be runs, strategy, lineage, batch or search")
        key = {"strategy": "strategy_id", "lineage": "lineage_of", "batch": "batch_id", "search": "search_id"}[src]
        return jsonify(call(svc.compare_runs, **{key: _id(ident, pattern, f"{src} id")}))

    @app.get("/api/results/<rid>/curve")
    def result_curve(rid):
        return jsonify(call(svc.run_curve, _id(rid, RUN_ID, "run id")))

    def _int(b: dict, key: str, default: int, lo: int, hi: int) -> int:
        v = b.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise _bad(f"{key} must be an integer in {lo}..{hi}")
        return v

    def _validation_target(b: dict) -> tuple:
        strategy = strategy_source(b.get("strategy"))
        if not (isinstance(strategy, dict) or (isinstance(strategy, str) and STRATEGY_ID.match(strategy))):
            raise _bad("strategy must be a stored strategy id or a definition object")
        return strategy, _id(b.get("dataset_id"), SAFE_ID, "dataset id")

    def _date(b: dict, key: str) -> str:
        v = b.get(key)
        if not isinstance(v, str) or not re.match(r"^\d{4}-\d{2}-\d{2}([ T][0-9:.+Z-]*)?$", v):
            raise _bad(f"{key} must be an ISO date (YYYY-MM-DD)")
        return v

    @app.post("/api/validation/oos")
    def validation_oos():
        b = body()
        strategy, did = _validation_target(b)
        return jsonify(call(svc.evaluate_oos, strategy, did, _date(b, "split_at"), bool(b.get("record", True)),
                            _int(b, "mc_sims", 1000, 1, 20000), _int(b, "mc_seed", 0, 0, 2**32 - 1)))

    @app.post("/api/validation/walkforward")
    def validation_walkforward():
        b = body()
        strategy, did = _validation_target(b)
        return jsonify(call(svc.walk_forward, strategy, did, _int(b, "train_months", 0, 1, 240),
                            _int(b, "test_months", 0, 1, 120), bool(b.get("anchored", False)),
                            bool(b.get("record", True)), _int(b, "mc_sims", 1000, 1, 20000),
                            _int(b, "mc_seed", 0, 0, 2**32 - 1)))

    @app.post("/api/validation/control")
    def validation_control():
        """Random-entry control over the whole dataset, or its OOS window when split_at is given."""
        b = body()
        strategy, did = _validation_target(b)
        n, seed = _int(b, "n_controls", 20, 1, 1000), _int(b, "seed", 0, 0, 2**32 - 1)
        if b.get("split_at"):
            return jsonify(call(svc.oos_random_control, strategy, did, _date(b, "split_at"), n, seed))
        return jsonify(call(svc.random_entry_control, strategy, did, n, seed))

    # ------------------------------------------------------------------ AI Discovery (Phase 9)
    @app.get("/api/ai/status")
    def ai_status():
        return jsonify(call(svc.ai_status))

    @app.post("/api/ai/context")
    def ai_context():
        return jsonify(call(svc.ai_context, body().get("request")))

    @app.post("/api/ai/generate")
    def ai_generate():
        return jsonify(call(svc.ai_generate, body().get("request")))

    @app.get("/api/ai/generations")
    def ai_generations():
        return jsonify(call(svc.ai_generations))

    @app.get("/api/ai/generations/<gid>")
    def ai_generation(gid):
        return jsonify(call(svc.ai_generation, _id(gid, AI_GEN_ID, "generation id")))

    @app.post("/api/ai/proposals/<pid>/decision")
    def ai_decide(pid):
        b = body()
        return jsonify(call(svc.ai_decide, _id(pid, AI_PROP_ID, "proposal id"), b.get("decision"), b.get("note") or ""))

    @app.post("/api/ai/proposals/<pid>/save")
    def ai_save(pid):
        return jsonify(call(svc.ai_save, _id(pid, AI_PROP_ID, "proposal id")))

    @app.get("/api/ai/proposals/<pid>/lineage")
    def ai_lineage(pid):
        return jsonify(call(svc.ai_lineage, _id(pid, AI_PROP_ID, "proposal id")))

    @app.get("/api/proposals/menu")
    def proposals_menu():
        n = request.args.get("n", "20")
        if not n.isdigit() or not 1 <= int(n) <= 200:
            raise _bad("n must be an integer in 1..200")
        return jsonify(call(svc.proposal_menu, int(n)))

    @app.post("/api/proposals/ingest")
    def proposals_ingest():
        """Mode B gate: a machine-readable proposal batch (object or YAML/JSON text) -> strict schema,
        claim-language rejection, the same validator and compiler -> ordinary library strategies."""
        b = body()
        batch = b.get("batch")
        if isinstance(batch, str):
            import yaml
            if len(batch) > 1_000_000:
                raise _bad("batch text is too large (max 1 MB)")
            try:
                batch = yaml.safe_load(batch)
            except yaml.YAMLError as exc:
                raise ApiError(422, "parse", "The proposal batch is not valid YAML/JSON.", reason=str(exc)) from None
        if not isinstance(batch, dict):
            raise _bad("batch must be a proposal batch object (or its YAML/JSON text)")
        return jsonify(call(svc.ingest_proposals, batch, bool(b.get("save", False))))

    # ------------------------------------------------------------------ prop lifecycle (ADR-64, read-only)
    @app.get("/api/prop/profiles")
    def prop_profiles():
        return jsonify(call(svc.prop_profiles))

    @app.get("/api/prop/lifecycle/<rid>")
    def prop_lifecycle(rid):
        return jsonify(call(svc.prop_lifecycle, _id(rid, RUN_ID, "run id")))

    # ------------------------------------------------------------------ prop simulation (Phase 6)
    @app.get("/api/prop/configs")
    def prop_configs():
        return jsonify(call(svc.prop_configs))

    @app.post("/api/prop/validate")
    def prop_validate():
        cfg = body().get("config")
        if not isinstance(cfg, (dict, str)):
            raise _bad("config must be a rule-set object, YAML text or a config id")
        return jsonify(call(svc.validate_prop_config, cfg))

    @app.post("/api/prop/simulate")
    def prop_simulate():
        b = body()
        rid = _id(b.get("run_id"), RUN_ID, "run id")
        accounts = b.get("accounts")
        if not isinstance(accounts, list) or not all(isinstance(a, dict) for a in accounts):
            raise _bad("accounts must be a list of {account_id?, config, start?}")
        for a in accounts:
            if a.get("account_id") is not None:
                _id(a["account_id"], SAFE_ID, "account id")
            if a.get("start") is not None and not isinstance(a["start"], str):
                raise _bad("account start must be an ISO timestamp string")
            if not isinstance(a.get("config"), (dict, str)):
                raise _bad("each account needs a config (id, object or YAML text)")
        return jsonify(call(svc.prop_simulate, rid, accounts, bool(b.get("record", True))))

    @app.get("/api/prop/simulations")
    def prop_simulations():
        return jsonify(call(svc.list_prop_simulations))

    @app.get("/api/prop/simulations/<sid>")
    def prop_simulation(sid):
        return jsonify(call(svc.get_prop_simulation, _id(sid, PROP_SIM_ID, "simulation id")))

    # ------------------------------------------------------------------ research (Phase 4)
    def _search_spec(b: dict) -> dict:
        spec = b.get("spec")
        if not isinstance(spec, dict):
            raise _bad("a search spec must be a JSON object under 'spec'")
        return spec

    @app.post("/api/research/validate")
    def research_validate():
        return jsonify(call(svc.validate_search, _search_spec(body())))

    @app.post("/api/research/plan")
    def research_plan():
        return jsonify(call(svc.plan_search, _search_spec(body())))

    @app.post("/api/research/jobs")
    def research_job_start():
        return jsonify(call(svc.start_search_job, _search_spec(body()))), 202

    @app.get("/api/research/jobs/<jid>")
    def research_job_status(jid):
        return jsonify(call(svc.job_status, _id(jid, JOB_ID, "job id")))

    @app.post("/api/research/jobs/<jid>/cancel")
    def research_job_cancel(jid):
        return jsonify(call(svc.cancel_job, _id(jid, JOB_ID, "job id")))

    # ------------------------------------------------------------------ research runs: frozen campaigns (ADR-69)
    @app.get("/api/campaigns")
    def campaigns_list():
        return jsonify(call(svc.campaigns))

    @app.get("/api/campaigns/active-job")
    def campaigns_active_job():                     # lock-free: in-memory job table only
        return jsonify(svc.active_job())

    @app.get("/api/campaigns/<cid>")
    def campaigns_detail(cid):
        return jsonify(call(svc.campaign_detail, _id(cid, CAMPAIGN_ID, "campaign id")))

    @app.get("/api/campaigns/<cid>/check")
    def campaigns_check(cid):
        return jsonify(call(svc.campaign_check, _id(cid, CAMPAIGN_ID, "campaign id")))

    @app.get("/api/campaigns/<cid>/families/<fid>")
    def campaigns_family(cid, fid):
        return jsonify(call(svc.campaign_family_results, _id(cid, CAMPAIGN_ID, "campaign id"),
                            _id(fid, FAMILY_ID, "family id")))

    @app.get("/api/campaigns/<cid>/strategies/<sid>")
    def campaigns_strategy(cid, sid):
        return jsonify(call(svc.campaign_strategy_result, _id(cid, CAMPAIGN_ID, "campaign id"),
                            _id(sid, STRATEGY_ID, "strategy id")))

    @app.post("/api/campaigns/<cid>/jobs")
    def campaigns_start(cid):
        b = body()
        fams = b.get("families")
        if fams is not None and (not isinstance(fams, list) or not all(isinstance(f, str) and FAMILY_ID.match(f)
                                                                       for f in fams)):
            raise _bad("families must be null (all families) or a list of family ids")
        sids = b.get("strategy_ids")
        if sids is not None and (not isinstance(sids, list) or not all(isinstance(x, str) and STRATEGY_ID.match(x)
                                                                       for x in sids)):
            raise _bad("strategy_ids must be null or a list of frozen STR_ ids")
        if fams is not None and sids is not None:
            raise _bad("give families or strategy_ids, not both")
        mf = b.get("max_failures", 0)
        if not isinstance(mf, int) or isinstance(mf, bool) or not 0 <= mf <= 1000:
            raise _bad("max_failures must be an integer 0..1000")
        procs = b.get("processes")                  # ADR-77: None = the Settings choice
        if procs is not None and (not isinstance(procs, int) or isinstance(procs, bool) or procs < 1):
            raise _bad("processes must be null or a positive integer")
        return jsonify(call(svc.start_campaign_job, _id(cid, CAMPAIGN_ID, "campaign id"), fams, mf, sids, procs)), 202

    @app.get("/api/campaigns/<cid>/tree")
    def campaigns_tree(cid):
        return jsonify(call(svc.campaign_tree, _id(cid, CAMPAIGN_ID, "campaign id")))

    @app.get("/api/campaigns/<cid>/runs/<rid>/scope")
    def campaigns_run_scope(cid, rid):
        return jsonify(call(svc.campaign_run_scope, _id(cid, CAMPAIGN_ID, "campaign id"), _id(rid, RUN_RECORD_ID, "run record id")))

    @app.get("/api/campaigns/jobs/<jid>")
    def campaigns_job(jid):                         # lock-free: never waits for the running research
        return jsonify(svc.campaign_job(_id(jid, JOB_ID, "job id")))

    @app.post("/api/campaigns/jobs/<jid>/cancel")
    def campaigns_job_cancel(jid):                  # sets a flag; the running cell finishes, nothing is marked done
        return jsonify(svc.cancel_job(_id(jid, JOB_ID, "job id")))

    # ------------------------------------------------------------------ holdout backtests (ADR-85)
    @app.get("/api/holdout/candidates")
    def holdout_candidates():
        return jsonify(call(svc.holdout_candidates))

    @app.get("/api/holdout/history")
    def holdout_history():
        return jsonify(call(svc.holdout_history))

    @app.post("/api/holdout/jobs")
    def holdout_start():
        sids = body().get("strategy_ids")
        if not isinstance(sids, list) or not sids or not all(isinstance(x, str) and STRATEGY_ID.match(x) for x in sids):
            raise _bad("strategy_ids must be a non-empty list of STR_ ids")
        return jsonify(call(svc.start_holdout_job, sids)), 202

    @app.get("/api/holdout/jobs/<jid>")
    def holdout_job(jid):                           # lock-free: in-memory job record
        return jsonify(svc.holdout_job(_id(jid, JOB_ID, "job id")))

    @app.post("/api/holdout/jobs/<jid>/cancel")
    def holdout_cancel(jid):                        # stops BEFORE the next strategy; a granted test always finishes
        return jsonify(svc.cancel_job(_id(jid, JOB_ID, "job id")))

    # ------------------------------------------------------------------ flip scan (ADR-88)
    def _cap(x: Any) -> int | None:
        if x in (None, ""):
            return None
        try:
            v = int(x)
        except (TypeError, ValueError):
            raise _bad("cap must be a whole number") from None
        if isinstance(x, bool) or (isinstance(x, float) and not float(x).is_integer()):
            raise _bad("cap must be a whole number")
        return v

    @app.get("/api/flips")
    def flips_view():
        return jsonify(call(svc.flip_scan, None, _cap(request.args.get("cap"))))

    @app.post("/api/flips")
    def flips_create():
        b = body()
        looks = b.get("holdout_looks")
        if looks is not None and (not isinstance(looks, int) or isinstance(looks, bool) or not 1 <= looks <= 100):
            raise _bad("holdout_looks must be a whole number from 1 to 100")
        return jsonify(call(svc.create_flip_scan, None, _cap(b.get("cap")), looks)), 201

    @app.post("/api/flips/jobs")
    def flips_start():
        return jsonify(call(svc.start_flip_job)), 202

    @app.get("/api/flips/jobs/<jid>")
    def flips_job(jid):                             # lock-free: in-memory job record
        return jsonify(svc.flip_job(_id(jid, JOB_ID, "job id")))

    @app.post("/api/flips/jobs/<jid>/cancel")
    def flips_cancel(jid):                          # sets a flag; running cells finish and are recorded
        return jsonify(svc.cancel_job(_id(jid, JOB_ID, "job id")))

    # ------------------------------------------------------------------ strategy pool 2 (ADR-87)
    @app.get("/api/pool2")
    def pool2_status():
        return jsonify(call(svc.pool2_status))

    @app.post("/api/pool2/generate")
    def pool2_generate():
        return jsonify(call(svc.start_pool2_job, "generate")), 202

    @app.post("/api/pool2/switch")
    def pool2_switch():
        conf = body().get("confirm")
        if not isinstance(conf, str):
            raise _bad("confirm must be the confirmation word")
        return jsonify(call(svc.start_pool2_job, "switch", conf)), 202

    @app.get("/api/pool2/jobs/<jid>")
    def pool2_job(jid):                             # lock-free: in-memory job record
        return jsonify(svc.pool2_job(_id(jid, JOB_ID, "job id")))

    @app.get("/api/research/searches")
    def research_searches():
        return jsonify(call(svc.list_searches))

    @app.get("/api/research/searches/<sid>")
    def research_search(sid):
        return jsonify(call(svc.get_search, _id(sid, SEARCH_ID, "search id")))

    @app.get("/api/research/searches/<sid>/ranking")
    def research_ranking(sid):
        q = request.args
        return jsonify(call(svc.rank_search, _id(sid, SEARCH_ID, "search id"), q.get("metric") or None,
                            q.get("min_sample_label") or None))

    @app.post("/api/research/searches/<sid>/shortlist")
    def research_shortlist(sid):
        ids = body().get("strategy_ids")
        if not isinstance(ids, list):
            raise _bad("strategy_ids must be a JSON list")
        return jsonify(call(svc.select_shortlist, _id(sid, SEARCH_ID, "search id"), ids))

    # ------------------------------------------------------------------ version + application updates
    from edgelab.updater.service import register_routes as register_update_routes
    register_update_routes(app, body, _bad)

    # ------------------------------------------------------------------ research terminal (read-only)
    @app.get("/api/overview")
    def research_overview():
        return jsonify(call(svc.research_overview))

    def _params(keys: tuple[str, ...]) -> dict:
        out = {}
        for k in keys:
            v = request.args.get(k)
            if v is None or v == "":
                continue
            if len(v) > 200:
                raise _bad(f"{k} is too long")
            out[k] = v
        for k in ("page", "page_size"):
            if k in out and not out[k].isdigit():
                raise _bad(f"{k} must be a positive integer")
        for k in ("min_trades", "max_trades_per_week"):
            if k in out:
                try:
                    float(out[k])
                except ValueError:
                    raise _bad(f"{k} must be a number") from None
        return out

    @app.get("/api/explorer/strategies")
    def explorer_strategies():
        return jsonify(call(svc.explore_strategies, _params((
            "q", "strategy_id", "family_id", "timeframe", "session", "direction", "entry_type", "stop_type",
            "target_type", "source", "instrument", "state", "protocol", "scope", "min_trades",
            "max_trades_per_week", "tested_only", "sort", "order", "page", "page_size", "survivors_only", "trailing",
            "signal_exit", "favorites_only", "prop", "campaign_run", "live_only"))))

    @app.get("/api/results-view/overview")
    def results_view_overview():
        return jsonify(call(svc.results_overview, _params(("scope", "basis", "controls", "campaign_run"))))

    @app.get("/api/results-view/pools")
    def results_view_pools():
        return jsonify(call(svc.strategy_pools))

    @app.get("/api/results-view/runs")
    def results_view_runs():
        return jsonify(call(svc.research_runs))

    @app.post("/api/campaigns/<cid>/runs/<rid>/name")
    def rename_campaign_run(cid, rid):
        return jsonify(call(svc.rename_campaign_run, _id(cid, CAMPAIGN_ID, "campaign id"),
                            _id(rid, RUN_RECORD_ID, "run record id"), body().get("name")))

    @app.get("/api/results-view/strategies/<sid>")
    def results_view_strategy(sid):
        return jsonify(call(svc.strategy_panel, _id(sid, STRATEGY_ID, "strategy id"), _params(("scope",))))

    @app.get("/api/results-view/controls/<cid>")
    def results_view_control(cid):
        return jsonify(call(svc.control_panel, _id(cid, CONTROL_ID, "control id")))

    @app.post("/api/prop/bootstrap")
    def prop_bootstrap():
        b = body()
        params = {k: b.get(k) for k in ("n", "block_days", "seed", "mode") if b.get(k) is not None}
        return jsonify(call(svc.prop_bootstrap, _id(b.get("run_id"), RUN_ID, "run id"),
                            _id(b.get("profile_id"), SAFE_ID, "profile id"), params))

    @app.get("/api/research/dashboard")
    def research_dashboard():
        return jsonify(call(svc.research_dashboard, _params(("scope", "instrument", "dataset_id", "family_id",
                                                             "include_synthetic"))))

    @app.get("/api/results/<rid>/analytics")
    def result_analytics(rid):
        return jsonify(call(svc.run_analytics, _id(rid, RUN_ID, "run id")))

    @app.get("/api/strategies/<sid>/pipeline")
    def strategy_pipeline(sid):
        return jsonify(call(svc.strategy_pipeline, _id(sid, STRATEGY_ID, "strategy id")))

    @app.get("/api/pipeline")
    def pipeline_board():
        return jsonify(call(svc.pipeline_board))

    @app.get("/api/protocols")
    def protocols():
        return jsonify(call(svc.list_protocols))

    @app.get("/api/protocols/<pid>")
    def protocol_detail(pid):
        """Read-only: the verified protocol record and its counters. There is deliberately no HTTP route
        that creates, edits or retires a protocol, or resets a ledger."""
        pid = _id(pid, PROTOCOL_ID, "protocol id")
        return jsonify({"record": call(svc.get_protocol, pid), "status": call(svc.protocol_status, pid)})

    @app.get("/api/protocols/<pid>/config-difference")
    def protocol_config_difference(pid):
        """ADR-89 (read only): how the workspace's research settings differ from the protocol's recorded ones."""
        return jsonify(call(svc.protocol_config_difference, _id(pid, PROTOCOL_ID, "protocol id")))

    @app.post("/api/protocols/<pid>/restore-config")
    def restore_protocol_config(pid):
        """ADR-89: rewrites the WORKSPACE's config files to the protocol's recorded settings (verified, backed up).
        The protocol record itself is never edited."""
        return jsonify(call(svc.restore_protocol_config, _id(pid, PROTOCOL_ID, "protocol id"), body().get("confirm")))

    # ------------------------------------------------------------------ My strategy (ADR-93)
    @app.get("/api/my")
    def my_overview():
        return jsonify(call(svc.my_strategy_overview))

    @app.get("/api/my/settings")
    def my_settings():
        return jsonify(call(svc.my_strategy_settings))

    @app.post("/api/my/settings")
    def my_settings_save():
        ov = body().get("overrides")
        if not isinstance(ov, dict):
            raise _bad("overrides must be an object")
        return jsonify(call(svc.my_strategy_save_settings, ov))

    def _my_window(b: dict):
        out = []
        for k in ("start", "end"):
            v = b.get(k)
            if v is not None and (not isinstance(v, str) or len(v) > 40):
                raise _bad(f"{k} must be a date string")
            out.append(v or None)
        return out

    @app.post("/api/my/backtests")
    def my_backtest_start():
        b = body()
        ov = b.get("overrides")
        if ov is not None and not isinstance(ov, dict):
            raise _bad("overrides must be an object")
        start, end = _my_window(b)
        label = str(b.get("label") or "")[:80]
        return jsonify(call(svc.my_strategy_start_backtest, ov, start, end, label)), 202

    @app.get("/api/my/es")
    def my_es():
        return jsonify(call(svc.my_strategy_es))

    @app.post("/api/my/es/import")
    def my_es_import():
        b = body()
        path = b.get("path")
        if not isinstance(path, str) or len(path) > 1000:
            raise _bad("path must be a string")
        return jsonify(call(svc.my_strategy_import_es, path, b.get("identity_confirmed") is True)), 202

    @app.get("/api/my/jobs/<jid>")
    def my_job(jid):
        return jsonify(svc.my_strategy_job(_id(jid, MY_JOB_ID, "job id")))

    @app.get("/api/my/reports/<rid>")
    def my_report(rid):
        return jsonify(call(svc.my_strategy_backtest, _id(rid, MY_REPORT_ID, "report id")))

    @app.get("/api/my/reports/<rid>/trades/<int:n>")
    def my_trade(rid, n):
        return jsonify(call(svc.my_strategy_trade, _id(rid, MY_REPORT_ID, "report id"), n))

    @app.get("/api/my/review")
    def my_review():
        return jsonify(call(svc.my_strategy_review))

    @app.post("/api/my/review")
    def my_review_start():
        ov = body().get("overrides")
        if ov is not None and not isinstance(ov, dict):
            raise _bad("overrides must be an object")
        return jsonify(call(svc.my_strategy_start_review, ov)), 202

    @app.get("/api/edge")                              # ADR-104: Edge lab
    def edge_status():
        return jsonify(call(svc.edge_status))

    @app.post("/api/edge/check")
    def edge_run():
        body = request.get_json(silent=True) or {}
        src = _id(body.get("source") or "discovery", EDGE_SOURCE, "data source (discovery or a dataset id)")
        return jsonify(call(svc.edge_run, src)), 202

    @app.get("/api/edge/jobs/<jid>")
    def edge_job(jid):
        return jsonify(call(svc.edge_job, _id(jid, MY_JOB_ID, "job id")))

    @app.get("/api/market")                            # ADR-106: Market simulator
    def market_status():
        return jsonify(call(svc.market_status))

    @app.get("/api/market/section/<name>")
    def market_section(name):
        return jsonify(call(svc.market_section, _id(name, MARKET_SECTION, "section")))

    @app.post("/api/market/news/key")
    def market_news_key():
        body = request.get_json(silent=True) or {}
        key = body.get("key")
        if key is not None and not isinstance(key, str):
            raise _bad("key must be text")
        return jsonify(call(svc.market_set_news_key, key))

    @app.post("/api/market/news/download")
    def market_news_download():
        body = request.get_json(silent=True) or {}
        src = body.get("source") or "forex-factory"
        if src not in ("forex-factory", "mql5", "fxstreet"):
            raise _bad("unknown news source")
        return jsonify(call(svc.market_news_download, src)), 202

    @app.post("/api/market/analyze")
    def market_analyze():
        body = request.get_json(silent=True) or {}
        return jsonify(call(svc.market_analyze, bool(body.get("force")))), 202

    @app.post("/api/market/newdays")
    def market_newdays():
        return jsonify(call(svc.market_newdays)), 202

    @app.get("/api/market/holdout")                    # ADR-107: holdout prediction test (one look)
    def market_holdout():
        return jsonify(call(svc.market_holdout))

    @app.post("/api/market/holdout")
    def market_holdout_run():
        body = request.get_json(silent=True) or {}
        confirm = body.get("confirm")
        if not isinstance(confirm, str):
            raise _bad("confirm must be text")
        return jsonify(call(svc.market_holdout_run, confirm)), 202

    @app.get("/api/charts")                            # ADR-111: live charts
    def charts_meta():
        return jsonify(call(svc.charts_meta))

    @app.get("/api/charts/bars")
    def charts_bars():
        a = request.args
        to = a.get("to")
        if to is not None and not to.isdigit():
            raise _bad("to must be a unix time in seconds")
        cnt = a.get("count", "1500")
        if not cnt.isdigit():
            raise _bad("count must be a number")
        return jsonify(call(svc.charts_bars, _id(a.get("symbol", ""), CHART_SYMBOL, "symbol"), _id(a.get("tf", ""), CHART_TF, "tf"),
                            int(to) if to else None, int(cnt)))

    @app.get("/api/charts/live")
    def charts_live():
        a = request.args
        since = a.get("since", "0")
        if not since.isdigit():
            raise _bad("since must be a unix time in seconds")
        return jsonify(call(svc.charts_live, _id(a.get("symbol", ""), CHART_SYMBOL, "symbol"), _id(a.get("tf", ""), CHART_TF, "tf"), int(since)))

    @app.get("/api/charts/drawings/<symbol>")
    def charts_drawings(symbol):
        return jsonify(call(svc.charts_drawings, _id(symbol, CHART_SYMBOL, "symbol")))

    @app.put("/api/charts/drawings/<symbol>")
    def charts_save_drawings(symbol):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("drawings"), list):
            raise _bad("send {drawings: [...]}")
        return jsonify(call(svc.charts_save_drawings, _id(symbol, CHART_SYMBOL, "symbol"), body["drawings"]))

    @app.get("/api/charts/layout")
    def charts_layout():
        return jsonify(call(svc.charts_layout))

    @app.put("/api/charts/layout")
    def charts_save_layout():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise _bad("send a JSON object")
        return jsonify(call(svc.charts_save_layout, body))

    @app.get("/api/charts/predictor")                  # ADR-113: the predictor drawn on the live chart
    def charts_predictor():
        return jsonify(call(svc.charts_predictor, _id(request.args.get("symbol", ""), CHART_SYMBOL, "symbol")))

    @app.get("/api/sim/accounts")                      # ADR-112: simulated Lucid accounts (never real orders)
    def sim_accounts():
        return jsonify(call(svc.sim_accounts))

    @app.post("/api/sim/accounts")
    def sim_create():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            raise _bad("send a JSON object")
        return jsonify(call(svc.sim_create, str(body.get("name", "")), body.get("start_balance", 50000))), 201

    @app.get("/api/sim/accounts/<aid>")
    def sim_account(aid):
        return jsonify(call(svc.sim_account, _id(aid, SIM_ID, "account")))

    @app.post("/api/sim/accounts/<aid>/<action>")
    def sim_action(aid, action):
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            raise _bad("send a JSON object")
        return jsonify(call(svc.sim_action, _id(aid, SIM_ID, "account"), _id(action, SIM_ACTION, "action"), body))

    @app.get("/api/sim/quote")
    def sim_quote():
        return jsonify(call(svc.sim_quote, _id(request.args.get("symbol", ""), CHART_SYMBOL, "symbol")))

    @app.get("/api/market/report")                     # ADR-110: report card of every forecast
    def market_report():
        return jsonify(call(svc.market_report))

    @app.get("/api/market/direction")                  # ADR-109: direction calls + the second holdout look
    def market_direction():
        return jsonify(call(svc.market_direction))

    @app.post("/api/market/direction/run")
    def market_direction_run():
        body = request.get_json(silent=True) or {}
        return jsonify(call(svc.market_direction_run, bool(body.get("force")))), 202

    @app.post("/api/market/direction/holdout")
    def market_direction_holdout():
        body = request.get_json(silent=True) or {}
        confirm = body.get("confirm")
        if not isinstance(confirm, str):
            raise _bad("confirm must be text")
        return jsonify(call(svc.market_direction_holdout_run, confirm)), 202

    @app.get("/api/market/jobs/<jid>")
    def market_job(jid):
        return jsonify(call(svc.market_job, _id(jid, MY_JOB_ID, "job id")))

    @app.get("/api/market/days")
    def market_days():
        src = request.args.get("src", "discovery")
        if src not in ("discovery", "new", "holdout"):
            raise _bad("src must be discovery, new or holdout")
        return jsonify(call(svc.market_days, src))

    @app.get("/api/market/day/<day>")
    def market_day(day):
        src = request.args.get("src", "discovery")
        if src not in ("discovery", "new", "holdout"):
            raise _bad("src must be discovery, new or holdout")
        return jsonify(call(svc.market_day, _id(day, MARKET_DAY, "date"), src))

    @app.get("/api/edge/anatomy/<rid>")
    def edge_anatomy(rid):
        return jsonify(call(svc.edge_anatomy, _id(rid, MY_REPORT_ID, "report id")))

    @app.get("/api/my/holdout")                        # ADR-102: automatic + manual holdout of one strategy
    def my_holdout():
        return jsonify(call(svc.my_holdout))

    @app.post("/api/my/holdout/automatic")
    def my_holdout_automatic():
        ref = body().get("ref")
        if not isinstance(ref, str) or not re.fullmatch(r"bt:BT_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}|opt:OPT_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}:[0-9]{1,4}", ref):
            raise _bad("ref must name one of your backtests or an autotuner result")
        return jsonify(call(svc.my_holdout_start_automatic, ref)), 202

    @app.post("/api/my/holdout/manual")
    def my_holdout_manual():
        n = body().get("strategy")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 2:
            raise _bad("strategy must be 1 or 2")
        return jsonify(call(svc.my_holdout_start_manual, n))

    @app.post("/api/my/review/decide")
    def my_review_decide():
        b = body()
        sb, take = b.get("signal_bar"), b.get("take")
        if not isinstance(sb, int) or isinstance(sb, bool) or not isinstance(take, bool):
            raise _bad("signal_bar (integer) and take (true/false) are required")
        return jsonify(svc.my_strategy_decide(sb, take))       # computes outside the service lock

    @app.post("/api/my/export")
    def my_export():
        b = body()
        ids = b.get("report_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 200:
            raise _bad("report_ids must be a non-empty list")
        ids = [_id(x, MY_EXPORT_ID, "report id") for x in ids]
        return jsonify(call(svc.my_strategy_export, ids, bool(b.get("include_candles", False))))

    @app.get("/api/my/setup-reviews")
    def my_setup_reviews():
        return jsonify(call(svc.my_strategy_setup_reviews))

    @app.post("/api/my/setup-reviews")
    def my_setup_review_start():
        b = body()
        size = b.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 2000):
            raise _bad("size must be an integer between 1 and 2000")
        return jsonify(call(svc.my_strategy_start_setup_review, _id(b.get("report_id"), MY_REPORT_ID, "report id"), size))

    @app.get("/api/my/setup-reviews/<sid>")
    def my_setup_review(sid):
        return jsonify(call(svc.my_strategy_setup_review, _id(sid, MY_SETUP_ID, "setup review id")))

    @app.post("/api/my/setup-reviews/<sid>/decide")
    def my_setup_decide(sid):
        b = body()
        n, take, reasons, note = b.get("trade_no"), b.get("take"), b.get("reasons", []), b.get("note", "")
        if not isinstance(n, int) or isinstance(n, bool) or not isinstance(take, bool):
            raise _bad("trade_no (integer) and take (true/false) are required")
        if not isinstance(reasons, list) or len(reasons) > 20 or not all(isinstance(x, str) for x in reasons):
            raise _bad("reasons must be a list of strings")
        if not isinstance(note, str):
            raise _bad("note must be a string")
        return jsonify(call(svc.my_strategy_setup_decide, _id(sid, MY_SETUP_ID, "setup review id"), n, take, reasons,
                            note))

    @app.post("/api/my/setup-reviews/<sid>/undo")
    def my_setup_undo(sid):
        return jsonify(call(svc.my_strategy_setup_undo, _id(sid, MY_SETUP_ID, "setup review id")))

    @app.get("/api/my/autotune")                       # ADR-101: Strategy autotuner (step-by-step optimiser)
    def my_autotune():
        return jsonify(call(svc.my_autotune_status))

    @app.get("/api/my/autotune/runs/<rid>")
    def my_autotune_run(rid):
        return jsonify(call(svc.my_autotune_run, _id(rid, MY_OPT_ID, "autotuner run id")))

    @app.post("/api/my/autotune/start")
    def my_autotune_start():
        b = body()
        p, mt = b.get("processes"), b.get("max_tries")
        if p is not None and (not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 256):
            raise _bad("processes must be an integer between 1 and 256")
        if mt is not None and (not isinstance(mt, int) or isinstance(mt, bool) or not 1 <= mt <= 5000):
            raise _bad("max_tries must be an integer between 1 and 5000")
        start = _id(b.get("start_id"), MY_BT_ID, "backtest id")
        return jsonify(call(svc.my_autotune_start, start, p, mt)), 202

    @app.post("/api/my/autotune/stop")
    def my_autotune_stop():
        return jsonify(call(svc.my_autotune_stop))

    @app.post("/api/my/autotune/runs/<rid>/bests/<int:n>/save")
    def my_autotune_save(rid, n):
        return jsonify(call(svc.my_autotune_save, _id(rid, MY_OPT_ID, "autotuner run id"), n)), 202

    @app.post("/api/my/reports/<rid>/meta")            # ADR-101: favourite / rename a backtest
    def my_report_meta(rid):
        b = body()
        fav, label = b.get("favorite"), b.get("label")
        if fav is not None and not isinstance(fav, bool):
            raise _bad("favorite must be true or false")
        if label is not None and (not isinstance(label, str) or len(label) > 160):
            raise _bad("label must be text of at most 160 characters")
        if fav is None and label is None:
            raise _bad("nothing to change")
        return jsonify(call(svc.my_strategy_set_meta, _id(rid, MY_REPORT_ID, "report id"), fav, label))

    @app.post("/api/my/exports/open")
    def my_open_exports():
        return jsonify(svc.my_strategy_open_exports())

    @app.post("/api/my/plans/check")
    def my_plan_check():
        plan = body().get("plan")
        if not isinstance(plan, dict):
            raise _bad("plan must be an object")
        return jsonify(svc.my_strategy_check_plan(plan))

    @app.post("/api/my/plans/run")
    def my_plan_run():
        plan = body().get("plan")
        if not isinstance(plan, dict):
            raise _bad("plan must be an object")
        return jsonify(call(svc.my_strategy_run_plan, plan)), 202

    @app.get("/api/my/plan-results/<pid>")
    def my_plan_result(pid):
        return jsonify(svc.my_strategy_plan_result(_id(pid, MY_REPORT_ID, "plan result id")))

    # ------------------------------------------------------------------ static SPA
    @app.get("/api/<path:_rest>")
    def api_404(_rest):
        raise ApiError(404, "not_found", "Unknown API endpoint.")

    @app.get("/")
    @app.get("/<path:path>")
    def spa(path: str = ""):
        if path and (STATIC / path).is_file():
            return send_from_directory(STATIC, path)
        if not (STATIC / "index.html").exists():
            return ("Frontend bundle missing: run `npm run build` in web/ (see WEB_UI.md).", 503)
        return send_from_directory(STATIC, "index.html")

    return app

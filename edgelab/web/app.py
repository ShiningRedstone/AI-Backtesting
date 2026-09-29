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
import traceback
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify, request, send_from_directory

from edgelab.web.config import WebConfig, load_web_config

STATIC = Path(__file__).parent / "static"
STRATEGY_ID = re.compile(r"^STR_[0-9A-F]{12}$")
BATCH_ID = re.compile(r"^VB_[0-9A-F]{12}$")
RUN_ID = re.compile(r"^RUN_\d{4}_\d{5}$")
SEARCH_ID = re.compile(r"^SRCH_[0-9A-F]{12}$")
JOB_ID = re.compile(r"^JOB_[0-9A-F]{12}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_\-.]{1,120}$")


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


def _id(x: Any, pattern: re.Pattern, what: str) -> str:
    if not isinstance(x, str) or not pattern.match(x):
        raise _bad(f"invalid {what}")
    return x


def create_app(root: str | Path = ".", demo: bool = False, web: WebConfig | None = None) -> Flask:
    from edgelab.services import Services

    root = Path(root).resolve()
    web = web or load_web_config(root)
    svc = Services(root=root)
    lock = svc.lock                  # the one service lock (shared with the Phase 4 job manager)
    if svc.store.backend == "sqlite":
        svc.jobs                     # start the job manager: searches a dead process left `running` -> interrupted
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = int(web.max_request_mb * 1024 * 1024)
    app.config["EDGELAB"] = {"root": root, "demo": demo, "services": svc, "web": web}

    def call(fn: Callable, *a, **kw):
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
        from edgelab.research.jobs import JobConflict
        from edgelab.research.ranking import RankingError
        from edgelab.research.search import SearchSpecError
        from edgelab.strategy.compiler import StrategyCompileError
        from edgelab.strategy.dsl import StrategyValidationError
        from edgelab.strategy.variations import VariationError
        details = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        if isinstance(e, HTTPException):
            kind = "not_found" if e.code == 404 else "http"
            return jsonify({"error": {"kind": kind, "message": e.description}}), e.code
        if isinstance(e, SearchSpecError):
            return jsonify({"error": {"kind": "search_spec", "message": "The search was refused.",
                                      "reason": str(e), "issues": [i.to_dict() for i in e.issues],
                                      "details": details}}), 422
        if isinstance(e, StrategyValidationError):
            return jsonify({"error": {"kind": "validation", "message": "The strategy is not valid.",
                                      "issues": [i.to_dict() for i in e.result.issues],
                                      "details": str(e)}}), 422
        table = [(VariationError, 422, "variation", "Variation generation was refused."),
                 (StrategyCompileError, 422, "compile", "The strategy cannot run on this dataset."),
                 (CostConfigError, 409, "cost_unconfigured",
                  "Backtest unavailable: the broker/provider cost profile is unconfigured. "
                  "Configure verified costs before running research."),
                 (BacktestError, 422, "backtest", "The backtest was stopped."),
                 (RankingError, 422, "ranking", "The ranking request was refused."),
                 (JobConflict, 409, "job_conflict", "Another search job is still active; one runs at a time."),
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
        repo_tests = Path(__file__).resolve().parents[2] / "reports" / "last_test_run.txt"
        tests = tests if tests.exists() else repo_tests
        return jsonify({**s, "demo": demo, "root": str(root), "frontend": bundle_status(),
                        "test_status": tests.read_text().strip() if tests.exists() else None})

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
        cfg = svc.cfg
        costs = {}
        for sym in load_instruments(cfg):
            try:
                cm = cost_model_from_config(cfg, sym)
                costs[sym] = {"status": cm.status, "profile": getattr(cm, "profile", None)}
            except CostConfigError as exc:
                costs[sym] = {"status": "unconfigured", "reason": str(exc)}
        return jsonify({"demo": demo, "root": str(root), "config_hash": svc._config_hash(),
                        "backtest": cfg["backtest"], "sessions": {k: v.definition() for k, v in svc.sessions.items()},
                        "instruments": {k: {"asset_class": v.asset_class, "tick_size": v.tick_size,
                                            "point_value": v.point_value, "calendar": v.calendar}
                                        for k, v in load_instruments(cfg).items()},
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
        return jsonify(call(svc.library.list, fam, archived))

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

    @app.get("/api/datasets/<did>")
    def dataset(did):
        return jsonify(call(svc.dataset_detail, _id(did, SAFE_ID, "dataset id")))

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
        return jsonify(call(svc.backtest_strategy, strategy_source(b.get("strategy")), did, True))

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

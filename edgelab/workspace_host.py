"""The desktop app's active research workspace (desktop layer; ``python -m edgelab.web`` is unaffected).

``WorkspaceHost`` is the WSGI application the launcher serves. It holds at most one workspace:
the existing Flask app (``create_app(root)``, unchanged) for that folder plus its instance lock.
Workspace routes (``/api/workspace*``) and the launcher's control routes (``/api/desktop/*``) are
answered by a small shell app; everything else goes to the active workspace's app. With no
workspace selected (first run) the shell serves the UI so the user can choose one, and every other
API call answers 409 ``no_workspace``.

Selecting a workspace is a pointer change: it is validated READ-ONLY first
(``runtime.inspect_workspace``), its lock is taken (one EdgeLab per workspace), the previous
workspace's background job is cancelled and its store closed, and the choice is saved in the app
settings file (never inside a workspace). Nothing is copied, merged, migrated, re-imported or
deleted, in either workspace.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from edgelab import runtime


class WorkspaceError(Exception):
    def __init__(self, status: int, message: str, info: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.info = status, message, info


def close_app(app) -> None:
    """Stop a workspace app cleanly: cancel/join its background search, close its store."""
    if app is None:
        return
    svc = app.config["EDGELAB"]["services"]
    try:
        jm = svc._jobs
        if jm is not None:
            for j in jm.list():
                if j.get("state") in ("queued", "running"):
                    jm.cancel(j["job_id"])
            jm.join(timeout=30)
    finally:
        with svc.lock:
            svc.store.close()


class WorkspaceHost:
    def __init__(self, app_factory: Callable[[Path], object], lock_factory: Callable[[Path], object], *,
                 on_switch: Callable[[Path | None, Path], None] | None = None, notice: str | None = None):
        from edgelab.desktop_window import count_request
        self.app_factory, self.lock_factory, self.on_switch = app_factory, lock_factory, on_switch
        self.app = self.root = self.lock = None
        self.source = "none"
        self.notice = notice                                    # e.g. "the saved workspace is missing"
        self.served: dict[str, int] = {}
        self.browse: Callable[[], str | None] | None = None
        self._count = count_request
        self._mu = threading.RLock()
        self.shell = self._make_shell()

    # ---------------------------------------------------------------- WSGI
    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        self._count(self.served, path)
        app = self.app
        if (app is None or path.startswith("/api/workspace") or path.startswith("/api/desktop/")
                or path.startswith("/api/update/") or path == "/api/version"):   # updater: one per process
            return self.shell(environ, start_response)
        return app(environ, start_response)

    # ---------------------------------------------------------------- switching
    def open(self, root: Path, *, source: str, persist: bool, lock=None, allow_demo: bool = False) -> dict:
        """Make ``root`` the active workspace. ``lock``: an already-acquired instance lock.
        ``allow_demo``: only for an explicit ``--demo`` launch (the GUI never selects a demo folder)."""
        with self._mu:
            root = Path(root).expanduser().resolve()
            if self.root is not None and root == self.root:
                if lock is not None:
                    lock.release()
                return self.current_info()
            info = runtime.inspect_workspace(root)
            problems = [p for p in info["problems"] if not (allow_demo and info["demo"] and "demo workspace" in p)]
            if problems:
                if lock is not None:
                    lock.release()
                raise WorkspaceError(422, "This folder is not a usable EdgeLab research workspace: "
                                     + "; ".join(problems), info)
            if lock is None:
                lock = self.lock_factory(root)
                if not lock.acquire():
                    raise WorkspaceError(409, f"{root} is open in another EdgeLab window; close it there first.", info)
            try:
                runtime.init_workspace(root)                    # existing configs/data untouched; adds logs/ etc.
                app = self.app_factory(root)
            except Exception:
                lock.release()
                raise
            old_app, old_lock, old_root = self.app, self.lock, self.root
            self.app, self.lock, self.root, self.source = app, lock, root, source
            self.notice = None
            try:
                close_app(old_app)
            finally:
                if old_lock is not None:
                    old_lock.release()
            if persist:
                runtime.save_settings({"workspace": str(root)})
            if self.on_switch:
                self.on_switch(old_root, root)
            return self.current_info()

    def close(self) -> None:
        with self._mu:
            try:
                close_app(self.app)
            finally:
                if self.lock is not None:
                    self.lock.release()
                self.app = self.lock = None

    def current_info(self) -> dict | None:
        if self.root is None:
            return None
        return {**runtime.inspect_workspace(self.root), "source": self.source}

    def state(self) -> dict:
        default = runtime.user_data_root()
        return {"current": self.current_info(), "switchable": True, "notice": self.notice,
                "settings_path": str(runtime.settings_path()),
                "default": {"path": str(default), "info": runtime.inspect_workspace(default)},
                "browse_available": self.browse is not None}

    # ---------------------------------------------------------------- shell app
    def _make_shell(self):
        from flask import Flask, jsonify, request, send_from_directory
        from edgelab.runtime import static_dir
        shell = Flask("edgelab_workspace_shell", static_folder=None)
        STATIC = static_dir()

        def body_path() -> str:
            p = (request.get_json(silent=True) or {}).get("path")
            if not isinstance(p, str) or not p.strip() or len(p) > 1000:
                raise WorkspaceError(400, "path must be a folder path")
            return p.strip()

        @shell.errorhandler(WorkspaceError)
        def _ws_error(e: WorkspaceError):
            return jsonify({"error": {"kind": "workspace", "message": e.message, "details": "",
                                      **({"workspace": e.info} if e.info else {})}}), e.status

        @shell.errorhandler(ValueError)
        def _value_error(e: ValueError):
            return jsonify({"error": {"kind": "workspace", "message": str(e)}}), 422

        def update_body() -> dict:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise WorkspaceError(400, "request body must be a JSON object")
            return data

        from edgelab.updater.service import register_routes as register_update_routes
        register_update_routes(shell, update_body, lambda msg: WorkspaceError(400, msg))

        @shell.get("/api/workspace")
        def ws_state():
            return jsonify(self.state())

        @shell.post("/api/workspace/inspect")
        def ws_inspect():
            return jsonify(runtime.inspect_workspace(body_path()))

        @shell.post("/api/workspace/select")
        def ws_select():
            self.open(Path(body_path()), source="selected in Settings", persist=True)
            return jsonify(self.state())

        @shell.post("/api/workspace/create")
        def ws_create():
            p = Path(body_path())
            runtime.create_workspace(p)
            self.open(p, source="created in Settings", persist=True)
            return jsonify(self.state())

        @shell.post("/api/workspace/browse")
        def ws_browse():
            if self.browse is None:
                return jsonify({"error": {"kind": "workspace", "message": "No folder dialog in this mode; type the path."}}), 409
            return jsonify({"path": self.browse()})

        @shell.get("/api/health")
        def health():
            return jsonify({"backend": "ok", "demo": False, "workspace": None})

        @shell.route("/api/<path:_rest>", methods=["GET", "POST"])
        def no_workspace(_rest):
            return jsonify({"error": {"kind": "no_workspace", "message": "No research workspace is selected. "
                                      "Choose one in Settings → Research Workspace."}}), 409

        @shell.get("/")
        @shell.get("/<path:path>")
        def spa(path: str = ""):
            if path and (STATIC / path).is_file():
                return send_from_directory(STATIC, path)
            return send_from_directory(STATIC, "index.html")

        return shell

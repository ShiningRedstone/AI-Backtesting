"""Process-wide updater (one per running application, shared across workspace switches) and its HTTP
routes. The desktop launcher calls :func:`configure` with the installation folder, the relaunch
arguments, the folders that must never be replaced and a shutdown callback; a development server
leaves it unconfigured, so checks work but installing is reported as unsupported."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import edgelab
from edgelab.updater.core import UpdateError
from edgelab.updater.manager import UpdateManager
from edgelab.updater.source import default_source

_lock = threading.Lock()
_manager: UpdateManager | None = None


def _dirs() -> tuple[Path, Path]:
    from edgelab import runtime
    return runtime.settings_path().parent, runtime.update_cache_dir()


def configure(*, install_dir: Path | None, restart_args: list[str], protected: Callable[[], list[Path]],
              shutdown: Callable[[], None], source=None) -> UpdateManager:
    """Called once by the packaged desktop launcher."""
    global _manager
    state_dir, cache = _dirs()
    with _lock:
        _manager = UpdateManager(edgelab.__version__, source or default_source(), state_dir, cache,
                                 install_dir=install_dir, restart_args=restart_args, protected=protected,
                                 shutdown=shutdown)
    threading.Thread(target=_manager.cleanup, name="edgelab-update-cleanup", daemon=True).start()
    return _manager


def manager() -> UpdateManager:
    global _manager
    with _lock:
        if _manager is None:
            state_dir, cache = _dirs()
            _manager = UpdateManager(edgelab.__version__, default_source(), state_dir, cache)
        return _manager


def set_manager(m: UpdateManager | None) -> None:
    """Tests: install a manager with fixture sources."""
    global _manager
    with _lock:
        _manager = m


def register_routes(app, body: Callable[[], dict], bad: Callable[[str], Exception]) -> None:
    from flask import jsonify

    def fail(e: UpdateError):
        return jsonify({"error": {"kind": "update_error", "code": e.code, "message": e.message, "details": e.detail}}), 409

    def version_arg() -> str:
        v = body().get("version")
        if not isinstance(v, str) or len(v) > 20:
            raise bad("version must be a MAJOR.MINOR.PATCH string")
        return v

    @app.get("/api/version")
    def app_version():
        from edgelab import runtime
        m = runtime.build_manifest() if runtime.is_frozen() else None
        return jsonify({"version": edgelab.__version__, "packaged": runtime.is_frozen(),
                        "build_id": (m or {}).get("build_id"), "git_commit": (m or {}).get("git_commit"),
                        "built_at": (m or {}).get("built_at"), "build": runtime.build_label()})

    @app.get("/api/update/status")
    def update_status():
        m = manager()
        m.maybe_auto_check()                              # background; never delays the response
        return jsonify(m.status())

    @app.post("/api/update/check")
    def update_check():
        return jsonify(manager().check())

    @app.post("/api/update/skip")
    def update_skip():
        try:
            return jsonify(manager().skip(version_arg()))
        except UpdateError as e:
            return fail(e)

    @app.post("/api/update/unskip")
    def update_unskip():
        return jsonify(manager().unskip(version_arg()))

    @app.post("/api/update/later")
    def update_later():
        return jsonify(manager().later(version_arg()))

    @app.post("/api/update/preferences")
    def update_preferences():
        v = body().get("auto_check")
        if not isinstance(v, bool):
            raise bad("auto_check must be true or false")
        return jsonify(manager().set_auto_check(v))

    @app.post("/api/update/download")
    def update_download():
        try:
            return jsonify(manager().download(version_arg()))
        except UpdateError as e:
            return fail(e)

    @app.post("/api/update/apply")
    def update_apply():
        try:
            return jsonify(manager().apply(version_arg()))
        except UpdateError as e:
            return fail(e)

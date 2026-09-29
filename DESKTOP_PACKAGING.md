# Desktop packaging (Windows `.exe`) — architecture note

Status: **not packaged**. This note records the intended path and the blockers found by auditing
the code (Phase 6). No packaging code exists yet, and this phase deliberately made no packaging
refactor.

## Goal

A Windows executable that:

1. launches the local application and starts the backend;
2. serves the existing React UI and opens it;
3. needs no Python, Node, npm or Git on the user's machine, and no internet for local research;
4. keeps the user's datasets, runs, strategies and configuration **outside** the executable,
   persistent across application updates.

Datasets are never embedded.

## Current path (already compatible)

- **One UI stack.** The React + TypeScript frontend is compiled by esbuild into
  `edgelab/web/static/` (`app.js`, `index.html`, `styles.css`, `build-info.json`). That build is
  committed, so a packaged app ships the built static files and needs no Node at runtime or at
  package time. A test fails if the bundle is stale.
- **One backend.** Flask (`edgelab/web/app.py`) serves the static files and the `/api/*` routes.
  The routes are thin wrappers over `edgelab/services.py`, the same contracts the CLI uses.
- **One root.** Everything user-owned hangs off a single `root` directory:
  - `configs/` (research config plus `web.yaml`, `configs/prop/`);
  - `data/` (the SQLite store, datasets, feature cache, strategy library, prop simulations,
    `data/import/`);
  - `reports/`.

  Code never writes inside the package.
- **Local and offline.** The server binds to loopback by default and refuses other addresses
  without `--allow-remote`. There's no auth, cloud call or CDN, and the fonts and JS are local.

## Intended design

| Concern | Plan |
|---|---|
| Tool | **PyInstaller, `--onedir`**, driven by a checked-in `.spec` file and a pinned requirements lock, built on a clean Windows runner so builds are reproducible. Prefer onedir to onefile: faster start, no temp extraction, and antivirus false positives are rarer. |
| Entry point | A new `edgelab/desktop.py`. It calls `multiprocessing.freeze_support()`, then resolves the data root, picks a free loopback port, starts the existing `create_app(root)` on a server thread, and opens the UI. It reuses `edgelab.web.__main__` logic, but must not depend on the repository layout. |
| UI window | Phase 1: the default browser (`webbrowser.open("http://127.0.0.1:<port>")`), which needs zero extra dependencies. Optional later: an embedded webview (pywebview on WebView2, present on Windows 10 and 11) pointed at the same URL. The React app is unchanged either way. |
| Bundled (read-only) | Python runtime, the `edgelab` package, `edgelab/web/static/**`, **default** `configs/**` (research config, `web.yaml`, synthetic prop examples), `strategies/fixtures/**`, and the `tzdata` package data (Windows has no system tz database; `zoneinfo` needs it). |
| External (persistent) | The user data root, default `%LOCALAPPDATA%\EdgeLab\workspace` and overridable (`--root`, or a setting). On first run the bundled default `configs/` are **copied** there and never overwritten by updates. Holds `data/` (datasets, runs, library, caches, prop simulations), `data/import/`, `reports/` and logs. |
| Updates | Replace the application folder only; the workspace is untouched. Config schema changes need an explicit migration step (none exists yet). |
| Storage backend | Force `storage.backend: sqlite` in the packaged defaults. With `auto`, bundling DuckDB would make research refuse, per the known Phase 4 limitation. |

## Blockers and required changes before a real `.exe`

1. **Code-version identity breaks when frozen.**
   - `core.identity.source_hash()` hashes the `.py` files under the package directory. In a
     PyInstaller build those files are compiled into an archive, so the hash would silently cover
     nothing.
   - `git_commit()` shells out to Git, which isn't available there.
   - Needed: a build-time version stamp (git commit plus source hash, written at build time into a
     bundled `build_version.json`) that `code_version()` reads when `sys.frozen` is set. Without
     it, run reproducibility (research principle 9) degrades.
2. **Repository-relative paths.**
   - `edgelab/web/__main__.py` and `edgelab/web/bundle.py` use `REPO = parents[2]`: the demo
     workspace copies repo `configs/` and fixtures, and there's the stale-bundle check.
   - `edgelab/web/app.py` reads `reports/last_test_run.txt`.
   - `core/config.py` uses `PROJECT_ROOT` (`parents[2]`).
   - Frozen, these point inside the app folder. Needed: one `resources()` helper that resolves
     bundled defaults (via `sys._MEIPASS` when frozen), with the stale-bundle and test-status
     checks disabled in frozen builds.
3. **Working-directory default.** `Services(root=".")` and `python -m edgelab.web` default to the
   current directory. The desktop entry point must always pass an explicit workspace root and
   create it on first run.
4. **Process-parallel search on Windows.** `research/batch.py` uses `ProcessPoolExecutor` with
   `spawn`. A frozen app must call `multiprocessing.freeze_support()` first thing in the entry
   point, or worker processes re-launch the app. Background jobs default to `workers: 1`.
5. **Single instance per data root.** Concurrent processes on one root aren't coordinated: run ids
   are MAX+1, and restart reconciliation marks running searches `interrupted`. Needed: a lock file
   in the workspace, so a second launch opens the existing window instead of starting a second
   server.
6. **Fixed port.** `web.yaml` uses port 8765. The desktop entry should choose a free loopback port
   (or fall back when 8765 is busy).
7. **Dataset import UX.** Imports come only from `web.import_dirs` (`data/import`), and there's no
   browser upload. A desktop user needs either an "open import folder" action or a file picker
   that copies into the import folder. Both are small UI and API additions.
8. **Server.** `werkzeug.run_simple` is Flask's development server. It's acceptable on loopback
   for a single user, but a production WSGI server (e.g. waitress, pure Python) is the safer
   choice for the packaged app.
9. **Native dependencies.** numpy, pandas, scipy and (optionally) pyarrow are collected by
   PyInstaller's hooks. The build needs a test pass: the full test suite plus a scripted smoke test
   (start the app, hit `/api/health`, run one synthetic backtest) executed against the built
   folder.
10. **Code signing.** An unsigned `.exe` will trigger SmartScreen warnings. Signing is a
    distribution decision outside this project's scope.

## What stays true

- The research engine, DSL and services don't change for packaging. The desktop layer is a thin
  launcher around `create_app(root)`.
- The DSL remains the only strategy format. The packaged UI edits DSL documents through the same
  API.
- Live trading stays absent and disabled by default. Packaging adds no broker, network or cloud
  capability.

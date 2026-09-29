# Desktop packaging (Windows `EdgeLab.exe`)

Status:

- **Phase 7 (done, confirmed on Windows by the user):** folder-mode PyInstaller build, launcher, path
  handling, persistent workspace, build manifest. `EdgeLab.exe` launched on the user's machine with the
  real research store.
- **Native window (this change):** `EdgeLab.exe` now opens its own **EdgeLab application window**
  (pywebview hosting Microsoft Edge WebView2), not the default browser.
  - Implemented, and tested with deterministic tests and a frozen Linux build.
  - The real WebView2 window has **not** been exercised here: the development environment is Linux.
    Run `packaging\window_test_windows.py` on Windows.
- No installer exists yet.

## Two ways to run EdgeLab

### Development: browser + local server

```bash
python -m edgelab.web                    # repository workspace, open http://127.0.0.1:8765 in any browser
python -m edgelab.web --demo             # synthetic demo workspace
python -m edgelab.desktop --ui browser   # the desktop launcher's workflow, UI in the default browser
```

Nothing about this changed. The React bundle and Flask API are the same ones the packaged app uses.

### Packaged desktop: the native EdgeLab window

Double-click `dist\EdgeLab\EdgeLab.exe` and the EdgeLab window opens.

- **Window:** title "EdgeLab", 1440×900 by default, minimum 1000×680, resizable and maximizable.
  Text is selectable, and zoom (Ctrl +/-) is allowed.
- **Contents:** the React UI, served from `http://127.0.0.1:<free port>` by the bundled backend.
- **Nothing else opens:** no browser window and no console window.
- **Closing the window stops EdgeLab:** the backend shuts down cleanly (see below).
- **No developer tools:** debug mode is off.
- **Downloads are disabled:** the UI offers none. External links, which the UI doesn't use, would
  open in the default browser.
- **Browser storage:** WebView2 keeps it (e.g. builder drafts in `localStorage`) in
  `<workspace>\webview\`.

Flags:

| Flag | Effect |
|---|---|
| `--ui window` | The native window. This is the default. |
| `--ui browser` | Show the UI in the default browser, as before. |
| `--ui none` (alias `--no-browser`) | Headless: serve only, for tests and automation. |

`EdgeLabConsole.exe` is the same launcher built as a console program:

- logs are visible;
- `EdgeLabConsole.exe cli …` is the command line;
- the headless packaged smoke test drives it.

`EdgeLab.exe` itself has no console. Its output goes to `<workspace>\logs\console.log`, and startup
errors to `<workspace>\logs\desktop.log` plus a message box.

### Windows runtime prerequisite: Microsoft Edge WebView2 Runtime

- **What it is:** the window is rendered by the **Microsoft Edge WebView2 Runtime (Evergreen)**,
  Microsoft's system component for embedded web content.
- **Normally already installed:** it's part of **Windows 11**, and Windows Update installs it on
  current **Windows 10** systems (it has been pushed to Windows 10 since 2021).
- **Detection:** the launcher reads the runtime's registry registration (`EdgeUpdate\Clients\{F3017226-…}`,
  machine or per-user). The check runs **before anything starts**.
- **If it's missing:** EdgeLab shows a message box saying so. It points to Microsoft's download page
  (the "Evergreen" bootstrapper or standalone installer) and to the `--ui browser` alternative, then
  exits. There is **no silent fallback** to another engine or to Internet Explorer: the launcher
  requests the Edge Chromium backend explicitly.
- **Internet:** installing the runtime is the only step that may need a download, and only once. The
  research application itself never needs internet access.
- **Other dependencies:** pywebview on Windows also uses `pythonnet` and the .NET Framework 4.x
  that ships with Windows 10/11. Both the Python packages and their native loader DLLs are bundled by
  the build.

## Architecture

```
EdgeLab.exe (windowed) / EdgeLabConsole.exe (console)   packaging/launcher.py -> edgelab.desktop.main
  multiprocessing.freeze_support()            first: frozen worker processes for research
  resolve workspace (edgelab.runtime)         %LOCALAPPDATA%\EdgeLab (or --data-root / EDGELAB_DATA_ROOT)
  init workspace (first run)                  copy bundled default configs/ once; create data/, logs/, reports/
  single-instance lock                        logs\edgelab.lock; a 2nd launch brings the running window to the front
  window runtime check (--ui window)          WebView2 present? otherwise a clear message and exit (nothing started)
  create_app(workspace)                       the existing Flask app + React bundle, unchanged
  + loopback control channel                  /api/desktop/{status,focus,navigate}; per-launch token in runtime.json
  bind 127.0.0.1 : OS-assigned free port      never 0.0.0.0; --port only if asked
  wait for /api/health                        then runtime.json (url, ui, token, build) is written
  EdgeLab window -> http://127.0.0.1:<port>   pywebview on the main thread; blocks until the window closes
  window closed / Ctrl+C / SIGTERM            stop server, finish/cancel background job, close SQLite, release lock
```

- **One UI stack:** the React bundle in `edgelab/web/static`, committed.
- **One backend:** Flask over `edgelab/services.py`.
- **The window only presents the local URL.** No frontend or backend code knows whether it runs in a
  window or a browser.
- **The control channel is loopback-only.** It exists only in the launcher, not in
  `python -m edgelab.web`. Every call needs the per-launch token, which is stored only in the user's
  workspace. It lets a second launch focus the running window, and lets the Windows test navigate the
  window and read what it loaded.

## Bundled resources vs user configuration vs user data

`edgelab/runtime.py` is the single place that resolves locations. Nothing else checks
`sys._MEIPASS`.

| Kind | Development (repository checkout) | Packaged |
|---|---|---|
| Application code | `edgelab/*.py` | compiled into the bundle (no `.py` files are shipped) |
| Frontend | `edgelab/web/static/` | `_internal/edgelab/web/static/` |
| Default configs (read-only) | `configs/` | `_internal/configs/` |
| Demo fixtures (read-only) | `strategies/fixtures/` | `_internal/strategies/fixtures/` |
| Build identity | git + source files | `_internal/edgelab_build.json` (a copy sits next to the exe) |
| **User configs** | `configs/` of the root in use | `<workspace>\configs\` (copied from the defaults on first run, never overwritten) |
| **User data** | `data/` of the root in use | `<workspace>\data\` (SQLite store, datasets, runs, strategy library, feature cache, prop simulations, `import\`) |
| Logs / runtime | `logs/` | `<workspace>\logs\` (`desktop.log`, `runtime.json`, `edgelab.lock`, `edgelab_workspace.json`) |

**Workspace location:** `%LOCALAPPDATA%\EdgeLab` by default.

- Override it with `EdgeLab.exe --data-root D:\Research\EdgeLab` or the `EDGELAB_DATA_ROOT`
  environment variable.
- `--demo` uses a separate synthetic workspace, `<workspace>\demo`.
- A workspace inside the bundle is refused.
- Only `configs/` is ever copied into a workspace: no data and no repository files.

**When a new build ships different default configs:** the launcher lists the files where your
copy differs from the bundled defaults, and never changes them. User configuration belongs to the
user, and its content feeds the research config hash.

Development keeps working as before. `python -m edgelab.web` serves the repository root, and
`python -m edgelab.desktop` runs the desktop launcher from source against the per-user workspace
(or `--data-root`).

## Build / version metadata (reproducibility)

Every code hash in the research lineage used to come from source files at runtime:

- `code_version()`: `source_sha256` (all package `.py` files) and `git_commit` (runs `git`);
- the compiler source hash in the strategy provenance;
- each feature's `impl_hash` via `inspect.getsource`, which is part of the feature-cache keys.

In a packaged build the sources aren't on disk. `inspect.getsource` would raise, and the file
hashes would silently cover nothing.

**The fix:** `packaging/build.py` runs `edgelab.runtime.generate_build_manifest()` against the real
sources at build time and bundles the result as `edgelab_build.json`. It records:

- app version, git commit, and whether tracked files had uncommitted changes;
- `source_sha256`, `compiler_source_sha256`, every feature's `impl_hash`;
- the frontend source hash, the Python and PyInstaller versions, the build platform;
- `build_id` = hash of the identity fields (`built_at` is excluded, so rebuilding identical inputs
  gives the same id, and any code or toolchain change gives a new one).

**At runtime, when frozen,** all of those functions return the manifest values.

- Development output is unchanged.
- Packaged `code_version()` also carries `packaged: true`, `app_version` and `build_id`, and every
  run record stores it.
- A packaged build without a valid manifest, or missing a feature's hash, **refuses** with
  `BuildManifestError`. Nothing is faked or silently degraded.

**Verified on the Linux build:** a packaged run and a development run of the same source produced
identical results:

- the same `trades_hash`, config hash and strategy id;
- the same source hash;
- identical feature-cache keys (70 files).

## Port, startup and shutdown

- **Binding:** always 127.0.0.1. Port 0 means the OS assigns a free port, so there's no race and no
  fixed port. `--port N` is honoured, and if that port is busy you get a clear error. The UI isn't
  exposed to the LAN, and there's no auth, telemetry or network call.
- **Startup:**
  - Checks the IANA timezone database (the Windows build bundles `tzdata`).
  - Checks the build manifest.
  - Refuses a non-SQLite store.
  - With `--ui window`, checks for the WebView2 runtime before anything else starts.
  - Waits for `/api/health` before showing the UI (window, browser or none).
- **Startup failures:** written to `logs\desktop.log` and shown in a Windows message box (packaged)
  or on stderr.
- **Shutdown:** closing the EdgeLab window, or (console launcher) Ctrl+C, closing the console
  (`SetConsoleCtrlHandler`), SIGTERM or Ctrl+Break, which also close the window:
  - stops the server;
  - cancels and joins a running background search (it resumes later as `interrupted`, per Phase 4);
  - closes the SQLite connection and deletes `runtime.json`.
- **Hard kill:** loses nothing that was committed, because each store write commits.

## SQLite

- The desktop app uses the existing SQLite store.
  - The build environment must not contain DuckDB: `packaging/build.py` refuses if it's
    installed, and the spec excludes it.
  - So `storage.backend: auto` resolves to SQLite exactly as in development, and the research
    config hash is unchanged.
  - The launcher refuses to start on any other backend.
- The store is served by one process per workspace, enforced by the instance lock. Requests are
  serialized behind the existing service lock (ADR-30/38).
- Search worker processes never write the store (ADR-40).
- The CLI (`EdgeLabConsole.exe cli ...`) does not take the desktop lock: don't run CLI research against a
  workspace while the app is serving it (the Phase 4 one-process limitation).

## Multiprocessing

`freeze_support()` runs before anything else. The `spawn` worker pool in `research/batch.py`
then re-launches the frozen executable as workers.

- **Verified in the frozen Linux build:** `EdgeLabConsole cli research run ... --workers 2` completed with
  worker processes.
- **Background jobs:** still run with `workers: 1`, as in development.

## Build (developers only)

Prerequisites for building (end users need none of these):

- **Windows 10/11 x64** with **Python 3.11+ 64-bit**;
- **Node 18+ with npm**, to rebuild the frontend. The build runs `npm ci` in `web\`, which installs
  the exact versions locked in `web\package-lock.json` into `web\node_modules` (network needed at
  build time only; nothing global). `-SkipFrontend` uses the committed bundle instead, which must
  match `web/src`;
- **Git**, so the manifest records the commit.

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1            # full build
powershell -ExecutionPolicy Bypass -File build_windows.ps1 -Smoke     # + packaged smoke test
```

`build_windows.ps1` creates `.venv-build` from `packaging/requirements-build.txt`, which has no
DuckDB and includes `tzdata`, `pywebview` (with `pythonnet` on Windows) and PyInstaller. It then runs
`packaging/build.py`, which:

1. installs the locked frontend dependencies (`npm ci`) and builds the frontend (`web/build.mjs`);
2. verifies the bundle is fresh;
3. writes `build/edgelab_build.json`;
4. runs PyInstaller with `packaging/edgelab.spec` (folder mode). The spec refuses to build without
   pywebview, and builds two launchers over one bundle;
5. copies the manifest next to the exe;
6. with `-Smoke`: runs the headless packaged smoke test (`EdgeLabConsole.exe`) and then, on Windows,
   the native-window test (`EdgeLab.exe`, which opens and closes its own window).

**Output:** `dist\EdgeLab\EdgeLab.exe` (the desktop app), `dist\EdgeLab\EdgeLabConsole.exe` and
`dist\EdgeLab\_internal\`. Distribute the whole
`dist\EdgeLab` folder; it contains no user data. Other platforms: `python packaging/build.py` gives
`dist/EdgeLab/EdgeLab`.

Build from a clean virtual environment. A system Python with unrelated broken packages can break
PyInstaller's hooks.

## Smoke test (read-only, scratch data only)

```bash
python packaging/smoke_packaged.py --exe dist/EdgeLab/EdgeLab.exe      # or --cmd "python -m edgelab.desktop"
```

It creates a fresh scratch workspace with the synthetic demo data and never touches a real store.
It checks:

- the exe exists and launches, binds loopback, and serves the UI, API and static assets;
- the data root is outside the bundle and resources come from inside it;
- config loads, the store is SQLite, and the build id and source hash match the manifest;
- datasets and caveats are listed;
- strategy workflow: open, validate (same id and hash), edit, save as a new version, lineage, and
  the parent hash is unchanged;
- a backtest runs, and its run records the build's code version;
- a prop simulation runs, and the run is unchanged afterwards;
- a second launch reuses the running instance;
- shutdown is clean, `runtime.json` is removed, and SQLite `integrity_check` passes with all runs
  kept;
- a restart reuses the store;
- a CLI search with 2 worker processes completes.

`tests/test_desktop.py` runs it automatically when `dist/EdgeLab` exists.

## Native window test (Windows only; real WebView2; scratch data only)

```powershell
.venv-build\Scripts\python.exe packaging\window_test_windows.py --exe dist\EdgeLab\EdgeLab.exe
```

`build_windows.ps1 -Smoke` runs it automatically after the headless smoke. It uses a fresh scratch
demo workspace, never a real research store. It checks:

- `EdgeLab.exe` launches in window mode (no browser) on a loopback URL, with the data root outside the
  bundle;
- a visible top-level window titled "EdgeLab" belongs to the process (Win32 `EnumWindows`);
- the React UI loaded inside it: the backend served `/`, `/app.js` and API calls to the window, and
  the window reported "loaded";
- the Datasets and Strategy Lab pages load: the window is navigated there, and `/api/datasets` and
  `/api/strategies` are served;
- closing the window (`WM_CLOSE`, like the title-bar ×) ends the process with exit code 0, removes
  `runtime.json`, releases the lock, and SQLite `integrity_check` passes.

Off Windows it prints `NOT RUN` and exits with code 2. It never reports a pass it didn't observe.

`tests/test_desktop_window.py` covers the same lifecycle deterministically on any OS, with a fake
pywebview GUI loop over the real launcher, server, lock and store:

- startup, readiness and navigation;
- focus and the control-channel token;
- a clean close;
- refusal when the runtime is missing;
- second-instance hand-off;
- WebView2 registry detection;
- redirecting the windowed exe's output to a log.

## Existing repository data (future migration path)

Nothing is migrated automatically, and the developer checkout's `data/` is never touched by the
packaged app unless you point it there explicitly. Options, in order of safety:

1. **Copy (recommended when ready):**
   1. Close both EdgeLab processes.
   2. Copy `C:\Users\<you>\Documents\AI-Backtesting\data` to `%LOCALAPPDATA%\EdgeLab\data`.
   3. Copy the repository's `configs\` to `%LOCALAPPDATA%\EdgeLab\configs`, so the config hash and
      the cost, exclusion and calendar definitions of your existing runs match.

   The original stays untouched as a backup. The copied store re-verifies dataset hashes on load.
2. **Point the app at the checkout:** `EdgeLab.exe --data-root C:\Users\<you>\Documents\AI-Backtesting`.
   It uses that root's `configs/` and `data/` in place, only adds `logs/` (git-ignored) and
   `data/import/`, and new runs record the packaged build id. Never run it at the same time as
   `python -m edgelab.web` on that root.
3. A guided, verified migration command (copy, then hash-check every dataset and run) is future
   work.

## Current limitations

- The native window has not been exercised on Windows yet; run `window_test_windows.py` on Windows.
  On Linux/macOS, pywebview needs GTK/Qt (Linux) or Cocoa, which the Linux build doesn't include.
  There, the window mode fails with a clear message: use `--ui browser`.
- Requires the Microsoft Edge WebView2 Runtime (see above). There is no automatic install and no
  bundled fixed-version runtime.
- A second launch focuses the running window. If the running instance has no window (`--ui none` or
  `browser`), the second launch opens a window onto the same backend. There is never a second server.
- There is no tray icon and no in-app "Quit": closing the window quits.
- Unsigned executable: Windows SmartScreen will warn. No installer or auto-update exists.
- The server is still werkzeug's threaded server (loopback, single user).
- File import still reads from `<workspace>\data\import\`. There's no browser upload or file picker
  yet.
- The build is about 175 MB (numpy/pandas/scipy, plus pywebview/pythonnet on Windows). No size
  optimization yet.
- Updating a build doesn't update user configs; differences are only reported.

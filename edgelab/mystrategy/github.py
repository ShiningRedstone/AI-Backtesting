"""Upload My strategy reports to GitHub and fetch test plans (ADR-93).

Reports go to the branch ``strategy-reports`` of the app's repository (it holds only report data: no workflow files,
so an upload never starts a Windows build) under ``my_strategy/reports/<date>/<report id>/``; ``my_strategy/latest.json``
always names the newest upload. Test plans (settings variations to run) are read from ``my_strategy/plans/*.json`` on
the same branch.

The GitHub token (fine-grained, "Contents: read and write" on that one repository) is stored per user OUTSIDE every
workspace (next to the app settings file, never inside a git clone), is never returned by any API and never logged.
Only HTTPS to api.github.com is used.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

REPO = "ShiningRedstone/AI-Backtesting"
BRANCH = "strategy-reports"
ROOT = "my_strategy"
API = "https://api.github.com"
TIMEOUT = 120.0
MAX_BLOB = 90 * 1024 * 1024


class UploadError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Transport(Protocol):
    def request(self, method: str, url: str, token: str, body: dict | None) -> tuple[int, Any]: ...


class UrllibTransport:
    def request(self, method: str, url: str, token: str, body: dict | None) -> tuple[int, Any]:
        if not url.startswith(API + "/"):
            raise UploadError("BAD_URL", "only api.github.com is used")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "MunyunLab-MyStrategy",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:      # noqa: S310 - https api.github.com only
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"null")
            except ValueError:
                payload = None
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise UploadError("OFFLINE", f"cannot reach GitHub ({type(e).__name__})") from None


# ------------------------------------------------------------------------------------------------ token
def token_path() -> Path:
    from edgelab.runtime import settings_path
    return settings_path().parent / "github_upload.json"


def _load() -> dict:
    try:
        d = json.loads(token_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def status() -> dict:
    d = _load()
    return {"token_present": bool(d.get("token")), "repo": REPO, "branch": BRANCH, "folder": ROOT,
            "saved_at": d.get("saved_at")}


def set_token(token: str) -> dict:
    token = (token or "").strip()
    if not re.fullmatch(r"(github_pat_|ghp_|gho_|ghu_)[A-Za-z0-9_]{20,255}", token):
        raise UploadError("BAD_TOKEN", "That does not look like a GitHub token (github_pat_... or ghp_...).")
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"token": token, "saved_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    return status()


def clear_token() -> dict:
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass
    return status()


def _token() -> str:
    t = _load().get("token")
    if not t:
        raise UploadError("NO_TOKEN", "Add a GitHub token first (My strategy -> Upload settings).")
    return t


def _fail(status_code: int, payload: Any, what: str) -> UploadError:
    msg = (payload or {}).get("message") if isinstance(payload, dict) else None
    if status_code in (401, 403):
        return UploadError("NOT_ALLOWED", f"GitHub refused the token while {what} (HTTP {status_code}). The token needs "
                                          "'Contents: read and write' on " + REPO + ".")
    return UploadError("HTTP_ERROR", f"GitHub answered HTTP {status_code} while {what}" + (f": {msg}" if msg else ""))


# ------------------------------------------------------------------------------------------------ upload
def upload_files(files: dict[str, bytes], message: str, transport: Transport | None = None,
                 token: str | None = None) -> dict:
    """Commit ``{repo path: bytes}`` to the reports branch in ONE commit (creating the branch, without history, if it
    does not exist yet). Returns {commit, branch, paths}."""
    tr = transport or UrllibTransport()
    tok = token or _token()
    base = f"{API}/repos/{REPO}"
    code, ref = tr.request("GET", f"{base}/git/ref/heads/{quote(BRANCH)}", tok, None)
    parent_sha, base_tree = None, None
    if code == 200:
        parent_sha = ref["object"]["sha"]
        code, com = tr.request("GET", f"{base}/git/commits/{parent_sha}", tok, None)
        if code != 200:
            raise _fail(code, com, "reading the reports branch")
        base_tree = com["tree"]["sha"]
    elif code != 404:
        raise _fail(code, ref, "reading the reports branch")
    tree = []
    for path, data in files.items():
        if len(data) > MAX_BLOB:
            raise UploadError("TOO_LARGE", f"{path} is {len(data) / 1e6:.0f} MB; GitHub accepts at most ~90 MB per file.")
        code, blob = tr.request("POST", f"{base}/git/blobs", tok,
                                {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
        if code != 201:
            raise _fail(code, blob, f"uploading {path}")
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    code, t = tr.request("POST", f"{base}/git/trees", tok, {"tree": tree, **({"base_tree": base_tree} if base_tree else {})})
    if code != 201:
        raise _fail(code, t, "writing the folder")
    code, c = tr.request("POST", f"{base}/git/commits", tok,
                         {"message": message, "tree": t["sha"], "parents": [parent_sha] if parent_sha else []})
    if code != 201:
        raise _fail(code, c, "writing the commit")
    if parent_sha:
        code, r = tr.request("PATCH", f"{base}/git/refs/heads/{quote(BRANCH)}", tok, {"sha": c["sha"], "force": False})
        ok = code == 200
    else:
        code, r = tr.request("POST", f"{base}/git/refs", tok, {"ref": f"refs/heads/{BRANCH}", "sha": c["sha"]})
        ok = code == 201
    if not ok:
        raise _fail(code, r, "moving the reports branch")
    return {"commit": c["sha"], "branch": BRANCH, "paths": sorted(files)}


def report_files(folder: Path, include_candles: bool = True) -> dict[str, bytes]:
    names = ["summary.json", "trades.json.gz", "days.json.gz"] + (["candles.jsonl.gz"] if include_candles else [])
    return {n: (folder / n).read_bytes() for n in names if (folder / n).exists()}


def upload_report(folder: Path, kind: str, include_candles: bool = True, transport: Transport | None = None,
                  extra: dict[str, bytes] | None = None) -> dict:
    sm = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    day = str(sm.get("created_at", ""))[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"{ROOT}/reports/{day}/{folder.name}"
    files = {f"{prefix}/{n}": b for n, b in report_files(folder, include_candles).items()}
    for n, b in (extra or {}).items():
        files[f"{prefix}/{n}"] = b
    latest = {"report_id": folder.name, "kind": kind, "path": prefix,
              "uploaded_at": datetime.now(timezone.utc).isoformat(), "label": sm.get("label"),
              "settings_hash": sm.get("settings_hash"), "trade_count": sm.get("trade_count")}
    files[f"{ROOT}/latest.json"] = json.dumps(latest, indent=1).encode()
    out = upload_files(files, f"My strategy report {folder.name} [skip ci]", transport)
    return {**out, "path": prefix, "latest": latest}


# ------------------------------------------------------------------------------------------------ plans
def list_plans(transport: Transport | None = None) -> list[dict]:
    tr = transport or UrllibTransport()
    code, rows = tr.request("GET", f"{API}/repos/{REPO}/contents/{ROOT}/plans?ref={quote(BRANCH)}", _token(), None)
    if code == 404:
        return []
    if code != 200:
        raise _fail(code, rows, "listing test plans")
    return [{"name": r["name"][:-5], "path": r["path"], "size": r.get("size")}
            for r in rows if r.get("type") == "file" and r["name"].endswith(".json")]


def get_plan(name: str, transport: Transport | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise UploadError("BAD_NAME", "invalid plan name")
    tr = transport or UrllibTransport()
    code, doc = tr.request("GET", f"{API}/repos/{REPO}/contents/{ROOT}/plans/{quote(name)}.json?ref={quote(BRANCH)}",
                           _token(), None)
    if code != 200:
        raise _fail(code, doc, f"reading plan {name}")
    try:
        return json.loads(base64.b64decode(doc["content"]).decode("utf-8"))
    except (KeyError, ValueError) as e:
        raise UploadError("BAD_PLAN", f"plan {name} is not valid JSON ({type(e).__name__})") from None

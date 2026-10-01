"""Atomic small-file writes that survive Windows sharing violations.

On Windows, replacing a file fails with ``PermissionError`` ([WinError 5] Access is denied / [WinError 32]) while ANY
other handle has the destination open: a page request reading the same file (reads run outside the service lock,
ADR-78), an antivirus scan or the search indexer. Those handles are held for milliseconds, so the replace is retried
with a short backoff. Each write uses its own temp file, so two writers never touch the same temp path.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

REPLACE_ATTEMPTS = 40                 # ~6 s in total with the backoff below
REPLACE_BACKOFF_S = (0.01, 0.25)      # first wait, longest wait


def replace_with_retry(src: Path | str, dst: Path | str, attempts: int = REPLACE_ATTEMPTS) -> None:
    """``os.replace(src, dst)``, retried while the destination (or source) is briefly held open elsewhere."""
    wait, cap = REPLACE_BACKOFF_S
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait)
            wait = min(wait * 2, cap)


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to a unique temp file next to ``path`` and move it into place (retried, see module doc).
    Readers see either the old or the new content, never a partial file."""
    p = Path(path)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        replace_with_retry(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

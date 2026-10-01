"""Test package. The read-view facets cache (ADR-75) normally lives in a per-user cache folder; the suite points it at
a temporary folder so running the tests leaves nothing behind outside the repository."""
import atexit
import os
import shutil
import tempfile

if "EDGELAB_VIEW_CACHE" not in os.environ:
    _cache = tempfile.mkdtemp(prefix="edgelab-test-view-cache-")
    os.environ["EDGELAB_VIEW_CACHE"] = _cache
    atexit.register(shutil.rmtree, _cache, ignore_errors=True)

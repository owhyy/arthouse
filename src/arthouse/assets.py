"""Content-hashed static asset URLs for cache busting.

Templates call the `asset()` global (registered in `create_app`) instead of
building static URLs by hand: `{{ asset('css/app.css') }}` renders
`/static/css/app.css?v=<hash>`. The hash changes whenever the file changes,
so browsers and CDNs treat it as a new resource.
"""

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STATIC_URL_PREFIX = "/static"


def hash_file(filepath: Path) -> str:
    """First 10 hex chars of the SHA-256 digest of the file contents."""
    return hashlib.sha256(filepath.read_bytes()).hexdigest()[:10]


def build_asset_map(static_dir: Path) -> dict[str, str]:
    """Map every file under static_dir (posix relative path) to its content hash."""
    return {
        filepath.relative_to(static_dir).as_posix(): hash_file(filepath)
        for filepath in sorted(static_dir.rglob("*"))
        if filepath.is_file()
    }


def asset_url(
    static_dir: Path, asset_map: dict[str, str], path: str, *, debug: bool = False
) -> str:
    """Versioned URL for a static path: /static/css/app.css?v=<hash>.

    With debug=True the hash is recomputed per call, so asset edits show up
    without a server restart. Unknown paths raise in debug and fall back to
    an unversioned URL in production.
    """
    clean_path = path.lstrip("/")

    if debug:
        filepath = static_dir / clean_path
        if not filepath.is_file():
            raise RuntimeError(f"asset(): unknown static path {path!r}")
        version = hash_file(filepath)
    else:
        version = asset_map.get(clean_path)
        if version is None:
            logger.warning("asset(): unknown static path %r, serving unversioned", path)
            return f"{STATIC_URL_PREFIX}/{clean_path}"

    return f"{STATIC_URL_PREFIX}/{clean_path}?v={version}"

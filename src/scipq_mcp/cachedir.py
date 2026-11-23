"""Where the auxiliary index lives, and the checks that keep it private.

Default: the user cache directory — $XDG_CACHE_HOME/scipq or ~/.cache/scipq on Linux,
~/Library/Caches/scipq on macOS. `--cache-dir DIR` replaces it. Each checkout gets its own
entry, `<basename>-<sha256(realpath)[:12]>/`, so worktrees never share one.

The base directory and every entry are created 0700 and, when opened, must be real directories
(not symlinks) owned by the current user with no group/other access. This matters most when the
cache is pointed at a shared place such as /tmp.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys

APP = "scipq"


class CacheError(Exception):
    pass


def default_base() -> str:
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", APP)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, APP)


def entry_name(root: str) -> str:
    real = os.path.realpath(root)
    digest = hashlib.sha256(real.encode("utf-8", errors="surrogateescape")).hexdigest()[:12]
    base = os.path.basename(real.rstrip(os.sep)) or "root"
    return f"{base}-{digest}"


def ensure_private_dir(path: str) -> str:
    """Creates `path` (0700) if needed and verifies it is a private directory of ours."""
    path = os.path.abspath(path)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        raise CacheError(f"cannot create cache directory {path}: {e}") from e
    check_private_dir(path)
    return path


def check_private_dir(path: str) -> None:
    try:
        st = os.lstat(path)
    except OSError as e:
        raise CacheError(f"cannot stat cache directory {path}: {e}") from e
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise CacheError(f"refusing cache directory {path}: not a real directory (symlink?)")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise CacheError(f"refusing cache directory {path}: owned by uid {st.st_uid}, "
                         f"not {os.getuid()}")
    if st.st_mode & 0o077:
        try:
            os.chmod(path, 0o700)
        except OSError as e:
            raise CacheError(f"refusing cache directory {path}: group/other access and "
                             f"cannot chmod it: {e}") from e


def entry_dir(root: str, base: str | None = None) -> str:
    """The private directory holding the index of one checkout, created if needed."""
    base_dir = ensure_private_dir(base or default_base())
    return ensure_private_dir(os.path.join(base_dir, entry_name(root)))


def entries(base: str | None = None) -> list[dict]:
    """Index entries under the cache base: {dir, root, exists, bytes, last_used}."""
    import sqlite3

    base = base or default_base()
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        d = os.path.join(base, name)
        db = os.path.join(d, "index.sqlite")
        if not os.path.isdir(d) or os.path.islink(d) or not os.path.exists(db):
            continue
        info = {"dir": d, "root": "", "exists": False, "bytes": 0, "last_used": 0.0}
        for suffix in ("", "-wal", "-shm"):
            try:
                info["bytes"] += os.path.getsize(db + suffix)
            except OSError:
                pass
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            meta = dict(con.execute("SELECT key, value FROM meta"))
            con.close()
            info["root"] = meta.get("root_realpath", "")
            info["last_used"] = float(meta.get("last_used_at") or meta.get("created_at") or 0)
        except Exception:  # unreadable entry: report it, prune will remove it
            pass
        info["exists"] = bool(info["root"]) and os.path.isdir(info["root"])
        out.append(info)
    return out


_ENTRY_NAME = re.compile(r".+-[0-9a-f]{12}")


def empty_entries(base: str | None = None, min_age: float = 86400.0) -> list[str]:
    """Entry directories without an index (an interrupted first build), older than min_age
    seconds (a younger one may be being built)."""
    import time

    base = base or default_base()
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    now = time.time()
    for name in names:
        d = os.path.join(base, name)
        if not _ENTRY_NAME.fullmatch(name) or not os.path.isdir(d) or os.path.islink(d) or \
                os.path.exists(os.path.join(d, "index.sqlite")):
            continue
        try:
            if now - os.path.getmtime(d) >= min_age:
                out.append(d)
        except OSError:
            continue
    return out


def remove_entry(d: str) -> None:
    import shutil

    check_private_dir(d)
    shutil.rmtree(d)


def prune(base: str | None = None, older_than_days: float | None = None) -> list[dict]:
    """Removes entries whose root is gone, and (if given) unused for that many days."""
    import time

    removed = []
    now = time.time()
    for e in entries(base):
        if not e["root"]:
            # No metadata yet (another process may be creating it) or unreadable: only when old.
            try:
                age = now - os.path.getmtime(e["dir"])
            except OSError:
                continue
            if age < 86400:
                continue
        stale = older_than_days is not None and e["last_used"] and \
            now - e["last_used"] > older_than_days * 86400
        if not e["exists"] or stale:
            try:
                remove_entry(e["dir"])
                removed.append(e)
            except (OSError, CacheError):
                pass
    for d in empty_entries(base):
        try:
            remove_entry(d)
            removed.append({"dir": d, "root": "", "exists": False, "bytes": 0,
                            "last_used": 0.0})
        except (OSError, CacheError):
            pass
    return removed

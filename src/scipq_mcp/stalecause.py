"""Why sources are newer than their shards, from file times alone (no git command, nothing
written): what changed them (a checkout, reset, merge… in HEAD's reflog) and whether javac
compiled them since (their class files). "Recompile" is the answer either way; the cause
says whether a build already ran and left the shards behind (FROM-CACHE, no plugin) or
none did (a branch switch after the last build)."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from .workspace import Freshness, Workspace

REFLOG_TAIL = 256 * 1024   # bytes of HEAD's reflog read: its newest entries
GIT_WINDOW = 10.0          # seconds a git operation may take writing the files it changes
MAX_CHECKED = 20           # stale files whose class file is looked at
_ENTRY = re.compile(r"([0-9a-f]{7,64}) ([0-9a-f]{7,64}) .*?> (\d+) [+-]\d{4}\t(.*)$")


@dataclass(frozen=True, slots=True)
class HeadMove:
    when: float
    old: str
    new: str
    message: str    # "checkout: moving from main to topic"

    @property
    def writes_files(self) -> bool:
        """A commit moves HEAD without touching the working tree; a checkout, reset, merge,
        pull, rebase… rewrites files."""
        return not self.message.startswith(("commit", "branch:"))


@dataclass(slots=True)
class Cause:
    brief: str = ""   # for the WARNING of an answer ("" when unknown)
    full: str = ""    # for index_status


def git_dir(root: str) -> str | None:
    """The checkout's git directory: `.git`, or the one a worktree's `.git` file names."""
    p = os.path.join(root, ".git")
    if os.path.isdir(p):
        return p
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            m = re.match(r"gitdir:\s*(.+)", fh.read().strip())
    except OSError:
        return None
    if not m:
        return None
    d = m.group(1).strip()
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(root, d))


_moves_cache: dict[str, tuple[tuple[float, int], list[HeadMove]]] = {}


def head_moves(root: str) -> list[HeadMove]:
    """HEAD's newest reflog entries, oldest first (read from the end of logs/HEAD)."""
    d = git_dir(root)
    if d is None:
        return []
    path = os.path.join(d, "logs", "HEAD")
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime, st.st_size)
    hit = _moves_cache.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            if st.st_size > REFLOG_TAIL:
                fh.seek(st.st_size - REFLOG_TAIL)
                fh.readline()   # a partial line
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        m = _ENTRY.match(line)
        if m:
            out.append(HeadMove(float(m.group(3)), m.group(1), m.group(2), m.group(4).strip()))
    _moves_cache[path] = (key, out)
    return out


def class_file(root: str, source: str, module_task: str) -> str | None:
    """The class file javac writes for a source's top-level type (Gradle's or Maven's
    layout), when it exists."""
    from .doctor import _class_dirs, _package, _source_set
    module, _, task = module_task.rpartition("/")
    sset = _source_set(task) or {"compile": "main", "testCompile": "test"}.get(task, "")
    if not sset:
        return None
    pkg = _package(root, source)
    cls = (pkg.replace(".", "/") + "/" if pkg else "") + \
        os.path.basename(source)[:-len(".java")] + ".class"
    for d in _class_dirs(root, module, sset):
        p = os.path.join(d, cls)
        if os.path.isfile(p):
            return p
    return None


def _clock(t: float) -> str:
    return time.strftime("%H:%M:%S" if time.strftime("%Y-%m-%d", time.localtime(t)) ==
                         time.strftime("%Y-%m-%d") else "%Y-%m-%d %H:%M:%S", time.localtime(t))


def _sha(text: str) -> str:
    return text[:10]


def _message(move: HeadMove) -> str:
    """The reflog message with its full hashes shortened."""
    return re.sub(r"\b([0-9a-f]{10})[0-9a-f]{10,54}\b", r"\1", move.message)


def cause(ws: Workspace, f: Freshness | None) -> Cause:
    """Why f's stale files are stale, when file times tell it."""
    if f is None or not f.stale:
        return Cause()
    stale = [s for s in f.stale if s in f.times]
    # Compiled since the change? Their class files say it (javac writes them every time).
    since, not_since = 0, 0
    for source in stale[:MAX_CHECKED]:
        shards = ws.shard_for_source(source)
        cls = class_file(ws.root, source, shards[0].module) if shards else None
        if cls is None:
            continue
        try:
            built = os.stat(cls).st_mtime
        except OSError:
            continue
        if built >= f.times[source][1]:
            since += 1
        else:
            not_since += 1
    # What changed them: a git operation writing files, at the time they changed.
    moves = [m for m in head_moves(ws.root) if m.writes_files]
    by_move: dict[HeadMove, list[str]] = {}
    for source in stale:
        compiled, changed = f.times[source]
        mv = next((m for m in moves if m.when > compiled and
                   m.when - GIT_WINDOW <= changed <= m.when + 2), None)
        if mv is not None:
            by_move.setdefault(mv, []).append(source)
    git_brief, git_full = "", ""
    if by_move:
        mv, files = max(by_move.items(), key=lambda kv: len(kv[1]))
        compiled = min(f.times[s][0] for s in files)
        before = [m for m in head_moves(ws.root) if m.when <= compiled]
        head = before[-1].new if before else mv.old
        n = len(files)
        git_brief = f"git changed {'it' if n == 1 else 'them'} at {_clock(mv.when)}, " \
                    f"{_message(mv)}"
        some = "it" if n == 1 else "them"
        git_full = (f"{'' if n == len(f.stale) else f'{n} of the {len(f.stale)} '}changed "
                    f"at {_clock(mv.when)}, when git wrote {some} ({_message(mv)}), after "
                    f"{'its' if n == 1 else 'their'} compilation at {_clock(compiled)} with "
                    f"HEAD at {_sha(head)}: the index shows {_sha(head)}'s version of {some}")
    them = "it" if len(stale) == 1 else "them"
    if since and not not_since:
        build = (f"javac compiled {them} after the change without writing the shards: the "
                 "task came FROM-CACHE without them, or ran without the plugin (an IDE build, "
                 "-Pscip=false): --rerun it with the plugin")
    elif not_since and not since:
        build = f"nothing compiled {them} since" + (
            "" if git_brief else " (the class files are older than the change)")
    elif since:
        build = (f"of {since + not_since} with class files, {since} were compiled after the "
                 f"change without writing their shards (FROM-CACHE without them, or no "
                 f"plugin: --rerun the task) and {not_since} not compiled since")
    else:
        build = ""
    brief = "; ".join(x for x in (git_brief, build) if x)
    if not_since and not since and not git_brief:
        brief = f"nothing compiled {them} since {'it' if them == 'it' else 'they'} changed"
    full = "; ".join(x for x in (git_full, build) if x)
    if brief and not since:
        # The next build may still come FROM-CACHE (back on a branch built before).
        rerun = "if the shards stay old after a build, it came FROM-CACHE without them: --rerun it"
        brief, full = f"{brief}; {rerun}", f"{full}; {rerun}"
    return Cause(brief, ("why stale: " + full) if full else "")

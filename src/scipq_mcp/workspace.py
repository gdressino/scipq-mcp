"""Shard discovery, content prefiltering and freshness checks for one project checkout.

Layout (see the Gradle init template):

    <root>/build/scip-targetroot/<module>/<task>/META-INF/scip/<source relative to root>.scip

Nothing is kept between queries: every call lists the shard roots, prefilters shards by content
with ``grep -F`` (or a pure-Python scan when grep is missing) and decodes only the candidates.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Iterable

DEFAULT_SHARDS_DIR = os.path.join("build", "scip-targetroot")
SUFFIX = ".scip"
MARKER = ("META-INF", "scip")

# Byte that follows a symbol string, by context (see scip.py for the field numbers).
# SymbolInformation.symbol (field 1) is followed by documentation (0x1a), relationships (0x22),
# kind (0x28) or display_name (0x32); Occurrence.symbol (field 2) by symbol_roles (0x18) or
# single/multi-line range (0x42/0x4a). These are only prefilters: candidates are always decoded.
DEFINITION_TAGS = (b"\x28", b"\x32", b"\x1a", b"\x22")
OCCURRENCE_TAGS = (b"\x42", b"\x18", b"\x4a")
# Relationship{symbol, is_implementation=true} and Relationship{symbol, is_reference=true,
# is_implementation=true}: how subtypes and overrides point at what they extend.
IMPLEMENTATION_TAGS = (b"\x18\x01", b"\x10\x01\x18\x01")


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Shard:
    path: str      # absolute path of the .scip file
    source: str    # source path relative to the project root, with forward slashes
    module: str    # <module>/<task> directory, relative to the shards dir


@dataclass(slots=True)
class Freshness:
    stale: list[str] = field(default_factory=list)       # source newer than its shard
    uncompiled: list[str] = field(default_factory=list)  # .java with no shard, newer than its module's shards
    tasks: set[str] = field(default_factory=set)         # compile tasks that would refresh them

    def __bool__(self) -> bool:
        return bool(self.stale or self.uncompiled)


def find_project_root(start: str) -> str:
    """Closest ancestor of `start` (inclusive) that has a shards dir, else the git top level,
    else `start` itself."""
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, DEFAULT_SHARDS_DIR)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    try:
        out = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.abspath(start)


class Workspace:
    def __init__(self, root: str, shards_dir: str | None = None):
        self.root = os.path.abspath(root)
        self.shards_dir = os.path.join(self.root, shards_dir or DEFAULT_SHARDS_DIR)
        if not os.path.isdir(self.shards_dir):
            raise WorkspaceError(f"no SCIP shards under {self.shards_dir}")
        self._scip_roots: list[tuple[str, str]] | None = None
        self._files: list[tuple[int, str]] | None = None
        self._root_prefixes: list[str] | None = None
        self._grep = shutil.which("grep")

    # --- layout -------------------------------------------------------------------------

    def scip_roots(self) -> list[tuple[str, str]]:
        """[(absolute .../META-INF/scip dir, "<module>/<task>")]."""
        if self._scip_roots is None:
            roots = []
            for d, dirs, _ in os.walk(self.shards_dir):
                if os.path.basename(d) == MARKER[1] and os.path.basename(os.path.dirname(d)) == MARKER[0]:
                    module = os.path.relpath(os.path.dirname(os.path.dirname(d)), self.shards_dir)
                    roots.append((d, module.replace(os.sep, "/")))
                    dirs[:] = []
                elif MARKER[0] in dirs:
                    dirs[:] = [MARKER[0]]
            roots.sort()
            self._scip_roots = roots
        return self._scip_roots

    def files(self) -> list[tuple[int, str]]:
        """[(index into scip_roots(), shard path relative to that root with '/')] for every
        shard on disk, orphans included. Listed once per Workspace (that is, per call)."""
        if self._files is None:
            out: list[tuple[int, str]] = []
            for k, (root, _module) in enumerate(self.scip_roots()):
                stack = [(root, "")]
                while stack:
                    d, prefix = stack.pop()
                    try:
                        it = os.scandir(d)
                    except OSError:
                        continue
                    with it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                stack.append((e.path, prefix + e.name + "/"))
                            elif e.name.endswith(SUFFIX):
                                out.append((k, prefix + e.name))
            out.sort()
            self._files = out
        return self._files

    def _make_shard(self, k: int, rel: str) -> Shard:
        root, module = self.scip_roots()[k]
        return Shard(os.path.join(root, *rel.split("/")), rel[:-len(SUFFIX)], module)

    def source_exists(self, source: str) -> bool:
        return os.path.exists(os.path.join(self.root, source))

    def all_shards(self) -> list[Shard]:
        """Every shard whose source still exists (orphans are skipped)."""
        out = [self._make_shard(k, rel) for k, rel in self.files()]
        return _dedupe([s for s in out if self.source_exists(s.source)])

    def shards_for_source(self, source: str) -> list[Shard]:
        source = source.replace(os.sep, "/").lstrip("/")
        out = []
        for root, module in self.scip_roots():
            path = os.path.join(root, *source.split("/")) + SUFFIX
            if os.path.isfile(path):
                out.append(Shard(path, source, module))
        return out

    def shards_by_basename(self, basenames: set[str]) -> list[Shard]:
        """Shards whose source file name is one of `basenames` (e.g. {"Account.java"}).
        Uses the file listing only, without reading any shard."""
        wanted = {b + SUFFIX for b in basenames}
        out = []
        for k, rel in self.files():
            if rel.rsplit("/", 1)[-1] in wanted:
                s = self._make_shard(k, rel)
                if self.source_exists(s.source):
                    out.append(s)
        return _dedupe(out)

    # --- content prefilter ----------------------------------------------------------------

    def grep(self, needles: Iterable[bytes]) -> list[Shard]:
        """Shards (with an existing source) whose bytes contain any of the fixed `needles`."""
        # grep is line oriented: a needle containing b"\n" would split into an empty pattern
        # that matches everything.
        pats = sorted({n for n in needles if n and b"\n" not in n})
        if not pats:
            return []
        return self._search(fixed=pats)

    def grep_regex(self, regex: bytes, ignore_case: bool = False) -> list[Shard]:
        """Shards whose bytes match `regex`, written in the syntax shared by POSIX ERE, PCRE and
        Python's re: literals, [classes], (a|b) groups, ? and +. Must not contain b"\n"."""
        if b"\n" in regex:
            raise ValueError("regex must not contain a newline byte")
        return self._search(regex=regex, ignore_case=ignore_case)

    def _search(self, fixed: list[bytes] | None = None, regex: bytes | None = None,
                ignore_case: bool = False) -> list[Shard]:
        files = self.files()
        if not files:
            return []
        rel_paths = [self._rel_to_shards_dir(k, rel) for k, rel in files]
        if self._grep:
            hits = self._grep_cmd(rel_paths, fixed, regex, ignore_case)
        else:
            hits = self._grep_python(rel_paths, fixed, regex, ignore_case)
        index = {p: kr for p, kr in zip(rel_paths, files)}
        out = []
        for p in hits:
            kr = index.get(p)
            if kr is None:
                continue
            s = self._make_shard(*kr)
            if self.source_exists(s.source):
                out.append(s)
        return _dedupe(out)

    def _rel_to_shards_dir(self, k: int, rel: str) -> str:
        if self._root_prefixes is None:
            self._root_prefixes = [os.path.relpath(r, self.shards_dir).replace(os.sep, "/") + "/"
                                   for r, _m in self.scip_roots()]
        return self._root_prefixes[k] + rel

    def _grep_cmd(self, rel_paths: list[str], fixed, regex, ignore_case: bool = False
                  ) -> list[str]:
        """Runs grep over the files in parallel chunks (cwd = shards dir, relative paths)."""
        if fixed is not None:
            base: list = [self._grep, "-lFaZ"]
            for p in fixed:
                base += [b"-e", p]
        else:
            mode = "-lPaZ" if _grep_has_pcre(self._grep) else "-lEaZ"
            base = [self._grep, mode] + (["-i"] if ignore_case else []) + [b"-e", regex]
        base.append("--")
        env = dict(os.environ, LC_ALL="C")
        out: list[str] = []
        pending = _chunks(rel_paths)
        running: list[subprocess.Popen] = []

        def drain(proc: subprocess.Popen) -> None:
            stdout, stderr = proc.communicate()
            if proc.returncode not in (0, 1):
                raise WorkspaceError(f"grep failed: {stderr.decode(errors='replace').strip()}")
            out.extend(p.decode("utf-8", errors="surrogateescape")
                       for p in stdout.split(b"\0") if p)

        try:
            while pending or running:
                while pending and len(running) < GREP_JOBS:
                    running.append(subprocess.Popen(base + pending.pop(), cwd=self.shards_dir,
                                                    stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE, env=env))
                drain(running.pop(0))
        finally:
            for proc in running:
                proc.kill()
                proc.wait()
        return out

    def _grep_python(self, rel_paths: list[str], fixed, regex, ignore_case: bool = False
                     ) -> list[str]:
        rx = re.compile(regex, re.IGNORECASE if ignore_case else 0) if regex is not None else None
        out = []
        for rel in rel_paths:
            try:
                with open(os.path.join(self.shards_dir, rel), "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if (rx.search(data) if rx is not None else any(p in data for p in fixed)):
                out.append(rel)
        return out

    # --- freshness --------------------------------------------------------------------------

    def freshness(self) -> Freshness:
        return self.start_freshness().result()

    def start_freshness(self) -> "_FreshnessCheck":
        """Starts the freshness check without blocking: sources edited after their shard was
        written, and .java files that sit in a compiled source directory, have no shard, and
        are newer than every shard of their task (created after its last compilation). A .java
        in a directory that never compiled is not seen. With GNU find the stat work runs in two
        child processes, in parallel with the query; otherwise in a Python thread."""
        return _FreshnessCheck(self)

    def _freshness_from(self, shard_mtimes: dict[str, float], java_mtimes: dict[str, float]
                        ) -> Freshness:
        """shard_mtimes: {path relative to shards dir: mtime}; java_mtimes: {source: mtime}."""
        result = Freshness()
        roots = {os.path.relpath(r, self.shards_dir).replace(os.sep, "/") + "/": k
                 for k, (r, _m) in enumerate(self.scip_roots())}
        newest: dict[int, float] = {}
        shard_of: dict[str, float] = {}
        task_of_source: dict[str, int] = {}
        task_of_dir: dict[str, int] = {}
        for rel, mt in shard_mtimes.items():
            split = split_shard_path(rel)
            if split is None:
                continue
            k = roots.get(split[0])
            if k is None:
                continue
            source = split[1]
            newest[k] = max(newest.get(k, 0.0), mt)
            shard_of[source] = max(shard_of.get(source, 0.0), mt)
            task_of_source[source] = k
            task_of_dir.setdefault(source.rpartition("/")[0], k)
        modules = [m for _r, m in self.scip_roots()]
        for source, mt in java_mtimes.items():
            sm = shard_of.get(source)
            if sm is not None:
                if mt > sm:
                    result.stale.append(source)
                    result.tasks.add(build_task(modules[task_of_source[source]]))
            else:
                k = task_of_dir.get(source.rpartition("/")[0])
                if k is not None and mt > newest.get(k, 0.0):
                    result.uncompiled.append(source)
                    result.tasks.add(build_task(modules[k]))
        result.stale.sort()
        result.uncompiled.sort()
        return result

    def freshness_of(self, sources: Iterable[str]) -> Freshness:
        """Cheap check restricted to these sources: are they newer than their shard?"""
        result = Freshness()
        want = set(sources)
        if not want:
            return result
        for k, rel in self.files():
            source = rel[:-len(SUFFIX)]
            if source not in want:
                continue
            try:
                sm = os.stat(os.path.join(self.scip_roots()[k][0], rel)).st_mtime
                if os.stat(os.path.join(self.root, source)).st_mtime > sm:
                    result.stale.append(source)
                    result.tasks.add(build_task(self.scip_roots()[k][1]))
            except OSError:
                pass
        result.stale = sorted(set(result.stale))
        return result

    def _source_dirs(self) -> list[str]:
        return sorted({rel[:-len(SUFFIX)].rpartition("/")[0] for _k, rel in self.files()})

    def _freshness_python(self) -> Freshness:
        shard_mtimes: dict[str, float] = {}
        for k, rel in self.files():
            path = os.path.join(self.scip_roots()[k][0], rel)
            try:
                shard_mtimes[self._rel_to_shards_dir(k, rel)] = os.stat(path).st_mtime
            except OSError:
                pass
        java: dict[str, float] = {}
        for d in self._source_dirs():
            try:
                entries = list(os.scandir(os.path.join(self.root, d)))
            except OSError:
                continue
            for e in entries:
                if e.name.endswith(".java"):
                    try:
                        if e.is_file():
                            java[f"{d}/{e.name}" if d else e.name] = e.stat().st_mtime
                    except OSError:
                        pass
        return self._freshness_from(shard_mtimes, java)

    # --- sources ----------------------------------------------------------------------------

    def source_lines(self, source: str, lines: Iterable[int]) -> dict[int, str]:
        """{0-based line: stripped text} for the requested lines of a source file."""
        want = set(lines)
        out: dict[int, str] = {}
        if not want:
            return out
        last = max(want)
        try:
            with open(os.path.join(self.root, source), encoding="utf-8", errors="replace") as fh:
                for i, text in enumerate(fh):
                    if i in want:
                        out[i] = text.strip()
                    if i >= last:
                        break
        except OSError:
            pass
        return out


@functools.lru_cache(maxsize=4)
def _grep_has_pcre(grep: str) -> bool:
    """GNU grep is usually built with PCRE (-P); BSD/macOS grep is not. PCRE is faster here."""
    try:
        proc = subprocess.run([grep, "-qP", "a"], input=b"a\n", capture_output=True, timeout=5,
                              env=dict(os.environ, LC_ALL="C"))
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def regex_escape(text: str | bytes) -> bytes:
    """Escapes for the ERE/PCRE/re common subset."""
    b = text.encode() if isinstance(text, str) else text
    out = bytearray()
    for c in b:
        if chr(c) in "\\.[](){}*+?|^$":
            out.append(0x5C)
        out.append(c)
    return bytes(out)


class _FreshnessCheck:
    """Runs GNU find (several processes over chunks of directories, since the cost is one
    stat per file) or, without it, a Python thread."""

    JOBS = 2  # per side; more processes do not help: the cost is one stat per file

    def __init__(self, ws: Workspace):
        self.ws = ws
        self._shard_procs: list[subprocess.Popen] = []
        self._java_procs: list[subprocess.Popen] = []
        self._thread = None
        self._result: Freshness | None = None
        find = _gnu_find()
        if find:
            try:
                shard_dirs = sorted({ws._rel_to_shards_dir(k, rel).rpartition("/")[0]
                                     for k, rel in ws.files()})
                for chunk in _split(shard_dirs, self.JOBS):
                    self._shard_procs.append(self._find(find, chunk, "*" + SUFFIX, ws.shards_dir))
                for chunk in _split([d or "." for d in ws._source_dirs()], self.JOBS):
                    self._java_procs.append(self._find(find, chunk, "*.java", ws.root))
                return
            except OSError:
                for proc in self._shard_procs + self._java_procs:
                    proc.kill()
                self._shard_procs, self._java_procs = [], []
        import threading

        def run():
            self._result = ws._freshness_python()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    @staticmethod
    def _find(find: str, dirs: list[str], pattern: str, cwd: str) -> subprocess.Popen:
        # Output goes to an unlinked temporary file, not a pipe: nobody reads until the query is
        # done, and a full pipe (64 KiB) would stall find for the whole query.
        # With relative start points, %p prints "<dir>/<name>"; find expands \t and \0 itself.
        out = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen([find, *dirs, "-maxdepth", "1", "-name", pattern, "-type", "f",
                                     "-printf", r"%T@\t%p\0"], cwd=cwd, stdout=out,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            out.close()
            raise
        proc.scipq_out = out  # type: ignore[attr-defined]
        return proc

    @staticmethod
    def _collect(proc: subprocess.Popen) -> dict[str, float]:
        proc.wait()
        out = proc.scipq_out  # type: ignore[attr-defined]
        try:
            out.seek(0)
            return _parse_find(out.read())
        finally:
            out.close()

    def result(self) -> Freshness:
        if self._shard_procs or self._java_procs:
            shard_mtimes: dict[str, float] = {}
            java: dict[str, float] = {}
            for proc in self._shard_procs:
                shard_mtimes.update(self._collect(proc))
            for proc in self._java_procs:
                for k, v in self._collect(proc).items():
                    java[k[2:] if k.startswith("./") else k] = v
            return self.ws._freshness_from(shard_mtimes, java)
        if self._thread is not None:
            self._thread.join()
        return self._result or Freshness()


def _split(items: list[str], n: int) -> list[list[str]]:
    if len(items) < 50:
        return [items] if items else []
    return [items[i::n] for i in range(n) if items[i::n]]


def _parse_find(out: bytes) -> dict[str, float]:
    res: dict[str, float] = {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        t, _, path = rec.partition(b"\t")
        try:
            res[path.decode("utf-8", errors="surrogateescape")] = float(t)
        except ValueError:
            pass
    return res


@functools.lru_cache(maxsize=1)
def _gnu_find() -> str | None:
    """GNU find (has -printf); BSD/macOS find does not."""
    find = shutil.which("find")
    if not find:
        return None
    try:
        proc = subprocess.run([find, "--version"], capture_output=True, timeout=5)
        return find if b"GNU findutils" in proc.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


def split_shard_path(rel: str) -> tuple[str, str] | None:
    """("<prefix>META-INF/scip/", source) for a shard path relative to the shards dir, e.g.
    "core/compileJava/META-INF/scip/a/B.java.scip" or "META-INF/scip/a/B.java.scip"."""
    marker = "META-INF/scip/"
    i = 0 if rel.startswith(marker) else rel.find("/" + marker) + 1
    if i < 0 or (i == 0 and not rel.startswith(marker)) or not rel.endswith(SUFFIX):
        return None
    j = i + len(marker)
    return rel[:j], rel[j:-len(SUFFIX)]


def build_task(module: str) -> str:
    """Gradle task path for a "<module>/<task>" shards directory written by the init template
    ("core/compileJava" -> ":core:compileJava", "_root/compileJava" -> ":compileJava");
    other layouts are returned as they are ("" when all shards share one directory)."""
    if module in (".", ""):
        return ""
    mod, _, task = module.rpartition("/")
    if task.startswith("compile") and task.endswith("Java"):
        return f":{task}" if mod in ("", "_root") else f":{mod.replace('/', ':')}:{task}"
    return module


GREP_JOBS = max(1, min(8, os.cpu_count() or 1))
_CHUNK_BYTES = 120_000   # keeps every grep command line far below ARG_MAX (1 MiB on macOS)
_CHUNK_FILES = 400


def _chunks(paths: list[str]) -> list[list[str]]:
    """Interleaved chunks (big directories spread across chunks), bounded in size."""
    n = max(1, min(GREP_JOBS * 4, (len(paths) + _CHUNK_FILES - 1) // _CHUNK_FILES))
    total = sum(len(p) + 1 for p in paths)
    n = max(n, (total + _CHUNK_BYTES - 1) // _CHUNK_BYTES)
    if len(paths) < 200:
        n = 1
    return [paths[i::n] for i in range(n) if paths[i::n]]


def _dedupe(shards: list[Shard]) -> list[Shard]:
    seen: dict[str, Shard] = {}
    for s in shards:
        prev = seen.get(s.source)
        if prev is None:
            seen[s.source] = s
        else:
            # Same source in two tasks: keep the most recently written shard.
            try:
                if os.stat(s.path).st_mtime > os.stat(prev.path).st_mtime:
                    seen[s.source] = s
            except OSError:
                pass
    return sorted(seen.values(), key=lambda s: s.source)

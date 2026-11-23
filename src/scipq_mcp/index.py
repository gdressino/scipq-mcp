"""The auxiliary index (v2): an on-disk SQLite database per checkout, for the queries that must
look at everything or follow call chains (impact, path, dead code, dependencies…).

It is never loaded into memory and it is disposable: missing, it is rebuilt from the shards;
outdated, it is brought up to date incrementally (by shard mtime and size) at query time, only
by the queries that need it. Several server processes may share it: SQLite in WAL mode, and
updates under an exclusive lock file so that only one process reprocesses shards.
"""

from __future__ import annotations

import fcntl
import os
from collections import deque
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from multiprocessing import get_context

from . import symbols as S
from .cachedir import default_base, entry_dir
from .scip import DEFINITION, KIND_NAMES, read_documents
from .workspace import SUFFIX, Workspace

SCHEMA_VERSION = "3"

TABLES = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS shards (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
    module TEXT NOT NULL, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
    orphan INTEGER NOT NULL DEFAULT 0, test INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS symbols (id INTEGER PRIMARY KEY, symbol TEXT UNIQUE NOT NULL,
    pkg TEXT NOT NULL DEFAULT '');                     -- dotted package, for grouping
CREATE TABLE IF NOT EXISTS defs (
    symbol_id INTEGER NOT NULL, shard_id INTEGER NOT NULL,
    line INTEGER NOT NULL, col INTEGER NOT NULL,       -- 0-based; generated: generating node
    body_start INTEGER, body_end INTEGER,              -- enclosing range, 0-based lines
    kind TEXT NOT NULL, flags INTEGER NOT NULL,        -- FLAG_*
    display_name TEXT, signature TEXT,
    PRIMARY KEY (symbol_id, shard_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS refs (
    symbol_id INTEGER NOT NULL, caller_id INTEGER NOT NULL,   -- 0: no enclosing definition
    shard_id INTEGER NOT NULL,
    sites TEXT NOT NULL,                                      -- "line:col[@local],…" 0-based
    PRIMARY KEY (symbol_id, caller_id, shard_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rels (
    target_id INTEGER NOT NULL, symbol_id INTEGER NOT NULL, kind INTEGER NOT NULL,
    shard_id INTEGER NOT NULL,
    PRIMARY KEY (target_id, symbol_id, kind)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id INTEGER NOT NULL, def_id INTEGER NOT NULL, shard_id INTEGER NOT NULL,
    line INTEGER NOT NULL,
    PRIMARY KEY (annotation_id, def_id, line)) WITHOUT ROWID;
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS shards_source ON shards(source);
CREATE INDEX IF NOT EXISTS defs_shard ON defs(shard_id);
CREATE INDEX IF NOT EXISTS refs_caller ON refs(caller_id, symbol_id);
CREATE INDEX IF NOT EXISTS refs_shard ON refs(shard_id);
CREATE INDEX IF NOT EXISTS rels_symbol ON rels(symbol_id);
CREATE INDEX IF NOT EXISTS rels_shard ON rels(shard_id);
CREATE INDEX IF NOT EXISTS annotations_def ON annotations(def_id);
CREATE INDEX IF NOT EXISTS annotations_shard ON annotations(shard_id);
"""

SCHEMA = TABLES + INDEXES

FLAG_GENERATED = 1
FLAG_TEST = 2

REL_IMPLEMENTATION = 1
REL_REFERENCE = 2
REL_TYPE_DEFINITION = 3

EXTERNAL_MODES = ("all", "methods", "none")
PARALLEL_FROM = 200  # shards to reprocess before using worker processes

_DECL = re.compile(r"\b(record|class|interface|enum)\s+")
_GENERATED = ("@Generated", "@lombok.Generated", "@javax.annotation.Generated",
              "@javax.annotation.processing.Generated")
_TEST_TASK = re.compile(r"test", re.IGNORECASE)
_TEST_PATH = re.compile(r"(^|/)src/[A-Za-z]*[tT]est[A-Za-z]*/")
_ANNOTATION_BEFORE = re.compile(r"@\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)*$")


class IndexError_(Exception):  # noqa: N801 - "IndexError" is a builtin
    pass


@dataclass
class UpdateStats:
    shards: int = 0          # on disk
    processed: int = 0       # (re)decoded in this update
    removed: int = 0
    seconds: float = 0.0
    built_from_scratch: bool = False
    unreadable: int = 0      # shards cut short (being written), retried next time


def default_workers() -> int:
    """Conservative: other work (builds, tests in other worktrees) shares the machine."""
    return max(1, min(4, (os.cpu_count() or 1) // 4))


NICE = 10


def _current_nice() -> int:
    try:
        return os.getpriority(os.PRIO_PROCESS, 0)
    except (OSError, AttributeError):
        return 0


def lower_own_priority() -> None:
    """The whole process yields to builds and tests: CPU nice NICE and, on Linux, best-effort
    I/O at the lowest level. Called once at startup, before any thread or child process exists:
    they inherit it (decoding, the freshness check and library indexing run in this process)."""
    cur = _current_nice()
    if cur < NICE:
        try:
            os.nice(NICE - cur)
        except OSError:
            pass
    ionice = shutil.which("ionice") if sys.platform.startswith("linux") else None
    if ionice:
        try:
            subprocess.run([ionice, "-c2", "-n7", "-p", str(os.getpid())], timeout=5,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def low_priority(cmd: list) -> list:
    """`cmd` at nice NICE (no further when this process already runs there) and, on Linux,
    under `ionice -c2 -n7` (best effort, lowest), when the tools exist: background work must
    not compete with builds and tests."""
    out = list(cmd)
    ionice = shutil.which("ionice")
    if ionice and sys.platform.startswith("linux"):
        out = [ionice, "-c2", "-n7"] + out
    nice = shutil.which("nice")
    step = NICE - _current_nice()
    if nice and step > 0:
        out = [nice, "-n", str(step)] + out
    return out


def is_test_shard(module: str, source: str) -> bool:
    task = module.rpartition("/")[2]
    if task in ("", "."):
        return bool(_TEST_PATH.search(source))
    return bool(_TEST_TASK.search(task))


# --- per-shard extraction (runs in worker processes for big updates) --------------------------

def extract(path: str, source: str, root: str) -> dict:
    """Rows of one shard, with symbols as strings: {defs, refs, rels, anns}."""
    out = {"defs": [], "refs": [], "rels": [], "anns": []}
    try:
        docs = read_documents(path)
    except (OSError, ValueError, IndexError):
        out["error"] = True  # cut short (being written): retried on the next update
        return out
    lines_cache: list[str] | None = None

    def lines() -> list[str]:
        nonlocal lines_cache
        if lines_cache is None:
            try:
                with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
                    lines_cache = fh.read().split("\n")
            except OSError:
                lines_cache = []
        return lines_cache

    for doc in docs:
        occs = doc.occurrences
        infos = {s.symbol: s for s in doc.symbols}
        containers = []
        defs_by_symbol = {}
        for o in occs:
            if not o.roles & DEFINITION or not o.symbol:
                continue
            gen = o.enclosing is not None and not o.enclosing.contains(o.range)
            name_pos = (o.range.start_line, o.range.start_char)
            if S.is_type(o.symbol) and o.enclosing is not None:
                decl = declaration_line(lines(), o.enclosing.start_line, S.name_of(o.symbol))
                if decl is not None and decl != o.range.start_line:
                    name_pos = (decl, 0)
            if o.enclosing is not None:
                e = o.enclosing
                containers.append((e.start_line, e.start_char, e.end_line, e.end_char, o.symbol,
                                   S.is_local_like(o.symbol), gen, name_pos))
            if S.is_local_like(o.symbol) or o.symbol in defs_by_symbol:
                continue
            info = infos.get(o.symbol)
            sig = info.signature if info else ""
            generated = gen or any(m in sig for m in _GENERATED)
            loc = o.enclosing if gen else o.range
            if S.is_type(o.symbol) and o.enclosing is not None:
                decl = declaration_line(lines(), o.enclosing.start_line, S.name_of(o.symbol))
                if decl is not None and decl != o.range.start_line:
                    from .scip import Range as _R
                    loc = _R(decl, 0, decl, 0)
            kind = (KIND_NAMES.get(info.kind, "") if info else "") or S.kind_of(o.symbol)
            if S.is_type(o.symbol) and info is not None and not re.search(
                    r"\b(class|interface|enum|record|@interface)\b", sig):
                text = lines()[o.range.start_line] if o.range.start_line < len(lines()) else ""
                m = _DECL.search(text)
                if m:
                    kind, sig = m.group(1), text.strip().rstrip("{").strip()
            body = None if gen or o.enclosing is None else (o.enclosing.start_line,
                                                            o.enclosing.end_line)
            defs_by_symbol[o.symbol] = (
                o.symbol, loc.start_line, loc.start_char, body[0] if body else None,
                body[1] if body else None, kind, FLAG_GENERATED if generated else 0,
                info.display_name if info else "", sig)
            if info is not None:
                for r in info.relationships:
                    k = (REL_IMPLEMENTATION if r.is_implementation else
                         REL_TYPE_DEFINITION if r.is_type_definition else
                         REL_REFERENCE if r.is_reference else 0)
                    if k and r.symbol:
                        out["rels"].append((r.symbol, o.symbol, k))
        out["defs"].extend(defs_by_symbol.values())

        # Attribute each reference to its innermost enclosing non-local definition: one sweep
        # over the (properly nested) enclosing ranges, in start order.
        containers.sort(key=lambda c: (c[0], c[1], -c[2], -c[3]))
        refs = [o for o in occs if not o.roles & DEFINITION and o.symbol
                and not S.is_local(o.symbol)]
        refs.sort(key=lambda o: (o.range.start_line, o.range.start_char))
        stack: list[tuple] = []
        ci = 0
        agg: dict[tuple[str, str], list[str]] = {}
        for o in refs:
            r = o.range
            rs, re_ = (r.start_line, r.start_char), (r.end_line, r.end_char)
            while ci < len(containers) and (containers[ci][0], containers[ci][1]) <= rs:
                c = containers[ci]
                ci += 1
                while stack and not _contains(stack[-1], (c[0], c[1]), (c[2], c[3])):
                    stack.pop()
                stack.append(c)
            while stack and not _contains(stack[-1], rs, re_):
                stack.pop()
            if stack and stack[-1][6]:
                continue  # inside generated code (e.g. a Lombok getter body)
            caller, via = "", ""
            for c in reversed(stack):
                if not c[5]:
                    caller = c[4]
                    break
                if not via:
                    info = infos.get(c[4])
                    if info and info.kind_name in ("method", "constructor", "abstract method"):
                        via = info.display_name
            site = f"{r.start_line}:{r.start_char}" + (f"@{via}" if via else "")
            agg.setdefault((o.symbol, caller), []).append(site)
            if o.symbol.endswith("#"):
                ls = lines()
                text = ls[r.start_line] if r.start_line < len(ls) else ""
                if _is_annotation(text, r.start_char):
                    target = None
                    for c in reversed(stack):
                        if c[7] > rs:
                            target = c
                            break
                    if target is not None and not target[5]:
                        out["anns"].append((o.symbol, target[4], r.start_line))
        out["refs"].extend((s, c, ",".join(v)) for (s, c), v in agg.items())
    return out


_DECLARATION = r"\b(class|interface|enum|record|@\s*interface)\s+{name}\b"


def declaration_line(lines: list[str], start: int, name: str, span: int = 80) -> int | None:
    """0-based line of `class|interface|enum|record <name>` from `start`: scip-javac sometimes
    reports a type at the first textual occurrence of its name (e.g. inside an annotation
    string above the declaration)."""
    rx = re.compile(_DECLARATION.format(name=re.escape(name)))
    for k in range(start, min(start + span, len(lines))):
        if rx.search(lines[k]):
            return k
    return None


def _contains(c, start, end) -> bool:
    return (c[0], c[1]) <= start and end <= (c[2], c[3])


def _is_annotation(line: str, col: int) -> bool:
    before = line[:col]
    if not _ANNOTATION_BEFORE.search(before):
        return False
    if before.lstrip().startswith(("*", "/*", "//")) or "//" in before or "/*" in before:
        return False
    return not re.search(r"\{\s*@(code|link|linkplain|literal)\b[^}]*$", before)


def _extract_job(args):
    return args[0], extract(*args[1:])


# --- the database -----------------------------------------------------------------------------

class Index:
    def __init__(self, ws: Workspace, cache_base: str | None = None, external: str = "all",
                 workers: int | None = None):
        if external not in EXTERNAL_MODES:
            raise ValueError(f"external must be one of {EXTERNAL_MODES}")
        self.workers = workers or default_workers()
        self.unreadable = 0
        self.ws = ws
        self.dir_base = cache_base or default_base()
        self.dir = entry_dir(ws.root, self.dir_base)
        self.path = os.path.join(self.dir, "index.sqlite")
        self.external = external

    # connection ------------------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA temp_store=MEMORY")
        con.executescript(TABLES)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        real = os.path.realpath(self.ws.root)
        if meta and (meta.get("schema_version") != SCHEMA_VERSION
                     or meta.get("root_realpath") != real
                     or meta.get("external", "all") != self.external):
            con.close()
            self._discard()
            return self.connect()
        if not meta:
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
                ("schema_version", SCHEMA_VERSION), ("root_realpath", real),
                ("shards_dir", self.ws.shards_dir), ("external", self.external),
                ("created_at", str(int(time.time())))])
            con.commit()
        else:
            con.executescript(INDEXES)
        return con

    def _discard(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    @contextmanager
    def _lock(self):
        with open(os.path.join(self.dir, "update.lock"), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # update ----------------------------------------------------------------------------------

    def pending(self) -> int:
        """Shards to (re)process or remove, without taking the lock (a hint, for deciding
        where to run the update)."""
        if not os.path.exists(self.path):
            return len(self.ws.files())
        try:
            con = sqlite3.connect(self.path, timeout=30)
            known = {p: (mt, size) for p, mt, size in con.execute(
                "SELECT path, mtime_ns, size FROM shards")}
            con.close()
        except sqlite3.DatabaseError:
            return len(self.ws.files())
        n = 0
        seen = set()
        for k, rel in self.ws.files():
            path = os.path.join(self.ws.scip_roots()[k][0], rel)
            seen.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if known.get(path) != (st.st_mtime_ns, st.st_size):
                n += 1
        return n + sum(1 for p in known if p not in seen)

    def ensure(self) -> UpdateStats:
        """update(), in a child process when there is a lot to do: a big build peaks at
        ~150 MB, which a long-lived server process would otherwise keep."""
        if self.pending() < PARALLEL_FROM:
            return self.update()
        t0 = time.perf_counter()
        cmd = low_priority([sys.executable, "-m", "scipq_mcp.index", self.ws.root,
                            self.dir_base, self.external,
                            os.path.relpath(self.ws.shards_dir, self.ws.root),
                            str(self.workers)])
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise IndexError_("index build failed: " + (proc.stderr.strip().splitlines()
                                                          or ["?"])[-1])
        stats = UpdateStats(built_from_scratch=True, seconds=time.perf_counter() - t0)
        try:
            vals = dict(x.split("=", 1) for x in proc.stdout.split())
            stats.shards, stats.processed = int(vals["shards"]), int(vals["processed"])
            stats.removed = int(vals["removed"])
            stats.built_from_scratch = vals["scratch"] == "1"
            stats.unreadable = int(vals.get("unreadable", 0))
        except (KeyError, ValueError):
            pass
        return stats

    def update(self, progress=None) -> UpdateStats:
        """Brings the index up to date with the shards on disk."""
        t0 = time.perf_counter()
        stats = UpdateStats()
        with self._lock():
            con = self.connect()
            try:
                self._update(con, stats, progress)
                con.execute("INSERT OR REPLACE INTO meta VALUES ('last_used_at', ?)",
                            (str(int(time.time())),))
                con.commit()
            finally:
                con.close()
        stats.seconds = time.perf_counter() - t0
        return stats

    def _update(self, con: sqlite3.Connection, stats: UpdateStats, progress) -> None:
        ws = self.ws
        known = {path: (sid, mt, size, orphan) for sid, path, mt, size, orphan in con.execute(
            "SELECT id, path, mtime_ns, size, orphan FROM shards")}
        stats.built_from_scratch = not known
        on_disk = {}
        for k, rel in ws.files():
            path = os.path.join(ws.scip_roots()[k][0], rel)
            try:
                st = os.stat(path)
            except OSError:
                continue
            source = rel[:-len(SUFFIX)]
            on_disk[path] = (source, ws.scip_roots()[k][1], st.st_mtime_ns, st.st_size,
                             0 if ws.source_exists(source) else 1)
        stats.shards = len(on_disk)
        gone = [sid for path, (sid, *_r) in known.items() if path not in on_disk]
        todo = []
        for path, (source, module, mt, size, orphan) in on_disk.items():
            old = known.get(path)
            if old is None or old[1:] != (mt, size, orphan):
                todo.append((path, source, module, mt, size, orphan))
        for sid in gone:
            self._delete_shard_rows(con, sid)
            con.execute("DELETE FROM shards WHERE id = ?", (sid,))
        stats.removed = len(gone)
        if not todo:
            return
        ids: dict[str, int] = {}
        if stats.built_from_scratch:
            ids = dict(con.execute("SELECT symbol, id FROM symbols"))
            for line in INDEXES.strip().splitlines():  # faster bulk insert; recreated below
                con.execute("DROP INDEX IF EXISTS " + line.split()[5])
        jobs = []
        for path, source, module, mt, size, orphan in todo:
            old = known.get(path)
            if old is not None:
                self._delete_shard_rows(con, old[0])
                con.execute("UPDATE shards SET source=?, module=?, mtime_ns=?, size=?, orphan=?, "
                            "test=? WHERE id=?", (source, module, mt, size, orphan,
                                                  int(is_test_shard(module, source)), old[0]))
                sid = old[0]
            else:
                sid = con.execute("INSERT INTO shards (path, source, module, mtime_ns, size, "
                                  "orphan, test) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (path, source, module, mt, size, orphan,
                                   int(is_test_shard(module, source)))).lastrowid
            if not orphan:
                jobs.append((sid, path, source, ws.root))
        stats.processed = len(jobs)
        self.unreadable = 0
        for n, (sid, rows) in enumerate(self._extract_all(jobs), 1):
            self._insert(con, sid, rows, ids)
            if progress and n % 500 == 0:
                progress(n, len(jobs))
        if self.external != "all":
            self._drop_external(con)
        stats.unreadable = self.unreadable
        if stats.built_from_scratch:
            con.executescript(INDEXES)
            if self.external != "all":
                con.commit()
                con.execute("VACUUM")

    def _extract_all(self, jobs):
        if len(jobs) < PARALLEL_FROM or (os.cpu_count() or 1) < 2:
            for job in jobs:
                yield _extract_job(job)
            return
        workers = max(1, self.workers)
        window = workers * 4  # results in flight: keeps memory bounded when inserts lag
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as ex:
            pending = deque()
            it = iter(jobs)
            for job in it:
                pending.append(ex.submit(_extract_job, job))
                if len(pending) >= window:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()

    @staticmethod
    def _delete_shard_rows(con: sqlite3.Connection, sid: int) -> None:
        for table in ("defs", "refs", "rels", "annotations"):
            con.execute(f"DELETE FROM {table} WHERE shard_id = ?", (sid,))

    @staticmethod
    def _ids(con: sqlite3.Connection, ids: dict[str, int], syms: set[str]) -> None:
        missing = [s for s in syms if s not in ids]
        if not missing:
            return
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            q = ",".join("?" * len(chunk))
            for sym, sid in con.execute(f"SELECT symbol, id FROM symbols WHERE symbol IN ({q})",
                                        chunk):
                ids[sym] = sid
        new = [s for s in missing if s not in ids]
        if new:
            con.executemany("INSERT OR IGNORE INTO symbols (symbol, pkg) VALUES (?, ?)",
                            [(s, S.package_of(s)) for s in new])
            for i in range(0, len(new), 500):
                chunk = new[i:i + 500]
                q = ",".join("?" * len(chunk))
                for sym, sid in con.execute(
                        f"SELECT symbol, id FROM symbols WHERE symbol IN ({q})", chunk):
                    ids[sym] = sid

    def _insert(self, con: sqlite3.Connection, sid: int, rows: dict, ids: dict[str, int]) -> None:
        if rows.get("error"):
            con.execute("UPDATE shards SET size = -1 WHERE id = ?", (sid,))  # force a retry
            self.unreadable += 1
            return
        syms = {d[0] for d in rows["defs"]}
        syms |= {r[0] for r in rows["refs"]} | {r[1] for r in rows["refs"] if r[1]}
        syms |= {r[0] for r in rows["rels"]} | {r[1] for r in rows["rels"]}
        syms |= {a[0] for a in rows["anns"]} | {a[1] for a in rows["anns"]}
        self._ids(con, ids, syms)
        con.executemany("INSERT OR REPLACE INTO defs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(ids[d[0]], sid, *d[1:]) for d in rows["defs"]])
        con.executemany("INSERT OR REPLACE INTO refs VALUES (?,?,?,?)",
                        [(ids[s], ids[c] if c else 0, sid, sites) for s, c, sites in rows["refs"]])
        con.executemany("INSERT OR IGNORE INTO rels VALUES (?,?,?,?)",
                        [(ids[t], ids[s], k, sid) for t, s, k in rows["rels"]])
        con.executemany("INSERT OR IGNORE INTO annotations VALUES (?,?,?,?)",
                        [(ids[a], ids[d], sid, line) for a, d, line in rows["anns"]])

    def _drop_external(self, con: sqlite3.Connection) -> None:
        """index.external=methods|none: drop references to symbols the project does not define
        (all of them, or all but methods)."""
        cond = "" if self.external == "none" else " AND symbol NOT LIKE '%).'"
        con.execute("DROP TABLE IF EXISTS temp.ext")
        con.execute(f"""CREATE TEMP TABLE ext AS SELECT id FROM symbols
                        WHERE id NOT IN (SELECT symbol_id FROM defs){cond}""")
        con.execute("DELETE FROM refs WHERE symbol_id IN (SELECT id FROM temp.ext)")
        con.execute("DROP TABLE temp.ext")

    # info ------------------------------------------------------------------------------------

    def status(self) -> dict:
        out = {"path": self.path, "exists": os.path.exists(self.path)}
        if not out["exists"]:
            return out
        con = sqlite3.connect(self.path, timeout=30)
        try:
            meta = dict(con.execute("SELECT key, value FROM meta"))
            out.update(meta)
            for table in ("shards", "symbols", "defs", "refs", "rels", "annotations"):
                out[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.DatabaseError:
            pass
        finally:
            con.close()
        out["bytes"] = sum(os.path.getsize(self.path + s) for s in ("", "-wal", "-shm")
                           if os.path.exists(self.path + s))
        return out


def _main(argv: list[str]) -> int:
    """`python -m scipq_mcp.index ROOT CACHE_BASE EXTERNAL SHARDS_DIR [WORKERS]`: run one update
    (used by Index.ensure for big builds, so the server process stays small)."""
    root, base, external, shards_dir = argv[:4]
    workers = int(argv[4]) if len(argv) > 4 else None
    ix = Index(Workspace(root, shards_dir), base, external, workers)
    st = ix.update()
    print(f"shards={st.shards} processed={st.processed} removed={st.removed} "
          f"scratch={int(st.built_from_scratch)} unreadable={st.unreadable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

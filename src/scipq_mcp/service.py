"""The operations exposed as MCP tools (and by the debug CLI), returning compact text.

Each call opens the project afresh: shards are listed, prefiltered and decoded for that call
only. The freshness check (sources newer than their shard) runs in parallel with the query,
and its warning is appended to every answer.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, TypeVar

from . import extras as X
from . import graph as G
from . import render
from . import setup as SETUP
from . import symbols as S
from . import config as CFG
from . import compare as CMP
from . import cucumber as CU
from . import jvm as JVM
from . import refactor as R
from . import context as C
from . import libsrc as L
from .cachedir import CacheError, default_base, entry_name
from .graph import Graph, GraphError
from .index import SCHEMA_VERSION, Index, IndexError_, unchanged_sources
from .queries import AmbiguousName, Navigator, QueryError
from .workspace import Freshness, Workspace, WorkspaceError, build_running, find_project_root

DEFAULT_LIMIT = 100
ENTRY_DEPTH = 6     # impact searches entry points (screens, jobs…) at least this deep
FRESHNESS_MODES = ("full", "results", "off")
JDK_CHOICE_SECONDS = 60  # how long the JDK picked for a checkout (from its build files) is kept
LIBRARY_FAMILY_TYPES = 300  # library types with a same-named method checked for a family
T = TypeVar("T")
Body = Callable[[Navigator], "tuple[str, set[str]]"]

NO_SHARDS_HINT = ("This project has not been compiled with the scip-javac plugin yet, so there "
                  "is nothing to navigate. Call the `setup` tool: it inspects the build and "
                  "returns the exact steps (scipq-mcp never changes the build or runs it).")


def _summary_of_changed(text: str) -> str:
    """`changed` without its list of definitions: the counts and what to run."""
    lines = text.split("\n")
    keep = [ln for ln in lines if ln.startswith(("changed since", "callers of"))]
    start = next((k for k, ln in enumerate(lines) if ln.startswith(
        ("tests to run", "cucumber", "no test"))), None)
    return "\n".join(keep + (lines[start:] if start is not None else []))


class OverloadLabels:
    """For S.short while a request runs: a method that takes parameters is written with them
    (`Service#process(String)`) instead of SCIP's numbering, since a name reads `process()` as
    the method without parameters, as Java does. The signatures come from the index (when it
    exists already), the file of the method's top-level type, or a library's sources jar; a
    few hundred methods at most per request."""

    MAX_GROUPS = 400
    MAX_SECONDS = 3.0   # spent looking labels up, per request: then the plain names

    def __init__(self, svc: "Service", nav: Navigator):
        self.svc, self.nav = svc, nav
        self.groups: dict[tuple[str, str], dict[str, str]] = {}
        self.files: dict[str, dict[tuple[str, str], list]] = {}
        self._con: sqlite3.Connection | None | bool = None   # False: no usable index
        self._spent = 0.0
        self._shard_ok: dict[str, bool] = {}

    def _index(self) -> sqlite3.Connection | None:
        """The auxiliary index, read-only and only if it exists already (a label never builds
        it): its defs answer in a fraction of a millisecond what decoding a shard takes."""
        if self._con is None:
            self._con = False
            try:
                ix = Index(self.nav.ws, self.svc.cache_dir, self.svc.index_external)
                if os.path.exists(ix.path):
                    con = sqlite3.connect(f"file:{ix.path}?mode=ro", uri=True, timeout=5)
                    meta = dict(con.execute("SELECT key, value FROM meta"))
                    if meta.get("schema_version") == SCHEMA_VERSION and \
                            meta.get("root_realpath") == os.path.realpath(self.nav.ws.root):
                        self._con = con
                    else:
                        con.close()
            except (sqlite3.Error, OSError, ValueError, CacheError):
                self._con = False
        return self._con or None

    def close(self) -> None:
        if self._con:
            self._con.close()
        self._con = False

    def __call__(self, symbol: str) -> str | None:
        if S.is_local(symbol) or not S.is_method(symbol):
            return None
        key = (S.parent(symbol), S.name_of(symbol))
        group = self.groups.get(key)
        if group is None:
            if len(self.groups) >= self.MAX_GROUPS or self._spent >= self.MAX_SECONDS:
                return None
            self.groups[key] = {}   # while computing: S.short inside prints the plain name
            t0 = time.perf_counter()
            try:
                group = self._group(symbol, key)
            except Exception:   # a label is never worth an error
                group = {}
            self._spent += time.perf_counter() - t0
            self.groups[key] = group
        return group.get(symbol)

    def _fresh(self, path: str, mtime_ns: int, size: int) -> bool:
        """The shard is as the index read it (else its overloads may be numbered otherwise)."""
        if path not in self._shard_ok:
            try:
                st = os.stat(path)
                self._shard_ok[path] = st.st_mtime_ns == mtime_ns and st.st_size == size
            except OSError:
                self._shard_ok[path] = False
        return self._shard_ok[path]

    def _group(self, symbol: str, key: tuple[str, str]) -> dict[str, str]:
        top = S.top_level_type_name(symbol)
        if not top:
            return {}
        con = self._index()
        if con is not None:
            owner, name = key
            lo = owner + S._format_name(name) + "("
            rows = con.execute(
                "SELECT s.symbol, d.signature, sh.path, sh.mtime_ns, sh.size FROM symbols s "
                "JOIN defs d ON d.symbol_id = s.id JOIN shards sh ON sh.id = d.shard_id "
                "WHERE s.symbol >= ? AND s.symbol < ?", (lo, lo[:-1] + ")")).fetchall()
            rows = [r for r in rows if S.is_method(r[0]) and S.parent(r[0]) == owner
                    and S.name_of(r[0]) == name]
            if all(self._fresh(path, mt, size) for _s, _g, path, mt, size in rows):
                if rows:
                    return _labels({sym: R._param_types(sig) for sym, sig, *_r in rows if sig})
                if con.execute("SELECT 1 FROM symbols s JOIN defs d ON d.symbol_id = s.id "
                               "WHERE s.symbol = ? LIMIT 1", (owner,)).fetchone():
                    return {}   # a project type: the method is inherited or implicit
                return self._library_group(symbol)
            # The file was compiled again since the index read it: from its shard below.
        if top not in self.files:
            methods: dict[tuple[str, str], list] = {}
            for v in self.nav._views(self.nav.ws.shards_by_basename({top + ".java"})):
                for d in v.definitions(lambda sym, _i: S.is_method(sym)
                                       and not S.is_local_like(sym)):
                    methods.setdefault((S.parent(d.symbol), S.name_of(d.symbol)), []).append(d)
            self.files[top] = methods
        found = {d.symbol: d for d in self.files[top].get(key, [])}
        if found:
            return _labels({sym: R._param_types(d.info.signature)
                            for sym, d in found.items() if d.info and d.info.signature})
        if any(k[0] == key[0] for k in self.files[top]):
            return {}   # a project type: the method is inherited or implicit
        return self._library_group(symbol)

    def _library_group(self, symbol: str) -> dict[str, str]:
        lib = self.svc._library(symbol, self.nav.ws.root, wait=False)   # a label never waits
        if lib is None or lib.member is None or not lib.overloads:
            return {}
        return _labels({S.overload_symbol(symbol, k): R._param_types(m.declaration)
                        for k, m in enumerate(lib.overloads)})


def _labels(params: dict[str, tuple]) -> dict[str, str]:
    """The parameter lists a method's overloads are written with: every one that takes
    parameters, and, among several, the one without too (`m()` alone is SCIP's numbering)."""
    if len(params) < 2:
        return {sym: ", ".join(p) for sym, p in params.items() if p}
    return {sym: ", ".join(p) for sym, p in params.items()}


class Service:
    def __init__(self, default_root: str | None = None, shards_dir: str | None = None,
                 freshness: str = "full", cache_dir: str | None = None,
                 index_external: str = "all", config_file: str | None = None,
                 library_roots: list[str] | None = None, build_workers: int | None = None):
        """freshness: "full" (every shard against its source, ~0.1 s on 10k files, run in
        parallel with the query), "results" (only the files in the answer) or "off"."""
        if freshness not in FRESHNESS_MODES:
            raise ValueError(f"freshness must be one of {FRESHNESS_MODES}")
        self.default_root = default_root
        self.shards_dir = shards_dir
        self.freshness = freshness
        self.cache_dir = cache_dir
        self.index_external = index_external
        self.config_file = config_file
        self.library_roots = library_roots
        self.build_workers = build_workers
        self._lib_indexes: dict[tuple, L.LibraryIndex] = {}
        self._jdk_choices: dict[str, tuple[float, str, str]] = {}
        self._lib_lock = threading.Lock()

    def settings(self, root: str, graph=None) -> CFG.Settings:
        present = graph.annotations_present() if graph is not None else None
        try:
            return CFG.settings_for(root, self.config_file, present_annotations=present)
        except CFG.ConfigError as e:
            st = CFG.Settings()
            st.warnings.append(str(e))
            return st

    # --- plumbing ---------------------------------------------------------------------------

    def _root(self, project_root: str | None) -> str:
        root = project_root or self.default_root or os.environ.get("SCIPQ_PROJECT_ROOT") \
            or os.getcwd()
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            raise WorkspaceError(f"project_root {root!r} is not a directory")
        return find_project_root(root)

    def workspace(self, project_root: str | None) -> Workspace:
        ws = Workspace(self._root(project_root), self.shards_dir)
        # The auxiliary index keeps the hash of each source as compiled: a file written again
        # unchanged (Gradle up to date by content) is not reported stale. Read-only: nothing
        # is created when there is no index yet.
        path = os.path.join(self.cache_dir or default_base(), entry_name(ws.root),
                            "index.sqlite")
        ws.content_check = lambda sources: unchanged_sources(path, ws.root, sources)
        return ws

    def _run(self, project_root: str | None, *bodies: Body, foot_first: bool = False) -> str:
        try:
            ws = self.workspace(project_root)
        except WorkspaceError as e:
            msg = str(e)
            return f"error: {msg}" + (f"\n{NO_SHARDS_HINT}" if "no SCIP shards" in msg else "")
        ws.files()
        check = ws.start_freshness() if self.freshness == "full" else None
        nav = Navigator(ws)
        nav.library_overloads = lambda sym, qualified=False: \
            self._library_overload_params(sym, ws.root, qualified)
        labeler = OverloadLabels(self, nav)
        labels = S.METHOD_LABELS.set(labeler)
        try:
            return self._run_bodies(nav, ws, check, bodies, foot_first)
        finally:
            S.METHOD_LABELS.reset(labels)
            labeler.close()

    def _run_bodies(self, nav: Navigator, ws: Workspace, check, bodies, foot_first: bool) -> str:
        texts, sources = [], set()
        for body in bodies:
            try:
                text, srcs = body(nav)
            except AmbiguousName as e:
                text, srcs = render.candidates(e.resolution), set()
            except (QueryError, WorkspaceError, ValueError, L.LibraryBusy) as e:
                text, srcs = f"error: {e}", set()
            except sqlite3.Error as e:  # e.g. a cache database removed or locked meanwhile
                text, srcs = f"error: cache database: {e} (retry; `scipq-mcp cache clear` " \
                             "rebuilds it)", set()
            texts.append(text)
            sources |= srcs
        fresh: Freshness | None = None
        if check is not None:
            fresh = check.result()
        elif self.freshness == "results":
            fresh = ws.freshness_of(sources)
        foot = render.freshness(fresh, sources)
        if nav.unreadable:
            foot = (foot + "\n" if foot else "") + render.unreadable(sorted(nav.unreadable))
        text = texts[0] if len(texts) == 1 else "\n\n".join(texts)
        if foot and foot_first:  # they condition everything below (context)
            return foot + "\n" + text
        return text + ("\n" + foot if foot else "")

    @staticmethod
    def _resolve(nav: Navigator, name: str) -> tuple[str, str]:
        """(symbol, note line or "")."""
        r = nav.resolve(name)
        if r.symbol is None:
            raise AmbiguousName(r)
        return r.symbol, ("" if S.looks_like_symbol(name) else f"-- {r.note}\n")

    @staticmethod
    def _resolved(nav: Navigator, name: str, query: Callable[[str], T]) -> tuple[str, str, T]:
        """Resolves `name` and runs query(symbol). When the name can be guessed from a file
        name, the query runs on the guess while the complete resolution confirms it in a
        thread; if they disagree the guess is thrown away. Returns (symbol, note, result)."""
        if S.looks_like_symbol(name):
            return name, "", query(name)
        guess = nav.guess(name)
        if guess is None:
            sym, note = Service._resolve(nav, name)
            return sym, note, query(sym)
        box: list = []

        def resolve() -> None:
            try:
                box.append(nav.resolve(name))
            except BaseException as e:  # re-raised below, in the caller's thread
                box.append(e)

        t = threading.Thread(target=resolve, daemon=True)
        t.start()
        try:
            result = query(guess)
        except QueryError:
            result = None
        t.join()
        r = box[0]
        if isinstance(r, BaseException):
            raise r
        if r.symbol is None:
            raise AmbiguousName(r)
        if r.symbol != guess or result is None:
            result = query(r.symbol)
        return r.symbol, f"-- {r.note}\n", result

    # --- bodies (shared by the tools and batch) ---------------------------------------------

    def _b_find(self, name: str, offset: int = 0, limit: int = 50, match: str = "auto") -> Body:
        def body(nav: Navigator):
            how = ""
            if match in ("auto", "exact"):
                found = nav.find(name)
                if not found and match == "auto":
                    found = [d for d in nav.find_fuzzy(name, "contains")
                             if not S.is_local_like(d.symbol)]
                    how = "no exact name; names containing it, ignoring case" if found else ""
            elif match in S.MATCH_MODES:
                found = [d for d in nav.find_fuzzy(name, match) if not S.is_local_like(d.symbol)]
                how = f"match={match}, ignoring case"
            else:
                raise ValueError(f"match must be auto or one of {S.MATCH_MODES}")
            return (render.find(name, found, offset, limit, how),
                    {d.location.source for d in found[offset:offset + limit]})
        return body

    def _b_references(self, symbol: str, offset: int = 0, limit: int = DEFAULT_LIMIT,
                      include_definitions: bool = False, exclude_imports: bool | None = None,
                      by_name: bool = False, by_name_globs: list[str] | None = None,
                      scope: str = "") -> Body:
        def body(nav: Navigator):
            sym, note, (refs, defs) = self._resolved(nav, symbol, nav.references)
            accessor = X.record_accessor(nav, sym, defs)
            if accessor:
                more, _ = nav.references(accessor)
                refs = sorted(refs + more, key=lambda r: (r.location.source,
                                                          r.location.range.start_line,
                                                          r.location.range.start_char))
                note += f"-- record component: includes the uses of its accessor {S.short(accessor)}\n"
            drop_imports = S.is_type(sym) if exclude_imports is None else exclude_imports
            if drop_imports:  # by default for types: import lines are noise
                refs = _drop_imports(nav, refs)
            if scope:
                refs = [r for r in refs if X.in_scope(r.container, r.location.source, scope)]
            text = render.references(nav.ws, sym, refs, defs, offset, limit, include_definitions)
            if scope:
                text = text.replace(" — ", f" — in {scope}: ", 1)
            if by_name:
                names = X.property_names(sym)
                exclude = {(r.location.source, r.location.line) for r in refs}
                exclude |= {(d.location.source, d.location.line) for d in defs}
                globs = by_name_globs or self.settings(nav.ws.root).by_name_globs
                hits = X.uses_by_name(nav.ws.root, names, globs, exclude)
                text += "\n" + render.by_name(hits, names)
                mentions = X.javadoc_mentions(nav.ws.root, sym)
                text += "\n" + render.javadoc_mentions(mentions, sym)
            return note + text, {r.location.source for r in refs}
        return body

    def _jdk_choice(self, root: str) -> tuple[str, str]:
        """(JDK src.zip for this checkout, note when it may not match the build), rechecked
        every JDK_CHOICE_SECONDS."""
        if self.library_roots is not None:
            return "", ""
        now = time.monotonic()
        with self._lib_lock:
            hit = self._jdk_choices.get(root)
        if hit is not None and now - hit[0] < JDK_CHOICE_SECONDS:
            return hit[1], hit[2]
        zip_path, note = L.jdk_for_project(root)
        with self._lib_lock:
            self._jdk_choices[root] = (now, zip_path, note)
        return zip_path, note

    def _library_index(self, root: str):
        """One per JDK, shared by the checkouts that use it and kept for the life of the
        service: it holds open jars and parsed sources (reopening the JDK's src.zip costs
        ~50 ms)."""
        zip_path, _note = self._jdk_choice(root)
        key = (zip_path, tuple(self.library_roots or ()))
        with self._lib_lock:
            ix = self._lib_indexes.get(key)
            if ix is None:
                ix = L.LibraryIndex(self.cache_dir, self.library_roots, zip_path or None)
                self._lib_indexes[key] = ix
        return ix

    def _library(self, symbol: str, root: str = "", wait: bool = True):
        if S.is_local(symbol):
            return None
        try:
            ix = self._library_index(root)
            d = L.lookup(ix, symbol, wait)
            note = self._jdk_choice(root)[1]
            if d is not None and d.lib.jar.endswith("src.zip") and note:
                d.note = note
            return d
        except L.LibraryBusy as e:
            raise QueryError(str(e)) from e
        except (CacheError, OSError, sqlite3.Error):
            return None

    def _library_overload_params(self, symbol: str, root: str,
                                 qualified: bool = False) -> list[tuple]:
        """Every overload of a library method or constructor, numbered as SCIP does, with its
        parameter types, from the sources jar ([] when they cannot be read); with qualified,
        a third element: those types as SCIP symbols (None where unknown)."""
        if not S.is_method(symbol):
            return []
        try:
            d = self._library(symbol, root)
        except QueryError:
            return []
        if d is None or d.member is None or not d.overloads:
            return []
        _path, types = L.class_path_of(symbol)
        tvars = L._type_variables(" ".join(d.type_declaration.split()), types[-1]) \
            if types else set()
        out = [(S.overload_symbol(symbol, k), L.param_types(m.declaration, tvars))
               for k, m in enumerate(d.overloads)]
        if qualified:
            try:
                quals = L.qualified_param_types(self._library_index(root), d)
            except (CacheError, OSError, sqlite3.Error):
                quals = [None] * len(out)
            out = [(sym, params, q) for (sym, params), q in zip(out, quals)]
        return out

    def _b_definition(self, symbol: str) -> Body:
        def body(nav: Navigator):
            sym, note, d = self._resolved(nav, symbol, nav.definition)
            if d is None:
                lib = self._library(sym, nav.ws.root)
                if lib is not None:
                    return note + render.library_definition(lib), set()
            return note + render.definition(nav.ws, d, sym), {d.location.source} if d else set()
        return body

    def _b_implementations(self, symbol: str, direct_only: bool = False, offset: int = 0,
                           limit: int = DEFAULT_LIMIT * 2) -> Body:
        def body(nav: Navigator):
            sym, note, impls = self._resolved(nav, symbol, nav.implementations)
            text = render.implementations(sym, impls, direct_only, offset, limit)
            return note + text, {i.definition.location.source for i in impls}
        return body

    def _b_supertypes(self, symbol: str) -> Body:
        def body(nav: Navigator):
            sym, note, (d, supers) = self._resolved(nav, symbol, nav.supertypes)
            srcs = {s.definition.location.source for s in supers if s.definition}
            return note + render.supertypes(sym, d, supers), srcs
        return body

    def _source_of(self, nav: Navigator, file: str) -> tuple[str, str]:
        if file.endswith(".java") or "/" in file or os.sep in file:
            return nav._normalize_source(file), ""
        sym, note, d = self._resolved(nav, file, nav.definition)
        if d is None:
            raise QueryError(f"{sym} is not defined in this project")
        return d.location.source, note

    def _b_outline(self, file: str, offset: int = 0, limit: int = 500) -> Body:
        def body(nav: Navigator):
            source, note = self._source_of(nav, file)
            defs = nav.outline(source)
            return note + render.outline(source, defs, offset, limit), {source}
        return body

    def _b_callers(self, symbol: str, depth: int = 1, include_overridden: bool = False,
                   max_nodes: int = 200, offset: int = 0, limit: int = DEFAULT_LIMIT,
                   scope: str = "") -> Body:
        def body(nav: Navigator):
            d = max(1, min(depth, 5))
            sym, note, root = self._resolved(
                nav, symbol, lambda s: nav.callers(s, d, include_overridden, max(1, max_nodes)))
            if scope:
                root.children = [c for c in root.children if c.sites and X.in_scope(
                    c.symbol, c.sites[0].location.source, scope)]
            srcs = set()
            stack = [root]
            while stack:
                n = stack.pop()
                srcs.update(s.location.source for s in n.sites)
                stack.extend(n.children)
            return note + render.callers(root, d, offset, limit), srcs
        return body

    def _b_callees(self, symbol: str, include_fields: bool = False, include_types: bool = False,
                   offset: int = 0, limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            sym, note, (d, out) = self._resolved(
                nav, symbol, lambda s: nav.callees(s, include_fields, include_types))
            return (note + render.callees(sym, d, out, offset, limit),
                    {d.location.source} if d else set())
        return body

    def _b_source(self, symbol: str, max_lines: int = 200) -> Body:
        def body(nav: Navigator):
            sym, note, src = self._resolved(nav, symbol, lambda s: X.source(nav, s, max_lines))
            if src is None:
                lib = self._library(sym, nav.ws.root)
                if lib is not None:
                    return note + render.library_source(lib, max_lines), set()
            return note + render.source(src, sym), (
                {src.definition.location.source} if src else set())
        return body

    def _b_members(self, symbol: str, inherited: bool = True, offset: int = 0,
                   limit: int = 300) -> Body:
        def body(nav: Navigator):
            r = nav.resolve(symbol)
            sym = r.symbol
            if sym is not None and S.is_type(sym) and nav.definition(sym) is None:
                return self._library_members(nav, sym, r.note, inherited, offset, limit), set()
            sym, note, (groups, external) = self._resolved(
                nav, symbol, lambda s: X.members(nav, s, inherited))
            srcs = {g.definition.location.source for g in groups if g.definition}
            libs = []
            if external:
                try:
                    index = self._library_index(nav.ws.root)
                except (CacheError, OSError):
                    index = None
                overridden = {o for g in groups for over in g.overrides.values() for o in over}
                if index is not None:
                    order = self._nearest_first(index, nav, sym, external)
                    hidden = {o for o in overridden if S.parent(o) in external}
                    for ext, lib, mems, nhidden in L.ordered_members(index, order, hidden):
                        libs.append((ext, lib, mems, nhidden))
                else:
                    libs = [(e, None, [], 0) for e in external]
            return note + render.members(sym, groups, external, offset, limit, libs), srcs
        return body

    def _nearest_first(self, index, nav: Navigator, sym: str, external: list[str]) -> list[str]:
        """External supertypes ordered by distance through the library hierarchy."""
        _d, supers = nav.supertypes(sym)
        start = [s.symbol for s in supers if s.definition is None and s.direct]
        order, seen = [], set()
        queue = list(start)
        while queue:
            t = queue.pop(0)
            if t in seen:
                continue
            seen.add(t)
            order.append(t)
            queue.extend(L.library_supertypes(index, t))
        order += [e for e in external if e not in seen]
        return [t for t in order if t in set(external)] + [t for t in order
                                                           if t not in set(external)]

    def _library_members(self, nav: Navigator, sym: str, note: str, inherited: bool,
                         offset: int, limit: int) -> str:
        try:
            index = self._library_index(nav.ws.root)
        except (CacheError, OSError) as e:
            raise QueryError(f"library sources unavailable: {e}") from e
        own = L.type_members(index, sym)
        if own is None:
            raise QueryError(f"{sym} is not defined in this project and its sources were not "
                             "found in the Gradle/Maven caches")
        types = [sym] + (L.library_ancestors(index, sym) if inherited else [])
        groups = L.ordered_members(index, types)
        head = f"-- {note}\n" if note and not S.looks_like_symbol(sym) else ""
        return head + render.library_members(sym, groups, offset, limit)

    def _b_annotated(self, annotation: str, offset: int = 0, limit: int = DEFAULT_LIMIT,
                     scope: str = "") -> Body:
        def body(nav: Navigator):
            sym, note, items = self._resolved(nav, annotation, lambda s: X.annotated(nav, s))
            if scope:
                items = [a for a in items if X.in_scope(
                    a.definition.symbol if a.definition else "", a.location.source, scope)]
            return (note + render.annotated(sym, items, offset, limit),
                    {a.location.source for a in items})
        return body

    def _b_typed(self, symbol: str, categories: list[str] | None = None, offset: int = 0,
                 limit: int = DEFAULT_LIMIT, scope: str = "",
                 modifiers: list[str] | None = None, subtypes: bool = False) -> Body:
        def body(nav: Navigator):
            sym, note, uses = self._resolved(nav, symbol, lambda s: X.typed(nav, s))
            if subtypes:
                subs = self._subtypes(nav, sym)
                for t in subs:
                    uses = uses + [dataclasses.replace(u, via=S.short(t))
                                   for u in X.typed(nav, t)]
                uses.sort(key=X.typed_order)  # one section per category, subtypes merged
                if subs:
                    note += (f"-- with {len(subs)} subtypes: "
                             + ", ".join(S.short(t) for t in subs[:12])
                             + (" …" if len(subs) > 12 else "") + "\n")
            if scope:
                uses = [u for u in uses if X.in_scope(u.owner, u.location.source, scope)]
            if modifiers:
                want = [m.strip() for m in modifiers if m.strip()]
                uses = [u for u in uses if all(re.search(r"\b" + re.escape(m) + r"\b", u.detail)
                                               for m in want)]
            filters = (f" with {' '.join(modifiers)}" if modifiers else "") + (
                f" in {scope}" if scope else "")
            return (note + render.typed(sym, uses, categories, offset, limit, filters),
                    {u.location.source for u in uses})
        return body

    def _subtypes(self, nav: Navigator, sym: str) -> list[str]:
        """Project subtypes (from the shards) plus library subtypes among the external types
        the project references (from library sources; same top-level package by default)."""
        out = [i.definition.symbol for i in nav.implementations(sym)
               if S.is_type(i.definition.symbol)]
        root_pkg = sym.split("/", 1)[0]
        try:
            index = self._library_index(nav.ws.root)
            with self._graph(nav) as (g, _stats):
                rows = g.con.execute(
                    "SELECT s.symbol FROM symbols s WHERE s.symbol LIKE ? AND s.symbol LIKE '%#' "
                    "AND NOT EXISTS (SELECT 1 FROM defs d WHERE d.symbol_id = s.id)",
                    (root_pkg + "/%",))
                candidates = [x for (x,) in rows if x != sym and S.is_type(x)]
            for c in candidates:
                if sym in L.library_ancestors(index, c):
                    out.append(c)
        except L.LibraryBusy as e:
            raise QueryError(str(e)) from e
        except (CacheError, OSError, sqlite3.Error, QueryError):
            pass
        return sorted(set(out))

    def _b_family(self, symbol: str, offset: int = 0, limit: int = DEFAULT_LIMIT,
                  show_uses: bool = True) -> Body:
        def body(nav: Navigator):
            notes: list[str] = []
            lib = self.library_family(nav, notes)
            sym, note, fam = self._resolved(nav, symbol, lambda s: X.family(nav, s, lib))
            srcs = {r.location.source for m in fam for r in m.uses}
            return note + "".join(dict.fromkeys(notes)) + \
                render.family(sym, fam, nav.ws, offset, limit, show_uses), srcs
        return body

    def library_family(self, nav: Navigator, notes: list[str]) -> Callable:
        """For X.family: the library part of a library method's override family."""
        return lambda method: self._library_family(nav, method, notes)

    def _library_family(self, nav: Navigator, method: str, notes: list[str]) -> list:
        """A library method's library family, from the sources jars: the library methods it
        overrides ("ancestor"), and the overrides in library subtypes that the project calls
        ("descendant": List#size, Set#size, ArrayList#size… for Collection#size; a subtype
        nobody here uses is left out). Its own entry ("this") carries its declaration."""
        if S.kind_of(method) != "method":
            return []
        root = nav.ws.root
        own = dict(self._library_overload_params(method, root))
        base = own.get(method)
        if base is None:
            notes.append(f"-- {S.short(method)}: its sources were not found (or not parsed): "
                         "no library subtypes or supertypes searched\n")
            return []
        owner, name = S.parent(method), S.name_of(method)
        try:
            index = self._library_index(root)
        except (CacheError, OSError, sqlite3.Error):
            return []

        def member(sym: str, relation: str):
            d = self._library(sym, root)
            if d is None or d.member is None:
                return X.FamilyMember(None, sym, relation)
            return X.FamilyMember(None, sym, relation, [], d.member.declaration,
                                  f"{d.lib.coordinates} {d.lib.entry}:{d.member.line}")

        out = [member(method, "this")]
        try:
            for anc in L.library_ancestors(index, owner):
                first = S.overload_symbol(f"{anc}{S._format_name(name)}().", 0)
                for sym, params in self._library_overload_params(first, root):
                    if params is not None and L._same_signature(params, base):
                        out.append(member(sym, "ancestor"))
                        break
            used = self._external_methods_named(nav, name)
            owners = sorted({S.parent(x) for x in used} - {owner})
            if len(owners) > LIBRARY_FAMILY_TYPES:
                notes.append(f"-- {len(owners) - LIBRARY_FAMILY_TYPES} more library types "
                             f"with a {name} method used here were not checked\n")
                owners = owners[:LIBRARY_FAMILY_TYPES]
            unread = []
            for o in owners:
                if owner not in L.library_ancestors(index, o):
                    continue
                mine = [x for x in used if S.parent(x) == o]
                known = self._library_overload_params(mine[0], root)
                if not known:
                    unread.append(S.short(o))
                    continue
                for sym, params in known:
                    if sym in mine and params is not None and L._same_signature(params, base):
                        out.append(member(sym, "descendant"))
            if unread:
                notes.append(f"-- library subtypes whose sources were not found, left out: "
                             f"{', '.join(unread[:8])}{' …' if len(unread) > 8 else ''}\n")
        except L.LibraryBusy as e:
            raise QueryError(str(e)) from e
        return out

    def _type_facts(self, nav: Navigator, g) -> dict:
        """For refactor's where types: whether a symbol is defined in the project (from the
        index: a library symbol would make Navigator.definition grep every shard), every
        supertype of a type, a library method's return type. Memoized for the plan."""
        defined: dict[str, bool] = {}
        ancestors: dict[str, list[str]] = {}
        returns: dict[str, str] = {}

        def is_defined(sym: str) -> bool:
            if sym not in defined:
                defined[sym] = g.con.execute(
                    "SELECT 1 FROM symbols s JOIN defs d ON d.symbol_id = s.id WHERE s.symbol = ?"
                    " LIMIT 1", (sym,)).fetchone() is not None
            return defined[sym]

        def ancestors_of(t: str) -> list[str]:
            if t not in ancestors:
                ancestors[t] = self._type_ancestors(nav, t, is_defined)
            return ancestors[t]

        def return_of(m: str) -> str:
            if m not in returns:
                returns[m] = self._library_return(nav, m)
            return returns[m]

        def exists(written: str) -> bool:
            """A type written in where types: a primitive, a type the index knows (declared,
            used or a supertype of one) or a library class with sources."""
            if written in S.PRIMITIVES or written in ("Object", "java.lang.Object"):
                return True
            if S.is_qualified_type(written):
                sym = S.type_symbol(written)
                if g.con.execute("SELECT 1 FROM symbols WHERE symbol = ?", (sym,)).fetchone():
                    return True
                try:
                    path, _t = L.class_path_of(sym)
                    return bool(path) and \
                        self._library_index(nav.ws.root).find_class(path) is not None
                except (CacheError, OSError, sqlite3.Error):
                    return True   # cannot tell: let it through
            name = written.rsplit(".", 1)[-1]
            return g.con.execute("SELECT 1 FROM symbols WHERE symbol GLOB ? OR symbol GLOB ? "
                                 "LIMIT 1", (f"*/{name}#", f"*#{name}#")).fetchone() is not None

        return {"defined": is_defined, "ancestors": ancestors_of, "returns": return_of,
                "exists": exists}

    def _type_ancestors(self, nav: Navigator, type_symbol: str,
                        defined: Callable) -> list[str] | None:
        """Every supertype of a type: a project type's from SCIP (which lists them all, library
        ones included); a library type's from its sources jar, None without one."""
        if defined(type_symbol):
            _d, supers = nav.supertypes(type_symbol)
            return [x.symbol for x in supers if x.symbol != type_symbol]
        try:
            index = self._library_index(nav.ws.root)
            path, _types = L.class_path_of(type_symbol)
            if not path or index.find_class(path) is None:
                return None
            return L.library_ancestors(index, type_symbol)
        except L.LibraryBusy as e:
            raise QueryError(str(e)) from e
        except (CacheError, OSError, sqlite3.Error):
            return None

    def _library_return(self, nav: Navigator, method: str) -> str:
        try:
            return L.return_type(self._library_index(nav.ws.root), method)
        except L.LibraryBusy as e:
            raise QueryError(str(e)) from e
        except (CacheError, OSError, sqlite3.Error):
            return ""

    def _external_methods_named(self, nav: Navigator, name: str) -> list[str]:
        """Library methods named `name` that the project uses (declared nowhere in it)."""
        like = "%#" + re.sub(r"([\\%_])", r"\\\1", name) + "(%"
        try:
            with self._graph(nav) as (g, _stats):
                rows = g.con.execute(
                    "SELECT s.symbol FROM symbols s WHERE s.symbol LIKE ? ESCAPE '\\' AND NOT "
                    "EXISTS (SELECT 1 FROM defs d WHERE d.symbol_id = s.id) AND "
                    "EXISTS (SELECT 1 FROM refs r WHERE r.symbol_id = s.id)", (like,))
                found = [x for (x,) in rows]
        except QueryError:
            found = nav.find_external(f"{name}()")
        return sorted({x for x in found if S.kind_of(x) == "method" and S.name_of(x) == name
                       and not S.is_local(x)})

    def _b_overloads(self, symbol: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            r = nav.resolve(symbol)
            if r.symbol is None:
                # A name matching several overloads of one method is what this tool is for.
                syms = sorted({d.symbol for d in r.candidates} | set(r.externals))
                if syms and all(S.is_method(x) and S.parent(x) == S.parent(syms[0])
                                and S.name_of(x) == S.name_of(syms[0]) for x in syms):
                    sym, note = syms[0], f"-- {symbol} → {len(syms)} overloads\n"
                    items = X.overloads(nav, sym) or self._library_overloads(nav, sym)
                    return note + render.overloads(sym, items, nav.ws, offset, limit), \
                        {u.location.source for m in items for u in m.uses}
            elif S.is_method(r.symbol) and nav.definition(r.symbol) is None:
                note = "" if S.looks_like_symbol(symbol) else f"-- {r.note}\n"
                items = self._library_overloads(nav, r.symbol)
                return note + render.overloads(r.symbol, items, nav.ws, offset, limit), \
                    {u.location.source for m in items for u in m.uses}
            sym, note, items = self._resolved(nav, symbol, lambda s: X.overloads(nav, s))
            srcs = {r.location.source for m in items for r in m.uses}
            return note + render.overloads(sym, items, nav.ws, offset, limit), srcs
        return body

    def _library_overloads(self, nav: Navigator, sym: str) -> list:
        """Overloads of a library method (from its sources jar, numbered as SCIP does), each
        with its uses in this project."""
        lib = self._library(sym, nav.ws.root)  # LibraryBusy becomes a QueryError
        if lib is None or lib.member is None or not lib.overloads:
            return []
        syms = [S.overload_symbol(sym, k) for k in range(len(lib.overloads))]
        uses = nav._references_many(set(syms))
        return [X.FamilyMember(None, s, "this" if s == sym else "overload", uses.get(s, []),
                               m.declaration, f"{lib.lib.coordinates} {lib.lib.entry}:{m.line}")
                for s, m in zip(syms, lib.overloads)]

    def _b_tests(self, symbol: str, transitive: bool = False, depth: int = 3) -> Body:
        def body(nav: Navigator):
            gradle = "./gradlew" if os.path.exists(os.path.join(nav.ws.root, "gradlew")) \
                else "gradle"
            template = self.settings(nav.ws.root).tests_filter
            if transitive:
                sym, note = self._resolve(nav, symbol)
                with self._graph(nav) as (g, stats):
                    nodes, _trunc = g.impact(sym, max(1, min(depth, 8)), root=nav.ws.root)
                    st = self.settings(nav.ws.root, g)
                    keep = self._test_classes(g, nodes, st)
                    # Glue methods are test code without @Test: before the filter below.
                    cuke = self._cucumber_affected(nav, g, st, {sym} | {n.symbol for n in nodes})
                allowed = {c for cs in keep.values() for c in cs}
                nodes = [n for n in nodes if not n.test or _fqcn(n.symbol) in allowed]
                text = render.tests_transitive(sym, nodes, depth, X.test_task, gradle, template)
                if cuke:
                    text = (text if "no test reaches" not in text else "") + (
                        "\n" if text and "no test reaches" not in text else "") + cuke
                return render.index_note(stats) + note + text, {n.source for n in nodes if n.test}
            sym, note, uses = self._resolved(nav, symbol, lambda s: X.tests_using(nav, s))
            return note + render.tests(sym, uses, X.test_task, gradle, template), \
                {t.source for t in uses}
        return body

    # --- cucumber ---------------------------------------------------------------------------

    def _b_steps(self, query: str = "", check: bool = False, offset: int = 0,
                 limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            with self._graph(nav) as (g, stats):
                glues = CU.glue(g, nav.ws.root)
                st = self.settings(nav.ws.root, g)
            feats = CU.features(nav.ws.root)
            note = render.index_note(stats)
            if not glues and not feats:
                return note + "no Cucumber step definitions or .feature files in this project", \
                    set()
            match = CU.matcher(glues)
            suite = CU.Suite(feats, st.cucumber_include_steps)
            if check or not query:
                return note + render.steps_check(glues, suite, match, st.cucumber_exclude_tags,
                                                 offset, limit), set()
            if query.endswith(".feature"):
                src = nav._normalize_source(query)
                feat = next((f for f in feats if f.source == src), None)
                if feat is None:  # e.g. under docs/: read it anyway when asked by path
                    try:
                        with open(os.path.join(nav.ws.root, src), encoding="utf-8",
                                  errors="replace") as fh:
                            feat = CU.parse_feature(fh.read(), src)
                    except OSError as e:
                        raise QueryError(f"{query}: {e}") from e
                if feat is None:
                    raise QueryError(f"{query}: no feature in it")
                return note + render.feature_steps(feat, match, offset, limit,
                                                   suite), set()
            by_symbol: dict[str, list] = {}
            for x in glues:
                by_symbol.setdefault(x.symbol, []).append(x)
            sym = query if query in by_symbol else None
            if sym is None:
                try:
                    r = nav.resolve(query)
                    if r.symbol in by_symbol:
                        sym = r.symbol
                except (QueryError, WorkspaceError):
                    pass
            if sym is not None:
                uses, affected = [], {}
                for f in feats:
                    for s in f.steps:
                        if any(x.symbol == sym for x in match(s)):
                            uses.append((f, s))
                            for src, idx in suite.runs_in(s).items():
                                affected.setdefault(src, set()).update(idx)
                run = self._cucumber_run_lines(nav.ws.root, affected, feats, st)
                return note + render.glue_uses(by_symbol[sym], uses, run, offset, limit,
                                               suite), {x.source for x in by_symbol[sym]}
            found: dict[str, object] = {}
            for s in CU.text_steps(query):
                for x in match(s):
                    found.setdefault(x.symbol + x.text, x)
            return note + render.step_text(query, list(found.values())), set()
        return body

    def _cucumber_run_lines(self, root: str, affected: dict[str, set[int]], feats, st) -> list:
        """The scenarios affected (rows), the commands to run them and warnings."""
        affected = {k: v for k, v in affected.items() if v}
        if not affected:
            return []
        gradle = "./gradlew" if os.path.exists(os.path.join(root, "gradlew")) else "gradle"
        notes: list[str] = []
        cmds = CU.run_commands(affected, feats, st.cucumber_run, st.cucumber_run_features,
                               gradle, st.cucumber_run_all, notes)
        by_source = {f.source: f for f in feats}
        excluded = []
        for src, idx in sorted(affected.items()):
            for k in sorted(idx):
                sc = by_source[src].scenarios[k]
                bad = [t for t in sc.tags if t in st.cucumber_exclude_tags]
                if bad:
                    excluded.append(f"{src}:{sc.line} {sc.name} ({' '.join(bad)})")
        return [affected, by_source, cmds, excluded, notes]

    def _cucumber_affected(self, nav: Navigator, g, st, glue_symbols: set[str],
                           changed_features: dict[str, list] | None = None) -> str:
        """The Cucumber scenarios a set of glue methods (reached by an impact) and changed
        .feature lines affect, with the commands to run them; "" when none."""
        try:
            glues = CU.glue(g, nav.ws.root)
        except sqlite3.Error:
            return ""
        if not glues and not changed_features:
            return ""
        feats = CU.features(nav.ws.root)
        match = CU.matcher(glues)
        suite = CU.Suite(feats, st.cucumber_include_steps)
        reached = glue_symbols & {x.symbol for x in glues}
        affected: dict[str, set[int]] = {}

        def add(found: dict[str, set[int]]) -> None:
            for src, idx in found.items():
                affected.setdefault(src, set()).update(idx)
        if reached:
            for f in feats:
                for s in f.steps:
                    if reached & {x.symbol for x in match(s)}:
                        add(suite.runs_in(s))
        for src, ranges in (changed_features or {}).items():
            lines = {ln for start, count in ranges for ln in range(start, start + max(count, 1))}
            add(suite.at_lines(src, lines))
        run = self._cucumber_run_lines(nav.ws.root, affected, feats, st)
        return render.cucumber_affected(run, len(reached)) if run else ""

    # --- refactor ---------------------------------------------------------------------------

    def _b_refactor(self, plan=None, dry_run: bool = False, plan_id: str = "",
                    verify: bool = False) -> Body:
        def body(nav: Navigator):
            root = nav.ws.root
            if verify:
                meta, _patch = R.load_meta(root, plan_id, self.cache_dir)
                ix = Index(nav.ws, self.cache_dir, self.index_external, self.build_workers)
                lines = R.verify(nav, meta, confirm=lambda srcs: ix.record_compiled(srcs))
                ok = lines and lines[0].startswith("verified")
                text = "\n".join(lines)
                if ok:
                    paths = [f["new"] for f in meta["files"]]
                    try:
                        changed, _srcs = self._b_changed("HEAD", False, paths, 3)(nav)
                        text += "\n\nwhat the change reaches (changed since HEAD in the " \
                                "patched files):\n" + _summary_of_changed(changed)
                    except (QueryError, WorkspaceError) as e:
                        text += f"\n(tests to run: run changed(); {e})"
                return text, {f["new"] for f in meta["files"]}
            if plan is None or plan == "":
                raise QueryError("pass plan (or plan_id with verify=true); refactor_help shows "
                                 "the format")
            with self._graph(nav) as (g, _stats):
                st = self.settings(root, g)
                lib_cache: dict = {}
                try:
                    res = R.run(nav, st, plan, lambda lvl: JVM.Worker(lvl, self.cache_dir),
                                self.cache_dir, dry_run,
                                lambda t, subtypes=False: self._members_named(
                                    nav, g, t, subtypes, lib_cache),
                                JVM.language_level(root),
                                types_named=lambda name: self._types_named(g, name),
                                library_family=lambda notes: self.library_family(nav, notes),
                                type_facts=self._type_facts(nav, g))
                except JVM.ToolError as e:
                    raise QueryError(str(e)) from e
            return res.text, set()
        return body

    @staticmethod
    def _types_named(g, name: str) -> list[str]:
        """Type symbols (project and library) with this simple name, from the index."""
        if not re.fullmatch(r"[\w$]+", name or ""):
            return []
        return sorted({x for (x,) in g.con.execute(
            "SELECT symbol FROM symbols WHERE symbol LIKE ?", (f"%/{name}#",))
            if S.is_type(x) and S.name_of(x) == name})

    def _members_named(self, nav: Navigator, g, sym: str, subtypes: bool = False,
                       lib_cache: dict | None = None) -> dict[str, list]:
        """{name: [(kind, arity)]} of a type's members, its supertypes' (libraries too) and,
        with subtypes, its subtypes': from the index, one query per type."""
        out: dict[str, list] = {}
        sid = g.ids([sym]).get(sym)
        supers: set[str] = set()
        subs: set[str] = set()
        if sid is not None:
            supers = {t for t in g.names(g.ancestors(sid)).values() if S.is_type(t)}
            if subtypes:
                subs = {t for t in g._subtypes_of({sym}) if S.is_type(t)}
        types = {sym} | supers | subs
        library = []
        for t in sorted(types):
            hi = t[:-1] + chr(ord(t[-1]) + 1)
            rows = list(g.con.execute(
                "SELECT s.symbol, d.signature FROM symbols s JOIN defs d ON d.symbol_id = s.id "
                "WHERE s.symbol >= ? AND s.symbol < ?", (t, hi)))
            if not rows:
                library.append(t)
                continue
            for member, sig in rows:
                if S.parent(member) != t or S.is_local_like(member) or \
                        S.kind_of(member) not in ("method", "term"):
                    continue
                if t in supers and S.is_private(sig or ""):
                    continue   # not inherited
                out.setdefault(S.name_of(member), []).append(
                    ("method" if S.is_method(member) else "field", R._sig_arity(sig or "")))
        cache = lib_cache if lib_cache is not None else {}
        for t in library:   # one library type at a time, each read once per call
            if t not in cache:
                cache[t] = []
                try:
                    index = self._library_index(nav.ws.root)
                    for _ext, _lib, mems, _h in L.ordered_members(index, [t], set()):
                        for m in mems:
                            if m.kind in ("method", "field"):
                                types_ = L.param_types(m.declaration) \
                                    if m.kind == "method" else ()
                                cache[t].append((m.name, m.kind, len(types_)
                                                 if types_ is not None else -1))
                except (CacheError, OSError, L.LibraryBusy, sqlite3.Error):
                    pass
            for name, kind, arity in cache[t]:
                out.setdefault(name, []).append((kind, arity))
        return out

    # --- context ----------------------------------------------------------------------------

    def _b_context(self, symbol: str, token_budget: int = 4000) -> Body:
        def body(nav: Navigator):
            sym, note = self._resolve(nav, symbol)
            short = S.short(sym)
            d = nav.definition(sym)
            if d is None:
                ctx = self._external_context(nav, sym, short)
            elif S.is_type(sym):
                ctx = C.Context("type", C.head_of(nav, d))
                for sec in (C.members_section(nav, d, short),
                            C.subtypes_section(nav, sym, short)):
                    if sec is not None:
                        ctx.sections.append(sec)
                refs = _drop_imports(nav, nav.references(sym)[0])
                users = C.users_section(refs, short)
                if users is not None:
                    ctx.sections.append(users)
                ctx.sections.append(self._tests_section(nav, sym, refs))
                ctx.sources |= {r.location.source for r in refs}
            elif S.kind_of(sym) == "term":
                ctx = C.Context("field", C.head_of(nav, d))
                secs, refs, accessors = C.field_sections(nav, d, short)
                ctx.sections += secs
                ctx.sections.append(self._tests_section(nav, sym, refs))
                ctx.hints.append(self._by_name_hint(nav, sym, refs, short))
                ctx.sources |= {r.location.source for r in refs}
            else:
                ctx = C.Context("method", C.head_of(nav, d))
                callers, srcs, refs = C.callers_section(nav, sym, short)
                if not callers.lines:
                    ctx.hints.append(callers.title)
                for sec in (C.body_section(nav, d, short),
                            C.overridden_by_section(nav, sym, short), callers,
                            C.callees_section(nav, sym, short),
                            self._tests_section(nav, sym, refs)):
                    if sec is not None:
                        ctx.sections.append(sec)
                ctx.sources |= srcs
                n = C.overload_count(nav, d)
                if n > 1:
                    ctx.hints.append(f"{n} overloads → overloads({short})")
                if re.match(r"(get|set|is|has)[A-Z]", S.name_of(sym)):
                    ctx.hints.append(self._by_name_hint(nav, sym, refs, short))
                ctx.hints.append(self._impact_hint(nav, sym, short))
            if d is not None:
                ctx.sources.add(d.location.source)
            ctx.hints = [h for h in ctx.hints if h]
            ctx.sections = [s for s in ctx.sections if s is not None]
            return note + C.fit(ctx, token_budget), ctx.sources
        return body

    def _external_context(self, nav: Navigator, sym: str, short: str) -> C.Context:
        ctx = C.Context("external", [sym])
        try:
            lib = self._library(sym, nav.ws.root)
        except QueryError as e:  # e.g. the library map is being built: uses still matter
            lib = None
            ctx.head.append(f"external (JDK or a dependency): sources unavailable now ({e})")
        if lib is None:
            if len(ctx.head) == 1:
                ctx.head.append("external (JDK or a dependency): no sources jar found for it")
        else:
            m = lib.member
            line = m.line if m else lib.body[0]
            ctx.head += [f"external — {lib.lib.coordinates}",
                         f"source: {lib.lib.jar}!/{lib.lib.entry}:{line}"]
            if getattr(lib, "note", ""):
                ctx.head.append(f"note: {lib.note}")
            if m is not None:
                ctx.head += [f"in: {render._decl(lib.type_declaration)}",
                             f"{m.kind}: {render._decl(m.declaration)}"]
                if len(lib.overloads) > 1:
                    ctx.hints.append(f"{len(lib.overloads)} overloads → overloads({short})")
            else:
                ctx.head.append(render._decl(lib.type_declaration))
            doc = (m.javadoc if m is not None else lib.type_javadoc).strip()
            if doc:
                if len(doc) > C.JAVADOC_CHARS:
                    doc = doc[:C.JAVADOC_CHARS].rstrip() + " …"
                ctx.head += [""] + doc.splitlines()
            first, last = lib.body
            if m is not None and last - first < 60:
                width = len(str(last))
                ctx.sections.append(C.Section(
                    "body", "body:", [f"{first + k:>{width}}  {t.rstrip()[:C.LINE_CHARS]}"
                                      for k, t in enumerate(lib.source.lines(first, last))],
                    f"source({short})", C.BODY_TAIL))
        if S.is_type(sym):
            refs = _drop_imports(nav, nav.references(sym)[0])
            users = C.users_section(refs, short)
            ctx.sources |= {r.location.source for r in refs}
            if users is not None:
                ctx.sections.append(users)
                users.name = "callers"  # its share of the budget
        else:
            callers, srcs, refs = C.callers_section(nav, sym, short)
            if not callers.lines:
                ctx.hints.append(callers.title)
            ctx.sections.append(callers)
            ctx.sources |= srcs
        ctx.sections.append(self._tests_section(nav, sym, refs))
        return ctx

    def _tests_section(self, nav: Navigator, sym: str, refs: list) -> C.Section | None:
        uses = X.tests_using(nav, sym, refs)
        if not uses:
            return None
        gradle = "./gradlew" if os.path.exists(os.path.join(nav.ws.root, "gradlew")) \
            else "gradle"
        lines = render.tests(sym, uses, X.test_task, gradle,
                             self.settings(nav.ws.root).tests_filter).splitlines()[1:]
        run = next((k for k, x in enumerate(lines) if x.startswith("run them")), len(lines))
        n = f"1 class uses" if len(uses) == 1 else f"{len(uses)} classes use"
        return C.Section("tests", f"tests ({n} it directly):", lines,
                         f"tests({S.short(sym)})", len(lines) - run)

    def _by_name_hint(self, nav: Navigator, sym: str, refs: list, short: str) -> str:
        names = X.property_names(sym)
        if not names:
            return ""
        exclude = {(r.location.source, r.location.line) for r in refs}
        hits = X.uses_by_name(nav.ws.root, names, self.settings(nav.ws.root).by_name_globs,
                              exclude)
        if not hits:
            return ""
        n = f"{len(hits)}+" if len(hits) >= 200 else str(len(hits))
        return (f"{n} possible uses by name ({', '.join(names)}) in strings and other files "
                f"→ references({short}, by_name=True)")

    def _impact_hint(self, nav: Navigator, sym: str, short: str) -> str:
        """Only from an auxiliary index that already exists and is (nearly) up to date: context
        never builds it."""
        try:
            ix = Index(nav.ws, self.cache_dir, self.index_external, self.build_workers)
            if ix._needs_scratch():  # missing, or for another schema/mode: never touched here
                return (f"impact needs the auxiliary index (built on first use, ~20 s on a "
                        f"large project) → impact({short})")
            behind = ix.pending()
            if behind > C.IMPACT_MAX_BEHIND:
                return (f"impact: the auxiliary index is {behind} shards behind → "
                        f"impact({short}) updates it")
            if behind:
                ix.update()
            g = Graph(ix)
            try:
                nodes, truncated = g.impact(sym, 3, True, root=nav.ws.root)
            finally:
                g.close()
        except (CacheError, IndexError_, sqlite3.Error, OSError):
            return ""
        if not nodes:
            return "impact (depth 3): nothing in this project reaches it"
        by_module: dict[str, int] = {}
        for n in nodes:
            by_module[n.module or "(root)"] = by_module.get(n.module or "(root)", 0) + 1
        top = sorted(by_module.items(), key=lambda kv: -kv[1])[:6]
        tests = sum(1 for n in nodes if n.test)
        modules = ("; " + ", ".join(f"{m} {c}" for m, c in top)) if len(by_module) > 1 else ""
        return (f"impact (depth 3): {len(nodes)} definitions ({tests} in tests"
                + ("; truncated" if truncated else "") + ")" + modules + f" → impact({short})")

    # --- compare checkouts -------------------------------------------------------------------

    COMPARE_BUILDS_PER_CALL = 2  # all_worktrees: indexes built at most per call

    def _b_compare(self, other_root: str = "", scope: str = "", api_only: bool = True,
                   limit: int = 50, all_worktrees: bool = False) -> Body:
        if all_worktrees:
            return self._b_compare_all(scope)

        def body(nav: Navigator):
            if not other_root:
                raise QueryError("pass other_root (a worktree or clone), or all_worktrees=True")
            try:
                ws_b = self.workspace(other_root)
            except WorkspaceError as e:
                raise QueryError(f"other checkout: {e}") from e
            a_root, b_root = nav.ws.root, ws_b.root
            if os.path.realpath(a_root) == os.path.realpath(b_root):
                raise QueryError("both sides are the same checkout")
            head_lines = []
            fb = ws_b.freshness()
            if fb:
                head_lines.append("B: " + render.freshness(fb).replace("\n", "\nB: "))
            ix_b = Index(ws_b, self.cache_dir, self.index_external, self.build_workers)
            if (ix_b._needs_scratch() or ix_b.pending()) and build_running(b_root):
                raise QueryError(f"a gradle/mvn build is running in {b_root}: its index is "
                                 "not updated meanwhile; retry when it finishes")
            try:
                stats_b = ix_b.ensure()
                g_b = Graph(ix_b)
            except (CacheError, IndexError_, sqlite3.Error) as e:
                raise QueryError(f"auxiliary index of B unavailable: {e}") from e
            try:
                with self._graph(nav) as (g_a, stats_a):
                    all_a, all_b = CMP.api_of(g_a, scope, False), CMP.api_of(g_b, scope, False)
                    full = CMP.api_diff(all_a, all_b)
                    diff = full if not api_only else CMP.api_diff(
                        {k: v for k, v in all_a.items() if CMP.is_api(v)},
                        {k: v for k, v in all_b.items() if CMP.is_api(v)})
                    # What a merge breaks does not depend on visibility: a package-private
                    # member changed in one module breaks its callers there too.
                    git = self._compare_git(a_root, b_root, g_a, g_b, full)
            finally:
                g_b.close()
            notes = render.index_note(stats_a)
            nb = render.index_note(stats_b)
            if nb:
                notes += nb.replace("-- index", "-- B: index", 1)
            a_label = self._checkout_label(a_root)
            b_label = self._checkout_label(b_root)
            text = render.compare(a_label, b_label, diff, api_only, scope, git, limit)
            return notes + ("\n".join(head_lines) + "\n" if head_lines else "") + text, set()
        return body

    def _b_compare_all(self, scope: str = "") -> Body:
        """Every other worktree of this repository against this one, one line each."""
        def body(nav: Navigator):
            a_root = nav.ws.root
            wts = CMP.worktrees(a_root)
            if not wts:
                raise QueryError("no git worktrees found for this checkout")
            rows, built, pending = [], [], []
            here = os.path.realpath(a_root)
            # The build may live in a subdirectory of the repository: the same one in each
            # worktree.
            top = CMP._git(a_root, "rev-parse", "--show-toplevel") or a_root
            sub = os.path.relpath(here, os.path.realpath(top.strip()))
            with self._graph(nav) as (g_a, _stats):
                all_a = CMP.api_of(g_a, scope, False)
                for w in wts:
                    path = w["path"]
                    if os.path.realpath(path) == here or here.startswith(
                            os.path.realpath(path) + os.sep):
                        continue
                    label = f"{path} ({w['branch'] or 'detached'} {w.get('head', '')[:10]})"
                    if w["locked"] or w["prunable"]:
                        why = "locked" if w["locked"] else "prunable"
                        rows.append(f"  {label}: skipped ({why})")
                        continue
                    try:
                        ws_b = self.workspace(path if sub in (".", "") or sub.startswith("..")
                                              else os.path.join(path, sub))
                        ws_b.files()
                    except WorkspaceError:
                        rows.append(f"  {label}: not compiled (no shards)")
                        continue
                    ix_b = Index(ws_b, self.cache_dir, self.index_external, self.build_workers)
                    needs = ix_b._needs_scratch() or ix_b.pending() > 0
                    if needs and build_running(ws_b.root):
                        rows.append(f"  {label}: compiling, retry when it finishes")
                        continue
                    if ix_b._needs_scratch() and len(built) >= self.COMPARE_BUILDS_PER_CALL:
                        pending.append(path)
                        rows.append(f"  {label}: index not built yet (call again)")
                        continue
                    try:
                        st = ix_b.ensure()
                        if st.built_from_scratch:
                            built.append(path)
                        g_b = Graph(ix_b)
                    except (CacheError, IndexError_, sqlite3.Error) as e:
                        rows.append(f"  {label}: index unavailable ({e})")
                        continue
                    try:
                        full = CMP.api_diff(all_a, CMP.api_of(g_b, scope, False))
                        git = self._compare_git(a_root, ws_b.root, g_a, g_b, full)
                    finally:
                        g_b.close()
                    stale = "  [B stale]" if ws_b.freshness() else ""
                    if git is None:
                        rows.append(f"  {label}: no shared history; {len(full.only_a)} only "
                                    f"here, {len(full.only_b)} only there{stale}")
                        continue
                    n_ba = sum(len(b.uses) for b in git["b_into_a"])
                    n_ab = sum(len(b.uses) for b in git["a_into_b"])
                    rows.append(f"  {label}: both changed {len(git['both_files'])} files, "
                                f"{len(git['both_members'])} members; merging it here breaks "
                                f"{n_ba} uses, merging here into it {n_ab}{stale}")
            head = [f"worktrees compared with {a_root} (details: compare(other_root=…)):"]
            if built:
                head.append(f"-- indexes built in this call: {', '.join(built)}")
            if pending:
                head.append(f"-- {len(pending)} worktrees still need their index (at most "
                            f"{self.COMPARE_BUILDS_PER_CALL} are built per call): call again")
            return "\n".join(head + rows), set()
        return body

    @staticmethod
    def _checkout_label(root: str) -> str:
        commit, branch = CMP.head(root)
        return root + (f" ({branch} {commit[:10]})" if commit else "")

    @staticmethod
    def _compare_git(a_root: str, b_root: str, g_a, g_b, diff) -> dict | None:
        base = CMP.merge_base(a_root, b_root)
        if not base:
            return None
        files_a = CMP.changed_files(a_root, base)
        files_b = CMP.changed_files(b_root, base)
        try:
            members_a = g_a.changed(a_root, base)["definitions"]
            members_b = g_b.changed(b_root, base)["definitions"]
        except GraphError:
            members_a, members_b = {}, {}
        both = sorted(set(members_a) & set(members_b))
        sources = {s: e.get("source", "") for s, e in members_a.items()}
        # B's removals and changes break A's uses (and the other way around).
        return {"base": base, "files_a": files_a, "files_b": files_b,
                "both_files": files_a & files_b, "both_members": both,
                "member_source": sources,
                "b_into_a": CMP.breaks(CMP.merge_effects(diff, True), files_b, files_a, g_a),
                "a_into_b": CMP.breaks(CMP.merge_effects(diff, False), files_a, files_b, g_b)}

    # --- v2: queries over the auxiliary index -----------------------------------------------

    @contextlib.contextmanager
    def _graph(self, nav: Navigator):
        try:
            ix = Index(nav.ws, self.cache_dir, self.index_external, self.build_workers)
            stats = ix.ensure()
            g = Graph(ix)
        except (CacheError, IndexError_, sqlite3.Error) as e:
            raise QueryError(f"auxiliary index unavailable: {e}") from e
        try:
            yield g, stats
        finally:
            g.close()

    def _b_impact(self, symbol: str, depth: int = 3, include_overridden: bool = True,
                  group_by: str = "module", offset: int = 0, limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            sym, note = self._resolve(nav, symbol)
            d = max(1, min(depth, 10))
            with self._graph(nav) as (g, stats):
                # Entry points are searched deeper than the rows go: a screen or a job is
                # often four or five calls away.
                deep = max(d, ENTRY_DEPTH)
                st = self.settings(nav.ws.root, g)
                every, trunc = g.impact(sym, deep, include_overridden, root=nav.ws.root,
                                        lifecycle=st.lifecycle_methods)
                nodes = [n for n in every if n.depth <= d]
                trunc = trunc and max((n.depth for n in every), default=0) <= d
                specs = [x for x in st.entry_specs if x.get("reached", True)]
                entries = g.entry_points(every, specs, nav.ws.root)
                defs = g.definitions(g.ids([sym]).values())
                dd = next(iter(defs.values()), None)
                itself = [e for e in g.entry_points(
                    [G.Node(sym, 0, "", dd[0] if dd else "", dd[1] if dd else 0)],
                    specs, nav.ws.root) if e.kind != "test"]
                left_out = g.left_out
                # Where the chain stops at an override of a library method, the library calls
                # it: its callers are whoever creates the object.
                vias = {n.via for n in every}
                tops = [sym] + [n.symbol for n in every if n.symbol not in vias and not n.test
                                and S.is_method(n.symbol)][:300]
                callbacks = g.library_callbacks(
                    tops, root=nav.ws.root, bound=g.last_bound,
                    skip={S.owner_type(e.node.symbol) for e in entries + itself})
                # Calls made only from inside an anonymous class or a lambda: they run when
                # whoever receives that object calls it (a library, often).
                deferred = list({n.symbol: n for n in nodes if n.inner and
                                 not n.test}.values())      # of the rows shown
            text = render.impact(sym, nodes, trunc, d, group_by, offset, limit, entries,
                                 itself, deep)
            text += render.library_callbacks(callbacks, deferred)
            if left_out:
                text += (f"\nnote: {left_out} callers of methods these override were left out: "
                         "their files name another subtype, or a library type's own objects "
                         "(nothing in them names the overriding type)")
            return render.index_note(stats) + note + text, {n.source for n in nodes}
        return body

    def _b_path(self, source: str, target: str, max_depth: int = 6, limit: int = 3) -> Body:
        def body(nav: Navigator):
            a, note_a = self._resolve(nav, source)
            b, note_b = self._resolve(nav, target)
            with self._graph(nav) as (g, stats):
                paths = g.path(a, b, max(1, min(max_depth, 12)), max(1, limit))
            return render.index_note(stats) + note_a + note_b + render.path(a, b, paths), set()
        return body

    def _test_classes(self, g, nodes, settings) -> OrderedDict[str, set[str]]:
        """{"<module>/<task>": {test class FQCN}} among the nodes that live in test code. With a
        test profile active (e.g. junit), only classes that have a test-annotated member
        count."""
        out: OrderedDict[str, set[str]] = OrderedDict()
        real = g.classes_with(settings.test_annotations) if settings.test_annotations else None
        for n in nodes:
            if not n.test:
                continue
            top, pkg = S.top_level_type_name(n.symbol), S.package_of(n.symbol)
            fqcn = f"{pkg}.{top}" if pkg else top
            if real is not None and fqcn not in real:
                continue
            out.setdefault(n.task_dir or n.module, set()).add(fqcn)
        return out

    def _tests_line(self, root: str, tests: OrderedDict[str, set[str]], template: str) -> str:
        if not tests:
            return ""
        gradle = "./gradlew" if os.path.exists(os.path.join(root, "gradlew")) else "gradle"
        body = render.test_commands({t: sorted(n) for t, n in tests.items()}, gradle, template)
        return "tests to run (scipq-mcp does not):\n  " + "\n  ".join(body)

    def _b_changed(self, base: str = "HEAD", staged: bool = False,
                   paths: list[str] | None = None, depth: int = 3) -> Body:
        def body(nav: Navigator):
            with self._graph(nav) as (g, stats):
                try:
                    res = g.changed(nav.ws.root, base, staged, paths)
                except GraphError as e:
                    raise QueryError(str(e)) from e
                nodes: dict[str, object] = {}
                st = self.settings(nav.ws.root, g)
                for sym in res["definitions"]:
                    for n in g.impact(sym, max(1, min(depth, 8)), root=nav.ws.root,
                                      lifecycle=st.lifecycle_methods)[0]:
                        nodes.setdefault(n.symbol, n)
                # Changed test code is itself to be run.
                for sym, e in res["definitions"].items():
                    if e.get("test") and sym not in nodes:
                        mod = next((m for _s, (src, m, _t) in g.shards().items()
                                    if src == e["source"]), "")
                        nodes[sym] = G.Node(sym, 0, "", e["source"], e["line"],
                                            G.module_of(mod), True, task_dir=mod)
                tests = self._test_classes(g, nodes.values(), st)
                feature_hunks = {p: r for p, r in G.git_diff_hunks(
                    nav.ws.root, base, staged, paths or []).items() if p.endswith(".feature")}
                cuke = self._cucumber_affected(
                    nav, g, st, set(res["definitions"]) | set(nodes), feature_hunks)
                entries = g.entry_points([n for n in nodes.values() if n.depth > 0],
                                         [x for x in st.entry_specs if x["kind"] != "test"
                                          and x.get("reached", True)], nav.ws.root)
            tests_line = self._tests_line(nav.ws.root, tests, st.tests_filter)
            if cuke:
                tests_line = (tests_line + "\n" if tests_line else "") + cuke
            if entries:   # screens, endpoints, jobs… to try by hand (tests are listed above)
                tests_line = "\n".join(render.entry_points(entries, depth=depth)) + (
                    "\n" + tests_line if tests_line else "")
            text = render.changed(res, base if not staged else f"{base} (staged)",
                                  [n for n in nodes.values() if n.depth > 0], tests_line, depth)
            return render.index_note(stats) + text, {e["source"] for e in
                                                     res["definitions"].values()}
        return body

    def _b_deps(self, level: str = "module", scope: str = "", target: str = "",
                cycles: bool = True, sites: bool = False, offset: int = 0,
                limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            if level not in ("module", "package"):
                raise QueryError("level must be module or package")
            with self._graph(nav) as (g, stats):
                if sites:
                    if not scope or not target:
                        raise QueryError("sites=true needs both scope and target")
                    rows, total = g.uses_between(scope, target, max(1, limit))
                    return render.index_note(stats) + render.uses_between(scope, target, rows,
                                                                          total), set()
                edges = g.deps(level, scope, target)
                cyc = G.cycles(edges.keys()) if cycles else None
            return render.index_note(stats) + render.deps(edges, level, cyc, offset, limit,
                                                          target), set()
        return body

    def _b_dead_code(self, scope: str = "", public_is_api: bool = False,
                     include_tests: bool = False, offset: int = 0,
                     limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            with self._graph(nav) as (g, stats):
                st = self.settings(nav.ws.root, g)
                if scope and not g.in_scope_count(scope):
                    raise QueryError(f"scope {scope!r} matches no definition: use a package "
                                     "(com.example.billing), a module or directory (billing, "
                                     "billing/src) or a path glob (*/model/*)")
                globs = tuple(dict.fromkeys(list(G.DEAD_CODE_GLOBS) + st.by_name_globs))
                rows = g.dead_code(scope, public_is_api, include_tests,
                                   entry_annotations=st.entry_annotations,
                                   entry_supertypes=st.entry_supertypes,
                                   entry_type_names=st.entry_type_names,
                                   entry_packages=st.entry_packages,
                                   entry_xml=[x for e in st.entry_specs for x in e["xml"]],
                                   by_name_globs=globs, by_name_exclude=st.by_name_exclude,
                                   root=nav.ws.root, exclude_paths=st.dead_code_exclude)
                suppressed = dict(g.suppressed)
                weak = list(g.low_confidence)
                excluded = g.excluded
            note = f"-- profiles: {', '.join(st.profiles) or 'none'} ({st.source or 'none'})\n"
            if excluded:
                note += (f"-- {excluded} unreferenced definitions in [dead_code] exclude paths "
                         "left out: "
                         + ", ".join(st.dead_code_exclude[:6])
                         + (" …" if len(st.dead_code_exclude) > 6 else "") + "\n")
            return render.index_note(stats) + note + render.dead_code(rows, offset, limit,
                                                                      suppressed, weak), set()
        return body

    def _b_persistence(self, symbol: str = "", scope: str = "", offset: int = 0,
                       limit: int = 100) -> Body:
        def body(nav: Navigator):
            from . import persistence as P
            with self._graph(nav) as (g, stats):
                st = self.settings(nav.ws.root, g)
                if not st.entity_annotations:
                    raise QueryError("no persistence profile active (jdo, jpa or one of "
                                     "yours): nothing marks entities")
                m = P.Model(g, st, nav.ws.root)
                if not symbol:
                    def in_scope(e) -> bool:
                        return not scope or G.scope_matches(scope, S.package_of(e.symbol),
                                                            e.source)
                    text = P.render_list(m, in_scope, offset, limit)
                    return render.index_note(stats) + text, set()
                sym, note = self._resolve(nav, symbol)
                if S.is_type(sym):
                    if sym not in m.entities() | m.mapped():
                        raise QueryError(f"{S.short(sym)} is not persistent (no "
                                         "entity annotation of the active profiles)")
                    e = m.entity(sym)
                    return render.index_note(stats) + note + P.render_entity(
                        m, e, offset, limit), {e.source}
                if S.kind_of(sym) != "term":
                    raise QueryError("persistence takes an entity, a field of one, or nothing "
                                     "(the list of entities)")
                text = P.render_field(nav, m, sym, list(st.by_name_globs), offset, limit)
                return render.index_note(stats) + note + text, set()
        return body

    def _b_check_refs(self, files: list[str] | None = None, exclude: list[str] | None = None,
                      offset: int = 0, limit: int = 200, fmt: str = "text",
                      since: str = "") -> Body:
        def body(nav: Navigator):
            from . import checkrefs as CR
            try:        # library class names, to tell library types from missing ones
                self._library_index(nav.ws.root).refresh()
            except Exception:  # noqa: BLE001 — without them, fewer types are recognized
                pass
            with self._graph(nav) as (g, stats):
                st = self.settings(nav.ws.root, g)
                # the scope: the files named, else [check_refs] files, else what agents read
                if files:
                    globs, scope = list(files), CR.Scope(how="given", globs=tuple(files))
                elif st.check_refs_files is not None:
                    globs = list(st.check_refs_files)
                    scope = CR.Scope(how="config", globs=tuple(globs),
                                     origin=st.check_refs_files_from.split(" (")[0])
                else:
                    globs = list(CR.AGENT_FILES)
                    scope = CR.Scope(how="default", globs=tuple(globs))
                problems, checked, scope = CR.check(
                    g, nav.ws.root, globs, list(exclude or []) + list(st.check_refs_exclude),
                    self.cache_dir, skip_archives=True,
                    other_versions=st.check_refs_versions, scope=scope, since=since.strip())
            out = render.index_note(stats) + CR.render(problems, checked, scope, offset,
                                                       limit, fmt)
            out += "".join(f"\nconfig warning: {w}" for w in st.warnings[:5])
            return out, set()
        return body

    def _b_doctor(self) -> Body:
        def body(nav: Navigator):
            from . import doctor as DR
            report = DR.run(nav.ws, self._settings_if_indexed(nav), self.cache_dir,
                            nav.ws.freshness())
            return DR.render(report), set()
        return body

    def _b_concepts(self, query: str, kind: str = "", scope: str = "",
                    include_tests: bool = False, offset: int = 0, limit: int = 20,
                    include_generated: bool = False) -> Body:
        def body(nav: Navigator):
            if not query.strip():
                raise QueryError("query: words describing what you look for")
            with self._graph(nav) as (g, stats):
                st = self.settings(nav.ws.root, g)
                rows = g.concepts(query, "" if kind == "any" else kind, scope, include_tests,
                                  offset + limit + 1, include_generated, st.concepts_exclude)
                terms = G.concept_terms(query)
                page_rows = rows[offset:offset + limit]
                hints = {r[0]: g.member_hints(r[0], terms) for r in page_rows[:10]
                         if S.is_type(r[0])}
            # The first sentence of each one's javadoc, from its shard (one search for all).
            docs = {}
            try:
                for sym, d in nav._definitions_of({r[0] for r in page_rows}).items():
                    text = " ".join(x.strip() for x in (d.info.documentation if d.info else [])
                                    if x.strip())
                    # One line: before the tags and lists, no markup, whitespace flattened.
                    text = re.split(r"(?:^|\s)@[a-z]+\b|<(?:ul|ol|p|pre|table)\b", text,
                                    maxsplit=1)[0]
                    text = " ".join(re.sub(r"<[^>]+>|\{@\w+\s+([^}]*)\}", r"\1", text).split())
                    if text:
                        docs[sym] = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0][:160]
            except (QueryError, OSError):
                pass
            return render.index_note(stats) + render.concepts(
                query, terms, page_rows, docs, offset, limit, len(rows) > offset + limit,
                hints), \
                {r[2][0] for r in page_rows}
        return body

    def _b_census(self, scope: str = "", limit: int = 50, category: str = "annotations",
                  offset: int = 0) -> Body:
        def body(nav: Navigator):
            with self._graph(nav) as (g, stats):
                if category == "entities":
                    st = self.settings(nav.ws.root, g)
                    if not st.entity_annotations:
                        raise QueryError("no persistence profile active (jpa, jdo or one of "
                                         "yours): nothing marks entities")
                    rows = g.entities(st.entity_annotations, st.table, nav.ws.root)
                    if scope:
                        rows = [r for r in rows if fnmatch.fnmatch(r[1], scope)]
                    return render.index_note(stats) + render.entities(rows, offset, limit), set()
                rows = g.annotation_census(scope, max(1, limit))
            return render.index_note(stats) + render.census(rows), set()
        return body

    def _b_cache_status(self) -> Body:
        def body(nav: Navigator):
            try:
                st = Index(nav.ws, self.cache_dir, self.index_external).status()
            except CacheError as e:
                raise QueryError(str(e)) from e
            return render.cache_status(st), set()
        return body

    def _b_index_status(self) -> Body:
        def body(nav: Navigator):
            st = X.index_status(nav)
            st.settings = self._settings_if_indexed(nav)
            return render.status(st, nav.ws.root), set()
        return body

    def _settings_if_indexed(self, nav: Navigator) -> CFG.Settings:
        """Settings with the profiles chosen from the project's annotations when the auxiliary
        index already exists (a status never builds or updates it)."""
        try:
            ix = Index(nav.ws, self.cache_dir, self.index_external, self.build_workers)
            if not ix._needs_scratch():  # opening one for another schema would discard it
                g = Graph(ix)
                try:
                    return self.settings(nav.ws.root, g)
                finally:
                    g.close()
        except (CacheError, IndexError_, sqlite3.Error):
            pass
        st = self.settings(nav.ws.root)
        if not st.source:
            st.source = ("chosen from the project's annotations once the auxiliary index "
                         "exists (the first impact, dead_code, census… builds it)")
        return st

    # --- tools ------------------------------------------------------------------------------

    def find(self, name: str, project_root: str | None = None, limit: int = 50,
             offset: int = 0, match: str = "auto") -> str:
        return self._run(project_root, self._b_find(name, offset, limit, match))

    def references(self, symbol: str, project_root: str | None = None, offset: int = 0,
                   limit: int = DEFAULT_LIMIT, include_definitions: bool = False,
                   exclude_imports: bool | None = None, by_name: bool = False,
                   by_name_globs: list[str] | None = None, scope: str = "") -> str:
        return self._run(project_root, self._b_references(
            symbol, offset, limit, include_definitions, exclude_imports, by_name, by_name_globs,
            scope))

    def definition(self, symbol: str, project_root: str | None = None) -> str:
        return self._run(project_root, self._b_definition(symbol))

    def implementations(self, symbol: str, project_root: str | None = None,
                        direct_only: bool = False, limit: int = DEFAULT_LIMIT * 2,
                        offset: int = 0) -> str:
        return self._run(project_root, self._b_implementations(symbol, direct_only, offset, limit))

    def supertypes(self, symbol: str, project_root: str | None = None) -> str:
        return self._run(project_root, self._b_supertypes(symbol))

    def outline(self, file: str, project_root: str | None = None, offset: int = 0,
                limit: int = 500) -> str:
        return self._run(project_root, self._b_outline(file, offset, limit))

    def callers(self, symbol: str, project_root: str | None = None, depth: int = 1,
                include_overridden: bool = False, max_nodes: int = 200, offset: int = 0,
                limit: int = DEFAULT_LIMIT, scope: str = "") -> str:
        return self._run(project_root, self._b_callers(symbol, depth, include_overridden,
                                                       max_nodes, offset, limit, scope))

    def callees(self, symbol: str, project_root: str | None = None,
                include_fields: bool = False, include_types: bool = False, offset: int = 0,
                limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_callees(symbol, include_fields, include_types,
                                                       offset, limit))

    def compare(self, other_root: str = "", project_root: str | None = None, scope: str = "",
                api_only: bool = True, limit: int = 50, all_worktrees: bool = False) -> str:
        return self._run(project_root, self._b_compare(other_root, scope, api_only, limit,
                                                       all_worktrees), foot_first=True)

    def persistence(self, symbol: str = "", project_root: str | None = None, scope: str = "",
                    offset: int = 0, limit: int = 100) -> str:
        return self._run(project_root, self._b_persistence(symbol, scope, offset, limit))

    def check_refs(self, project_root: str | None = None, files: list[str] | None = None,
                   exclude: list[str] | None = None, offset: int = 0, limit: int = 200,
                   fmt: str = "text", since: str = "") -> str:
        return self._run(project_root, self._b_check_refs(files, exclude, offset, limit, fmt,
                                                          since))

    def doctor(self, project_root: str | None = None) -> str:
        return self._run(project_root, self._b_doctor())

    def concepts(self, query: str, project_root: str | None = None, kind: str = "",
                 scope: str = "", include_tests: bool = False, offset: int = 0,
                 limit: int = 20, include_generated: bool = False) -> str:
        return self._run(project_root, self._b_concepts(query, kind, scope, include_tests,
                                                        offset, limit, include_generated))

    def refactor(self, plan=None, project_root: str | None = None, dry_run: bool = False,
                 plan_id: str = "", verify: bool = False) -> str:
        return self._run(project_root, self._b_refactor(plan, dry_run, plan_id, verify))

    def steps(self, query: str = "", project_root: str | None = None, check: bool = False,
              offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_steps(query, check, offset, limit))

    def context(self, symbol: str, project_root: str | None = None,
                token_budget: int = 4000) -> str:
        return self._run(project_root, self._b_context(symbol, token_budget), foot_first=True)

    def source(self, symbol: str, project_root: str | None = None, max_lines: int = 200) -> str:
        return self._run(project_root, self._b_source(symbol, max_lines))

    def members(self, symbol: str, project_root: str | None = None, inherited: bool = True,
                offset: int = 0, limit: int = 300) -> str:
        return self._run(project_root, self._b_members(symbol, inherited, offset, limit))

    def annotated(self, annotation: str, project_root: str | None = None, offset: int = 0,
                  limit: int = DEFAULT_LIMIT, scope: str = "") -> str:
        return self._run(project_root, self._b_annotated(annotation, offset, limit, scope))

    def typed(self, symbol: str, project_root: str | None = None,
              categories: list[str] | None = None, offset: int = 0,
              limit: int = DEFAULT_LIMIT, scope: str = "",
              modifiers: list[str] | None = None, subtypes: bool = False) -> str:
        return self._run(project_root, self._b_typed(symbol, categories, offset, limit, scope,
                                                     modifiers, subtypes))

    def family(self, symbol: str, project_root: str | None = None, offset: int = 0,
               limit: int = DEFAULT_LIMIT, show_uses: bool = True) -> str:
        return self._run(project_root, self._b_family(symbol, offset, limit, show_uses))

    def overloads(self, symbol: str, project_root: str | None = None, offset: int = 0,
                  limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_overloads(symbol, offset, limit))

    def tests(self, symbol: str, project_root: str | None = None, transitive: bool = False,
              depth: int = 3) -> str:
        return self._run(project_root, self._b_tests(symbol, transitive, depth))

    def impact(self, symbol: str, project_root: str | None = None, depth: int = 3,
               include_overridden: bool = True, group_by: str = "module", offset: int = 0,
               limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_impact(symbol, depth, include_overridden,
                                                      group_by, offset, limit))

    def path(self, source: str, target: str, project_root: str | None = None,
             max_depth: int = 6, limit: int = 3) -> str:
        return self._run(project_root, self._b_path(source, target, max_depth, limit))

    def changed(self, project_root: str | None = None, base: str = "HEAD", staged: bool = False,
                paths: list[str] | None = None, depth: int = 3) -> str:
        return self._run(project_root, self._b_changed(base, staged, paths, depth))

    def deps(self, project_root: str | None = None, level: str = "module", scope: str = "",
             target: str = "", cycles: bool = True, sites: bool = False, offset: int = 0,
             limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_deps(level, scope, target, cycles, sites, offset,
                                                    limit))

    def dead_code(self, project_root: str | None = None, scope: str = "",
                  public_is_api: bool = False, include_tests: bool = False, offset: int = 0,
                  limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_dead_code(scope, public_is_api, include_tests,
                                                         offset, limit))

    def census(self, project_root: str | None = None, scope: str = "", limit: int = 50,
               category: str = "annotations", offset: int = 0) -> str:
        return self._run(project_root, self._b_census(scope, limit, category, offset))

    def cache_status(self, project_root: str | None = None) -> str:
        return self._run(project_root, self._b_cache_status())

    def index_status(self, project_root: str | None = None) -> str:
        return self._run(project_root, self._b_index_status())

    def setup(self, project_root: str | None = None, include_template: bool = True) -> str:
        try:
            root = self._root(project_root)
        except WorkspaceError as e:
            return f"error: {e}"
        text = SETUP.render(SETUP.diagnose(root, self.shards_dir), include_template)
        st = self.settings(root)
        if not st.source:
            st.source = "chosen from the project's annotations once the auxiliary index exists"
        return text + "\n\nscipq configuration:\n" + "\n".join(
            "  " + x for x in render.settings_lines(st)) + (
            "" if st.files else f"\n  no {CFG.REPO_FILE} in the checkout (`scipq-mcp init-config` "
            "prints one to start from)")

    BATCH_TOOLS = {
        "find_symbols": "_b_find", "references": "_b_references", "definition": "_b_definition",
        "implementations": "_b_implementations", "supertypes": "_b_supertypes",
        "outline": "_b_outline", "callers": "_b_callers", "callees": "_b_callees",
        "source": "_b_source", "members": "_b_members", "annotated": "_b_annotated",
        "typed": "_b_typed", "family": "_b_family", "overloads": "_b_overloads",
        "tests": "_b_tests", "index_status": "_b_index_status",
        "impact": "_b_impact", "path": "_b_path", "changed": "_b_changed", "deps": "_b_deps",
        "dead_code": "_b_dead_code", "census": "_b_census", "concepts": "_b_concepts", "cache_status": "_b_cache_status",
        "context": "_b_context", "compare": "_b_compare", "steps": "_b_steps",
    }

    def batch(self, queries: list[dict[str, Any]], project_root: str | None = None) -> str:
        """Several queries over the same checkout in one call; one freshness check."""
        bodies = []
        for k, q in enumerate(queries, 1):
            tool = q.get("tool", "")
            args = dict(q.get("args") or {})
            args.pop("project_root", None)
            method = self.BATCH_TOOLS.get(tool)
            if method is None:
                bodies.append(_const(f"### {k}. {tool}\nerror: unknown tool {tool!r}; one of "
                                     + ", ".join(sorted(self.BATCH_TOOLS))))
                continue
            try:
                inner = getattr(self, method)(**args)
            except TypeError as e:
                bodies.append(_const(f"### {k}. {tool}\nerror: {e}"))
                continue
            bodies.append(_titled(f"### {k}. {tool} {_brief(args)}", inner))
        if not bodies:
            return "error: no queries"
        return self._run(project_root, *bodies)


def _fqcn(symbol: str) -> str:
    top, pkg = S.top_level_type_name(symbol), S.package_of(symbol)
    return f"{pkg}.{top}" if pkg else top


def _drop_imports(nav: Navigator, refs: list) -> list:
    """References that are not on an import line. Imports come first in a file, so only the
    lines up to the last import-looking reference of each file are read."""
    by_file: dict[str, list] = {}
    for r in refs:
        by_file.setdefault(r.location.source, []).append(r)
    kept = []
    for source, rs in by_file.items():
        # A reference on an import line has no enclosing definition.
        maybe = {r.location.range.start_line for r in rs if not r.container}
        texts = nav.ws.source_lines(source, maybe) if maybe else {}
        kept.extend(r for r in rs if r.container or
                    not texts.get(r.location.range.start_line, "").startswith("import "))
    kept.sort(key=lambda r: (r.location.source, r.location.range.start_line,
                             r.location.range.start_char))
    return kept


def _const(text: str) -> Body:
    return lambda nav: (text, set())


def _titled(title: str, inner: Body) -> Body:
    """One query of a batch: whatever it raises (batch arguments skip the validation single
    tools get, e.g. depth="2") becomes its own error line, never the whole batch's."""
    def body(nav: Navigator):
        try:
            text, srcs = inner(nav)
        except AmbiguousName as e:
            text, srcs = render.candidates(e.resolution), set()
        except (QueryError, WorkspaceError, ValueError, L.LibraryBusy) as e:
            text, srcs = f"error: {e}", set()
        except Exception as e:  # noqa: BLE001 - reported to the caller, per query
            text, srcs = f"error: {type(e).__name__}: {e}", set()
        return f"{title}\n{text}", srcs
    return body


def _brief(args: dict[str, Any]) -> str:
    return " ".join(f"{v}" if k in ("symbol", "name", "file", "annotation") else f"{k}={v}"
                    for k, v in args.items())

"""The operations exposed as MCP tools (and by the debug CLI), returning compact text.

Each call opens the project afresh: shards are listed, prefiltered and decoded for that call
only. The freshness check (sources newer than their shard) runs in parallel with the query,
and its warning is appended to every answer.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import sqlite3
import threading
from collections import OrderedDict
from typing import Any, Callable, TypeVar

from . import extras as X
from . import graph as G
from . import render
from . import setup as SETUP
from . import symbols as S
from . import config as CFG
from .cachedir import CacheError
from .graph import Graph, GraphError
from .index import Index, IndexError_
from .queries import AmbiguousName, Navigator, QueryError
from .workspace import Freshness, Workspace, WorkspaceError, find_project_root

DEFAULT_LIMIT = 100
FRESHNESS_MODES = ("full", "results", "off")
T = TypeVar("T")
Body = Callable[[Navigator], "tuple[str, set[str]]"]

NO_SHARDS_HINT = ("This project has not been compiled with the scip-javac plugin yet, so there "
                  "is nothing to navigate. Call the `setup` tool: it inspects the build and "
                  "returns the exact steps (scipq-mcp never changes the build or runs it).")


class Service:
    def __init__(self, default_root: str | None = None, shards_dir: str | None = None,
                 freshness: str = "full", cache_dir: str | None = None,
                 index_external: str = "all", config_file: str | None = None):
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
        return Workspace(self._root(project_root), self.shards_dir)

    def _run(self, project_root: str | None, *bodies: Body) -> str:
        try:
            ws = self.workspace(project_root)
        except WorkspaceError as e:
            msg = str(e)
            return f"error: {msg}" + (f"\n{NO_SHARDS_HINT}" if "no SCIP shards" in msg else "")
        ws.files()
        check = ws.start_freshness() if self.freshness == "full" else None
        texts, sources = [], set()
        nav = Navigator(ws)
        for body in bodies:
            try:
                text, srcs = body(nav)
            except AmbiguousName as e:
                text, srcs = render.candidates(e.resolution), set()
            except (QueryError, WorkspaceError, ValueError) as e:
                text, srcs = f"error: {e}", set()
            texts.append(text)
            sources |= srcs
        fresh: Freshness | None = None
        if check is not None:
            fresh = check.result()
        elif self.freshness == "results":
            fresh = ws.freshness_of(sources)
        foot = render.freshness(fresh, sources)
        text = texts[0] if len(texts) == 1 else "\n\n".join(texts)
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
        t = threading.Thread(target=lambda: box.append(nav.resolve(name)), daemon=True)
        t.start()
        try:
            result = query(guess)
        except QueryError:
            result = None
        t.join()
        r = box[0]
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
                      by_name: bool = False, by_name_globs: list[str] | None = None) -> Body:
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
            text = render.references(nav.ws, sym, refs, defs, offset, limit, include_definitions)
            if by_name:
                names = X.property_names(sym)
                exclude = {(r.location.source, r.location.line) for r in refs}
                exclude |= {(d.location.source, d.location.line) for d in defs}
                globs = by_name_globs or self.settings(nav.ws.root).by_name_globs
                hits = X.uses_by_name(nav.ws.root, names, globs, exclude)
                text += "\n" + render.by_name(hits, names)
            return note + text, {r.location.source for r in refs}
        return body

    def _b_definition(self, symbol: str) -> Body:
        def body(nav: Navigator):
            sym, note, d = self._resolved(nav, symbol, nav.definition)
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
                   max_nodes: int = 200, offset: int = 0, limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            d = max(1, min(depth, 5))
            sym, note, root = self._resolved(
                nav, symbol, lambda s: nav.callers(s, d, include_overridden, max(1, max_nodes)))
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
            return note + render.source(src, sym), (
                {src.definition.location.source} if src else set())
        return body

    def _b_members(self, symbol: str, inherited: bool = True, offset: int = 0,
                   limit: int = 300) -> Body:
        def body(nav: Navigator):
            sym, note, (groups, external) = self._resolved(
                nav, symbol, lambda s: X.members(nav, s, inherited))
            srcs = {g.definition.location.source for g in groups if g.definition}
            return note + render.members(sym, groups, external, offset, limit), srcs
        return body

    def _b_annotated(self, annotation: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            sym, note, items = self._resolved(nav, annotation, lambda s: X.annotated(nav, s))
            return (note + render.annotated(sym, items, offset, limit),
                    {a.location.source for a in items})
        return body

    def _b_typed(self, symbol: str, categories: list[str] | None = None, offset: int = 0,
                 limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            sym, note, uses = self._resolved(nav, symbol, lambda s: X.typed(nav, s))
            return (note + render.typed(sym, uses, categories, offset, limit),
                    {u.location.source for u in uses})
        return body

    def _b_family(self, symbol: str, offset: int = 0, limit: int = DEFAULT_LIMIT,
                  show_uses: bool = True) -> Body:
        def body(nav: Navigator):
            sym, note, fam = self._resolved(nav, symbol, lambda s: X.family(nav, s))
            srcs = {r.location.source for m in fam for r in m.uses}
            return note + render.family(sym, fam, nav.ws, offset, limit, show_uses), srcs
        return body

    def _b_overloads(self, symbol: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            r = nav.resolve(symbol)
            if r.symbol is None:
                # A name matching several overloads of one method is what this tool is for.
                syms = sorted({d.symbol for d in r.candidates})
                if syms and all(S.is_method(x) and S.parent(x) == S.parent(syms[0])
                                and S.name_of(x) == S.name_of(syms[0]) for x in syms):
                    sym, note = syms[0], f"-- {symbol} → {len(syms)} overloads\n"
                    return note + render.overloads(sym, X.overloads(nav, sym), nav.ws, offset,
                                                   limit), set()
            sym, note, items = self._resolved(nav, symbol, lambda s: X.overloads(nav, s))
            srcs = {r.location.source for m in items for r in m.uses}
            return note + render.overloads(sym, items, nav.ws, offset, limit), srcs
        return body

    def _b_tests(self, symbol: str, transitive: bool = False, depth: int = 3) -> Body:
        def body(nav: Navigator):
            gradle = "./gradlew" if os.path.exists(os.path.join(nav.ws.root, "gradlew")) \
                else "gradle"
            template = self.settings(nav.ws.root).tests_filter
            if transitive:
                sym, note = self._resolve(nav, symbol)
                with self._graph(nav) as (g, stats):
                    nodes, _trunc = g.impact(sym, max(1, min(depth, 8)))
                text = render.tests_transitive(sym, nodes, depth, X.test_task, gradle, template)
                return render.index_note(stats) + note + text, {n.source for n in nodes if n.test}
            sym, note, uses = self._resolved(nav, symbol, lambda s: X.tests_using(nav, s))
            return note + render.tests(sym, uses, X.test_task, gradle, template), \
                {t.source for t in uses}
        return body

    # --- v2: queries over the auxiliary index -----------------------------------------------

    @contextlib.contextmanager
    def _graph(self, nav: Navigator):
        try:
            ix = Index(nav.ws, self.cache_dir, self.index_external)
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
                nodes, trunc = g.impact(sym, d, include_overridden)
            text = render.impact(sym, nodes, trunc, d, group_by, offset, limit)
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

    def _b_changed(self, base: str = "HEAD", staged: bool = False,
                   paths: list[str] | None = None, depth: int = 2) -> Body:
        def body(nav: Navigator):
            with self._graph(nav) as (g, stats):
                try:
                    res = g.changed(nav.ws.root, base, staged, paths)
                except GraphError as e:
                    raise QueryError(str(e)) from e
                nodes: dict[str, object] = {}
                for sym in res["definitions"]:
                    for n in g.impact(sym, max(1, min(depth, 6)))[0]:
                        nodes.setdefault(n.symbol, n)
            gradle = "./gradlew" if os.path.exists(os.path.join(nav.ws.root, "gradlew")) \
                else "gradle"
            tests: OrderedDict[str, set[str]] = OrderedDict()
            for n in nodes.values():
                if n.test:
                    top = S.top_level_type_name(n.symbol)
                    pkg = S.package_of(n.symbol)
                    tests.setdefault(n.module, set()).add(f"{pkg}.{top}" if pkg else top)
            tests_line = ""
            template = self.settings(nav.ws.root).tests_filter
            if tests and template:
                tests_line = "tests to run (scipq-mcp does not):\n  " + "\n  ".join(
                    render.format_test_filter(template, mod, sorted(names))
                    for mod, names in tests.items())
            elif tests:
                parts = []
                for mod, names in tests.items():
                    task = "test" if mod in (".", "") else ":" + mod.replace("/", ":") + ":test"
                    parts.append(f"{gradle} {task} " + " ".join(f"--tests '{c}'"
                                                              for c in sorted(names)))
                tests_line = "tests to run (scipq-mcp does not):\n  " + "\n  ".join(parts)
            text = render.changed(res, base if not staged else f"{base} (staged)",
                                  list(nodes.values()), tests_line)
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
                    rows = g.uses_between(scope, target, max(1, limit))
                    return render.index_note(stats) + render.uses_between(scope, target, rows,
                                                                          limit), set()
                edges = g.deps(level, scope, target)
                cyc = G.cycles(edges.keys()) if cycles else None
            return render.index_note(stats) + render.deps(edges, level, cyc, offset, limit), set()
        return body

    def _b_dead_code(self, scope: str = "", public_is_api: bool = False,
                     include_tests: bool = False, offset: int = 0,
                     limit: int = DEFAULT_LIMIT) -> Body:
        def body(nav: Navigator):
            with self._graph(nav) as (g, stats):
                st = self.settings(nav.ws.root, g)
                rows = g.dead_code(scope, public_is_api, include_tests,
                                   entry_annotations=st.entry_annotations,
                                   entry_supertypes=st.entry_supertypes,
                                   entry_type_names=st.entry_type_names,
                                   by_name_globs=st.by_name_globs, root=nav.ws.root)
            note = f"-- profiles: {', '.join(st.profiles) or 'none'} ({st.source or 'none'})\n"
            return render.index_note(stats) + note + render.dead_code(rows, offset, limit), set()
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
            st.settings = self.settings(nav.ws.root)
            return render.status(st, nav.ws.root), set()
        return body

    # --- tools ------------------------------------------------------------------------------

    def find(self, name: str, project_root: str | None = None, limit: int = 50,
             offset: int = 0, match: str = "auto") -> str:
        return self._run(project_root, self._b_find(name, offset, limit, match))

    def references(self, symbol: str, project_root: str | None = None, offset: int = 0,
                   limit: int = DEFAULT_LIMIT, include_definitions: bool = False,
                   exclude_imports: bool | None = None, by_name: bool = False,
                   by_name_globs: list[str] | None = None) -> str:
        return self._run(project_root, self._b_references(
            symbol, offset, limit, include_definitions, exclude_imports, by_name, by_name_globs))

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
                limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_callers(symbol, depth, include_overridden,
                                                       max_nodes, offset, limit))

    def callees(self, symbol: str, project_root: str | None = None,
                include_fields: bool = False, include_types: bool = False, offset: int = 0,
                limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_callees(symbol, include_fields, include_types,
                                                       offset, limit))

    def source(self, symbol: str, project_root: str | None = None, max_lines: int = 200) -> str:
        return self._run(project_root, self._b_source(symbol, max_lines))

    def members(self, symbol: str, project_root: str | None = None, inherited: bool = True,
                offset: int = 0, limit: int = 300) -> str:
        return self._run(project_root, self._b_members(symbol, inherited, offset, limit))

    def annotated(self, annotation: str, project_root: str | None = None, offset: int = 0,
                  limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_annotated(annotation, offset, limit))

    def typed(self, symbol: str, project_root: str | None = None,
              categories: list[str] | None = None, offset: int = 0,
              limit: int = DEFAULT_LIMIT) -> str:
        return self._run(project_root, self._b_typed(symbol, categories, offset, limit))

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
                paths: list[str] | None = None, depth: int = 2) -> str:
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
        return SETUP.render(SETUP.diagnose(root, self.shards_dir), include_template)

    BATCH_TOOLS = {
        "find_symbols": "_b_find", "references": "_b_references", "definition": "_b_definition",
        "implementations": "_b_implementations", "supertypes": "_b_supertypes",
        "outline": "_b_outline", "callers": "_b_callers", "callees": "_b_callees",
        "source": "_b_source", "members": "_b_members", "annotated": "_b_annotated",
        "typed": "_b_typed", "family": "_b_family", "overloads": "_b_overloads",
        "tests": "_b_tests", "index_status": "_b_index_status",
        "impact": "_b_impact", "path": "_b_path", "changed": "_b_changed", "deps": "_b_deps",
        "dead_code": "_b_dead_code", "census": "_b_census", "cache_status": "_b_cache_status",
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
    def body(nav: Navigator):
        try:
            text, srcs = inner(nav)
        except AmbiguousName as e:
            text, srcs = render.candidates(e.resolution), set()
        except (QueryError, WorkspaceError, ValueError) as e:
            text, srcs = f"error: {e}", set()
        return f"{title}\n{text}", srcs
    return body


def _brief(args: dict[str, Any]) -> str:
    return " ".join(f"{v}" if k in ("symbol", "name", "file", "annotation") else f"{k}={v}"
                    for k, v in args.items())

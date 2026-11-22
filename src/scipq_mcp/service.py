"""The operations exposed as MCP tools (and by the debug CLI), returning compact text.

Each call opens the project afresh: shards are listed, prefiltered and decoded for that call
only. The freshness check (sources newer than their shard) runs in a thread while the query
runs, and its warning is appended to every answer.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, TypeVar

from . import render
from . import symbols as S
from .queries import AmbiguousName, Navigator, QueryError
from .workspace import Freshness, Workspace, WorkspaceError, find_project_root

DEFAULT_LIMIT = 100
FRESHNESS_MODES = ("full", "results", "off")
T = TypeVar("T")


class Service:
    def __init__(self, default_root: str | None = None, shards_dir: str | None = None,
                 freshness: str = "full"):
        """freshness: "full" (every shard against its source, ~0.1 s on 10k files, run in
        parallel with the query), "results" (only the files in the answer) or "off"."""
        if freshness not in FRESHNESS_MODES:
            raise ValueError(f"freshness must be one of {FRESHNESS_MODES}")
        self.default_root = default_root
        self.shards_dir = shards_dir
        self.freshness = freshness

    # --- plumbing ---------------------------------------------------------------------------

    def workspace(self, project_root: str | None) -> Workspace:
        root = project_root or self.default_root or os.environ.get("SCIPQ_PROJECT_ROOT") \
            or os.getcwd()
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            raise WorkspaceError(f"project_root {root!r} is not a directory")
        return Workspace(find_project_root(root), self.shards_dir)

    def _run(self, project_root: str | None,
             body: Callable[[Navigator], tuple[str, set[str]]]) -> str:
        try:
            ws = self.workspace(project_root)
        except WorkspaceError as e:
            return f"error: {e}"
        ws.files()
        check = ws.start_freshness() if self.freshness == "full" else None
        try:
            text, sources = body(Navigator(ws))
        except AmbiguousName as e:
            text, sources = render.candidates(e.resolution), set()
        except (QueryError, WorkspaceError) as e:
            text, sources = f"error: {e}", set()
        fresh: Freshness | None = None
        if check is not None:
            fresh = check.result()
        elif self.freshness == "results":
            fresh = ws.freshness_of(sources)
        foot = render.freshness(fresh, sources)
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
        result = query(guess)
        t.join()
        r = box[0]
        if r.symbol is None:
            raise AmbiguousName(r)
        if r.symbol != guess:
            result = query(r.symbol)
        return r.symbol, f"-- {r.note}\n", result

    # --- tools ------------------------------------------------------------------------------

    def find(self, name: str, project_root: str | None = None, limit: int = 50) -> str:
        def body(nav: Navigator):
            found = nav.find(name)
            return render.find(name, found, limit), {d.location.source for d in found[:limit]}
        return self._run(project_root, body)

    def references(self, symbol: str, project_root: str | None = None, offset: int = 0,
                   limit: int = DEFAULT_LIMIT, include_definitions: bool = False) -> str:
        def body(nav: Navigator):
            sym, note, (refs, defs) = self._resolved(nav, symbol, nav.references)
            text = render.references(nav.ws, sym, refs, defs, max(0, offset), max(1, limit),
                                     include_definitions)
            return note + text, {r.location.source for r in refs}
        return self._run(project_root, body)

    def definition(self, symbol: str, project_root: str | None = None) -> str:
        def body(nav: Navigator):
            sym, note, d = self._resolved(nav, symbol, nav.definition)
            return note + render.definition(d, sym), {d.location.source} if d else set()
        return self._run(project_root, body)

    def implementations(self, symbol: str, project_root: str | None = None,
                        direct_only: bool = False, limit: int = DEFAULT_LIMIT * 2) -> str:
        def body(nav: Navigator):
            sym, note, impls = self._resolved(nav, symbol, nav.implementations)
            text = render.implementations(sym, impls, direct_only, max(1, limit))
            return note + text, {i.definition.location.source for i in impls}
        return self._run(project_root, body)

    def supertypes(self, symbol: str, project_root: str | None = None) -> str:
        def body(nav: Navigator):
            sym, note, (d, supers) = self._resolved(nav, symbol, nav.supertypes)
            srcs = {s.definition.location.source for s in supers if s.definition}
            return note + render.supertypes(sym, d, supers), srcs
        return self._run(project_root, body)

    def outline(self, file: str, project_root: str | None = None) -> str:
        def body(nav: Navigator):
            note = ""
            source = file
            if not (file.endswith(".java") or "/" in file or os.sep in file):
                sym, note, d = self._resolved(nav, file, nav.definition)
                if d is None:
                    raise QueryError(f"{sym} is not defined in this project")
                source = d.location.source
            source = nav._normalize_source(source)
            defs = nav.outline(source)
            return note + render.outline(source, defs), {source}
        return self._run(project_root, body)

    def callers(self, symbol: str, project_root: str | None = None, depth: int = 1,
                include_overridden: bool = False, max_nodes: int = 200) -> str:
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
            return note + render.callers(root, d), srcs
        return self._run(project_root, body)

    def callees(self, symbol: str, project_root: str | None = None,
                include_fields: bool = False, include_types: bool = False) -> str:
        def body(nav: Navigator):
            sym, note, (d, out) = self._resolved(
                nav, symbol, lambda s: nav.callees(s, include_fields, include_types))
            return note + render.callees(sym, d, out), {d.location.source} if d else set()
        return self._run(project_root, body)

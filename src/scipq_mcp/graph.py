"""Queries over the auxiliary index (v2): transitive callers, call paths, affected tests,
changed code, dependencies between modules or packages, dead-code candidates and censuses."""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
import shutil
import sqlite3
import subprocess
import tempfile
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field

from . import symbols as S
from .index import FLAG_GENERATED, REL_IMPLEMENTATION, Index


class GraphError(Exception):
    pass


# A false "dead" costs more than an extra "maybe used": dead_code looks for names in more files
# than references(by_name) does.
DEAD_CODE_GLOBS = ("*.java", "*.xml", "*.jrxml", "*.properties", "*.html", "*.jsp", "*.ftl",
                   "*.vm", "*.yml", "*.yaml", "*.json", "*.feature", "*.sql", "*.kt", "*.groovy")


@dataclass
class Node:
    symbol: str
    depth: int
    via: str                      # the symbol it calls (one step closer to the target)
    source: str = ""
    line: int = 0                 # 1-based, of the definition
    module: str = ""
    test: bool = False
    sites: list[int] = field(default_factory=list)  # 1-based lines where it calls `via`
    task_dir: str = ""            # "<module>/<task>" of its shard (e.g. "core/compileTestJava")
    inner: bool = False           # every call is inside a local or anonymous class


@dataclass
class Entry:
    """A reached definition someone starts from outside (a screen, an endpoint, a job…)."""
    kind: str
    node: Node
    detail: str
    on_type: bool = False       # identified by its type (supertype, name, descriptor)


_HTTP_VERBS = {v: v for v in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")}
# Kinds that run once (startup hooks): a lambda they register runs later, not in them.
_AT_START = {"startup"}
# Annotations of a type that hold its routes: its methods are endpoints only with a route.
_ROUTE_HOLDERS = {"Path", "RequestMapping", "RestController", "Controller"}
_MAPPINGS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
             "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": ""}


def module_of(task_dir: str) -> str:
    """"core/compileJava" -> "core"; "a/b/compileTestJava" -> "a/b"; "." -> "."."""
    head, _, _task = task_dir.rpartition("/")
    return head or task_dir


class Graph:
    def __init__(self, index: Index):
        self.index = index
        self.con: sqlite3.Connection = index.connect()
        self._shards: dict[int, tuple[str, str, int]] | None = None

    def close(self) -> None:
        self.con.close()

    # names in text (FTS5, incremental by mtime) ----------------------------------------------

    def _text_index(self, root: str, globs: list[str]) -> None:
        """Identifiers inside .java string literals (and, apart, those in annotation lines) and
        every identifier of the other text files, per file, kept up to date by mtime/size."""
        con = self.con
        con.executescript(TEXT_SCHEMA)
        files = candidate_files(root, globs, [])
        known = {p: (fid, mt, size) for fid, p, mt, size in con.execute(
            "SELECT id, path, mtime_ns, size FROM text_files")}
        on_disk = set(files)
        for p, (fid, _mt, _size) in known.items():
            if p not in on_disk:
                con.execute("DELETE FROM text_words WHERE rowid = ?", (fid,))
                con.execute("DELETE FROM text_files WHERE id = ?", (fid,))
        for p in files:
            try:
                st = os.stat(os.path.join(root, p))
            except OSError:
                continue
            old = known.get(p)
            if old is not None and old[1:] == (st.st_mtime_ns, st.st_size):
                continue
            if old is not None:
                con.execute("DELETE FROM text_words WHERE rowid = ?", (old[0],))
                con.execute("DELETE FROM text_files WHERE id = ?", (old[0],))
            try:
                with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            strings, annots, other = _tokens(p, text)
            fid = con.execute("INSERT INTO text_files (path, mtime_ns, size) VALUES (?, ?, ?)",
                              (p, st.st_mtime_ns, st.st_size)).lastrowid
            con.execute("INSERT INTO text_words (rowid, java_strings, java_annotations, other) "
                        "VALUES (?, ?, ?, ?)", (fid, strings, annots, other))
        con.commit()

    def names_seen(self, root: str, names: list[str], properties: set[str], globs: list[str],
                   exclude: list[str]) -> dict[str, str]:
        """{name: a file it appears in} — like names_in_strings, from the text index."""
        with self.index._lock():  # another session may be refreshing it too
            self._text_index(root, list(globs))
        excl = list(exclude) + list(DEFAULT_BY_NAME_EXCLUDE)
        found: dict[str, str] = {}
        for name in names:
            cols = "{java_annotations other}" if name in properties else "{java_strings other}"
            q = f'{cols} : "{name}"'
            try:
                rows = self.con.execute(
                    "SELECT f.path, w.java_strings, w.java_annotations, w.other "
                    "FROM text_words w JOIN text_files f ON f.id = w.rowid "
                    "WHERE text_words MATCH ?", (q,))
            except sqlite3.OperationalError:
                continue
            needle = f" {name} "
            for path, js, ja, other in rows:
                words = (ja if name in properties else js) or other
                if needle not in f" {words} ":  # FTS5 folds case; identifiers do not
                    continue
                base = path.rsplit("/", 1)[-1]
                if not any(fnmatch.fnmatch(base, g) for g in globs):
                    continue
                if any(fnmatch.fnmatch(path, x.replace("**", "*")) for x in excl):
                    continue
                found[name] = path
                break
        return found

    # concepts (FTS5 over each definition's words) ------------------------------------------

    def concepts(self, query: str, kind: str = "", scope: str = "", include_tests: bool = False,
                 limit: int = 30, include_generated: bool = False,
                 exclude: list[str] | None = None) -> list[tuple[str, float, tuple]]:
        """[(symbol, score, (source, line, module, test, signature))], best first: definitions
        whose own name, annotation texts, signature, labels (strings registering a type) or
        javadoc hold the query's words (as stems: prefixes, accents and plurals ignored). Every
        match counts, so the order does not depend on `limit`; the owner's name only adds to a
        member that matches by itself."""
        groups = concept_terms(query)
        if not groups:
            return []
        cond = ("" if include_tests else " AND s.test = 0") + \
            ("" if include_generated else " AND m.generated = 0")
        join = ("JOIN concept_map m ON m.rowid = {t}.rowid JOIN shards s ON s.id = m.shard_id "
                "AND s.orphan = 0")

        def scores(table: str, weights: str, q: str) -> list:
            return list(self.con.execute(
                f"SELECT m.symbol_id, bm25({table}{weights}) FROM {table} "
                f"{join.format(t=table)} WHERE {table} MATCH ?{cond}", (q,)))
        # One query per word (its family of prefixes), to know how many of the words each
        # definition holds: a definition with all of them beats one word in a name.
        total: dict[int, float] = {}
        covered: dict[int, float] = {}
        hit_groups: dict[int, set[int]] = {}
        try:
            for gi, group in enumerate(groups):
                q = " OR ".join(f'"{v}"*' for v in group)
                name_of: dict[int, float] = {}
                for sid, score in scores("concept_names", "", q):
                    name_of[sid] = max(name_of.get(sid, 0.0), -score)
                text_of: dict[int, list[float]] = {}
                said: set[int] = set()      # in an annotation's text or a label
                for sid, score in scores("concepts", ", 5.0, 2.0, 4.0", f"{{ann calls}} : ({q})"):
                    text_of.setdefault(sid, []).append(-score)
                    said.add(sid)
                for sid, score in scores("concepts", ", 5.0, 2.0, 4.0", f"sig : ({q})"):
                    text_of.setdefault(sid, []).append(-score)
                doc_of: dict[int, float] = {}
                for sid, score in scores("concept_docs", "", q):
                    doc_of[sid] = max(doc_of.get(sid, 0.0), -score)
                for sid in set(name_of) | set(text_of) | set(doc_of):
                    v = sorted(text_of.get(sid, []), reverse=True)
                    # The name weighs most; then the best text row and a capped share of the
                    # others (labels from many call sites); then the javadoc.
                    total[sid] = total.get(sid, 0.0) + 3.0 * name_of.get(sid, 0.0) + \
                        (v[0] if v else 0.0) + 0.2 * sum(v[1:4]) + 0.5 * doc_of.get(sid, 0.0)
                    # How surely the definition is about this word: its name says it, its
                    # annotations or labels, or only its signature (a parameter's type) or
                    # javadoc.
                    covered[sid] = covered.get(sid, 0.0) + (
                        1.0 if sid in name_of else 0.8 if sid in said else 0.5)
                    hit_groups.setdefault(sid, set()).add(gi)
        except sqlite3.OperationalError:
            return []
        n_terms = len(groups)
        for sid in total:
            total[sid] *= (covered[sid] / n_terms) ** 2
        names = self.names(total)
        excl = [x.replace("**", "*") for x in exclude or []]
        scored = []
        full: set[int] = set()       # all the query's words, counting its owner's
        for sid, score in total.items():
            sym = names.get(sid)
            if sym is None:
                continue
            k = S.kind_of(sym)
            if kind and {"field": "term"}.get(kind, kind) != k:
                continue
            owner = " ".join(words_of_symbol_owner(sym, packages=True))
            owner_groups = {gi for gi, grp in enumerate(groups)
                            if any(re.search(r"(?:^|\s)" + re.escape(v), owner) for v in grp)}
            in_owner = len(owner_groups)
            score *= 1 + 0.4 * in_owner / len(groups)
            if len(hit_groups.get(sid, set()) | owner_groups) == len(groups):
                full.add(sid)
            score *= (1.6 if k == "type" else 1.25 if k == "method" else
                      0.7 if re.fullmatch(r"[A-Z0-9_$]+", S.name_of(sym) or "") else 1.0)
            scored.append((score, sid, sym))
        scored.sort(key=lambda x: (-x[0], x[2]))
        # Private members are implementation details: behind the types and the API that
        # match as well, unless only they hold all the words, with their type's (the
        # leading ones only; the rest keep their order).
        head_n = max(600, 4 * limit)
        defs: dict[int, tuple] = self.definitions([sid for _s, sid, _y in scored[:head_n]])
        head = []
        for score, sid, sym in scored[:head_n]:
            sig = (defs.get(sid) or ("", 0, "", 0, ""))[4] or ""
            sig = re.sub(r"@[\w$.]+(\((?:[^()]|\([^()]*\))*\))?", " ", sig).split("(", 1)[0]
            private = re.search(r"\bprivate\b", sig) and sid not in full
            head.append((score * (0.6 if private else 1.0), sid, sym))
        head.sort(key=lambda x: (-x[0], x[2]))
        scored = head + scored[head_n:]
        out = []
        for i in range(0, len(scored), 400):
            if len(out) >= limit:
                break
            chunk = scored[i:i + 400]
            defs.update(self.definitions([sid for _s, sid, _y in chunk]))
            for score, sid, sym in chunk:
                d = defs.get(sid)
                if d is None:
                    continue
                if scope and not scope_matches(scope, S.package_of(sym), d[0]):
                    continue
                if excl and any(fnmatch.fnmatch(d[0], x) for x in excl):
                    continue
                out.append((sym, score, d))
                if len(out) >= limit:
                    break
        return out

    def member_hints(self, type_symbol: str, groups: list[list[str]], limit: int = 3
                     ) -> list[str]:
        """Members of a type or its supertypes whose names hold query words the type's own
        name does not: what a query like "the account's name" means (an inherited field)."""
        own = " ".join(words_of_symbol_owner(type_symbol + "x."))
        missing = [v for grp in groups
                   if not any(re.search(r"(?:^|\s)" + re.escape(x), own) for x in grp)
                   for v in grp]
        if not missing:
            return []
        sid = self.ids([type_symbol]).get(type_symbol)
        types = [type_symbol] + ([x for x in self.names(self.ancestors(sid)).values()
                                  if S.is_type(x)] if sid is not None else [])
        from .index import words_of
        out = []
        for t in types:
            hi = t[:-1] + chr(ord(t[-1]) + 1)
            for (member,) in self.con.execute(
                    "SELECT s.symbol FROM symbols s JOIN defs d ON d.symbol_id = s.id "
                    "WHERE s.symbol >= ? AND s.symbol < ?", (t, hi)):
                if S.parent(member) != t or S.kind_of(member) not in ("term", "method"):
                    continue
                words = words_of(S.name_of(member))
                if any(re.search(r"(?:^|\s)" + re.escape(x), words) for x in missing):
                    label = S.name_of(member) + ("()" if S.is_method(member) else "")
                    label += f" (in {S.name_of(t)})" if t != type_symbol else ""
                    if label not in out:
                        out.append(label)
                if len(out) >= limit:
                    return out
        return out

    # lookups ---------------------------------------------------------------------------------

    def shards(self) -> dict[int, tuple[str, str, int]]:
        """{shard id: (source, module/task, test flag)} for shards whose source exists."""
        if self._shards is None:
            self._shards = {sid: (src, mod, test) for sid, src, mod, test in self.con.execute(
                "SELECT id, source, module, test FROM shards WHERE orphan = 0")}
        return self._shards

    def ids(self, symbols) -> dict[str, int]:
        out = {}
        syms = list(set(symbols))
        for i in range(0, len(syms), 500):
            chunk = syms[i:i + 500]
            q = ",".join("?" * len(chunk))
            out.update(self.con.execute(f"SELECT symbol, id FROM symbols WHERE symbol IN ({q})",
                                        chunk))
        return out

    def names(self, ids) -> dict[int, str]:
        out = {}
        ids = list(set(ids))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            out.update(self.con.execute(f"SELECT id, symbol FROM symbols WHERE id IN ({q})",
                                        chunk))
        return out

    def definitions(self, ids) -> dict[int, tuple[str, int, str, int, str]]:
        """{symbol id: (source, 1-based line, module, test, signature)} of the first live
        definition of each symbol."""
        out = {}
        sh = self.shards()
        ids = list(set(ids))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for sid, shard_id, line, sig in self.con.execute(
                    f"SELECT symbol_id, shard_id, line, signature FROM defs "
                    f"WHERE symbol_id IN ({q})", chunk):
                if sid in out or shard_id not in sh:
                    continue
                src, mod, test = sh[shard_id]
                out[sid] = (src, line + 1, mod, test, sig or "")
        return out

    def ancestors(self, sid: int) -> list[int]:
        """Methods (or types) this one overrides (or extends): all levels, as recorded."""
        return [t for (t,) in self.con.execute(
            "SELECT DISTINCT target_id FROM rels WHERE symbol_id = ? AND kind = ?",
            (sid, REL_IMPLEMENTATION))]

    def _referenced_types(self, refs: list[tuple[str, str]], root: str) -> dict[str, str]:
        """{type symbol: "named in @A(attr)"} for the types written as X.class in that
        attribute of annotation A anywhere in the project (resolved by simple name, when
        one project type has it)."""
        out: dict[str, str] = {}
        sh = self.shards()
        texts: dict[str, list[str]] = {}
        for ann, attr in refs:
            aid = self.ids([ann]).get(ann)
            if aid is None:
                continue
            wanted: set[str] = set()
            for shard_id, line in self.con.execute(
                    "SELECT shard_id, line FROM annotations WHERE annotation_id = ?", (aid,)):
                if shard_id not in sh:
                    continue
                src = sh[shard_id][0]
                if src not in texts:
                    try:
                        with open(os.path.join(root, src), encoding="utf-8",
                                  errors="replace") as fh:
                            texts[src] = fh.read().split("\n")
                    except OSError:
                        texts[src] = []
                args = _annotation_args(texts[src], line, S.name_of(ann))
                rx = (r"(?:^|[(,\s])" + re.escape(attr) + r"\s*=\s*([\w$.]+)\.class") \
                    if attr != "value" else r"(?:^|[(,\s])(?:value\s*=\s*)?([\w$.]+)\.class"
                wanted |= {m.group(1).rsplit(".", 1)[-1] for m in re.finditer(rx, args)}
            for name in wanted:
                cands = [x for (x,) in self.con.execute(
                    "SELECT s.symbol FROM symbols s WHERE (s.symbol LIKE ? OR s.symbol LIKE ?) "
                    "AND EXISTS (SELECT 1 FROM defs d WHERE d.symbol_id = s.id)",
                    (f"%/{name}#", f"%#{name}#"))]
                if len(cands) == 1:
                    out.setdefault(cands[0], f"named in @{S.name_of(ann)}({attr})")
        return out

    def _project_overridden(self, ids: list[int]) -> list[tuple[int, int]]:
        """(method, project method it overrides) for these definitions."""
        out = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            out.extend(self.con.execute(
                f"SELECT r.symbol_id, r.target_id FROM rels r WHERE r.kind = ? AND "
                f"r.symbol_id IN ({q}) AND r.target_id != r.symbol_id AND EXISTS "
                f"(SELECT 1 FROM defs d WHERE d.symbol_id = r.target_id)",
                (REL_IMPLEMENTATION, *chunk)))
        return [(c, t) for c, t in out]

    def callers_of(self, targets: list[int]) -> list[tuple[int, int, int, str]]:
        """(target id, caller id, shard id, sites) of every non-empty caller."""
        out = []
        for i in range(0, len(targets), 500):
            chunk = targets[i:i + 500]
            q = ",".join("?" * len(chunk))
            out.extend(self.con.execute(
                f"SELECT symbol_id, caller_id, shard_id, sites FROM refs "
                f"WHERE symbol_id IN ({q}) AND caller_id != 0", chunk))
        return out

    # impact / tests / path -------------------------------------------------------------------

    def impact(self, symbol: str, depth: int = 3, include_overridden: bool = True,
               max_nodes: int = 20000, root: str = "", lifecycle: set[str] | None = None
               ) -> tuple[list[Node], bool]:
        """Transitive callers, breadth first. With include_overridden, calls to the methods it
        overrides count too (they may dispatch here); further up, for an override of type X
        that is reached, the calls to the project methods it overrides made from X's own
        hierarchy (a base class calling this.m(): template methods). `super.m()` calls, read
        from the sources under `root`, are left out of those (bound to the caller's
        superclass), and a method reached through such a dispatch for X keeps only the super
        calls from X's hierarchy. A reached `lifecycle` method (one the framework calls on an
        object it was given, such as onAttach) goes on to the constructors of its type: whoever
        creates the object. Returns (nodes, truncated)."""
        start = self.ids([symbol]).get(symbol)
        if start is None:
            return [], False
        frontier = [start]
        overridden: set[int] = set()
        if include_overridden and S.is_method(symbol):
            overridden = set(self.ancestors(start)) - {start}
            frontier += sorted(overridden)
        seen = set(frontier)
        texts: dict[str, list[str]] = {}
        dispatch: dict[int, str] = {}       # overridden method -> the type whose override
        bound: dict[int, str] = {}          # reached through it -> that type
        families: dict[str, set[str]] = {}

        def family(t: str) -> set[str]:
            if t not in families:
                tid = self.ids([t]).get(t)
                families[t] = {t} | self._subtypes_of({t}) | (
                    set(self.names(self.ancestors(tid)).values()) if tid is not None else set())
            return families[t]
        nodes: dict[int, Node] = {}
        names: dict[int, str] = self.names(frontier)
        truncated = False
        sh = self.shards()
        lifecycle = lifecycle or set()

        def creators(found: list[int]) -> list[int]:
            """Constructors of the types whose lifecycle methods were reached (the type the
            chain is bound to, when the method is inherited from a base class)."""
            out_ = []
            for sid in found:
                sym = names.get(sid, "")
                if S.kind_of(sym) != "method" or S.name_of(sym) not in lifecycle:
                    continue
                prefix = (bound.get(sid) or S.owner_type(sym)) + "`<init>`"
                for (cid,) in self.con.execute(
                        "SELECT s.id FROM symbols s WHERE s.symbol >= ? AND s.symbol < ? AND "
                        "EXISTS (SELECT 1 FROM defs d WHERE d.symbol_id = s.id)",
                        (prefix, prefix + "\uffff")):
                    if cid not in seen:
                        seen.add(cid)
                        out_.append(cid)
            return out_
        if lifecycle:
            extra0 = creators([start])
            if extra0:
                names.update(self.names(extra0))
                frontier += extra0
        for level in range(1, depth + 1):
            rows = self.callers_of(frontier)
            if bound or dispatch or (level == 1 and overridden):
                names.update(self.names({c for t, c, _s, _x in rows
                                         if (t in bound or t in dispatch or t in overridden)
                                         and c not in names}))
            nxt = []
            for target, caller, shard_id, sites in rows:
                if caller in seen or shard_id not in sh:
                    continue
                if target in dispatch and \
                        S.owner_type(names.get(caller, "")) not in family(dispatch[target]):
                    continue        # another object's call: it dispatches to its own type
                if root and ((level == 1 and target in overridden) or target in dispatch):
                    sites = _not_super(root, sh[shard_id][0], sites,
                                       S.name_of(names.get(target, symbol)), texts)
                    if not sites:
                        continue
                if root and target in bound and \
                        S.owner_type(names.get(caller, "")) not in family(bound[target]):
                    # Another class's this.m(), m() or super.m() runs on its own object.
                    sites = _not_super(root, sh[shard_id][0], sites,
                                       S.name_of(names.get(target, "")), texts, own=True)
                    if not sites:
                        continue
                seen.add(caller)
                nxt.append(caller)
                if target in dispatch:
                    bound[caller] = dispatch[target]
                elif level == 1 and target in overridden and \
                        S.owner_type(names.get(caller, "")) in family(S.owner_type(symbol)):
                    bound[caller] = S.owner_type(symbol)    # the base's code, on this object
                elif target in bound and \
                        S.owner_type(names.get(caller, "")) in family(bound[target]):
                    bound[caller] = bound[target]   # still the same object's code
                src, mod, test = sh[shard_id]
                nodes[caller] = Node("", level, names.get(target, ""), src, 0, module_of(mod),
                                     bool(test), _lines(sites), mod,
                                     inner=all("@" in x for x in sites.split(",") if x))
                if len(nodes) >= max_nodes:
                    truncated = True
                    break
            if truncated or not nxt:
                break
            names.update(self.names(nxt))
            frontier = nxt
            if level < depth:
                extra = []
                if include_overridden:
                    for caller, target in self._project_overridden(nxt):
                        if target not in seen:
                            seen.add(target)
                            # An override reached for X stays X's (narrower than its owner).
                            dispatch[target] = bound.get(caller) or \
                                S.owner_type(names.get(caller, ""))
                            extra.append(target)
                if lifecycle:
                    extra += creators(nxt)
                if extra:
                    names.update(self.names(extra))
                    frontier = nxt + extra
        names.update(self.names(nodes))
        defs = self.definitions(nodes)
        for sid, n in nodes.items():
            n.symbol = names.get(sid, "")
            d = defs.get(sid)
            if d:
                n.line = d[1]
        return sorted(nodes.values(), key=lambda n: (n.depth, n.module, n.source, n.line)), \
            truncated

    def entry_points(self, nodes: list[Node], specs: list[dict], root: str) -> list[Entry]:
        """The reached nodes that are entry points by a profile's [[entry_points]]: the member
        or its type carries one of the annotations, or the type extends one of the supertypes
        and/or has a name matching type_names (both, when both are given), or is named in a
        deployment descriptor (xml). The detail says what identifies it: an HTTP route (verb
        and the type's and member's paths), an annotation's first string (a step text), the
        type's declared supertype, the descriptor (with its URL patterns)."""
        if not nodes or not specs:
            return []
        syms = {n.symbol for n in nodes if n.symbol}
        owners = {S.owner_type(x) for x in syms if S.owner_type(x)}
        ids = self.ids(syms | owners)
        by_id = {v: k for k, v in ids.items()}
        anns: dict[str, list[tuple[str, int, int]]] = {}
        id_list = list(ids.values())
        for i in range(0, len(id_list), 500):
            chunk = id_list[i:i + 500]
            q = ",".join("?" * len(chunk))
            for def_id, ann, shard_id, line in self.con.execute(
                    f"SELECT a.def_id, s.symbol, a.shard_id, a.line FROM annotations a JOIN "
                    f"symbols s ON s.id = a.annotation_id WHERE a.def_id IN ({q})", chunk):
                anns.setdefault(by_id[def_id], []).append((ann, shard_id, line))
        supers: dict[str, set[str]] = {}
        for t in owners:
            if t in ids:
                supers[t] = set(self.names(self.ancestors(ids[t])).values())
        xml_types = {}
        for k, spec in enumerate(specs):
            if spec.get("xml"):
                xml_types[k] = _xml_classes(root, spec["xml"])
            if spec.get("referenced_by"):
                xml_types.setdefault(k, {}).update(self._referenced_types(
                    spec["referenced_by"], root))
        sh = self.shards()
        texts: dict[str, list[str]] = {}

        def lines_of(src: str) -> list[str]:
            if src not in texts:
                try:
                    with open(os.path.join(root, src), encoding="utf-8",
                              errors="replace") as fh:
                        texts[src] = fh.read().split("\n")
                except OSError:
                    texts[src] = []
            return texts[src]

        def args_of(sym: str, names: set[str]) -> list[tuple[str, str]]:
            """[(annotation simple name, its argument text)] of sym's annotations named so."""
            out_ = []
            for a, shard_id, line in anns.get(sym, []):
                n_ = S.name_of(a)
                if n_ in names and shard_id in sh:
                    out_.append((n_, _annotation_args(lines_of(sh[shard_id][0]), line, n_)))
            return out_

        def first_string(text: str) -> str:
            m = re.search(r'"((?:[^"\\]|\\.)*)"', text)
            return m.group(1)[:80] if m else ""

        def route(sym: str, owner: str) -> str:
            """"POST /orders/{id}" from JAX-RS or Spring annotations, or ""."""
            if S.kind_of(sym) != "method":
                return ""
            verb = ""
            for n_, a in args_of(sym, set(_HTTP_VERBS) | set(_MAPPINGS)):
                verb = _HTTP_VERBS.get(n_) or _MAPPINGS.get(n_) or verb
                if n_ == "RequestMapping":
                    m = re.search(r"RequestMethod\.(\w+)", a)
                    verb = m.group(1) if m else verb
            own = next((first_string(a) for _n, a in args_of(sym, {"Path"} | set(_MAPPINGS))
                        if first_string(a)), "")
            base = next((first_string(a) for _n, a in args_of(owner, {"Path",
                                                                       "RequestMapping"})
                         if first_string(a)), "")
            if not verb and not own:
                return ""
            path = "/" + "/".join(x.strip("/") for x in (base, own) if x.strip("/"))
            return f"{verb} {path}".strip()

        sigs: dict[str, str] = {}

        def private(sym: str) -> bool:
            if sym not in sigs:
                d = self.definitions([ids[sym]]).get(ids[sym]) if sym in ids else None
                sigs[sym] = d[4] if d else ""
            head = sigs[sym].split("(", 1)[0]
            return re.search(r"\bprivate\b", re.sub(r"@\w+(\([^)]*\))?", " ", head)) is not None

        def abstract(t: str) -> bool:
            """An abstract class or an interface: a base of screens, jobs…, not one."""
            if t not in sigs:
                d = self.definitions([ids[t]]).get(ids[t]) if t in ids else None
                sigs[t] = d[4] if d else ""
            head = re.sub(r"@\w+(\([^)]*\))?", " ", sigs[t]).split("{", 1)[0]
            return re.search(r"\babstract\b|\binterface\b", head) is not None

        declared: dict[str, str] = {}

        def declared_super(t: str) -> str:
            """The supertype the declaration names after extends (else implements)."""
            if t not in declared:
                declared[t] = ""
                d = self.definitions([ids[t]]).get(ids[t]) if t in ids else None
                if d:
                    text = " ".join(lines_of(d[0])[max(0, d[1] - 1):d[1] + 6])
                    m = re.search(r"\b(?:class|interface|enum|record)\s+" +
                                  re.escape(S.name_of(t)) + r"\b([^{]*)", text)
                    head = re.sub(r"<[^<>]*(<[^<>]*>[^<>]*)*>", "", m.group(1)) if m else ""
                    e = re.search(r"\bextends\s+([\w$.]+)", head) or \
                        re.search(r"\bimplements\s+([\w$.]+)", head)
                    declared[t] = e.group(1).rsplit(".", 1)[-1] if e else ""
            return declared[t]

        def deferred(n: Node) -> bool:
            """Every call it makes on the way is inside a lambda, a method reference or an
            anonymous class: it registers code that runs later, it does not run it."""
            if n.inner:
                return True
            lines = lines_of(n.source)
            return bool(n.sites) and all(0 < x <= len(lines) and ("->" in lines[x - 1] or
                                                                  "::" in lines[x - 1])
                                         for x in n.sites)
        out = []
        for n in nodes:
            sym = n.symbol
            owner = S.owner_type(sym)
            for k, spec in enumerate(specs):
                if spec["kind"] in _AT_START and deferred(n):
                    continue
                detail, on_type = None, False
                if S.kind_of(sym) == "method" and private(sym) and not any(
                        a in spec["annotations"] for a, _s, _l in anns.get(sym, [])):
                    continue        # a private method is reached through its class's code
                for target in (sym, owner):
                    hit = [a for a, _s, _l in anns.get(target, []) if a in spec["annotations"]]
                    if hit:
                        on_type = target != sym or S.kind_of(sym) == "type"
                        detail = route(sym, owner)
                        if target != sym and S.kind_of(sym) == "method" and not detail and \
                                S.name_of(hit[0]) in _ROUTE_HOLDERS:
                            detail = ""     # a controller's helper, not one of its routes
                            break
                        if not detail:
                            text = next((first_string(a) for _n, a in
                                         args_of(target, {S.name_of(hit[0])})), "")
                            detail = "@" + S.name_of(hit[0]) + (f'("{text}")' if text else "")
                        break
                if detail is None and S.kind_of(sym) in ("type", "constructor", "method"):
                    t = owner if owner else sym
                    ok_super = not spec["supertypes"] or bool(supers.get(t, set())
                                                              & spec["supertypes"])
                    ok_name = not spec["type_names"] or any(
                        fnmatch.fnmatch(S.name_of(t), g) for g in spec["type_names"])
                    if (spec["supertypes"] or spec["type_names"]) and ok_super and ok_name \
                            and not abstract(t):
                        sup = sorted(supers.get(t, set()) & spec["supertypes"])
                        direct = declared_super(t)
                        if sup:
                            rule = S.name_of(sup[0])
                            detail = f"extends {direct or rule}" + (
                                f" (a {rule})" if direct and direct != rule else "")
                        else:
                            detail = "named like " + "/".join(spec["type_names"]) + (
                                f", extends {direct}" if direct else "")
                        on_type = True
                    elif t in xml_types.get(k, {}):
                        detail, on_type = f"in {xml_types[k][t]}", True
                if detail == "":
                    continue
                if detail is not None:
                    out.append(Entry(spec["kind"], n, detail, on_type))
                    break
        return out

    def path(self, src: str, dst: str, max_depth: int = 6, limit: int = 3,
             max_nodes: int = 100000) -> list[list[tuple[str, list[int]]]]:
        """Shortest call chains from `src` to `dst` ([(symbol, lines where it calls the next)]),
        searching callers backwards from `dst`."""
        ids = self.ids([src, dst])
        if src not in ids or dst not in ids:
            return []
        a, b = ids[src], ids[dst]
        parents: dict[int, list[tuple[int, str]]] = {b: []}
        frontier = [b]
        found = False
        for _level in range(max_depth):
            rows = self.callers_of(frontier)
            nxt = []
            for target, caller, _shard, sites in rows:
                if caller not in parents:
                    parents[caller] = []
                    nxt.append(caller)
                if caller != target:
                    parents[caller].append((target, sites))
                if caller == a:
                    found = True
            if found or not nxt or len(parents) > max_nodes:
                break
            frontier = nxt
        if a not in parents:
            return []
        paths: list[list[tuple[int, str]]] = []

        def walk(node: int, acc: list[tuple[int, str]]):
            if len(paths) >= limit:
                return
            if node == b:
                paths.append(acc + [(b, "")])
                return
            for nxt_node, sites in parents.get(node, []):
                if nxt_node not in {x for x, _ in acc}:
                    walk(nxt_node, acc + [(node, sites)])

        walk(a, [])
        names = self.names({x for p in paths for x, _ in p})
        return [[(names.get(x, "?"), _lines(sites)) for x, sites in p] for p in paths]

    # changed ---------------------------------------------------------------------------------

    def changed(self, root: str, base: str = "HEAD", staged: bool = False,
                paths: list[str] | None = None) -> dict:
        """Definitions covering the lines changed since `base` (working tree, or the index with
        staged=True), from `git diff -U0`, plus untracked .java files (working tree only).

        Javadoc, annotations and comments count for the member that follows them; blank lines
        and import lines are skipped. Changes inside a class body but outside any member are
        reported for the class without making it "changed" for its users unless the class
        declaration itself (its header) changed. Hunks that only delete lines cannot be mapped
        (the deleted code is not in the index) and are reported as such."""
        hunks = git_diff_hunks(root, base, staged, paths or [])
        untracked = [] if staged else git_untracked_java(root, paths or [])
        for u in untracked:
            hunks.setdefault(u, [(1, 10 ** 6)])  # every line is new
        sh = self.shards()
        by_source = defaultdict(list)
        for sid, (src, _mod, _t) in sh.items():
            by_source[src].append(sid)
        out = {"definitions": OrderedDict(), "unmapped_deletions": [], "not_indexed": [],
               "stale": [], "files": len(hunks),
               "java_files": sum(1 for h in hunks if h.endswith(".java")),
               "untracked": untracked, "trivial_hunks": 0, "class_level": OrderedDict()}
        for src, ranges in hunks.items():
            if not src.endswith(".java"):
                continue
            shard_ids = by_source.get(src)
            if not shard_ids:
                out["not_indexed"].append(src)
                continue
            if _is_stale(root, src, self.con, shard_ids[0]):
                out["stale"].append(src)
                continue
            try:
                with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                    text = fh.read().split("\n")
            except OSError:
                text = []
            q = ",".join("?" * len(shard_ids))
            defs = [d for d in self.con.execute(
                f"SELECT d.symbol_id, s.symbol, d.body_start, d.body_end, d.line FROM defs d "
                f"JOIN symbols s ON s.id = d.symbol_id WHERE d.shard_id IN ({q})", shard_ids)
                if d[2] is not None]
            for start, count in ranges:
                if count == 0:
                    out["unmapped_deletions"].append(f"{src}:{start}")
                    continue
                touched = False
                last = min(start - 1 + count, len(text)) if text else start - 1 + count
                for ln in range(start - 1, last):  # 0-based
                    line = text[ln].strip() if ln < len(text) else ""
                    if not line or line in ("}", "};", "{") or \
                            line.startswith(("import ", "package ")):
                        continue
                    best = _innermost(defs, ln)
                    if best is None:
                        continue
                    if S.is_type(best[1]) and ln >= best[4] + 1 and _is_preamble(line):
                        nxt = _next_member(defs, best, ln)
                        if nxt is not None:
                            best = nxt
                    touched = True
                    if S.is_type(best[1]) and ln < best[4]:
                        # Above the declaration: annotations or javadoc of the class. They change
                        # how frameworks treat it, not the API its users see.
                        kind = "annotation" if line.startswith("@") else "documentation"
                        out["class_level"].setdefault(best[1], {
                            "source": src, "line": best[4] + 1, "changed": set(),
                            "kind": set()})["changed"].add(ln + 1)
                        out["class_level"][best[1]].setdefault("kind", set()).add(kind)
                        continue
                    if S.is_type(best[1]) and ln > best[4] + _header_lines(text, best[4]):
                        out["class_level"].setdefault(best[1], {
                            "source": src, "line": best[4] + 1, "changed": set()})[
                            "changed"].add(ln + 1)
                        continue
                    entry = out["definitions"].setdefault(best[1], {
                        "source": src, "line": best[4] + 1, "changed": set(),
                        "test": bool(sh[shard_ids[0]][2])})
                    entry["changed"].add(ln + 1)
                if not touched:
                    out["trivial_hunks"] += 1
        return out

    # dependencies ----------------------------------------------------------------------------

    def deps(self, level: str = "module", scope: str = "", target_scope: str = "",
             min_count: int = 1) -> dict[tuple[str, str], int]:
        """Edges between modules (from shard directories) or packages, with reference counts.
        Only references to symbols defined in the project count."""
        if level == "package":
            scope = _as_package(scope)
            target_scope = _as_package(target_scope)
            # With a target, external packages count too (e.g. what a package uses of a library).
            project_only = "" if target_scope else \
                "AND EXISTS (SELECT 1 FROM defs d WHERE d.symbol_id = r.symbol_id)"
            rows = self.con.execute(f"""
                SELECT cs.pkg, ts.pkg, count(*) FROM refs r
                JOIN symbols cs ON cs.id = r.caller_id
                JOIN symbols ts ON ts.id = r.symbol_id
                WHERE r.caller_id != 0 AND cs.pkg != ts.pkg {project_only}
                GROUP BY cs.pkg, ts.pkg""")
            edges = {(a, b): n for a, b, n in rows}
        else:
            sh = self.shards()
            rows = self.con.execute("""
                SELECT r.shard_id, d.shard_id, count(*) FROM refs r
                JOIN defs d ON d.symbol_id = r.symbol_id
                GROUP BY r.shard_id, d.shard_id""")
            edges: dict[tuple[str, str], int] = defaultdict(int)
            for a, b, n in rows:
                if a in sh and b in sh:
                    ma, mb = module_of(sh[a][1]), module_of(sh[b][1])
                    if ma != mb:
                        edges[(ma, mb)] += n
        return {k: v for k, v in edges.items()
                if v >= min_count and _in_scope(k[0], scope) and _in_scope(k[1], target_scope)}

    def uses_between(self, scope: str, target_scope: str, limit: int = 200
                     ) -> tuple[list[tuple[str, str, str, list[int]]], int]:
        """Call sites where code in `scope` (package glob) uses symbols in `target_scope`
        (package glob, or a symbol prefix such as `org/apache/commons/`): (caller, target, source,
        lines)."""
        rows = self.con.execute("""
            SELECT cs.symbol, cs.pkg, ts.symbol, ts.pkg, r.shard_id, r.sites FROM refs r
            JOIN symbols cs ON cs.id = r.caller_id
            JOIN symbols ts ON ts.id = r.symbol_id
            WHERE r.caller_id != 0""")
        sh = self.shards()
        out = []
        total = 0
        pkg_scope, pkg_target = _as_package(scope), _as_package(target_scope)
        for caller, cpkg, target, tpkg, shard_id, sites in rows:
            if shard_id not in sh or not _in_scope(cpkg, pkg_scope):
                continue
            if not (_in_scope(tpkg, pkg_target) or target.startswith(target_scope)):
                continue
            total += 1
            if len(out) < limit:
                out.append((caller, target, sh[shard_id][0], _lines(sites)))
        return out, total

    # dead code -------------------------------------------------------------------------------

    def dead_code(self, scope: str = "", public_is_api: bool = False,
                  include_tests: bool = False, entry_annotations=(), entry_supertypes=(),
                  entry_type_names=(), entry_packages=(), entry_xml=(),
                  by_name_globs=DEAD_CODE_GLOBS,
                  by_name_exclude=(), root: str = "", exclude_paths=()) -> list[tuple]:
        """Candidates: project definitions nobody references. Left out: overrides and what is
        overridden (dispatch), Lombok-generated members and fields with generated accessors that
        are used, main/serialization hooks, no-arg constructors of persistent classes, members
        carrying a non-java.lang annotation (framework-managed) or one of entry_annotations, and
        names used inside string literals. Definitions in sources matching `exclude_paths`
        (globs, e.g. generated code) are never candidates; `self.excluded` counts them.
        Returns [(symbol, kind, source, line, signature)]."""
        sh = self.shards()
        self.excluded = 0
        excl = [g.replace("**", "*") for g in exclude_paths]
        rows = self.con.execute("""
            SELECT d.symbol_id, s.symbol, d.kind, d.flags, d.shard_id, d.line, d.signature
            FROM defs d JOIN symbols s ON s.id = d.symbol_id
            WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.symbol_id = d.symbol_id
                              AND r.caller_id != d.symbol_id)
              AND NOT EXISTS (SELECT 1 FROM rels x WHERE x.symbol_id = d.symbol_id
                              AND x.kind = ?)
              AND NOT EXISTS (SELECT 1 FROM rels y WHERE y.target_id = d.symbol_id
                              AND y.kind = ?)""", (REL_IMPLEMENTATION, REL_IMPLEMENTATION))
        annotated = defaultdict(set)
        for def_id, ann in self.con.execute(
                "SELECT a.def_id, s.symbol FROM annotations a JOIN symbols s "
                "ON s.id = a.annotation_id"):
            annotated[def_id].add(ann)
        entry = set(entry_annotations)
        entry_types = self._subtypes_of(set(entry_supertypes)) if entry_supertypes else set()
        if entry_xml and root:   # classes a deployment descriptor names (web.xml servlets…)
            entry_types |= set(_xml_classes(root, list(entry_xml)))
        used_accessors = self._used_generated_accessors()
        rows = list(rows)
        owner_ids = self.ids({S.parent(r[1]) for r in rows})
        out = []
        for sid, sym, kind, flags, shard_id, line, sig in rows:
            if shard_id not in sh or S.is_local_like(sym):
                continue
            if sh[shard_id][2] and not include_tests:
                continue  # test code is run by the test framework
            src = sh[shard_id][0]
            if scope and not scope_matches(scope, S.package_of(sym), src):
                continue
            if excl and any(fnmatch.fnmatch(src, g) for g in excl):
                self.excluded += 1
                continue
            if flags & FLAG_GENERATED:
                continue
            name = S.name_of(sym)
            k = S.kind_of(sym)
            if k == "package" or name in ("main", "readObject", "writeObject", "readResolve",
                                          "writeReplace", "serialVersionUID", "values",
                                          "valueOf", "readObjectNoData"):
                continue
            anns = annotated.get(sid, set())
            if any(not a.startswith("java/lang/") for a in anns):
                continue  # framework-managed (or listed as an entry annotation)
            if k == "constructor" and (not sig or re.search(r"\(\s*\)", sig)):
                continue  # implicit or no-arg: frameworks (JPA, JDO, Spring…) instantiate them
            if k == "term" and sym in used_accessors:
                continue
            owner_anns = annotated.get(owner_ids.get(S.parent(sym), -1), set())
            if any(not a.startswith("java/lang/") for a in owner_anns) and (
                    k == "term" or re.match(r"(get|set|is)[A-Z]", name)):
                continue  # bean properties of framework-managed classes (reflection)
            if public_is_api and re.search(r"\bpublic\b", sig or ""):
                continue
            if k == "type" and self._members_referenced(sym):
                continue  # e.g. only `new T()` (a constructor reference) mentions it
            if k == "term" and self._referenced(S.parent(sym) + S.format_descriptors(
                    [S.Descriptor(name, S.METHOD)])):
                continue  # record component read through its implicit accessor
            if anns & entry:
                continue
            if entry_packages and any(_in_scope(S.package_of(sym), p) for p in entry_packages):
                continue  # declared API for code outside the repository
            owner = sym if k == "type" else S.parent(sym)
            if owner in entry_types or (k in ("type", "constructor") and any(
                    fnmatch.fnmatch(S.name_of(owner), g) for g in entry_type_names)):
                continue
            out.append((sym, kind, src, line + 1, sig or ""))
        self.suppressed = {}
        self.low_confidence = []
        if out and root:
            aliases: dict[str, set[str]] = defaultdict(set)   # name in text -> candidate names
            properties: set[str] = set()
            own_names = {S.name_of(y[0]) for y in out}
            for x in out:
                n = S.name_of(x[0])
                aliases[n].add(n)
                m = re.match(r"(get|set|is)([A-Z][\w$]*)$", n)
                if m:  # templates and reports use the bean property: getActividad -> actividad
                    prop = m.group(2)[0].lower() + m.group(2)[1:]
                    aliases[prop].add(n)
                    if prop not in own_names:
                        properties.add(prop)
            names = sorted(a for a in aliases if len(a) > 2)
            seen = self.names_seen(root, names, properties, list(by_name_globs),
                                   list(by_name_exclude))
            exact: dict[str, str] = {}
            by_property: dict[str, str] = {}
            for w, src in seen.items():
                for c in aliases.get(w, ()):
                    (exact if w == c else by_property).setdefault(c, (src, w))
            kept = []
            for x in out:
                n = S.name_of(x[0])
                if n in exact:
                    src = exact[n][0]
                    self.suppressed[src] = self.suppressed.get(src, 0) + 1
                elif n in by_property:
                    # Only the bean property name (getDescription -> "description") appears:
                    # too weak to drop the candidate silently.
                    src, prop = by_property[n]
                    self.low_confidence.append((*x, src, prop))
                else:
                    kept.append(x)
            out = kept
        out.sort(key=lambda x: (x[2], x[3]))
        return out

    def in_scope_count(self, scope: str) -> int:
        """How many live definitions a scope covers (to tell "nothing dead" from "no match")."""
        sh = self.shards()
        n = 0
        for (sym, shard_id) in self.con.execute(
                "SELECT s.symbol, d.shard_id FROM defs d JOIN symbols s ON s.id = d.symbol_id"):
            if shard_id in sh and scope_matches(scope, S.package_of(sym), sh[shard_id][0]):
                n += 1
                if n > 0:
                    break
        return n

    def _subtypes_of(self, types: set[str]) -> set[str]:
        ids = self.ids(types)
        if not ids:
            return set()
        q = ",".join("?" * len(ids))
        rows = self.con.execute(f"SELECT s.symbol FROM rels r JOIN symbols s ON s.id = r.symbol_id "
                                f"WHERE r.kind = ? AND r.target_id IN ({q})",
                                (REL_IMPLEMENTATION, *ids.values()))
        return {x for (x,) in rows}

    def classes_with(self, annotations: set[str]) -> set[str]:
        """Top-level types (as `pkg.Type`) that have a member carrying one of the annotations."""
        ids = self.ids(annotations)
        if not ids:
            return set()
        q = ",".join("?" * len(ids))
        out = set()
        for (sym,) in self.con.execute(
                f"SELECT DISTINCT s.symbol FROM annotations a JOIN symbols s ON s.id = a.def_id "
                f"WHERE a.annotation_id IN ({q})", list(ids.values())):
            pkg, top = S.package_of(sym), S.top_level_type_name(sym)
            out.add(f"{pkg}.{top}" if pkg else top)
        return out

    def annotations_present(self) -> set[str]:
        return {a for (a,) in self.con.execute(
            "SELECT DISTINCT s.symbol FROM annotations a JOIN symbols s "
            "ON s.id = a.annotation_id")}

    def entities(self, entity_annotations: set[str], table_specs, root: str
                 ) -> list[tuple[str, str, int, str]]:
        """(entity type, source, line, table) for types carrying an entity annotation; the table
        is read from the annotation's own text in the source (attribute values are not
        indexed)."""
        ent_ids = self.ids(entity_annotations)
        if not ent_ids:
            return []
        table_ids = {}
        for anns, attr in table_specs:
            for a, aid in self.ids(anns).items():
                table_ids[aid] = (attr, S.name_of(a))
        sh = self.shards()
        q = ",".join("?" * len(ent_ids))
        entities = list(self.con.execute(
            f"SELECT DISTINCT a.def_id, a.shard_id FROM annotations a "
            f"WHERE a.annotation_id IN ({q})", list(ent_ids.values())))
        names = self.names({d for d, _ in entities})
        defs = self.definitions({d for d, _ in entities})
        texts: dict[str, list[str]] = {}
        out = []
        for def_id, shard_id in entities:
            sym = names.get(def_id, "")
            if shard_id not in sh or not S.is_type(sym):
                continue
            src = sh[shard_id][0]
            if src not in texts:
                try:
                    with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                        texts[src] = fh.read().split("\n")
                except OSError:
                    texts[src] = []
            table = ""
            for aid, line in self.con.execute(
                    "SELECT annotation_id, line FROM annotations WHERE def_id = ? AND shard_id = ?",
                    (def_id, shard_id)):
                spec = table_ids.get(aid)
                if spec is None:
                    continue
                attr, ann_name = spec
                args = _annotation_args(texts[src], line, ann_name)
                v = re.search(r"\b" + re.escape(attr) + r'\s*=\s*"([^"]*)"', args)
                if v:
                    table = v.group(1)
                    break
            d = defs.get(def_id)
            out.append((sym, src, d[1] if d else 0, table))
        return sorted(set(out), key=lambda x: (x[1], x[2]))

    def _referenced(self, symbol: str) -> bool:
        return self.con.execute(
            "SELECT 1 FROM refs r JOIN symbols s ON s.id = r.symbol_id WHERE s.symbol = ? "
            "LIMIT 1", (symbol,)).fetchone() is not None

    def _members_referenced(self, type_symbol: str) -> bool:
        """Whether any member of the type (constructors included) is referenced from outside
        the type itself."""
        hi = type_symbol[:-1] + chr(ord(type_symbol[-1]) + 1)
        return self.con.execute("""
            SELECT 1 FROM symbols s JOIN refs r ON r.symbol_id = s.id
            JOIN symbols c ON c.id = r.caller_id
            WHERE s.symbol > ? AND s.symbol < ? AND NOT (c.symbol >= ? AND c.symbol < ?)
            LIMIT 1""", (type_symbol, hi, type_symbol, hi)).fetchone() is not None

    def _used_generated_accessors(self) -> set[str]:
        """Fields whose generated (Lombok) getter or setter is referenced."""
        out = set()
        for sym, in self.con.execute("""
                SELECT s.symbol FROM defs d JOIN symbols s ON s.id = d.symbol_id
                WHERE (d.flags & ?) != 0 AND EXISTS (SELECT 1 FROM refs r
                                                     WHERE r.symbol_id = d.symbol_id)""",
                                     (FLAG_GENERATED,)):
            m = re.match(r"(get|set|is)([A-Z]\w*)$", S.name_of(sym))
            if m:
                prop = m.group(2)[0].lower() + m.group(2)[1:]
                out.add(S.parent(sym) + prop + ".")
        return out

    # census ----------------------------------------------------------------------------------

    def annotation_census(self, scope: str = "", limit: int = 50) -> list[tuple[str, int, int]]:
        """(annotation, uses, files) for the annotations used on project definitions."""
        sh = self.shards()
        counts: dict[str, list] = {}
        for ann, def_id, line, shard_id in self.con.execute(
                "SELECT s.symbol, a.def_id, a.line, a.shard_id FROM annotations a JOIN symbols s "
                "ON s.id = a.annotation_id"):
            if shard_id not in sh or (scope and not fnmatch.fnmatch(sh[shard_id][0], scope)):
                continue
            c = counts.setdefault(ann, [set(), set()])
            c[0].add((def_id, line))       # once per use, also when a source has two shards
            c[1].add(sh[shard_id][0])
        rows = sorted(((a, len(u), len(f)) for a, (u, f) in counts.items()),
                      key=lambda x: -x[1])
        return rows[:limit]

    def annotated_with(self, annotation: str) -> list[tuple[str, str, int, str]]:
        """(symbol, source, line, signature) of definitions carrying an annotation."""
        aid = self.ids([annotation]).get(annotation)
        if aid is None:
            return []
        sh = self.shards()
        out = []
        for sym, shard_id, line, sig in self.con.execute("""
                SELECT s.symbol, d.shard_id, d.line, d.signature FROM annotations a
                JOIN symbols s ON s.id = a.def_id
                JOIN defs d ON d.symbol_id = a.def_id AND d.shard_id = a.shard_id
                WHERE a.annotation_id = ?""", (aid,)):
            if shard_id in sh:
                out.append((sym, sh[shard_id][0], line + 1, sig or ""))
        return sorted(set(out), key=lambda x: (x[1], x[2], x[0]))


def cycles(edges) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan), largest first."""
    graph = defaultdict(set)
    for a, b in edges:
        graph[a].add(b)
        graph.setdefault(b, set())
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = [0]

    def strong(v: str) -> None:  # iterative to survive deep graphs
        work = [(v, iter(sorted(graph[v])))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        while work:
            node, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(graph[w]))))
                    break
                if w in on:
                    low[node] = min(low[node], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        out.append(sorted(comp))

    for v in sorted(graph):
        if v not in index:
            strong(v)
    return sorted(out, key=lambda c: (-len(c), c))


def _annotation_args(lines: list[str], line: int, name: str, max_lines: int = 40) -> str:
    """The text between the parentheses of annotation `name` on `line` (0-based), across lines,
    balancing nested parentheses outside string literals; "" when it has no arguments."""
    text = "\n".join(lines[line:line + max_lines])
    m = re.search(r"@\s*(?:[\w$]+\s*\.\s*)*" + re.escape(name) + r"\b\s*", text)
    if not m or m.end() >= len(text) or text[m.end()] != "(":
        return ""
    start = m.end()
    depth, in_str, k = 0, False, start
    while k < len(text):
        ch = text[k]
        if ch == '"' and (k == 0 or text[k - 1] != "\\"):
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[start + 1:k]
        k += 1
    return text[start + 1:]


def _annotation_text(root: str, source: str, def_line: int, before: int = 25) -> str:
    """The source text above a definition (its annotations), joined."""
    try:
        with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return ""
    lo = max(0, def_line - 1 - before)
    return "\n".join(lines[lo:def_line])


# --- helpers ----------------------------------------------------------------------------------

def _not_super(root: str, source: str, sites: str, name: str, cache: dict,
               own: bool = False) -> str:
    """The sites ("line:col,…", 0-based) of a reference list that are not `super.name(…)`
    (with own, nor any call on the caller's own object: this.name(…), name(…))."""
    lines = cache.get(source)
    if lines is None:
        try:
            with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
        except OSError:
            lines = []
        cache[source] = lines
    keep = []
    for s in sites.split(","):
        if not s:
            continue
        line, _, rest = s.partition(":")
        col = int(rest.split("@", 1)[0] or -1) if rest else -1
        ln = int(line)
        if ln < len(lines) and (S.is_self_call(lines[ln], col, name) if own else
                                S.is_super_call(lines[ln], col, name)):
            continue
        keep.append(s)
    return ",".join(keep)


def _lines(sites: str) -> list[int]:
    out = []
    for s in sites.split(","):
        if s:
            out.append(int(s.split(":", 1)[0]) + 1)
    return sorted(set(out))


def _xml_classes(root: str, rules: list) -> dict[str, str]:
    """{type symbol: file} for the classes named by <element>…</element> in matching files
    (web.xml's servlet-class…), searched with the repository's files."""
    out: dict[str, str] = {}
    for files, elements in rules:
        if not elements:
            continue
        rx = re.compile(r"<(" + "|".join(re.escape(e) for e in elements) +
                        r")>\s*([\w$.]+)\s*</\1>")
        for path in candidate_files(root, [g.rsplit("/", 1)[-1] for g in files], []):
            if not any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", ""))
                       for g in files):
                continue
            try:
                with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            urls = _web_xml_urls(text)
            for m in rx.finditer(text):
                fq = m.group(2).replace("$", ".")
                parts = fq.split(".")
                pkg = [p for p in parts if p[:1].islower()]
                types = parts[len(pkg):]
                if types:
                    sym = "/".join(pkg) + "/" + "#".join(types) + "#"
                    u = urls.get(m.group(2), [])
                    out.setdefault(sym, path + (f" ({', '.join(u[:4])})" if u else ""))
    return out


def _web_xml_urls(text: str) -> dict[str, list[str]]:
    """{servlet or filter class: its URL patterns (or servlet names, for filters mapped to
    servlets)} from web.xml's <servlet>/<filter> and their -mapping elements."""
    out: dict[str, list[str]] = {}
    for kind in ("servlet", "filter"):
        cls = {}
        for b in re.finditer(rf"<{kind}>(.*?)</{kind}>", text, re.S):
            n = re.search(rf"<{kind}-name>\s*(.*?)\s*</{kind}-name>", b.group(1), re.S)
            c = re.search(rf"<{kind}-class>\s*([\w$.]+)\s*</{kind}-class>", b.group(1))
            if n and c:
                cls[n.group(1)] = c.group(1)
        for b in re.finditer(rf"<{kind}-mapping>(.*?)</{kind}-mapping>", text, re.S):
            n = re.search(rf"<{kind}-name>\s*(.*?)\s*</{kind}-name>", b.group(1), re.S)
            if not n or n.group(1) not in cls:
                continue
            pats = re.findall(r"<url-pattern>\s*(.*?)\s*</url-pattern>", b.group(1)) + \
                [f"servlet {x}" for x in re.findall(r"<servlet-name>\s*(.*?)\s*</servlet-name>",
                                                    b.group(1)) if kind == "filter"]
            out.setdefault(cls[n.group(1)], []).extend(pats)
    return out


_STOPWORDS = set("""de del la las el los lo un una unos unas y o u e en a al para por con sin
que se su sus es son como entre sobre the of a an to in for on and or with is are be by at
from as into via""".split())


_WHOLE: set[str] = set()       # stems that are whole words (nothing was taken off)
_IES: set[str] = set()         # stems of plurals in -ies (entities, series): + y or + ie
_KEEP_BASE: set[str] = set()   # stems also searched bare (none for now)
_SUFFIXES = ("aciones", "iciones", "acion", "icion", "ciones", "cion", "adores", "adoras",
             "ador", "adora", "adas", "ados", "ada", "ado", "ando", "iendo", "ings", "ing",
             "ions", "ion", "ers", "er", "ed", "ar", "ir")


def stem(word: str) -> str:
    """A light stem for prefix search (Spanish and English): accents and plurals off, then a
    verb or noun ending: exportar/exportación/exporter -> export, facturas -> factura,
    enviadas -> envi, entities -> entit (searched as entity, entitie)."""
    w = "".join(c for c in unicodedata.normalize("NFD", word.lower())
                if unicodedata.category(c) != "Mn")
    # Plurals: -ies (entities -> entity, series -> serie), -es after a consonant that takes
    # it in Spanish (factores, funciones, ordenes), else -s (clases, states, tickets).
    if len(w) > 4 and w.endswith("ies"):
        _IES.add(w[:-3])
        return w[:-3]
    if len(w) > 4 and w.endswith("es") and w[-3] in "lnrdzj":
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    _WHOLE.add(w)        # no ending taken off: the word itself, as a prefix
    return w


def words_of_symbol_owner(symbol: str, packages: bool = False) -> list[str]:
    """Words of the types around a symbol (and, with packages, of its last three package
    segments: a service in …billing.invoices is about invoices even if its name is not)."""
    from .index import words_of
    parts = S.parse(symbol)[:-1]
    out = [w for d in parts if d.suffix == S.TYPE for w in words_of(d.name).split()]
    if packages:
        pkgs = [d.name for d in parts if d.suffix == S.NAMESPACE][-3:]
        out += [w for p in pkgs for w in words_of(p).split()]
    return out


def prefix_family(stem_: str) -> list[str]:
    """The prefixes a stem stands for: the stem itself when it is a whole word, else the
    stem followed by the starts of its usual endings (import -> importac, importar, importad,
    importer…), so that it does not match words that only share the start (importe)."""
    base = stem_
    if base in _IES:
        return [base + "y", base + "ie"]
    if len(base) < 4 or base in _WHOLE:
        return [base]
    fam = [base + x for x in ("ac", "ar", "ad", "er", "ing", "ion", "ed", "or", "ab", "ib")]
    return fam + [base] if base in _KEEP_BASE else fam


def _plain(text: str) -> str:
    """Lowercase words without accents, camelCase split: "Facturas Pendientes"."""
    from .index import words_of
    words = []
    for tok in re.findall(r"[^\W_]+", text, re.UNICODE):
        words += words_of(tok).split() if any(c.isupper() for c in tok[1:]) else [tok.lower()]
    return " ".join("".join(c for c in unicodedata.normalize("NFD", w)
                            if unicodedata.category(c) != "Mn") for w in words)


def concept_terms(query: str) -> list[list[str]]:
    """What a concept query searches: one group of prefixes per word. Forms written by the
    caller as alternatives (pago|pagar|payment) are searched as given, as prefixes (a
    singular finds its plural); a word alone is reduced to a light stem and its usual
    endings (see stem and prefix_family). Stopwords and identifiers split as for names."""
    out: list[list[str]] = []
    for tok in query.split():
        if "|" in tok:
            group = [v for v in dict.fromkeys(_plain(x) for x in tok.split("|")) if v]
            if group and group not in out:
                out.append(group)
            continue
        for w in _plain(tok).split():
            if len(w) < 2 or w in _STOPWORDS:
                continue
            group = prefix_family(stem(w))
            if group not in out:
                out.append(group)
    return out[:12]


def scope_matches(scope: str, package: str, source: str) -> bool:
    """A scope may be a package (`com.example.billing`, `com/example/billing`, globs), a module
    or a directory (`billing`, `billing/src`), or a source path glob (`*/model/*.java`)."""
    if not scope:
        return True
    s = scope.strip().rstrip("/")
    if any(c in s for c in "*?["):
        return fnmatch.fnmatch(source, s.replace("**", "*")) or \
            fnmatch.fnmatch(package, s.replace("/", ".").replace("**", "*"))
    return source == s or source.startswith(s + "/") or _in_scope(package, s.replace("/", "."))


def _as_package(scope: str) -> str:
    """`dev/example/impl/` and `dev.example.impl` both mean the package dev.example.impl."""
    if not scope or any(c in scope for c in "*?[") and "/" not in scope:
        return scope
    return scope.strip("/").replace("/", ".")


def _in_scope(name: str, pattern: str) -> bool:
    """Package/module glob: `dev.example.ai` (and subpackages), `dev.example.*`, `**`."""
    if not pattern:
        return True
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(name, pattern.replace("**", "*"))
    return name == pattern or name.startswith(pattern + ".") or name.startswith(pattern + "/")


def _is_stale(root: str, source: str, con: sqlite3.Connection, shard_id: int) -> bool:
    row = con.execute("SELECT mtime_ns FROM shards WHERE id = ?", (shard_id,)).fetchone()
    try:
        return row is not None and os.stat(os.path.join(root, source)).st_mtime_ns > row[0]
    except OSError:
        return False


_HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git_diff_hunks(root: str, base: str, staged: bool, paths: list[str]
                   ) -> dict[str, list[tuple[int, int]]]:
    """{path: [(first new line, count)]} from `git diff -U0` (read-only)."""
    git = shutil.which("git")
    if not git:
        raise GraphError("git is not available")
    # --relative: paths relative to the project root (and only below it), as in the shards,
    # also when the build lives in a subdirectory of its git repository.
    cmd = [git, "-C", root, "-c", "core.quotePath=false", "diff", "-U0", "--no-color",
           "--no-ext-diff", "--relative"]
    if staged:
        cmd.append("--cached")
    if base:
        cmd.append(base)
    cmd.append("--")
    cmd.extend(paths)
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise GraphError("git diff failed: " + proc.stderr.decode(errors="replace").strip())
    out: dict[str, list[tuple[int, int]]] = {}
    current = None
    for line in proc.stdout.split(b"\n"):
        if line.startswith(b"+++ "):
            # git ends a name holding spaces with a TAB in this header, and quotes names
            # with special characters
            name = line[4:].rstrip(b"\t").decode("utf-8", errors="replace")
            if name.startswith('"') and name.endswith('"'):  # core.quotePath=false: only
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")  # " and \
            current = name[2:] if name.startswith("b/") else (None if name == "/dev/null"
                                                              else name)
            if current is not None:
                out.setdefault(current, [])
        elif current is not None:
            m = _HUNK.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                out[current].append((start, count))
    return out


TEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS text_files (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS text_words USING fts5(
    java_strings, java_annotations, other, tokenize = "unicode61 tokenchars '_$'");
"""

_IDENT = re.compile(r"(?:[^\W\d]|\$)[\w$]{2,}")
_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')


def _tokens(path: str, text: str) -> tuple[str, str, str]:
    """(identifiers in .java string literals, those in annotation lines, identifiers of any
    other file) — unique, space-separated."""
    if path.endswith(".java"):
        strings, annots = set(), set()
        for line in text.split("\n"):
            lits = _STRING.findall(line)
            if not lits:
                continue
            words = {w for lit in lits for w in _IDENT.findall(lit)}
            strings |= words
            if line.lstrip().startswith("@"):
                annots |= words
        return " ".join(sorted(strings)), " ".join(sorted(annots)), ""
    return "", "", " ".join(sorted(set(_IDENT.findall(text))))


def _innermost(defs, ln):
    best = None
    for d in defs:
        if d[2] <= ln <= d[3] and (best is None or (d[3] - d[2]) < (best[3] - best[2])):
            best = d
    return best


def _next_member(defs, owner, ln):
    """The member of `owner` that starts first after line ln (javadoc/annotations belong to it)."""
    best = None
    for d in defs:
        if d is owner or not (owner[2] <= d[2] <= owner[3]) or S.parent(d[1]) != owner[1]:
            continue
        start = min(d[2], d[4])
        if start >= ln and (best is None or start < min(best[2], best[4])):
            best = d
    return best


def _is_preamble(line: str) -> bool:
    return line.startswith(("/**", "/*", "*", "//", "@")) or line == "*/"


def _header_lines(text: list[str], decl_line: int) -> int:
    """Lines of a type declaration from its name to the opening brace (extends, implements…)."""
    for k in range(decl_line, min(decl_line + 15, len(text))):
        if "{" in text[k]:
            return k - decl_line
    return 0


def git_untracked_java(root: str, paths: list[str]) -> list[str]:
    git = shutil.which("git")
    if not git:
        return []
    cmd = [git, "-C", root, "ls-files", "--others", "--exclude-standard", "-z", "--"]
    cmd += paths or ["*.java"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return [p.decode("utf-8", errors="replace") for p in proc.stdout.split(b"\0")
            if p.endswith(b".java")]


MAX_TEXT_BYTES = 2_000_000  # bigger files are generated (graphs, dumps, minified bundles)
# Never searched by name: hidden directories (editor and CI settings, tools), dependencies and
# their lockfiles, and documentation (notes and data dumps there mention names without using
# them).
DEFAULT_BY_NAME_EXCLUDE = (".*/*", "*/.*/*",            # hidden directories (.github, .idea…)
                           "*/node_modules/*", "node_modules/*", "*.min.js",
                           "*package-lock.json", "*npm-shrinkwrap.json", "*yarn.lock",
                           "*pnpm-lock.yaml", "*.lock",
                           "docs/*", "*/docs/*", "doc/*", "*/doc/*", "*.md", "*.adoc")


def candidate_files(root: str, globs: list[str], exclude: list[str] = (),
                    max_bytes: int = MAX_TEXT_BYTES) -> list[str]:
    """Files to search by name: tracked or untracked-but-not-ignored (git), matching globs, not
    excluded, not huge. Without git: a walk that skips build and dependency directories."""
    git = shutil.which("git")
    paths: list[str] = []
    if git:
        try:
            proc = subprocess.run([git, "-C", root, "ls-files", "-z", "--cached", "--others",
                                   "--exclude-standard"], capture_output=True, timeout=60)
            if proc.returncode == 0:
                paths = [p.decode("utf-8", errors="replace") for p in proc.stdout.split(b"\0")
                         if p]
        except (OSError, subprocess.SubprocessError):
            paths = []
    if not paths:
        skip = {".git", "build", "target", "out", "node_modules", ".gradle", ".idea", "venv",
                ".venv", "dist"}
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in skip and not x.startswith(".")]
            rel = os.path.relpath(d, root)
            paths.extend(n if rel == "." else f"{rel}/{n}" for n in files)
    out = []
    exclude = list(exclude) + list(DEFAULT_BY_NAME_EXCLUDE)
    for p in sorted(set(paths)):
        base = p.rsplit("/", 1)[-1]
        if not any(fnmatch.fnmatch(base, g) for g in globs):
            continue
        if any(fnmatch.fnmatch(p, x.replace("**", "*")) for x in exclude):
            continue
        try:
            if os.path.getsize(os.path.join(root, p)) > max_bytes:
                continue
        except OSError:
            continue
        out.append(p)
    return out


# Not "reports": report templates (.jrxml) are where names are used as text.
_GENERATED_DIR = re.compile(r"(?:^|[-_.])(?:out|output|generated|gen|dist)$",
                            re.I)
# A "generated / do not edit" comment line (`//`, `/*`, ` *`, `#`, `<!--`), or the
# @Generated annotation (javax/jakarta) that tools put on the type. Not @AutoGenerated: a
# visual designer marks only the members it generated with it, not the whole file.
# A generator's notice in a comment: in the file's banner, before `package` (jOOQ, protobuf,
# Axis, JAXB and wsimport in their localized forms), never in the type's javadoc, where prose
# says such things ("the CSS generated by the editor").
_GENERATED_HEADER = re.compile(rb"(?im)^\s*(?://|/\*|\*|#|<!--).*?(?:@generated\b|do not edit|"
                               rb"auto[- ]?generated|generated by|"
                               # localized tool headers (JAXB, wsimport…): es, pt, fr
                               rb"generad[oa]s? (?:por|autom|el\b)|gerad[oa]s? (?:por|autom)|"
                               rb"g\xc3\xa9n\xc3\xa9r\xc3\xa9e?s? (?:par|autom))")
# … or an annotation on the type (@Generated, javax/jakarta.annotation[.processing]).
_GENERATED_ANNOTATION = re.compile(rb"(?m)^\s*@(?:(?:javax|jakarta)\.annotation\."
                                   rb"(?:processing\.)?)?Generated\b")
_PACKAGE_OR_IMPORT = re.compile(rb"(?m)^\s*(?:package|import)\s")
_COMMENT_BYTES = re.compile(rb"/\*.*?\*/|//[^\n]*", re.S)


def generated_header(data: bytes) -> bool:
    """Whether a source's start says it is generated: a generator's notice in the comments
    before `package` (or, without a package, before the type's own javadoc), or @Generated
    on the type."""
    head = _file_header(data)
    pk = _PACKAGE_OR_IMPORT.search(head)
    if pk:
        banner, rest = head[:pk.start()], head[pk.start():]
    else:
        last_doc = head.rfind(b"/**")
        banner, rest = (head[:last_doc], head[last_doc:]) if last_doc >= 0 else (head, b"")
    return _GENERATED_HEADER.search(banner) is not None or \
        _GENERATED_ANNOTATION.search(_COMMENT_BYTES.sub(b" ", rest)) is not None


_TYPE_START = re.compile(rb"(?m)^[ \t]*(?:(?:public|protected|private|abstract|final|static|"
                         rb"sealed|non-sealed|strictfp)\s+)*(?:class|interface|enum|record|"
                         rb"@\s*interface)\s+[\w$]")


def _file_header(data: bytes) -> bytes:
    """What comes before the first type declaration (license, generator notice, imports,
    type annotations), or the first 400 bytes of a file without one. Comments inside the
    code, such as Eclipse's "Auto-generated method stub", do not count."""
    m = _TYPE_START.search(data)
    return data[:m.start()] if m else data[:400]


GENERATED_SHARE = 0.9  # share of a directory's files with a "generated" header to propose it


def generated_dirs(root: str, globs=DEAD_CODE_GLOBS, min_files: int = 3,
                   java_only: bool = False) -> list[str]:
    """Directories that look generated, for `init-config` to propose excluding: named like
    generated output (`*-out`, `generated`, `gen`, `dist`) and holding no Java sources (a
    package may be called `gen`), or where at least
    GENERATED_SHARE of the files below start with a "generated / do not edit" header. The
    highest such directory is proposed; in a mixed one (own code next to generated packages)
    only its generated subdirectories are. build/ and target/ are never searched anyway.
    java_only: only directories holding Java sources (for [dead_code] exclude)."""
    total: dict[str, int] = defaultdict(int)
    marked: dict[str, int] = defaultdict(int)
    java: dict[str, int] = defaultdict(int)
    children: dict[str, set[str]] = defaultdict(set)
    for p in candidate_files(root, list(globs)):
        try:
            with open(os.path.join(root, p), "rb") as fh:
                gen = generated_header(fh.read(1500))
        except OSError:
            continue
        parts = p.split("/")[:-1]
        parent = ""
        for k in range(1, len(parts) + 1):
            d = "/".join(parts[:k])
            total[d] += 1
            marked[d] += gen
            java[d] += p.endswith(".java")
            children[parent].add(d)
            parent = d
    out: list[str] = []

    def visit(d: str) -> None:
        name = d.rsplit("/", 1)[-1]
        by_name = _GENERATED_DIR.search(name) is not None and not java[d]
        if total[d] >= min_files and (by_name or marked[d] >= GENERATED_SHARE * total[d]):
            if not java_only or java[d]:
                out.append(d)
            return
        for c in sorted(children.get(d, ())):
            visit(c)

    for top in sorted(children.get("", ())):
        visit(top)
    return out


def names_in_strings(root: str, names: list[str], globs: list[str],
                     properties: set[str] = frozenset(), exclude: list[str] = ()
                     ) -> dict[str, str]:
    """{name: first file it was seen in} for the names that appear where code refers to members
    by name: inside string literals of .java files, or anywhere in the other files matching
    globs. Bean property names (`properties`, from getX/setX) only count in .java when the
    literal is an annotation argument (e.g. @JsonProperty("x")): plain strings such as "name"
    or "description" are too common. One grep pass over the candidate files."""
    if not names:
        return {}
    grep = shutil.which("grep")
    wanted = set(names)
    in_string = re.compile(r'"(?:[^"\\]|\\.)*"')
    ident = re.compile(r"(?:[^\W\d]|\$)[\w$]*")
    found: dict[str, str] = {}
    files = candidate_files(root, globs, list(exclude))
    if not grep or not files:
        return found
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("\n".join(names) + "\n")
        pat = fh.name
    try:
        base = [grep, "-IwF", "-f", pat, "--null", "-n", "--"]
        for chunk in _chunked(files):
            proc = subprocess.run(base + chunk, cwd=root, capture_output=True)
            for rec in proc.stdout.split(b"\n"):
                path, _, rest = rec.partition(b"\0")
                if not rest:
                    continue
                text = rest.partition(b":")[2].decode("utf-8", errors="replace")
                src = path.decode("utf-8", errors="replace")
                if src.endswith(".java"):
                    annotation_line = text.lstrip().startswith("@")
                    for lit in in_string.finditer(text):
                        for w in ident.findall(lit.group(0)):
                            if w in wanted and (w not in properties or annotation_line):
                                found.setdefault(w, src)
                else:
                    for w in ident.findall(text):
                        if w in wanted:
                            found.setdefault(w, src)
    finally:
        os.unlink(pat)
    return found


def _chunked(paths: list[str], max_bytes: int = 120_000) -> list[list[str]]:
    out, cur, size = [], [], 0
    for p in paths:
        if cur and size + len(p) + 1 > max_bytes:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p) + 1
    if cur:
        out.append(cur)
    return out

"""Queries over the auxiliary index (v2): transitive callers, call paths, affected tests,
changed code, dependencies between modules or packages, dead-code candidates and censuses."""

from __future__ import annotations

import fnmatch
import os
import re
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
            "SELECT target_id FROM rels WHERE symbol_id = ? AND kind = ?",
            (sid, REL_IMPLEMENTATION))]

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
               max_nodes: int = 20000) -> tuple[list[Node], bool]:
        """Transitive callers, breadth first. With include_overridden, calls to the methods it
        overrides count too (they may dispatch here). Returns (nodes, truncated)."""
        start = self.ids([symbol]).get(symbol)
        if start is None:
            return [], False
        frontier = [start]
        if include_overridden and S.is_method(symbol):
            frontier += self.ancestors(start)
        seen = set(frontier)
        nodes: dict[int, Node] = {}
        names: dict[int, str] = self.names(frontier)
        truncated = False
        sh = self.shards()
        for level in range(1, depth + 1):
            rows = self.callers_of(frontier)
            nxt = []
            for target, caller, shard_id, sites in rows:
                if caller in seen or shard_id not in sh:
                    continue
                seen.add(caller)
                nxt.append(caller)
                src, mod, test = sh[shard_id]
                nodes[caller] = Node("", level, names.get(target, ""), src, 0, module_of(mod),
                                     bool(test), _lines(sites))
                if len(nodes) >= max_nodes:
                    truncated = True
                    break
            if truncated or not nxt:
                break
            names.update(self.names(nxt))
            frontier = nxt
        names.update(self.names(nodes))
        defs = self.definitions(nodes)
        for sid, n in nodes.items():
            n.symbol = names.get(sid, "")
            d = defs.get(sid)
            if d:
                n.line = d[1]
        return sorted(nodes.values(), key=lambda n: (n.depth, n.module, n.source, n.line)), \
            truncated

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
        (package glob, or a symbol prefix such as `org/flowable/`): (caller, target, source,
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
                  entry_type_names=(), entry_packages=(), by_name_globs=DEAD_CODE_GLOBS,
                  root: str = "") -> list[tuple]:
        """Candidates: project definitions nobody references. Left out: overrides and what is
        overridden (dispatch), Lombok-generated members and fields with generated accessors that
        are used, main/serialization hooks, no-arg constructors of persistent classes, members
        carrying a non-java.lang annotation (framework-managed) or one of entry_annotations, and
        names used inside string literals. Returns [(symbol, kind, source, line, signature)]."""
        sh = self.shards()
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
        if out and root:
            aliases: dict[str, set[str]] = defaultdict(set)   # name in text -> candidate names
            for x in out:
                n = S.name_of(x[0])
                aliases[n].add(n)
                m = re.match(r"(get|set|is)([A-Z][\w$]*)$", n)
                if m:  # templates and reports use the bean property: getActividad -> actividad
                    aliases[m.group(2)[0].lower() + m.group(2)[1:]].add(n)
            names = sorted(a for a in aliases if len(a) > 2)
            used_words = names_in_strings(root, names, list(by_name_globs))
            used = {c for w in used_words for c in aliases.get(w, ())}
            out = [x for x in out if S.name_of(x[0]) not in used]
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
        for ann, shard_id in self.con.execute(
                "SELECT s.symbol, a.shard_id FROM annotations a JOIN symbols s "
                "ON s.id = a.annotation_id"):
            if shard_id not in sh or (scope and not fnmatch.fnmatch(sh[shard_id][0], scope)):
                continue
            c = counts.setdefault(ann, [0, set()])
            c[0] += 1
            c[1].add(shard_id)
        rows = sorted(((a, n, len(f)) for a, (n, f) in counts.items()), key=lambda x: -x[1])
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
        return sorted(out, key=lambda x: (x[1], x[2]))


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

def _lines(sites: str) -> list[int]:
    out = []
    for s in sites.split(","):
        if s:
            out.append(int(s.split(":", 1)[0]) + 1)
    return sorted(set(out))


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
    cmd = [git, "-C", root, "diff", "-U0", "--no-color", "--no-ext-diff"]
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
            name = line[4:].decode("utf-8", errors="replace")
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


def names_in_strings(root: str, names: list[str], globs: list[str]) -> set[str]:
    """Which of `names` appear inside a string literal of a .java file (or anywhere in other
    files matching globs): one grep pass for all of them, then a set lookup per word."""
    if not names:
        return set()
    grep = shutil.which("grep")
    wanted = set(names)
    in_string = re.compile(r'"(?:[^"\\]|\\.)*"')
    ident = re.compile(r"[A-Za-z_$][\w$]*")
    found: set[str] = set()
    if not grep:
        return found
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("\n".join(names) + "\n")
        pat = fh.name
    try:
        args = [grep, "-rIwF", "-f", pat, "--null", "-n"]
        args += [f"--include={g}" for g in globs]
        args += ["--exclude-dir=" + d for d in (".git", "build", "target", "out",
                                                 "node_modules", ".gradle", ".idea")]
        args += ["--", "."]
        proc = subprocess.run(args, cwd=root, capture_output=True)
        for rec in proc.stdout.split(b"\n"):
            path, _, rest = rec.partition(b"\0")
            text = rest.partition(b":")[2].decode("utf-8", errors="replace")
            chunks = [m.group(0) for m in in_string.finditer(text)] \
                if path.endswith(b".java") else [text]
            for chunk in chunks:
                found.update(w for w in ident.findall(chunk) if w in wanted)
    finally:
        os.unlink(pat)
    return found

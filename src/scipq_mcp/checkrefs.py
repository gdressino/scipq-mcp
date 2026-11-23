"""check_refs: the code that Markdown (docs, skills, agent instructions) mentions, checked
against the index.

Mentions: `Type#member`, `Type.method(…)`, `Type.member`, `com.example.Type`, {@link …}, and
paths `dir/File.java` or `dir/File.java:120`. Each is resolved with the auxiliary index at
once (types by simple name, members by type, supertypes), not with a search per mention.
Reported: types and members that are not there (with the closest names), parameter lists no
overload has, ambiguous simple names, missing files, and line numbers that no longer fall in
the symbol named next to them.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from . import symbols as S
from .index import REL_IMPLEMENTATION

# Type#member(…) / Type.member(…) / Type.member — Type starts upper case, may be qualified or
# nested (Outer.Inner).
_HASH = re.compile(r"(?<![\w$.])((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*)#"
                   r"([A-Za-z_$][\w$]*)(\(([^()]*)\))?")
_DOT_CALL = re.compile(r"(?<![\w$.])((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*)\."
                       r"([a-z_$][\w$]*)\(([^()]*)\)?")
# Not members: file extensions and class literals (Foo.feature, Foo.class).
_NOT_MEMBERS = {"class", "java", "feature", "md", "xml", "sql", "json", "properties", "txt",
                "groovy", "jrxml", "yml", "yaml", "html", "kt", "gradle", "jar", "log"}
_FQN = re.compile(r"(?<![\w$./])((?:[a-z_][\w$]*\.){2,}[A-Z][\w$]*)(?![\w$(])")
_PATH = re.compile(r"(?<![\w$./-])((?:(?:[\w$.-]+|\.\.\.|…)/)*[\w$-]+\.java)"
                   r"(?::(\d+)(?:-(\d+))?)?")
_CODE = re.compile(r"`[^`\n]+`|\{@link(?:plain)?\s[^}]+\}")
_LIB_ROOTS = {"java/lang/Object#", "java/lang/Enum#", "java/lang/Record#"}


@dataclass
class Problem:
    path: str
    line: int
    mention: str
    what: str


class Model:
    """The project's definitions from the auxiliary index, for name lookups."""

    def __init__(self, g):
        self.g = g
        self.types: dict[str, list[str]] = {}       # simple name -> type symbols (project)
        self.members: dict[str, dict[str, list[str]]] = {}   # type -> name -> signatures
        self.member_syms: dict[tuple[str, str], list[str]] = {}
        self.where: dict[str, tuple[str, int, int | None, int | None]] = {}
        sh = g.shards()
        for sym, shard_id, line, b0, b1, sig in g.con.execute(
                "SELECT s.symbol, d.shard_id, d.line, d.body_start, d.body_end, d.signature "
                "FROM defs d JOIN symbols s ON s.id = d.symbol_id"):
            if shard_id not in sh or S.is_local_like(sym):
                continue
            if sym not in self.where:
                self.where[sym] = (sh[shard_id][0], line + 1, b0, b1)
            if S.is_type(sym):
                self.types.setdefault(S.name_of(sym), []).append(sym)
            parent = S.parent(sym)
            if parent.endswith("#") and S.kind_of(sym) in ("method", "term", "constructor",
                                                           "type"):
                mname = S.name_of(S.parent(sym)) if S.kind_of(sym) == "constructor" \
                    else S.name_of(sym)
                self.members.setdefault(parent, {}).setdefault(mname, []).append(sig or "")
                self.member_syms.setdefault((parent, mname), []).append(sym)
        for k in self.types:
            self.types[k] = sorted(set(self.types[k]))
        self.external = {x for (x,) in g.con.execute(
            "SELECT symbol FROM symbols WHERE symbol LIKE '%#' AND id NOT IN "
            "(SELECT symbol_id FROM defs)")}
        self.external_names = {S.name_of(x) for x in self.external}
        self._supers: dict[str, list[str]] = {}

    def supertypes(self, t: str) -> list[str]:
        if t not in self._supers:
            ids = self.g.ids([t])
            if t not in ids:
                self._supers[t] = []
            else:
                self._supers[t] = [x for x in self.g.names([y for (y,) in self.g.con.execute(
                    "SELECT DISTINCT target_id FROM rels WHERE symbol_id = ? AND kind = ?",
                    (ids[t], REL_IMPLEMENTATION))]).values() if x.endswith("#")]
        return self._supers[t]

    def resolve_type(self, written: str) -> tuple[list[str], bool]:
        """(project type symbols the written name may be, whether it is a library type)."""
        parts = written.split(".")
        simple = parts[-1]
        cands = self.types.get(simple, [])
        if len(parts) > 1:
            pkg = [p for p in parts if p[:1].islower()]
            outer = [p for p in parts[:-1] if p[:1].isupper()]
            want_pkg = "/".join(pkg)
            cands = [c for c in cands if (not pkg or S.package_of(c).replace(".", "/")
                                          == want_pkg)
                     and all(f"/{o}#" in c or c.startswith(f"{o}#") or f"#{o}#" in c
                             for o in outer)]
        library = not cands and (simple in self.external_names)
        return cands, library

    def member(self, t: str, name: str) -> tuple[list[str] | None, bool]:
        """(signatures of `name` in the type or its project supertypes, None when absent;
        whether a library supertype might declare it)."""
        seen, todo, lib = set(), [t], False
        found: list[str] = []
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            if x in self.members:
                found += self.members[x].get(name, [])
                todo += self.supertypes(x)
            elif x not in _LIB_ROOTS:
                lib = True
        return (found or None), lib

    def names_in(self, t: str) -> list[str]:
        out = set(self.members.get(t, {}))
        for s in self.supertypes(t):
            out |= set(self.members.get(s, {}))
        return sorted(out)


def _files(root: str, globs: list[str], exclude: list[str]) -> list[str]:
    git = shutil.which("git")
    listed = None
    if git:
        try:
            proc = subprocess.run([git, "-C", root, "-c", "core.quotePath=false", "ls-files",
                                   "-z", "--cached", "--others", "--exclude-standard"],
                                  capture_output=True, timeout=60)
            if proc.returncode == 0:
                listed = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
        except (OSError, subprocess.SubprocessError):
            listed = None
    if listed is None:
        listed = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in (".git", "build", "target", "node_modules")]
            for n in names:
                listed.append(os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/"))

    def match(p: str, gl: str) -> bool:
        return fnmatch.fnmatch(p, gl) or (gl.startswith("**/") and fnmatch.fnmatch(p, gl[3:]))
    return sorted(p for p in listed if any(match(p, g_) for g_ in globs)
                  and not any(match(p, x) for x in exclude))


def _params(text: str) -> tuple:
    from .refactor import _link_params
    return _link_params(text or "")


def _closest(name: str, names: list[str]) -> str:
    near = difflib.get_close_matches(name, names, n=2, cutoff=0.7)
    return f" (did you mean {', '.join(near)}?)" if near else ""


def check(g, root: str, globs: list[str], exclude: list[str]
          ) -> tuple[list[Problem], int, int]:
    """(problems, mentions checked, files read)."""
    m = Model(g)
    sources = {v[0] for v in m.where.values()}
    by_suffix: dict[str, list[str]] = {}
    for s in sources:
        parts = s.split("/")
        for k in range(len(parts)):
            by_suffix.setdefault("/".join(parts[k:]), []).append(s)
    problems: list[Problem] = []
    checked = 0
    files = _files(root, globs, exclude)
    line_counts: dict[str, int] = {}
    for path in files:
        try:
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for ln, line in enumerate(text.split("\n"), 1):
            code = [mo.span() for mo in _CODE.finditer(line)]

            def in_code(pos: int) -> bool:
                return any(a <= pos < b for a, b in code)
            named_here: list[str] = []      # symbols this line names, for its line numbers
            spans = []
            for mo in _HASH.finditer(line):
                spans.append(mo.span())
                if mo.group(2) in _NOT_MEMBERS:
                    continue
                checked += 1
                named_here += _check_member(m, path, ln, mo.group(0), mo.group(1), mo.group(2),
                                            mo.group(4) if mo.group(3) else None, True,
                                            problems)
            for mo in _DOT_CALL.finditer(line):
                if any(a <= mo.start() < b for a, b in spans) or mo.group(2) in _NOT_MEMBERS:
                    continue
                spans.append(mo.span())
                checked += 1
                named_here += _check_member(m, path, ln, mo.group(0).rstrip(), mo.group(1),
                                            mo.group(2), mo.group(3), in_code(mo.start()),
                                            problems, call=True)
            for mo in _FQN.finditer(line):
                if any(a <= mo.start() < b for a, b in spans) or not in_code(mo.start()):
                    continue
                checked += 1
                cands, lib = m.resolve_type(mo.group(1))
                if cands:
                    named_here += cands
                elif not lib and _looks_like_project(mo.group(1), m):
                    simple = mo.group(1).rsplit(".", 1)[-1]
                    problems.append(Problem(path, ln, mo.group(1), "no such type"
                                            + _closest(simple, list(m.types))))
            for mo in _PATH.finditer(line):
                checked += 1
                written, n0 = mo.group(1), mo.group(2)
                hits = _find_path(written, by_suffix, sources)
                if not hits and os.path.isfile(os.path.join(root, written)):
                    hits = [written]
                if not hits:
                    base = os.path.basename(written)
                    same = sorted({s for s in sources if os.path.basename(s) == base})
                    problems.append(Problem(path, ln, mo.group(0), "no such file"
                                            + (f" (a {base} is at {', '.join(same[:2])})"
                                               if same else "")))
                    continue
                if n0 is None:
                    continue
                n = int(n0)
                src = hits[0]
                if src not in line_counts:
                    try:
                        with open(os.path.join(root, src), encoding="utf-8",
                                  errors="replace") as fh:
                            line_counts[src] = fh.read().count("\n") + 1
                    except OSError:
                        line_counts[src] = 0
                if n > line_counts[src]:
                    problems.append(Problem(path, ln, mo.group(0), f"line {n} is past the end "
                                            f"({line_counts[src]} lines)"))
                    continue
                # The symbols named on the same line in that file: does the line still fall in
                # one of them? (Several may be named; the nearest one is reported.)
                spans_here = []
                for sym in named_here:
                    w = m.where.get(sym)
                    if w is None or w[0] != src:
                        continue
                    lo = min((w[2] + 1) if w[2] is not None else w[1], w[1])
                    hi = (w[3] + 1) if w[3] is not None else w[1]
                    spans_here.append((sym, lo, hi))
                if spans_here and not any(lo - 2 <= n <= hi + 1 for _s, lo, hi in spans_here):
                    sym, lo, hi = min(spans_here, key=lambda x: min(abs(n - x[1]), abs(n - x[2])))
                    problems.append(Problem(path, ln, mo.group(0),
                                            f"line {n} is not in {S.short(sym)} any more "
                                            f"(now lines {lo}–{hi})"))
    return problems, checked, len(files)


def _find_path(written: str, by_suffix: dict, sources: set[str]) -> list[str]:
    """Sources a written path names: a suffix of a path, or with `.../` standing for any
    folders (core/.../model/Account.java)."""
    w = written.lstrip("./") if not written.startswith("...") else written
    if "..." not in w and "…" not in w:
        return by_suffix.get(w, [])
    parts = [p for p in re.split(r"(?:^|/)(?:\.\.\.|…)(?:/|$)", w) if p]
    if not parts:
        return []
    tail = parts[-1]
    out = []
    for s in by_suffix.get(tail, []):
        pos = 0
        ok = True
        for part in parts[:-1]:
            k = s.find(part, pos)
            if k < 0:
                ok = False
                break
            pos = k + len(part)
        if ok:
            out.append(s)
    return out


def _looks_like_project(fqn: str, m: Model) -> bool:
    """A qualified name in one of the project's own packages."""
    pkg = fqn.rsplit(".", 1)[0].replace(".", "/") + "/"
    return any(t.startswith(pkg) for ts in m.types.values() for t in ts[:1]) or \
        any(t.startswith(pkg.rsplit("/", 2)[0] + "/") for ts in m.types.values()
            for t in ts[:1])


def _check_member(m: Model, path: str, ln: int, mention: str, written_type: str, name: str,
                  params, strict: bool, problems: list[Problem], call: bool = False
                  ) -> list[str]:
    """Checks Type#name / Type.name(…); the symbols it resolved to."""
    cands, lib = m.resolve_type(written_type)
    if not cands:
        # Prose mentions an unknown capitalized word followed by .call(): only code spans,
        # links and # forms are sure to be code.
        if not lib and strict and not _is_library_like(written_type):
            problems.append(Problem(path, ln, mention, f"no type {written_type}"
                                    + _closest(written_type.split(".")[-1], list(m.types))))
        return []
    if len(cands) > 1 and "." not in written_type:
        found = [c for c in cands if m.member(c, name)[0] is not None]
        if len(found) > 1:
            problems.append(Problem(path, ln, mention, f"ambiguous: {len(found)} types named "
                                    f"{written_type} have {name} ("
                                    + ", ".join(S.package_of(c) for c in found[:3]) + ")"))
            return []
        cands = found or cands[:1]
    t = cands[0]
    sigs, lib_super = m.member(t, name)
    if sigs is None:
        if lib_super or written_type.split(".")[-1] in m.external_names:
            return [t]          # maybe inherited from, or meant as, a library type
        problems.append(Problem(path, ln, mention, f"{S.short(t)} has no member {name}"
                                + _closest(name, m.names_in(t))))
        return [t]
    if params is not None and params.strip() and not call:
        want = _params(params)
        from .refactor import _param_types
        have = [_param_types(s) for s in sigs if "(" in s]
        if have and want not in have:
            shown = "; ".join("(" + ", ".join(h) + ")" for h in have[:3])
            problems.append(Problem(path, ln, mention, f"no {name}({', '.join(want)}) in "
                                    f"{S.short(t)}: its overloads take {shown}"))
    return m.member_syms.get((t, name)) or [t]


def _is_library_like(written: str) -> bool:
    return written.split(".")[0] in ("System", "Math", "String", "Integer", "Long", "List",
                                     "Map", "Set", "Arrays", "Collections", "Objects",
                                     "Optional", "Stream", "Collectors", "Files", "Paths",
                                     "Path", "LocalDate", "LocalDateTime", "Instant",
                                     "Duration", "BigDecimal", "Thread", "Class")


def render(problems: list[Problem], checked: int, nfiles: int, offset: int, limit: int) -> str:
    if not nfiles:
        return "check_refs: no file matches (files: globs relative to the checkout)"
    head = (f"check_refs: {checked} mentions in {nfiles} files; "
            f"{len(problems) or 'no'} problem{'s' if len(problems) != 1 else ''}")
    if not problems:
        return head
    lines = [head + " (by folder):"]
    by_dir: dict[str, list[Problem]] = {}
    for p in problems:
        by_dir.setdefault(os.path.dirname(p.path) or ".", []).append(p)
    rows = []
    for d in sorted(by_dir):
        rows.append(f"{d}/ ({len(by_dir[d])})")
        for p in by_dir[d]:
            rows.append(f"  {p.path}:{p.line}  {p.mention[:80]}  — {p.what}")
    shown = rows[offset:offset + limit]
    lines += shown
    if offset + limit < len(rows):
        lines.append(f"next offset {offset + limit}")
    return "\n".join(lines)

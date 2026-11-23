"""Queries built on the core ones: source, members, annotated, typed, family, overloads,
uses by name, index status and tests. All read-only, all without stored state."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field

from . import symbols as S
from .queries import Definition, Location, Navigator, QueryError, Reference, signature_line
from .workspace import SUFFIX, build_task

SKIP_DIRS = {".git", ".gradle", ".idea", ".vscode", "build", "target", "out", "node_modules",
             ".mvn", "bin", ".venv", "__pycache__"}
TEST_TASK = re.compile(r"test", re.IGNORECASE)
TEST_PATH = re.compile(r"(^|/)src/[A-Za-z]*[tT]est[A-Za-z]*/")
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;")


# --- source -----------------------------------------------------------------------------------

@dataclass
class Source:
    definition: Definition
    lines: list[tuple[int, str]]  # (1-based line, text)
    end_line: int                 # last line of the body, 1-based
    truncated: bool


def source(nav: Navigator, symbol: str, max_lines: int = 200) -> Source | None:
    d = nav.definition(symbol)
    if d is None:
        return None
    rng = d.enclosing or d.location.range
    first, last = rng.start_line, rng.end_line
    if rng.end_char == 0 and last > first:
        last -= 1  # half-open range ending at column 0 of the next line
    shown_last = min(last, first + max(1, max_lines) - 1)
    raw = read_raw_lines(nav.ws.root, d.location.source, first, shown_last)
    lines = [(first + 1 + k, t) for k, t in enumerate(raw)]
    return Source(d, lines, last + 1, shown_last < last)


def read_raw_lines(root: str, source: str, first: int, last: int) -> list[str]:
    """Lines first..last (0-based, inclusive) without stripping."""
    out = []
    try:
        with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
            for i, t in enumerate(fh):
                if i > last:
                    break
                if i >= first:
                    out.append(t.rstrip("\n"))
    except OSError:
        pass
    return out


def record_accessor(nav: Navigator, symbol: str, defs: list[Definition]) -> str:
    """For a record component (a field of a record), its implicit accessor symbol, else ""."""
    if S.kind_of(symbol) != "term" or not defs:
        return ""
    owner = nav.definition(S.parent(symbol))
    if owner is None or owner.kind != "record":
        return ""
    return S.parent(symbol) + S.format_descriptors([S.Descriptor(S.name_of(symbol), S.METHOD)])


def in_scope(symbol: str, source: str, scope: str) -> bool:
    """`scope` is a package (prefix, e.g. `com.example.billing`, or glob) or, when it contains
    a '/', a source path glob (`*/billing/*`, `core/src/**`)."""
    if not scope:
        return True
    if "/" in scope or scope.endswith(".java"):
        pattern = scope.replace("**", "*")
        return fnmatch.fnmatch(source, pattern) or source.startswith(scope.rstrip("*"))
    pkg = S.package_of(symbol) if symbol else ""
    if any(c in scope for c in "*?["):
        return fnmatch.fnmatch(pkg, scope.replace("**", "*"))
    return pkg == scope or pkg.startswith(scope + ".")


_JAVADOC_TAG = re.compile(r"(\{@(?:link|linkplain)\s+|@see\s+|@throws\s+|@exception\s+)"
                          r"([\w.$]*(?:#[\w$]+(?:\([^)]*\))?)?)")


def javadoc_mentions(root: str, symbol: str, limit: int = 100) -> list[NameHit]:
    """{@link X}, {@link X#m}, @see X… that point at the symbol: javadoc is not indexed."""
    d = S.parse(symbol)
    if not d:
        return []
    if S.is_type(symbol):
        type_name, member = d[-1].name, ""
    else:
        type_name = S.name_of(S.parent(symbol)) if S.parent(symbol) else ""
        member = S.name_of(symbol)
    needle = member or type_name
    if not needle:
        return []
    out = []
    for source, line, text in _grep_words(root, [needle], ["*.java"]):
        for m in _JAVADOC_TAG.finditer(text):
            ref = m.group(2)
            target, _, mem = ref.partition("#")
            simple = target.rsplit(".", 1)[-1]
            if member:
                ok = mem.split("(")[0] == member and simple in ("", type_name)
            else:
                ok = simple == type_name
            if ok:
                out.append(NameHit(source, line, text.strip()[:160], ref))
                break
        if len(out) >= limit:
            break
    return out


# --- members ----------------------------------------------------------------------------------

@dataclass
class MemberGroup:
    owner: str                      # type symbol
    definition: Definition | None   # of the owner type
    members: list[Definition] = field(default_factory=list)
    overrides: dict[str, list[str]] = field(default_factory=dict)  # member -> what it overrides
    hidden: int = 0                 # inherited members overridden by a nearer type


def _own_members(nav: Navigator, type_symbol: str, d: Definition) -> list[Definition]:
    for v in nav._views(nav.ws.shards_for_source(d.location.source)[:1]):
        return sorted(v.definitions(lambda sym, _i: not S.is_local_like(sym)
                                    and S.parent(sym) == type_symbol),
                      key=lambda x: (x.location.range.start_line, x.location.range.start_char))
    return []


def members(nav: Navigator, type_symbol: str, inherited: bool = True
            ) -> tuple[list[MemberGroup], list[str]]:
    """(groups, external supertypes): the type's own members first, then each supertype's
    non-private, non-constructor members that are not overridden by a nearer type."""
    d = nav.definition(type_symbol)
    if d is None:
        raise QueryError(f"{type_symbol} is not defined in this project")
    if not S.is_type(type_symbol):
        raise QueryError(f"{type_symbol} is not a type")
    own = MemberGroup(type_symbol, d, _own_members(nav, type_symbol, d))
    groups = [own]
    external: list[str] = []
    overridden: set[str] = set()

    def note_overrides(g: MemberGroup):
        for m in g.members:
            if m.info and S.is_method(m.symbol):
                over = [r.symbol for r in m.info.relationships if r.is_implementation]
                if over:
                    g.overrides[m.symbol] = over
                    overridden.update(over)

    note_overrides(own)
    if inherited:
        _, supers = nav.supertypes(type_symbol)
        for sup in supers:  # direct first
            if sup.definition is None:
                external.append(sup.symbol)
                continue
            g = MemberGroup(sup.symbol, sup.definition)
            for m in _own_members(nav, sup.symbol, sup.definition):
                sig = m.info.signature if m.info else ""
                if S.kind_of(m.symbol) == "constructor" or _is_private(sig):
                    continue
                if m.symbol in overridden:
                    g.hidden += 1
                    continue
                g.members.append(m)
            note_overrides(g)
            groups.append(g)
    return groups, external


def _is_private(signature: str) -> bool:
    last = [x for x in signature.splitlines() if x.strip() and not x.strip().startswith("@")]
    return bool(last) and re.match(r"\s*private\b", last[-1]) is not None


# --- annotated --------------------------------------------------------------------------------

@dataclass
class Annotated:
    definition: Definition | None   # None: the annotated element is not a definition we know
    location: Location              # of the annotation
    text: str                       # its source line (holds the attribute values)


def annotated(nav: Navigator, annotation: str) -> list[Annotated]:
    """Definitions carrying an annotation (any element: types, methods, fields, parameters).
    The annotated definition is the innermost one whose range includes the annotation and
    whose name comes after it."""
    out: list[Annotated] = []
    for v, occs in nav.scan({annotation}):
        defs = [o for o in v.doc.definitions() if o.enclosing is not None]
        raw = _raw_lines(nav.ws.root, v.source, {o.range.start_line for o in occs})
        lines = {k: t.strip() for k, t in raw.items()}
        for o in occs:
            if o.is_definition or not is_annotation_use(raw.get(o.range.start_line, ""),
                                                        o.range.start_char):
                continue
            best = None
            for dd in defs:
                if not dd.enclosing.contains(o.range):
                    continue
                pos = (dd.range.start_line, dd.range.start_char)
                if S.is_type(dd.symbol):
                    decl = v.declaration_line(dd)
                    if decl is not None and decl != dd.range.start_line:
                        pos = (decl, 0)
                if pos > (o.range.start_line, o.range.start_char):
                    if best is None or dd.enclosing.size() < best.enclosing.size():
                        best = dd
            definition = v._make(best, v.info(best.symbol)) if best is not None else None
            out.append(Annotated(definition, Location(v.source, o.range),
                                 lines.get(o.range.start_line, "")))
    out.sort(key=lambda a: (a.location.source, a.location.range.start_line))
    return out


_ANNOTATION_BEFORE = re.compile(r"@\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)*$")


def is_annotation_use(line: str, col: int) -> bool:
    """Whether the type name at `col` is written as an annotation: `@Name` or `@pkg.Name`, not
    inside a comment (javadoc `{@link Name}` and `{@code @Name}` do not count)."""
    before = line[:col]
    if not _ANNOTATION_BEFORE.search(before):
        return False
    stripped = before.lstrip()
    if stripped.startswith(("*", "/*", "//")) or "//" in before or "/*" in before:
        return False
    return not re.search(r"\{\s*@(code|link|linkplain|literal)\b[^}]*$", before)


def _raw_lines(root: str, source: str, lines) -> dict[int, str]:
    want = set(lines)
    out: dict[int, str] = {}
    if not want:
        return out
    last = max(want)
    try:
        with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
            for i, t in enumerate(fh):
                if i in want:
                    out[i] = t.rstrip("\n")
                if i >= last:
                    break
    except OSError:
        pass
    return out


# --- typed ------------------------------------------------------------------------------------

TYPED_ORDER = ("field", "parameter", "return type", "local variable", "resource", "supertype",
               "method body", "initializer", "qualifier", "import", "other")

# `Map.Entry`: the type only qualifies a nested type's name (not `Map.of(` nor `Integer.MAX`).
_QUALIFIER = re.compile(r"\s*\.\s*([A-Z][\w$]*)(?!\s*\()")


def _is_qualifier(line: str, end_char: int) -> bool:
    m = _QUALIFIER.match(line, end_char)
    return m is not None and not re.fullmatch(r"[A-Z0-9_$]+", m.group(1))


@dataclass
class TypedUse:
    category: str
    owner: str          # the field, method, or type the use belongs to
    location: Location
    detail: str         # signature line (or local variable declaration)
    via: str = ""       # the subtype this is a use of, with typed(subtypes=True)


def typed_order(u: TypedUse):
    return (TYPED_ORDER.index(u.category), u.location.source, u.location.range.start_line,
            u.location.range.start_char)


def typed(nav: Navigator, type_symbol: str) -> list[TypedUse]:
    """Where a type is used, classified: fields, parameters, return types, local variables,
    supertype clauses, method bodies (new, casts, static calls, generics) and imports."""
    out: list[TypedUse] = []
    for v, occs in nav.scan({type_symbol}):
        raw = _raw_lines(nav.ws.root, v.source, {o.range.start_line for o in occs})
        texts = {k: t.strip() for k, t in raw.items()}
        defs = {o.symbol: o for o in v.doc.definitions()}
        for o in occs:
            if o.is_definition:
                continue
            line = texts.get(o.range.start_line, "")
            loc = Location(v.source, o.range)
            inner = v.innermost(o.range)
            if o.range.start_line == o.range.end_line and \
                    _is_qualifier(raw.get(o.range.start_line, ""), o.range.end_char):
                owner = inner[1] if inner is not None and not inner[2] else \
                    (v.container_of(o.range)[0] if inner is not None else "")
                out.append(TypedUse("qualifier", owner, loc, line))
                continue
            if inner is None:
                cat = "import" if line.startswith("import ") else "other"
                out.append(TypedUse(cat, "", loc, line))
                continue
            enc, sym, local, _gen = inner
            info = v.info(sym)
            if local:
                owner, via, _g = v.container_of(o.range)
                kind = info.kind_name if info else ""
                name_occ = defs.get(sym)
                after_name = name_occ is not None and (o.range.start_line, o.range.start_char) > \
                    (name_occ.range.start_line, name_occ.range.start_char)
                if kind in ("method", "constructor", "abstract method") or after_name:
                    cat, detail = "method body", line
                elif not S.is_local(sym):
                    # scip-javac gives try-with-resources variables a global symbol under the
                    # method (…#load().pm.): a local resource, not a field.
                    cat, detail = "resource", (signature_line(info) or line).strip()
                elif kind == "variable":
                    cat, detail = "local variable", (info.signature if info else line).strip()
                else:
                    cat, detail = "parameter", (info.signature if info else line).strip()
                if via:
                    detail += f"  (in anonymous {via})"
                out.append(TypedUse(cat, owner, loc, detail))
                continue
            k = S.kind_of(sym)
            name_occ = defs.get(sym)
            before_name = name_occ is not None and (o.range.start_line, o.range.start_char) < \
                (name_occ.range.start_line, name_occ.range.start_char)
            if k == "term":
                # The declared type comes before the name; after it is the initializer
                # (`Set<String> s = ConcurrentHashMap.newKeySet()` is not a ConcurrentHashMap).
                cat = "field" if before_name or (name_occ is None and
                                                 o.range.start_line == enc.start_line) \
                    else "initializer"
            elif k in ("method", "constructor"):
                cat = "return type" if before_name else "method body"
            elif k == "type":
                cat = "supertype" if not before_name and name_occ is not None and \
                    o.range.start_line - name_occ.range.start_line <= 3 else "initializer"
            else:
                cat = "other"
            detail = line if cat in ("method body", "initializer") else (signature_line(info)
                                                                          or line)
            out.append(TypedUse(cat, sym, loc, detail))
    out.sort(key=typed_order)
    return out


# --- family / overloads -----------------------------------------------------------------------

@dataclass
class FamilyMember:
    definition: Definition | None
    symbol: str
    relation: str                   # "this", "ancestor", "descendant", "sibling"
    uses: list[Reference] = field(default_factory=list)
    declaration: str = ""           # library members (no definition): their declaration
    where: str = ""                 # and location in the sources jar


def family(nav: Navigator, method: str) -> list[FamilyMember]:
    """The override family of a method: what it overrides, what overrides it, and the other
    overrides of its ancestors, each with its uses (a call through any of them may dispatch to
    any other)."""
    if not S.is_method(method):
        raise QueryError(f"{method} is not a method")
    _d, supers = nav.supertypes(method)
    ancestors = [s.symbol for s in supers]
    top = set(ancestors) | {method}
    impl: dict[str, tuple[Definition, set[str]]] = {}
    for sym in top:
        for i in nav.implementations(sym):
            rels = {r.symbol for r in i.definition.info.relationships
                    if r.is_implementation} if i.definition.info else set()
            impl.setdefault(i.definition.symbol, (i.definition, rels))
    members: OrderedDict[str, FamilyMember] = OrderedDict()
    me = nav.definition(method)
    members[method] = FamilyMember(me, method, "this")
    for s in supers:
        members[s.symbol] = FamilyMember(s.definition, s.symbol, "ancestor")
    for sym, (d, rels) in impl.items():
        if sym in members:
            continue
        members[sym] = FamilyMember(d, sym, "descendant" if method in rels else "sibling")
    uses = nav._references_many(set(members))
    for sym, m in members.items():
        m.uses = uses.get(sym, [])
    order = {"this": 0, "ancestor": 1, "descendant": 2, "sibling": 3}
    return sorted(members.values(), key=lambda m: (order[m.relation],
                                                   m.definition.location.source if m.definition
                                                   else "~", m.symbol))


def overloads(nav: Navigator, method: str) -> list[FamilyMember]:
    """All overloads of a method in its type ((), (+1), (+2)…), each with its uses."""
    if not S.is_method(method):
        raise QueryError(f"{method} is not a method")
    d = nav.definition(method)
    owner, name = S.parent(method), S.name_of(method)
    found: list[Definition] = []
    if d is not None:
        for v in nav._views(nav.ws.shards_for_source(d.location.source)[:1]):
            found = v.definitions(lambda sym, _i: not S.is_local_like(sym) and S.is_method(sym)
                                  and S.parent(sym) == owner and S.name_of(sym) == name)
    if not found:
        return []
    uses = nav._references_many({x.symbol for x in found})
    out = [FamilyMember(x, x.symbol, "this" if x.symbol == method else "overload",
                        uses.get(x.symbol, [])) for x in found]
    out.sort(key=lambda m: m.definition.location.range.start_line)
    return out


# --- uses by name -----------------------------------------------------------------------------

def property_names(symbol: str) -> list[str]:
    """Names a member is likely to appear under in strings: its own name and, for accessors
    and fields, the bean property name."""
    name = S.name_of(symbol)
    if S.kind_of(symbol) == "constructor":
        return []
    out = [name]
    m = re.match(r"(get|set|is|has)([A-Z].*)", name)
    if m:
        prop = m.group(2)
        out.append(prop[0].lower() + prop[1:] if not prop[1:2].isupper() else prop)
    return list(OrderedDict.fromkeys(out))


@dataclass
class NameHit:
    source: str
    line: int       # 1-based
    text: str
    name: str


def uses_by_name(root: str, names: list[str], globs: list[str], exclude: set[tuple[str, int]],
                 limit: int = 200) -> list[NameHit]:
    """Lines where one of `names` appears as a whole word in files matching `globs`, outside the
    indexed uses in `exclude`. In .java files only matches inside string literals count (Map
    keys, JPQL/JDOQL, property paths…); other files count any whole-word match."""
    names = [n for n in names if n]
    if not names:
        return []
    globs = globs or ["*.java"]
    hits: list[NameHit] = []
    words = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
    in_string = re.compile(r'"(?:[^"\\]|\\.)*"')
    for source, line, text in _grep_words(root, names, globs):
        if (source, line) in exclude:
            continue
        found = None
        if source.endswith(".java"):
            for lit in in_string.finditer(text):
                m = words.search(lit.group(0))
                if m:
                    found = m.group(1)
                    break
        else:
            m = words.search(text)
            found = m.group(1) if m else None
        if found:
            hits.append(NameHit(source, line, text.strip()[:160], found))
            if len(hits) >= limit:
                break
    return hits


def _grep_words(root: str, names: list[str], globs: list[str]):
    grep = shutil.which("grep")
    if grep:
        args = [grep, "-rnwIF", "--null"]
        for g in globs:
            args.append(f"--include={g}")
        for d in sorted(SKIP_DIRS):
            args.append(f"--exclude-dir={d}")
        for n in names:
            args += ["-e", n]
        args += ["--", "."]
        proc = subprocess.run(args, cwd=root, capture_output=True,
                              env=dict(os.environ, LC_ALL="C"))
        for rec in proc.stdout.split(b"\n"):
            if b"\0" not in rec:
                continue
            path, _, rest = rec.partition(b"\0")
            num, _, text = rest.partition(b":")
            try:
                yield (path.decode(errors="replace").removeprefix("./"), int(num),
                       text.decode("utf-8", errors="replace"))
            except ValueError:
                continue
        return
    word = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for n in files:
            if not any(fnmatch.fnmatch(n, g) for g in globs):
                continue
            path = os.path.join(d, n)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for i, text in enumerate(fh, 1):
                        if word.search(text):
                            yield rel, i, text.rstrip("\n")
            except OSError:
                continue


# --- index status -----------------------------------------------------------------------------

@dataclass
class Status:
    tasks: list[tuple[str, int]]          # (module/task, shards)
    shards: int
    orphans: list[str]
    stale: list[str]
    uncompiled: list[str]                 # newer than the last compilation of their task
    without_shard: list[str] | None       # git-tracked .java with no shard (None: not a git repo)
    refresh: list[str]                    # tasks to recompile
    settings: object = None               # config.Settings, filled by the service


def index_status(nav: Navigator) -> Status:
    ws = nav.ws
    counts: OrderedDict[str, int] = OrderedDict((m, 0) for _r, m in ws.scip_roots())
    sources: set[str] = set()
    orphans = []
    for k, rel in ws.files():
        counts[ws.scip_roots()[k][1]] += 1
        src = rel[:-len(SUFFIX)]
        if ws.source_exists(src):
            sources.add(src)
        else:
            orphans.append(src)
    f = ws.freshness()
    tracked = _git_java_files(ws.root)
    without = sorted(set(tracked) - sources) if tracked is not None else None
    return Status(list(counts.items()), len(ws.files()), sorted(set(orphans)), f.stale,
                  f.uncompiled, without, sorted(f.tasks))


def _git_java_files(root: str) -> list[str] | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run([git, "-C", root, "ls-files", "-z", "--", "*.java"],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p.decode("utf-8", errors="replace") for p in proc.stdout.split(b"\0") if p]


# --- tests ------------------------------------------------------------------------------------

@dataclass
class TestUse:
    test_class: str                 # fully qualified, from the file's package and name
    module: str                     # <module>/<task>
    source: str
    lines: list[int]


def is_test_reference(r: Reference) -> bool:
    """In a test compile task (or, with all shards in one directory, under a test path)."""
    task = r.module.rpartition("/")[2]
    if task in (".", ""):
        return bool(TEST_PATH.search(r.location.source))
    return bool(TEST_TASK.search(task))


def tests_using(nav: Navigator, symbol: str, refs: list[Reference] | None = None
                ) -> list[TestUse]:
    """Test classes (shards of test compile tasks) that use the symbol directly."""
    if refs is None:
        refs, _ = nav.references(symbol)
    by_file: OrderedDict[str, TestUse] = OrderedDict()
    for r in refs:
        if not is_test_reference(r):
            continue
        t = by_file.get(r.location.source)
        if t is None:
            pkg = _package_of_file(nav.ws.root, r.location.source) or (
                S.package_of(r.container) if r.container else "")
            cls = os.path.basename(r.location.source).rsplit(".", 1)[0]
            t = TestUse(f"{pkg}.{cls}" if pkg else cls, r.module, r.location.source, [])
            by_file[r.location.source] = t
        if r.location.line not in t.lines:
            t.lines.append(r.location.line)
    return sorted(by_file.values(), key=lambda t: (t.module, t.test_class))


def _package_of_file(root: str, source: str) -> str:
    try:
        with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                m = _PACKAGE.match(line)
                if m:
                    return m.group(1)
                if i > 200:
                    break
    except OSError:
        pass
    return ""


def test_task(module: str) -> str:
    """Gradle test task for a compile task: compileTestJava -> :m:test,
    compileQaTestJava -> :m:qaTest."""
    path = build_task(module)
    if not path:
        return ""
    if not path.startswith(":"):
        return module
    head, _, task = path.rpartition(":")
    m = re.match(r"compile(\w*?)Java$", task)
    if not m:
        return path
    name = m.group(1)
    name = name[0].lower() + name[1:] if name else "test"
    return f"{head}:{name}"

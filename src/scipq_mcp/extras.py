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
    # The file changed after its compilation: the lines of the last compilation (1-based,
    # first and last), and whether the definition was looked for in the current text:
    # "found" (then `lines` are where it is now), "absent", or "" (it cannot be looked for:
    # a parameter, a local, a member of a local class…, or the text did not parse).
    compiled: tuple[int, int] | None = None
    located: str = ""
    hint: str = ""   # when "absent": a method there now with the same parameters (renamed?)


def source(nav: Navigator, symbol: str, max_lines: int = 200) -> Source | None:
    """The source of a definition, read from the file as it is now. When the file changed
    after its compilation, the definition is looked for in the current text (by its types,
    name and parameter types), since the compiled lines may have moved."""
    d = nav.definition(symbol)
    if d is None:
        return None
    rng = d.enclosing or d.location.range
    first, last = rng.start_line, rng.end_line
    if rng.end_char == 0 and last > first:
        last -= 1  # half-open range ending at column 0 of the next line
    compiled, located, hint = None, "", ""
    if not d.generated and nav.ws.freshness_of({d.location.source}).stale:
        compiled = (first + 1, last + 1)
        owner = S.parent(d.symbol)
        known = {S.name_of(x.symbol) for v in nav._views(nav.ws.shard_for_source(
                 d.location.source)[:1]) for x in v.definitions(
                 lambda sym, _i: S.is_method(sym) and S.parent(sym) == owner)}
        try:
            located, now, hint = _locate_now(nav.ws.root, d, known=known)
        except Exception:   # half-edited code the parser does not expect: not looked for
            located, now = "", None
        if now is not None:
            first, last = now
    shown_last = min(last, first + max(1, max_lines) - 1)
    raw = read_raw_lines(nav.ws.root, d.location.source, first, shown_last)
    lines = [(first + 1 + k, t) for k, t in enumerate(raw)]
    return Source(d, lines, last + 1, shown_last < last, compiled, located, hint)


def _locate_now(root: str, d: Definition, parsed: dict | None = None,
                known: set[str] | None = None) -> tuple[str, tuple[int, int] | None, str]:
    """Where a type, method, constructor, field, enum constant or record component is in its
    file now (0-based first and last lines), found by its enclosing types, name and
    parameter types: ("found", lines, ""), ("absent", None, hint: a method with the same
    parameters that did not exist when it was compiled (`known`: the type's method names
    then), as it may have been renamed), or ("", None, "") when it cannot be looked for.
    `parsed` keeps each file's parse for the next call."""
    from .javasrc import JavaSource
    from .libsrc import _type_variables, class_path_of, param_types
    descs = [x for x in S.parse(d.symbol) if x.suffix != S.NAMESPACE]
    kind = S.kind_of(d.symbol)
    if S.is_local_like(d.symbol) or not descs or kind not in (
            "type", "method", "constructor", "term"):
        return "", None, ""
    # Only members of types nested in types: not of a class declared inside a method.
    owners = descs if kind == "type" else descs[:-1]
    if not owners or any(x.suffix != S.TYPE for x in owners):
        return "", None, ""
    src = parsed.get(d.location.source) if parsed is not None else None
    if src is None:
        try:
            with open(os.path.join(root, d.location.source), encoding="utf-8",
                      errors="replace") as fh:
                src = JavaSource(fh.read())
        except OSError:
            return "absent", None, ""
        if parsed is not None:
            parsed[d.location.source] = src
    _path, types = class_path_of(d.symbol)
    decl = src.find_type(types) if types else None
    if decl is None:
        return "absent", None, ""
    start, body_open, body_close = decl
    if kind == "type":
        return "found", (src.line_of(_annotations_before(src.masked, start)) - 1,
                         src.line_of(body_close) - 1), ""
    header = " ".join(src.masked[start:body_open].split())
    is_enum = " enum " in f" {header} "
    members = src.members(body_open, body_close, is_enum=is_enum, type_name=types[-1])
    name = S.name_of(d.symbol)
    pick = None
    if kind in ("method", "constructor"):
        same = [m for m in members if m.kind == kind and (kind == "constructor" or m.name == name)]
        tvars = _type_variables(header, types[-1])
        want = param_types(S.without_annotations(d.info.signature), tvars) \
            if d.info and d.info.signature else None
        if want is not None:   # the same parameters, also when one overload is left
            same = [m for m in same if param_types(m.declaration, tvars) == want]
        pick = same[0] if len(same) == 1 else None
        if pick is None and len(same) > 1:
            return "", None, ""
        if pick is None and kind == "method" and want is not None and known is not None:
            # Renamed? The one method with these parameters the compilation did not have.
            new = [m for m in members if m.kind == "method" and m.name != name
                   and m.name not in known and param_types(m.declaration, tvars) == want]
            if len(new) == 1:
                m = new[0]
                return "absent", None, f"{m.name}({', '.join(t or 'T' for t in want)}) at " \
                    f"lines {m.start_line}–{m.end_line}"
    else:
        fields = [m for m in members if m.kind == "field" and m.name == name]
        pick = fields[0] if len(fields) == 1 else None
        if pick is None and is_enum:
            pick = next((m for m in src.enum_constants(body_open, body_close)
                         if m.name == name), None)
        if pick is None:
            for _typ, comp, pos in src.record_components(start, body_open, types[-1]):
                if comp == name:
                    line = src.line_of(pos) - 1
                    return "found", (line, line), ""
    if pick is None:
        return "absent", None, ""
    return "found", (pick.start_line - 1, pick.end_line - 1), ""


def _annotations_before(masked: str, pos: int) -> int:
    """The start of the annotations right before a declaration at pos (`@Entity
    @Table(name = "x") public class …`): where scip-javac's range starts too."""
    k = pos
    while True:
        j = k
        while j > 0 and masked[j - 1].isspace():
            j -= 1
        if j > 0 and masked[j - 1] == ")":   # an annotation's arguments
            depth, i = 0, j - 1
            while i >= 0:
                if masked[i] == ")":
                    depth += 1
                elif masked[i] == "(":
                    depth -= 1
                    if depth == 0:
                        break
                i -= 1
            j = i
            while j > 0 and masked[j - 1].isspace():
                j -= 1
        i = j
        while i > 0 and (masked[i - 1].isalnum() or masked[i - 1] in "_$."):
            i -= 1
        if i == j or i == 0 or masked[i - 1] != "@":
            return k
        k = i - 1


def quote_sites(nav: Navigator, sites: dict, stale: set[str] | None = None) -> dict:
    """The text of sites (calls, uses) to quote: {key: (source, compiled 0-based line, the
    patterns its text shows (site_patterns), its container's symbol)} -> {key: (line now,
    stripped text, how)}. In a file changed after its compilation (`stale`: those of the
    answer, computed once) a site moves with its container, found again as `source` finds
    it, and is quoted only if that line shows it: how is "" (where it was), "moved", or
    "unsure" (the text may be other code)."""
    ws = nav.ws
    if not sites:
        return {}
    sources = {v[0] for v in sites.values()}
    if stale is None:
        stale = set(ws.freshness_of(sources).stale)
    out: dict = {}
    plain: dict[str, set[int]] = {}
    for key, (source, k, _p, _c) in sites.items():
        if source not in stale:
            plain.setdefault(source, set()).add(k)
    texts = {source: ws.source_lines(source, ls) for source, ls in plain.items()}
    now_lines: dict[str, list[str]] = {}
    parsed: dict = {}
    shifts: dict[tuple[str, str], tuple[int, ...]] = {}
    for key, (source, k, patterns, container) in sites.items():
        if source not in stale:
            out[key] = (k, texts.get(source, {}).get(k, ""), "")
            continue
        if source not in now_lines:
            try:
                with open(os.path.join(ws.root, source), encoding="utf-8",
                          errors="replace") as fh:
                    now_lines[source] = fh.read().split("\n")
            except OSError:
                now_lines[source] = []
        now = now_lines[source]
        if (source, container) not in shifts:
            shifts[source, container] = _container_shifts(nav, container, source, parsed)
        found = [re.compile(p) for p in patterns]
        hits = {k + s_ for s_ in shifts[source, container] if 0 <= k + s_ < len(now)
                and any(w.search(now[k + s_]) for w in found)}
        if len(hits) == 1:
            j = hits.pop()
            out[key] = (j, now[j].strip(), "" if j == k else "moved")
        else:
            j = k + shifts[source, container][0]
            j = j if 0 <= j < len(now) else min(k, max(len(now) - 1, 0))
            out[key] = (j, now[j].strip() if j < len(now) else "", "unsure")
    return out


def site_patterns(symbol: str, container: str = "") -> list[str]:
    """Regular expressions for what a site of `symbol` shows in the code: its name; for a
    constructor, `new T`, `T::new`, `this(` or `super(` (and an enum constant calling its
    enum's constructor shows the constant)."""
    if S.kind_of(symbol) == "constructor":
        t = re.escape(S.name_of(S.parent(symbol)))
        out = [r"\bnew\s+(?:[\w$]+\s*\.\s*)*" + t + r"(?![\w$])",
               r"(?<![\w$])" + t + r"\s*::\s*new\b",
               r"(?<![\w$.])this\s*\(", r"(?<![\w$.])super\s*\("]
        if container and S.kind_of(container) == "term" and S.parent(container) == \
                S.parent(symbol):
            out.append(r"^\s*" + re.escape(S.name_of(container)) + r"(?![\w$])")
        return out
    return [r"(?<![\w$])" + re.escape(S.name_of(symbol)) + r"(?![\w$])"]


def stale_of(nav: Navigator, sources) -> set[str]:
    """Of these sources, those changed after their compilation (one pass over the shards)."""
    return set(nav.ws.freshness_of(set(sources)).stale)


def _container_shifts(nav: Navigator, container: str, source: str, parsed: dict
                      ) -> tuple[int, ...]:
    """How many lines a definition moved since its compilation: (0,) when it cannot be told,
    one shift when its first and last lines moved alike, both when the edit is inside it."""
    if not container or S.is_local(container):
        return (0,)
    d = nav.definition(container)
    if d is None or d.location.source != source or d.generated:
        return (0,)
    try:
        status, now, _hint = _locate_now(nav.ws.root, d, parsed=parsed)
    except Exception:
        return (0,)
    if status != "found" or now is None:
        return (0,)
    rng = d.enclosing or d.location.range
    last = rng.end_line - 1 if rng.end_char == 0 and rng.end_line > rng.start_line \
        else rng.end_line
    first_shift, last_shift = now[0] - rng.start_line, now[1] - last
    return (first_shift,) if first_shift == last_shift else (first_shift, last_shift)


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


INITIALIZER_CHARS = 200


def field_initializer(declaration: str, name: str, limit: int = INITIALIZER_CHARS) -> str:
    """The initializer of field `name` in the text of its declaration (`500` in
    `static final int MAX = 500;`), "" when it has none; its line breaks joined, clipped to
    `limit` characters."""
    text = S.without_annotations(declaration)
    n = len(text)

    def skip(i: int) -> int:
        """Past a literal or a comment starting at i, else i."""
        if text[i] in "\"'":
            return S._literal_end(text, i)
        if text.startswith("//", i):
            j = text.find("\n", i)
            return n if j < 0 else j
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            return n if j < 0 else j + 2
        return i

    i, start = 0, -1
    while i < n and start < 0:
        j = skip(i)
        if j != i:
            i = j
        elif text[i].isalpha() or text[i] in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            k = j
            while k < n and text[k].isspace():
                k += 1
            if text[i:j] == name and text.startswith("=", k) and not text.startswith("==", k):
                start = k + 1
            i = j
        else:
            i += 1
    if start < 0:
        return ""
    depth = angles = 0
    i, pieces, piece = start, [], start
    while i < n:
        j = skip(i)
        if j != i:
            if text[i] == "/":      # a comment: not part of the value shown
                pieces.append(text[piece:i])
                piece = j
            i = j
            continue
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                break
        elif c == "<" and (text[i - 1].isalnum() or text[i - 1] in "_$.") and \
                not text.startswith(("<<", "<="), i):
            angles += 1             # a type argument list (`HashMap<K, V>`), not `a < b`
        elif c == ">" and angles and text[i - 1] != "-" and not text.startswith(">=", i):
            angles -= 1
        elif depth == 0 and (c == ";" or (c == "," and not angles)):
            break
        i += 1
    pieces.append(text[piece:i])
    value = ""
    for part in (re.sub(r"\s*\n\s*", " ", x).strip() for x in pieces):
        if part:    # the code around a comment left out, joined by a space
            value += (" " if value and part[0] not in ")]},.;" else "") + part
    return value if len(value) <= limit else value[:limit - 1] + "…"


def initializer_of(nav: Navigator, d: Definition, src: Source | None = None) -> str:
    """A field's initializer as it is in the file now ("" for a method or a type, a field
    without one, or one no longer found in a changed file)."""
    if S.kind_of(d.symbol) != "term" or d.generated:
        return ""
    src = src or source(nav, d.symbol, max_lines=50)
    if src is None or not src.lines or src.located == "absent":
        return ""
    lines = [t for _n, t in src.lines]
    rng = d.enclosing
    if rng is not None and src.compiled is None:
        # Unchanged since its compilation: only the declaration's columns of its lines.
        if not src.truncated and src.end_line == rng.end_line + 1 and rng.end_char:
            lines[-1] = lines[-1][:rng.end_char]
        lines[0] = lines[0][rng.start_char:]
    return field_initializer("\n".join(lines), S.name_of(d.symbol))


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
    for v in nav._views(nav.ws.shard_for_source(d.location.source)):
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


_ANNOTATION_BEFORE = re.compile(r"@\s*(?:(?:[^\W\d]|\$)[\w$]*\s*\.\s*)*$")


def is_annotation_use(line: str, col: int) -> bool:
    """Whether the type name at `col` is written as an annotation: `@Name` or `@pkg.Name`, not
    inside a comment (javadoc `{@link Name}` and `{@code @Name}` do not count)."""
    from .javasrc import mask
    before = line[:col]
    if not _ANNOTATION_BEFORE.search(before):
        return False
    if before.lstrip().startswith("*"):
        return False  # a javadoc or block comment continuation line
    # Comments and string contents blanked: `@Url("https://x") @Inject` still counts, and
    # an `@` inside `// …`, `/* … */` or a string does not.
    if not _ANNOTATION_BEFORE.search(mask(before)):
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

TYPED_ORDER = ("field", "parameter", "return type", "throws", "local variable", "resource",
               "supertype", "annotation", "method body", "initializer", "qualifier", "import",
               "other")
_THROWS_BEFORE = re.compile(r"\)\s*throws\b[^(){};=]*$|^\s*throws\b[^(){};=]*$")

# `Map.Entry`: the type only qualifies a nested type's name (not `Map.of(` nor `Integer.MAX`).
_QUALIFIER = re.compile(r"\s*\.\s*([A-Z][\w$]*)(?!\s*\()")


def _mask_line(text: str) -> str:
    from .javasrc import mask
    return mask(text)


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
            raw_line = raw.get(o.range.start_line, "")
            if is_annotation_use(raw_line, o.range.start_char):
                owner = inner[1] if inner is not None and not inner[2] else \
                    (v.container_of(o.range)[0] if inner is not None else "")
                out.append(TypedUse("annotation", owner, loc, line))
                continue
            if _THROWS_BEFORE.search(_mask_line(raw_line[:o.range.start_char])):
                owner = inner[1] if inner is not None and not inner[2] else \
                    (v.container_of(o.range)[0] if inner is not None else "")
                out.append(TypedUse("throws", owner, loc, line))
                continue
            if o.range.start_line == o.range.end_line and \
                    _is_qualifier(raw_line, o.range.end_char):
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


def family(nav: Navigator, method: str, library=None) -> list[FamilyMember]:
    """The override family of a method: what it overrides, what overrides it, and the other
    overrides of its ancestors, each with its uses (a call through any of them may dispatch to
    any other). For a library method, library(method) adds the library's part (Service.
    _library_family): its library supertypes' methods and the overrides in library subtypes
    the project uses."""
    if not S.is_method(method):
        raise QueryError(f"{method} is not a method")
    _d, supers = nav.supertypes(method)
    ancestors = [s.symbol for s in supers]
    # (not for a project type's implicit member: a record accessor, values(), a default
    # constructor)
    extra = library(method) if library is not None and _d is None and \
        not nav._implicit_member(method) else []
    top = set(ancestors) | {method} | {m.symbol for m in extra if m.relation == "ancestor"}
    impl: dict[str, tuple[Definition, set[str]]] = {}
    for sym in top:
        for i in nav.implementations(sym):
            rels = {r.symbol for r in i.definition.info.relationships
                    if r.is_implementation} if i.definition.info else set()
            impl.setdefault(i.definition.symbol, (i.definition, rels))
    members: OrderedDict[str, FamilyMember] = OrderedDict()
    me = nav.definition(method)
    members[method] = next((m for m in extra if m.relation == "this"), None) or \
        FamilyMember(me, method, "this")
    for s in supers:
        members[s.symbol] = FamilyMember(s.definition, s.symbol, "ancestor")
    for m in extra:
        members.setdefault(m.symbol, m)
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
        for v in nav._views(nav.ws.shard_for_source(d.location.source)):
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
    same_content: int = 0                 # newer than their shard, content as compiled
    aux: str = ""                         # the auxiliary index's state, filled by the service


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
                  f.uncompiled, without, sorted(f.tasks), same_content=f.same_content)


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

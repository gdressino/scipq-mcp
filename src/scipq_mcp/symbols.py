"""SCIP symbol strings: parsing, short display names, and matching user-supplied names.

scip-javac writes symbols without the scheme/package prefix, e.g.
``com/example/model/Entity#setState().``; locals are ``local N``. Full global symbols
(``scheme manager name version descriptors``) are accepted too: only the descriptors matter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Descriptor suffixes (scip.proto, Descriptor.Suffix).
NAMESPACE = "/"
TYPE = "#"
TERM = "."
METHOD = "()"
TYPE_PARAMETER = "[]"
PARAMETER = "(param)"
META = ":"
MACRO = "!"

CONSTRUCTOR = "<init>"


@dataclass(frozen=True, slots=True)
class Descriptor:
    name: str
    suffix: str
    disambiguator: str = ""  # for methods: "" or "+1", "+2", ...


def is_local(symbol: str) -> bool:
    return symbol.startswith("local ")


def is_local_like(symbol: str) -> bool:
    """Locals, plus the variables scip-javac gives a global symbol under a method (try-with-
    resources variables: ``…/Dao#load().#pm.``) — not members of any type."""
    if symbol.startswith("local "):
        return True
    # Cheap test (this runs for every definition of every decoded document): a method
    # descriptor "…)." followed by more descriptors.
    i = symbol.find(").", 0, len(symbol) - 2)
    if i < 0:
        return False
    d = parse(symbol)
    return len(d) >= 2 and d[-2].suffix == METHOD and d[-1].suffix in (TERM, TYPE)


def _descriptor_part(symbol: str) -> str:
    # "scheme manager package-name version descriptors"; ' ' inside names is escaped as '  '.
    if " " not in symbol:
        return symbol
    parts, i, n = [], 0, len(symbol)
    while i < n and len(parts) < 4:
        j = i
        while j < n:
            if symbol[j] == " ":
                if j + 1 < n and symbol[j + 1] == " ":
                    j += 2
                    continue
                break
            j += 1
        parts.append(symbol[i:j])
        i = j + 1
    return symbol[i:] if len(parts) == 4 else symbol


def parse(symbol: str) -> list[Descriptor]:
    """Descriptors of a global symbol; [] for locals or unparsable input."""
    if not symbol or is_local(symbol):
        return []
    s = _descriptor_part(symbol)
    out: list[Descriptor] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "(":  # parameter: (name)
            j = s.find(")", i)
            if j < 0:
                return out
            out.append(Descriptor(s[i + 1:j], PARAMETER))
            i = j + 1
            continue
        if s[i] == "[":  # type parameter: [name]
            j = s.find("]", i)
            if j < 0:
                return out
            out.append(Descriptor(s[i + 1:j], TYPE_PARAMETER))
            i = j + 1
            continue
        if s[i] == "`":
            j = i + 1
            name_chars = []
            while j < n:
                if s[j] == "`":
                    if j + 1 < n and s[j + 1] == "`":
                        name_chars.append("`")
                        j += 2
                        continue
                    break
                name_chars.append(s[j])
                j += 1
            name, i = "".join(name_chars), j + 1
        else:
            j = i
            while j < n and (s[j].isalnum() or s[j] in "_$+-"):
                j += 1
            name, i = s[i:j], j
        if i >= n:
            out.append(Descriptor(name, TERM))  # malformed, best effort
            break
        c = s[i]
        if c == "/":
            out.append(Descriptor(name, NAMESPACE))
            i += 1
        elif c == "#":
            out.append(Descriptor(name, TYPE))
            i += 1
        elif c == ".":
            out.append(Descriptor(name, TERM))
            i += 1
        elif c == ":":
            out.append(Descriptor(name, META))
            i += 1
        elif c == "!":
            out.append(Descriptor(name, MACRO))
            i += 1
        elif c == "(":
            j = s.find(")", i)
            if j < 0:
                return out
            out.append(Descriptor(name, METHOD, s[i + 1:j]))
            i = j + 1
            if i < n and s[i] == ".":
                i += 1
        else:
            return out
    return out


def kind_of(symbol: str) -> str:
    """'package', 'type', 'method', 'constructor', 'term' (field/enum constant), 'local' or ''."""
    if is_local(symbol):
        return "local"
    d = parse(symbol)
    if not d:
        return ""
    last = d[-1]
    if last.suffix == METHOD:
        return "constructor" if last.name == CONSTRUCTOR else "method"
    return {NAMESPACE: "package", TYPE: "type", TERM: "term"}.get(last.suffix, "")


def is_method(symbol: str) -> bool:
    return kind_of(symbol) in ("method", "constructor")


def is_type(symbol: str) -> bool:
    return kind_of(symbol) == "type"


def short(symbol: str) -> str:
    """Readable name without the package: ``Entity#setState()``, ``Account.Builder``,
    ``Account#<init>(+1)``. Locals are returned as is."""
    d = parse(symbol)
    if not d:
        return symbol
    parts = [x for x in d if x.suffix not in (NAMESPACE, PARAMETER, TYPE_PARAMETER)]
    if not parts:
        return ".".join(x.name for x in d if x.suffix == NAMESPACE) or symbol
    out = []
    for k, x in enumerate(parts):
        sep = "" if k == 0 else ("." if parts[k - 1].suffix == TYPE and x.suffix == TYPE else "#")
        if x.suffix == METHOD:
            out.append(f"{sep}{x.name}({x.disambiguator})")
        else:
            out.append(f"{sep}{x.name}")
    return "".join(out) or symbol


def _format_name(name: str) -> str:
    if name and all(c.isalnum() or c in "_$+-" for c in name):
        return name
    return "`" + name.replace("`", "``") + "`"


def format_descriptors(descs: list[Descriptor]) -> str:
    """Inverse of parse() for the descriptor part of a symbol."""
    out = []
    for d in descs:
        if d.suffix == PARAMETER:
            out.append(f"({_format_name(d.name)})")
        elif d.suffix == TYPE_PARAMETER:
            out.append(f"[{_format_name(d.name)}]")
        elif d.suffix == METHOD:
            out.append(f"{_format_name(d.name)}({d.disambiguator}).")
        else:
            out.append(_format_name(d.name) + d.suffix)
    return "".join(out)


def parent(symbol: str) -> str:
    """The symbol that owns this one: ``…/Account#`` for ``…/Account#number.``,
    ``…/Account#`` for ``…/Account#Builder#``; "" at the top."""
    d = parse(symbol)
    return format_descriptors(d[:-1]) if len(d) > 1 else ""


def name_of(symbol: str) -> str:
    d = parse(symbol)
    return d[-1].name if d else symbol


_SUPER_BEFORE = re.compile(r"\bsuper\s*\.\s*$")


def is_super_call(text: str, col: int, name: str) -> bool:
    """Whether the occurrence of a method `name` at column `col` of a source line is
    `super.name(…)` (or `Iface.super.name(…)`): bound to the caller's superclass at compile
    time, so it never dispatches to an override. When the column does not point at the name
    (tabs, astral characters), every call of that name on the line must be a super call."""
    if col >= 0 and text[col:col + len(name)] == name:
        return bool(_SUPER_BEFORE.search(text[:col]))
    calls = re.findall(r"(\bsuper\s*\.\s*)?(?<![\w$])" + re.escape(name) + r"\s*\(", text)
    return bool(calls) and all(calls)


def package_of(symbol: str) -> str:
    return ".".join(x.name for x in parse(symbol) if x.suffix == NAMESPACE)


def owner_type(symbol: str) -> str:
    """Symbol of the innermost type containing a member (or the type itself)."""
    s = _descriptor_part(symbol)
    i = s.rfind("#")
    return s[:i + 1] if i >= 0 else ""


def top_level_type_name(symbol: str) -> str:
    for x in parse(symbol):
        if x.suffix == TYPE:
            return x.name
    return ""


# --- user queries ---------------------------------------------------------------------------

_QUERY_TOKEN = re.compile(r"([^#./()\s]+)(\((\+\d+)?\))?")


@dataclass(frozen=True, slots=True)
class Query:
    """A name typed by a user or an agent: ``Entity``, ``Entity#setState``,
    ``com.example.model.Entity.setState()``, ``setState(+1)``, ``Account.Builder``,
    ``Account#<init>``. Matching is on the trailing descriptor names; package separators
    may be '/', '.' or '#'. A trailing ``()`` restricts to methods (any overload), ``(+N)`` to
    one overload."""

    raw: str
    names: tuple[str, ...]
    method_only: bool
    disambiguator: str | None  # None = any overload


def looks_like_symbol(text: str) -> bool:
    """A complete SCIP symbol (as printed by the tools), to be used verbatim."""
    t = text.strip()
    if is_local(t):
        return True
    return "/" in t and (t.endswith("#") or t.endswith(".") or t.endswith("/"))


def parse_query(text: str) -> Query:
    t = text.strip().replace("`", "")
    names, method_only, disamb = [], False, None
    pos = 0
    for m in _QUERY_TOKEN.finditer(t):
        if t[pos:m.start()].strip(" #./"):
            break
        names.append(m.group(1))
        pos = m.end()
        if m.group(2):
            method_only = True
            disamb = m.group(3)  # None for "()": any overload
        else:
            method_only, disamb = False, None
    # Only the parentheses of the last token count.
    return Query(text, tuple(names), method_only, disamb)


def matches(symbol: str, q: Query) -> bool:
    if not q.names or is_local(symbol):
        return False
    d = [x for x in parse(symbol) if x.suffix not in (PARAMETER, TYPE_PARAMETER)]
    if len(d) < len(q.names):
        return False
    tail = d[-len(q.names):]
    for x, name in zip(tail, q.names):
        if x.name != name:
            return False
    last = tail[-1]
    if q.method_only:
        if last.suffix != METHOD:
            return False
        if q.disambiguator is not None and last.disambiguator != q.disambiguator:
            return False
    if last.suffix == NAMESPACE and len(q.names) == 1:
        return False  # a bare name never means a package
    return True


def _escape(text: str) -> bytes:
    from .workspace import regex_escape

    return regex_escape(text)


def definition_regex(q: Query) -> bytes:
    """Regex (ERE/PCRE/re subset) matching a shard that *defines* a symbol matching `q`.

    The trailing byte is the tag that follows SymbolInformation.symbol (see workspace.py), so
    shards that only *mention* the name are skipped. It is a prefilter: candidates are decoded."""
    parts = [_escape("`<init>`" if n == CONSTRUCTOR else n) for n in q.names]
    body = b"[/#]".join(parts)
    # A query that starts at a package root starts at the beginning of the symbol string, which
    # is preceded by its length (any byte, digits and letters included): no boundary check.
    start = b"" if len(q.names) > 1 and q.names[0][:1].islower() else rb"(^|[^A-Za-z0-9_$])"
    if q.method_only:
        if q.disambiguator:
            end = _escape(f"({q.disambiguator}).")
        else:
            end = rb"\((\+[0-9]+)?\)\."
    else:
        end = rb"(#|\.|\((\+[0-9]+)?\)\.)"
    return start + body + end + b"[\x28\x32\x1a\x22]"


def occurrence_regex(q: Query) -> bytes:
    """Like definition_regex, for occurrences (the tag after Occurrence.symbol)."""
    return definition_regex(q)[:-len(b"[\x28\x32\x1a\x22]")] + b"[\x42\x18\x4a]"


MATCH_MODES = ("exact", "prefix", "contains", "camel")
_IDENT = rb"[A-Za-z0-9_$\x80-\xff]*"


def fuzzy_definition_regex(q: Query, mode: str) -> bytes:
    """Prefilter for definitions whose LAST name matches q's last name by prefix, substring or
    camel humps (case-insensitive: use with ignore_case); earlier names must match exactly."""
    last = q.names[-1]
    if mode == "camel":
        humps = _humps(last)
        pat = rb"[A-Za-z0-9_$\x80-\xff]*".join(_escape(h) for h in humps)
        core = pat + _IDENT
    elif mode == "prefix":
        core = _escape(last) + _IDENT
    else:
        core = _IDENT + _escape(last) + _IDENT
    head = b"[/#]".join(_escape(n) for n in q.names[:-1])
    lead = (head + b"[/#]") if head else b"[/#]"
    return lead + core + rb"(#|\.|\((\+[0-9]+)?\)\.)[\x28\x32\x1a\x22]"


def _humps(name: str) -> list[str]:
    """`UsoBlAct` -> ['Uso', 'Bl', 'Act']; `uba` -> ['u', 'b', 'a'] (every letter a hump)."""
    parts = re.findall(r"[A-Z][a-z0-9_$]*|[a-z0-9_$]+", name)
    if len(parts) == 1 and parts[0].islower():
        return list(parts[0])
    return parts or [name]


def fuzzy_matches(symbol: str, q: Query, mode: str) -> bool:
    if mode == "exact":
        return matches(symbol, q)
    if not q.names or is_local_like(symbol):
        return False
    d = [x for x in parse(symbol) if x.suffix not in (PARAMETER, TYPE_PARAMETER)]
    if len(d) < len(q.names) or d[-1].suffix == NAMESPACE:
        return False
    tail = d[-len(q.names):]
    if any(x.name.lower() != n.lower() for x, n in zip(tail[:-1], q.names[:-1])):
        return False
    name, want = tail[-1].name, q.names[-1]
    if q.method_only and tail[-1].suffix != METHOD:
        return False
    if mode == "prefix":
        return name.lower().startswith(want.lower())
    if mode == "contains":
        return want.lower() in name.lower()
    # camel: every hump starts a word of the name, in order.
    words = [w.lower() for w in re.findall(r"[A-Z]?[a-z0-9_$]+|[A-Z]+(?![a-z])", name)]
    k = 0
    for h in (x.lower() for x in _humps(want)):
        while k < len(words) and not words[k].startswith(h):
            k += 1
        if k == len(words):
            return False
        k += 1
    return True


def definition_regex_exact(symbol: str) -> bytes:
    return _escape(symbol) + b"[\x28\x32\x1a\x22]"


def reference_search(symbol: str) -> tuple[str, bytes]:
    """('fixed' | 'regex', pattern) for shards with occurrences of `symbol`.

    A type symbol (``…/Foo#``) is a prefix of every member symbol (``…/Foo#bar().``), so it
    must not be followed by an identifier character. 'B' and 'J' stay allowed: they are the
    range tags (0x42, 0x4a) that follow the symbol of an Occurrence."""
    if symbol.endswith("#"):
        return "regex", _escape(symbol) + rb"([^A-Za-z0-9_$`]|[BJ])"
    return "fixed", symbol.encode()


def implementation_needles(symbols) -> list[bytes]:
    """Fixed needles for Relationship{symbol, [is_reference,] is_implementation}."""
    from .workspace import IMPLEMENTATION_TAGS

    return [s.encode() + t for s in symbols for t in IMPLEMENTATION_TAGS]

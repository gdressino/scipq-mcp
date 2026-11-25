"""Navigation queries over a Workspace: find, references, definition, implementations,
supertypes, outline, callers and callees.

Every query prefilters shards by content and decodes only the candidates; nothing is cached.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import symbols as S
from .scip import Document, Occurrence, Range, SymbolInfo, read_documents
from .workspace import (
    DEFINITION_TAGS,
    OCCURRENCE_TAGS,
    Shard,
    Workspace,
)

GENERATED_MARKERS = ("@Generated", "@lombok.Generated", "@javax.annotation.Generated",
                     "@javax.annotation.processing.Generated")


_DECL_KEYWORD = re.compile(r"\b(class|interface|enum|record|@interface)\b")


def _is_method_symbol(symbol: str) -> bool:
    d = S.parse(symbol)
    return bool(d) and d[-1].suffix == S.METHOD


def _declared_params(d) -> tuple | None:
    """Parameter types of a project method or constructor, from its signature."""
    from .libsrc import param_types
    if d.info is None or not d.info.signature or not _is_method_symbol(d.symbol):
        return None
    return param_types(S.without_annotations(d.info.signature))


def by_params(items: list, params: tuple, declared, qualified=None) -> list:
    """The overloads a written parameter list means, per method (owner and name): those
    taking exactly those types; for `()` with no overload without parameters, every overload
    (`m()` also reads as "the method m"). `items` are Definitions or symbols; declared(item)
    gives its parameter types (None: unknown), qualified(item) the same as SCIP symbols, for
    the types written with their package."""
    if qualified is None or not any(p and S.is_qualified_type(p) for p in params):
        qualified = lambda _it: None   # noqa: E731
    groups: dict[tuple[str, str], list] = {}
    for it in items:
        sym = it if isinstance(it, str) else it.symbol
        groups.setdefault((S.parent(sym), S.name_of(sym)), []).append(it)
    if not params and len(groups) == 1 and len(items) == 1:
        return list(items)
    per = []
    for group in groups.values():
        types = [declared(it) for it in group]
        quals = [qualified(it) if t is not None else None for it, t in zip(group, types)]
        hits = [(it, t) for it, t, q in zip(group, types, quals) if S.same_params(params, t, q)]
        # Exact types win over a type variable (`m(String)`: m(String s), not <T> m(T x)).
        exact = [it for it, t in hits if None not in t and None not in params]
        if not exact and None in params:
            # `m(T)` as answers write <T> m(T x): the overload with a type variable there.
            exact = [it for it, t in hits if all((w is None) == (d is None)
                                                 for w, d in zip(params, t))]
        unknown = [it for it, t in zip(group, types) if t is None]
        per.append((group, exact or [it for it, _t in hits], unknown))
    if not params and not any(hits for _g, hits, _u in per):
        return list(items)   # `m()`, none without parameters: the method, whatever it takes
    # Otherwise the overloads that fit, in every type that has one (`m()` across types: those
    # without parameters); where it cannot be told, those left for the caller to choose.
    return [x for _g, hits, unknown in per for x in (hits or unknown)]


def _param_list(params: tuple, written: str = "") -> str:
    return "(" + (written or ", ".join(p or "T" for p in params)) + ")"


def _read_header(path: str, line: int, max_lines: int = 30) -> str:
    """From `line` (0-based) up to the `{` that opens the body, outside parentheses (a record
    header may span lines: `record Range(\n int lo,\n int hi) {`)."""
    from .javasrc import mask, strip_comments
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, text in enumerate(fh):
                if i < line:
                    continue
                out.append(text.rstrip("\n"))
                masked = mask("\n".join(out))
                depth = 0
                for k, ch in enumerate(masked):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                    elif ch in "{;" and depth <= 0:
                        head = strip_comments("\n".join(out))[:k]
                        return " ".join(x.strip() for x in head.splitlines() if x.strip())
                if i - line >= max_lines:
                    break
    except OSError:
        pass
    return " ".join(x.strip() for x in out if x.strip())


def _read_line(path: str, line: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, text in enumerate(fh):
                if i == line:
                    return text
    except OSError:
        pass
    return ""


class QueryError(Exception):
    pass


@dataclass(slots=True)
class Location:
    source: str
    range: Range

    @property
    def line(self) -> int:  # 1-based, for display
        return self.range.start_line + 1


@dataclass(slots=True)
class Definition:
    symbol: str
    location: Location
    info: SymbolInfo | None
    enclosing: Range | None = None
    container: str = ""      # for locals (anonymous classes): nearest non-local definition
    generated: bool = False
    # Generated members (Lombok) are located at the node that generates them: the field, or
    # a class-level annotation such as @Setter. `location` points there.
    generated_by: Range | None = None
    kind_override: str = ""

    @property
    def kind(self) -> str:
        if self.kind_override:
            return self.kind_override
        if self.info and self.info.kind_name:
            return self.info.kind_name
        return S.kind_of(self.symbol)


@dataclass(slots=True)
class Reference:
    symbol: str
    location: Location
    roles: int
    container: str = ""      # enclosing non-local definition (the "caller")
    via_local: str = ""      # display name of a local definition in between (anonymous class method)
    module: str = ""         # <module>/<task> of the shard (e.g. "app/compileTestJava")


@dataclass(slots=True)
class Resolution:
    symbol: str | None
    candidates: list[Definition] = field(default_factory=list)
    note: str = ""
    externals: list[str] = field(default_factory=list)  # matching symbols not defined here


@dataclass(slots=True)
class Implementation:
    definition: Definition
    direct: bool
    via: list[str] = field(default_factory=list)  # results this one reaches the target through


@dataclass(slots=True)
class Supertype:
    symbol: str
    direct: bool
    definition: Definition | None  # None when it is not defined in this project (e.g. the JDK)


@dataclass(slots=True)
class CallNode:
    symbol: str
    sites: list[Reference] = field(default_factory=list)  # where this node calls its parent
    children: list["CallNode"] = field(default_factory=list)
    definition: Definition | None = None
    via: str = ""             # the overridden method actually called, when it is not the target
    truncated: bool = False
    # Root only, without include_overridden: the calls it leaves out (method it overrides ->
    # its call sites) and the callers in the tree that override project methods (not followed).
    unfollowed: dict[str, list["Reference"]] = field(default_factory=dict)
    overriders: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Callee:
    symbol: str
    sites: list[Location]


def signature_line(info: SymbolInfo | None) -> str:
    """Last line of the signature: the declaration without its annotations."""
    if not info or not info.signature:
        return ""
    lines = [x.strip() for x in info.signature.splitlines() if x.strip()]
    for x in reversed(lines):
        if not x.startswith("@"):
            return x
    return lines[-1] if lines else ""


def declared_supertypes(d: Definition) -> set[str]:
    """Simple names after `extends` / `implements` (and `permits` excluded) in a type's
    declaration: its direct supertypes."""
    sig = signature_line(d.info) if d.info else ""
    names: set[str] = set()
    for kw in ("extends", "implements"):
        m = re.search(r"\b" + kw + r"\b(.*?)(?=\b(?:extends|implements|permits)\b|\{|$)", sig)
        if not m:
            continue
        depth, cur = 0, ""
        for ch in m.group(1) + ",":
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif ch == "," and depth == 0:
                name = cur.strip().split("(")[0].strip()
                if name:
                    names.add(name.split(".")[-1].strip())
                cur = ""
                continue
            if depth == 0 and ch not in "<>":
                cur += ch
    return names



class DocView:
    """One document plus the lookups the queries need, decoding as little as possible."""

    def __init__(self, shard: Shard, doc: Document, root: str = ""):
        self.shard = shard
        self.doc = doc
        self.source = shard.source
        self.root = root
        self._infos: dict[str, SymbolInfo] | None = None
        self._containers: list[tuple[Range, str, bool, bool]] | None = None

    @property
    def infos(self) -> dict[str, SymbolInfo]:
        """All SymbolInformation of the document (full decode)."""
        if self._infos is None:
            self._infos = {s.symbol: s for s in self.doc.symbols}
        return self._infos

    def info(self, symbol: str) -> SymbolInfo | None:
        if self._infos is not None:
            return self._infos.get(symbol)
        found = self.doc.symbol_infos_of([symbol])
        return found[0] if found else None

    @staticmethod
    def structurally_generated(occ: Occurrence) -> bool:
        # Lombok members are reported at their field, with a range that is not inside their own
        # enclosing range.
        return occ.enclosing is not None and not occ.enclosing.contains(occ.range)

    def is_generated(self, occ: Occurrence, info: SymbolInfo | None = None) -> bool:
        if self.structurally_generated(occ):
            return True
        info = info or self.info(occ.symbol)
        return bool(info) and any(m in info.signature for m in GENERATED_MARKERS)

    def containers(self) -> list[tuple[Range, str, bool, bool]]:
        """[(enclosing range, symbol, is_local, is_generated)] of definitions, innermost first."""
        if self._containers is None:
            out = []
            for o in self.doc.definitions():
                # A type parameter's range is its method's: the method contains the code.
                if o.enclosing is not None and not S.is_parameter(o.symbol):
                    out.append((o.enclosing, o.symbol, S.is_local_like(o.symbol),
                                self.structurally_generated(o)))
            out.sort(key=lambda t: t[0].size())
            self._containers = out
        return self._containers

    def container_of(self, rng: Range) -> tuple[str, str, bool]:
        """(innermost non-local definition containing rng, innermost local method in between
        or "", whether the innermost container is generated code)."""
        via = ""
        generated = None
        for enc, sym, local, gen in self.containers():
            if not enc.contains(rng):
                continue
            if generated is None:
                generated = gen
            if local:
                if not via:
                    info = self.info(sym)
                    if info and info.kind_name in ("method", "constructor", "abstract method"):
                        via = info.display_name or sym
                continue
            return sym, via, bool(generated)
        return "", via, bool(generated)

    def innermost(self, rng: Range) -> tuple[Range, str, bool, bool] | None:
        """Innermost definition (locals included) whose enclosing range contains rng."""
        for c in self.containers():
            if c[0].contains(rng):
                return c
        return None

    def _make(self, o: Occurrence, info: SymbolInfo | None) -> Definition:
        if self.structurally_generated(o):
            # scip-javac puts the definition of a generated member at the first textual
            # occurrence of its name after the generating node (possibly a call elsewhere in
            # the file); the enclosing range is the node itself.
            d = Definition(o.symbol, Location(self.source, o.enclosing), info, None,
                           generated=True, generated_by=o.enclosing)
            return d
        d = Definition(o.symbol, Location(self.source, o.range), info, o.enclosing,
                       generated=self.is_generated(o, info))
        if S.is_local(o.symbol):
            d.container = self.container_of(o.range)[0]
        if S.is_type(o.symbol) and o.enclosing is not None:
            decl = self.declaration_line(o)
            if decl is not None and decl != o.range.start_line:
                d.location = Location(self.source, Range(decl, 0, decl, 0))
        if info is not None and S.is_type(o.symbol) and not _DECL_KEYWORD.search(info.signature):
            self._fix_type_declaration(d)
        return d

    def declaration_line(self, o: Occurrence) -> int | None:
        """Where a type is really declared (scip-javac may report the first textual occurrence
        of its name, e.g. inside an annotation string above the declaration)."""
        if not self.root or o.enclosing is None:
            return None
        from .index import declaration_line

        try:
            with open(os.path.join(self.root, self.source), encoding="utf-8",
                      errors="replace") as fh:
                lines = fh.read().split("\n")
        except OSError:
            return None
        return declaration_line(lines, o.enclosing.start_line, S.name_of(o.symbol))

    def _fix_type_declaration(self, d: Definition) -> None:
        """scip-javac writes records as `public static final Name`, without the keyword or the
        components: take the declaration from the source line instead."""
        if not self.root:
            return
        line = _read_header(os.path.join(self.root, self.source), d.location.range.start_line)
        m = re.search(r"\b(record|class|interface|enum)\s+" + re.escape(S.name_of(d.symbol))
                      + r"\b", line)
        if not m:
            return
        decl = line.strip().rstrip("{").strip()
        d.kind_override = m.group(1)
        d.info.signature = decl

    def definitions_of(self, symbols: Iterable[str],
                       infos: dict[str, SymbolInfo] | None = None) -> list[Definition]:
        """Definitions in this document of exactly these symbols (selective decode)."""
        want = set(symbols)
        if not want:
            return []
        if infos is None:
            infos = {x.symbol: x for x in self.doc.symbol_infos_of(want)}
        out, seen = [], set()
        for o in self.doc.occurrences_of(want):
            if o.is_definition and o.symbol not in seen:
                seen.add(o.symbol)
                out.append(self._make(o, infos.get(o.symbol)))
        return out

    def definitions(self, wanted) -> list[Definition]:
        """Definitions of the symbols for which wanted(symbol, info) is true (decodes every
        SymbolInformation of the document)."""
        infos = self.infos
        out, seen = [], set()
        for o in self.doc.definitions():
            if o.symbol in seen:
                continue
            info = infos.get(o.symbol)
            if not wanted(o.symbol, info):
                continue
            seen.add(o.symbol)
            out.append(self._make(o, info))
        return out


class Navigator:
    def __init__(self, workspace: Workspace):
        self.ws = workspace
        self.unreadable: set[str] = set()
        # Every overload of a library method with its parameter types (from its sources jar),
        # set by the service: picks among them when a name is written with a parameter list.
        self.library_overloads: Callable[..., list[tuple]] = lambda _sym, _q=False: []

    # --- plumbing ---------------------------------------------------------------------------

    def _views(self, shards: Iterable[Shard]) -> Iterable[DocView]:
        for sh in shards:
            try:
                docs = read_documents(sh.path)
            except (OSError, ValueError, IndexError):
                # Typically a shard being rewritten by a compilation right now.
                self.unreadable.add(sh.source)
                continue
            for doc in docs:
                yield DocView(sh, doc, self.ws.root)

    def _definitions_of(self, wanted_symbols: set[str]) -> dict[str, Definition]:
        """Definitions of exact global symbols, looking first at the file named after their
        top-level type and then, for the rest, grepping."""
        found: dict[str, Definition] = {}
        pending = {s for s in wanted_symbols if s and not S.is_local(s)}
        if not pending:
            return found
        names = {S.top_level_type_name(s) + ".java" for s in pending if S.top_level_type_name(s)}
        if names:
            for v in self._views(self.ws.shards_by_basename(names)):
                for d in v.definitions_of(pending):
                    found.setdefault(d.symbol, d)
            pending -= found.keys()
        if pending:
            needles = [p.encode() + t for p in pending for t in DEFINITION_TAGS]
            for v in self._views(self.ws.grep(needles)):
                for d in v.definitions_of(pending):
                    found.setdefault(d.symbol, d)
        return found

    # --- find / resolve ---------------------------------------------------------------------

    def find_fuzzy(self, text: str, mode: str) -> list[Definition]:
        """find() with the last name matched by prefix, substring or camel humps, ignoring
        case."""
        q = S.parse_query(text.strip())
        if not q.names:
            return []
        regex = S.fuzzy_definition_regex(q, mode)
        compiled = re.compile(regex, re.IGNORECASE)
        out = []
        for v in self._views(self.ws.grep_regex(regex, ignore_case=True)):
            infos = {i.symbol: i for i in v.doc.symbol_infos_matching(compiled)
                     if S.fuzzy_matches(i.symbol, q, mode)}
            out.extend(v.definitions_of(infos, infos))
        out.sort(key=lambda d: (S.name_of(d.symbol).lower() != q.names[-1].lower(),
                                d.location.source, d.location.range.start_line))
        return out

    def find(self, text: str) -> list[Definition]:
        """Definitions matching a name. A parameter list picks overloads as Java reads it:
        `m(String, int)` those taking these types, `m()` the one without parameters (or every
        overload when none is without parameters)."""
        q = S.parse_query(text.strip())
        out = self._find(text)
        if q.params is not None and not S.looks_like_symbol(text.strip()):
            out = by_params(out, q.params, _declared_params, self._qualified_params)
        return out

    def _qualified_params(self, d: Definition) -> tuple | None:
        """A project method's parameter types as SCIP symbols: each name as the declaring file
        means it (its type occurrences of that name, when they are one type)."""
        from .libsrc import JAVA_LANG_TYPES, param_types
        if d.info is None or not d.info.signature:
            return None
        names = param_types(S.without_annotations(d.info.signature), written=True)
        if names is None:
            return None
        by_name: dict[str, set] = {}
        for v in self._views(self.ws.shard_for_source(d.location.source)[:1]):
            for o in v.doc.occurrences:
                sym = S.parent(o.symbol) if S.kind_of(o.symbol) == "constructor" else o.symbol
                if S.kind_of(sym) == "type":
                    by_name.setdefault(S.name_of(sym), set()).add(sym)
        out = []
        for n in names:
            if n is None or n in S.PRIMITIVES:
                out.append(None)
            elif S.is_qualified_type(n):
                out.append(S.type_symbol(n))
            else:
                found = by_name.get(n.rsplit(".", 1)[-1], set())
                out.append(next(iter(found)) if len(found) == 1 else
                           (f"java/lang/{n}#" if not found and n in JAVA_LANG_TYPES else None))
        return tuple(out)

    def _find(self, text: str) -> list[Definition]:
        text = text.strip()
        if not text:
            return []
        if S.looks_like_symbol(text):
            d = self._definitions_of({text}).get(text)
            return [d] if d else []
        q = S.parse_query(text)
        if not q.names:
            return []
        out = []
        regex = S.definition_regex(q)
        compiled = re.compile(regex)
        for v in self._views(self.ws.grep_regex(regex)):
            infos = {i.symbol: i for i in v.doc.symbol_infos_matching(compiled)
                     if S.matches(i.symbol, q)}
            out.extend(v.definitions_of(infos, infos))
        if not out and len(q.names) == 1:
            # Types in the default package: the symbol has no '/' or '#' before the name.
            for v in self._views(self.ws.grep([q.names[0].encode()])):
                out.extend(v.definitions(lambda sym, _i: S.matches(sym, q)))
        out.sort(key=lambda d: (d.location.source, d.location.range.start_line, d.symbol))
        return out

    def guess(self, text: str) -> str | None:
        """Fast, possibly incomplete resolution: look only in the file named after the first
        capitalized name (Java keeps a top-level type in <Type>.java). The caller must confirm
        it with resolve(); see Service._resolved."""
        q = S.parse_query(text)
        head = next((n for n in q.names if n[:1].isupper()), None)
        if not head or q.params:
            return None
        found = set()
        for v in self._views(self.ws.shards_by_basename({head + ".java"})):
            for info in v.doc.symbols:
                if not S.is_local_like(info.symbol) and S.matches(info.symbol, q):
                    found.add(info.symbol)
        return found.pop() if len(found) == 1 else None

    def resolve(self, text: str) -> Resolution:
        text = text.strip()
        if S.looks_like_symbol(text):
            return Resolution(text)
        q = S.parse_query(text)
        every = [d for d in self._find(text) if not S.is_local_like(d.symbol)]
        found = by_params(every, q.params, _declared_params, self._qualified_params) \
            if q.params is not None else every
        if every and not found:
            return Resolution(None, every, f"no overload of {text.split('(')[0]!r} takes "
                                           f"{_param_list(q.params, q.params_text)}; its overloads:")
        syms = sorted({d.symbol for d in found})
        # (Constructors are not inherited: `X#<init>()` is one of X's, never a supertype's.)
        none_without = ""
        if q.params == () and syms and len({(S.parent(x), S.name_of(x)) for x in syms}) == 1:
            declared = [_declared_params(d) for d in found]
            if all(t for t in declared):   # every one takes parameters
                sigs = "; ".join(" ".join(S.without_annotations(d.info.signature).split())
                                 for d in found if d.info)
                inh = self._inherited_parameterless(S.parent(syms[0]), S.name_of(syms[0])) \
                    if S.name_of(syms[0]) != S.CONSTRUCTOR else None
                if inh:
                    return Resolution(inh, found, f"{text} → {inh} (the overload without "
                                                  f"parameters, inherited: "
                                                  f"{S.short(S.parent(syms[0]))} declares only "
                                                  f"{sigs})")
                if len(syms) > 1:
                    return Resolution(None, found, f"no overload of {text.split('(')[0]!r} is "
                                                   "without parameters; its overloads:")
                what = "constructor" if S.name_of(syms[0]) == S.CONSTRUCTOR else "method"
                none_without = f" (none without parameters: the {what}, which takes " \
                               f"{_param_list(declared[0])})"
        # A bare type name may also be a JDK or library type used everywhere (e.g. `Button`).
        # Only when it changes the answer: no project match, or already ambiguous.
        bare_type = len(q.names) == 1 and q.names[0][:1].isupper() and not q.method_only
        external = self.find_external(text, {d.symbol for d in every}) \
            if (not syms or (bare_type and len(syms) > 1)) else []
        external = [e for e in external if not self._implicit_member(e)] if syms else external
        picked = False
        if external and q.params is not None:
            narrowed, picked, known = self._library_by_params(external, q.params)
            if not narrowed:
                return Resolution(None, [], f"no overload of {text.split('(')[0]!r} takes "
                                            f"{_param_list(q.params, q.params_text)}; its "
                                            "overloads:", known or external)
            external = narrowed
        if len(syms) == 1 and not external:
            return Resolution(syms[0], found, f"{text} → {syms[0]}"
                              + (none_without or self._overload_note(q, syms[0], every)))
        if syms:
            n = len(syms) + len(external)
            return Resolution(None, found, f"{text!r} matches {n} symbols; pass one of them",
                              external)
        if not external:
            similar = [d for d in self.find_fuzzy(text, "contains")
                       if not S.is_local_like(d.symbol)]
            if similar:
                return Resolution(None, similar, f"no symbol named {text!r}; names containing it "
                                                 "(case-insensitive):")
        if len(external) == 1:
            label = "implicit, not declared in source" if self._implicit_member(external[0]) \
                else "external"
            if picked:
                label += ", the overload " + ("without parameters" if not q.params else
                                              "taking " + _param_list(q.params, q.params_text))
            return Resolution(external[0], [], f"{text} → {external[0]} ({label})")
        if external:   # listed by render.candidates
            return Resolution(None, [], f"{text!r} matches {len(external)} external symbols "
                                        f"(not defined in this project); pass one of them",
                              external)
        return Resolution(None, [], f"no symbol in this project matches {text!r}")

    def _inherited_parameterless(self, owner: str, name: str) -> str | None:
        """The method `name` without parameters that a project type inherits: from its
        nearest supertype declaring one (project types from the shards, library ones from
        their sources jars)."""
        _d, supers = self.supertypes(owner)
        for sup in supers:
            if sup.definition is not None:
                for v in self._views(self.ws.shard_for_source(sup.definition.location.source)[:1]):
                    for d in v.definitions(lambda sym, _i: _is_method_symbol(sym) and
                                           S.parent(sym) == sup.symbol and
                                           S.name_of(sym) == name):
                        if _declared_params(d) == ():
                            return d.symbol
                continue
            first = f"{sup.symbol}{S._format_name(name)}()."
            for k in self.library_overloads(first):
                if k[1] == ():
                    return k[0]
        return None

    def _library_by_params(self, external: list[str], params: tuple
                           ) -> tuple[list[str], bool, list[str]]:
        """The library overloads a parameter list means, among every overload in the sources
        jar (used here or not: `println()`); a method whose sources cannot be read keeps the
        symbols the project uses. (symbols, whether the list picked them, every overload
        known from the sources)."""
        groups: dict[tuple[str, str], list[str]] = {}
        for e in external:
            groups.setdefault((S.parent(e), S.name_of(e)), []).append(e)
        out, picked, every = [], False, []
        for group in groups.values():
            want_q = any(p and S.is_qualified_type(p) for p in params)
            known = self.library_overloads(group[0], want_q) \
                if _is_method_symbol(group[0]) else []
            if not known:
                out += by_params(group, params, lambda _s: None)
                continue
            types = {k[0]: k[1] for k in known}
            every += list(types)
            quals = {k[0]: (k[2] if len(k) > 2 else None) for k in known}
            hits = [sym for sym, t in types.items() if t == ()] if not params else \
                by_params(list(types), params, types.get, quals.get)
            if hits:
                out += hits
                picked = picked or len(known) > 1
            elif not params:
                out += group
        return out, picked, every

    @staticmethod
    def _overload_note(q: S.Query, symbol: str, every: list[Definition]) -> str:
        """When a parameter list picked one of several overloads: which, and SCIP's `m()`
        when it is another one (SCIP numbers overloads in declaration order, and `m().` is
        the first, whatever its parameters)."""
        if q.params is None:
            return ""
        group = [d for d in every if S.parent(d.symbol) == S.parent(symbol)
                 and S.name_of(d.symbol) == S.name_of(symbol)]
        if len(group) < 2:
            return ""
        out = " (the overload " + ("without parameters" if not q.params
                                   else "taking " + _param_list(q.params, q.params_text))
        first = next((d for d in group if d.symbol.endswith("().")), None)
        if not q.params and first is not None and first.symbol != symbol:
            sig = " ".join(S.without_annotations(first.info.signature).split()) \
                if first.info and first.info.signature else first.symbol
            num = S.parse(symbol)[-1].disambiguator
            out += (f"; SCIP numbers it {S.name_of(symbol)}({num}), and SCIP's "
                    f"{S.name_of(symbol)}() is another one: {sig}")
        return out + ")"

    def _implicit_member(self, symbol: str) -> bool:
        """A member with no declaration whose owner is a project type: record accessors, enum
        values()/valueOf(), default constructors."""
        owner = S.parent(symbol)
        return bool(owner) and S.is_type(owner) and self.definition(owner) is not None

    def find_external(self, text: str, exclude: Iterable[str] = ()) -> list[str]:
        """Symbols referenced but not defined in the project (JDK, libraries) whose names match:
        looked up among occurrences, since they have no SymbolInformation in the shards. Reads
        the symbol string straight from the bytes around each match, without decoding."""
        q = S.parse_query(text)
        if not q.names:
            return []
        regex = S.occurrence_regex(q)
        compiled = re.compile(regex)
        skip = set(exclude)
        found: set[str] = set()
        for sh in self.ws.grep_regex(regex):
            try:
                with open(sh.path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            for m in compiled.finditer(data):
                sym = _symbol_before(data, m.end() - 1)
                if sym and sym not in skip and S.matches(sym, q):
                    found.add(sym)
        return sorted(found)

    def require(self, text: str) -> str:
        r = self.resolve(text)
        if r.symbol is None:
            raise AmbiguousName(r)
        return r.symbol

    # --- references / definition ------------------------------------------------------------

    def scan(self, syms: set[str]) -> Iterable[tuple[DocView, list[Occurrence]]]:
        """(document, occurrences of `syms` in it) for every shard that mentions them."""
        syms = {s for s in syms if s and not S.is_local(s)}
        if not syms:
            return
        if len(syms) == 1:
            mode, pat = S.reference_search(next(iter(syms)))
            shards = self.ws.grep([pat]) if mode == "fixed" else self.ws.grep_regex(pat)
        else:
            needles = []
            for s in syms:
                if s.endswith("#"):
                    needles.extend(s.encode() + t for t in OCCURRENCE_TAGS)
                else:
                    needles.append(s.encode())
            shards = self.ws.grep(needles)
        for v in self._views(shards):
            occs = v.doc.occurrences_of(syms)
            if occs:
                yield v, occs

    def _references(self, syms: set[str], with_definitions: bool = False
                    ) -> tuple[list[Reference], list[Definition]]:
        refs: list[Reference] = []
        defs: list[Definition] = []
        seen = set()
        for v, occs in self.scan(syms):
            defined = set()
            for o in occs:
                key = (o.symbol, v.source, o.range.start_line, o.range.start_char, o.is_definition)
                if key in seen:
                    continue
                seen.add(key)
                if o.is_definition:
                    defined.add(o.symbol)
                    continue
                container, via, generated = v.container_of(o.range)
                if generated:
                    continue
                refs.append(Reference(o.symbol, Location(v.source, o.range), o.roles, container,
                                      via, v.shard.module))
            if with_definitions and defined:
                defs.extend(v.definitions_of(defined))
        refs.sort(key=lambda r: (r.location.source, r.location.range.start_line,
                                 r.location.range.start_char))
        defs.sort(key=lambda d: (d.location.source, d.location.range.start_line))
        return refs, defs

    def references(self, symbol: str) -> tuple[list[Reference], list[Definition]]:
        """(references, definitions) of an exact symbol. Occurrences inside generated code
        (e.g. the body of a Lombok getter, reported at its field) are left out."""
        if S.is_local(symbol):
            raise QueryError("locals are only meaningful inside one file; use a global symbol")
        return self._references({symbol}, with_definitions=True)

    def definition(self, symbol: str) -> Definition | None:
        if S.is_local(symbol):
            raise QueryError("locals are only meaningful inside one file; use a global symbol")
        return self._definitions_of({symbol}).get(symbol)

    # --- hierarchy --------------------------------------------------------------------------

    def implementations(self, symbol: str) -> list[Implementation]:
        """Subtypes of a type, or overrides of a method, at any depth (scip-javac records the
        whole chain of supertypes on every type and method). `direct` is computed from the
        results themselves: an implementation is indirect when it also implements another
        result."""
        found: dict[str, tuple[Definition, set[str]]] = {}
        needles = S.implementation_needles([symbol])
        for v in self._views(self.ws.grep(needles)):
            infos: dict[str, SymbolInfo] = {}
            for nd in needles:
                for info in v.doc.symbol_infos_containing(nd):
                    if any(r.symbol == symbol and r.is_implementation for r in info.relationships):
                        infos[info.symbol] = info
            for d in v.definitions_of(infos, infos):
                rels = {r.symbol for r in d.info.relationships if r.is_implementation}
                found.setdefault(d.symbol, (d, rels))
        keys = set(found)
        out = []
        for sym, (d, rels) in found.items():
            via = sorted(rels & keys)
            out.append(Implementation(d, not via, via))
        out.sort(key=lambda i: (not i.direct, i.definition.location.source,
                                i.definition.location.range.start_line))
        return out

    def supertypes(self, symbol: str) -> tuple[Definition | None, list[Supertype]]:
        """Supertypes of a type, or the methods a method overrides, nearest first when it can
        be told (direct ones are those not reachable through another supertype)."""
        d = self.definition(symbol)
        if d is None or d.info is None:
            return d, []
        supers = [r.symbol for r in d.info.relationships if r.is_implementation]
        defs = self._definitions_of(set(supers))
        reach: dict[str, set[str]] = {}
        for s in supers:
            sd = defs.get(s)
            reach[s] = ({r.symbol for r in sd.info.relationships if r.is_implementation}
                        if sd and sd.info else set())
        declared = declared_supertypes(d) if S.is_type(symbol) else set()
        out = []
        for s in supers:
            if declared:
                # The declaration is authoritative, and the only source for external chains
                # (scip-javac records every supertype, transitively).
                direct = S.name_of(s) in declared
            else:
                direct = not any(s in reach[o] for o in supers if o != s)
            out.append(Supertype(s, direct, defs.get(s)))
        out.sort(key=lambda x: (not x.direct, x.definition is None))
        return d, out

    # --- outline ----------------------------------------------------------------------------

    def outline(self, source: str) -> list[Definition]:
        source = self._normalize_source(source)
        shards = self.ws.shard_for_source(source)
        if not shards:
            raise QueryError(f"no shard for {source}: not compiled yet, or not a source of this "
                             "project")
        out = []
        for v in self._views(shards[:1]):
            out.extend(v.definitions(lambda sym, _i: not S.is_local_like(sym)))
        out.sort(key=lambda d: (d.location.range.start_line, d.location.range.start_char))
        return out

    def _normalize_source(self, source: str) -> str:
        s = source.strip()
        if os.path.isabs(s):
            s = os.path.relpath(s, self.ws.root)
        s = s.replace(os.sep, "/")
        while s.startswith("./"):
            s = s[2:]
        return s

    # --- call hierarchy ---------------------------------------------------------------------

    def callers(self, symbol: str, depth: int = 1, include_overridden: bool = False,
                max_nodes: int = 200) -> CallNode:
        """Incoming references grouped by the enclosing non-local definition, `depth` levels up.
        Field initializers are attributed to the field; static and instance initializer blocks
        to the class. With include_overridden, calls to the methods each one in the tree
        overrides (which may dispatch to it) are included too, marked with `via`: any method the
        root overrides, and further up the project methods (a library calls its own). Without
        it, the root keeps what was left out: `unfollowed` (the calls through the methods it
        overrides) and `overriders` (callers in the tree overriding project methods)."""
        root = CallNode(symbol, definition=self.definition(symbol))
        targets: dict[str, list[tuple[CallNode, str]]] = {symbol: [(root, "")]}
        overridden = _overridden(root.definition)
        if include_overridden:
            for o in overridden:
                targets.setdefault(o, []).append((root, o))
        visited = {symbol}
        nodes = 0
        level = 0
        texts: dict[str, list[str]] = {}

        def dispatching(target: str, refs: list[Reference]) -> list[Reference]:
            # super.m() in another class is bound to its own superclass: never here.
            return [r for r in refs if not self._is_super_call(r, S.name_of(target), texts)]

        while targets and level < max(1, depth):
            level += 1
            # Without include_overridden, the calls left out are counted (Object's methods
            # are not: calls on an Object, from generic code).
            unfollowed = [] if include_overridden or level > 1 else \
                [o for o in overridden if not o.startswith("java/lang/Object#")]
            refs_by_target = self._references_many(set(targets) | set(unfollowed))
            for o in unfollowed:
                sites = dispatching(o, refs_by_target.get(o, []))
                if sites:
                    root.unfollowed[o] = sites
            next_targets: dict[str, list[tuple[CallNode, str]]] = {}
            for target, parents in targets.items():
                refs = refs_by_target.get(target, [])
                by_via: dict[bool, dict[str, list[Reference]]] = {}
                for parent, via in parents:
                    if bool(via) not in by_via:
                        grouped: dict[str, list[Reference]] = {}
                        for ref in (dispatching(target, refs) if via else refs):
                            grouped.setdefault(ref.container or "(top level)", []).append(ref)
                        by_via[bool(via)] = grouped
                    grouped = by_via[bool(via)]
                    for caller, sites in sorted(grouped.items(), key=lambda kv: _site_key(kv[1][0])):
                        if nodes >= max_nodes:
                            parent.truncated = True
                            break
                        node = CallNode(caller, sites, via=via)
                        parent.children.append(node)
                        nodes += 1
                        if caller not in visited and caller != "(top level)":
                            visited.add(caller)
                            next_targets.setdefault(caller, []).append((node, ""))
            targets = next_targets
            if level < depth and targets:
                self._dispatching_targets(root, targets, include_overridden)
        return root

    def _dispatching_targets(self, root: CallNode, targets: dict[str, list[tuple]],
                             include_overridden: bool) -> None:
        """The project methods the callers just reached override: added to `targets` (their
        calls may dispatch to those callers) or, without include_overridden, the callers
        noted in root.overriders."""
        defs = self._definitions_of({c for c in targets if S.is_method(c)})
        ups = {c: _overridden(d) for c, d in defs.items()}
        # Only those whose top-level type has a file here (a library's would be looked for in
        # every shard, to be left out anyway).
        wanted = {o for os_ in ups.values() for o in os_}
        files = {s.source.rsplit("/", 1)[-1] for s in self.ws.shards_by_basename(
            {S.top_level_type_name(o) + ".java" for o in wanted if S.top_level_type_name(o)})}
        project = self._definitions_of({o for o in wanted if S.top_level_type_name(o)
                                        and S.top_level_type_name(o) + ".java" in files})
        for caller, os_ in sorted(ups.items()):
            os_ = [o for o in os_ if o in project]
            if not os_:
                continue
            if not include_overridden:
                if caller not in root.overriders:
                    root.overriders.append(caller)
                continue
            direct = [node for node, via in targets[caller] if not via]
            for o in os_:
                for node in direct:
                    targets.setdefault(o, []).append((node, o))

    def _is_super_call(self, ref: Reference, name: str, cache: dict) -> bool:
        src = ref.location.source
        lines = cache.get(src)
        if lines is None:
            try:
                with open(os.path.join(self.ws.root, src), encoding="utf-8",
                          errors="replace") as fh:
                    lines = fh.read().split("\n")
            except OSError:
                lines = []
            cache[src] = lines
        r = ref.location.range
        return r.start_line < len(lines) and \
            S.is_super_call(lines[r.start_line], r.start_char, name)

    def _references_many(self, syms: set[str]) -> dict[str, list[Reference]]:
        out: dict[str, list[Reference]] = {}
        for r in self._references(syms)[0]:
            out.setdefault(r.symbol, []).append(r)
        return out

    def callees(self, symbol: str, include_fields: bool = False,
                include_types: bool = False) -> tuple[Definition | None, list[Callee]]:
        """What the body of a definition references: methods and constructors, and optionally
        fields and types. Locals are left out."""
        d = self.definition(symbol)
        if d is None or d.enclosing is None:
            return d, []
        shards = self.ws.shard_for_source(d.location.source)
        by_symbol: dict[str, list[Location]] = {}
        for v in self._views(shards[:1]):
            for o in v.doc.occurrences:
                if o.is_definition or S.is_local(o.symbol) or not o.symbol:
                    continue
                if not d.enclosing.contains(o.range):
                    continue
                k = S.kind_of(o.symbol)
                if k in ("method", "constructor") or (include_fields and k == "term") \
                        or (include_types and k == "type"):
                    if v.container_of(o.range)[2]:
                        continue  # generated code
                    by_symbol.setdefault(o.symbol, []).append(Location(v.source, o.range))
        out = [Callee(s, sorted(locs, key=lambda l: (l.range.start_line, l.range.start_char)))
               for s, locs in by_symbol.items()]
        out.sort(key=lambda c: (c.sites[0].range.start_line, c.sites[0].range.start_char))
        return d, out


class AmbiguousName(QueryError):
    def __init__(self, resolution: Resolution):
        super().__init__(resolution.note)
        self.resolution = resolution


def _symbol_before(data: bytes, tag_pos: int, window: int = 600) -> str:
    """The symbol string of the field that ends right before `tag_pos`: the longest
    `0x12 <varint length> <bytes>` (Occurrence.symbol) that ends exactly there."""
    best = ""
    lo = max(0, tag_pos - window)
    p = tag_pos - 2
    while p >= lo:
        if data[p] == 0x12:
            n = shift = 0
            j = p + 1
            ok = False
            while j < tag_pos and shift <= 14:
                c = data[j]
                n |= (c & 0x7F) << shift
                j += 1
                if c < 0x80:
                    ok = True
                    break
                shift += 7
            if ok and j + n == tag_pos and n > 0:
                try:
                    best = data[j:tag_pos].decode("utf-8")
                except UnicodeDecodeError:
                    pass
        p -= 1
    return best


def _site_key(ref: Reference):
    return (ref.location.source, ref.location.range.start_line)


def _overridden(d: Definition | None) -> list[str]:
    """The methods a method definition overrides (transitively, as SCIP records them)."""
    if d is None or d.info is None:
        return []
    return [r.symbol for r in d.info.relationships
            if r.is_implementation and S.is_method(r.symbol) and r.symbol != d.symbol]

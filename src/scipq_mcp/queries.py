"""Navigation queries over a Workspace: find, references, definition, implementations,
supertypes, outline, callers and callees.

Every query prefilters shards by content and decodes only the candidates; nothing is cached.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable

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

    @property
    def kind(self) -> str:
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


@dataclass(slots=True)
class Resolution:
    symbol: str | None
    candidates: list[Definition] = field(default_factory=list)
    note: str = ""


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


class DocView:
    """One document plus the lookups the queries need, decoding as little as possible."""

    def __init__(self, shard: Shard, doc: Document):
        self.shard = shard
        self.doc = doc
        self.source = shard.source
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
                if o.enclosing is not None:
                    out.append((o.enclosing, o.symbol, S.is_local(o.symbol),
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

    def _make(self, o: Occurrence, info: SymbolInfo | None) -> Definition:
        d = Definition(o.symbol, Location(self.source, o.range), info, o.enclosing,
                       generated=self.is_generated(o, info))
        if S.is_local(o.symbol):
            d.container = self.container_of(o.range)[0]
        return d

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

    # --- plumbing ---------------------------------------------------------------------------

    def _views(self, shards: Iterable[Shard]) -> Iterable[DocView]:
        for sh in shards:
            try:
                docs = read_documents(sh.path)
            except (OSError, ValueError):
                continue
            for doc in docs:
                yield DocView(sh, doc)

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

    def find(self, text: str) -> list[Definition]:
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
        if not head:
            return None
        found = set()
        for v in self._views(self.ws.shards_by_basename({head + ".java"})):
            for info in v.doc.symbols:
                if not S.is_local(info.symbol) and S.matches(info.symbol, q):
                    found.add(info.symbol)
        return found.pop() if len(found) == 1 else None

    def resolve(self, text: str) -> Resolution:
        text = text.strip()
        if S.looks_like_symbol(text):
            return Resolution(text)
        found = [d for d in self.find(text) if not S.is_local(d.symbol)]
        syms = sorted({d.symbol for d in found})
        if len(syms) == 1:
            return Resolution(syms[0], found, f"{text} → {syms[0]}")
        if not syms:
            return Resolution(None, [], f"no symbol defined in this project matches {text!r}")
        return Resolution(None, found, f"{text!r} matches {len(syms)} symbols; pass one of them")

    def require(self, text: str) -> str:
        r = self.resolve(text)
        if r.symbol is None:
            raise AmbiguousName(r)
        return r.symbol

    # --- references / definition ------------------------------------------------------------

    def references(self, symbol: str) -> tuple[list[Reference], list[Definition]]:
        """(references, definitions) of an exact symbol. Occurrences inside generated code
        (e.g. the body of a Lombok getter, reported at its field) are left out."""
        if S.is_local(symbol):
            raise QueryError("locals are only meaningful inside one file; use a global symbol")
        mode, pat = S.reference_search(symbol)
        shards = self.ws.grep([pat]) if mode == "fixed" else self.ws.grep_regex(pat)
        refs: list[Reference] = []
        defs: list[Definition] = []
        seen = set()
        for v in self._views(shards):
            has_def = False
            for o in v.doc.occurrences_of([symbol]):
                key = (v.source, o.range.start_line, o.range.start_char, o.is_definition)
                if key in seen:
                    continue
                seen.add(key)
                if o.is_definition:
                    has_def = True
                    continue
                container, via, generated = v.container_of(o.range)
                if generated:
                    continue
                refs.append(Reference(symbol, Location(v.source, o.range), o.roles, container, via))
            if has_def:
                defs.extend(v.definitions_of([symbol]))
        refs.sort(key=lambda r: (r.location.source, r.location.range.start_line,
                                 r.location.range.start_char))
        defs.sort(key=lambda d: (d.location.source, d.location.range.start_line))
        return refs, defs

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
        out = []
        for s in supers:
            indirect = any(s in reach[o] for o in supers if o != s)
            out.append(Supertype(s, not indirect, defs.get(s)))
        out.sort(key=lambda x: (not x.direct, x.definition is None))
        return d, out

    # --- outline ----------------------------------------------------------------------------

    def outline(self, source: str) -> list[Definition]:
        source = self._normalize_source(source)
        shards = self.ws.shards_for_source(source)
        if not shards:
            raise QueryError(f"no shard for {source}: not compiled yet, or not a source of this "
                             "project")
        out = []
        for v in self._views(shards[:1]):
            out.extend(v.definitions(lambda sym, _i: not S.is_local(sym)))
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
        to the class. With include_overridden, calls to the methods this one overrides (which
        may dispatch here) are included too, marked with `via`."""
        root = CallNode(symbol, definition=self.definition(symbol))
        targets: dict[str, list[tuple[CallNode, str]]] = {symbol: [(root, "")]}
        if include_overridden and root.definition and root.definition.info:
            for r in root.definition.info.relationships:
                if r.is_implementation and S.is_method(r.symbol):
                    targets.setdefault(r.symbol, []).append((root, r.symbol))
        visited = {symbol}
        nodes = 0
        level = 0
        while targets and level < max(1, depth):
            level += 1
            refs_by_target = self._references_many(set(targets))
            next_targets: dict[str, list[tuple[CallNode, str]]] = {}
            for target, parents in targets.items():
                grouped: dict[str, list[Reference]] = {}
                for ref in refs_by_target.get(target, []):
                    grouped.setdefault(ref.container or "(top level)", []).append(ref)
                for parent, via in parents:
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
        return root

    def _references_many(self, syms: set[str]) -> dict[str, list[Reference]]:
        if len(syms) == 1:
            s = next(iter(syms))
            return {s: self.references(s)[0]}
        needles = []
        for s in syms:
            if s.endswith("#"):
                needles.extend(s.encode() + t for t in OCCURRENCE_TAGS)
            else:
                needles.append(s.encode())
        out: dict[str, list[Reference]] = {}
        seen = set()
        for v in self._views(self.ws.grep(needles)):
            for o in v.doc.occurrences_of(syms):
                if o.is_definition:
                    continue
                key = (o.symbol, v.source, o.range.start_line, o.range.start_char)
                if key in seen:
                    continue
                seen.add(key)
                container, via, generated = v.container_of(o.range)
                if generated:
                    continue
                out.setdefault(o.symbol, []).append(
                    Reference(o.symbol, Location(v.source, o.range), o.roles, container, via))
        return out

    def callees(self, symbol: str, include_fields: bool = False,
                include_types: bool = False) -> tuple[Definition | None, list[Callee]]:
        """What the body of a definition references: methods and constructors, and optionally
        fields and types. Locals are left out."""
        d = self.definition(symbol)
        if d is None or d.enclosing is None:
            return d, []
        shards = self.ws.shards_for_source(d.location.source)
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


def _site_key(ref: Reference):
    return (ref.location.source, ref.location.range.start_line)

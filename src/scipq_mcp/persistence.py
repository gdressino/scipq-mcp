"""The persistence model the active profiles describe (jdo, jpa or one of yours): entities and
their tables, inheritance and discriminators, fields and their columns, relations, fetch
groups; for a field, who writes it.

Attribute values come from the annotations' source text (the index does not keep them); an
attribute that is a constant, not a literal, is shown as written.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import symbols as S
from .graph import Graph, _annotation_args

_JAVA_MODS = {"public", "protected", "private", "static", "final", "transient", "volatile",
              "abstract", "synchronized", "native", "strictfp", "default"}


@dataclass
class PField:
    symbol: str
    name: str
    type_text: str
    source: str
    line: int
    column: str = ""             # as written in the mapping, or ""
    column_from: str = ""        # the annotation that names it
    persistent: bool = True
    why_not: str = ""            # @NotPersistent, transient, static
    relation: str = ""           # the entity it points to (symbol)
    many: bool = False           # a collection or map of it
    mapped_by: str = ""
    join: bool = False
    key: bool = False
    version: bool = False
    flags: list[str] = field(default_factory=list)
    inherited_from: str = ""


@dataclass
class Entity:
    symbol: str
    source: str
    line: int
    table: str = ""
    inheritance: str = ""
    discriminator: str = ""
    superclass: str = ""         # the nearest persistent supertype
    subclasses: list[str] = field(default_factory=list)
    fields: list[PField] = field(default_factory=list)
    fetch_groups: list[tuple[str, list[str]]] = field(default_factory=list)
    uniques: list[str] = field(default_factory=list)
    mapped: bool = False         # a mapped superclass / embeddable, no table of its own


class Model:
    def __init__(self, g: Graph, settings, root: str):
        self.g, self.st, self.root = g, settings, root
        self.entity_anns = set(settings.entity_annotations)
        self.mapped_anns = set(settings.persistence_markers.get("mapped", set()))
        self.texts: dict[str, list[str]] = {}
        self._entities: set[str] | None = None
        self._mapped: set[str] | None = None
        self._by_name: dict[str, list[str]] | None = None
        self._supers: dict[str, list[str]] = {}

    # --- lookups -------------------------------------------------------------------------

    def _annotated(self, anns: set[str]) -> set[str]:
        ids = self.g.ids(anns)
        if not ids:
            return set()
        q = ",".join("?" * len(ids))
        rows = self.g.con.execute(f"SELECT DISTINCT def_id FROM annotations "
                                  f"WHERE annotation_id IN ({q})", list(ids.values()))
        return {x for x in self.g.names([d for (d,) in rows]).values() if S.is_type(x)}

    def entities(self) -> set[str]:
        if self._entities is None:
            self._entities = self._annotated(self.entity_anns)
        return self._entities

    def mapped(self) -> set[str]:
        if self._mapped is None:
            self._mapped = self._annotated(self.mapped_anns)
        return self._mapped

    def lines(self, source: str) -> list[str]:
        if source not in self.texts:
            try:
                with open(os.path.join(self.root, source), encoding="utf-8",
                          errors="replace") as fh:
                    self.texts[source] = fh.read().split("\n")
            except OSError:
                self.texts[source] = []
        return self.texts[source]

    def annotations_of(self, symbols: list[str]) -> dict[str, list[tuple[str, str]]]:
        """{symbol: [(annotation symbol, its argument text)]} for these definitions."""
        ids = self.g.ids(symbols)
        if not ids:
            return {}
        by_id = {v: k for k, v in ids.items()}
        sh = self.g.shards()
        out: dict[str, list[tuple[str, str]]] = {}
        q = ",".join("?" * len(ids))
        for def_id, ann, shard_id, line in self.g.con.execute(
                f"SELECT a.def_id, s.symbol, a.shard_id, a.line FROM annotations a JOIN symbols "
                f"s ON s.id = a.annotation_id WHERE a.def_id IN ({q})", list(ids.values())):
            if shard_id not in sh:
                continue
            args = _annotation_args(self.lines(sh[shard_id][0]), line, S.name_of(ann))
            out.setdefault(by_id[def_id], []).append((ann, args))
        return out

    @staticmethod
    def attribute(args: str, attr: str) -> str:
        """An attribute's value as written: a string's text, else the expression."""
        m = re.search(r"(?:^|[(,\s])" + re.escape(attr) + r'\s*=\s*"((?:[^"\\]|\\.)*)"', args)
        if m:
            return m.group(1)
        m = re.search(r"(?:^|[(,\s])" + re.escape(attr) + r"\s*=\s*([\w$.]+)", args)
        if m:
            return m.group(1)
        if attr in ("value", "name"):
            m = re.match(r'\s*\(?\s*"((?:[^"\\]|\\.)*)"', args)
            if m:
                return m.group(1)
        return ""

    def _spec_value(self, anns: list[tuple[str, str]], specs) -> tuple[str, str]:
        for spec_anns, attr in specs:
            for a, args in anns:
                if a in spec_anns:
                    v = self.attribute(args, attr)
                    if v:
                        return v, "@" + S.name_of(a)
        return "", ""

    def _members(self, t: str) -> list[tuple[str, str, int, str]]:
        """[(field symbol, source, line, signature)] of a type's own fields."""
        hi = t[:-1] + chr(ord(t[-1]) + 1)
        sh = self.g.shards()
        out, seen = [], set()
        for sym, shard_id, line, sig in self.g.con.execute(
                "SELECT s.symbol, d.shard_id, d.line, d.signature FROM symbols s JOIN defs d "
                "ON d.symbol_id = s.id WHERE s.symbol >= ? AND s.symbol < ?", (t, hi)):
            if S.parent(sym) != t or S.kind_of(sym) != "term" or sym in seen \
                    or shard_id not in sh:
                continue
            seen.add(sym)
            out.append((sym, sh[shard_id][0], line + 1, sig or ""))
        return sorted(out, key=lambda x: x[2])

    def supertypes(self, t: str) -> list[str]:
        if t not in self._supers:
            sid = self.g.ids([t]).get(t)
            self._supers[t] = [] if sid is None else [
                x for x in self.g.names(self.g.ancestors(sid)).values() if S.is_type(x)]
        return self._supers[t]

    def persistent_by_name(self) -> dict[str, list[str]]:
        if self._by_name is None:
            self._by_name = {}
            for x in self.entities() | self.mapped():
                self._by_name.setdefault(S.name_of(x), []).append(x)
        return self._by_name

    # --- the model -------------------------------------------------------------------------

    def entity(self, t: str, light: bool = False) -> Entity:
        """The model of one persistent type; `light` (for lists) leaves out its subclasses
        and the fields of its supertypes."""
        defs = self.g.definitions(self.g.ids([t]).values())
        d = next(iter(defs.values()), ("", 0, "", 0, ""))
        e = Entity(t, d[0], d[1], mapped=t in self.mapped() and t not in self.entities())
        type_anns = self.annotations_of([t]).get(t, [])
        e.table, _ = self._spec_value(type_anns, self.st.table)
        e.inheritance, _ = self._spec_value(type_anns, self.st.persistence_attrs.get(
            "inheritance", []))
        for spec_anns, attr in self.st.persistence_attrs.get("discriminator", []):
            for a, args in type_anns:
                if a in spec_anns:
                    e.discriminator = re.sub(r"\s+", " ", args.strip())[:160]
        persistent = self.entities() | self.mapped()
        for s_ in self.supertypes(t):
            if s_ in persistent:
                e.superclass = s_
                break
        if not light:
            e.subclasses = sorted(x for x in self.g._subtypes_of({t}) if x in persistent)
        # Fetch groups and unique/index members name fields in strings.
        for a, args in type_anns:
            name = S.name_of(a)
            if name in ("FetchGroup", "FetchGroups"):
                for fg in re.finditer(r'name\s*=\s*"([^"]*)"\s*,\s*members\s*=\s*\{(.*?)\}',
                                      args, re.S):
                    members = re.findall(r'name\s*=\s*"([^"]*)"', fg.group(2))
                    e.fetch_groups.append((fg.group(1), members))
            if name in ("Unique", "Uniques", "Index", "Indices"):
                for mm in re.finditer(r'members\s*=\s*\{([^}]*)\}', args):
                    e.uniques.append(f"@{name}(" + ", ".join(re.findall(r'"([^"]*)"',
                                                                        mm.group(1))) + ")")
        # A container (@FetchGroups, @Uniques) and the annotations inside it are both
        # recorded: once each.
        e.fetch_groups = list(dict.fromkeys((n, tuple(ms)) for n, ms in e.fetch_groups))
        e.fetch_groups = [(n, list(ms)) for n, ms in e.fetch_groups]
        seen_u, uniq = set(), []
        for u in e.uniques:
            key = re.sub(r"^@(Unique|Index)(s|es|ices)?", r"@\1", u)
            key = key.replace("@Indic", "@Index")
            if key not in seen_u:
                seen_u.add(key)
                uniq.append(key)
        e.uniques = uniq
        # Own fields, then those of persistent supertypes.
        chain = [(t, "")] + ([] if light else [(s_, s_) for s_ in self.supertypes(t)
                                                if s_ in persistent])
        for owner, inherited in chain:
            members = self._members(owner)
            anns = self.annotations_of([m[0] for m in members])
            for sym, src, line, sig in members:
                e.fields.append(self._field(sym, src, line, sig, anns.get(sym, []), inherited))
        return e

    def _field(self, sym: str, src: str, line: int, sig: str, anns: list[tuple[str, str]],
               inherited: str) -> PField:
        name = S.name_of(sym)
        head = re.sub(r"@\s*[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?", " ", sig)
        head = head.split("=")[0]
        words = head.split()
        mods = {w for w in words if w in _JAVA_MODS}
        type_text = " ".join(w for w in words if w not in _JAVA_MODS)
        if type_text.endswith(" " + name):
            type_text = type_text[:-len(name) - 1].strip()
        f = PField(sym, name, type_text, src, line, inherited_from=inherited)
        markers = self.st.persistence_markers
        names = {a for a, _x in anns}
        if "static" in mods:
            f.persistent, f.why_not = False, "static"
        elif "transient" in mods:
            f.persistent, f.why_not = False, "transient"
        elif names & markers.get("transient", set()):
            f.persistent, f.why_not = False, "@" + S.name_of(next(iter(
                names & markers["transient"])))
        f.column, f.column_from = self._spec_value(anns, self.st.column)
        f.key = bool(names & markers.get("key", set()))
        f.version = bool(names & markers.get("version", set()))
        f.join = bool(names & markers.get("join", set()))
        f.flags = sorted("@" + S.name_of(a) for a in names & markers.get("flags", set()))
        f.mapped_by, _ = self._spec_value(anns, self.st.persistence_attrs.get("mapped_by", []))
        # A relation: the type, or its last type argument, is persistent.
        targets = re.findall(r"[A-Z][\w$]*", type_text)
        by_name = self.persistent_by_name()
        for tname in reversed(targets):
            cands = by_name.get(tname, [])
            if len(cands) == 1:
                f.relation = cands[0]
                f.many = "<" in type_text or type_text.endswith("[]")
                break
        return f


def table_of(m: Model, e: Entity) -> str:
    if e.table:
        return e.table
    if e.superclass and "SUPERCLASS" in e.inheritance.upper():
        base = m.entity(e.superclass)
        return f"{table_of(m, base)} (the superclass's table: {e.inheritance})"
    return "(named after the class)"


def render_entity(m: Model, e: Entity, offset: int, limit: int) -> str:
    kind = "mapped superclass / embeddable" if e.mapped else "entity"
    lines = [f"{S.short(e.symbol)}  {kind}  {e.source}:{e.line}",
             f"  table: {table_of(m, e)}"]
    if e.inheritance:
        lines.append(f"  inheritance: {e.inheritance}")
    if e.discriminator:
        lines.append(f"  discriminator: {e.discriminator}")
    if e.superclass:
        lines.append(f"  extends the persistent {S.short(e.superclass)}")
    if e.subclasses:
        lines.append(f"  persistent subclasses ({len(e.subclasses)}): "
                     + ", ".join(S.short(x) for x in e.subclasses[:12])
                     + (" …" if len(e.subclasses) > 12 else ""))
    keys = [f.name for f in e.fields if f.key]
    lines.append("  identity: " + (", ".join(keys) if keys else "not declared in the fields "
                                   "(datastore identity, or in a superclass)"))
    persistent = [f for f in e.fields if f.persistent]
    rows = []
    for f in persistent:
        col = (f"column {f.column} ({f.column_from})" if f.column else
               "column from the field name")
        rel = ""
        if f.relation:
            rel = f"  → {'many ' if f.many else ''}{S.short(f.relation)}"
            if f.mapped_by:
                rel += f" (mappedBy {f.mapped_by})"
            if f.join:
                rel += " (join table)"
        extra = " ".join((["key"] if f.key else []) + (["version"] if f.version else [])
                         + f.flags)
        rows.append(f"    {f.name}  {f.type_text}  {col}{rel}"
                    + (f"  [{extra}]" if extra else "")
                    + (f"  (from {S.name_of(f.inherited_from)})" if f.inherited_from else ""))
    lines.append(f"  persistent fields ({len(persistent)}):")
    shown = rows[offset:offset + limit]
    lines += shown
    if offset + limit < len(rows):
        lines.append(f"    next offset {offset + limit}")
    other = [f for f in e.fields if not f.persistent]
    if other:
        lines.append("  not persistent: " + ", ".join(f"{f.name} ({f.why_not})"
                                                      for f in other[:20]))
    for name, members in e.fetch_groups:
        lines.append(f"  fetch group {name}: {', '.join(members)}")
    for u in e.uniques:
        lines.append(f"  {u}")
    return "\n".join(lines)


def render_list(m: Model, scope_filter, offset: int, limit: int) -> str:
    ents = sorted(m.entities())
    rows = []
    for t in ents:
        e = m.entity(t, light=True)
        if not scope_filter(e):
            continue
        persistent = sum(1 for f in e.fields if f.persistent and not f.inherited_from)
        rels = sum(1 for f in e.fields if f.relation and not f.inherited_from)
        rows.append(f"  {S.short(t)}  {table_of(m, e)}"
                    + (f"  {e.inheritance}" if e.inheritance else "")
                    + (f"  extends {S.name_of(e.superclass)}" if e.superclass else "")
                    + f"  {persistent} fields, {rels} relations  {e.source}:{e.line}")
    if not rows:
        return "no entities (no persistence profile active, or none in this scope)"
    lines = [f"{len(rows)} entities (table, inheritance, own persistent fields):"]
    lines += rows[offset:offset + limit]
    if offset + limit < len(rows):
        lines.append(f"next offset {offset + limit}")
    return "\n".join(lines)


def writes_by_name(root: str, name: str, globs: list[str], limit: int = 40) -> list[str]:
    """Lines that write (or read) a persistent field by its name: reflection, fixtures, form
    bindings, queries — strings in .java that read as code, table cells and quoted names in
    .feature files, any mention in other configured files."""
    from . import extras as X
    from .refactor import _code_like
    hits = X.uses_by_name(root, [name], list(dict.fromkeys(globs + ["*.feature"])), set(),
                          limit=5000)
    cell = re.compile(r"\|\s*" + re.escape(name) + r"\s*\||[\"'`]" + re.escape(name) + r"[\"'`]")
    out = []
    for h in hits:
        if any(p.startswith(".") for p in h.source.split("/")[:-1]):
            continue
        if h.source.endswith(".java") and not _code_like(h.text, [name]):
            continue
        if h.source.endswith((".feature", ".md", ".txt")) and not cell.search(h.text):
            continue
        out.append(f"    {h.source}:{h.line}  {h.text[:140]}")
        if len(out) >= limit:
            break
    return out


def render_field(nav, m: Model, sym: str, globs: list[str]) -> str:
    from . import context as C
    owner = S.parent(sym)
    e = m.entity(owner)
    f = next((x for x in e.fields if x.symbol == sym), None)
    d = nav.definition(sym)
    if f is None or d is None:
        return f"{S.short(sym)} is not a field of a persistent type here"
    lines = [f"{S.short(sym)}  {f.type_text}  {f.source}:{f.line}"]
    if not f.persistent:
        lines.append(f"  not persistent ({f.why_not})")
    else:
        lines.append(f"  table {table_of(m, e)}, column "
                     + (f"{f.column} ({f.column_from})" if f.column else
                        "from the field name (no explicit column)"))
        if f.relation:
            lines.append(f"  relation to {'many ' if f.many else ''}{S.short(f.relation)}"
                         + (f", mappedBy {f.mapped_by}" if f.mapped_by else "")
                         + (", join table" if f.join else ""))
        groups = [n for n, members in e.fetch_groups if f.name in members]
        if groups:
            lines.append(f"  in fetch groups: {', '.join(groups)}")
        named = [u for u in e.uniques if f'"{f.name}"' in u or f" {f.name}" in u
                 or f"({f.name}" in u]
        if named:
            lines.append("  in " + ", ".join(named))
    sections, _refs, _srcs = C.field_sections(nav, d, S.short(sym))
    for s_ in sections:
        if s_.name in ("accessors", "writes", "reads"):
            body = s_.lines if s_.name != "reads" else s_.lines[:15] + (
                [f"  … and {len(s_.lines) - 15} more"] if len(s_.lines) > 15 else [])
            lines.append(s_.title)
            lines += ["  " + x for x in body[:60]]
    by_name = writes_by_name(nav.ws.root, f.name, globs)
    if by_name:
        lines.append(f"by its name, the index does not see (reflection, fixtures, forms, "
                     f"queries; check them, {len(by_name)}{'+' if len(by_name) >= 40 else ''}):")
        lines += by_name
    return "\n".join(lines)

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
    via: str = ""                # the interface it is typed with, when that is not the entity
    many: bool = False           # a collection or map of it
    mapped_by: str = ""
    join: bool = False
    join_table: str = ""
    join_column: str = ""        # the join table's column pointing back to this entity
    element_column: str = ""     # … and the one pointing to the element
    key: bool = False
    version: bool = False
    flags: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)   # length, nullability
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
    version: str = ""            # a version declared on the class (strategy, column, field)
    test: bool = False


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
        self._types: dict[str, list[str]] | None = None
        self._implementers: dict[str, list[str]] = {}

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

    def project_types(self) -> dict[str, list[str]]:
        """{simple name: [type symbols]} of the types defined in the project."""
        if self._types is None:
            self._types = {}
            for (sym,) in self.g.con.execute(
                    "SELECT DISTINCT s.symbol FROM symbols s JOIN defs d ON d.symbol_id = s.id "
                    "WHERE s.symbol LIKE '%#'"):
                self._types.setdefault(S.name_of(sym), []).append(sym)
        return self._types

    def implementers(self, t: str) -> list[str]:
        """The persistent subtypes of a type that is not persistent itself (an interface)."""
        if t not in self._implementers:
            persistent = self.entities() | self.mapped()
            self._implementers[t] = sorted(x for x in self.g._subtypes_of({t})
                                           if x in persistent)
        return self._implementers[t]

    def family(self, t: str) -> set[str]:
        """A type and all its subtypes."""
        return {t} | self.g._subtypes_of({t})

    # --- the model -------------------------------------------------------------------------

    def entity(self, t: str, light: bool = False) -> Entity:
        """The model of one persistent type; `light` (for lists) leaves out its subclasses
        and the fields of its supertypes."""
        defs = self.g.definitions(self.g.ids([t]).values())
        d = next(iter(defs.values()), ("", 0, "", 0, ""))
        e = Entity(t, d[0], d[1], mapped=t in self.mapped() and t not in self.entities(),
                   test=bool(d[3]))
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
        version_field = version_column = ""
        for a, args in type_anns:
            name = S.name_of(a)
            if name in ("FetchGroup", "FetchGroups"):
                for fg in re.finditer(r'name\s*=\s*"([^"]*)"\s*,\s*members\s*=\s*\{(.*?)\}',
                                      args, re.S):
                    members = re.findall(r'name\s*=\s*"([^"]*)"', fg.group(2))
                    e.fetch_groups.append((fg.group(1), members))
            if name in ("Unique", "Uniques", "Index", "Indices"):
                # One per members list; a container holds several (@Uniques({@Unique(…)})).
                parts = re.split(r"@\s*(?:[\w$]+\s*\.\s*)*(?:Unique|Index)\b", args)
                for part in parts:
                    mm = re.search(r'members\s*=\s*\{([^}]*)\}', part)
                    if not mm:
                        continue
                    label = self.attribute(part, "name")
                    e.uniques.append(f"@{name}(" + ", ".join(re.findall(r'"([^"]*)"',
                                                                        mm.group(1))) + ")"
                                     + (f" {label}" if label else ""))
            if a in self.st.persistence_markers.get("version", set()):
                strategy = self.attribute(args, "strategy").rsplit(".", 1)[-1]
                col = self.attribute(args, "column")
                fm = re.search(r'key\s*=\s*"field-name"\s*,\s*value\s*=\s*"([^"]+)"', args)
                e.version = ", ".join(x for x in (strategy, col and f"column {col}",
                                                  fm and f"field {fm.group(1)}") if x)
                version_field, version_column = (fm.group(1) if fm else ""), col
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
        for f in e.fields:
            if version_field and f.name == version_field and not f.inherited_from:
                f.version = True
                if not f.column and version_column:
                    f.column, f.column_from = version_column, "@Version of the class"
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
        flags = []
        for a, args in anns:
            if a not in markers.get("flags", set()):
                continue
            label = "@" + S.name_of(a)
            if S.name_of(a) == "Convert":
                conv = self.attribute(args, "value") or self.attribute(args, "converter")
                if not conv:
                    cm = re.match(r"\s*([\w$.]+)\.class\b", args)
                    conv = cm.group(1) if cm else ""
                if conv:
                    label += f"({conv.removesuffix('.class').rsplit('.', 1)[-1]})"
            flags.append(label)
        f.flags = sorted(set(flags))
        f.mapped_by, _ = self._spec_value(anns, self.st.persistence_attrs.get("mapped_by", []))
        for a, args in anns:
            if S.name_of(a) == "Column":
                for attr, label in (("length", "length"), ("allowsNull", "allowsNull"),
                                    ("nullable", "nullable"), ("jdbcType", "jdbcType")):
                    v = self.attribute(args, attr)
                    if v:
                        f.details.append(f"{label} {v}")
        # A join table: its name and columns (JDO @Join/@Element, JPA @JoinTable).
        for a, args in anns:
            n = S.name_of(a)
            if a in markers.get("join", set()):
                f.join_table = f.join_table or self.attribute(args, "table") or \
                    self.attribute(args, "name")
                f.join_column = self.attribute(args, "column") or _nested_column(
                    args, "joinColumns")
                f.element_column = f.element_column or _nested_column(args,
                                                                      "inverseJoinColumns")
            elif n == "Element":
                f.element_column = self.attribute(args, "column") or f.element_column
            elif n == "Persistent" and self.attribute(args, "table"):
                f.join_table = self.attribute(args, "table")
        f.join = f.join or bool(f.join_table)
        # A relation: the type, or its last type argument, is persistent, or an interface
        # that one persistent class implements.
        targets = re.findall(r"[A-Z][\w$]*", type_text)
        by_name = self.persistent_by_name()
        many = "<" in type_text or type_text.endswith("[]")
        for tname in reversed(targets):
            cands = by_name.get(tname, [])
            if len(cands) == 1:
                f.relation, f.many = cands[0], many
                break
        else:
            for tname in reversed(targets):
                types = self.project_types().get(tname, [])
                impl = self.implementers(types[0]) if len(types) == 1 else []
                if len(impl) == 1:
                    f.relation, f.via, f.many = impl[0], types[0], many
                    break
        return f

    def inverse(self, owner: str, f: PField) -> str:
        """The field on the other side that names this one in its mappedBy, or ""."""
        if not f.relation or f.mapped_by:
            return ""
        mine = self.family(owner) | set(self.supertypes(owner))
        for x in self.entity(f.relation).fields:
            if x.mapped_by == f.name and (not x.relation or x.relation in mine):
                return x.symbol
        return ""

    def foreign_key(self, f: PField) -> tuple[str, str, str]:
        """For a mappedBy collection: (table, column, field) of the other side's key."""
        if not (f.relation and f.mapped_by):
            return "", "", ""
        other = self.entity(f.relation)
        x = next((y for y in other.fields if y.name == f.mapped_by), None)
        if x is None:
            return table_of(self, other), "", ""
        return table_of(self, other), x.column or x.name.upper(), x.symbol


def _nested_column(args: str, attr: str) -> str:
    """The name of the first @JoinColumn in `attr = {@JoinColumn(name = "X")…}`."""
    m = re.search(re.escape(attr) + r'\s*=\s*\{?\s*@\s*[\w$.]*JoinColumn\s*\(\s*'
                  r'(?:name\s*=\s*)?"([^"]+)"', args)
    return m.group(1) if m else ""


def column_text(m: Model, f: PField) -> str:
    """Where a persistent field is stored, in words."""
    if f.relation and f.mapped_by:
        table, col, sym = m.foreign_key(f)
        return (f"no column here: key {col} in {table} ({S.short(sym)})" if col else
                f"no column here: the other side ({S.name_of(f.relation)}#{f.mapped_by}) "
                "holds the key")
    if f.join:
        cols = ", ".join(x for x in (f.join_column and f"{f.join_column} → this",
                                     f.element_column and f"{f.element_column} → the element")
                         if x)
        return f"join table {f.join_table or '(default name)'}" + (f" ({cols})" if cols else "")
    if f.column:
        return f"column {f.column} ({f.column_from})"
    if f.many and f.relation:
        return "join table (the default for a collection without mappedBy)"
    return f"column {f.name.upper()} (from the field name)"


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
    if e.version:
        lines.append(f"  version: {e.version}")
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
        extra = " ".join((["key"] if f.key else []) + (["version"] if f.version else [])
                         + f.flags + [x.replace(" ", "=") for x in f.details])
        rows.append(f"    {f.name}  {f.type_text}  {column_text(m, f)}"
                    + relation_text(m, e.symbol, f)
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


def relation_text(m: Model, owner: str, f: PField) -> str:
    if not f.relation:
        return ""
    out = f"  → {'many ' if f.many else ''}{S.short(f.relation)}"
    if f.via:
        out += f" (implements {S.name_of(f.via)})"
    if f.mapped_by:
        out += f" (mappedBy {f.mapped_by})"
    inv = m.inverse(f.inherited_from or owner, f)
    if inv:
        out += f" (other side: {S.short(inv)}, mappedBy)"
    return out


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
                    + f"  {persistent} fields, {rels} relations  {e.source}:{e.line}"
                    + ("  [test]" if e.test else ""))
    if not rows:
        return "no entities (no persistence profile active, or none in this scope)"
    lines = [f"{len(rows)} entities (table, inheritance, own persistent fields):"]
    lines += rows[offset:offset + limit]
    if offset + limit < len(rows):
        lines.append(f"next offset {offset + limit}")
    return "\n".join(lines)


TIERS = {
    1: "strings in the files of {owner} and its subtypes (forms, annotations)",
    2: "paths through a relation to it",
    3: "step tables that name it",
    4: "other strings read as code (property ids, queries, other files)",
    5: "other step tables and quoted mentions",
}
_KEY_CALL = re.compile(r"\b(put|get|getOrDefault|containsKey|remove|setParameter|addParameter)"
                       r"\s*\(\s*\"")
_PROSE = (".feature", ".md", ".txt", ".adoc")


@dataclass
class ByName:
    rows: list[tuple[int, str, int, str]]      # (tier, source, line, text), best first
    keys: list[tuple[str, int, str]]           # map or parameter keys: put/get("name")
    elsewhere: list[tuple[str, int, str]]      # paths through other types' fields of that name
    aliases: list[str]

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.keys) + len(self.elsewhere)


def _alias(name: str) -> list[str]:
    """How code names an instance of a type: Order -> order; a one-letter prefix off too
    (XOrder -> order)."""
    out = [name[:1].lower() + name[1:]]
    if re.match(r"[A-Z][A-Z][a-z]", name):
        out.append(name[1:2].lower() + name[2:])
    return out


def relation_names(m: Model, family: set[str]) -> set[str]:
    """Names of the fields, in any type, typed as one of these types (or as an interface only
    they implement among persistent classes)."""
    names = {S.name_of(t) for t in family}
    persistent = m.entities() | m.mapped()
    for t in family:
        for sup in m.supertypes(t):
            if sup not in persistent and m.implementers(sup) and \
                    set(m.implementers(sup)) <= family:
                names.add(S.name_of(sup))
    names = {n for n in names if n}
    if not names:
        return set()
    where = " OR ".join("d.signature LIKE ?" for _ in names)
    word = re.compile(r"(?<![\w$])(" + "|".join(re.escape(n) for n in names) + r")(?![\w$])")
    out = set()
    for sym, sig in m.g.con.execute(
            f"SELECT s.symbol, d.signature FROM defs d JOIN symbols s ON s.id = d.symbol_id "
            f"WHERE {where}", [f"%{n}%" for n in names]):
        if S.kind_of(sym) != "term" or not sig:
            continue
        head = re.sub(r"@\s*[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?", " ", sig).split("=")[0]
        if word.search(head):
            out.add(S.name_of(sym))
    return out


def by_name(m: Model, field_sym: str, globs: list[str]) -> ByName:
    """Uses of a persistent field by its name, ranked by how surely they mean this field:
    strings in its type's own files (forms), paths through relations to the type, step
    tables that name the type, other strings read as code, other mentions; map keys and
    paths through other types' fields of the same name are only counted."""
    from . import extras as X
    from .refactor import _code_like
    name = S.name_of(field_sym)
    owner = S.parent(field_sym)
    family = m.family(owner)
    defs = m.g.definitions(m.g.ids(family).values())
    own_files = {d[0] for d in defs.values()}
    type_names = {S.name_of(t) for t in family}
    aliases = sorted(relation_names(m, family) | {a for t in type_names for a in _alias(t)})
    alias_set = set(aliases)
    hits = X.uses_by_name(m.root, [name], list(dict.fromkeys(globs + ["*.feature"])), set(),
                          limit=20000)
    seg = re.compile(r"([\w$]+)\s*\.\s*" + re.escape(name) + r"(?![\w$])")
    cell = re.compile(r"\|\s*" + re.escape(name) + r"\s*\|")
    quoted = re.compile(r"[\"'`]" + re.escape(name) + r"[\"'`]")
    named = re.compile(r"(?<![\w$])(" + "|".join(re.escape(n) for n in type_names) +
                       r")(?![\w$])") if type_names else None
    rows, keys, elsewhere = [], [], []
    for h in hits:
        if any(p.startswith(".") for p in h.source.split("/")[:-1]):
            continue
        java = h.source.endswith(".java")
        if java and not _code_like(h.text, [name]):
            continue
        if h.source.endswith(_PROSE):
            if cell.search(h.text):
                step = _step_above(m.lines(h.source), h.line - 1)
                rows.append((3 if named and named.search(step) else 5, h.source, h.line,
                             h.text))
            elif quoted.search(h.text):
                rows.append((5, h.source, h.line, h.text))
            continue
        if java and h.source in own_files:
            rows.append((1, h.source, h.line, h.text))
            continue
        if java and _KEY_CALL.search(h.text):
            keys.append((h.source, h.line, h.text))
            continue
        segs = {x.group(1) for x in seg.finditer(h.text)} - {"this"}
        if segs & alias_set:
            rows.append((2, h.source, h.line, h.text))
        elif segs:
            elsewhere.append((h.source, h.line, h.text))
        else:
            rows.append((4, h.source, h.line, h.text))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return ByName(rows, keys, elsewhere, aliases)


def _n(k: int, what: str) -> str:
    return f"{k} {what}" + ("" if k == 1 else "s")


def _step_above(lines: list[str], i: int) -> str:
    """The step a table row belongs to: the nearest line above that is not a table row."""
    while i >= 0:
        t = lines[i].strip() if i < len(lines) else ""
        if t and not t.startswith(("|", "#")):
            return t
        i -= 1
    return ""


def render_by_name(b: ByName, owner: str, offset: int, limit: int) -> list[str]:
    if not b.total:
        return []
    lines = [f"by its name, the index does not see (reflection, forms, fixtures, queries; "
             f"{b.total}, most likely to matter first):"]
    tier = None
    for t, src, line, text in b.rows[offset:offset + limit]:
        if t != tier:
            tier = t
            title = TIERS[t].format(owner=S.name_of(owner))
            if t == 2:
                title += f" ({', '.join(a + '.' for a in b.aliases[:8])})"
            lines.append(f"  {title}:")
        lines.append(f"    {src}:{line}  {text[:140]}")
    if offset + limit < len(b.rows):
        lines.append(f"  next offset {offset + limit} ({len(b.rows)} ranked)")
    if b.keys:
        files = sorted({k[0] for k in b.keys})
        lines.append(f"  {_n(len(b.keys), 'map or parameter key')} (put/get(\"…\")) in "
                     f"{_n(len(files), 'file')}, e.g. " + ", ".join(f"{a}:{n}" for a, n, _t in b.keys[:4]))
    if b.elsewhere:
        lines.append(f"  {_n(len(b.elsewhere), 'path')} through other types' fields of that name "
                     f"(not a relation to {S.name_of(owner)}), e.g. "
                     + ", ".join(f"{a}:{n}" for a, n, _t in b.elsewhere[:4]))
    return lines


def render_field(nav, m: Model, sym: str, globs: list[str], offset: int = 0,
                 limit: int = 100) -> str:
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
        lines.append(f"  table {table_of(m, e)}, {column_text(m, f)}"
                     + (f"  [{', '.join(f.details)}]" if f.details else ""))
        if f.relation:
            lines.append(f"  relation to{relation_text(m, owner, f)[3:]}"
                         + (", join table" if f.join else ""))
        if f.version:
            lines.append("  the version of the entity" + (f" ({e.version})" if e.version else ""))
        if f.flags:
            lines.append("  " + ", ".join(f.flags))
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
    lines += render_by_name(by_name(m, sym, globs), owner, offset, limit)
    return "\n".join(lines)

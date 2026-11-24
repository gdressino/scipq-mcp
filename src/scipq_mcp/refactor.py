"""Refactoring plans: data the agent writes, turned into a verified patch.

A plan is JSON, never code. Its steps are macros (`rename`, `change_signature`,
`replace_calls`, `move`, `annotate`, `create`) or primitives: `select` (where: SCIP sites of a
symbol, or nodes of a kind inside a declaration, optionally filtered by a structural `where`
pattern) and `do` (what: rename, replace with a template, wrap, remove, insert, list edits of
arguments and parameters, annotations, modifiers, members, imports). Macros expand to
primitives, plus the checks a compiler does not make (a persistent field's column, a local that
would capture a renamed field, an override of a library method…).

Sites come from the index (exact, compiled), the nodes around them from JavaParser (the
refactor worker, `jvm.py`), the new text from edits of the original ranges (`edits.py`); every
edited file must parse again with a node of the expected kind over each edit. The patch is
stored in the cache: scipq-mcp never writes the checkout. After the agent applies it and the
build compiles, `verify` checks the new index against what the plan expected.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import cachedir
from . import edits as E
from . import extras as X
from . import symbols as S
from .context import accessor_symbols
from .queries import AmbiguousName, Navigator, QueryError

MACROS = ("rename", "change_signature", "replace_calls", "move", "annotate", "create")
ACTIONS = ("rename", "replace", "wrap", "remove", "insert_before", "insert_after",
           "add_argument", "remove_argument", "reorder_arguments", "add_parameter",
           "remove_parameter", "reorder_parameters", "add_annotation", "remove_annotation",
           "set_modifiers", "add_member", "add_import", "remove_import")
MACRO_KEYS = {
    "rename": {"symbol", "to", "family", "accessors", "keep_column", "migration"},
    "change_signature": {"symbol", "parameters", "family", "imports", "no_calls"},
    "replace_calls": {"symbol", "symbols", "overloads", "template", "imports", "family"},
    "move": {"symbol", "package", "to", "migration"},
    "annotate": {"symbols", "symbol", "add", "remove", "imports"},
    "create": {"path", "template"},
}
SELECT_KEYS = {"symbol", "symbols", "overloads", "role", "family", "accessors", "node", "files",
               "in", "kind", "where"}
STEP_KEYS = {"select", "do", "imports", "description", "comment"}
MAX_STEPS = 50
MAX_REVIEW = 20000   # uses by name kept per search (all shown in review.txt)
MAX_SITES = 20000
MAX_TEMPLATE = 20000
DIFF_LINES = 150
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
KEYWORDS = set("""abstract assert boolean break byte case catch char class const continue
default do double else enum extends final finally float for goto if implements import
instanceof int interface long native new package private protected public return short static
strictfp super switch synchronized this throw throws transient try void volatile while true
false null var record yield sealed permits""".split())
GENERATED_DIRS = {"build", "target", "out", "generated", "generated-sources",
                  "generated-test-sources", "bin"}
PERSISTENT_TYPES = ("PersistenceCapable", "Entity", "Embeddable", "MappedSuperclass",
                    "PersistenceAware")
JDO_FIELD_STRINGS = ("Persistent", "FetchGroup", "Element", "Key", "Value", "Index", "Unique",
                     "OneToMany", "ManyToMany", "OneToOne", "ManyToOne", "ElementCollection",
                     "OrderBy", "MapKey", "Order")


class PlanError(QueryError):
    """The plan is malformed: what to change in it."""


HELP = {
    "flow": """\
Refactoring with scipq-mcp (it never writes the checkout):
1. Write a plan (JSON: {"steps": [...]}) with macros or select/do primitives (topics: macros,
   select, actions, placeholders, examples).
2. refactor(plan, dry_run=true): the sites and nodes each step resolves to, the sites without
   a node, `where` captures, and the checks' refusals. Fix the plan until it says what you
   mean.
3. refactor(plan): the checks run (topic: checks); on success, a patch stored in the cache, a
   plan_id and the diff. Nothing is changed in the checkout.
4. Ask the user, then: git apply --check <patch> && git apply <patch>
5. Compile, then refactor(plan_id=..., verify=true): each renamed identifier must resolve to the
   renamed symbol (a local capturing it shows up), renamed or moved symbols must have no uses
   left, class files must be newer than their sources (a failed build can still write
   shards); it lists what the change reaches, the tests to run, and the tests and features
   that named the old names in text.
The patch is syntax-checked (JavaParser, at the project's Java version), not compiled.""",
    "macros": """\
Macros (one per step, as {"<macro>": {...}}):
- rename {symbol, to, family?=true, accessors?, keep_column?, migration?}: a method (with its
  override family), a field (Lombok's generated accessors and a record's accessor follow;
  hand-written accessors too when Lombok is in play or accessors=true), or a type (its
  constructors, imports, qualified names; the file when it is named after it). Javadoc
  {@link}/@see are rewritten; uses by name (strings, XML, .feature) are listed to review.
  A persistent field without an explicit column is refused unless keep_column="<COLUMN>"
  (adds @Column(name = ...)); a persistent class named after its table, or in a hierarchy
  whose discriminator stores class names, is refused unless migration=true.
- change_signature {symbol, parameters: [{"from": i} | {"add": "Type name", "default":
  "expr"}], family?=true, imports?}: the new parameter list, in order; declarations (with the
  override family) and every call change (varargs calls keep all their arguments); the types
  of new parameters and of defaults like Type.CONST are imported when the name is
  unambiguous, otherwise pass imports. Removed parameters still used in a body, and new ones
  hiding a field the body uses, are reported.
- replace_calls {symbol | symbols: [...], overloads?, template, imports?, family?=false}:
  every call of a method becomes the template ($recv, $0…, $args); overloads=true takes every
  overload (all constructors: "Type#<init>"); method references are listed as sites without
  a node.
- move {symbol | package, to, migration?}: a top-level type (or every type of a package) to
  another package: package declaration, file, imports and qualified names everywhere, imports
  the moved code now needs; non-public access across the new boundary is reported.
- annotate {symbols: [...], add?: "@X(...)", remove?: "X", imports?}
- create {path, template}: a new .java file (must parse; package must match the folder).""",
    "select": """\
Primitive step: {"select": {...}, "do": {"<action>": ...}, "imports": [...]?}
select by symbol: {"symbol": "Type#member" | "symbols": [...], "overloads": bool,
  "role": "all|definition|references", "family": bool (methods; with a library method, the
  library methods it overrides and the overrides in library subtypes this project calls, from
  the sources jars: java.util.Collection#size brings List#size, Set#size, ArrayList#size…,
  plus the project's overrides; or list symbols, e.g. ["java.util.List#size",
  "java.util.Set#size"]: a listed symbol nobody uses is skipped), "accessors": bool
  (fields), "files": "glob" | [...],
  "in": "Type or Type#method" (only sites inside it), "node": ..., "where": ...}
select by kind: {"in": "Type#method" | "files": "glob", "kind": "<JavaParser class>",
  "where": ...}: every node of that kind (CatchClause, IfStmt, MethodCallExpr, BinaryExpr,
  ReturnStmt, LambdaExpr, ObjectCreationExpr, FieldDeclaration…) inside it.
node (what a symbol's site selects): name (the identifier), expr (the use: call, field
  access, name), call, statement, declaration, argument[i], parameter[i], ancestor:<Kind>
  (e.g. ancestor:BinaryExpr: `x.size() == 0` around a size() call).
where: a pattern the node must match structurally, e.g. "$x.size() == 0" or
  {"pattern": "if ($c) { $$body }", "kind": "statement"}: $name captures one subtree, $$name
  the rest of a list; captures can be used in the do template. The shape alone does not tell
  types apart (a table's isEmpty() is not a list's): {"pattern": "$x.size() == 0", "types":
  {"$x": "java.util.Collection"}} keeps the sites where $x is that type or a subtype
  (qualified, or a simple name: any type so named; primitives match themselves), told from
  the index: names, fields, calls (return types), new, casts, literals. A capture whose type
  cannot be told (another kind of expression, a type variable, an array) leaves its site out,
  listed.""",
    "actions": """\
do actions (the node comes from select.node; defaults in brackets):
  rename: "newName" [name]            replace: "template" [expr]
  wrap: "template with $this" [statement]   remove: true [statement]
  insert_before / insert_after: "statement or member" [statement]
  add_argument {index?, template} / remove_argument {index} / reorder_arguments {order} [call]
  add_parameter {index?, text} / remove_parameter {index} / reorder_parameters {order}
    [declaration]
  add_annotation "@X(...)" / remove_annotation "X" / set_modifiers {add, remove}
    [declaration]
  add_member "member text" [the type's declaration]
  add_import / remove_import "pkg.Name" | "static pkg.Type.member" (select files or symbol)
"imports": [...] on a step adds those imports to every file the step changed (not in the same
package, not java.lang, not twice); on a macro step it is the macro's imports. Unknown keys
are refused.""",
    "placeholders": """\
Templates are Java text; these names stand for the original text at the site (comments and
inner edits kept): $this (the node), $name (its identifier), $recv (a call's receiver), $0 $1…
(arguments), $args (all arguments with their separators), plus the `where` captures. A
template is parsed as the node's kind (expression, statement, member…) at the project's Java
version; an expression that would merge with the code around it is parenthesized. Multi-line
templates are re-indented with the file's indentation (tabs or spaces).""",
    "checks": """\
Refused (no patch): affected files stale or a build running; a site in generated code; a
method overriding a library method; a member or type with the new name in the hierarchy; a
local or parameter that would capture a renamed field, or an enclosing (anonymous) class with
the new name; JDO/JPA columns, tables and class-name discriminators (see macros); overlapping
edits; a template or edited file that does not parse, or a node of the wrong kind over an
edit. Reported: sites without a node, uses by name to review (strings, XML, .feature, upper-case
column names, qualified names in any text file), comments inside removed code, removed
parameters still used, null arguments next to overloads.""",
    "examples": """\
{"steps": [{"rename": {"symbol": "Entity#setState", "to": "changeState"}}]}
{"steps": [{"change_signature": {"symbol": "Entity#setState", "parameters": [
   {"add": "String reason", "default": "\\"manual\\""}, {"from": 0}]}}]}
{"steps": [{"replace_calls": {"symbol": "Entity#findById",
   "template": "Objects.requireNonNull(Entity.findById($0))", "imports": ["java.util.Objects"]}}]}
{"steps": [{"move": {"symbol": "Session", "to": "com.example.store"}}]}
{"steps": [{"select": {"in": "Group", "kind": "MethodCallExpr", "where": "$x.size()"},
            "do": {"replace": "$x.toArray().length"}}]}
{"steps": [{"select": {"symbol": "Entity#setState", "role": "references", "node":
   "statement", "files": "**/Group.java"}, "do": {"wrap": "if (e != null) {\\n    $this\\n}"}}]}
{"steps": [{"select": {"in": "Store#load", "kind": "TryStmt"},
            "do": {"insert_before": "log.debug(\\"loading {}\\", id);"}}]}""",
}


def help_topic(topic: str = "") -> str:
    topic = (topic or "").strip().lower()
    if topic in ("", "all"):
        return "\n\n".join(HELP.values())
    if topic not in HELP:
        return f"topics: {', '.join(HELP)} (or all)"
    return HELP[topic]


# --- the plan --------------------------------------------------------------------------------

def load_plan(plan) -> dict:
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except json.JSONDecodeError as e:
            raise PlanError(f"the plan is not valid JSON: {e}") from e
    if isinstance(plan, list):
        plan = {"steps": plan}
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) \
            or not plan["steps"]:
        raise PlanError('a plan is {"steps": [ … ]} with at least one step (refactor_help '
                        'shows the format)')
    if len(plan["steps"]) > MAX_STEPS:
        raise PlanError(f"at most {MAX_STEPS} steps per plan")
    for k, st in enumerate(plan["steps"], 1):
        if not isinstance(st, dict):
            raise PlanError(f"step {k} is not an object")
        macros = [m for m in MACROS if m in st]
        if len(macros) > 1 or (macros and ("select" in st or "do" in st)):
            raise PlanError(f"step {k}: one macro ({', '.join(MACROS)}) or select+do per step")
        if not macros and not ("select" in st and "do" in st):
            raise PlanError(f"step {k}: needs a macro ({', '.join(MACROS)}) or select and do; "
                            f"has {', '.join(sorted(st)) or 'nothing'}")
        if macros and not isinstance(st[macros[0]], dict):
            raise PlanError(f"step {k}: {macros[0]} takes an object")
        extra = set(st) - STEP_KEYS - set(macros)
        if extra:
            raise PlanError(f"step {k}: unknown keys {', '.join(sorted(extra))} (a step has "
                            f"one macro, or select and do, plus imports)")
        if macros:
            m = macros[0]
            bad = set(st[m]) - MACRO_KEYS[m]
            if bad:
                raise PlanError(f"step {k}: {m} does not take {', '.join(sorted(bad))} (it "
                                f"takes {', '.join(sorted(MACRO_KEYS[m]))})")
            if "imports" in st:
                if "imports" not in MACRO_KEYS[m]:
                    raise PlanError(f"step {k}: {m} does not take imports")
                merged = _imports(st) + [i for i in _imports(st[m]) if i not in _imports(st)]
                st[m] = dict(st[m], imports=merged)
        if "select" in st:
            if not isinstance(st["select"], dict):
                raise PlanError(f"step {k}: select takes an object")
            bad = set(st["select"]) - SELECT_KEYS
            if bad:
                raise PlanError(f"step {k}: select does not take {', '.join(sorted(bad))} "
                                f"(it takes {', '.join(sorted(SELECT_KEYS))})")
        if "do" in st:
            do = st["do"]
            if not isinstance(do, dict) or len(do) != 1 or next(iter(do)) not in ACTIONS:
                raise PlanError(f"step {k}: do takes one action of: {', '.join(ACTIONS)}")
        for key in ("template", "replace", "wrap", "insert_before", "insert_after", "add_member"):
            for holder in (st.get("do", {}), *(st[m] for m in macros)):
                v = holder.get(key) if isinstance(holder, dict) else None
                if isinstance(v, str) and len(v) > MAX_TEMPLATE:
                    raise PlanError(f"step {k}: {key} longer than {MAX_TEMPLATE} characters")
    return plan


@dataclass
class Site:
    source: str
    line: int               # SCIP: 0-based
    col: int                # SCIP: 0-based, UTF-16
    name: str               # the identifier expected there
    symbol: str = ""
    role: str = "reference"  # or "definition"
    optional: bool = False   # not found: covered by another site (a constructor at a type)
    why: str = ""


@dataclass
class Located:
    site: Site
    node: dict
    ident: list
    qualified: bool
    locals: list
    enclosing: list
    member: dict | None
    captures: dict


@dataclass
class Op:
    step: int
    label: str
    action: str                     # one of ACTIONS, or "inspect"
    args: dict = field(default_factory=dict)
    sites: list[Site] = field(default_factory=list)
    node: str = "name"
    where: dict | None = None
    scopes: list[dict] = field(default_factory=list)   # kind-in-scope selections
    files: list[str] = field(default_factory=list)     # file-level (imports)
    imports: list[str] = field(default_factory=list)
    hook: Callable | None = None    # after analyze: (planner, op) -> None
    found: list[Located] = field(default_factory=list)
    unmatched: list[tuple[Site, str]] = field(default_factory=list)
    expect: str = ""                # the new identifier, for verify (renames)
    filtered: int = 0               # sites the node spec or where left out
    where_types: dict = field(default_factory=dict)    # "$x" -> the type it must be
    untyped: list = field(default_factory=list)        # (capture, Site) whose type is unknown


@dataclass
class FileState:
    source: E.Source
    text: E.Text
    unit: dict = field(default_factory=dict)
    edits: list[E.Edit] = field(default_factory=list)
    unit_ok: bool = True


class Planner:
    def __init__(self, nav: Navigator, settings, members_named: Callable | None = None,
                 types_named: Callable | None = None, library_family: Callable | None = None,
                 type_facts: dict | None = None):
        self.nav = nav
        # Supertypes of a type and return types of library methods (Service), for where types.
        self.captures = CaptureTypes(self, type_facts or {})
        self.root = nav.ws.root
        self.st = settings
        self._members_fn = members_named or (lambda _t, subtypes=False: {})
        self._members_cache: dict = {}
        self.types_named = types_named or (lambda _n: [])
        self.ops: list[Op] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.review: list[str] = []
        self.notes: list[str] = []
        # The library part of a library method's family (Service.library_family), notes here.
        self.library_family = library_family(self.notes) if library_family else None
        self.moves: dict[str, str] = {}
        self.creates: dict[str, str] = {}
        self.files: dict[str, FileState] = {}
        self.unit_level = ""
        self.summary: list[str] = []
        self._clashes: set = set()

    # --- helpers --------------------------------------------------------------------------

    def resolve(self, text: str, what: str = "symbol") -> str:
        if not isinstance(text, str) or not text.strip():
            raise PlanError(f"{what}: a name or SCIP symbol is required")
        r = self.nav.resolve(text)
        if r.symbol is None:
            raise AmbiguousName(r)
        return r.symbol

    def file(self, path: str) -> FileState:
        if path not in self.files:
            src = E.read_source(self.root, path)
            self.files[path] = FileState(src, E.Text(src.text))
        return self.files[path]

    @staticmethod
    def ident_name(symbol: str) -> str:
        if S.kind_of(symbol) == "constructor":
            return S.name_of(S.parent(symbol))
        return S.name_of(symbol)

    def sites_of(self, symbol: str, role: str = "all", why: str = "",
                 optional_refs: bool = False) -> list[Site]:
        refs, defs = self.nav.references(symbol)
        name = self.ident_name(symbol)
        out = []
        if role in ("all", "definition"):
            if not defs and self.nav.definition(symbol) is None and S.kind_of(symbol) != "package":
                pass
            for d in defs:
                if d.generated:
                    continue
                r = d.location.range
                out.append(Site(d.location.source, r.start_line, r.start_char, name, symbol,
                                "definition", why=why))
        if role in ("all", "references"):
            for ref in refs:
                r = ref.location.range
                out.append(Site(ref.location.source, r.start_line, r.start_char, name, symbol,
                                "reference", optional=optional_refs, why=why))
        return out

    def members_named(self, type_symbol: str, subtypes: bool = False) -> dict:
        key = (type_symbol, subtypes)
        if key not in self._members_cache:
            self._members_cache[key] = self._members_fn(type_symbol, subtypes=subtypes)
        return self._members_cache[key]

    def symbol_at(self, source: str, line: int, col: int) -> str:
        for v in self.nav._views(self.nav.ws.shard_for_source(source)):
            for o in v.doc.occurrences:
                if (o.range.start_line, o.range.start_char) == (line, col) \
                        and not S.is_local(o.symbol):
                    return o.symbol
        return ""

    def local_sites(self, symbol: str, source: str, name: str, why: str = "family"
                    ) -> list[Site]:
        """Occurrences of a local symbol (a method of an anonymous class) in its file."""
        out = []
        for v in self.nav._views(self.nav.ws.shard_for_source(source)):
            for o in v.doc.occurrences:
                if o.symbol == symbol:
                    r = o.range
                    out.append(Site(source, r.start_line, r.start_char, name, symbol,
                                    "definition" if o.is_definition else "reference", why=why))
        return out

    def resolve_many(self, sel: dict, what: str = "symbol") -> list[str]:
        """select's symbol (or symbols): with overloads=true, every overload of a method
        (constructors too), project or library."""
        texts = sel.get("symbols") or [sel.get("symbol")]
        if isinstance(texts, str):
            texts = [texts]
        listed = len(texts) > 1
        out: list[str] = []
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise PlanError(f"{what}: a name or SCIP symbol is required")
            r = self.nav.resolve(text)
            if r.symbol is None and not r.candidates and not r.externals:
                ext = self.nav.find_external(text)   # library overloads, constructors
                if len(ext) == 1:
                    r.symbol = ext[0]
                elif ext:
                    r.externals = ext
                elif listed:   # one of a list with no uses here: skip it
                    self.notes.append(f"{text}: no symbol in this project matches it; skipped")
                    continue
            if r.symbol is not None and not (sel.get("overloads") and S.is_method(r.symbol)):
                out.append(r.symbol)
                continue
            if r.symbol is not None:
                syms = [r.symbol]
                try:
                    syms = [m.symbol for m in X.overloads(self.nav, r.symbol)] or syms
                except QueryError:
                    pass
                if len(syms) == 1 and self.nav.definition(r.symbol) is None:
                    owner, name = S.parent(r.symbol), S.name_of(r.symbol)
                    found = self.nav.find_external(text.split("(")[0])
                    syms = sorted({x for x in found if S.parent(x) == owner
                                   and S.name_of(x) == name}) or syms
                out += syms
                continue
            cands = sorted({d.symbol for d in r.candidates} | set(r.externals))
            if sel.get("overloads") and cands and all(
                    S.is_method(x) and S.parent(x) == S.parent(cands[0])
                    and S.name_of(x) == S.name_of(cands[0]) for x in cands):
                out += cands
                continue
            raise AmbiguousName(r)
        if not out:
            raise QueryError(f"none of {', '.join(texts)} is used in this project")
        return list(dict.fromkeys(out))

    def generated_path(self, path: str) -> bool:
        return _is_generated(path)

    def add(self, op: Op) -> Op:
        kept = []
        for s in op.sites:
            if self.generated_path(s.source):
                self.warnings.append(f"{s.source}:{s.line + 1}: generated code, not edited "
                                     f"({op.label})")
                continue
            kept.append(s)
        op.sites = kept
        self.ops.append(op)
        return op

    def by_name(self, names: list[str], label: str, extra_globs: tuple = (),
                near: set[str] | None = None, quoted: bool = False,
                java_only: bool = False) -> None:
        """Uses by name the index does not see: strings in .java, other text files. With
        `near`, .java hits only count in those files (the ones using the owner type); with
        `quoted`, a hit in prose (.feature, .md, .txt) only counts between quotes."""
        globs = list(dict.fromkeys(list(self.st.by_name_globs) + list(extra_globs)))
        if "cucumber" in getattr(self.st, "profiles", []) or \
                getattr(self.st, "cucumber_run", ""):
            globs.append("*.feature")
        if java_only:
            globs = ["*.java"]
        hits = X.uses_by_name(self.root, names, globs, set(), limit=MAX_REVIEW)
        if len(hits) >= MAX_REVIEW:
            self.notes.append(f"uses by name of {', '.join(names)}: more than {MAX_REVIEW}; "
                              "the review list stops there")
        prose = (".feature", ".md", ".txt", ".adoc")
        alts = "|".join(re.escape(n) for n in names)
        # In prose: between quotes, or a table cell (| field |: fixtures read by reflection).
        quotes = re.compile("[\"'`](" + alts + ")[\"'`]|\\|\\s*(" + alts + ")\\s*\\|")
        for h in hits:
            if any(part.startswith(".") for part in h.source.split("/")[:-1]):
                continue
            if near is not None and h.source.endswith(".java") and h.source not in near:
                continue
            if quoted and h.source.endswith(prose) and not quotes.search(h.text):
                continue
            if quoted and h.source.endswith(".java") and not _code_like(h.text, names):
                continue
            self.review.append((h.source, h.line, h.text, label))

    def users_of(self, type_symbol: str) -> set[str]:
        """Files that use a type (or define it)."""
        out = set()
        d = self.nav.definition(type_symbol)
        if d is not None:
            out.add(d.location.source)
        try:
            out |= {r.location.source for r in self.nav.references(type_symbol)[0]}
        except QueryError:
            pass
        return out

    # --- macros -----------------------------------------------------------------------------

    def expand(self, plan: dict) -> None:
        self.move_to: dict[str, str] = {}      # every type the plan moves -> its package
        self.move_steps: dict[str, int] = {}
        self.move_migration = False
        for k, step in enumerate(plan["steps"], 1):
            macro = next((m for m in MACROS if m in step), None)
            try:
                if macro is None:
                    self.primitive(k, step)
                else:
                    getattr(self, "m_" + macro)(k, step[macro])
            except AmbiguousName:
                raise
            except PlanError as e:
                raise PlanError(f"step {k}: {e}") from e
            except QueryError as e:
                self.errors.append(f"step {k}: {e}")
        # All moves at once: each file is fixed against where every moved type ends up.
        if self.move_to:
            try:
                _plan_move(self, min(self.move_steps.values()), self.move_to,
                           self.move_migration)
            except QueryError as e:
                self.errors.append(str(e))

    def m_create(self, k: int, a: dict) -> None:
        path, tpl = a.get("path"), a.get("template")
        if not isinstance(path, str) or not path.endswith(".java") or os.path.isabs(path) \
                or ".." in path.split("/"):
            raise PlanError("create: path must be a relative .java path inside the checkout")
        if not isinstance(tpl, str) or not tpl.strip():
            raise PlanError("create: template (the file's text) is required")
        if os.path.exists(os.path.join(self.root, path)) or path in self.creates:
            self.errors.append(f"step {k}: {path} already exists")
            return
        self.creates[path] = tpl if tpl.endswith("\n") else tpl + "\n"
        self.summary.append(f"step {k} create {path}")

    def _new_name(self, a: dict, key: str = "to") -> str:
        new = a.get(key)
        if not isinstance(new, str) or not IDENT.fullmatch(new) or new in KEYWORDS:
            raise PlanError(f"{key}: a Java identifier is required (got {new!r})")
        return new

    def m_rename(self, k: int, a: dict) -> None:
        sym = self.resolve(a.get("symbol"))
        new = self._new_name(a)
        kind = S.kind_of(sym)
        if self.nav.definition(sym) is None:
            raise QueryError(f"{S.short(sym)} is not defined in this project (a library "
                             "symbol cannot be renamed)")
        if kind == "method":
            self._rename_method(k, sym, new, a)
        elif kind == "term":
            self._rename_field(k, sym, new, a)
        elif kind == "type":
            self._rename_type(k, sym, new, a)
        elif kind == "constructor":
            raise PlanError("rename the type (its constructors follow)")
        else:
            raise PlanError(f"cannot rename a {kind or 'symbol'} ({sym}); packages: move")

    def _family(self, sym: str, k: int) -> list:
        fam = X.family(self.nav, sym)
        lib = [m for m in fam if m.definition is None]
        if lib:
            raise QueryError(f"{S.short(sym)} overrides or is overridden by a library method "
                             f"({', '.join(S.short(m.symbol) for m in lib[:3])}): renaming it "
                             "breaks the override")
        return fam

    def _check_member_clash(self, k: int, owner: str, new: str, kind: str, arity: int) -> None:
        """A member named `new` in the type, its supertypes (library ones too) or subtypes."""
        found = self.members_named(owner, subtypes=True)
        key = (k, new, kind)
        if key in self._clashes:
            return
        for mk, ar in found.get(new, []):
            if kind == "field" and mk == "field":
                self._clashes.add(key)
                self.errors.append(f"step {k}: {S.short(owner)} or a type of its hierarchy "
                                   f"already has a field `{new}`")
                return
            if kind == "method" and mk == "method":
                if ar == arity or ar < 0 or arity < 0:
                    self._clashes.add(key)
                    self.errors.append(f"step {k}: {S.short(owner)} or a type of its hierarchy "
                                       f"already has a method `{new}` with {ar if ar >= 0 else 'some'}"
                                       " parameters: it would override or clash")
                    return
                self.warnings.append(f"step {k}: `{new}` becomes an overload of an existing "
                                     "method: check calls with null or lambdas")

    def _rename_method(self, k: int, sym: str, new: str, a: dict) -> None:
        d = self.nav.definition(sym)
        if d is not None and d.generated:
            raise QueryError(f"{S.short(sym)} is generated (Lombok?): rename what generates it")
        fam = self._family(sym, k) if a.get("family", True) else []
        members = [m.symbol for m in fam] or [sym]
        old = S.name_of(sym)
        sites: list[Site] = []
        for m in fam or []:
            why = "" if m.symbol == sym else "family"
            if S.is_local(m.symbol):   # an override in an anonymous class: one file only
                if m.definition is not None:
                    sites += self.local_sites(m.symbol, m.definition.location.source, old)
                continue
            # family() already found each member's definition and uses: no grep again.
            defs = [m.definition] if m.definition is not None else []
            try:        # the same class in several modules or source sets: each copy
                defs = self.nav.references(m.symbol)[1] or defs
            except QueryError:
                pass
            for dd in defs:
                if dd.generated:
                    continue
                r = dd.location.range
                sites.append(Site(dd.location.source, r.start_line, r.start_char,
                                  old, m.symbol, "definition", why=why))
            for ref in m.uses:
                r = ref.location.range
                sites.append(Site(ref.location.source, r.start_line, r.start_char, old,
                                  m.symbol, "reference", why=why))
        if not fam:
            sites = self.sites_of(sym)
        arity = _arity_of(d) if d else -1
        for m in members:
            if not S.is_local(m):
                self._check_member_clash(k, S.parent(m), new, "method", arity)
        members = [m for m in members if not S.is_local(m)]
        op = self.add(Op(k, f"rename {S.short(sym)} → {new}", "rename", {"to": new},
                         sites, expect=new))
        op.args["old"] = old
        self._javadoc(k, members, old, new)
        self._static_imports(k, sym, old, new)
        self.by_name([old], f"named like {old}")
        props = X.property_names(sym)[1:]
        if props:
            self.by_name(props, f"the property of {old}", near=self.users_of(S.parent(sym)),
                         quoted=True)
        self.summary.append(f"step {k} rename {S.short(sym)} → {new}"
                            + (f" (and {len(members) - 1} in its override family)"
                               if len(members) > 1 else ""))

    def _javadoc(self, k: int, members: list[str], old: str, new: str) -> None:
        files = set()
        if len(members) > 1:   # a family: one search for the name, then the owners
            owners = {S.name_of(S.parent(m)) for m in members} | {""}
            for source, _line, text in X._grep_words(self.root, [old], ["*.java"]):
                for m in X._JAVADOC_TAG.finditer(text):
                    target, _, mem = m.group(2).partition("#")
                    if mem.split("(")[0] == old and target.rsplit(".", 1)[-1] in owners:
                        files.add(source)
                        break
        else:
            for m in members:
                for h in X.javadoc_mentions(self.root, m):
                    files.add(h.source)
        if not files:
            return
        args = {"old": old, "new": new, "members": members, "params": [], "kept": False,
                "owner_files": []}
        if members and S.is_method(members[0]):
            # Which parameter lists are renamed ({@link #m(String)} names one overload), and
            # whether an overload keeps the old name (then a bare {@link #m} stays).
            for m in members:
                d = self.nav.definition(m)
                if d is not None:
                    args["params"].append(_param_types(d.info.signature if d.info else ""))
                    args["owner_files"].append(d.location.source)
            try:
                renamed = set(members)
                args["kept"] = any(o.symbol not in renamed for m in members[:1]
                                   for o in X.overloads(self.nav, m))
            except QueryError:
                pass
        self.add(Op(k, f"javadoc links to {old}", "javadoc", args, files=sorted(files)))

    def _static_imports(self, k: int, sym: str, old: str, new: str) -> None:
        """`import static Owner.old;`: the index has no occurrence of the member there."""
        owner = S.parent(sym)
        try:
            files = {r.location.source for r in self.nav.references(owner)[0]}
        except QueryError:
            return
        want = re.compile(r"import\s+static\s+" + re.escape(_qualified(owner)).replace(
            r"\.", r"\s*\.\s*") + r"\s*\.\s*" + re.escape(old) + r"\s*;")
        found = set()
        for path in files:
            try:
                with open(os.path.join(self.root, path), encoding="utf-8",
                          errors="replace") as fh:
                    if want.search(fh.read(65536)):
                        found.add(path)
            except OSError:
                continue
        files = found
        if files:
            self.add(Op(k, f"static imports of {old}", "static_import",
                        {"owner": _qualified(owner), "old": old, "new": new},
                        files=sorted(files)))

    def _rename_field(self, k: int, sym: str, new: str, a: dict) -> None:
        old = S.name_of(sym)
        owner = S.parent(sym)
        d = self.nav.definition(sym)
        self._check_member_clash(k, owner, new, "field", -1)
        sites = self.sites_of(sym)
        op = self.add(Op(k, f"rename {S.short(sym)} → {new}", "rename",
                         {"to": new, "old": old, "field": True}, sites, expect=new))
        # The declaration: persistence (a column named after the field) and its annotations.
        if d is not None:
            r = d.location.range
            insp = self.add(Op(k, f"declaration of {S.short(sym)}", "inspect",
                               {"field": sym, "new": new, "old": old,
                                "keep_column": a.get("keep_column")},
                               [Site(d.location.source, r.start_line, r.start_char, old, sym,
                                     "definition")], node="declaration"))
            insp.hook = _persistence_hook
        # Accessors: Lombok's (calls only) and a record's implicit one always follow; a
        # hand-written one too when Lombok is in play (it would generate the new one next to
        # it) or when the plan asks ("accessors": true).
        lombok = self._lombok(owner, d)
        want = a.get("accessors")
        record_acc = X.record_accessor(self.nav, sym, [d]) if d is not None else ""
        renames = _accessor_renames(old, new)
        for acc in accessor_symbols(sym):
            acc_name = S.name_of(acc)
            target = renames.get(acc_name)
            if target is None:
                continue
            refs, defs = self.nav.references(acc)
            if not refs and not defs:
                continue
            generated = bool(defs) and all(x.generated for x in defs)
            hand = [x for x in defs if not x.generated]
            if not (generated or acc == record_acc or (hand and (lombok or want))):
                if hand and want is None:
                    self.notes.append(f"step {k}: {S.short(acc)} is written by hand and kept; "
                                      'pass "accessors": true to rename it too')
                continue
            fam_syms = [acc]
            try:
                fam = X.family(self.nav, acc)
                lib = [m for m in fam if m.definition is None]
                if lib:
                    self.errors.append(f"step {k}: {S.short(acc)} implements or overrides a "
                                       f"library method ({S.short(lib[0].symbol)}): renaming "
                                       f"the field renames it")
                    continue
                fam_syms = [m.symbol for m in fam] or [acc]
                if len(fam_syms) > 1:
                    self.notes.append(f"step {k}: {S.short(acc)} is part of an override family "
                                      f"({len(fam_syms)} methods): all renamed to {target}")
            except QueryError:
                pass
            asites = []
            for fs_ in fam_syms:
                asites += self.sites_of(fs_, why=f"accessor {acc_name}")
            self.add(Op(k, f"rename accessor {acc_name} → {target}", "rename",
                        {"to": target, "old": acc_name}, asites, expect=target))
            self._check_member_clash(k, owner, target, "method",
                                     1 if acc_name.startswith("set") else 0)
        self._javadoc(k, [sym], old, new)
        self._static_imports(k, sym, old, new)
        persistent = bool({_ann_name(x) for x in _type_annotations(self, owner)}
                          & set(PERSISTENT_TYPES))
        private = d is not None and d.info is not None and \
            re.search(r"\bprivate\b", d.info.signature or "") is not None
        label = f"named like the field {old}"
        if persistent:
            # Queries and test fixtures name a persistent field through relations
            # ("owner.field") and by reflection: anywhere, not only near the class.
            self.by_name(X.property_names(sym), label, quoted=True)
            upper = old.upper()
            if upper != old and not a.get("keep_column"):
                self.by_name([upper], f"a column named after {old}?", extra_globs=("*.sql",))
        elif private:   # only its own class can name it: its file, no prose
            self.by_name(X.property_names(sym), label, quoted=True, java_only=True,
                         near={d.location.source} if d is not None else set())
        else:
            self.by_name(X.property_names(sym), label, near=self.users_of(owner), quoted=True)
        self.summary.append(f"step {k} rename field {S.short(sym)} → {new}")

    def _lombok(self, owner: str, d) -> bool:
        """Lombok accessors on the class or the field (@Getter, @Setter, @Data, @Value)."""
        want = {"Getter", "Setter", "Data", "Value"}
        if {_ann_name(x) for x in _type_annotations(self, owner)} & want:
            return True
        if d is None:
            return False
        fs = self.file(d.location.source)
        k = d.location.range.start_line
        chunk = []
        for j in range(k, max(-1, k - 12), -1):
            ln = fs.text.line(j)
            if j < k and ln.rstrip().endswith((";", "{", "}")):
                break
            chunk.append(ln if j < k else ln[:d.location.range.start_char])
        return bool({_ann_name(m.group(0)) for m in re.finditer(r"@\s*[\w$.]+",
                                                                "\n".join(chunk))} & want)

    def _rename_type(self, k: int, sym: str, new: str, a: dict) -> None:
        d = self.nav.definition(sym)
        old = S.name_of(sym)
        parent = S.parent(sym)
        clash = parent + S.format_descriptors([S.Descriptor(new, S.TYPE)])
        if self.nav.definition(clash) is not None:
            self.errors.append(f"step {k}: {S.short(clash)} already exists")
        sites = self.sites_of(sym)
        for m in X._own_members(self.nav, sym, d):
            if S.kind_of(m.symbol) == "constructor":
                sites += self.sites_of(m.symbol, why="constructor")
        self.add(Op(k, f"rename {S.short(sym)} → {new}", "rename", {"to": new, "old": old},
                    sites, expect=new))
        src = d.location.source
        if "#" not in S.parent(sym) and os.path.basename(src) == old + ".java":
            target = os.path.join(os.path.dirname(src), new + ".java").replace(os.sep, "/")
            if os.path.exists(os.path.join(self.root, target)):
                self.errors.append(f"step {k}: {target} already exists")
            self.moves[src] = target
        r = d.location.range
        insp = self.add(Op(k, f"declaration of {S.short(sym)}", "inspect",
                           {"type": sym, "migration": a.get("migration", False)},
                           [Site(src, r.start_line, r.start_char, old, sym, "definition")],
                           node="declaration"))
        insp.hook = _type_persistence_hook
        self._javadoc(k, [sym], old, new)
        self.by_name([old, S.package_of(sym) + "." + old], f"named like {old}",
                     extra_globs=("*.xml", "*.properties", "*.yml", "*.yaml"))
        self.summary.append(f"step {k} rename type {S.short(sym)} → {new}"
                            + (f" (file → {self.moves[src]})" if src in self.moves else ""))

    def m_replace_calls(self, k: int, a: dict) -> None:
        syms = self.resolve_many(a)
        sym = syms[0]
        tpl = a.get("template")
        if not isinstance(tpl, str) or not tpl.strip():
            raise PlanError("replace_calls: template is required, e.g. \"Util.run($recv, $0)\"")
        members = []
        for s_ in syms:
            if a.get("family") and S.kind_of(s_) == "method":
                members += [m.symbol for m in X.family(self.nav, s_, self.library_family)
                            if not S.is_local(m.symbol)] or [s_]
            else:
                members.append(s_)
        sites = []
        for m in dict.fromkeys(members):
            sites += self.sites_of(m, role="references")
        self.add(Op(k, f"replace calls of {S.short(sym)}" + (f" ({len(syms)} overloads)"
                                                             if len(syms) > 1 else ""),
                    "replace", {"template": tpl}, sites, node="call", imports=_imports(a)))
        self.summary.append(f"step {k} replace calls of {S.short(sym)}"
                            + (f" ({len(syms)} overloads)" if len(syms) > 1 else "")
                            + f" with {tpl[:60]!r}")

    def m_annotate(self, k: int, a: dict) -> None:
        syms = a.get("symbols") or ([a["symbol"]] if a.get("symbol") else [])
        if not syms or not isinstance(syms, list):
            raise PlanError("annotate: symbols (a list) is required")
        if not a.get("add") and not a.get("remove"):
            raise PlanError("annotate: add (\"@X(…)\") or remove (\"X\") is required")
        sites = []
        for s in syms:
            sym = self.resolve(s)
            sites += self.sites_of(sym, role="definition")
        if a.get("add"):
            self.add(Op(k, f"annotate with {a['add']}", "add_annotation",
                        {"text": a["add"]}, sites, node="declaration", imports=_imports(a)))
        if a.get("remove"):
            self.add(Op(k, f"remove @{a['remove']}", "remove_annotation", {"name": a["remove"]},
                        list(sites), node="declaration"))
        self.summary.append(f"step {k} annotate {len(syms)} declarations")

    def m_change_signature(self, k: int, a: dict) -> None:
        sym = self.resolve(a.get("symbol"))
        if not S.is_method(sym):
            raise PlanError("change_signature: symbol must be a method or constructor")
        params = a.get("parameters")
        if not isinstance(params, list):
            raise PlanError('change_signature: parameters is the new list, e.g. [{"from": 0}, '
                            '{"add": "Reason reason", "default": "Reason.MANUAL"}]')
        for p in params:
            if not isinstance(p, dict) or not (("from" in p and isinstance(p["from"], int))
                                               or isinstance(p.get("add"), str)):
                raise PlanError(f"change_signature: each parameter is {{\"from\": i}} or "
                                f"{{\"add\": \"Type name\", \"default\": \"expr\"}}, got {p!r}")
            if "add" in p and not isinstance(p.get("default"), str) and not a.get("no_calls"):
                raise PlanError(f"change_signature: {p['add']!r} needs a default (the "
                                "argument passed at every call)")
        members = [sym]
        if S.kind_of(sym) == "method" and a.get("family", True):
            members = [m.symbol for m in self._family(sym, k)] or [sym]
        decls, calls = [], []
        for m in members:
            decls += self.sites_of(m, role="definition")
            calls += self.sites_of(m, role="references")
        d = self.nav.definition(sym)
        if d is None:
            raise QueryError(f"{S.short(sym)} is not defined in this project")
        sig = (d.info.signature if d.info else "") or ""
        types = _param_types(sig)
        # scip-javac writes varargs as an array: the source says whether it is one.
        varargs = bool(types) and self._declares_varargs(d)
        args = {"parameters": params, "old_arity": len(types), "varargs": varargs}
        for p_ in params:
            if "from" in p_ and not 0 <= p_["from"] < len(types):
                raise PlanError(f"change_signature: {S.short(sym)} has {len(types)} "
                                f"parameters, no {p_['from']}")
        if varargs and params and \
                not ("from" in params[-1] and params[-1]["from"] == len(types) - 1) \
                and any(x.get("from") == len(types) - 1 for x in params):
            raise PlanError("change_signature: the varargs parameter must stay last")
        try:
            same = [m for m in X.overloads(self.nav, sym) if m.symbol != sym
                    and _arity_of(m.definition) == len(params)]
        except QueryError:
            same = []
        if same:
            args["_overloads"] = True
            self.warnings.append(f"step {k}: {S.short(same[0].symbol)} has the same number of "
                                 "parameters as the new signature: calls with null or lambdas "
                                 "may become ambiguous")
        given = _imports(a)
        decl_imports, call_imports = list(given), list(given)
        if not given:   # import what the new parameters and defaults name, when it is clear
            decl_files = sorted({s_.source for s_ in decls})
            call_files = sorted({s_.source for s_ in calls})
            for p_ in params:
                if "add" in p_:
                    decl_imports += self._import_for(k, p_["add"].split()[0] if
                                                     p_["add"].split() else "", decl_files)
                    m_ = re.match(r"([A-Z][\w$]*)\s*\.", p_.get("default", ""))
                    if m_:
                        call_imports += self._import_for(k, m_.group(1), call_files)
        self.add(Op(k, f"change signature of {S.short(sym)} (declarations)", "signature_decl",
                    args, decls, node="declaration", imports=decl_imports))
        call_op = self.add(Op(k, f"change signature of {S.short(sym)} (calls)", "signature_call",
                              args, calls, node="call", imports=call_imports))
        call_op.args["method"] = sym
        self.summary.append(f"step {k} change signature of {S.short(sym)}"
                            + (f" (and {len(members) - 1} in its override family)"
                               if len(members) > 1 else ""))

    def _declares_varargs(self, d) -> bool:
        from .javasrc import mask
        try:
            fs = self.file(d.location.source)
        except OSError:
            return False
        r = d.location.range
        start = fs.text.scip(r.start_line, r.start_char)
        masked = mask(fs.text.text[start:start + 4000])
        i = masked.find("(")
        depth = 0
        for k in range(max(i, 0), len(masked)):
            if masked[k] == "(":
                depth += 1
            elif masked[k] == ")":
                depth -= 1
                if depth == 0:
                    return "..." in masked[i:k]
        return False

    def _import_for(self, k: int, type_text: str, files: list[str]) -> list[str]:
        """The import a simple type name in a template needs: [] when it needs none (a
        primitive, java.lang, qualified) or cannot be told (then a warning). What the
        touched files already import says it first; else the index."""
        base = re.sub(r"<.*", "", type_text).replace("...", "").replace("[]", "").strip()
        if not base or "." in base or base in ("int", "long", "short", "byte", "char",
                                               "boolean", "float", "double", "void", "var"):
            return []
        rx = re.compile(r"^\s*import\s+([\w$.]+\." + re.escape(base) + r")\s*;", re.M)
        for f in files[:50]:
            try:
                m = rx.search(self.file(f).text.text)
            except OSError:
                continue
            if m:
                return [m.group(1)]
        syms = [x for x in self.types_named(base) if "#" not in S.parent(x)]
        if len(syms) > 1:   # prefer the project's own types and java.lang
            own = [x for x in syms if self.nav.definition(x) is not None]
            syms = own if len(own) == 1 else syms
        if len(syms) == 1:
            fq = _qualified(syms[0])
            return [] if fq.startswith("java.lang.") and fq.count(".") == 2 else [fq]
        if len(syms) > 1:
            self.warnings.append(f"step {k}: {base} is ambiguous ({', '.join(_qualified(x) for x in syms[:3])}): "
                                 'say which with "imports"')
        return []

    def m_move(self, k: int, a: dict) -> None:
        to = a.get("to")
        if not isinstance(to, str) or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", to):
            raise PlanError("move: to is the target package, e.g. \"com.example.billing\"")
        types: list[str] = []
        if a.get("package"):
            pkg = a["package"]
            types = _package_types(self.nav, pkg)
            if not types:
                raise QueryError(f"no top-level types of package {pkg} in this project")
        else:
            sym = self.resolve(a.get("symbol"))
            if not S.is_type(sym) or "#" in S.parent(sym):
                raise PlanError("move: symbol must be a top-level type (or pass package)")
            types = [sym]
        for t in types:
            if t in self.move_to:
                raise PlanError(f"{S.short(t)} is moved twice")
            self.move_to[t] = to
            self.move_steps[t] = k
        self.move_migration |= bool(a.get("migration"))

    # --- primitives -------------------------------------------------------------------------

    def primitive(self, k: int, step: dict) -> None:
        sel, do = step["select"], step["do"]
        if not isinstance(sel, dict):
            raise PlanError("select takes an object")
        action = next(a for a in ACTIONS if a in do)
        value = do[action]
        args = value if isinstance(value, dict) else {"value": value}
        node = sel.get("node", "name" if action == "rename" else _default_node(action))
        if not isinstance(node, str) or not re.fullmatch(
                r"name|expr|call|statement|declaration|argument\[\d+\]|parameter\[\d+\]|"
                r"ancestor:\w+", node):
            raise PlanError(f"node {node!r}: one of name, expr, call, statement, declaration, "
                            "argument[i], parameter[i], ancestor:<JavaParser class>")
        where = sel.get("where")
        if isinstance(where, str):
            where = {"pattern": where, "kind": "expression"}
        where_types = _where_types(where)
        if where_types:
            where = {k_: v for k_, v in where.items() if k_ != "types"}
        op = Op(k, f"{action} ({json.dumps(sel)[:80]})", action, args, node=node, where=where,
                imports=_imports(step), where_types=where_types)
        if where and not where_types and sel.get("kind") and \
                re.search(r"\$\w+\s*\.\s*\w+\s*\(", str(where.get("pattern", ""))):
            self.notes.append(f"step {k}: where matches the code's shape only; its captures "
                              "are not checked by type: a call on $x may be another type's "
                              "method of that name (add \"types\": {\"$x\": "
                              "\"java.util.Collection\"} to where)")
        if sel.get("symbol") or sel.get("symbols"):
            role = sel.get("role", "all")
            if role not in ("all", "definition", "references"):
                raise PlanError("role: all, definition or references")
            members: list[str] = []
            for sym in self.resolve_many(sel):
                if S.kind_of(sym) == "method" and sel.get("family", action == "rename"):
                    if action == "rename":
                        fam = [m.symbol for m in self._family(sym, k)]
                    else:   # a call through a library supertype is still a call: keep it
                        fam = [m.symbol for m in X.family(self.nav, sym, self.library_family)]
                    members += [m for m in fam if not S.is_local(m)] or [sym]
                else:
                    members.append(sym)
                if S.kind_of(sym) == "term" and sel.get("accessors", action == "rename"):
                    if action == "rename":
                        raise PlanError("renaming a field with its accessors: use the rename "
                                        "macro")
                    members += [x for x in accessor_symbols(sym) if self.nav.references(x)[0]]
            for m in dict.fromkeys(members):
                op.sites += self.sites_of(m, role)
            files = sel.get("files")
            if files:
                globs = [files] if isinstance(files, str) else list(files)
                op.sites = [s for s in op.sites if any(fnmatch.fnmatch(s.source, g)
                                                       for g in globs)]
            if sel.get("in"):
                scope = self.resolve(sel["in"], "in")
                op.sites = [s for s in op.sites if self._inside(s, scope)]
        elif sel.get("kind"):
            if sel.get("in"):
                scope = self.resolve(sel["in"], "in")
                d = self.nav.definition(scope)
                if d is None:
                    raise QueryError(f"{S.short(scope)} is not defined in this project")
                r = d.location.range
                op.scopes.append({"source": d.location.source, "line": r.start_line,
                                  "col": r.start_char, "name": self.ident_name(scope),
                                  "kind": sel["kind"]})
            elif sel.get("files"):
                globs = [sel["files"]] if isinstance(sel["files"], str) else sel["files"]
                for path in _java_files(self.root, globs):
                    op.scopes.append({"source": path, "kind": sel["kind"]})
            else:
                raise PlanError("select by kind needs in (a type or method) or files (globs)")
        elif sel.get("files") and action in ("add_import", "remove_import"):
            globs = [sel["files"]] if isinstance(sel["files"], str) else sel["files"]
            op.files = _java_files(self.root, globs)
        else:
            raise PlanError("select needs symbol, or kind with in/files")
        if action == "rename":
            op.args["to"] = self._new_name({"to": args.get("value", args.get("to"))})
            op.expect = op.args["to"]
        self.add(op)
        self.summary.append(f"step {k} {action}: {len(op.sites)} sites"
                            + (f", {len(op.scopes)} scopes" if op.scopes else ""))

    def _inside(self, site: Site, scope: str) -> bool:
        d = self.nav.definition(scope)
        if d is None or d.location.source != site.source:
            return False
        enc = d.enclosing
        if enc is None:
            return S.owner_type(site.symbol).startswith(scope) if S.is_type(scope) else False
        return (enc.start_line, enc.start_char) <= (site.line, site.col) <= \
            (enc.end_line, enc.end_char)


_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def _code_like(line: str, names: list[str]) -> bool:
    """A name inside a string literal used as code, not prose: the whole literal, a path
    segment (owner.field), a comparison (field == :p), an element of a list."""
    alts = "|".join(re.escape(n) for n in names)
    rx = re.compile(rf"^\s*({alts})\s*$|\.({alts})\b|\b({alts})\.|\b({alts})\s*(==|!=|<=|>=|<|>"
                    rf"|=|\bin\b)|(^|[,;(])\s*({alts})\s*([,;)]|$)")
    return any(rx.search(m.group(1)) for m in _LITERAL.finditer(line))


def _qualified(type_symbol: str) -> str:
    """com/example/Outer#Inner# -> com.example.Outer.Inner"""
    return ".".join(d.name for d in S.parse(type_symbol) if d.suffix in (S.NAMESPACE, S.TYPE))


def _default_node(action: str) -> str:
    return {"replace": "expr", "wrap": "statement", "remove": "statement",
            "insert_before": "statement", "insert_after": "statement",
            "add_argument": "call", "remove_argument": "call", "reorder_arguments": "call",
            "add_parameter": "declaration", "remove_parameter": "declaration",
            "reorder_parameters": "declaration", "add_annotation": "declaration",
            "remove_annotation": "declaration", "set_modifiers": "declaration",
            "add_member": "declaration", "add_import": "name",
            "remove_import": "name"}.get(action, "name")


def _imports(holder: dict) -> list[str]:
    imp = holder.get("imports") or []
    if isinstance(imp, str):
        imp = [imp]
    out = []
    for i in imp:
        if not isinstance(i, str) or not re.fullmatch(
                r"(static\s+)?[A-Za-z_][\w$]*(\.[A-Za-z_$][\w$]*)*(\.\*)?", i.strip()):
            raise PlanError(f"imports: {i!r} is not a qualified name")
        out.append(i.strip())
    return out


def _arity_of(d) -> int:
    return _sig_arity((d.info.signature if d is not None and d.info else "") or "")


def _sig_arity(sig: str) -> int:
    m = re.search(r"\((.*)\)", sig, re.S)
    if not m:
        return -1
    inner = m.group(1).strip()
    if not inner:
        return 0
    depth, n = 0, 1
    for c in inner:
        if c in "<([":
            depth += 1
        elif c in ">)]":
            depth -= 1
        elif c == "," and depth == 0:
            n += 1
    return n


def _accessor_renames(old: str, new: str) -> dict[str, str]:
    cap_o, cap_n = old[:1].upper() + old[1:], new[:1].upper() + new[1:]
    out = {f"get{cap_o}": f"get{cap_n}", f"is{cap_o}": f"is{cap_n}",
           f"set{cap_o}": f"set{cap_n}", old: new}
    if re.match(r"is[A-Z]", old):
        out[f"set{old[2:]}"] = f"set{new[2:]}" if re.match(r"is[A-Z]", new) else f"set{cap_n}"
    return out


def _is_generated(path: str) -> bool:
    """Build output: a build/target/out/bin folder above the source root (`src`), or a
    generated* folder anywhere. A package named `build` under src/main/java is source."""
    parts = path.split("/")[:-1]
    if any(p.startswith("generated") for p in parts):
        return True
    for p in parts:
        if p == "src":
            return False
        if p in GENERATED_DIRS:
            return True
    return False


def _java_files(root: str, globs: list[str]) -> list[str]:
    out = []
    for d, dirs, names in os.walk(root):
        rel_d = os.path.relpath(d, root).replace(os.sep, "/")
        dirs[:] = [x for x in dirs if not x.startswith(".")
                   and not _is_generated(("" if rel_d == "." else rel_d + "/") + x + "/f")]
        for n in names:
            if n.endswith(".java"):
                rel = os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/")
                if any(fnmatch.fnmatch(rel, g) for g in globs):
                    out.append(rel)
        if len(out) > 5000:
            break
    return sorted(out)


def _package_types(nav: Navigator, pkg: str) -> list[str]:
    prefix = pkg.replace(".", "/") + "/"
    out = set()
    for shard in nav.ws.all_shards():
        src = shard.source
        if not src.endswith(".java"):
            continue
        for v in nav._views([shard]):
            for d in v.definitions(lambda sym, _i: sym.startswith(prefix) and S.is_type(sym)
                                   and S.parent(sym) == prefix and not S.is_local_like(sym)):
                out.add(d.symbol)
    return sorted(out)


# --- hooks after analyze -----------------------------------------------------------------------

def _annotations_text(fs: FileState, node: dict) -> list[str]:
    out = []
    for a in node.get("parts", {}).get("annotations", []) or []:
        s, e = fs.text.span(a["range"])
        out.append(fs.text.text[s:e])
    return out


def _modifiers_text(fs: FileState, node: dict) -> list[str]:
    out = []
    for m in node.get("parts", {}).get("modifiers", []) or []:
        s, e = fs.text.span(m["range"])
        out.append(fs.text.text[s:e].strip())
    return out


def _ann_name(text: str) -> str:
    m = re.match(r"@\s*([\w$.]+)", text)
    return m.group(1).rsplit(".", 1)[-1] if m else ""


def _type_annotations(p: Planner, type_symbol: str) -> list[str]:
    """Annotation texts on a type's declaration, read from its source header."""
    d = p.nav.definition(type_symbol)
    if d is None:
        return []
    fs = p.file(d.location.source)
    k = d.location.range.start_line
    lines = []
    for j in range(k, max(-1, k - 40), -1):
        ln = fs.text.line(j)
        if j < k and (ln.rstrip().endswith((";", "}")) or not ln.strip() and lines and
                      not lines[-1].strip().startswith("@")):
            break
        lines.append(ln)
    header = "\n".join(reversed(lines))
    return [m.group(0) for m in re.finditer(r"@\s*[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?",
                                            header)]


def _persistence_hook(p: Planner, op: Op) -> None:
    a = op.args
    for loc in op.found:
        fs = p.file(loc.site.source)
        node = loc.node
        anns = _annotations_text(fs, node)
        mods = _modifiers_text(fs, node)
        names = {_ann_name(x) for x in anns}
        owner = S.parent(a["field"])
        owner_anns = {_ann_name(x) for x in _type_annotations(p, owner)}
        persistent_class = owner_anns & set(PERSISTENT_TYPES)
        if not persistent_class or "static" in mods or "transient" in mods \
                or names & {"NotPersistent", "Transient"}:
            continue
        col = next((x for x in anns if _ann_name(x) == "Column"), "")
        explicit = re.search(r"\bname\s*=\s*\"", col)
        if explicit:
            continue
        keep = a.get("keep_column")
        if not keep:
            p.errors.append(
                f"step {op.step}: {S.short(a['field'])} is persistent ({', '.join(sorted(persistent_class))}) "
                "and its column is named after the field: renaming it changes the column. Pass "
                '"keep_column": "<current column name>" to add @Column(name = …), or rename '
                "the column with a migration")
            continue
        if not isinstance(keep, str) or not re.fullmatch(r"[\w$]+", keep):
            p.errors.append(f"step {op.step}: keep_column must be a column name")
            continue
        start = fs.text.span(node["range"])[0]
        base = fs.text.indent_at(start)
        if col:
            # @Column(...) without a name: add it.
            cs = next(fs.text.span(x["range"]) for x in node["parts"]["annotations"]
                      if _ann_name(fs.text.text[slice(*fs.text.span(x["range"]))]) == "Column")
            text = fs.text.text[cs[0]:cs[1]]
            new = (text[:-1] + f', name = "{keep}")' if text.rstrip().endswith(")") and "(" in
                   text and text[text.index("(") + 1:-1].strip() else f'@Column(name = "{keep}")')
            fs.edits.append(E.Edit(cs[0], cs[1], [E.Piece(new, "AnnotationExpr")], op.step,
                                   f"step {op.step} keep column {keep}"))
        else:
            fs.edits.append(E.Edit(start, start, [E.Piece(f'@Column(name = "{keep}")',
                                                          "AnnotationExpr"),
                                                  E.Piece(fs.text.eol + base)],
                                   op.step, f"step {op.step} keep column {keep}"))
            pkg = _column_package(fs)
            if pkg:
                _add_import(p, fs, f"{pkg}.Column", op.step)
            else:
                p.warnings.append(f"step {op.step}: import Column in {fs.source.path} "
                                  "(its package could not be told from the imports)")
    # Strings of persistence annotations that name the field (fetch groups, mappedBy…), in
    # the owner's file and in the files that use the owner type.
    _rewrite_field_strings(p, op)


def _column_package(fs: FileState) -> str:
    for i in fs.unit.get("imports", []):
        name = i["name"]
        for pkg in ("javax.jdo.annotations", "jakarta.jdo.annotations", "javax.persistence",
                    "jakarta.persistence"):
            if name.startswith(pkg + ".") or name == pkg:
                return pkg
    return ""


_ANN_STRING = re.compile(r'@\s*(?:[\w$]+\.)*(\w+)\s*\(')


def _rewrite_field_strings(p: Planner, op: Op) -> None:
    a = op.args
    old, new, owner = a["old"], a["new"], S.parent(a["field"])
    files = set()
    d = p.nav.definition(owner)
    if d is not None:
        files.add(d.location.source)
    try:
        refs, _defs = p.nav.references(owner)
        files |= {r.location.source for r in refs}
    except QueryError:
        pass
    from .javasrc import mask
    for path in sorted(files):
        if p.generated_path(path) or not path.endswith(".java"):
            continue
        try:
            fs = p.file(path)
        except OSError:
            continue
        text = fs.text.text
        if f'"{old}"' not in text and f"{old}" not in text:
            continue
        masked = mask(text)
        for m in _ANN_STRING.finditer(masked):
            if m.group(1) not in JDO_FIELD_STRINGS:
                continue
            depth, end = 0, -1
            for i in range(m.end() - 1, min(len(masked), m.end() + 4000)):
                if masked[i] == "(":
                    depth += 1
                elif masked[i] == ")":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end < 0:
                continue
            for lit in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', text[m.end():end]):
                value = lit.group(1)
                s0 = m.end() + lit.start(1)
                if value == old:
                    fs.edits.append(E.Edit(s0, s0 + len(old), [E.Piece(new)], op.step,
                                           f"step {op.step} @{m.group(1)} string \"{old}\""))
                elif re.fullmatch(rf"{re.escape(old)}(\.[\w$]+)+|[\w$]+\.{re.escape(old)}"
                                  rf"(\.[\w$]+)*", value):
                    p.review.append((path, fs.text.line_of(s0) + 1, f'"{value}"',
                                     f"a path through {old} in @{m.group(1)}: check it"))


def _type_persistence_hook(p: Planner, op: Op) -> None:
    sym = op.args["type"]
    if op.args.get("migration"):
        return
    anns = _type_annotations(p, sym)
    names = {_ann_name(x) for x in anns}
    if names & {"PersistenceCapable", "Entity"}:
        table = next((x for x in anns if _ann_name(x) in ("PersistenceCapable", "Table")
                      and re.search(r"\b(table|name)\s*=", x)), "")
        if not table:
            p.errors.append(f"step {op.step}: {S.short(sym)} is persistent and its table is "
                            "named after the class: renaming or moving it changes the table "
                            '(set the table explicitly first, or pass "migration": true)')
    # A discriminator storing class names in this type's hierarchy.
    hierarchy = [sym] + [s.symbol for s in p.nav.supertypes(sym)[1] if s.definition]
    for t in hierarchy:
        for x in _type_annotations(p, t):
            if _ann_name(x) in ("Discriminator", "DiscriminatorValue") and "CLASS_NAME" in x:
                p.errors.append(f"step {op.step}: {S.short(t)} stores class names in a "
                                f"discriminator ({x[:80]}): rows of {S.short(sym)} keep the "
                                'old name (pass "migration": true when a migration updates '
                                "them)")
                return


# --- moves -------------------------------------------------------------------------------------

def _plan_move(p: Planner, k: int, move_to: dict[str, str], migration: bool) -> None:
    """Moves top-level types to other packages (one or several steps, done together)."""
    files: dict[str, str] = {}             # old path -> new path
    for t, to in move_to.items():
        d = p.nav.definition(t)
        if d is None:
            raise QueryError(f"step {p.move_steps[t]}: {S.short(t)} is not defined in this "
                             "project")
        src = d.location.source
        old_pkg = S.package_of(t)
        rel_dir = old_pkg.replace(".", "/")
        folder = os.path.dirname(src)
        if not (folder == rel_dir or folder.endswith("/" + rel_dir)):
            raise QueryError(f"{src}: its folder does not match package {old_pkg}")
        base = folder[:len(folder) - len(rel_dir)]
        target = (base + to.replace(".", "/") + "/" + os.path.basename(src)).lstrip("/")
        if os.path.exists(os.path.join(p.root, target)):
            p.errors.append(f"step {p.move_steps[t]}: {target} already exists")
        files[src] = target
        r = d.location.range
        insp = p.add(Op(p.move_steps[t], f"declaration of {S.short(t)}", "inspect",
                        {"type": t, "migration": migration},
                        [Site(src, r.start_line, r.start_char, S.name_of(t), t,
                              "definition")], node="declaration"))
        insp.hook = _type_persistence_hook
    p.moves.update(files)
    sites: list[Site] = []
    for t in move_to:
        sites += p.sites_of(t, role="references")
        for m in X._own_members(p.nav, t, p.nav.definition(t)):
            if S.kind_of(m.symbol) == "constructor":
                sites += p.sites_of(m.symbol, role="references", optional_refs=True)
    targets = sorted(set(move_to.values()))
    op = p.add(Op(k, f"move {len(move_to)} types to {', '.join(targets)}", "move",
                  {"move_to": dict(move_to), "files": files}, sites, node="name",
                  files=sorted(files)))
    op.hook = _move_hook
    for t in move_to:
        fq = S.package_of(t) + "." + S.name_of(t)
        p.by_name([fq], f"the old qualified name of {S.name_of(t)}",
                  extra_globs=("*.xml", "*.properties", "*.yml", "*.yaml", "*.feature",
                               "*.md", "*.txt", "*.json", "*.sql", "*.html"))
    names = [S.short(t) for t in move_to]
    p.summary.append(f"step {k} move {', '.join(names[:5])}" + (" …" if len(names) > 5 else "")
                     + f" to {', '.join(targets)}")


def _move_hook(p: Planner, op: Op) -> None:
    move_to: dict[str, str] = op.args["move_to"]
    files: dict[str, str] = op.args["files"]
    old_pkg = {t: S.package_of(t) for t in move_to}
    step = op.step
    # The package of each moved file, after the move.
    file_pkg: dict[str, str] = {}
    for t, to in move_to.items():
        d = p.nav.definition(t)
        if d is not None:
            file_pkg[d.location.source] = to
    by_file: dict[str, list[Located]] = {}
    for loc in op.found:
        by_file.setdefault(loc.site.source, []).append(loc)
    for path in sorted(set(by_file) | set(files)):
        fs = p.file(path)
        if not fs.unit_ok:
            continue
        unit = fs.unit
        pkg_before = unit.get("package", {}).get("name", "")
        pkg_after = file_pkg.get(path, pkg_before)
        text = fs.text.text
        imported: set[str] = set()      # moved types this file imports (by their old name)
        for imp in unit.get("imports", []):
            name, static = imp["name"], imp["static"]
            s, e = fs.text.span(imp["range"])
            for t, to in move_to.items():
                fq = old_pkg[t] + "." + S.name_of(t)
                if not (name == fq or name.startswith(fq + ".")):
                    continue
                if name == fq and not static:
                    imported.add(t)
                if not static and not imp["asterisk"] and name == fq and pkg_after == to:
                    _remove_lines(fs, s, e, step, f"step {step} import {fq} (same package "
                                                  "now)")
                else:
                    i = text.index(fq, s, e)
                    fs.edits.append(E.Edit(i, i + len(old_pkg[t]), [E.Piece(to)], step,
                                           f"step {step} import {fq}"))
                break
        needed: set[str] = set()
        for loc in by_file.get(path, []):
            sym = loc.site.symbol
            t = sym if sym in move_to else S.parent(sym) if S.parent(sym) in move_to else None
            if t is None:
                continue
            s, e = fs.text.span(loc.ident)
            q = old_pkg[t]
            if text[max(0, s - len(q) - 1):s] == q + ".":      # a qualified name
                fs.edits.append(E.Edit(s - len(q) - 1, s - 1, [E.Piece(move_to[t])], step,
                                       f"step {step} qualified {S.name_of(t)}"))
                continue
            if any(fs.text.span(i["range"])[0] <= s < fs.text.span(i["range"])[1]
                   for i in unit.get("imports", [])) or loc.qualified:
                continue
            if t not in imported and pkg_after != move_to[t]:
                needed.add(move_to[t] + "." + S.name_of(t))
        for fq in sorted(needed):
            _add_import(p, fs, fq, step, package=pkg_after)
        if path in files:
            pd = unit.get("package")
            if pd is None:
                p.errors.append(f"step {step}: {path} has no package declaration")
                continue
            s, e = fs.text.span(pd["name_range"])
            fs.edits.append(E.Edit(s, e, [E.Piece(pkg_after)], step,
                                   f"step {step} package of {os.path.basename(path)}"))
            _old_package_imports(p, fs, path, pkg_before, pkg_after, move_to, step)
    _package_private(p, op, move_to, file_pkg)


def _old_package_imports(p: Planner, fs: FileState, path: str, pkg_old: str, pkg_new: str,
                         move_to: dict[str, str], step: int) -> None:
    """A moved file used types of its old package without importing them: now it must
    (types moved too are imported where they go, by the references above)."""
    prefix = pkg_old.replace(".", "/") + "/"
    used = set()
    for v in p.nav._views(p.nav.ws.shard_for_source(path)):
        for o in v.doc.occurrences:
            sym = o.symbol
            if o.is_definition or not sym.startswith(prefix) or S.is_local(sym) \
                    or S.package_of(sym) != pkg_old:   # not a subpackage's
                continue
            top_name = S.top_level_type_name(sym)
            if not top_name:
                continue
            top = prefix + top_name + "#"
            if top in move_to:
                continue
            used.add(top)
    have = {i["name"] for i in fs.unit.get("imports", [])}
    for top in sorted(used):
        fq = pkg_old + "." + S.name_of(top)
        if fq not in have and pkg_old + ".*" not in have:
            _add_import(p, fs, fq, step, package=pkg_new)


def _visibility(d) -> str:
    sig = (d.info.signature if d is not None and d.info else "") or ""
    head = sig.split("(")[0]
    for v in ("public", "protected", "private"):
        if re.search(rf"\b{v}\b", head):
            return v
    return "package"


def _package_private(p: Planner, op: Op, move_to: dict[str, str],
                     file_pkg: dict[str, str]) -> None:
    """Package access that the move breaks: a moved type's package-private members (or the
    type itself) used from its old package (any module), and package-private members of the
    old package that a moved type uses. They will not compile."""
    found: list[str] = []
    # Members another step makes public or protected do not break.
    opened = {s_.symbol for o in p.ops if o.action == "set_modifiers"
              and {"public", "protected"} & set(o.args.get("add") or []) for s_ in o.sites}

    def pkg_of(path: str) -> tuple[str, str]:
        before = _package_of_file(p, path)
        return before, file_pkg.get(path, before)
    for t, to in move_to.items():
        d = p.nav.definition(t)
        if d is None:
            continue
        old = S.package_of(t)
        members = [(t, d)] + [(m.symbol, m) for m in X._own_members(p.nav, t, d)]
        for sym, md in members:
            if _visibility(md) != "package" or S.is_local(sym) or sym in opened:
                continue
            try:
                refs = p.nav.references(sym)[0]
            except QueryError:
                continue
            bad = {}
            for r in refs:
                before, after = pkg_of(r.location.source)
                if before == old and after != to and r.location.source != d.location.source:
                    bad.setdefault(r.location.source, r.location.line)
            if bad:
                where = ", ".join(f"{f}:{ln}" for f, ln in sorted(bad.items())[:3])
                found.append(f"{S.short(sym)} is package-private and {len(bad)} files left in "
                             f"{old} use it ({where}{' …' if len(bad) > 3 else ''})")
        # What the moved type uses of its old package that is package-private.
        prefix = old.replace(".", "/") + "/"
        seen = set()
        for v in p.nav._views(p.nav.ws.shard_for_source(d.location.source)):
            for o in v.doc.occurrences:
                sym = o.symbol
                if o.is_definition or S.is_local(sym) or not sym.startswith(prefix) \
                        or S.package_of(sym) != old or sym in seen:
                    continue
                seen.add(sym)
                top = prefix + S.top_level_type_name(sym) + "#"
                if top in move_to and move_to[top] == to:
                    continue
                owner = S.owner_type(sym)
                if owner == t or owner.startswith(t):
                    continue        # the moved type or a type nested in it
                md = p.nav.definition(sym)
                if md is not None and _visibility(md) == "package" and sym not in opened:
                    found.append(f"{S.short(t)} uses {S.short(sym)}, package-private in {old} "
                                 f"({d.location.source}:{o.range.start_line + 1})")
    for f in found[:30]:
        p.warnings.append(f"step {op.step}: WILL NOT COMPILE: {f}")


def _package_of_file(p: Planner, path: str) -> str:
    try:
        text = p.file(path).text.text
    except OSError:
        return ""
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    return m.group(1) if m else ""


# --- edits from located nodes --------------------------------------------------------------------

def _category(kind: str) -> str:
    if kind in ("SimpleName", "Name"):
        return "Identifier"
    if kind.endswith("Expr"):
        return "Expression"
    if kind.endswith("Stmt"):
        return "Statement"
    if kind.endswith("Declaration") and kind not in ("ImportDeclaration", "PackageDeclaration"):
        return "BodyDeclaration"
    if kind.endswith("Type"):
        return "Type"
    return kind


def _parse_kind(category: str) -> str:
    return {"Expression": "expression", "Statement": "statement",
            "BodyDeclaration": "member", "Parameter": "parameter", "Type": "type",
            "ImportDeclaration": "import"}.get(category, "")


def _add_import(p: Planner, fs: FileState, name: str, step: int,
                package: str | None = None) -> None:
    """Adds `import name;` unless it is there, in the file's package (`package`: the one it
    moves to) or java.lang; sorted in when the imports are sorted."""
    static = name.startswith("static ")
    fq = name[len("static "):].strip() if static else name
    unit = fs.unit
    imports = unit.get("imports", [])
    if any(i["name"] == fq and i["static"] == static for i in imports):
        return
    pkg = package if package is not None else unit.get("package", {}).get("name", "")
    head = fq.rsplit(".", 1)[0] if "." in fq else ""
    if not static and (head == pkg or head == "java.lang") and not fq.endswith(".*"):
        return
    if any(e.what == f"import {name}" for e in fs.edits):
        return
    simple = fq.rsplit(".", 1)[-1]
    if not static and simple != "*":
        other = [i["name"] for i in imports if not i["static"] and not i["asterisk"]
                 and i["name"].rsplit(".", 1)[-1] == simple and i["name"] != fq]
        if other:
            p.warnings.append(f"{fs.source.path}: {simple} is already imported from "
                              f"{other[0]}; {fq} not imported")
            return
    eol = fs.text.eol
    line = f"import {'static ' if static else ''}{fq};"
    group = [i for i in imports if i["static"] == static] or imports
    if group:
        after = [i for i in group if i["name"] > fq]
        names = [i["name"] for i in group]
        if after and names == sorted(names):
            at = fs.text.line_start(fs.text.span(after[0]["range"])[0])
            fs.edits.append(E.Edit(at, at, [E.Piece(line, "ImportDeclaration"), E.Piece(eol)],
                                   step, f"import {name}"))
        else:
            at = fs.text.line_end(fs.text.span(group[-1]["range"])[1] - 1)
            fs.edits.append(E.Edit(at, at, [E.Piece(line, "ImportDeclaration"), E.Piece(eol)],
                                   step, f"import {name}"))
        return
    pd = unit.get("package")
    if pd:
        at = fs.text.line_end(fs.text.span(pd["range"])[1] - 1)
        fs.edits.append(E.Edit(at, at, [E.Piece(eol), E.Piece(line, "ImportDeclaration"),
                                        E.Piece(eol)], step, f"import {name}"))
    else:
        fs.edits.append(E.Edit(0, 0, [E.Piece(line, "ImportDeclaration"), E.Piece(eol + eol)],
                               step, f"import {name}"))


def _remove_lines(fs: FileState, start: int, end: int, step: int, what: str,
                  comment: list | None = None) -> None:
    """Removes [start, end): whole lines when nothing else is on them (with the comment
    attached to the node when it starts on a line of its own)."""
    t = fs.text
    if comment:
        cs, _ce = t.span(comment)
        if cs < start and not t.text[t.line_start(cs):cs].strip():
            start = cs
    if t.alone_on_lines(start, end):
        start = t.line_start(start)
        end = t.line_end(max(start, end - 1))
    fs.edits.append(E.Edit(start, end, [], step, what))


def _list_info(fs: FileState, node: dict, part: str) -> tuple[list[tuple[int, int]],
                                                             int | None, int | None]:
    """(item spans, offset just after "(", offset of ")") of a node's argument or parameter
    list, from its parts and delimiter tokens."""
    t = fs.text
    items = [t.span(x["range"]) for x in node.get("parts", {}).get(part, []) or []]
    toks = [(tk["t"], t.span(tk["range"])) for tk in node.get("tokens", [])]
    name = node.get("parts", {}).get("name") or node.get("parts", {}).get("type")
    after = t.span(name["range"])[1] if name else t.span(node["range"])[0]
    open_at = close_at = None
    depth = 0
    for kind, (s, e) in toks:
        if s < after:
            continue
        if kind == "(":
            depth += 1
            if depth == 1 and open_at is None:
                open_at = e
        elif kind == ")":
            depth -= 1
            if depth == 0 and open_at is not None:
                close_at = s
                break
    return items, open_at, close_at


def _insert_item(fs: FileState, items, open_at, index: int, text: str, kind: str, step: int,
                 what: str) -> E.Edit:
    t = fs.text
    if not items:
        return E.Edit(open_at, open_at, [E.Piece(text, kind)], step, what)
    multiline = len(items) > 1 and t.line_of(items[0][0]) != t.line_of(items[-1][0])
    if index >= len(items):
        s = items[-1][1]
        sep = ("," + t.eol + t.indent_at(items[-1][0])) if multiline else ", "
        return E.Edit(s, s, [E.Piece(sep), E.Piece(text, kind)], step, what)
    s = items[index][0]
    sep = ("," + t.eol + t.indent_at(s)) if multiline else ", "
    return E.Edit(s, s, [E.Piece(text, kind), E.Piece(sep)], step, what)


def _remove_item(fs: FileState, items, index: int, step: int, what: str) -> E.Edit:
    if len(items) == 1:
        return E.Edit(items[0][0], items[0][1], [], step, what)
    if index < len(items) - 1:
        return E.Edit(items[index][0], items[index + 1][0], [], step, what)
    return E.Edit(items[index - 1][1], items[index][1], [], step, what)


def _rebuild_list(fs: FileState, items, open_at, close_at, new: list, step: int, what: str,
                  kind: str) -> E.Edit:
    """The whole list from scratch: each entry an original item (index) or new text."""
    t = fs.text
    parts: list = []
    multiline = len(items) > 1 and t.line_of(items[0][0]) != t.line_of(items[-1][0])
    sep = ("," + t.eol + t.indent_at(items[0][0])) if multiline else ", "
    for k, entry in enumerate(new):
        if k:
            parts.append(E.Piece(sep))
        if isinstance(entry, int):
            parts.append(E.Slot(*items[entry]))
        else:
            parts.append(E.Piece(entry, kind))
    if items:
        return E.Edit(items[0][0], items[-1][1], parts, step, what)
    return E.Edit(open_at, open_at, parts, step, what)


class Builder:
    """Turns located nodes into edits on their files."""

    def __init__(self, p: Planner):
        self.p = p
        self.unit_indent: dict[str, str] = {}

    def unit(self, fs: FileState) -> str:
        if fs.source.path not in self.unit_indent:
            self.unit_indent[fs.source.path] = E.indent_unit(fs.text.text)
        return self.unit_indent[fs.source.path]

    def slots(self, fs: FileState, loc: Located) -> dict:
        t = fs.text
        n = loc.node
        parts = n.get("parts", {})
        out: dict = {"$this": E.Slot(*t.span(n["range"])), "$name": E.Slot(*t.span(loc.ident))}
        scope = parts.get("scope")
        out["$recv"] = E.Slot(*t.span(scope["range"])) if scope else None
        args = parts.get("arguments") or []
        for k, a in enumerate(args):
            out[f"${k}"] = E.Slot(*t.span(a["range"]))
        if args:
            out["$args"] = E.Slot(t.span(args[0]["range"])[0], t.span(args[-1]["range"])[1])
        else:
            out["$args"] = ""
        for name, cap in (loc.captures or {}).items():
            if isinstance(cap, list):
                out[name] = (E.Slot(t.span(cap[0]["range"])[0], t.span(cap[-1]["range"])[1])
                             if cap else "")
            else:
                out[name] = E.Slot(*t.span(cap["range"]))
        return out

    def template(self, fs: FileState, loc: Located, tpl: str, base: str) -> list:
        body = E.reindent(tpl.replace("\r\n", "\n"), base, self.unit(fs))
        body = body.replace("\n", fs.text.eol)
        parts = E.template_parts(body, self.slots(fs, loc))
        for x in parts:
            if isinstance(x, E.Piece):
                x.track = True
        return parts

    def build(self, op: Op, loc: Located) -> None:
        p = self.p
        fs = p.file(loc.site.source)
        t = fs.text
        n = loc.node
        a = op.args
        step = op.step
        what = f"step {step} {op.action} at {loc.site.source}:{loc.site.line + 1}"
        s, e = t.span(n["range"])
        cat = _category(n["kind"])
        base = t.indent_at(s)
        act = op.action
        try:
            if act == "rename":
                i0, i1 = t.span(loc.ident)
                current = t.text[i0:i1]
                if current != a.get("old", current) and loc.site.optional:
                    return
                if a.get("field") and not loc.qualified and a["to"] in (loc.locals or []) \
                        and loc.site.role == "reference":
                    p.errors.append(f"step {step}: {loc.site.source}:{loc.site.line + 1}: a "
                                    f"local or parameter named `{a['to']}` there would capture "
                                    f"the renamed field (qualify it with this. first)")
                    return
                if not loc.qualified and loc.site.role == "reference":
                    self._enclosing_clash(op, loc)
                fs.edits.append(E.Edit(i0, i1, [E.Piece(a["to"], "Identifier")], step, what))
            elif act in ("replace", "wrap"):
                tpl = a.get("template", a.get("value"))
                if not isinstance(tpl, str):
                    raise E.EditError("replace/wrap need a template")
                parts = self.template(fs, loc, tpl, base)
                fs.edits.append(E.Edit(s, e, parts, step, what, kind=cat,
                                       paren_ok=cat == "Expression"))
            elif act == "remove":
                if cat in ("Statement", "BodyDeclaration") or n["kind"] == "ImportDeclaration":
                    _remove_lines(fs, s, e, step, what, n.get("comment"))
                elif n["kind"] == "Parameter" or op.node.startswith(("argument[", "parameter[")):
                    raise E.EditError("to remove an argument or parameter use remove_argument "
                                      "or remove_parameter on the call or declaration")
                else:
                    fs.edits.append(E.Edit(s, e, [], step, what))
            elif act in ("insert_before", "insert_after"):
                tpl = a.get("template", a.get("value"))
                kind = cat if cat in ("Statement", "BodyDeclaration") else ""
                if not kind:
                    raise E.EditError(f"insert next to a {n['kind']}: select a statement or a "
                                      "declaration (node)")
                anchor_start = s
                if act == "insert_before" and n.get("comment"):
                    cs = t.span(n["comment"])[0]
                    anchor_start = min(s, cs)
                if act == "insert_before":
                    at = t.line_start(anchor_start)
                    parts = self.template(fs, loc, tpl, base)
                    parts = [E.Piece(base)] + parts + [E.Piece(t.eol)]
                else:
                    at = t.line_end(e - 1)
                    parts = [E.Piece(base)] + self.template(fs, loc, tpl, base) + \
                        [E.Piece(t.eol)]
                    if at == len(t.text) and not t.text.endswith("\n"):
                        parts = [E.Piece(t.eol)] + parts[:-1]
                ed = E.Edit(at, at, parts, step, what)
                self._check_piece(ed, kind)
                fs.edits.append(ed)
            elif act in ("add_argument", "remove_argument", "reorder_arguments"):
                items, open_at, close_at = _list_info(fs, n, "arguments")
                self._list_edit(fs, act, a, items, open_at, close_at, step, what, "Expression",
                                loc)
            elif act in ("add_parameter", "remove_parameter", "reorder_parameters"):
                items, open_at, close_at = _list_info(fs, n, "parameters")
                self._list_edit(fs, act, a, items, open_at, close_at, step, what, "Parameter",
                                loc)
            elif act == "add_annotation":
                text = a.get("text", a.get("value"))
                if not isinstance(text, str) or not text.strip().startswith("@"):
                    raise E.EditError("add_annotation takes \"@Name(…)\"")
                name = _ann_name(text.strip())
                if any(_ann_name(x) == name for x in _annotations_text(fs, n)):
                    p.notes.append(f"{loc.site.source}:{loc.site.line + 1}: already has @{name}")
                    return
                own_line = cat == "BodyDeclaration"
                sep = (t.eol + base) if own_line else " "
                fs.edits.append(E.Edit(s, s, [E.Piece(text.strip(), "AnnotationExpr"),
                                              E.Piece(sep)], step, what))
            elif act == "remove_annotation":
                name = str(a.get("name", a.get("value", ""))).lstrip("@").rsplit(".", 1)[-1]
                for x in n.get("parts", {}).get("annotations", []) or []:
                    xs, xe = t.span(x["range"])
                    if _ann_name(t.text[xs:xe]) == name:
                        if t.alone_on_lines(xs, xe):
                            _remove_lines(fs, xs, xe, step, what)
                        else:
                            end = xe
                            while end < len(t.text) and t.text[end] in " \t":
                                end += 1
                            fs.edits.append(E.Edit(xs, end, [], step, what))
            elif act == "set_modifiers":
                self._modifiers(fs, loc, a, step, what)
            elif act == "add_member":
                tpl = a.get("template", a.get("value"))
                if not n["kind"].endswith(("ClassOrInterfaceDeclaration", "EnumDeclaration",
                                           "RecordDeclaration", "AnnotationDeclaration")):
                    raise E.EditError(f"add_member needs a type declaration, not {n['kind']}")
                close = e - 1  # the type's "}"
                inner = base + self.unit(fs)
                at = t.line_start(close) if not t.text[t.line_start(close):close].strip() \
                    else close
                body = self.template(fs, loc, tpl, inner)
                prev_blank = not t.line(t.line_of(at) - 1).strip()
                lead = [] if prev_blank or t.text[t.line_start(close):close].strip() else \
                    [E.Piece(t.eol)]
                parts = lead + [E.Piece(inner)] + body + [E.Piece(t.eol)]
                ed = E.Edit(at, at, parts, step, what)
                self._check_piece(ed, "BodyDeclaration")
                fs.edits.append(ed)
            elif act in ("add_import", "remove_import"):
                pass  # file-level: see file_ops
            elif act == "signature_decl":
                items, open_at, close_at = _list_info(fs, n, "parameters")
                if open_at is None:
                    raise E.EditError("no parameter list")
                new = []
                for prm in a["parameters"]:
                    if "from" in prm:
                        if prm["from"] >= len(items):
                            raise E.EditError(f"has {len(items)} parameters, no {prm['from']}")
                        new.append(prm["from"])
                    else:
                        new.append(prm["add"])
                removed = [k for k in range(len(items)) if k not in new]
                if removed:
                    self._removed_params_used(fs, n, removed, loc, op)
                self._new_params_hide(fs, n, [x for x in new if isinstance(x, str)], loc, op)
                fs.edits.append(_rebuild_list(fs, items, open_at, close_at, new, step, what,
                                              "Parameter"))
            elif act == "signature_call":
                items, open_at, close_at = _list_info(fs, n, "arguments")
                if open_at is None:
                    raise E.EditError("no argument list")
                n_old, varargs = a.get("old_arity", -1), a.get("varargs", False)
                if varargs and n_old >= 1:
                    fixed = n_old - 1
                    if len(items) < fixed:
                        raise E.EditError(f"the call has {len(items)} arguments, fewer than "
                                          f"the {fixed} fixed parameters")
                    groups = [[j] for j in range(fixed)] + [list(range(fixed, len(items)))]
                else:
                    if n_old >= 0 and len(items) != n_old:
                        raise E.EditError(f"the call has {len(items)} arguments, the method "
                                          f"{n_old} parameters")
                    groups = [[j] for j in range(len(items))]
                new = []
                for prm in a["parameters"]:
                    if "from" in prm:
                        if prm["from"] >= len(groups):
                            raise E.EditError(f"no parameter {prm['from']}: the method has "
                                              f"{len(groups)}")
                        new += groups[prm["from"]]   # a varargs group: all its arguments
                    else:
                        new.append(prm["default"])
                fs.edits.append(_rebuild_list(fs, items, open_at, close_at, new, step, what,
                                              "Expression"))
                self._null_ambiguity(fs, loc, items, op)
            elif act == "inspect" or act in ("move", "javadoc"):
                pass
            else:
                raise E.EditError(f"unknown action {act}")
        except E.EditError as ex:
            op.unmatched.append((loc.site, str(ex)))

    def _check_piece(self, ed: E.Edit, kind: str) -> None:
        """Mark the template's text in an insertion for validation (between the base indent
        and the end of line)."""
        if not kind:
            return
        inner = [x for x in ed.parts[1:-1]]
        if len(inner) == 1 and isinstance(inner[0], E.Piece):
            inner[0].kind = kind
        else:
            ed.kind = ""

    def _list_edit(self, fs, act, a, items, open_at, close_at, step, what, kind, loc):
        if open_at is None:
            raise E.EditError(f"no ( … ) list on {loc.node['kind']}")
        if act.startswith("add_"):
            idx = a.get("index", len(items))
            text = a.get("template", a.get("text", a.get("value")))
            if not isinstance(idx, int) or idx < 0:
                raise E.EditError("index must be a number ≥ 0 (or omitted: at the end)")
            if not isinstance(text, str) or not text.strip():
                raise E.EditError("the new argument or parameter text is required (template)")
            parts = self.template(fs, loc, text.strip(), fs.text.indent_at(open_at)) \
                if "$" in text else [E.Piece(text.strip())]
            if len(parts) == 1 and isinstance(parts[0], E.Piece):
                fs.edits.append(_insert_item(fs, items, open_at, min(idx, len(items)),
                                             parts[0].text, kind, step, what))
            else:
                ed = _insert_item(fs, items, open_at, min(idx, len(items)), "", kind, step, what)
                k = next(i for i, x in enumerate(ed.parts) if isinstance(x, E.Piece) and
                         x.kind == kind)
                ed.parts[k:k + 1] = parts
                ed.kind = ""
                fs.edits.append(ed)
        elif act.startswith("remove_"):
            idx = a.get("index", a.get("value"))
            if not isinstance(idx, int) or not 0 <= idx < len(items):
                raise E.EditError(f"has {len(items)} items, no index {idx}")
            fs.edits.append(_remove_item(fs, items, idx, step, what))
        else:
            order = a.get("order", a.get("value"))
            if not isinstance(order, list) or sorted(order) != list(range(len(items))):
                raise E.EditError(f"order must be a permutation of 0…{len(items) - 1}, got "
                                  f"{order!r}")
            fs.edits.append(_rebuild_list(fs, items, open_at, close_at, order, step, what, kind))

    def _modifiers(self, fs: FileState, loc: Located, a: dict, step: int, what: str) -> None:
        t = fs.text
        n = loc.node
        mods = n.get("parts", {}).get("modifiers", []) or []
        present = {t.text[slice(*t.span(m["range"]))].strip(): t.span(m["range"]) for m in mods}
        order = ["public", "protected", "private", "abstract", "static", "final", "sealed",
                 "non-sealed", "transient", "volatile", "synchronized", "native", "strictfp",
                 "default"]
        for m in a.get("remove", []) or []:
            if m in present:
                s, e = present[m]
                while e < len(t.text) and t.text[e] in " \t":
                    e += 1
                fs.edits.append(E.Edit(s, e, [], step, what + f" -{m}"))
        for m in a.get("add", []) or []:
            if m in present or m not in order:
                if m not in order:
                    raise E.EditError(f"unknown modifier {m}")
                continue
            later = [(order.index(x), sp) for x, sp in present.items() if x in order and
                     order.index(x) > order.index(m)]
            if later:
                at = min(later)[1][0]
                fs.edits.append(E.Edit(at, at, [E.Piece(m + " ")], step, what + f" +{m}"))
            elif present:
                at = max(sp[1] for sp in present.values())
                fs.edits.append(E.Edit(at, at, [E.Piece(" " + m)], step, what + f" +{m}"))
            else:
                from .javasrc import mask
                s = t.span(n["range"])[0]
                anns = n.get("parts", {}).get("annotations", []) or []
                at = max([t.span(x["range"])[1] for x in anns] + [s])
                masked = mask(t.text[at:at + 2000])
                skip = len(masked) - len(masked.lstrip())
                at += skip
                fs.edits.append(E.Edit(at, at, [E.Piece(m + " ")], step, what + f" +{m}"))

    def _enclosing_clash(self, op: Op, loc: Located) -> None:
        """An unqualified use inside a nested or anonymous class whose own members (or its
        supertype's) have the new name: the use would bind to them."""
        new = op.args["to"]
        owner = S.owner_type(loc.site.symbol) if loc.site.symbol else ""
        owner_name = S.name_of(owner) if owner else ""
        for enc in loc.enclosing or []:
            if enc.get("kind") == "type" and enc.get("name") == owner_name:
                break
            if new in (enc.get("members") or []):
                self.p.errors.append(f"step {op.step}: {loc.site.source}:{loc.site.line + 1}: "
                                     f"the enclosing class already declares `{new}`: the "
                                     "renamed use would bind to it")
                return
            if enc.get("kind") == "anonymous" and new in self._supertype_members(
                    loc.site.source, enc.get("type")):
                self.p.errors.append(f"step {op.step}: {loc.site.source}:{loc.site.line + 1}: "
                                     f"inside an anonymous class whose supertype has `{new}`: "
                                     "the renamed use would bind to it")
                return

    def _supertype_members(self, source: str, rng) -> set[str]:
        """Member names (inherited too) of the type named at `rng` (an anonymous class's
        supertype), through the index."""
        if not rng:
            return set()
        sym = self.p.symbol_at(source, rng[0] - 1, rng[1] - 1)
        if not sym or not S.is_type(sym):
            return set()
        return set(self.p.members_named(sym))

    def _removed_params_used(self, fs: FileState, n: dict, removed: list[int], loc: Located,
                             op: Op) -> None:
        t = fs.text
        params = n.get("parts", {}).get("parameters", []) or []
        body = n.get("parts", {}).get("body")
        if not body:
            return
        bs, be = t.span(body["range"])
        from .javasrc import mask
        masked = mask(t.text[bs:be])
        for k in removed:
            ps, pe = t.span(params[k]["range"])
            name = re.findall(r"[\w$]+", t.text[ps:pe])[-1]
            if re.search(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", masked):
                self.p.warnings.append(f"step {op.step}: {loc.site.source}:"
                                       f"{t.line_of(ps) + 1}: the removed parameter `{name}` "
                                       "is still used in the body (it will not compile)")

    def _new_params_hide(self, fs: FileState, n: dict, added: list[str], loc: Located,
                         op: Op) -> None:
        """A new parameter named like something the body already uses (a field, usually):
        the body now reads the parameter."""
        body = n.get("parts", {}).get("body")
        if not body or not added:
            return
        from .javasrc import mask
        bs, be = fs.text.span(body["range"])
        masked = mask(fs.text.text[bs:be])
        for text in added:
            names = re.findall(r"[\w$]+", text)
            if not names:
                continue
            name = names[-1]
            if re.search(rf"(?<![\w$.]){re.escape(name)}(?![\w$(])", masked):
                self.p.warnings.append(f"step {op.step}: {loc.site.source}:{loc.site.line + 1}: "
                                       f"the new parameter `{name}` hides the `{name}` the body "
                                       "already uses (a field?): the body now reads the "
                                       "parameter")

    def _null_ambiguity(self, fs: FileState, loc: Located, items, op: Op) -> None:
        for k, (s, e) in enumerate(items):
            if fs.text.text[s:e].strip() == "null" and op.args.get("_overloads"):
                self.p.warnings.append(f"step {op.step}: {loc.site.source}:"
                                       f"{loc.site.line + 1}: `null` argument with overloads "
                                       "of the same arity: the call may become ambiguous")
                return


# --- running a plan --------------------------------------------------------------------------------

@dataclass
class Result:
    plan_id: str = ""
    patch_path: str = ""
    text: str = ""
    refused: bool = False


def _analyze(p: Planner, worker, ops: list[Op]) -> None:
    """One analyze request for the sites and scopes of `ops`; fills op.found / op.unmatched."""
    by_file: dict[str, dict] = {}
    index: dict[str, tuple[Op, Site]] = {}
    for oi, op in enumerate(ops):
        for si, s in enumerate(op.sites):
            sid = f"{oi}.{si}"
            index[sid] = (op, s)
            req = {"id": sid, "line": s.line, "col": s.col, "name": s.name, "node": op.node}
            if op.where:
                req["where"] = op.where
            by_file.setdefault(s.source, {"sites": [], "scopes": []})["sites"].append(req)
        for ci, sc in enumerate(op.scopes):
            cid = f"{oi}.s{ci}"
            index[cid] = (op, Site(sc["source"], sc.get("line", 0), sc.get("col", 0),
                                   sc.get("name", "")))
            req = {"id": cid, "kind": sc["kind"]}
            if "line" in sc:
                req.update(line=sc["line"], col=sc["col"], name=sc["name"])
            if op.where:
                req["where"] = op.where
            by_file.setdefault(sc["source"], {"sites": [], "scopes": []})["scopes"].append(req)
        for f in op.files:
            by_file.setdefault(f, {"sites": [], "scopes": []})
    for path in p.files:
        by_file.setdefault(path, {"sites": [], "scopes": []})
    files = []
    for path, req in by_file.items():
        try:
            fs = p.file(path)
        except OSError as e:
            p.errors.append(f"{path}: {e}")
            continue
        files.append({"path": path, "text": fs.text.text, **req})
    if not files:
        return
    resp = worker.request({"op": "analyze", "files": files})
    for f in resp.get("files", []):
        fs = p.files.get(f["path"])
        if fs is None:
            continue
        if f.get("problems") or f.get("error"):
            msg = f.get("error") or "; ".join(x["message"] for x in f["problems"])
            p.errors.append(f"{f['path']} does not parse at {worker.level}: {msg}")
            fs.unit_ok = False
            continue
        fs.unit = f.get("unit", {})
        for s in f.get("sites", []):
            op, site = index[s["id"]]
            if not s.get("found"):
                reason = s.get("reason", "not found")
                if reason.startswith("no enclosing"):
                    op.filtered += 1          # the node spec selects, it did not fail
                elif not site.optional:
                    op.unmatched.append((site, reason))
                continue
            if op.where and not s.get("match"):
                continue
            op.found.append(Located(site, s["node"], s["ident"], s.get("qualified", False),
                                    s.get("locals", []), s.get("enclosing", []),
                                    s.get("member"), s.get("captures", {})))
        for sc in f.get("scopes", []):
            op, site = index[sc["id"]]
            if not sc.get("found"):
                op.unmatched.append((site, sc.get("reason", "not found")))
                continue
            for one in sc.get("nodes", []):
                node = one["node"]
                ident = node["range"]
                op.found.append(Located(Site(f["path"], ident[0] - 1, ident[1] - 1, "",
                                             role="node"), node, ident,
                                        one.get("qualified", False), one.get("locals", []),
                                        one.get("enclosing", []), one.get("member"),
                                        one.get("captures", {})))
    for op in ops:
        if op.where_types:
            _check_types(p, op)


def _where_types(where) -> dict:
    """where's "types": {"$x": "java.util.Collection"}: each capture named must be of that
    type or a subtype (primitives: that one)."""
    if not isinstance(where, dict) or "types" not in where:
        return {}
    types = where["types"]
    if not isinstance(types, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                              and v.strip() for k, v in types.items()):
        raise PlanError('where types: {"$x": "java.util.Collection", …}')
    pattern = str(where.get("pattern", ""))
    out = {}
    for name, typ in types.items():
        cap = name if name.startswith("$") else "$" + name
        if cap.startswith("$$"):
            raise PlanError(f"where types: {cap} captures a list; only $name captures have a "
                            "type")
        if not re.search(re.escape(cap) + r"(?![\w$])", pattern):
            raise PlanError(f"where types: {cap} is not a capture of the pattern {pattern!r}")
        out[cap] = typ.strip()
    return out


def _check_types(p: Planner, op: Op) -> None:
    """Leaves out the sites whose where captures are not of the types asked; those whose type
    cannot be told are left out too, and listed."""
    kept = []
    for loc in op.found:
        ok = True
        for cap, want in op.where_types.items():
            c = (loc.captures or {}).get(cap)
            if not isinstance(c, dict):
                continue
            typ = p.captures.type_of(loc.site.source, c)
            if not typ:
                op.untyped.append((cap, Site(loc.site.source, c["range"][0] - 1,
                                             c["range"][1] - 1, "")))
                ok = False
                break
            hit = p.captures.is_a(typ, want)
            if hit is None:   # a library type without its sources: its supertypes unknown
                op.untyped.append((cap, Site(loc.site.source, c["range"][0] - 1,
                                             c["range"][1] - 1, "", why=S.short(typ))))
                ok = False
                break
            if not hit:
                op.filtered += 1
                ok = False
                break
        if ok:
            kept.append(loc)
    op.found = kept
    if op.untyped:
        where = ", ".join(f"{s.source}:{s.line + 1}" + (f" ({s.why})" if s.why else "")
                          for _c, s in op.untyped[:5])
        caps = ", ".join(dict.fromkeys(c for c, _s in op.untyped))
        p.notes.append(f"step {op.step}: {len(op.untyped)} sites left out: the type of {caps} "
                       f"could not be told, or its supertypes (a library type without its "
                       f"sources jar, named in parentheses) ({where}"
                       f"{' …' if len(op.untyped) > 5 else ''}); check them by hand")


_LITERAL_TYPES = {"StringLiteralExpr": "java/lang/String#", "TextBlockLiteralExpr":
                  "java/lang/String#", "IntegerLiteralExpr": "int", "LongLiteralExpr": "long",
                  "BooleanLiteralExpr": "boolean", "CharLiteralExpr": "char",
                  "DoubleLiteralExpr": "double"}


def _top_level(text: str, start: int, end: int, stops: str) -> int:
    """The first of `stops` in text[start:end] outside `<…>`, or end."""
    depth = 0
    for k in range(start, end):
        ch = text[k]
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth <= 0 and ch in stops:
            return k
    return end


class CaptureTypes:
    """The static type of a where capture, from the SCIP occurrences in its range: a name or
    a field access (its declared type), a call (its return type), `new T(…)`, a cast, a
    literal. A SCIP type symbol (`java/util/List#`), a primitive, or "" when it cannot be
    told (a type variable, an array, another kind of expression)."""

    def __init__(self, p: Planner, facts: dict):
        self.p = p
        self.ancestors = facts.get("ancestors") or (lambda _t: [])
        self.returns = facts.get("returns") or (lambda _m: "")
        self.defined = facts.get("defined") or (lambda sym: p.nav.definition(sym) is not None)
        self._docs: dict = {}
        self._declared_cache: dict = {}
        self._named: dict = {}

    def _doc(self, source: str):
        if source not in self._docs:
            views = list(self.p.nav._views(self.p.nav.ws.shard_for_source(source)))
            self._docs[source] = views[0] if views else None
        return self._docs[source]

    def type_of(self, source: str, cap: dict) -> str:
        kind = cap.get("kind", "")
        fs = self.p.file(source)
        lo, hi = fs.text.span(cap["range"])
        if kind == "DoubleLiteralExpr":
            return "float" if fs.text.text[lo:hi].rstrip().endswith(("f", "F")) else "double"
        if kind in _LITERAL_TYPES:
            return _LITERAL_TYPES[kind]
        v = self._doc(source)
        if v is None:
            return ""
        occs = []
        for o in v.doc.occurrences:
            a = fs.text.scip(o.range.start_line, o.range.start_char)
            b = fs.text.scip(o.range.end_line, o.range.end_char)
            if lo <= a and b <= hi and not o.is_definition:
                occs.append((a, b, o.symbol))
        occs.sort()
        if kind == "EnclosedExpr":
            inner = fs.text.text[lo:hi].strip()
            if inner.startswith("(") and inner.endswith(")"):
                return ""   # (x): not looked into
        if kind == "NameExpr":
            hit = [x for x in occs if x[0] == lo]
            return self._declared(source, hit[0][2]) if hit else ""
        if kind == "FieldAccessExpr":
            hit = [x for x in occs if x[1] == hi]
            return self._declared(source, hit[-1][2]) if hit else ""
        if kind == "MethodCallExpr":
            sym = self._outer_call(fs.text.text, occs, hi)
            if not sym:
                return ""
            if self.defined(sym):
                return self._declared(source, sym)
            return self.returns(sym)
        text = fs.text.text
        if kind == "ObjectCreationExpr":    # new a.Outer.Inner<T>(…) {…}
            start = lo + text[lo:hi].find("new") + 3
            return self._head_type(text, occs, start, _top_level(text, start, hi, "([{"))
        if kind == "CastExpr":              # (Outer.Inner<T>) x
            start = text.find("(", lo, hi) + 1
            return self._head_type(text, occs, start, _top_level(text, start, hi, ")"))
        return ""

    @staticmethod
    def _head_type(text: str, occs: list, start: int, end: int) -> str:
        """The type written in text[start:end]: its last type (or constructor) occurrence
        outside `<…>` (type arguments)."""
        best = ""
        for a, _b, sym in occs:
            if not start <= a < end or text[start:a].count("<") != text[start:a].count(">"):
                continue
            if S.kind_of(sym) == "constructor":
                sym = S.parent(sym)
            if S.kind_of(sym) == "type":
                best = sym
        return best

    @staticmethod
    def _outer_call(text: str, occs: list, hi: int) -> str:
        """The method whose call ends the range: its name is followed by `(` closing at hi."""
        for a, b, sym in reversed(occs):
            if S.kind_of(sym) != "method":
                continue
            k = b
            while k < hi and text[k].isspace():
                k += 1
            if k >= hi or text[k] != "(":
                continue
            depth = 0
            for j in range(k, hi):
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        return sym if text[j + 1:hi].strip() == "" else ""
        return ""

    def _declared(self, source: str, sym: str) -> str:
        """The declared type of a variable, field or parameter, or a method's return type, from
        its signature; its simple name resolved through the type occurrences of the file that
        declares it."""
        key = (source, sym) if S.is_local(sym) else sym
        if key not in self._declared_cache:
            self._declared_cache[key] = self._declared_type(source, sym)
        return self._declared_cache[key]

    def _declared_type(self, source: str, sym: str) -> str:
        info, where = None, source
        if S.is_local(sym):
            v = self._doc(source)
            info = v.info(sym) if v else None
        else:
            d = self.p.nav.definition(sym) if self.defined(sym) else None
            if d is None:
                return ""   # a library field: not looked up
            info, where = d.info, d.location.source
        if info is None or not info.signature:
            return ""
        sig = S.without_annotations(info.signature)
        if S.is_method(sym):
            at = re.search(r"\b" + re.escape(S.name_of(sym)) + r"\s*\(", sig)
            if not at:
                return ""
            head = sig[:at.start()]
        else:
            head = sig.rsplit(None, 1)[0] if len(sig.split()) > 1 else ""
        while "<" in head:
            erased = re.sub(r"<[^<>]*>", " ", head)
            if erased == head:
                break
            head = erased
        words = head.split()
        if not words or "[" in words[-1] or words[-1].endswith("..."):
            return ""
        typ = words[-1]
        if typ in S.PRIMITIVES:
            return typ
        if "." in typ and typ[:1].islower():
            parts = typ.split(".")
            pkg = [x for x in parts if x[:1].islower()]
            return "/".join(pkg) + "/" + "".join(x + "#" for x in parts[len(pkg):])
        return self._type_named(where, typ)

    def _type_named(self, source: str, name: str) -> str:
        """A simple type name as the file that wrote it means it: the type (or constructor)
        occurrences of that file with that name."""
        if source not in self._named:
            names: dict[str, set] = {}
            v = self._doc(source)
            for o in (v.doc.occurrences if v is not None else []):
                sym = o.symbol
                if S.kind_of(sym) == "constructor":
                    sym = S.parent(sym)
                if S.kind_of(sym) == "type":
                    names.setdefault(S.name_of(sym), set()).add(sym)
            self._named[source] = names
        last = name.rsplit(".", 1)[-1]
        found = set(self._named[source].get(last, ()))
        if len(found) == 1:
            return found.pop()
        from .libsrc import JAVA_LANG_TYPES
        if not found and last in JAVA_LANG_TYPES:
            return f"java/lang/{last}#"
        return ""

    def is_a(self, typ: str, want: str) -> bool | None:
        """`typ` is `want` or one of its subtypes; `want` written qualified (java.util.List)
        or simple (List: any type of that name); primitives only match themselves. None when
        typ is not it and its supertypes cannot be read (a library type without sources)."""
        if typ in S.PRIMITIVES or typ == "void":
            return typ == want
        if want in ("Object", "java.lang.Object"):
            return True
        if "." in want and want[:1].islower():
            parts = want.split(".")
            pkg = [x for x in parts if x[:1].islower()]
            target = "/".join(pkg) + "/" + "".join(x + "#" for x in parts[len(pkg):])

            def same(t: str) -> bool:
                return t == target
        else:
            simple = want.rsplit(".", 1)[-1]

            def same(t: str) -> bool:
                return S.name_of(t) == simple
        if same(typ):
            return True
        ancestors = self.ancestors(typ)
        if ancestors is None:
            return None
        return any(same(a) for a in ancestors)


def _file_ops(p: Planner, op: Op) -> None:
    targets = set(op.files) | {loc.site.source for loc in op.found}
    for path in sorted(targets):
        fs = p.files.get(path)
        if fs is None or not fs.unit_ok:
            continue
        name = op.args.get("name", op.args.get("value"))
        if not isinstance(name, str):
            op.unmatched.append((Site(path, 0, 0, ""), "the import's name is required"))
            continue
        if op.action == "add_import":
            _add_import(p, fs, name, op.step)
        else:
            static = name.startswith("static ")
            fq = name[7:].strip() if static else name
            for i in fs.unit.get("imports", []):
                if i["name"] == fq and i["static"] == static:
                    s, e = fs.text.span(i["range"])
                    _remove_lines(fs, s, e, op.step, f"remove import {name}")


_TAG = r"(@(?:link|linkplain|see|value)\s+)"


def _javadoc_op(p: Planner, op: Op) -> None:
    a = op.args
    old, new = a["old"], a["new"]
    is_type = all(S.is_type(m) for m in a["members"])
    if is_type:
        # Whole dotted segments before the name: {@link FooBar} is not {@link Bar}.
        pat = re.compile(_TAG + r"((?:[\w$]+\.)*)" + re.escape(old) + r"(?![\w$])")
    else:
        pat = re.compile(_TAG + r"((?:[\w$]+\.)*[\w$]*)#" + re.escape(old) +
                         r"(?![\w$])(\s*\(([^)]*)\))?")
    owners = {S.name_of(S.parent(m)) for m in a["members"]} if not is_type else set()
    for path in op.files:
        fs = p.files.get(path)
        if fs is None:
            continue
        for c in fs.unit.get("comments", []):
            s, e = fs.text.span(c["range"])
            for m in pat.finditer(fs.text.text, s, e):
                if not is_type:
                    simple = m.group(2).rsplit(".", 1)[-1]
                    if simple and simple not in owners:
                        continue
                    if not simple and path not in a.get("owner_files", []):
                        continue
                    if m.group(3) is not None:
                        if _link_params(m.group(4)) not in a.get("params", []) and \
                                a.get("params"):
                            continue  # another overload's link
                    elif a.get("kept"):
                        continue      # {@link #m} still names the overload that stays
                at = m.start(0) + m.group(0).index(old, len(m.group(1)) + len(m.group(2)))
                fs.edits.append(E.Edit(at, at + len(old), [E.Piece(new)], op.step,
                                       f"step {op.step} javadoc {path}:{fs.text.line_of(at) + 1}"))


def _simple_type(t: str) -> str:
    t = re.sub(r"<.*>", "", t).replace("...", "[]")
    t = re.sub(r"@\s*[\w$.]+(\s*\([^)]*\))?", "", t)
    t = re.sub(r"\bfinal\b", "", t).strip()
    dims = t.count("[")
    base = re.sub(r"\[\s*\]", "", t).strip().rsplit(".", 1)[-1]
    return base + "[]" * dims


def _param_types(signature: str) -> tuple:
    """Erased simple parameter types of a method signature (names left out)."""
    from .compare import shape
    m = re.search(r"\((.*)\)", shape(signature or ""), re.S)
    if not m or not m.group(1).strip():
        return ()
    parts, depth, start, inner = [], 0, 0, m.group(1)
    for k, ch in enumerate(inner):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:k])
            start = k + 1
    parts.append(inner[start:])
    return tuple(_simple_type(x) for x in parts)


def _link_params(text: str) -> tuple:
    """{@link #m(java.lang.String, int[])} -> ("String", "int[]")."""
    if not text.strip():
        return ()
    return tuple(_simple_type(x.strip().split()[0] if x.strip() else "")
                 for x in text.split(","))


def _static_import_op(p: Planner, op: Op) -> None:
    want = op.args["owner"] + "." + op.args["old"]
    for path in op.files:
        fs = p.files.get(path)
        if fs is None or not fs.unit_ok:
            continue
        for i in fs.unit.get("imports", []):
            if i["static"] and not i["asterisk"] and i["name"] == want:
                s, e = fs.text.span(i["range"])
                at = fs.text.text.rindex(op.args["old"], s, e)
                fs.edits.append(E.Edit(at, at + len(op.args["old"]), [E.Piece(op.args["new"])],
                                       op.step, f"step {op.step} static import in {path}"))


def _imports_of_steps(p: Planner) -> None:
    for op in p.ops:
        if not op.imports:
            continue
        touched = {loc.site.source for loc in op.found}
        for path in touched:
            fs = p.files.get(path)
            if fs is None:
                continue
            if not any(e.step == op.step for e in fs.edits):
                continue
            for name in op.imports:
                _add_import(p, fs, name, op.step)


def run(nav: Navigator, settings, plan, worker_factory: Callable, cache_base: str | None,
        dry_run: bool = False, members_named: Callable | None = None,
        level: str = "", types_named: Callable | None = None,
        library_family: Callable | None = None, type_facts: dict | None = None) -> Result:
    plan = load_plan(plan)
    p = Planner(nav, settings, members_named, types_named, library_family, type_facts)
    p.expand(plan)
    total = sum(len(op.sites) for op in p.ops)
    if total > MAX_SITES:
        raise PlanError(f"{total} sites: more than {MAX_SITES}; split the plan")
    sources = {s.source for op in p.ops for s in op.sites} | \
        {sc["source"] for op in p.ops for sc in op.scopes} | {f for op in p.ops for f in op.files}
    fresh = nav.ws.freshness_of(sources)
    if fresh.stale:
        p.errors.append(f"{len(fresh.stale)} affected files changed after their last "
                        f"compilation ({', '.join(fresh.stale[:5])}"
                        f"{' …' if len(fresh.stale) > 5 else ''}): compile ("
                        f"{', '.join(sorted(fresh.tasks)) or 'the build'}) and run the plan again")
    from .workspace import build_running
    if build_running(nav.ws.root):
        p.errors.append("a gradle/mvn build is running in this checkout: wait for it")
    if p.errors and not dry_run:
        return Result(text=_report(p, None, dry_run=False), refused=True)
    with worker_factory(level) as w:
        p.unit_level = w.level
        if w.note:
            p.notes.append(w.note)
        rounds = 0
        pending = list(p.ops)
        while pending and rounds < 3:
            rounds += 1
            _analyze(p, w, pending)
            for op in pending:
                if op.hook:
                    op.hook(p, op)
            pending = [op for op in p.ops if op not in pending and not op.found
                       and not op.unmatched and (op.sites or op.scopes)]
        builder = Builder(p)
        for op in p.ops:
            if op.action in ("add_import", "remove_import"):
                _file_ops(p, op)
            elif op.action == "javadoc":
                _javadoc_op(p, op)
            elif op.action == "static_import":
                _static_import_op(p, op)
            else:
                for loc in op.found:
                    if p.files[loc.site.source].unit_ok:
                        builder.build(op, loc)
        _imports_of_steps(p)
        _templates_parse(p, w)
        if dry_run:   # the checks ran (captures, clashes, templates); nothing is written
            return Result(text=_report(p, None, dry_run=True))
        outcome = _compose(p, w) if not p.errors else None
        if p.errors:
            return Result(text=_report(p, None, dry_run=False), refused=True)
        return _store(p, plan, outcome, cache_base, w)


def _templates_parse(p: Planner, w) -> None:
    """Templates parse on their own (placeholders as names) at the project's level."""
    seen = set()
    for op in p.ops:
        tpl = op.args.get("template", op.args.get("value")) if op.action in (
            "replace", "wrap", "insert_before", "insert_after", "add_member") else None
        if not isinstance(tpl, str) or (op.step, tpl) in seen or not op.found:
            continue
        seen.add((op.step, tpl))
        loc = op.found[0]
        cat = _category(loc.node["kind"])
        if op.action == "add_member":
            cat = "BodyDeclaration"
        kind = _parse_kind(cat)
        if not kind:
            continue
        text = re.sub(r"\$this\b", "$this;", tpl) if cat == "Statement" and \
            op.action == "wrap" else tpl
        r = w.request({"op": "parse", "kind": kind, "text": text})
        if not r.get("ok"):
            msg = "; ".join(x["message"] for x in r.get("problems", []))[:300]
            p.errors.append(f"step {op.step}: the template does not parse as {kind} at "
                            f"{p.unit_level.replace('JAVA_', 'Java ')}: {msg}")


@dataclass
class Outcome:
    files: dict = field(default_factory=dict)     # path -> (FileState, Rendered)
    expect: list = field(default_factory=list)
    templates: list = field(default_factory=list)   # where template text landed
    removed_comments: list = field(default_factory=list)


def _compose(p: Planner, w) -> Outcome:
    out = Outcome()
    for path, fs in sorted(p.files.items()):
        if not fs.edits:
            continue
        edits = E.dedupe(fs.edits)
        problems = E.check_overlaps(edits)
        if problems:
            p.errors += [f"{path}: {x}" for x in problems]
            continue
        rendered = E.render(fs.text.text, edits)
        for d in rendered.dropped:
            p.warnings.append(f"{path}: {d.what} is inside code another step replaces: "
                              "dropped")
        out.files[path] = (fs, rendered, edits)
    # Validate, retrying failed expressions with parentheses once.
    for attempt in (0, 1):
        reqs = []
        for path, (fs, rendered, edits) in out.files.items():
            nt = E.Text(rendered.text)
            regions = []
            for k, pl in enumerate(rendered.placed):
                if pl.end <= pl.start or pl.kind == "Template":
                    continue
                bl, bc = nt.position(pl.start)
                el, ec = nt.end_position(pl.end)
                regions.append({"id": k, "begin": [bl, bc], "end": [el, ec], "kind": pl.kind})
            reqs.append({"path": path, "text": rendered.text, "regions": regions})
        if not reqs:
            break
        resp = w.request({"op": "validate", "files": reqs})
        retry = False
        for f in resp.get("files", []):
            path = f["path"]
            fs, rendered, edits = out.files[path]
            if not f.get("ok"):
                msg = "; ".join(x["message"] + (f" (line {x['at'][0]})" if x.get("at") else "")
                                for x in f.get("problems", []))
                if attempt == 1 or not _paren_candidates(rendered, f):
                    p.errors.append(f"{path}: the edited file does not parse: {msg[:400]}")
                    continue
            for reg in f.get("regions", []):
                if reg.get("ok"):
                    continue
                pl = rendered.placed[reg["id"]]
                if attempt == 0 and pl.edit.paren_ok and not pl.edit.paren:
                    pl.edit.paren = True
                    retry = True
                    continue
                nt = E.Text(rendered.text)
                line = nt.position(pl.start)[0]
                p.errors.append(f"{path}:{line}: {pl.edit.what}: `{rendered.text[pl.start:pl.end][:80]}`"
                                f" is not a {pl.kind} there"
                                + (f" (found {', '.join(reg.get('there', []))})"
                                   if reg.get("there") else ""))
        if not retry:
            break
        for path, (fs, _r, edits) in list(out.files.items()):
            out.files[path] = (fs, E.render(fs.text.text, edits), edits)
    # Expectations for verify: where each renamed identifier is in the new text.
    for path, (fs, rendered, _edits) in out.files.items():
        nt = E.Text(rendered.text)
        new_path = p.moves.get(path, path)
        for pl in rendered.placed:
            if pl.kind == "Identifier":
                line, col = nt.position(pl.start)
                out.expect.append({"source": new_path, "line": line - 1, "col": col - 1,
                                   "name": rendered.text[pl.start:pl.end]})
            elif pl.kind == "Template":
                bl, bc = nt.position(pl.start)
                el, ec = nt.position(pl.end)
                out.templates.append({"source": new_path, "start": [bl - 1, bc - 1],
                                      "end": [el - 1, ec - 1]})
        for s, e in rendered.removed:
            for c in fs.unit.get("comments", []):
                cs, ce = fs.text.span(c["range"])
                if s <= cs and ce <= e:
                    out.removed_comments.append(f"{path}:{fs.text.line_of(cs) + 1}  "
                                                f"{c['text'].strip()[:80]}")
    return out


def _paren_candidates(rendered: E.Rendered, f: dict) -> bool:
    return any(pl.edit.paren_ok and not pl.edit.paren for pl in rendered.placed)


def _store(p: Planner, plan: dict, outcome: Outcome, cache_base: str | None, w) -> Result:
    root = p.root
    patch = b""
    changed = []
    meta_files = []
    for path in sorted(set(outcome.files) | set(p.moves)):
        if path in outcome.files:
            fs, rendered, _e = outcome.files[path]
            new_text = rendered.text
        else:
            fs = p.file(path)
            new_text = fs.text.text
        new_path = p.moves.get(path, path)
        patch += E.file_patch(fs.source, new_path, new_text)
        new_bytes = fs.source.encode(new_text)
        meta_files.append({"old": path, "new": new_path, "old_blob": E.blob_id(fs.source.data),
                           "new_blob": E.blob_id(new_bytes)})
        changed.append(new_path)
    for path, text in sorted(p.creates.items()):
        src = E.Source(path, b"", "")
        patch += E.file_patch(None, path, text, src)
        meta_files.append({"old": None, "new": path, "old_blob": None,
                           "new_blob": E.blob_id(text.encode("utf-8"))})
        changed.append(path)
    if p.creates:
        resp = w.request({"op": "validate", "files": [
            {"path": k, "text": v, "regions": []} for k, v in p.creates.items()]})
        for f in resp.get("files", []):
            if not f.get("ok"):
                msg = "; ".join(x["message"] for x in f.get("problems", []))
                return Result(text=_report(p, None, False, extra=[
                    f"{f['path']}: the new file does not parse: {msg[:300]}"]), refused=True)
            pkg = f.get("unit", {}).get("package", {}).get("name", "")
            folder = os.path.dirname(f["path"])
            if pkg and not (folder == pkg.replace(".", "/") or
                            folder.endswith("/" + pkg.replace(".", "/"))):
                p.warnings.append(f"{f['path']}: package {pkg} does not match its folder")
    if not patch:
        p.notes.append("nothing to change")
        return Result(text=_report(p, None, False))
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode() + patch).hexdigest()[:12]
    entry = cachedir.entry_dir(root, cache_base)
    d = cachedir.ensure_private_dir(os.path.join(entry, "plans"))
    d = cachedir.ensure_private_dir(os.path.join(d, digest))
    patch_path = os.path.join(d, "patch.diff")
    with open(patch_path, "wb") as fh:
        fh.write(patch)
    edited = {(path, fs.text.line_of(e.start) + 1)
              for path, (fs, _r, edits) in outcome.files.items() for e in edits}
    review = [r for r in _ranked(p.review) if (r[0], r[1]) not in edited]
    if review:
        with open(os.path.join(d, "review.txt"), "w", encoding="utf-8") as fh:
            for a, b, c, d_ in review:
                fh.write(f"{a}:{b}  {c}  ({d_})\n")
    meta = {"plan": plan, "root": os.path.realpath(root), "created": time.time(),
            "files": meta_files, "expect": outcome.expect, "templates": outcome.templates,
            "level": p.unit_level,
            "javaparser": w.version, "summary": p.summary,
            # Tests and features that name the old names in text: after the change they may
            # fail, or pass without checking anything.
            "review_tests": [f"{a}:{b}" for a, b, c, _d in sorted(
                {r for r in p.review if _is_test(r[0])},
                key=lambda r: (not re.search(r"Pattern\.compile|\\\\[.s(]|\.matches\(", r[2]),
                               r[0], r[1]))],
            # What a rename or a move removes (a replace leaves its symbol in place, and a
            # library symbol never had a definition here).
            "symbols": sorted({s.symbol for op in p.ops if op.action in ("rename", "move")
                               for s in op.sites if s.symbol and not S.is_local(s.symbol)
                               and p.nav.definition(s.symbol) is not None})}
    with open(os.path.join(d, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    res = Result(digest, patch_path)
    res.text = _report(p, outcome, False, plan_id=digest, patch_path=patch_path, patch=patch,
                       version=w.version)
    return res


def _report(p: Planner, outcome: Outcome | None, dry_run: bool, plan_id: str = "",
            patch_path: str = "", patch: bytes = b"", version: str = "",
            extra: list[str] | None = None) -> str:
    lines: list[str] = []
    errors = list(dict.fromkeys(p.errors + (extra or [])))
    unmatched = [(op, s, why) for op in p.ops for s, why in op.unmatched
                 if op.action != "inspect"]
    if dry_run:
        lines.append("refactor plan (dry run: nothing written)")
    elif errors:
        lines.append("refactor plan REFUSED: no patch written")
    else:
        n_files = len(outcome.files) if outcome else 0
        lines.append(f"refactor plan {plan_id}: {n_files} files changed"
                     + (f", {len(p.moves)} renamed or moved" if p.moves else "")
                     + (f", {len(p.creates)} created" if p.creates else ""))
    for s in p.summary:
        lines.append("  " + s)
    if dry_run or len(p.ops) <= 12:
        for op in p.ops:
            if op.action == "inspect":
                continue
            if op.files and not op.sites and not op.scopes:
                if dry_run:
                    lines.append(f"  · {op.label}: {len(op.files)} files to check")
                continue
            files = {loc.site.source for loc in op.found}
            lines.append(f"  · {op.label}: {len(op.found)} nodes in {len(files)} files"
                         + (f", {len(op.unmatched)} sites without a node"
                            if op.unmatched else "")
                         + (f", {op.filtered} sites left out by node/where" if op.filtered
                            else ""))
            if dry_run:
                for loc in op.found[:8]:
                    caps = "  " + ", ".join(f"{k}" for k in (loc.captures or {})) \
                        if loc.captures else ""
                    lines.append(f"      {loc.site.source}:{loc.site.line + 1}  "
                                 f"{loc.node.get('kind')}{caps}")
                if len(op.found) > 8:
                    lines.append(f"      … and {len(op.found) - 8} more")
    if errors:
        lines.append(f"refused because ({len(errors)}):")
        lines += [f"  - {x}" for x in errors[:40]]
    if unmatched:
        lines.append(f"sites the plan could not apply ({len(unmatched)}; adapt them by hand or "
                     "change the plan):")
        for op, s, why in unmatched[:30]:
            lines.append(f"  {s.source}:{s.line + 1}  {why}  (step {op.step})")
    edited = set()
    if outcome:
        for path, (fs, _r, edits) in outcome.files.items():
            edited |= {(path, fs.text.line_of(e.start) + 1) for e in edits}
    review = [r for r in _ranked(p.review) if (r[0], r[1]) not in edited]
    if review:
        keys = [r for r in review if _rank(r) == 3]
        rows = [f"{a}:{b}  {c}  ({d})" for a, b, c, d in review if _rank((a, b, c, d)) < 3]
        lines.append(f"review by hand ({len(review)}: uses by name the index does not see, not "
                     "changed; most likely to matter first):")
        lines += [f"  {x}" for x in rows[:40]]
        if len(rows) > 40:
            lines.append(f"  … and {len(rows) - 40} more" + (
                f" (all in {os.path.join(os.path.dirname(patch_path), 'review.txt')})"
                if patch_path else ""))
        if keys:
            files = sorted({r[0] for r in keys})
            lines.append(f"  {len(keys)} map or parameter keys (put/get(\"…\")) in "
                         f"{len(files)} files, e.g. " + ", ".join(f"{a}:{b}" for a, b, _c, _d
                                                                in keys[:4]))
    if outcome:
        from .graph import generated_header
        generated = [path for path, (fs, _r, _e) in outcome.files.items()
                     if generated_header(fs.source.data[:4000])]
        if generated:
            p.notes.append(f"{len(generated)} edited files say they are generated (their "
                           f"header): regenerating them undoes these edits ("
                           f"{', '.join(generated[:5])}{' …' if len(generated) > 5 else ''})")
    if outcome and outcome.removed_comments:
        lines.append("comments inside replaced or removed code (gone):")
        lines += [f"  {x}" for x in outcome.removed_comments[:10]]
    if p.warnings:
        lines.append("warnings:")
        lines += [f"  - {x}" for x in list(dict.fromkeys(p.warnings))[:30]]
    if p.notes:
        lines += [f"note: {x}" for x in p.notes]
    if dry_run or errors or not patch_path:
        return "\n".join(lines)
    lines.append(f"patch: {patch_path}")
    lines.append(f"  syntax verified with JavaParser {version} at "
                 f"{p.unit_level.replace('JAVA_', 'Java ')}; NOT compiled, and data flow "
                 "(extract/inline) is not checked")
    lines.append("apply it (ask the user first):")
    lines.append(f"  git apply --check '{patch_path}' && git apply '{patch_path}'")
    lines.append(f"then compile, and run refactor(plan_id=\"{plan_id}\", verify=true): it "
                 "checks the new index and lists the tests to run")
    if _has_formatter(p.root):
        lines.append("the project has a formatter (Spotless/Checkstyle config): run it before "
                     "committing")
    text = patch.decode("utf-8", errors="replace").split("\n")
    lines.append(f"diff ({len(text)} lines" + (f", first {DIFF_LINES}" if len(text) > DIFF_LINES
                                                 else "") + "):")
    lines += text[:DIFF_LINES]
    return "\n".join(lines)


def _is_test(path: str) -> bool:
    parts = path.split("/")
    return path.endswith(".feature") or "test" in parts or "tests" in parts or \
        re.search(r"(Test|Tests|IT)\.java$", path) is not None


def _rank(r) -> int:
    """How likely a use by name is to break: 0 a path or a comparison in a query, 1 a
    feature/fixture, 2 a name in a string, 3 a map or parameter key."""
    path, _line, text, _label = r
    if path.endswith(".java"):
        if re.search(r"\b(put|get|getOrDefault|containsKey|remove|setParameter|addParameter)"
                     r"\s*\(\s*\"", text):
            return 3
        if re.search(r'"[^"]*\w\.[a-z]\w*|"[^"]*\b\w+\s*(==|!=|<|>)', text):
            return 0
        return 2
    if path.endswith(".feature"):
        return 1
    return 2


def _ranked(review: list) -> list:
    return sorted(dict.fromkeys(review), key=lambda r: (_rank(r), r[0], r[1]))


def _has_formatter(root: str) -> bool:
    for name in ("build.gradle", "build.gradle.kts", "pom.xml"):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as fh:
                if re.search(r"spotless|googleJavaFormat|palantir", fh.read()):
                    return True
        except OSError:
            continue
    return False


# --- verify ----------------------------------------------------------------------------------------

def load_meta(root: str, plan_id: str, cache_base: str | None) -> tuple[dict, str]:
    if not re.fullmatch(r"[0-9a-f]{12}", plan_id or ""):
        raise PlanError("plan_id is the 12-hex id a refactor call printed")
    d = os.path.join(cachedir.entry_dir(root, cache_base), "plans", plan_id)
    try:
        with open(os.path.join(d, "plan.json"), encoding="utf-8") as fh:
            return json.load(fh), os.path.join(d, "patch.diff")
    except OSError as e:
        raise PlanError(f"no plan {plan_id} for this checkout (plans live in its cache)") from e


def _class_files(nav: Navigator, sources: list[str], since: float | None = None) -> list[str]:
    """A build that failed may still write shards (the plugin runs before javac stops): a
    class file missing, or older than its source, in the usual Gradle/Maven output folder
    tells. Unknown layouts are not judged."""
    root = nav.ws.root
    out = []
    for src in sources:
        full = os.path.join(root, src)
        try:
            mtime = os.stat(full).st_mtime
            with open(full, encoding="utf-8", errors="replace") as fh:
                m = re.search(r"^\s*package\s+([\w.]+)\s*;", fh.read(20000), re.M)
        except OSError:
            continue
        rel = (m.group(1).replace(".", "/") + "/" if m else "") + \
            os.path.basename(src)[:-len(".java")] + ".class"
        for sh in nav.ws.shard_for_source(src):
            module, _, task = sh.module.rpartition("/")
            tm = re.fullmatch(r"compile(\w*)Java", task)
            if not tm:
                continue
            sset = tm.group(1)[:1].lower() + tm.group(1)[1:] if tm.group(1) else "main"
            mod_dir = os.path.join(root, module) if module else root
            for d in (os.path.join(mod_dir, "build", "classes", "java", sset),
                      os.path.join(mod_dir, "target",
                                   "classes" if sset == "main" else "test-classes")):
                if not os.path.isdir(d):
                    continue
                cls = os.path.join(d, rel)
                try:
                    cm = os.stat(cls).st_mtime
                    if cm + 1 < mtime and not (since and cm > since):
                        out.append(f"{src}: its class file is older than the source (did "
                                   f"{task} fail?)")
                except OSError:
                    out.append(f"{src}: no {rel} in {os.path.relpath(d, root)} (did {task} "
                               "fail?)")
                break
    return out


def verify(nav: Navigator, meta: dict, confirm: Callable | None = None) -> list[str]:
    root = nav.ws.root
    lines: list[str] = []
    problems: list[str] = []
    not_applied, changed_since = [], []
    for f in meta["files"]:
        path = os.path.join(root, f["new"])
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            not_applied.append(f["new"])
            continue
        blob = E.blob_id(data)
        if blob == f["new_blob"]:
            continue
        if f["old"] == f["new"] and blob == f["old_blob"]:
            not_applied.append(f["new"])
        else:
            changed_since.append(f["new"])
    if not_applied:
        problems.append(f"the patch is not applied: {len(not_applied)} files as before "
                        f"({', '.join(not_applied[:5])})")
    if changed_since:
        lines.append(f"note: {len(changed_since)} files changed after the patch "
                     f"({', '.join(changed_since[:5])}): checked anyway")
    targets = [f["new"] for f in meta["files"]]
    fresh = nav.ws.freshness_of(targets)
    # A file written again with the content it was compiled from (a checkout and the same
    # patch again): Gradle finds the build up to date by content and its shard stays older
    # than the file. With the patched content and a shard written after the plan, it counts.
    patched = {f["new"] for f in meta["files"]} - set(not_applied) - set(changed_since)
    created = meta.get("created", 0)

    def shard_after_plan(src: str) -> bool:
        """A shard written after the patched file (a build of that content), not only after
        the plan (a build between planning and applying saw the old sources)."""
        try:
            written = max(created, os.stat(os.path.join(nav.ws.root, src)).st_mtime)
            return any(os.stat(sh.path).st_mtime > written
                       for sh in nav.ws.shard_for_source(src))
        except OSError:
            return False
    same = [t for t in fresh.stale if t in patched and shard_after_plan(t)]
    if same and confirm is not None:
        confirm(same)   # recorded: freshness checks stop calling them stale
    if same:
        lines.append(f"note: {len(same)} patched files are newer than their shards but hold "
                     "the patched content, compiled after the plan (an up-to-date build): "
                     "counted as compiled")
        fresh.stale = [t for t in fresh.stale if t not in same]
    missing = [t for t in targets if not nav.ws.shard_for_source(t)]
    if fresh.stale or missing:
        problems.append("not compiled since the patch: " + ", ".join((fresh.stale + missing)[:8])
                        + (f" — compile ({', '.join(sorted(fresh.tasks))}) and verify again"
                           if fresh.tasks else " — compile and verify again"))
        return problems + lines
    missing_classes = _class_files(nav, targets, created if same else None)
    by_task: dict[str, list[str]] = {}
    for m in missing_classes:
        task = re.search(r"did (\w+) fail", m)
        by_task.setdefault(task.group(1) if task else "?", []).append(m.split(":", 1)[0])
    for task, srcs in sorted(by_task.items()):
        problems.append(f"{len(srcs)} patched files have no class file newer than their source "
                        f"(did {task} fail?): {', '.join(srcs[:4])}{' …' if len(srcs) > 4 else ''}")
    # Each renamed identifier must now be an occurrence of a symbol with that name, not a
    # local (a local capturing a renamed field shows up here).
    wrong = []
    by_source: dict[str, list] = {}
    for x in meta.get("expect", []):
        by_source.setdefault(x["source"], []).append(x)
    for source, xs in by_source.items():
        occ_at = {}
        for v in nav._views(nav.ws.shard_for_source(source)):
            for o in v.doc.occurrences:
                occ_at.setdefault((o.range.start_line, o.range.start_char), []).append(o.symbol)
        for x in xs:
            syms = occ_at.get((x["line"], x["col"]), [])
            if not syms:
                wrong.append(f"{source}:{x['line'] + 1}: `{x['name']}` does not resolve to any "
                             "symbol (did the build fail? check its output)")
                continue
            if all(S.is_local(s) for s in syms):
                wrong.append(f"{source}:{x['line'] + 1}: `{x['name']}` now refers to a local "
                             "variable (shadowed)")
            elif not any(S.name_of(s) == x["name"] or (S.kind_of(s) == "constructor" and
                                                       S.name_of(S.parent(s)) == x["name"])
                         for s in syms):
                wrong.append(f"{source}:{x['line'] + 1}: `{x['name']}` refers to "
                             f"{', '.join(S.short(s) for s in syms)}")
    problems += wrong
    gone = []
    for sym in meta.get("symbols", []):
        if S.is_local(sym):
            continue
        if nav.definition(sym) is None:
            refs = nav.references(sym)[0]
            if refs:
                gone.append(f"{S.short(sym)} no longer exists but {len(refs)} references remain"
                            f" ({refs[0].location.source}:{refs[0].location.line})")
    problems += gone
    resolved: dict[str, int] = {}
    for x in meta.get("templates", []):
        for v in nav._views(nav.ws.shard_for_source(x["source"])):
            for o in v.doc.occurrences:
                r = o.range
                if o.is_definition or S.is_local(o.symbol):
                    continue
                if tuple(x["start"]) <= (r.start_line, r.start_char) and \
                        (r.end_line, r.end_char) <= tuple(x["end"]):
                    resolved[o.symbol] = resolved.get(o.symbol, 0) + 1
    if resolved:
        top = sorted(resolved.items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append("the code the templates added resolves to (check the unexpected ones): "
                     + ", ".join(f"{S.short(k)} ×{n}" for k, n in top[:15])
                     + (" …" if len(top) > 15 else ""))
    checked = len(meta.get("expect", []))
    if meta.get("review_tests"):
        rt = meta["review_tests"]
        lines.append(f"tests and features that named the old names in text ({len(rt)}): run "
                     "them; they may fail, or pass without checking anything: "
                     + ", ".join(rt[:12]) + (" …" if len(rt) > 12 else ""))
    if not problems:
        lines.insert(0, f"verified: {len(meta['files'])} files compiled after the patch"
                     + (f", {checked} renamed identifiers resolve to the renamed symbols"
                        if checked else "")
                     + " (from the index: check that the build itself succeeded)")
    return problems + lines

"""`context(symbol, token_budget)`: what an agent needs before touching a symbol, in one answer
trimmed to a budget.

- A method: its definition and body, what overrides it, callers, callees and tests.
- A type: its declaration, members, subtypes, the files that use it, and tests.
- A field: its accessors (generated ones included), writers and readers, and tests.
- A library or JDK symbol: its declaration from the sources jar, and its uses in the project.

Everything comes from the shards (and the sources jars). Nothing is padded: a small symbol
gets a small answer. Each trimmed section names the tool that shows all of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import extras as X
from . import render
from . import symbols as S
from .queries import Definition, Navigator, Reference, declared_supertypes, signature_line

CHARS_PER_TOKEN = 4
JAVADOC_CHARS = 600
LINE_CHARS = 160
BODY_TAIL = 3   # closing lines of a body kept when its middle is cut
MIN_BUDGET = 250
IMPACT_MAX_BEHIND = 50  # shards: more than that and context does not update the index

# Initial share of the budget left after the header; what a section does not use goes to the
# next ones that were cut, in this order.
SHARES = {
    "method": {"body": .40, "family": .10, "callers": .25, "callees": .15, "tests": .10},
    "type": {"members": .35, "subtypes": .15, "users": .30, "tests": .20},
    "field": {"accessors": .10, "writes": .35, "reads": .35, "tests": .20},
    "external": {"body": .30, "callers": .50, "tests": .20},
}


@dataclass
class Section:
    name: str
    title: str
    lines: list[str] = field(default_factory=list)
    more: str = ""      # the tool call that shows all of it
    tail: int = 0       # lines kept at the end when it is cut (a body's closing lines)

    def size(self) -> int:
        return len(self.title) + 1 + sum(len(x) + 1 for x in self.lines)


@dataclass
class Context:
    kind: str                                   # a key of SHARES
    head: list[str]
    sections: list[Section] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)   # one-liners after the header
    sources: set[str] = field(default_factory=set)


# --- fitting ---------------------------------------------------------------------------------

def fit(ctx: Context, budget_tokens: int) -> str:
    budget = max(MIN_BUDGET, budget_tokens) * CHARS_PER_TOKEN
    head = ctx.head + ctx.hints
    out = list(head)
    avail = max(0, budget - sum(len(x) + 1 for x in head))
    secs = [s for s in ctx.sections if s.lines]
    shares = SHARES.get(ctx.kind, {})
    alloc = {s.name: int(avail * shares.get(s.name, .1)) for s in secs}
    spare = avail - sum(min(alloc[s.name], s.size()) for s in secs)
    for s in secs:  # priority order: give what others left to the sections that were cut
        need = s.size() - alloc[s.name]
        if need > 0 and spare > 0:
            give = min(need, spare)
            alloc[s.name] += give
            spare -= give
    for s in secs:
        out.append("")
        out += _cut(s, alloc[s.name])
    return "\n".join(out)


def _cut(s: Section, room: int) -> list[str]:
    if s.size() <= room:
        return [s.title] + s.lines
    out = [s.title]
    used = len(s.title) + 1
    tail = s.lines[-s.tail:] if s.tail and len(s.lines) > s.tail else []
    tail_size = sum(len(x) + 1 for x in tail)
    reserve = 60 + len(s.more)
    if used + tail_size + reserve > room:
        tail, tail_size = [], 0
    body = s.lines[:len(s.lines) - len(tail)]
    kept = 0
    for x in body:
        if used + len(x) + 1 + tail_size + reserve > room:
            break
        out.append(x)
        used += len(x) + 1
        kept += 1
    left = len(body) - kept
    if not kept and not tail:  # nothing fits: just say where it is
        return [s.title.rstrip(":") + (f" → {s.more}" if s.more else "")]
    if left:
        what = "lines omitted" if s.tail else "more"
        out.append(f"  … {left} {what}" + (f" → {s.more}" if s.more else ""))
    return out + tail


# --- pieces ----------------------------------------------------------------------------------

def _clip(text: str, n: int = LINE_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def _javadoc(d: Definition) -> list[str]:
    docs = [x.strip() for x in (d.info.documentation if d.info else []) if x.strip()]
    if not docs:
        return []
    text = "\n".join(docs)
    if len(text) > JAVADOC_CHARS:
        text = text[:JAVADOC_CHARS].rstrip() + " …"
    return [""] + text.splitlines()


def _signature(d: Definition) -> list[str]:
    if not d.info or not d.info.signature:
        return []
    lines = [x.strip() for x in d.info.signature.strip().splitlines() if x.strip()]
    return [_clip(" ".join(lines), 300)]


def head_of(nav: Navigator, d: Definition) -> list[str]:
    where = f"{d.kind} at {render.loc(d)}"
    if d.generated_by is not None:
        node = nav.ws.source_lines(d.location.source, [d.generated_by.start_line]).get(
            d.generated_by.start_line, "")
        where += f"  (generated by: {_clip(node, 100)})" if node else "  (generated)"
    elif d.generated:
        where += "  (generated)"
    elif d.enclosing is not None and d.enclosing.end_line > d.enclosing.start_line:
        where += f", body {d.enclosing.start_line + 1}–{d.enclosing.end_line + 1}"
    out = [d.symbol, where]
    if d.container:
        out.append(f"anonymous class member, in {d.container}")
    out += _signature(d)
    if d.info:
        impl = [r.symbol for r in d.info.relationships if r.is_implementation]
        if impl and S.is_method(d.symbol):
            out.append("overrides: " + ", ".join(S.short(s) for s in impl[:6])
                       + (" …" if len(impl) > 6 else ""))
        elif impl:
            declared = declared_supertypes(d)
            direct = [s for s in impl if S.name_of(s) in declared] if declared else impl
            out.append("supertypes: " + ", ".join(S.short(s) for s in direct[:8])
                       + (f" (+{len(impl) - len(direct)} indirect)" if len(impl) > len(direct)
                          else ""))
    return out + _javadoc(d)


def body_section(nav: Navigator, d: Definition, short: str) -> Section | None:
    if d.generated or d.enclosing is None:
        return None
    src = X.source(nav, d.symbol, max_lines=100_000)
    if src is None or not src.lines:
        return None
    width = len(str(src.lines[-1][0]))
    lines = [f"{n:>{width}}  {t.rstrip()[:LINE_CHARS]}" for n, t in src.lines]
    return Section("body", "body:", lines, f"source({short})", BODY_TAIL)


def _call_lines(nav: Navigator, nodes) -> tuple[list[str], int, int]:
    """One line per caller (its first call site and the call's text), non-test ones first."""
    wanted: dict[str, set[int]] = {}
    for n in nodes:
        if n.sites:
            r = n.sites[0]
            wanted.setdefault(r.location.source, set()).add(r.location.range.start_line)
    texts = {src: nav.ws.source_lines(src, ls) for src, ls in wanted.items()}
    rows = []
    for n in nodes:
        if not n.sites:
            continue
        r = n.sites[0]
        test = X.is_test_reference(r)
        text = texts.get(r.location.source, {}).get(r.location.range.start_line, "")
        extra = f"  (+{len(n.sites) - 1} more calls)" if len(n.sites) > 1 else ""
        via = f"  via {S.short(n.via)}" if n.via else ""
        tag = "  [test]" if test else ""
        rows.append((test, f"  {render.container_label(n.symbol) or '(top level)'}  "
                           f"{r.location.source}:{r.location.line}: {_clip(text, 110)}"
                           f"{extra}{via}{tag}"))
    rows.sort(key=lambda x: x[0])
    files = len({n.sites[0].location.source for n in nodes if n.sites})
    tests = sum(1 for t, _ in rows if t)
    return [x for _, x in rows], files, tests


def callers_section(nav: Navigator, symbol: str, short: str
                    ) -> tuple[Section, set[str], list[Reference]]:
    """(section, their sources, the direct references: calls through an overridden method
    are listed, marked `via`, but are not references to this symbol)."""
    root = nav.callers(symbol, 1, include_overridden=S.is_method(symbol), max_nodes=2000)
    if not root.children:
        return Section("callers", "callers: none in this project"), set(), []
    lines, files, tests = _call_lines(nav, root.children)
    # A caller may appear twice: calling this method and, `via`, one it overrides.
    distinct = len({n.symbol for n in root.children})
    title = (f"callers ({distinct} in {files} files"
             + (f", {tests} in tests" if tests else "")
             + ("; truncated" if root.truncated else "") + "):")
    srcs = {n.sites[0].location.source for n in root.children if n.sites}
    refs = [r for n in root.children if not n.via for r in n.sites]
    return Section("callers", title, lines, f"callers({short})"), srcs, refs


def callees_section(nav: Navigator, symbol: str, short: str) -> Section | None:
    _d, callees = nav.callees(symbol)
    if not callees:
        return None
    defs = nav._definitions_of({c.symbol for c in callees})
    lines = []
    for c in callees:
        d = defs.get(c.symbol)
        if d is not None:
            sig = signature_line(d.info)
            lines.append(f"  {S.short(c.symbol)}  {render.loc(d)}" + (f"  — {_clip(sig, 110)}"
                                                                       if sig else ""))
        else:
            lines.append(f"  {S.short(c.symbol)}  (external)")
    return Section("callees", f"callees ({len(callees)}):", lines, f"callees({short})")


def overridden_by_section(nav: Navigator, symbol: str, short: str) -> Section | None:
    impls = nav.implementations(symbol)
    if not impls:
        return None
    lines = []
    for i in impls:
        sig = signature_line(i.definition.info)
        lines.append(f"  {S.short(i.definition.symbol)}  {render.loc(i.definition)}"
                     + ("" if i.direct else "  (indirect)")
                     + (f"  — {_clip(sig, 100)}" if sig else ""))
    return Section("family", f"overridden by ({len(impls)}):", lines,
                   f"implementations({short})")


def overload_count(nav: Navigator, d: Definition) -> int:
    owner, name = S.parent(d.symbol), S.name_of(d.symbol)
    n = 0
    for v in nav._views(nav.ws.shard_for_source(d.location.source)):
        n = len(v.definitions(lambda sym, _i: not S.is_local_like(sym) and S.is_method(sym)
                              and S.parent(sym) == owner and S.name_of(sym) == name))
    return n


def members_section(nav: Navigator, d: Definition, short: str) -> Section | None:
    defs = [x for x in nav.outline(d.location.source)
            if S.parent(x.symbol) == d.symbol and not S.is_local_like(x.symbol)]
    if not defs:
        return None
    lines = []
    for x in defs:
        sig = signature_line(x.info) or S.short(x.symbol)
        lines.append(f"  {x.location.line:>5}  {x.kind}: {_clip(sig, 120)}"
                     + ("  (generated)" if x.generated else ""))
    return Section("members", f"members ({len(defs)}):", lines, f"members({short})")


def subtypes_section(nav: Navigator, symbol: str, short: str) -> Section | None:
    impls = nav.implementations(symbol)
    if not impls:
        return None
    lines = [f"  {S.short(i.definition.symbol)}  {render.loc(i.definition)}"
             + ("" if i.direct else "  (indirect)") for i in impls]
    return Section("subtypes", f"subtypes ({len(impls)}, "
                               f"{sum(1 for i in impls if i.direct)} direct):", lines,
                   f"implementations({short})")


def users_section(refs: list[Reference], short: str) -> Section | None:
    if not refs:
        return None
    by_file: dict[str, int] = {}
    for r in refs:
        by_file[r.location.source] = by_file.get(r.location.source, 0) + 1
    lines = [f"  {src}  {n}" for src, n in sorted(by_file.items(), key=lambda kv: (-kv[1],
                                                                                   kv[0]))]
    return Section("users", f"used in {len(by_file)} files ({len(refs)} references, imports "
                            "left out):", lines, f"references({short}) or typed({short})")


_ASSIGN_AFTER = re.compile(r"\s*(?:=(?!=)|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>>=|>>=|\+\+|--)")
_INCREMENT_BEFORE = re.compile(r"(?:\+\+|--)\s*(?:this\s*\.\s*)?$")


def is_write_at(line: str, start: int, end: int, next_line: str = "") -> bool:
    """Whether the reference at [start, end) of `line` is assigned: `x = …`, `x += …`, `x++`,
    `++x` (`this.x` too), also with the operator on the next line. Only this occurrence counts:
    in `this.a = other.a;` the second `a` is a read."""
    after = line[end:]
    if not after.strip():
        after = next_line
    return _ASSIGN_AFTER.match(after) is not None or \
        _INCREMENT_BEFORE.search(line[:start]) is not None


def _collapse(rows: list[str]) -> list[str]:
    """Identical rows (two uses on one line) once, with "(×n)"."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r] = counts.get(r, 0) + 1
    return [r + (f"  (×{n})" if n > 1 else "") for r, n in counts.items()]


def accessor_symbols(field_symbol: str) -> list[str]:
    """Bean accessors (Lombok's too), a `boolean isX` field's `isX()`/`setX()`, and a record
    component's or fluent accessor `name()`."""
    owner, name = S.parent(field_symbol), S.name_of(field_symbol)
    cap = name[:1].upper() + name[1:]
    out = [f"{owner}get{cap}().", f"{owner}is{cap}().", f"{owner}set{cap}().",
           f"{owner}{name}()."]
    if re.match(r"is[A-Z]", name):
        out.append(f"{owner}set{name[2:]}().")
    return out


def field_sections(nav: Navigator, d: Definition, short: str
                   ) -> tuple[list[Section], list[Reference], list[str]]:
    accessors = nav._definitions_of(set(accessor_symbols(d.symbol)))
    # A record component's accessor is implicit: scip-javac gives it no definition.
    implicit = X.record_accessor(nav, d.symbol, [d])
    implicit = implicit if implicit and implicit not in accessors else ""
    sections = []
    if accessors or implicit:
        lines = [f"  {S.short(a.symbol)}  {render.loc(a)}" + ("  (generated)" if a.generated
                                                               else "")
                 for a in sorted(accessors.values(), key=lambda a: a.symbol)]
        if implicit:
            lines.append(f"  {S.short(implicit)}  (implicit: a record component)")
        sections.append(Section("accessors", f"accessors ({len(lines)}):", lines))
    refs_by = nav._references_many({d.symbol} | set(accessors) | ({implicit} if implicit
                                                                   else set()))
    refs = [r for rs in refs_by.values() for r in rs]
    wanted: dict[str, set[int]] = {}
    for r in refs:  # each line and the next (an assignment may continue there)
        wanted.setdefault(r.location.source, set()).update(
            (r.location.range.start_line, r.location.range.start_line + 1))
    raw = {src: X._raw_lines(nav.ws.root, src, ls) for src, ls in wanted.items()}
    writes, reads = [], []
    for r in sorted(refs, key=lambda r: (r.location.source, r.location.range.start_line,
                                         r.location.range.start_char)):
        lines = raw.get(r.location.source, {})
        ln = r.location.range.start_line
        line = lines.get(ln, "")
        text = line.strip()
        accessor = r.symbol != d.symbol
        # A setter writes; a record's implicit accessor (`settings()`) only reads.
        write = (r.symbol != implicit and re.match(r"set[A-Z_$]", S.name_of(r.symbol))
                 is not None) if accessor else is_write_at(
            line, r.location.range.start_char, r.location.range.end_char, lines.get(ln + 1, ""))
        row = (f"  {render.container_label(r.container) or '(top level)'}  "
               f"{r.location.source}:{r.location.line}: {_clip(text, 110)}"
               + (f"  ({S.name_of(r.symbol)})" if accessor else ""))
        (writes if write else reads).append(row)
    n_writes, n_reads = len(writes), len(reads)  # uses (rows collapse two on one line)
    writes, reads = _collapse(writes), _collapse(reads)
    if writes:
        sections.append(Section("writes", f"writes ({n_writes}; told apart by the line's "
                                          "text, a heuristic — the index has no read/write "
                                          "roles):", writes, f"references({short})"))
    if reads:
        sections.append(Section("reads", f"reads ({n_reads}):", reads,
                                f"references({short})"))
    return sections, refs, sorted(accessors)

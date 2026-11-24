"""Compact text rendering for the tools: paths relative to the project root, 1-based lines,
and the line of code where it helps. Written for agents: few tokens, no decoration."""

from __future__ import annotations

import os

import re
import time
from collections import OrderedDict
from typing import Sequence, TypeVar

from . import symbols as S
from .queries import (
    CallNode,
    Callee,
    Definition,
    Implementation,
    Reference,
    Resolution,
    Supertype,
    declared_supertypes,
    signature_line,
)
from .workspace import Freshness, Workspace

MAX_LISTED_FILES = 5
SUMMARY_OVER = 300  # references: above this, open with a summary


def _summary(symbol: str, refs) -> list[str]:
    by_file: OrderedDict[str, int] = OrderedDict()
    by_dir: OrderedDict[str, int] = OrderedDict()
    for r in refs:
        by_file[r.location.source] = by_file.get(r.location.source, 0) + 1
        top = r.location.source.split("/", 1)[0]
        by_dir[top] = by_dir.get(top, 0) + 1
    top_files = sorted(by_file.items(), key=lambda kv: -kv[1])[:5]
    hint = ("typed(categories=[...]) to see one kind of use, callers to group by method"
            if S.is_type(symbol) else "callers to group them by method")
    return [
        "summary: by top directory " + ", ".join(
            f"{d} {n}" for d, n in sorted(by_dir.items(), key=lambda kv: -kv[1])[:8]),
        "  most uses: " + ", ".join(f"{f.rsplit('/', 1)[-1]} {n}" for f, n in top_files),
        f"  narrow it: {hint}, or page with offset/limit",
    ]
T = TypeVar("T")


def page(items: Sequence[T], offset: int, limit: int) -> tuple[Sequence[T], str]:
    """(items on this page, header suffix such as " (showing 1–100; next offset 100)")."""
    offset, limit = max(0, offset), max(1, limit)
    shown = items[offset:offset + limit]
    total = len(items)
    if not offset and len(shown) == total:
        return shown, ""
    if not shown:
        return shown, f" (offset {offset} is past the end)"
    end = offset + len(shown)
    return shown, f" (showing {offset + 1}–{end}" + (f"; next offset {end})" if end < total
                                                      else ")")


def loc(d: Definition) -> str:
    return f"{d.location.source}:{d.location.line}"


def definition_row(d: Definition, full_symbol: bool = True) -> str:
    name = d.symbol if full_symbol else S.short(d.symbol)
    if d.container and d.info and d.info.display_name:
        name = f"{d.info.display_name} (anonymous class member, in {S.short(d.container)})"
    sig = signature_line(d.info)
    tail = "  (generated)" if d.generated else ""
    return f"{loc(d)}  {d.kind}  {name}" + (f"  — {sig}" if sig else "") + tail


def candidates(r: Resolution, limit: int = 30) -> str:
    lines = [r.note]
    cands = sorted(r.candidates, key=lambda d: (d.location.source, d.location.range.start_line))
    owners = {S.parent(d.symbol) for d in cands}
    if len(cands) > limit:
        modules: OrderedDict[str, int] = OrderedDict()
        for d in cands:
            m = d.location.source.split("/", 1)[0]
            modules[m] = modules.get(m, 0) + 1
        lines.append(f"  in {len(owners)} types; by top directory: "
                     + ", ".join(f"{m} {n}" for m, n in sorted(modules.items(),
                                                              key=lambda kv: -kv[1])[:8]))
    if cands and len(owners) > 1 and len(S.parse_query(r.note.split("'")[1] if "'" in r.note
                                                       else "x").names) == 1:
        ex = S.short(cands[0].symbol).split("(")[0]
        lines.append(f"  tip: add the owner type to narrow it, e.g. `{ex}`")
    r = Resolution(r.symbol, cands, r.note, r.externals)
    for d in r.candidates[:limit]:
        lines.append("  " + definition_row(d))
    if len(r.candidates) > limit:
        lines.append(f"  … and {len(r.candidates) - limit} more")
    for e in r.externals[:limit]:
        lines.append(f"  external  {e}")
    if len(r.externals) > limit:
        lines.append(f"  … and {len(r.externals) - limit} more external")
    return "\n".join(lines)


def find(text: str, found: list[Definition], offset: int, limit: int, how: str = "") -> str:
    if not found:
        return f"no symbol defined in this project matches {text!r}" + (
            f" ({how})" if how else "; try match='contains'")
    shown, suffix = page(found, offset, limit)
    lines = [f"{len(found)} definition{'s' if len(found) != 1 else ''} match {text!r}"
             + (f" ({how})" if how else "") + suffix]
    lines += [definition_row(d) for d in shown]
    return "\n".join(lines)


def container_label(symbol: str) -> str:
    if not symbol:
        return ""
    k = S.kind_of(symbol)
    if k == "type":
        return f"{S.short(symbol)} initializer"
    return S.short(symbol)


def references(ws: Workspace, symbol: str, refs: list[Reference], defs: list[Definition],
               offset: int, limit: int, include_definitions: bool,
               show_containers: bool = True) -> str:
    files = {r.location.source for r in refs}
    shown, suffix = page(refs, offset, limit)
    head = (f"{S.short(symbol)} — {len(refs)} reference{'s' if len(refs) != 1 else ''} "
            f"in {len(files)} file{'s' if len(files) != 1 else ''}{suffix}")
    lines = [head]
    if len(refs) > SUMMARY_OVER and offset == 0:
        lines += _summary(symbol, refs)
    if include_definitions:
        for d in defs:
            lines.append(f"defined at {loc(d)}" + ("  (generated)" if d.generated else ""))
    by_file: OrderedDict[str, list[Reference]] = OrderedDict()
    for r in shown:
        by_file.setdefault(r.location.source, []).append(r)
    for source, rs in by_file.items():
        lines.append(source)
        text = ws.source_lines(source, {r.location.range.start_line for r in rs})
        rows: OrderedDict[int, list[Reference]] = OrderedDict()
        for r in rs:
            rows.setdefault(r.location.range.start_line, []).append(r)
        for line, group in rows.items():
            mult = f" (×{len(group)})" if len(group) > 1 else ""
            where = ""
            if show_containers:
                c = container_label(group[0].container)
                if group[0].via_local:
                    c += f" › {group[0].via_local}"
                where = f"  [{c}]" if c else ""
            lines.append(f"  {line + 1}: {text.get(line, '')[:160]}{mult}{where}")
    return "\n".join(lines)


def definition(ws: Workspace, d: Definition | None, symbol: str) -> str:
    if d is None:
        return (f"{symbol} is not defined in this project (external, e.g. JDK or a dependency, "
                "or its file was not compiled)")
    head = f"{d.kind} at {loc(d)}"
    if d.generated_by is not None:
        node = ws.source_lines(d.location.source, [d.generated_by.start_line]).get(
            d.generated_by.start_line, "")
        head += f"  (generated by: {node[:100]})" if node else "  (generated)"
    elif d.generated:
        head += "  (generated)"
    elif d.enclosing is not None:
        head += f", body {d.enclosing.start_line + 1}–{d.enclosing.end_line + 1}"
    lines = [d.symbol, head]
    if d.container:
        lines.append(f"anonymous class member, in {d.container}")
    if d.info:
        if d.info.signature:
            lines.append(d.info.signature.strip())
        if d.info.enclosing_symbol and not S.is_local(d.info.enclosing_symbol):
            lines.append(f"enclosed by {d.info.enclosing_symbol}")
        impl = [r.symbol for r in d.info.relationships if r.is_implementation]
        if impl and S.is_method(d.symbol):
            lines.append("overrides: " + ", ".join(S.short(s) for s in impl))
        elif impl:
            declared = declared_supertypes(d)
            direct = [s for s in impl if S.name_of(s) in declared] if declared else impl
            rest = [s for s in impl if s not in direct]
            lines.append("supertypes: " + ", ".join(S.short(s) for s in direct)
                         + (f"; indirect ({len(rest)}): " + ", ".join(S.short(s) for s in rest[:8])
                            + (" …" if len(rest) > 8 else "") if rest else ""))
        for doc in d.info.documentation:
            doc = doc.strip()
            if doc:
                lines.append("")
                lines.append(doc)
    return "\n".join(lines)


def implementations(symbol: str, impls: list[Implementation], direct_only: bool,
                    offset: int, limit: int) -> str:
    items = [i for i in impls if i.direct] if direct_only else impls
    n_direct = sum(1 for i in impls if i.direct)
    noun = "overrides" if S.is_method(symbol) else "subtypes"
    if not impls:
        return f"no {noun} of {S.short(symbol)} in this project"
    shown, suffix = page(items, offset, limit)
    lines = [f"{S.short(symbol)} — {len(impls)} {noun} ({n_direct} direct){suffix}"]
    for i in shown:
        row = definition_row(i.definition, full_symbol=False)
        if not i.direct:
            row += "  via " + ", ".join(S.short(v) for v in i.via[:3])
        lines.append(row)
    return "\n".join(lines)


def supertypes(symbol: str, d: Definition | None, supers: list[Supertype]) -> str:
    if d is None:
        return (f"{symbol} is not defined in this project (external, e.g. JDK or a dependency, "
                "or its file was not compiled)")
    noun = "overrides" if S.is_method(symbol) else "supertypes"
    if not supers:
        return f"{S.short(symbol)} has no {noun} recorded (besides java.lang.Object)"
    lines = [f"{S.short(symbol)} — {noun}, direct first"]
    for s in supers:
        mark = "direct  " if s.direct else "indirect"
        where = f"{loc(s.definition)}  " if s.definition else "external  "
        sig = signature_line(s.definition.info) if s.definition else ""
        lines.append(f"{mark}  {where}{s.symbol}" + (f"  — {sig}" if sig else ""))
    return "\n".join(lines)


def outline(source: str, defs: list[Definition], offset: int, limit: int) -> str:
    shown, suffix = page(defs, offset, limit)
    lines = [f"{source} — {len(defs)} definitions{suffix}"]
    for d in shown:
        depth = max(0, sum(1 for x in S.parse(d.symbol) if x.suffix == S.TYPE)
                    - (1 if S.is_type(d.symbol) else 0))
        sig = signature_line(d.info) or S.short(d.symbol)
        gen = "  (generated)" if d.generated else ""
        lines.append(f"{d.location.line:>5}  {'  ' * depth}{d.kind}: {sig}{gen}")
    return "\n".join(lines)


def callers(root: CallNode, depth: int, offset: int, limit: int) -> str:
    total_nodes = 0
    total_sites = 0

    def count(n: CallNode):
        nonlocal total_nodes, total_sites
        for c in n.children:
            total_nodes += 1
            total_sites += len(c.sites)
            count(c)

    count(root)
    if not root.children:
        return f"no callers of {S.short(root.symbol)} in this project"
    distinct = set()

    def collect(n: CallNode):
        for c in n.children:
            distinct.add(c.symbol)
            collect(c)

    collect(root)
    shown, suffix = page(root.children, offset, limit)
    nodes = f" ({total_nodes} tree nodes)" if total_nodes != len(distinct) else ""
    lines = [f"callers of {S.short(root.symbol)} (depth {depth}): {len(distinct)} distinct "
             f"callers{nodes}, {total_sites} call sites; {len(root.children)} direct{suffix}"
             " — lines are the calls"]

    def row(c: CallNode, indent: int):
        sites = c.sites
        src = sites[0].location.source if sites else ""
        nums = ", ".join(str(s.location.line) + (f" (in {s.via_local})" if s.via_local else "")
                         for s in sites)
        label = S.short(c.symbol)
        kind = S.kind_of(c.symbol)
        if kind == "type":
            label += " (initializer block)"
        elif kind == "term":
            label += " (field initializer)"
        via = f"  via {S.short(c.via)}" if c.via else ""
        lines.append(f"{'  ' * indent}{label}  {src}:{nums}{via}")
        for g in c.children:
            row(g, indent + 1)
        if c.truncated:
            lines.append(f"{'  ' * (indent + 1)}… truncated (max_nodes)")

    for c in shown:
        row(c, 0)
    if root.truncated:
        lines.append("… truncated (max_nodes)")
    return "\n".join(lines)


def callees(symbol: str, d: Definition | None, out: list[Callee], offset: int,
            limit: int) -> str:
    if d is None:
        return (f"{symbol} is not defined in this project (external, e.g. JDK or a dependency, "
                "or its file was not compiled)")
    if not out:
        return f"{S.short(symbol)} references no methods{'' if d.enclosing else ' (no body range)'}"
    shown, suffix = page(out, offset, limit)
    lines = [f"{S.short(symbol)} ({loc(d)}) — {len(out)} callees{suffix}"]
    for c in shown:
        lines.append(f"  {S.short(c.symbol)}  " + ", ".join(str(s.line) for s in c.sites))
    return "\n".join(lines)


def freshness(f: Freshness | None, result_sources: set[str] | None = None) -> str:
    if not f:
        return ""
    lines = []
    if f.recent:
        from .workspace import RECENT_SECONDS
        n = f"{f.recent} shard{'s' if f.recent != 1 else ''}"
        at = time.strftime("%H:%M:%S", time.localtime(time.time() - f.newest_age))
        if f.build_running is False:
            lines.append(f"note: {n} written in the last {RECENT_SECONDS} s (the newest at {at}, "
                         f"{f.newest_age:.0f} s ago) and no gradle/mvn command runs in this "
                         "checkout: a compilation probably just finished (builds started from "
                         "an IDE are not seen)")
        elif f.build_running:
            lines.append(f"WARNING: a gradle/mvn command is running in this checkout and {n} "
                         f"were written in the last {RECENT_SECONDS} s: results may be "
                         "incomplete (shards being written lose references and subtypes "
                         "without an error); retry when it finishes")
        else:
            lines.append(f"WARNING: {n} written in the last {RECENT_SECONDS} s (the newest at "
                         f"{at}): if a compilation is running in this checkout, results may be incomplete "
                         "(shards being written lose references and subtypes without an "
                         "error); retry when it finishes")
    if f.stale:
        hit = sorted(set(f.stale) & result_sources) if result_sources else []
        lines.append(f"WARNING: {len(f.stale)} source{'s' if len(f.stale) != 1 else ''} changed "
                     f"after their last compilation (results reflect the compiled version): "
                     + _some(f.stale))
        if hit:
            lines.append(f"  of them, in these results: {_some(hit)}")
    if f.uncompiled:
        lines.append(f"WARNING: {len(f.uncompiled)} .java file{'s' if len(f.uncompiled) != 1 else ''} "
                     "created after the last compilation of their module, not indexed: "
                     + _some(f.uncompiled))
    tasks = sorted(t for t in f.tasks if t)
    if tasks:
        lines.append("  recompile to refresh: " + ", ".join(tasks[:6])
                     + (" …" if len(tasks) > 6 else "") + " (scipq-mcp does not build; if "
                     "the task comes FROM-CACHE the shards are not rewritten: --rerun it)")
    elif f.stale or f.uncompiled:
        lines.append("  recompile the project to refresh (scipq-mcp does not build)")
    return "\n".join(lines)


def _some(items: list[str]) -> str:
    more = f" (+{len(items) - MAX_LISTED_FILES} more)" if len(items) > MAX_LISTED_FILES else ""
    return ", ".join(items[:MAX_LISTED_FILES]) + more


# --- extras ------------------------------------------------------------------------------------

def source(src, symbol: str) -> str:
    if src is None:
        return (f"{symbol} is not defined in this project (external, e.g. JDK or a dependency, "
                "or its file was not compiled)")
    d = src.definition
    head = f"{d.symbol} — {loc(d)}"
    if d.generated:
        head += "  (generated: no source body; this is the node that generates it)"
    out = [head]
    width = len(str(src.lines[-1][0])) if src.lines else 1
    out += [f"{n:>{width}}  {t}" for n, t in src.lines]
    if src.truncated:
        out.append(f"… truncated; the body ends at line {src.end_line} (raise max_lines)")
    return "\n".join(out)


def members(type_symbol: str, groups, external: list[str], offset: int, limit: int,
            libs=None) -> str:
    rows: list[str] = []
    own = groups[0]
    for g in groups:
        if g is own:
            rows.append(f"own ({len(g.members)}):")
        else:
            hidden = f", {g.hidden} overridden" if g.hidden else ""
            where = f"  {loc(g.definition)}" if g.definition else ""
            rows.append(f"from {S.short(g.owner)} ({len(g.members)}{hidden}):{where}")
        for m in g.members:
            sig = signature_line(m.info) or S.short(m.symbol)
            extra = []
            if m.generated:
                extra.append("generated")
            over = g.overrides.get(m.symbol)
            if over:
                extra.append("overrides " + ", ".join(S.short(o) for o in over[:2]))
            tail = f"  ({'; '.join(extra)})" if extra else ""
            rows.append(f"  {m.location.line:>5}  {m.kind}: {sig}{tail}")
    total = sum(len(g.members) for g in groups)
    no_sources = []
    for ext, lib, mems, nhidden in libs or []:
        if lib is None:
            no_sources.append(ext)
            continue
        total += len(mems)
        hidden = f", {nhidden} overridden by nearer types" if nhidden else ""
        rows.append(f"from {S.short(ext)} ({len(mems)}{hidden}, library sources: "
                    f"{lib.coordinates}):")
        for m in mems:
            rows.append(f"  {m.line:>5}  {m.kind}: {_decl(m.declaration)}")
    if libs is None:
        no_sources = list(external)
    shown, suffix = page(rows, offset, limit)
    lines = [f"{S.short(type_symbol)} — {len(own.members)} own members, "
             f"{total - len(own.members)} inherited{suffix}"]
    lines += list(shown)
    if no_sources:
        lines.append("external supertypes without sources (no -sources.jar in the Gradle/Maven "
                     "caches): " + ", ".join(S.short(e) for e in no_sources))
    return "\n".join(lines)


def _decl(declaration: str) -> str:
    """A declaration without its annotations, for listings."""
    out = re.sub(r"@\s*(?!interface\b)[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?\s*", "",
                 declaration).strip()
    return out[:200]


def library_definition(lib) -> str:
    m = lib.member
    where = f"{lib.lib.coordinates}" + (f" (+{lib.lib.others} other versions cached)"
                                         if lib.lib.others else "")
    lines = [f"{lib.symbol}", f"external — {where}",
             f"source: {lib.lib.jar}!/{lib.lib.entry}:{m.line if m else lib.body[0]} "
             "(read it with `source`)"]
    if getattr(lib, "note", ""):
        lines.append(f"note: {lib.note}")
    if m is None:
        lines.append(_decl(lib.type_declaration))
        doc = lib.type_javadoc
    else:
        lines.append(f"in: {_decl(lib.type_declaration)}")
        lines.append(f"{m.kind}: {_decl(m.declaration)}")
        doc = m.javadoc
        if len(lib.overloads) > 1:
            lines.append(f"overloads ({len(lib.overloads)}; SCIP numbers them in declaration "
                         "order, which this one assumes):")
            for o in lib.overloads:
                mark = "→" if o is m else " "
                lines.append(f"  {mark} {o.line}: {_decl(o.declaration)}")
    if doc:
        lines.append("")
        lines.append(doc)
    return "\n".join(lines)


def library_source(lib, max_lines: int) -> str:
    first, last = lib.body
    shown_last = min(last, first + max(1, max_lines) - 1)
    head = (f"{lib.symbol} — {lib.lib.coordinates} {lib.lib.entry}:{first}"
            + (f" (+{lib.lib.others} other versions cached)" if lib.lib.others else ""))
    width = len(str(shown_last))
    body = [f"{first + k:>{width}}  {t}" for k, t in enumerate(lib.source.lines(first, shown_last))]
    out = [head] + body
    if shown_last < last:
        out.append(f"… truncated; the body ends at line {last} (raise max_lines)")
    return "\n".join(out)


def annotated(annotation: str, items, offset: int, limit: int) -> str:
    if not items:
        return f"nothing in this project is annotated with {S.short(annotation)}"
    shown, suffix = page(items, offset, limit)
    lines = [f"@{S.short(annotation)} — {len(items)} uses{suffix}"]
    for a in shown:
        if a.definition is not None:
            d = a.definition
            name = S.short(d.symbol) if not S.is_local(d.symbol) else (
                (d.info.display_name if d.info else d.symbol) + f" in {S.short(d.container)}")
            lines.append(f"{a.location.source}:{a.location.line}  {d.kind} {name}  — {a.text}")
        else:
            lines.append(f"{a.location.source}:{a.location.line}  — {a.text}")
    return "\n".join(lines)


def typed(type_symbol: str, uses, categories: list[str] | None, offset: int, limit: int,
          filters: str = "") -> str:
    if categories:
        uses = [u for u in uses if u.category in categories]
    seen, unique = set(), []
    for u in uses:  # a declaration that mentions the type twice (e.g. `Map<…> m = Map.of()`)
        key = (u.category, u.owner, u.location.source, u.location.range.start_line)
        if key not in seen:
            seen.add(key)
            unique.append(u)
    uses = unique
    if not uses:
        what = (", ".join(categories) + " " if categories else "") + "uses"
        return f"no {what} of {S.short(type_symbol)}{filters or ' in this project'}"
    counts = OrderedDict()
    for u in uses:
        counts[u.category] = counts.get(u.category, 0) + 1
    shown, suffix = page(uses, offset, limit)
    lines = [f"{S.short(type_symbol)} — {len(uses)} uses: "
             + ", ".join(f"{c} {n}" for c, n in counts.items()) + suffix]
    current = None
    for u in shown:
        if u.category != current:
            current = u.category
            lines.append(f"{current}:")
        owner = f"{S.short(u.owner)}  " if u.owner and u.category != "import" else ""
        via = f"[{u.via}]  " if u.via else ""
        lines.append(f"  {u.location.source}:{u.location.line}  {via}{owner}— "
                     f"{u.detail[:140]}")
    return "\n".join(lines)


def family(method: str, fam, ws: Workspace, offset: int, limit: int,
           show_uses: bool = True) -> str:
    if not fam:
        return f"{S.short(method)}: no override family found"
    total_uses = sum(len(m.uses) for m in fam)
    lines = [f"{S.short(method)} — family of {len(fam)} methods, {total_uses} uses in total"]
    for m in fam:
        where = loc(m.definition) if m.definition else (m.where or "external")
        sig = signature_line(m.definition.info) if m.definition else _decl(m.declaration)
        lines.append(f"  {m.relation:<10} {member_name(m)}  {where}  {len(m.uses)} uses"
                     + (f"  — {sig}" if sig else ""))
    if show_uses:
        flat = [(m, r) for m in fam for r in m.uses]
        shown, suffix = page(flat, offset, limit)
        lines.append(f"uses{suffix}:")
        texts: dict[str, dict[int, str]] = {}
        for _m, r in shown:
            texts.setdefault(r.location.source, set()).add(r.location.range.start_line)
        texts = {s: ws.source_lines(s, ls) for s, ls in texts.items()}
        current = None
        for m, r in shown:
            if m.symbol != current:
                current = m.symbol
                lines.append(f"  {member_name(m)}:")
            t = texts[r.location.source].get(r.location.range.start_line, "")
            lines.append(f"    {r.location.source}:{r.location.line}: {t[:140]}"
                         + (f"  [{container_label(r.container)}]" if r.container else ""))
    return "\n".join(lines)


def member_name(m) -> str:
    d = m.definition
    if d is not None and S.is_local(m.symbol):
        name = d.info.display_name if d.info and d.info.display_name else m.symbol
        return f"{name} (anonymous class, in {S.short(d.container)})" if d.container else name
    return S.short(m.symbol)


def overloads(method: str, items, ws: Workspace, offset: int, limit: int) -> str:
    if not items:
        return f"no overloads of {S.short(method)} found"
    return family(method, items, ws, offset, limit).replace(
        f"— family of {len(items)} methods", f"— {len(items)} overloads", 1)


def by_name(hits, names: list[str]) -> str:
    if not hits:
        return f"possible uses by name ({', '.join(names)}): none"
    lines = [f"possible uses by name, not in the index — check by hand ({', '.join(names)}): "
             f"{len(hits)}"]
    for h in hits:
        lines.append(f"  {h.source}:{h.line}: {h.text}")
    return "\n".join(lines)


def status(st, root: str) -> str:
    lines = [f"index of {root}: {st.shards} shards in {len(st.tasks)} compile tasks"]
    for task, n in st.tasks:
        lines.append(f"  {task}: {n}")
    if st.orphans:
        lines.append(f"orphan shards (source deleted, ignored): {len(st.orphans)}")
    lines.append(f"stale (source edited after its shard): {len(st.stale)}"
                 + (": " + _some(st.stale) if st.stale else ""))
    if getattr(st, "same_content", 0):
        lines.append(f"  not counted: {st.same_content} files newer than their shard but with "
                     "the content they were compiled from (written again unchanged; the "
                     "build is up to date)")
    lines.append(f"new since the last compilation, not indexed: {len(st.uncompiled)}"
                 + (": " + _some(st.uncompiled) if st.uncompiled else ""))
    if st.without_shard is None:
        lines.append("files without a shard: unknown (not a git repository)")
    else:
        lines.append(f"git-tracked .java files without a shard: {len(st.without_shard)}"
                     + (": " + _some(st.without_shard) if st.without_shard else "")
                     + (" (some may be legitimately excluded: not in a source set, or "
                        "empty/commented out)" if st.without_shard else ""))
    if getattr(st, "settings", None) is not None:
        lines += settings_lines(st.settings)
    refresh = [t for t in st.refresh if t]
    if refresh:
        lines.append("recompile to refresh: " + ", ".join(refresh)
                     + " (scipq-mcp does not build; a task that comes FROM-CACHE does not "
                     "rewrite the shards: --rerun it)")
    elif st.stale or st.uncompiled:
        lines.append("recompile the project to refresh (scipq-mcp does not build)")
    return "\n".join(lines)


def _module_and_test_task(task_dir: str) -> tuple[str, str]:
    """("a:b", ":a:b:test") for "a/b/compileTestJava"; ("", ":test") for the root project
    ("_root/…"); ("core", ":core:integrationTest") for compileIntegrationTestJava; for Maven
    ("app/testCompile") the module directory and "test"; for one shared directory ("", "")."""
    from .extras import test_task
    from .workspace import build_task
    path = build_task(task_dir)
    if path.startswith(":"):
        return path.rpartition(":")[0].lstrip(":"), test_task(task_dir)
    if task_dir.endswith("/testCompile") or task_dir.endswith("/compile"):
        mod = task_dir.rpartition("/")[0]
        return ("" if mod in (".", "_root") else mod), "test"
    return "", ""


def test_commands(by_task: dict[str, list[str]], gradle: str = "gradle",
                  template: str = "") -> list[str]:
    """Commands to run test classes, grouped by the "<module>/<task>" of their shards."""
    out = []
    for task_dir, classes in by_task.items():
        if template:
            out.append(format_test_filter(template, task_dir, classes))
            continue
        mod, task = _module_and_test_task(task_dir)
        filters = " ".join(f"--tests '{c}'" for c in classes)
        if task.startswith(":"):
            out.append(f"{gradle} {task} {filters}")
        elif task_dir.endswith("/testCompile"):
            names = ",".join(c.rsplit(".", 1)[-1] for c in classes)
            out.append("mvn " + (f"-pl {mod} " if mod else "") + f"test -Dtest='{names}'")
        elif not task:
            out.append(f"{gradle} test {filters}   (use the test task of your build)")
        else:
            out.append(f"{task_dir}: " + " ".join(classes))
    return out


def format_test_filter(template: str, task_dir: str, classes: list[str]) -> str:
    """A configured test command: {module} (`a:b`, "" for the root project), {task}
    (`:a:b:test`, `:core:integrationTest`); per class {fqcn} / {simple} (one line per class);
    or, for one line per module, {classes} (FQCNs separated by spaces) or {tests}
    (`--tests 'A' --tests 'B'`)."""
    mod, task = _module_and_test_task(task_dir)
    task = task or "test"
    tests = " ".join(f"--tests '{c}'" for c in classes)
    if "{fqcn}" in template or "{simple}" in template:
        return "\n  ".join(template.format(module=mod, task=task, fqcn=c, tests=f"--tests '{c}'",
                                            simple=c.rsplit(".", 1)[-1], classes=c)
                           for c in classes)
    return template.format(module=mod, task=task, classes=" ".join(classes), tests=tests,
                           fqcn="", simple="")


def tests(symbol: str, uses, task_of, gradle: str = "gradle", template: str = "") -> str:
    if not uses:
        return f"no test class uses {S.short(symbol)} directly"
    lines = [f"{S.short(symbol)} — used directly by {len(uses)} test classes"]
    for t in uses:  # (task_of is kept for callers; the commands come from test_commands)
        lines.append(f"  {t.test_class}  {t.source}:" + ", ".join(map(str, t.lines)))
    lines.append("run them (scipq-mcp does not):")
    groups: OrderedDict[str, list[str]] = OrderedDict()
    for t in uses:
        groups.setdefault(t.module, []).append(t.test_class)
    lines += ["  " + c for c in test_commands(groups, gradle, template)]
    return "\n".join(lines)


# --- v2 (auxiliary index) ----------------------------------------------------------------------

def index_note(stats) -> str:
    if stats is None or not stats.processed and not stats.removed:
        return ""
    if getattr(stats, "unreadable", 0):
        return (index_note_plain(stats) + f"-- {stats.unreadable} shards could not be read "
                "(probably being written by a compilation); they are retried next time\n")
    return index_note_plain(stats)


def index_note_plain(stats) -> str:
    if stats.built_from_scratch:
        return (f"-- index built: {stats.processed} shards in {stats.seconds:.0f} s (once per "
                "checkout; later updates are incremental)\n")
    return f"-- index updated: {stats.processed} shards, {stats.removed} removed\n"


def entry_points(entries, per_kind: int = 12, depth: int = 0, searched: int = 0) -> list[str]:
    """What a change reaches that someone starts from outside: screens, endpoints, jobs…,
    one row per type with its reached members. `depth`: the depth the rest of the output
    stops at; `searched`: how deep the entry points were looked for."""
    if not entries:
        return []
    by_kind: OrderedDict[str, OrderedDict[str, list]] = OrderedDict()
    for e in sorted(entries, key=lambda e: (e.kind, e.node.depth, e.node.source, e.node.line)):
        t = S.owner_type(e.node.symbol) or e.node.symbol
        by_kind.setdefault(e.kind, OrderedDict()).setdefault(t, []).append(e)

    def count(types) -> str:
        members = sum(len(v) for v in types.values())
        return (f"{len(types)} type{'s' if len(types) != 1 else ''}" +
                (f" ({members} members)" if members != len(types) else ""))
    kinds = sorted(by_kind.items(), key=lambda kv: -len(kv[1]))
    deeper = searched > depth and any(e.node.depth > depth for e in entries)
    lines = ["entry points reached (by kind" + (f", searched to depth {searched}; the rows "
                                                f"stop at {depth}" if deeper else "") + "): " +
             ", ".join(f"{k} {count(v)}" for k, v in kinds)
             + (" (tests: listed by tests(transitive=true))" if "test" in by_kind else "")]
    for kind, types in kinds:
        if kind == "test":
            continue
        for t, es in list(types.items())[:per_kind]:
            first = es[0]
            head = f"  {kind}  d{first.node.depth}  {S.short(t)}  {first.node.source}"
            if first.on_type:
                head += f"  {first.detail}"
            if all(S.kind_of(e.node.symbol) == "type" for e in es):
                lines.append(head)
                continue
            members: dict[str, list] = {}       # overloads once: name(+1)
            for e in es:
                members.setdefault(S.name_of(e.node.symbol), []).append(e)
            shown = []
            for name, group in list(members.items())[:6]:
                e = group[0]
                shown.append(name + (f"(+{len(group) - 1})" if len(group) > 1 else "()")
                             + (f" {e.detail}" if not e.on_type else "")
                             + (f" d{e.node.depth}" if e.node.depth != first.node.depth
                                else ""))
            lines.append(head + "  — " + ", ".join(shown)
                         + (f" (+{len(members) - 6})" if len(members) > 6 else ""))
        if len(types) > per_kind:
            lines.append(f"  {kind}  … and {len(types) - per_kind} more types")
    return lines


def library_callbacks(callbacks, deferred=()) -> str:
    """Methods a library calls (overrides of its methods): who creates their objects; and
    callers whose calls are all inside an anonymous class or a lambda."""
    lines = []
    if deferred:
        lines.append("\ncalled from inside an anonymous class or a lambda (it runs when "
                     "whoever receives that object calls it, often a library):")
        lines += [f"  {S.short(n.symbol)}  {n.source}:{', '.join(map(str, n.sites[:3]))}"
                  for n in deferred]
    if not callbacks:
        return "\n".join(lines)
    lines += ["\nlibrary callbacks (the library calls these; their callers are whoever creates "
              "the object and hands it over):"]
    for sym, lib, places, count, made, sub in callbacks:
        owner = S.short(made)
        where = ", ".join(f"{src}:{ln}" for src, ln in places) + (" …" if count > len(places)
                                                                  else "")
        made = (f"{owner}{' ' + sub if sub else ''} created at {count} "
                f"place{'s' if count != 1 else ''}: {where}"
                if count else f"no constructor of {owner} is called in the project (built by "
                "the framework, or through a no-arg constructor)")
        lines.append(f"  {S.short(sym)} overrides {S.short(lib)}: {made}")
    return "\n".join(lines)


def impact(symbol: str, nodes, truncated: bool, depth: int, group_by: str, offset: int,
           limit: int, entries=None, itself=None, searched: int = 0) -> str:
    """`entries` may come from deeper than `depth`; `itself`: the symbol's own entry points."""
    head = [f"{S.short(symbol)} is itself an entry point: {e.kind} {e.detail}"
            for e in itself or []]
    if not nodes:
        return "\n".join(head + [f"nothing in this project calls {S.short(symbol)}"])
    groups: OrderedDict[str, list] = OrderedDict()
    for n in nodes:
        key = S.package_of(n.symbol) if group_by == "package" else n.module
        groups.setdefault(key or "(root)", []).append(n)
    by_depth: OrderedDict[int, int] = OrderedDict()
    for n in nodes:
        by_depth[n.depth] = by_depth.get(n.depth, 0) + 1
    tests = sum(1 for n in nodes if n.test)
    lines = head + [f"impact of {S.short(symbol)} (callers up to depth {depth}): {len(nodes)} "
             f"definitions, {tests} in tests; by depth "
             + ", ".join(f"{d}: {c}" for d, c in by_depth.items())
             + ("; TRUNCATED" if truncated else "")
             + " — lines are where each caller is defined (callers shows the calls)"]
    lines.append(f"by {group_by}: " + ", ".join(
        f"{g} {len(ns)}" for g, ns in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:12]))
    lines += entry_points(entries, depth=depth, searched=searched)
    rows = []
    for g, ns in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rows.append(f"{g}:")
        for n in ns:
            test = "  [test]" if n.test else ""
            rows.append(f"  d{n.depth}  {S.short(n.symbol)}  {n.source}:{n.line}  "
                        f"→ {S.short(n.via)}{test}")
    shown, suffix = page(rows, offset, limit)
    if suffix:
        lines.append("rows" + suffix + ":")
    lines += list(shown)
    return "\n".join(lines)


def path(src: str, dst: str, paths) -> str:
    if not paths:
        return f"no call path from {S.short(src)} to {S.short(dst)} within the depth searched"
    lines = [f"{len(paths)} shortest call path{'s' if len(paths) != 1 else ''} from "
             f"{S.short(src)} to {S.short(dst)}:"]
    for k, p in enumerate(paths, 1):
        chain = []
        for sym, sites in p:
            at = f" (line {', '.join(map(str, sites[:3]))})" if sites else ""
            chain.append(S.short(sym) + at)
        lines.append(f"{k}. " + "  →  ".join(chain))
    return "\n".join(lines)


def tests_transitive(symbol: str, nodes, depth: int, task_of, gradle: str,
                     template: str = "") -> str:
    tests = [n for n in nodes if n.test]
    if not tests:
        return f"no test reaches {S.short(symbol)} within depth {depth}"
    classes: OrderedDict[tuple[str, str], list] = OrderedDict()
    for n in tests:
        top = S.top_level_type_name(n.symbol)
        fqcn = (S.package_of(n.symbol) + "." if S.package_of(n.symbol) else "") + top
        classes.setdefault((n.module, fqcn), []).append(n)
    lines = [f"{S.short(symbol)} — reached by {len(classes)} test classes "
             f"({len(tests)} test methods/members, depth ≤ {depth})"]
    for (mod, fqcn), ns in classes.items():
        best = min(n.depth for n in ns)
        lines.append(f"  {fqcn}  d{best}  {ns[0].source}")
    lines.append("run them (scipq-mcp does not):")
    by_task: OrderedDict[str, list[str]] = OrderedDict()
    for (mod, fqcn), ns in classes.items():
        by_task.setdefault(ns[0].task_dir or mod, []).append(fqcn)
    lines += ["  " + c for c in test_commands(by_task, gradle, template)]
    return "\n".join(lines)


def changed(result: dict, base: str, impact_nodes, tests_line: str, depth: int = 3) -> str:
    defs = result["definitions"]
    java = result.get("java_files", result["files"])
    other = result["files"] - java
    lines = [f"changed since {base or 'the index'}: {java} .java files"
             + (f" (+{other} other files)" if other else "") + f", {len(defs)} definitions"]
    for sym, e in defs.items():
        ch = sorted(e["changed"])
        span = f"{ch[0]}" if len(ch) == 1 else f"{ch[0]}–{ch[-1]} ({len(ch)} lines)"
        test = "  [test]" if e.get("test") else ""
        lines.append(f"  {S.short(sym)}  {e['source']}:{e['line']}  changed {span}{test}")
    for sym, e in result.get("class_level", {}).items():
        ch = sorted(e["changed"])
        where = " and ".join(sorted(e.get("kind", set()))) or "class body outside members"
        if e.get("kind"):
            where = f"class {where} (framework behavior, not its API)"
        lines.append(f"  {S.short(sym)}  {e['source']}:{e['line']}  {where} changed "
                     f"({', '.join(map(str, ch[:5]))}{' …' if len(ch) > 5 else ''}); "
                     "not expanded to the class's users")
    if result.get("untracked"):
        lines.append(f"untracked .java included: {_some(result['untracked'])}")
    if result.get("trivial_hunks"):
        lines.append(f"{result['trivial_hunks']} hunks only touch imports or blank lines "
                     "(ignored)")
    if result["unmapped_deletions"]:
        lines.append(f"{len(result['unmapped_deletions'])} hunks only delete code, which is not "
                     "in the index (cannot map them): " + _some(result["unmapped_deletions"]))
    if result["stale"]:
        lines.append("stale (edited after their last compilation; recompile to map them): "
                     + _some(result["stale"]))
    if result["not_indexed"]:
        lines.append("not indexed (new, or never compiled): " + _some(result["not_indexed"]))
    if impact_nodes is not None:
        n_tests = sum(1 for n in impact_nodes if n.test)
        lines.append(f"callers of the changed definitions (depth ≤ {depth}): "
                     f"{len(impact_nodes)} ({n_tests} in tests)")
    if tests_line:
        lines.append(tests_line)
    return "\n".join(lines)


def deps(edges: dict, level: str, cycles_found, offset: int, limit: int,
         target: str = "") -> str:
    if not edges:
        hint = "" if level == "package" or not target else \
            " (module level counts project modules only; try level=package or sites=true)"
        return f"no dependencies between {level}s in scope{hint}"
    rows = sorted(edges.items(), key=lambda kv: -kv[1])
    shown, suffix = page(rows, offset, limit)
    lines = [f"{len(rows)} {level} dependencies (references counted){suffix}"]
    lines += [f"  {a} → {b}  {n}" for (a, b), n in shown]
    if cycles_found is not None:
        if cycles_found:
            lines.append(f"cycles: {len(cycles_found)}")
            for c in cycles_found[:20]:
                lines.append("  " + " ↔ ".join(c[:12]) + (" …" if len(c) > 12 else ""))
        else:
            lines.append("cycles: none")
    return "\n".join(lines)


def uses_between(scope: str, target: str, rows, total: int) -> str:
    if not rows:
        return f"no uses of {target} from {scope}"
    lines = [f"uses of {target} from {scope}: {total}"
             + (f" (showing {len(rows)})" if len(rows) < total else "")]
    for caller, tgt, src, sites in rows:
        lines.append(f"  {S.short(caller)} → {S.short(tgt)}  {src}:{', '.join(map(str, sites[:5]))}")
    return "\n".join(lines)


def dead_code(rows, offset: int, limit: int, suppressed: dict | None = None,
              weak=None) -> str:
    """Candidates, then the low-confidence ones, paged as one list (offset/limit cover both)."""
    weak = weak or []
    omitted = ""
    if suppressed:
        top = sorted(suppressed.items(), key=lambda kv: -kv[1])[:5]
        omitted = (f"omitted because their name appears in strings or other files: "
                   f"{sum(suppressed.values())} — " + ", ".join(f"{p} {n}" for p, n in top)
                   + (" … (exclude generated files with [project.by_name] exclude)"
                      if len(suppressed) > 5 else ""))
    offset, limit = max(0, offset), max(1, limit)
    total = len(rows) + len(weak)
    shown = rows[offset:offset + limit]
    weak_shown = weak[max(0, offset - len(rows)):max(0, offset + limit - len(rows))]
    end = offset + len(shown) + len(weak_shown)
    if not offset and end == total:
        suffix = ""
    elif end == offset:
        suffix = f" (offset {offset} is past the end of {total})"
    else:  # one list: the candidates, then the low-confidence ones
        suffix = (f" (showing {offset + 1}–{end} of {total}"
                  + (f" = {len(rows)} + {len(weak)} low confidence" if weak else "")
                  + (f"; next offset {end})" if end < total else ")"))
    weak_lines = []
    if weak_shown:
        weak_lines.append(f"{len(weak)} more candidates whose bean property name appears "
                          "elsewhere (low confidence: e.g. getDescription and \"description\")"
                          + (suffix if not rows else "") + ":")
        for sym, kind, src, line, sig, where, prop in weak_shown:
            weak_lines.append(f"  {src}:{line}  {S.short(sym)}  (\"{prop}\" in {where})")
    elif weak:
        weak_lines.append(f"{len(weak)} more candidates whose bean property name appears "
                          f"elsewhere (low confidence) follow at offset {len(rows)}")
    if not rows:
        head = "no dead-code candidates in scope" + ("" if weak_shown else suffix)
        return "\n".join([head] + ([omitted] if omitted else []) + weak_lines)
    also = f" + {len(weak)} low confidence (bean property names) = {total}" if weak else ""
    lines = [f"{len(rows)} dead-code candidates{also} (no references; framework entry points, "
             f"overrides and names used in strings left out — check before deleting){suffix}"]
    if omitted:
        lines.append(omitted)
    current = None
    for sym, kind, src, line, sig in shown:
        if src != current:
            current = src
            lines.append(src)
        lines.append(f"  {line}  {kind}: {signature_text(sig) or S.short(sym)}")
    return "\n".join(lines + weak_lines)


def signature_text(sig: str) -> str:
    parts = [x.strip() for x in (sig or "").splitlines() if x.strip()]
    for x in reversed(parts):
        if not x.startswith("@"):
            return x
    return parts[-1] if parts else ""


def census(rows) -> str:
    if not rows:
        return "no annotations on project definitions"
    lines = [f"annotations on project definitions (top {len(rows)}): uses, files"]
    lines += [f"  @{S.short(a)}  {n}  {f}" for a, n, f in rows]
    return "\n".join(lines)


def cache_status(st: dict) -> str:
    if not st.get("exists"):
        return f"no auxiliary index yet for this checkout ({st['path']}); the first query that " \
               "needs it builds it"
    lines = [f"auxiliary index: {st['path']}  {st.get('bytes', 0) / 1e6:.1f} MB"]
    for k in ("shards", "symbols", "defs", "refs", "rels", "annotations"):
        if k in st:
            lines.append(f"  {k}: {st[k]}")
    for k in ("external", "created_at", "last_used_at"):
        if k in st:
            v = st[k]
            if k.endswith("_at"):
                import datetime as _dt
                v = _dt.datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def entities(rows, offset: int, limit: int) -> str:
    if not rows:
        return "no entities found"
    shown, suffix = page(rows, offset, limit)
    lines = [f"{len(rows)} entities{suffix}: type  table  location"]
    lines += [f"  {S.short(sym)}  {table or '-'}  {src}:{line}" for sym, src, line, table in shown]
    return "\n".join(lines)


def settings_lines(st) -> list[str]:
    out = [f"profiles: {', '.join(st.profiles) or 'none'}"
           + (f" — {st.source}" if st.source else "")]
    if getattr(st, "files", None):
        out.append("configuration (later ones win): " + "; ".join(st.files))
    if st.tests_filter:
        origin = getattr(st, "tests_filter_from", "")
        out.append(f"tests filter: {st.tests_filter}" + (f"  (from {origin})" if origin else ""))
    for w in st.warnings:
        out.append(f"config warning: {w}")
    return out


def javadoc_mentions(hits, symbol: str) -> str:
    if not hits:
        return f"javadoc mentions ({{@link}}, @see) of {S.short(symbol)}: none"
    lines = [f"javadoc mentions ({{@link}}, @see), not in the index: {len(hits)}"]
    lines += [f"  {h.source}:{h.line}: {h.text}" for h in hits]
    return "\n".join(lines)


def library_members(type_symbol: str, groups, offset: int, limit: int) -> str:
    rows = []
    total = 0
    for t, lib, mems, hidden in groups:
        if lib is None:
            rows.append(f"from {S.short(t)}: no sources")
            continue
        total += len(mems)
        extra = f", {hidden} overridden by nearer types" if hidden else ""
        label = "own" if t == type_symbol else f"from {S.short(t)}"
        rows.append(f"{label} ({len(mems)}{extra}, {lib.coordinates}):")
        for m in mems:
            rows.append(f"  {m.line:>5}  {m.kind}: {_decl(m.declaration)}")
    shown, suffix = page(rows, offset, limit)
    return "\n".join([f"{S.short(type_symbol)} (library) — {total} members{suffix}"]
                     + list(shown))


def unreadable(sources: list[str]) -> str:
    return (f"WARNING: {len(sources)} shard{'s' if len(sources) != 1 else ''} could not be read, "
            f"probably being written by a compilation right now (their results are missing; "
            f"retry when it finishes): {_some(sources)}")


# --- compare ----------------------------------------------------------------------------------

def _entries(d: dict, limit: int) -> list[str]:
    rows = []
    for sym, e in sorted(d.items(), key=lambda kv: (kv[1].source, kv[1].line))[:limit]:
        rows.append(f"    {S.short(sym)}  {e.source}:{e.line}  — {e.signature[:120]}")
    if len(d) > limit:
        rows.append(f"    … and {len(d) - limit} more")
    return rows


def compare(a_label: str, b_label: str, diff, api_only: bool, scope: str, git: dict | None,
            limit: int = 50) -> str:
    what = "public/protected API" if api_only else "definitions"
    lines = [f"compare A = {a_label} with B = {b_label}",
             f"{what} (tests left out{', in ' + scope if scope else ''}): {len(diff.only_a)} only "
             f"in A, {len(diff.only_b)} only in B, {len(diff.changed)} with another signature"
             + (f", {len(diff.renamed)} maybe renamed" if diff.renamed else "")
             + (f", {len(diff.moved)} maybe moved" if diff.moved else "")
             + " (annotations ignored)"]
    if diff.renamed:
        lines.append("  maybe renamed (same owner, kind and signature but the name):")
        lines += [f"    {S.short(a)} → {S.short(b)}" for a, b in diff.renamed[:limit]]
    if diff.moved:
        lines.append("  maybe moved (same simple name and members, another package):")
        lines += [f"    {a} → {b}" for a, b in diff.moved[:limit]]
    renamed = {x for pair in diff.renamed + diff.moved for x in pair}
    only_a = {k: v for k, v in diff.only_a.items() if k not in renamed}
    only_b = {k: v for k, v in diff.only_b.items() if k not in renamed}
    if git is not None:  # who did what, from the files each side touched since the base
        from .compare import ApiDiff, classify
        groups = classify(ApiDiff(only_a, only_b, diff.changed), git["files_a"], git["files_b"])
        for name, items in groups.items():
            if not items:
                continue
            lines.append(f"  {name} ({len(items)}):")
            for sym, e in sorted(items.items(), key=lambda kv: (
                    (kv[1][0] if isinstance(kv[1], tuple) else kv[1]).source,
                    (kv[1][0] if isinstance(kv[1], tuple) else kv[1]).line))[:limit]:
                if isinstance(e, tuple):
                    lines.append(f"    {S.short(sym)}  {e[0].source}:{e[0].line}")
                    lines.append(f"      A: {e[0].signature[:120]}")
                    lines.append(f"      B: {e[1].signature[:120]}")
                else:
                    lines.append(f"    {S.short(sym)}  {e.source}:{e.line}  — {e.signature[:120]}")
            if len(items) > limit:
                lines.append(f"    … and {len(items) - limit} more")
        only_a, only_b = {}, {}
        diff = ApiDiff(diff.only_a, diff.only_b, {}, diff.renamed, diff.moved)
    if only_a:
        lines.append(f"  only in A ({len(only_a)}):")
        lines += _entries(only_a, limit)
    if only_b:
        lines.append(f"  only in B ({len(only_b)}):")
        lines += _entries(only_b, limit)
    if diff.changed:
        lines.append(f"  another signature ({len(diff.changed)}):")
        for sym, (ea, eb) in sorted(diff.changed.items(),
                                    key=lambda kv: (kv[1][0].source, kv[1][0].line))[:limit]:
            lines.append(f"    {S.short(sym)}  {ea.source}:{ea.line}")
            lines.append(f"      A: {ea.signature[:120]}")
            lines.append(f"      B: {eb.signature[:120]}")
        if len(diff.changed) > limit:
            lines.append(f"    … and {len(diff.changed) - limit} more")
    if git is None:
        lines.append("no shared git history (or no git): changes since a merge base, clashes "
                     "and merge breaks are not shown")
        return "\n".join(lines)
    def n(files):
        return f"{len(files)} files ({sum(1 for f in files if f.endswith('.java'))} .java)"
    lines.append(f"since the merge base {git['base'][:10]}: A changed {n(git['files_a'])}, B "
                 f"{n(git['files_b'])}; both changed {n(git['both_files'])}")
    if git["both_members"]:
        lines.append(f"  members changed on both sides (merge conflicts likely, "
                     f"{len(git['both_members'])}):")
        for sym in git["both_members"][:limit]:
            lines.append(f"    {S.short(sym)}  {git['member_source'].get(sym, '')}")
    elif git["both_files"]:
        lines.append("  files changed on both sides (different members): "
                     + ", ".join(sorted(git["both_files"])[:12])
                     + (" …" if len(git["both_files"]) > 12 else ""))
    for label, brk in (("merging B into A would break, in files A changed since the base",
                        git["b_into_a"]),
                       ("merging A into B would break, in files B changed since the base",
                        git["a_into_b"])):
        if not brk:
            lines.append(f"{label}: none found")
            continue
        n = sum(len(b.uses) for b in brk)
        lines.append(f"{label}: {n} uses of {len(brk)} symbols")
        shown = 0
        for b in brk:
            if shown >= limit:
                lines.append(f"  … and {len(brk) - shown} more symbols")
                break
            if b.how == "removed":
                change = "removed"
            elif b.how == "abstract method added":
                change = f"abstract method added: {b.new.signature[:90]}"
            else:
                change = f"signature {b.old.signature[:70]} → {b.new.signature[:70]}"
            lines.append(f"  {S.short(b.symbol)}  ({change}; {b.source})")
            for u in b.uses[:8]:
                tag = f"  [{u.how}]" if u.how in ("overrides it", "concrete subtype") else ""
                lines.append(f"    {container_label(u.caller) or '(top level)'}  {u.source}:"
                             + ",".join(map(str, u.lines[:5])) + tag)
            if len(b.uses) > 8:
                lines.append(f"    … and {len(b.uses) - 8} more uses")
            shown += 1
    lines.append("(from both indexes, syntactic: behavior changes and overload resolution are "
                 "not seen; scipq-mcp never merges)")
    return "\n".join(lines)


# --- cucumber ---------------------------------------------------------------------------------

def _glue_label(x) -> str:
    return f"{S.short(x.symbol)}  {x.source}:{x.line}"


def _cut(rows: list[str], limit: int) -> list[str]:
    return rows[:limit] + ([f"    … and {len(rows) - limit} more"] if len(rows) > limit else [])


def _step_count(steps) -> str:
    """"N steps", and the distinct lines when an outline's rows make them differ (grep counts
    lines; each Examples row is a step of its own here)."""
    lines = len({(s.source, s.line) for s in steps})
    n = len(steps)
    return f"{n} steps" + (f" ({lines} lines: an outline's steps count once per Examples row)"
                           if lines != n else "")


def _distinct(steps) -> list:
    """Steps once per (file, line, text): an outline step without placeholders is one."""
    seen, out = set(), []
    for s in steps:
        if (s.source, s.line, s.text) not in seen:
            seen.add((s.source, s.line, s.text))
            out.append(s)
    return out


def steps_check(glues, suite, match, exclude_tags, offset: int, limit: int) -> str:
    feats = suite.feats
    steps = [(f, s) for f in feats for s in f.steps]
    undefined, left_out, nowhere, ambiguous, used = [], [], [], [], set()
    excluded = set(exclude_tags)
    for f, s in steps:
        m = match(s)
        used |= {id(x) for x in m}
        row = f"    {s.source}:{s.line}  {s.keyword} {s.text}".rstrip()
        if not m:
            runs = suite.runs_in(s)
            tags = [set(suite.by_source[src].scenarios[k].tags) for src, idx in runs.items()
                    for k in idx]
            if not tags:
                nowhere.append(row)
            elif all(t & excluded for t in tags):
                left_out.append(row + "  (" + " ".join(sorted(set().union(*tags) & excluded))
                                + ")")
            else:
                undefined.append(row)
        elif len(m) > 1:
            ambiguous.append(row + "  → " + ", ".join(S.short(x.symbol) for x in m))
    unknown = [f"    {_glue_label(x)}  @{x.annotation}" + (f"({x.text!r})" if x.text else
                                                             "(not made of string literals)")
               for x in glues if x.kind == "unknown"]
    unused = [f"    {_glue_label(x)}  @{x.annotation}({x.text!r})" for x in glues
              if x.regex is not None and id(x) not in used]
    missing = [f"    {i.step.source}:{i.step.line}  {i.step.keyword} {i.step.text}".rstrip()
               for i in suite.missing()]
    kinds = {k: sum(1 for x in glues if x.kind == k) for k in ("expression", "regex", "unknown")}
    lines = [f"cucumber: {len(glues)} step definitions ({kinds['expression']} expressions, "
             f"{kinds['regex']} regexes" + (f", {kinds['unknown']} not understood"
                                            if kinds["unknown"] else "")
             + f"); {len(feats)} features, {sum(len(f.scenarios) for f in feats)} scenarios, "
             f"{_step_count(_distinct(s for _f, s in steps))} (docs/, hidden and ignored files "
             "left out)"]
    if suite.includes:
        lines.append(f"  {len(suite.includes)} steps include another feature's Background "
                     "([cucumber] include_steps)")
    sections = [("undefined steps (no step definition matches)", undefined, True),
                ("undefined steps only in scenarios the build leaves out ([cucumber] "
                 "exclude_tags)", left_out, False),
                ("undefined steps that run in no scenario (a Background with no scenario that "
                 "no include step loads, e.g. a fixture read by a test)", nowhere, False),
                ("ambiguous steps (several match)", ambiguous, True),
                ("include steps naming a .feature that is not there", missing, False),
                ("step definitions not understood (pattern)", unknown, True),
                ("step definitions no step uses", unused, True)]
    for title, rows, always in sections:
        if rows or always:
            lines.append(f"  {title}: {len(rows) or 'none'}")
            lines += _cut(rows[offset:], limit)
    if suite.cycles:
        lines.append("  WARNING: features that include themselves (a cycle): "
                     + ", ".join(sorted(set(suite.cycles))[:5]))
    return "\n".join(lines)


def feature_steps(feat, match, offset: int, limit: int, suite=None) -> str:
    lines = [f"{feat.name or '(no name)'} — {feat.source} ({feat.language}, "
             f"{len(feat.scenarios)} scenarios" + (f", tags {' '.join(feat.tags)}" if feat.tags
                                                   else "") + ")"]
    if suite is not None and suite.included(feat.source):
        reach = suite.reach(feat.source)
        lines.append(f"its Background is included by other features: it runs in "
                     f"{sum(len(v) for v in reach.values())} scenarios of {len(reach)} features")
    includes = {} if suite is None else {(i.step.line, i.step.text): i for i in suite.includes
                                         if i.step.source == feat.source}
    rows, seen = [], set()
    for s in feat.steps:
        key = (s.line, s.text)
        if key in seen:  # an outline's step, once per Examples row: show its first expansion
            continue
        seen.add(key)
        m = match(s)
        target = ("UNDEFINED" if not m else _glue_label(m[0]) if len(m) == 1 else
                  "AMBIGUOUS: " + ", ".join(S.short(x.symbol) for x in m))
        inc = includes.get(key)
        if inc is not None:
            target += (f"; includes {inc.target}" if inc.target else
                       f"; includes {inc.path!r}: NOT FOUND")
        rows.append(f"  {s.line:>5}  {s.keyword} {s.text}  → {target}")
    shown, suffix = page(rows, offset, limit)
    return "\n".join(lines + ([f"steps{suffix}:"] if suffix else []) + list(shown))


def _run_block(run) -> list[str]:
    if not run:
        return []
    affected, by_source, cmds, excluded, notes = run
    n = sum(len(v) for v in affected.values())
    lines = [f"scenarios to run: {n} in {len(affected)} features"]
    shown = 0
    for src, idx in sorted(affected.items()):
        for k in sorted(idx):
            if shown == 30:
                break
            sc = by_source[src].scenarios[k]
            tags = f"  [{' '.join(sc.tags)}]" if sc.tags else ""
            lines.append(f"  {src}:{sc.line}  {sc.name[:100]}{tags[:80]}")
            shown += 1
    if n > shown:
        lines.append(f"  … and {n - shown} more")
    lines.append("run them (scipq-mcp does not):")
    lines += [f"  {c}" for c in cmds]
    lines += [f"note: {x}" for x in notes]
    if excluded:
        lines.append(f"WARNING: {len(excluded)} of them carry a tag the build leaves out "
                     "([cucumber] exclude_tags), so they will not run: " + "; ".join(excluded[:5])
                     + (" …" if len(excluded) > 5 else ""))
    return lines


def glue_uses(glues, uses, run, offset: int, limit: int, suite=None) -> str:
    x = glues[0]
    lines = [_glue_label(x)]
    for g in glues:
        lines.append(f"  @{g.annotation}({g.text!r})  ({g.kind}"
                     + (f": {g.regex.pattern[:120]}" if g.regex is not None else "") + ")")
    rows, seen, distinct = [], set(), []
    for f, s in uses:
        key = (s.source, s.line, s.text)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(s)
        sc = [f.scenarios[k].name for k in s.pickles[:1]]
        where = (f"  [{sc[0]}]" if sc else
                 "  [a Background other features include]" if suite is not None
                 and suite.exported(s) else "")
        rows.append(f"  {s.source}:{s.line}  {s.keyword} {s.text}{where}")
    if not rows:
        lines.append("no step in the project's .feature files uses it (docs/, hidden and "
                     "ignored files left out)")
        return "\n".join(lines)
    shown, suffix = page(rows, offset, limit)
    lines.append(f"used by {_step_count(distinct)} in "
                 f"{len({f.source for f, _s in uses})} features{suffix}:")
    lines += list(shown)
    return "\n".join(lines + _run_block(run))


def step_text(query: str, found) -> str:
    if not found:
        return f"no step definition matches {query!r} (undefined)"
    head = (f"{query!r} is run by" if len(found) == 1 else
            f"{query!r} is AMBIGUOUS: {len(found)} step definitions match")
    return "\n".join([head] + [f"  {_glue_label(x)}  @{x.annotation}({x.text!r})"
                               for x in found])


def concepts(query: str, terms: list[str], rows, docs: dict, offset: int, limit: int,
             more: bool, hints: dict | None = None) -> str:
    if not terms:
        return f"no words to search in {query!r}"
    def shown(grp: list[str]) -> str:
        if len(grp) > 2 and "|" not in query:   # a stem and its usual endings
            base = os.path.commonprefix(grp)
            if len(base) >= 3:
                return base + "…*"
        return "|".join(v + "*" for v in grp)
    searched = " ".join(shown(grp) for grp in terms)
    tip = "" if "|" in query else (
        "\n(each word was reduced to a stem and its usual endings; write a word's forms and "
        "synonyms as alternatives joined by | (spaces separate different words) to search "
        "those instead, each as a prefix: pago|pagar|payment)")
    if not rows:
        return (f"no definition mentions {searched} (in names, owners, annotations, "
                "signatures or javadoc; tests left out unless include_tests)" + tip)
    lines = [f"definitions about {searched} (best first: the more words, and the surer "
             f"(name, then annotations and labels, then signature or javadoc), the better; "
             f"tests and generated files left out unless asked):"]
    for k, (sym, score, d) in enumerate(rows, offset + 1):
        src, line, _mod, test, sig = d
        kind = {"term": "field"}.get(S.kind_of(sym), S.kind_of(sym) or "symbol")
        lines.append(f"{k:>3}. {S.short(sym)}  {kind}  {src}:{line}"
                     + ("  [test]" if test else ""))
        if sig:
            lines.append(f"       {' '.join(sig.split())[:150]}")
        if docs.get(sym):
            lines.append(f"       — {docs[sym]}")
        if hints and hints.get(sym):
            lines.append(f"       members about the other words: {', '.join(hints[sym])}")
    if more:
        lines.append(f"next offset {offset + limit}")
    return "\n".join(lines) + tip


def cucumber_affected(run, reached: int) -> str:
    lines = _run_block(run)
    if not lines:
        return ""
    lines[0] = "cucumber " + lines[0] + (f" (through {reached} step definitions reached)"
                                         if reached else "") + ":"
    return "\n".join(lines)

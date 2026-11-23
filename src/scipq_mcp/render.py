"""Compact text rendering for the tools: paths relative to the project root, 1-based lines,
and the line of code where it helps. Written for agents: few tokens, no decoration."""

from __future__ import annotations

import re
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
             f"callers{nodes}, {total_sites} call sites; {len(root.children)} direct{suffix}"]

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
        lines.append(f"WARNING: {f.recent} shard{'s' if f.recent != 1 else ''} written in the "
                     f"last {RECENT_SECONDS} s: if a compilation is running in this checkout, "
                     "results may be incomplete (shards being written lose references and "
                     "subtypes without an error); retry when it finishes")
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
                     + (" …" if len(tasks) > 6 else "") + " (scipq-mcp does not build)")
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
    out = re.sub(r"@\s*[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?\s*", "", declaration).strip()
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
                     + " (scipq-mcp does not build)")
    elif st.stale or st.uncompiled:
        lines.append("recompile the project to refresh (scipq-mcp does not build)")
    return "\n".join(lines)


def format_test_filter(template: str, module: str, classes: list[str]) -> str:
    """A configured test command: {module} (`a:b`), {task} (`:a:b:test`); per class {fqcn} /
    {simple} (one line per class); or, for one line per module, {classes} (FQCNs separated by
    spaces) or {tests} (`--tests 'A' --tests 'B'`)."""
    mod = module.replace("/", ":") if module not in (".", "") else ""
    task = f":{mod}:test" if mod else "test"
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
    by_task: OrderedDict[str, list] = OrderedDict()
    for t in uses:
        by_task.setdefault(task_of(t.module), []).append(t)
        lines.append(f"  {t.test_class}  {t.source}:" + ", ".join(map(str, t.lines)))
    lines.append("run them (scipq-mcp does not):")
    if template:
        mods: OrderedDict[str, list[str]] = OrderedDict()
        for t in uses:
            mods.setdefault(t.module.rpartition("/")[0], []).append(t.test_class)
        for mod, classes in mods.items():
            lines.append("  " + format_test_filter(template, mod, classes))
        return "\n".join(lines)
    for task, ts in by_task.items():
        filters = " ".join(f"--tests '{t.test_class}'" for t in ts)
        if task.startswith(":"):
            lines.append(f"  {gradle} {task} {filters}")
        elif not task:
            lines.append(f"  {gradle} test {filters}   (use the test task of your build)")
        elif task.endswith("/testCompile"):
            module = task.rsplit("/", 1)[0]
            names = ",".join(t.test_class.rsplit(".", 1)[-1] for t in ts)
            lines.append(f"  mvn -pl {module} test -Dtest='{names}'")
        else:
            lines.append(f"  {task}: " + " ".join(t.test_class for t in ts))
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


def impact(symbol: str, nodes, truncated: bool, depth: int, group_by: str, offset: int,
           limit: int) -> str:
    if not nodes:
        return f"nothing in this project calls {S.short(symbol)}"
    groups: OrderedDict[str, list] = OrderedDict()
    for n in nodes:
        key = S.package_of(n.symbol) if group_by == "package" else n.module
        groups.setdefault(key or "(root)", []).append(n)
    by_depth: OrderedDict[int, int] = OrderedDict()
    for n in nodes:
        by_depth[n.depth] = by_depth.get(n.depth, 0) + 1
    tests = sum(1 for n in nodes if n.test)
    lines = [f"impact of {S.short(symbol)} (callers up to depth {depth}): {len(nodes)} "
             f"definitions, {tests} in tests; by depth "
             + ", ".join(f"{d}: {c}" for d, c in by_depth.items())
             + ("; TRUNCATED" if truncated else "")]
    lines.append(f"by {group_by}: " + ", ".join(
        f"{g} {len(ns)}" for g, ns in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:12]))
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
        by_task.setdefault(mod, []).append(fqcn)
    if template:
        for mod, names in by_task.items():
            lines.append("  " + format_test_filter(template, mod, names))
        return "\n".join(lines)
    for mod, names in by_task.items():
        filters = " ".join(f"--tests '{c}'" for c in names)
        lines.append(f"  {gradle} {':' + mod.replace('/', ':') + ':test' if mod not in ('.', '') else 'test'} {filters}")
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
    else:
        suffix = (f" (showing {offset + 1}–{end} of {total}, low confidence included"
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
    lines = [f"{len(rows)} dead-code candidates (no references; framework entry points, "
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
    if st.tests_filter:
        out.append(f"tests filter: {st.tests_filter}")
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

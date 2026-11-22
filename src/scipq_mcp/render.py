"""Compact text rendering for the tools: paths relative to the project root, 1-based lines,
and the line of code where it helps. Written for agents: few tokens, no decoration."""

from __future__ import annotations

from collections import OrderedDict

from . import symbols as S
from .queries import (
    CallNode,
    Callee,
    Definition,
    Implementation,
    Reference,
    Resolution,
    Supertype,
    signature_line,
)
from .workspace import Freshness, Workspace

MAX_LISTED_FILES = 5


def loc(d: Definition) -> str:
    return f"{d.location.source}:{d.location.line}"


def definition_row(d: Definition, full_symbol: bool = True) -> str:
    name = d.symbol if full_symbol else S.short(d.symbol)
    sig = signature_line(d.info)
    extra = []
    if d.generated:
        extra.append("generated")
    if d.container:
        extra.append(f"local, in {S.short(d.container)}")
    tail = f"  ({', '.join(extra)})" if extra else ""
    return f"{loc(d)}  {d.kind}  {name}" + (f"  — {sig}" if sig else "") + tail


def candidates(r: Resolution, limit: int = 30) -> str:
    lines = [r.note]
    for d in r.candidates[:limit]:
        lines.append("  " + definition_row(d))
    if len(r.candidates) > limit:
        lines.append(f"  … and {len(r.candidates) - limit} more")
    return "\n".join(lines)


def find(text: str, found: list[Definition], limit: int) -> str:
    if not found:
        return f"no symbol defined in this project matches {text!r}"
    head = f"{len(found)} definition{'s' if len(found) != 1 else ''} match {text!r}"
    lines = [head + (f" (showing {limit})" if len(found) > limit else "")]
    lines += [definition_row(d) for d in found[:limit]]
    return "\n".join(lines)


def references(ws: Workspace, symbol: str, refs: list[Reference], defs: list[Definition],
               offset: int, limit: int, include_definitions: bool) -> str:
    files = OrderedDict()
    for r in refs:
        files.setdefault(r.location.source, []).append(r)
    total = len(refs)
    page = refs[offset:offset + limit]
    head = (f"{S.short(symbol)} — {total} reference{'s' if total != 1 else ''} "
            f"in {len(files)} file{'s' if len(files) != 1 else ''}")
    if total > len(page) or offset:
        end = offset + len(page)
        head += f" (showing {offset + 1}–{end}" if page else " (offset past the end"
        head += f"; next offset {end})" if end < total else ")"
    lines = [head]
    if include_definitions:
        for d in defs:
            lines.append(f"defined at {loc(d)}")
    by_file: OrderedDict[str, list[Reference]] = OrderedDict()
    for r in page:
        by_file.setdefault(r.location.source, []).append(r)
    for source, rs in by_file.items():
        lines.append(source)
        text = ws.source_lines(source, {r.location.range.start_line for r in rs})
        counts: OrderedDict[int, int] = OrderedDict()
        for r in rs:
            counts[r.location.range.start_line] = counts.get(r.location.range.start_line, 0) + 1
        for line, n in counts.items():
            mult = f" (×{n})" if n > 1 else ""
            lines.append(f"  {line + 1}: {text.get(line, '')[:160]}{mult}")
    return "\n".join(lines)


def definition(d: Definition | None, symbol: str) -> str:
    if d is None:
        return (f"{symbol} is not defined in this project (external, e.g. JDK or a dependency, "
                "or its file was not compiled)")
    lines = [d.symbol, f"{d.kind} at {loc(d)}" + ("  (generated)" if d.generated else "")]
    if d.enclosing is not None:
        lines[-1] += f", body {d.enclosing.start_line + 1}–{d.enclosing.end_line + 1}"
    if d.info:
        if d.info.signature:
            lines.append(d.info.signature.strip())
        if d.info.enclosing_symbol:
            lines.append(f"enclosed by {d.info.enclosing_symbol}")
        impl = [r.symbol for r in d.info.relationships if r.is_implementation]
        if impl:
            label = "overrides" if S.is_method(d.symbol) else "supertypes"
            lines.append(f"{label}: " + ", ".join(S.short(s) for s in impl))
        for doc in d.info.documentation:
            doc = doc.strip()
            if doc:
                lines.append("")
                lines.append(doc)
    return "\n".join(lines)


def implementations(symbol: str, impls: list[Implementation], direct_only: bool,
                    limit: int) -> str:
    shown = [i for i in impls if i.direct] if direct_only else impls
    n_direct = sum(1 for i in impls if i.direct)
    noun = "overrides" if S.is_method(symbol) else "subtypes"
    if not impls:
        return f"no {noun} of {S.short(symbol)} in this project"
    head = f"{S.short(symbol)} — {len(impls)} {noun} ({n_direct} direct)"
    if len(shown) > limit:
        head += f" (showing {limit})"
    lines = [head]
    for i in shown[:limit]:
        row = definition_row(i.definition, full_symbol=False)
        if not i.direct:
            row += "  via " + ", ".join(S.short(v) for v in i.via[:3])
        lines.append(row)
    return "\n".join(lines)


def supertypes(symbol: str, d: Definition | None, supers: list[Supertype]) -> str:
    if d is None:
        return definition(None, symbol)
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


def outline(source: str, defs: list[Definition]) -> str:
    lines = [f"{source} — {len(defs)} definitions"]
    for d in defs:
        depth = max(0, sum(1 for x in S.parse(d.symbol) if x.suffix == S.TYPE)
                    - (1 if S.is_type(d.symbol) else 0))
        sig = signature_line(d.info) or S.short(d.symbol)
        gen = "  (generated)" if d.generated else ""
        lines.append(f"{d.location.line:>5}  {'  ' * depth}{d.kind}: {sig}{gen}")
    return "\n".join(lines)


def callers(root: CallNode, depth: int) -> str:
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
    lines = [f"callers of {S.short(root.symbol)} (depth {depth}): {total_nodes} callers, "
             f"{total_sites} call sites"]

    def walk(n: CallNode, indent: int):
        for c in n.children:
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
            walk(c, indent + 1)
        if n.truncated:
            lines.append(f"{'  ' * indent}… truncated (max_nodes)")

    walk(root, 0)
    return "\n".join(lines)


def callees(symbol: str, d: Definition | None, out: list[Callee]) -> str:
    if d is None:
        return definition(None, symbol)
    if not out:
        return f"{S.short(symbol)} references no methods{'' if d.enclosing else ' (no body range)'}"
    lines = [f"{S.short(symbol)} ({loc(d)}) — {len(out)} callees"]
    for c in out:
        lines.append(f"  {S.short(c.symbol)}  " + ", ".join(str(s.line) for s in c.sites))
    return "\n".join(lines)


def freshness(f: Freshness | None, result_sources: set[str] | None = None) -> str:
    if not f:
        return ""
    lines = []
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
    return "\n".join(lines)


def _some(items: list[str]) -> str:
    more = f" (+{len(items) - MAX_LISTED_FILES} more)" if len(items) > MAX_LISTED_FILES else ""
    return ", ".join(items[:MAX_LISTED_FILES]) + more

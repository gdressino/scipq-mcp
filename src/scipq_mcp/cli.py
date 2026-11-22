"""Command line: run the MCP server (default), print the Gradle init template, or run a query
directly for debugging.

    scipq-mcp [serve] [--root DIR]        MCP server on stdio
    scipq-mcp gradle-init                 print the Gradle init script template
    scipq-mcp query [--root DIR] <tool> <args>
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources

from . import __version__
from .service import FRESHNESS_MODES, Service


def _template() -> str:
    return resources.files("scipq_mcp").joinpath("templates/scip-shards.init.gradle").read_text(
        encoding="utf-8")


def _query_parser(sub) -> None:
    q = sub.add_parser("query", help="run one query and print the answer (debugging)")
    q.add_argument("--root", help="project root (default: closest ancestor with shards)")
    q.add_argument("--shards-dir", help="shards directory relative to the root "
                                        "(default: build/scip-targetroot)")
    q.add_argument("--freshness", choices=FRESHNESS_MODES, default="full",
                   help="stale-shard check: every file (full), only the files in the answer "
                        "(results), or none (off)")
    t = q.add_subparsers(dest="tool", required=True)

    def add(name, *positional, **flags):
        sp = t.add_parser(name)
        for a in positional:
            sp.add_argument(a)
        for flag, kw in flags.items():
            sp.add_argument("--" + flag.replace("_", "-"), **kw)
        return sp

    paging = {"offset": {"type": int, "default": 0}, "limit": {"type": int, "default": 100}}
    add("find", "name", **paging)
    add("refs", "symbol", **paging, defs={"action": "store_true"},
        no_imports={"action": "store_true"}, by_name={"action": "store_true"},
        globs={"nargs": "*", "default": None})
    add("def", "symbol")
    add("source", "symbol", max_lines={"type": int, "default": 200})
    add("impl", "symbol", **paging, direct={"action": "store_true"})
    add("supertypes", "symbol")
    add("members", "symbol", **paging, own={"action": "store_true"})
    add("family", "symbol", **paging, no_uses={"action": "store_true"})
    add("overloads", "symbol", **paging)
    add("outline", "file", **paging)
    add("callers", "symbol", **paging, depth={"type": int, "default": 1},
        overridden={"action": "store_true"}, max_nodes={"type": int, "default": 200})
    add("callees", "symbol", **paging, fields={"action": "store_true"},
        types={"action": "store_true"})
    add("annotated", "annotation", **paging)
    add("typed", "symbol", **paging, only={"nargs": "*", "default": None})
    add("tests", "symbol")
    add("status")
    add("setup", no_template={"action": "store_true"})


def _run_query(a) -> str:
    svc = Service(a.root, a.shards_dir, freshness=a.freshness)
    t = a.tool
    if t == "find":
        return svc.find(a.name, limit=a.limit, offset=a.offset)
    if t == "refs":
        return svc.references(a.symbol, offset=a.offset, limit=a.limit,
                              include_definitions=a.defs, exclude_imports=a.no_imports,
                              by_name=a.by_name, by_name_globs=a.globs)
    if t == "def":
        return svc.definition(a.symbol)
    if t == "source":
        return svc.source(a.symbol, max_lines=a.max_lines)
    if t == "impl":
        return svc.implementations(a.symbol, direct_only=a.direct, limit=a.limit,
                                   offset=a.offset)
    if t == "supertypes":
        return svc.supertypes(a.symbol)
    if t == "members":
        return svc.members(a.symbol, inherited=not a.own, offset=a.offset, limit=a.limit)
    if t == "family":
        return svc.family(a.symbol, offset=a.offset, limit=a.limit, show_uses=not a.no_uses)
    if t == "overloads":
        return svc.overloads(a.symbol, offset=a.offset, limit=a.limit)
    if t == "outline":
        return svc.outline(a.file, offset=a.offset, limit=a.limit)
    if t == "callers":
        return svc.callers(a.symbol, depth=a.depth, include_overridden=a.overridden,
                           max_nodes=a.max_nodes, offset=a.offset, limit=a.limit)
    if t == "callees":
        return svc.callees(a.symbol, include_fields=a.fields, include_types=a.types,
                           offset=a.offset, limit=a.limit)
    if t == "annotated":
        return svc.annotated(a.annotation, offset=a.offset, limit=a.limit)
    if t == "typed":
        return svc.typed(a.symbol, categories=a.only, offset=a.offset, limit=a.limit)
    if t == "tests":
        return svc.tests(a.symbol)
    if t == "status":
        return svc.index_status()
    if t == "setup":
        return svc.setup(include_template=not a.no_template)
    raise SystemExit(f"unknown tool {t}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="scipq-mcp",
        description="MCP server for Java code navigation over per-file SCIP shards.")
    parser.add_argument("--version", action="version", version=f"scipq-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="run the MCP server on stdio (default)")
    s.add_argument("--root", help="default project root when a tool call does not pass one")
    s.add_argument("--shards-dir", help="shards directory relative to the root")
    s.add_argument("--freshness", choices=FRESHNESS_MODES, default="full",
                   help="stale-shard check on every answer (default: full)")
    s.add_argument("--always", action="store_true",
                   help="expose the tools even when started outside a Java project (by default "
                        "the server stays idle there, with no tools)")

    sub.add_parser("gradle-init", help="print the Gradle init script that writes the shards")
    _query_parser(sub)

    a = parser.parse_args(argv)
    if a.command in (None, "serve"):
        from .server import serve

        serve(getattr(a, "root", None), getattr(a, "shards_dir", None),
              getattr(a, "freshness", "full"), getattr(a, "always", False))
    elif a.command == "gradle-init":
        sys.stdout.write(_template())
    elif a.command == "query":
        print(_run_query(a))


if __name__ == "__main__":
    main()

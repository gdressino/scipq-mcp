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

    p = t.add_parser("find")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=50)

    p = t.add_parser("refs")
    p.add_argument("symbol")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--defs", action="store_true", help="also print the definition")

    p = t.add_parser("def")
    p.add_argument("symbol")

    p = t.add_parser("impl")
    p.add_argument("symbol")
    p.add_argument("--direct", action="store_true")
    p.add_argument("--limit", type=int, default=200)

    p = t.add_parser("supertypes")
    p.add_argument("symbol")

    p = t.add_parser("outline")
    p.add_argument("file")

    p = t.add_parser("callers")
    p.add_argument("symbol")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--overridden", action="store_true",
                   help="include calls to the methods it overrides")
    p.add_argument("--max-nodes", type=int, default=200)

    p = t.add_parser("callees")
    p.add_argument("symbol")
    p.add_argument("--fields", action="store_true")
    p.add_argument("--types", action="store_true")


def _run_query(a) -> str:
    svc = Service(a.root, a.shards_dir, freshness=a.freshness)
    if a.tool == "find":
        return svc.find(a.name, limit=a.limit)
    if a.tool == "refs":
        return svc.references(a.symbol, offset=a.offset, limit=a.limit,
                              include_definitions=a.defs)
    if a.tool == "def":
        return svc.definition(a.symbol)
    if a.tool == "impl":
        return svc.implementations(a.symbol, direct_only=a.direct, limit=a.limit)
    if a.tool == "supertypes":
        return svc.supertypes(a.symbol)
    if a.tool == "outline":
        return svc.outline(a.file)
    if a.tool == "callers":
        return svc.callers(a.symbol, depth=a.depth, include_overridden=a.overridden,
                           max_nodes=a.max_nodes)
    if a.tool == "callees":
        return svc.callees(a.symbol, include_fields=a.fields, include_types=a.types)
    raise SystemExit(f"unknown tool {a.tool}")


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

    sub.add_parser("gradle-init", help="print the Gradle init script that writes the shards")
    _query_parser(sub)

    a = parser.parse_args(argv)
    if a.command in (None, "serve"):
        from .server import serve

        serve(getattr(a, "root", None), getattr(a, "shards_dir", None),
              getattr(a, "freshness", "full"))
    elif a.command == "gradle-init":
        sys.stdout.write(_template())
    elif a.command == "query":
        print(_run_query(a))


if __name__ == "__main__":
    main()

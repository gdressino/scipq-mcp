"""Command line: run the MCP server (default), print the Gradle init template, or run a query
directly for debugging.

    scipq-mcp [serve] [--root DIR]        MCP server on stdio
    scipq-mcp gradle-init                 print the Gradle init script template
    scipq-mcp query [--root DIR] <tool> <args>
"""

from __future__ import annotations

import argparse
import sys
import time
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
    q.add_argument("--cache-dir", help="auxiliary index base directory (default: user cache)")
    q.add_argument("--config", help="project configuration file (default: "
                                    "$XDG_CONFIG_HOME/scipq/config.toml)")
    q.add_argument("--index-external", choices=["all", "methods", "none"], default="all")
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
    add("find", "name", **paging, match={"default": "auto",
                                         "choices": ["auto", "exact", "prefix", "contains",
                                                     "camel"]})
    add("refs", "symbol", **paging, defs={"action": "store_true"},
        no_imports={"action": "store_const", "const": True, "default": None},
        with_imports={"action": "store_const", "const": False, "dest": "no_imports"},
        by_name={"action": "store_true"},
        globs={"nargs": "*", "default": None}, scope={"default": ""})
    add("def", "symbol")
    add("source", "symbol", max_lines={"type": int, "default": 200})
    add("impl", "symbol", **paging, direct={"action": "store_true"})
    add("supertypes", "symbol")
    add("members", "symbol", **paging, own={"action": "store_true"})
    add("family", "symbol", **paging, no_uses={"action": "store_true"})
    add("overloads", "symbol", **paging)
    add("outline", "file", **paging)
    add("callers", "symbol", **paging, depth={"type": int, "default": 1},
        overridden={"action": "store_true"}, max_nodes={"type": int, "default": 200},
        scope={"default": ""})
    add("callees", "symbol", **paging, fields={"action": "store_true"},
        types={"action": "store_true"})
    add("annotated", "annotation", **paging, scope={"default": ""})
    add("typed", "symbol", **paging, only={"nargs": "*", "default": None},
        scope={"default": ""}, modifiers={"nargs": "*", "default": None},
        subtypes={"action": "store_true"})
    add("tests", "symbol", transitive={"action": "store_true"},
        depth={"type": int, "default": 3})
    add("impact", "symbol", **paging, depth={"type": int, "default": 3},
        no_overridden={"action": "store_true"},
        group_by={"choices": ["module", "package"], "default": "module"})
    add("path", "source", "target", max_depth={"type": int, "default": 6},
        limit={"type": int, "default": 3})
    add("changed", base={"default": "HEAD"}, staged={"action": "store_true"},
        paths={"nargs": "*", "default": None}, depth={"type": int, "default": 3})
    add("deps", **paging, level={"choices": ["module", "package"], "default": "module"},
        scope={"default": ""}, target={"default": ""}, no_cycles={"action": "store_true"},
        sites={"action": "store_true"})
    add("dead", **paging, scope={"default": ""}, public_is_api={"action": "store_true"},
        include_tests={"action": "store_true"})
    add("census", **paging, scope={"default": ""},
        category={"choices": ["annotations", "entities"], "default": "annotations"})
    add("cache-status")
    add("status")
    add("setup", no_template={"action": "store_true"})


def _run_query(a) -> str:
    svc = Service(a.root, a.shards_dir, freshness=a.freshness, cache_dir=a.cache_dir,
                  index_external=a.index_external, config_file=a.config)
    t = a.tool
    if t == "find":
        return svc.find(a.name, limit=a.limit, offset=a.offset, match=a.match)
    if t == "refs":
        return svc.references(a.symbol, offset=a.offset, limit=a.limit,
                              include_definitions=a.defs, exclude_imports=a.no_imports,
                              by_name=a.by_name, by_name_globs=a.globs, scope=a.scope)
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
                           max_nodes=a.max_nodes, offset=a.offset, limit=a.limit,
                           scope=a.scope)
    if t == "callees":
        return svc.callees(a.symbol, include_fields=a.fields, include_types=a.types,
                           offset=a.offset, limit=a.limit)
    if t == "annotated":
        return svc.annotated(a.annotation, offset=a.offset, limit=a.limit, scope=a.scope)
    if t == "typed":
        return svc.typed(a.symbol, categories=a.only, offset=a.offset, limit=a.limit,
                         scope=a.scope, modifiers=a.modifiers, subtypes=a.subtypes)
    if t == "tests":
        return svc.tests(a.symbol, transitive=a.transitive, depth=a.depth)
    if t == "impact":
        return svc.impact(a.symbol, depth=a.depth, include_overridden=not a.no_overridden,
                          group_by=a.group_by, offset=a.offset, limit=a.limit)
    if t == "path":
        return svc.path(a.source, a.target, max_depth=a.max_depth, limit=a.limit)
    if t == "changed":
        return svc.changed(base=a.base, staged=a.staged, paths=a.paths, depth=a.depth)
    if t == "deps":
        return svc.deps(level=a.level, scope=a.scope, target=a.target, cycles=not a.no_cycles,
                        sites=a.sites, offset=a.offset, limit=a.limit)
    if t == "dead":
        return svc.dead_code(scope=a.scope, public_is_api=a.public_is_api,
                             include_tests=a.include_tests, offset=a.offset, limit=a.limit)
    if t == "census":
        return svc.census(scope=a.scope, limit=a.limit, category=a.category, offset=a.offset)
    if t == "cache-status":
        return svc.cache_status()
    if t == "status":
        return svc.index_status()
    if t == "setup":
        return svc.setup(include_template=not a.no_template)
    raise SystemExit(f"unknown tool {t}")


def _cache(a) -> str:
    import os

    from . import cachedir as C
    from .workspace import find_project_root

    base = a.cache_dir or C.default_base()
    if a.action == "status":
        rows = C.entries(base)
        if not rows:
            return f"no indexes under {base}"
        out = [f"{len(rows)} indexes under {base}:"]
        for e in rows:
            when = time.strftime("%Y-%m-%d", time.localtime(e["last_used"])) if e["last_used"] \
                else "?"
            gone = "" if e["exists"] else "  (root gone)"
            out.append(f"  {e['bytes'] / 1e6:7.1f} MB  used {when}  {e['root'] or e['dir']}{gone}")
        return "\n".join(out)
    if a.action == "prune":
        removed = C.prune(base, a.older_than)
        return f"removed {len(removed)} entries" + "".join(
            f"\n  {e['root'] or e['dir']}" for e in removed)
    if a.all:
        rows = C.entries(base)
        for e in rows:
            C.remove_entry(e["dir"])
        return f"removed {len(rows)} entries"
    root = find_project_root(a.root or os.getcwd())
    d = os.path.join(base, C.entry_name(root))
    if not os.path.isdir(d):
        return f"no index for {root}"
    C.remove_entry(d)
    return f"removed the index of {root}"


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
    s.add_argument("--cache-dir", help="where the auxiliary index lives (default: "
                                       "$XDG_CACHE_HOME/scipq, ~/.cache/scipq, or "
                                       "~/Library/Caches/scipq on macOS)")
    s.add_argument("--index-external", choices=["all", "methods", "none"], default="all",
                   help="references to JDK/library symbols kept in the index (default: all)")
    s.add_argument("--config", help="project configuration file (default: "
                                    "$XDG_CONFIG_HOME/scipq/config.toml)")
    s.add_argument("--tools", default="all",
                   help="tools to expose: all (default), core (the navigation basics), or a "
                        "comma-separated list of names; fewer tools cost less context")
    s.add_argument("--always", action="store_true",
                   help="expose the tools even when started outside a Java project (by default "
                        "the server stays idle there, with no tools)")

    sub.add_parser("gradle-init", help="print the Gradle init script that writes the shards")
    c = sub.add_parser("cache", help="manage the auxiliary indexes")
    c.add_argument("action", choices=["status", "prune", "clear"])
    c.add_argument("--cache-dir", help="cache base directory (default: user cache)")
    c.add_argument("--older-than", type=float, default=30.0,
                   help="prune: also entries unused for this many days (default 30)")
    c.add_argument("--root", help="clear: the checkout whose entry to remove (default: cwd)")
    c.add_argument("--all", action="store_true", help="clear: every entry")
    _query_parser(sub)

    a = parser.parse_args(argv)
    if a.command in (None, "serve"):
        from .server import serve

        serve(getattr(a, "root", None), getattr(a, "shards_dir", None),
              getattr(a, "freshness", "full"), getattr(a, "always", False),
              getattr(a, "cache_dir", None), getattr(a, "index_external", "all"),
              getattr(a, "config", None), getattr(a, "tools", "all"))
    elif a.command == "gradle-init":
        sys.stdout.write(_template())
    elif a.command == "query":
        print(_run_query(a))
    elif a.command == "cache":
        print(_cache(a))


if __name__ == "__main__":
    main()

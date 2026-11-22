"""MCP server (stdio). Works with the mcp SDK 2.x (MCPServer) and 1.x (FastMCP)."""

from __future__ import annotations

import functools
from typing import Annotated, Any, Callable

import anyio.to_thread
from pydantic import Field

from . import __version__
from .service import Service

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from mcp.types import ToolAnnotations

INSTRUCTIONS = """\
Java code navigation from the SCIP shards that javac (scip-javac plugin) writes at compile time.
Nothing is loaded in memory: each call greps the shards and decodes only the matching ones.

Results reflect the LAST COMPILATION. Answers end with a WARNING when sources changed after
it; a file that never compiled is not indexed. Paths are relative to the project root; lines
are 1-based.

Symbols can be given as names: `Account`, `Account#setState`, `Account.setState()`,
`com.example.Account`, `setState(+1)` (one overload), `Account#<init>` (constructor), or as
the exact SCIP symbol printed by find_symbols/definition (e.g. `com/example/Account#setState().`).
A unique name is resolved and the answer starts with `-- name → symbol`; an ambiguous name
returns the candidates instead.

project_root (optional on every tool) selects the checkout or worktree; by default, the one
the server was started in.
"""

ProjectRoot = Annotated[str | None, Field(
    description="Project root (checkout or worktree). Default: the server's working directory "
                "or --root. Any directory inside the project works.")]
Symbol = Annotated[str, Field(
    description="A name (`Type`, `Type#member`, `pkg.Type.member()`, `member(+1)`) or an exact "
                "SCIP symbol")]

READ_ONLY = ToolAnnotations.model_validate(
    {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
     "openWorldHint": False})


def build(service: Service) -> Any:
    mcp = _Server("scipq", instructions=INSTRUCTIONS)

    def tool(fn: Callable[..., str]) -> Callable[..., str]:
        """Registers an async wrapper that runs the blocking query in a worker thread."""

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> str:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

        mcp.tool(annotations=READ_ONLY)(wrapper)
        return fn

    @tool
    def find_symbols(
        name: Annotated[str, Field(description="Name to look up, e.g. `Account`, "
                                               "`Account#setState`, `setState`, `Account.Builder`")],
        project_root: ProjectRoot = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
    ) -> str:
        """Definitions in the project whose trailing names match (like workspace/symbol).
        Each row: path:line  kind  exact symbol — signature."""
        return service.find(name, project_root, limit)

    @tool
    def references(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        offset: Annotated[int, Field(ge=0, description="Skip this many references")] = 0,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
        include_definitions: Annotated[bool, Field(
            description="Also print where it is defined")] = False,
    ) -> str:
        """Every use of a symbol (like findReferences), grouped by file, with the line of code.
        Overrides are separate symbols: use implementations or callers(include_overridden)."""
        return service.references(symbol, project_root, offset, limit, include_definitions)

    @tool
    def definition(symbol: Symbol, project_root: ProjectRoot = None) -> str:
        """Where a symbol is defined, with kind, signature, javadoc and what it overrides or
        extends (like goToDefinition + hover)."""
        return service.definition(symbol, project_root)

    @tool
    def implementations(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        direct_only: Annotated[bool, Field(
            description="Only direct subtypes / overrides")] = False,
        limit: Annotated[int, Field(ge=1, le=5000)] = 200,
    ) -> str:
        """Subtypes of a class or interface, or overrides of a method, at any depth; indirect
        ones say which result they go through. Anonymous classes are included."""
        return service.implementations(symbol, project_root, direct_only, limit)

    @tool
    def supertypes(symbol: Symbol, project_root: ProjectRoot = None) -> str:
        """Supertypes of a type, or the methods a method overrides, direct ones first."""
        return service.supertypes(symbol, project_root)

    @tool
    def outline(
        file: Annotated[str, Field(description="Source path (relative to the project root or "
                                               "absolute), or a type name")],
        project_root: ProjectRoot = None,
    ) -> str:
        """Definitions in a file (like documentSymbol), nested, with line and signature."""
        return service.outline(file, project_root)

    @tool
    def callers(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        depth: Annotated[int, Field(ge=1, le=5, description="Levels of callers")] = 1,
        include_overridden: Annotated[bool, Field(
            description="Also calls to the methods this one overrides, which may dispatch "
                        "here")] = False,
        max_nodes: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> str:
        """Incoming calls / uses grouped by the enclosing method (like incomingCalls). Calls in
        lambdas and anonymous classes count for the enclosing method; field initializers for
        the field; initializer blocks for the class."""
        return service.callers(symbol, project_root, depth, include_overridden, max_nodes)

    @tool
    def callees(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        include_fields: bool = False,
        include_types: bool = False,
    ) -> str:
        """Methods and constructors referenced from the body of a definition (like
        outgoingCalls), with the lines; optionally fields and types too."""
        return service.callees(symbol, project_root, include_fields, include_types)

    return mcp


def serve(root: str | None = None, shards_dir: str | None = None,
          freshness: str = "full") -> None:
    build(Service(root, shards_dir, freshness)).run("stdio")


__all__ = ["build", "serve", "__version__"]

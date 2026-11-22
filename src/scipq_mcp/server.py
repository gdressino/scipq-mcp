"""MCP server (stdio). Works with the mcp SDK 2.x (MCPServer) and 1.x (FastMCP)."""

from __future__ import annotations

import functools
from typing import Annotated, Any, Callable, Literal

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
Java code navigation from the SCIP shards that javac writes when the build runs the scip-javac
plugin. Read-only: it never compiles, runs tests or writes to the repository.

FIRST USE: the tools need the shards. If a tool answers that there are no SCIP shards, call
`setup`: it inspects the build (Gradle/Maven) and returns the exact steps and the init script
or pom snippet to add. Ask the user before changing their build or ~/.gradle, then compile.

Results reflect the LAST COMPILATION. Answers end with a WARNING (and the task to recompile)
when sources changed after it; a file that never compiled is not indexed. Paths are relative
to the project root; lines are 1-based.

Symbols can be given as names: `Account`, `Account#setState`, `Account.setState()`,
`com.example.Account`, `setState(+1)` (one overload), `Account#<init>` (constructor), or as
the exact SCIP symbol printed by find_symbols/definition (e.g. `com/example/Account#setState().`).
JDK and library symbols work too (`PersistenceManager#evict`). A unique name is resolved and
the answer starts with `-- name → symbol`; an ambiguous name returns the candidates.

Every list takes offset/limit and says `next offset N` when there is more. `batch` runs several
queries in one call. project_root (optional everywhere) selects the checkout or worktree; by
default, the one the server was started in.
"""

ProjectRoot = Annotated[str | None, Field(
    description="Project root (checkout or worktree). Default: the server's working directory "
                "or --root. Any directory inside the project works.")]
Symbol = Annotated[str, Field(
    description="A name (`Type`, `Type#member`, `pkg.Type.member()`, `member(+1)`) or an exact "
                "SCIP symbol")]
Offset = Annotated[int, Field(ge=0, description="Skip this many items (pagination)")]


def Limit(default: int, hi: int = 5000):  # noqa: N802 - reads as a type
    return Annotated[int, Field(ge=1, le=hi, description="Maximum items to return")]


READ_ONLY = ToolAnnotations.model_validate(
    {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
     "openWorldHint": False})

TYPED_CATEGORIES = Literal["field", "parameter", "return type", "local variable", "supertype",
                           "method body", "initializer", "import", "other"]


INACTIVE_INSTRUCTIONS = ("scipq (Java code navigation) is idle: this is not a Java project, so it "
                         "exposes no tools.")


def _new_server(instructions: str) -> Any:
    try:
        return _Server("scipq", instructions=instructions, version=__version__)
    except TypeError:  # mcp 1.x FastMCP has no version argument
        return _Server("scipq", instructions=instructions)


def build(service: Service, active: bool = True) -> Any:
    """The MCP server. With active=False (not a Java project) it registers no tools, so a
    server installed for every project costs nothing where it does not apply."""
    if not active:
        return _new_server(INACTIVE_INSTRUCTIONS)
    mcp = _new_server(INSTRUCTIONS)

    def tool(fn: Callable[..., str]) -> Callable[..., str]:
        """Registers an async wrapper that runs the blocking query in a worker thread."""

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> str:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

        mcp.tool(annotations=READ_ONLY)(wrapper)
        return fn

    @tool
    def setup(project_root: ProjectRoot = None,
              include_template: Annotated[bool, Field(
                  description="Include the init script / pom snippet to add")] = True) -> str:
        """How this project produces the SCIP shards the other tools need, and whether they
        are there and fresh. When they are missing it returns the exact steps for its build
        (Gradle init script or Maven compiler configuration). It changes nothing itself."""
        return service.setup(project_root, include_template)

    @tool
    def find_symbols(
        name: Annotated[str, Field(description="Name to look up, e.g. `Account`, "
                                               "`Account#setState`, `setState`, `Account.Builder`")],
        project_root: ProjectRoot = None,
        offset: Offset = 0,
        limit: Limit(50) = 50,
    ) -> str:
        """Definitions in the project whose trailing names match (like workspace/symbol).
        Each row: path:line  kind  exact symbol — signature."""
        return service.find(name, project_root, limit, offset)

    @tool
    def references(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        offset: Offset = 0,
        limit: Limit(100) = 100,
        include_definitions: Annotated[bool, Field(
            description="Also print where it is defined")] = False,
        exclude_imports: Annotated[bool, Field(description="Leave out import lines")] = False,
        by_name: Annotated[bool, Field(
            description="Append possible uses by name that the index cannot see: the name (or "
                        "bean property name) inside string literals of .java files and in "
                        "other files matching by_name_globs")] = False,
        by_name_globs: Annotated[list[str] | None, Field(
            description="File globs for by_name, e.g. ['*.java', '*.xml']. Default *.java")]
        = None,
    ) -> str:
        """Every use of a symbol (like findReferences), grouped by file, with the line of code
        and [the enclosing method]. Overrides are separate symbols: see family / callers."""
        return service.references(symbol, project_root, offset, limit, include_definitions,
                                  exclude_imports, by_name, by_name_globs)

    @tool
    def definition(symbol: Symbol, project_root: ProjectRoot = None) -> str:
        """Where a symbol is defined, with kind, signature, javadoc and what it overrides or
        extends (like goToDefinition + hover). Lombok members point at what generates them."""
        return service.definition(symbol, project_root)

    @tool
    def source(symbol: Symbol, project_root: ProjectRoot = None,
               max_lines: Limit(200, 5000) = 200) -> str:
        """The exact source of a definition (method, field, class) with line numbers, so you do
        not have to read the whole file."""
        return service.source(symbol, project_root, max_lines)

    @tool
    def implementations(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        direct_only: Annotated[bool, Field(
            description="Only direct subtypes / overrides")] = False,
        offset: Offset = 0,
        limit: Limit(200) = 200,
    ) -> str:
        """Subtypes of a class or interface, or overrides of a method, at any depth; indirect
        ones say which result they go through. Anonymous classes are included."""
        return service.implementations(symbol, project_root, direct_only, limit, offset)

    @tool
    def supertypes(symbol: Symbol, project_root: ProjectRoot = None) -> str:
        """Supertypes of a type, or the methods a method overrides, direct ones first."""
        return service.supertypes(symbol, project_root)

    @tool
    def members(symbol: Symbol, project_root: ProjectRoot = None,
                inherited: Annotated[bool, Field(
                    description="Include members inherited from supertypes")] = True,
                offset: Offset = 0, limit: Limit(300) = 300) -> str:
        """Members of a type: its own, then those inherited from each supertype (not private,
        not constructors, not overridden), marking overrides and generated (Lombok) ones."""
        return service.members(symbol, project_root, inherited, offset, limit)

    @tool
    def family(symbol: Symbol, project_root: ProjectRoot = None, offset: Offset = 0,
               limit: Limit(100) = 100,
               show_uses: Annotated[bool, Field(description="List the uses too")] = True) -> str:
        """The override family of a method — what it overrides, its overrides, and the other
        overrides of its ancestors — with the uses of each. references on an abstract method
        does not see calls through the overrides; this does."""
        return service.family(symbol, project_root, offset, limit, show_uses)

    @tool
    def overloads(symbol: Symbol, project_root: ProjectRoot = None, offset: Offset = 0,
                  limit: Limit(100) = 100) -> str:
        """Every overload of a method in its type, with the uses of each."""
        return service.overloads(symbol, project_root, offset, limit)

    @tool
    def outline(
        file: Annotated[str, Field(description="Source path (relative to the project root or "
                                               "absolute), or a type name")],
        project_root: ProjectRoot = None,
        offset: Offset = 0,
        limit: Limit(500) = 500,
    ) -> str:
        """Definitions in a file (like documentSymbol), nested, with line and signature."""
        return service.outline(file, project_root, offset, limit)

    @tool
    def callers(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        depth: Annotated[int, Field(ge=1, le=5, description="Levels of callers")] = 1,
        include_overridden: Annotated[bool, Field(
            description="Also calls to the methods this one overrides, which may dispatch "
                        "here")] = False,
        max_nodes: Annotated[int, Field(ge=1, le=2000)] = 200,
        offset: Offset = 0,
        limit: Limit(100) = 100,
    ) -> str:
        """Incoming calls / uses grouped by the enclosing method (like incomingCalls). Calls in
        lambdas and anonymous classes count for the enclosing method; field initializers for
        the field; initializer blocks for the class. offset/limit page the direct callers."""
        return service.callers(symbol, project_root, depth, include_overridden, max_nodes,
                               offset, limit)

    @tool
    def callees(
        symbol: Symbol,
        project_root: ProjectRoot = None,
        include_fields: bool = False,
        include_types: bool = False,
        offset: Offset = 0,
        limit: Limit(100) = 100,
    ) -> str:
        """Methods and constructors referenced from the body of a definition (like
        outgoingCalls), with the lines; optionally fields and types too."""
        return service.callees(symbol, project_root, include_fields, include_types, offset,
                               limit)

    @tool
    def annotated(
        annotation: Annotated[str, Field(description="Annotation type, e.g. `Deprecated`, "
                                                     "`Entity`, `org.junit.jupiter.api.Test`")],
        project_root: ProjectRoot = None,
        offset: Offset = 0,
        limit: Limit(100) = 100,
    ) -> str:
        """Classes, methods, fields and parameters carrying an annotation, with the annotation's
        line (which holds its attribute values)."""
        return service.annotated(annotation, project_root, offset, limit)

    @tool
    def typed(
        symbol: Annotated[str, Field(description="A type, e.g. `EntityManager`, `Account`")],
        project_root: ProjectRoot = None,
        categories: Annotated[list[TYPED_CATEGORIES] | None, Field(
            description="Only these kinds of use")] = None,
        offset: Offset = 0,
        limit: Limit(100) = 100,
    ) -> str:
        """Where a type is used, by kind: fields, parameters, return types, local variables,
        supertype clauses, method bodies (new, casts, static calls) and imports."""
        return service.typed(symbol, project_root, categories, offset, limit)

    @tool
    def tests(symbol: Symbol, project_root: ProjectRoot = None) -> str:
        """Test classes (in test compile tasks) that use a symbol directly, with a ready
        `--tests` filter for Gradle. It does not run them."""
        return service.tests(symbol, project_root)

    @tool
    def index_status(project_root: ProjectRoot = None) -> str:
        """The state of the index: shards per compile task, stale and not-yet-compiled
        sources, git-tracked .java files without a shard, and what to recompile."""
        return service.index_status(project_root)

    @tool
    def batch(
        queries: Annotated[list[dict[str, Any]], Field(
            description="[{'tool': 'references', 'args': {'symbol': 'Account#setState'}}, …] — "
                        "any tool above except setup and batch, with its arguments")],
        project_root: ProjectRoot = None,
    ) -> str:
        """Run several queries on the same checkout in one call (one freshness check)."""
        return service.batch(queries, project_root)

    prompt = getattr(mcp, "prompt", None)
    if prompt is not None:
        @prompt(name="setup", description="Set up SCIP indexing for this project")
        def setup_prompt() -> str:
            return ("Call the scipq `setup` tool for this project. If the shards are missing or "
                    "stale, explain the steps it returns, ask me before changing the build or "
                    "~/.gradle, and once I agree make the change and tell me which build "
                    "command to run (do not run long builds without asking).")

    return mcp


def serve(root: str | None = None, shards_dir: str | None = None,
          freshness: str = "full", always: bool = False) -> None:
    import os
    import sys

    from .setup import java_project

    active, why = (True, "--always") if always else java_project(root or os.getcwd())
    if not active:
        print(f"scipq-mcp: {why}; no tools exposed (start with --always to override)",
              file=sys.stderr)
    build(Service(root, shards_dir, freshness), active).run("stdio")


__all__ = ["build", "serve", "__version__"]

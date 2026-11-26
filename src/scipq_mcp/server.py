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

WHEN TO USE: for anything about Java symbols — uses, callers, hierarchy, overrides, Lombok
accessors, a method's body — use these tools instead of grep. grep also matches same-named
members of other classes and text in comments; these tools do not, and they answer in well
under a second with no startup. Use grep for text (string literals, comments, logs, non-Java
files) and for code edited since the last compilation. If an LSP/IDE tool is also available,
prefer these unless you need uncompiled code. find_symbols finds a symbol from part of its
name. Before changing a symbol, start with `context`: definition, body, callers, callees,
overrides and tests in one answer, trimmed to a token budget. To change code across files
(rename, signature, move, API migration): call refactor_help first, then refactor; it returns
a verified patch for you to apply, never edits the checkout itself.

FIRST USE: the tools need the shards. If a tool answers that there are no SCIP shards, call
`setup`: it inspects the build (Gradle/Maven) and returns the exact steps and the init script
or pom snippet to add. Ask the user before changing their build or ~/.gradle, then compile.

Results reflect the LAST COMPILATION. Answers end with a WARNING (and the task to recompile)
when sources changed after it, explained in full once, then in a line; a file that never
compiled is not indexed. Paths are relative to the project root; lines are 1-based.

Symbols can be given as names: `Account`, `Account#setState`, `com.example.Account`,
`Account#<init>` (constructor); with overloads, `pick(String, int)` (by parameter types),
`pick()` (the one without parameters) or `pick(+1)` (SCIP's numbering, in declaration
order); answers write overloaded methods with their parameter list, which reads back as the
same overload. Or the exact SCIP symbol printed by find_symbols/definition (e.g.
`com/example/Account#setState().`).
JDK and library symbols work too (`PersistenceManager#evict`). A unique name is resolved and
the answer starts with `-- name → symbol`; an ambiguous name returns the candidates.

Every list takes offset/limit and says `next offset N` when there is more. `batch` runs several
queries in one call. project_root (optional everywhere) selects the checkout or worktree; by
default, the one the server was started in.
"""

ProjectRoot = Annotated[str, Field(
    description="Checkout or worktree to query (any directory in it); default: where the "
                "server started")]
Symbol = Annotated[str, Field(description="Name or exact SCIP symbol (forms: see the server "
                                         "instructions)")]
Offset = Annotated[int, Field(ge=0, description="Items to skip")]


def Limit(default: int, hi: int = 5000):  # noqa: N802 - reads as a type
    return Annotated[int, Field(ge=1, le=hi, description="Max items")]


READ_ONLY = ToolAnnotations.model_validate(
    {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
     "openWorldHint": False})

TYPED_CATEGORIES = Literal["field", "parameter", "return type", "throws", "local variable",
                           "resource", "supertype", "annotation", "method body", "initializer",
                           "qualifier", "import", "other"]


INACTIVE_INSTRUCTIONS = ("scipq (Java code navigation) is idle: this is not a Java project, so it "
                         "exposes no tools.")


def _new_server(instructions: str) -> Any:
    try:
        return _Server("scipq", instructions=instructions, version=__version__)
    except TypeError:  # mcp 1.x FastMCP has no version argument
        return _Server("scipq", instructions=instructions)


CORE_TOOLS = {"setup", "context", "find_symbols", "references", "definition", "source",
              "implementations",
              "supertypes", "members", "callers", "callees", "outline", "tests", "index_status",
              "batch"}


def tool_selection(spec: str) -> set[str] | None:
    """--tools: "all" (None: everything), "core", or a comma-separated list of tool names."""
    spec = (spec or "all").strip()
    if spec == "all":
        return None
    if spec == "core":
        return set(CORE_TOOLS)
    return {t.strip() for t in spec.split(",") if t.strip()} | {"setup"}


def build(service: Service, active: bool = True, tools: set[str] | None = None) -> Any:
    """The MCP server. With active=False (not a Java project) it registers no tools, so a
    server installed for every project costs nothing where it does not apply."""
    if not active:
        return _new_server(INACTIVE_INSTRUCTIONS)
    mcp = _new_server(INSTRUCTIONS)

    def tool(fn: Callable[..., str]) -> Callable[..., str]:
        """Registers an async wrapper that runs the blocking query in a worker thread."""
        if tools is not None and fn.__name__ not in tools:
            return fn

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> str:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

        try:  # plain text results: no outputSchema, no duplicated structuredContent
            mcp.tool(annotations=READ_ONLY, structured_output=False)(wrapper)
        except TypeError:  # older SDKs
            mcp.tool(annotations=READ_ONLY)(wrapper)
        return fn

    @tool
    def setup(project_root: ProjectRoot = "",
              include_template: Annotated[bool, Field(
                  description="Include the init script / pom snippet to add")] = True) -> str:
        """How this project produces the SCIP shards the other tools need, and whether they
        are there and fresh. When they are missing it returns the exact steps for its build
        (Gradle init script or Maven compiler configuration). It changes nothing itself."""
        return service.setup(project_root, include_template)

    @tool
    def find_symbols(
        name: Annotated[str, Field(description="Name or part of it, e.g. `Account`, "
                                               "`Account#setState`, `setState`, `Blanqueo`")],
        project_root: ProjectRoot = "",
        match: Annotated[Literal["auto", "exact", "prefix", "contains", "camel"], Field(
            description="auto: exact name, else names containing it. prefix/contains/camel "
                        "(humps: `getUA` → getUserAccount) ignore case and apply to the last "
                        "name; owner names before it must match")] = "auto",
        offset: Offset = 0,
        limit: Limit(50) = 50,
    ) -> str:
        """Definitions in the project by name or part of a name (like workspace/symbol).
        Each row: path:line  kind  exact symbol — signature."""
        return service.find(name, project_root, limit, offset, match)

    @tool
    def references(
        symbol: Symbol,
        project_root: ProjectRoot = "",
        offset: Offset = 0,
        limit: Limit(100) = 100,
        include_definitions: Annotated[bool, Field(
            description="Also print where it is defined")] = False,
        exclude_imports: Annotated[bool | None, Field(
            description="Leave out import lines (default: yes for types, no otherwise)")] = None,
        by_name: Annotated[bool, Field(
            description="Append what the index cannot see: the name (or bean property name) "
                        "inside string literals of .java files and in other files matching "
                        "by_name_globs, and javadoc {@link}/@see mentions")] = False,
        by_name_globs: Annotated[list[str] | None, Field(
            description="File globs for by_name, e.g. ['*.java', '*.xml']. Default *.java")]
        = None,
        scope: Annotated[str, Field(description="Only results in this package (prefix or glob) "
                                                "or, with '/', source path glob")] = "",
    ) -> str:
        """Every use of a symbol (like findReferences), grouped by file, with the line of code
        and [the enclosing method]. Exact: only this symbol, not same-named members of other
        types. Overrides are separate symbols: see family / callers. Large results open with a
        summary."""
        return service.references(symbol, project_root, offset, limit, include_definitions,
                                  exclude_imports, by_name, by_name_globs, scope)

    @tool
    def definition(symbol: Symbol, project_root: ProjectRoot = "") -> str:
        """Where a symbol is defined, with kind, signature, javadoc and what it overrides or
        extends (like goToDefinition + hover). Lombok members point at what generates them.
        JDK and library symbols come from the JDK's src.zip and the -sources.jar files in the
        Gradle/Maven caches, saying which version."""
        return service.definition(symbol, project_root)

    @tool
    def context(symbol: Symbol, project_root: ProjectRoot = "",
                token_budget: Annotated[int, Field(
                    ge=250, le=32000, description="Approximate size of the answer in tokens; "
                                                  "a small symbol gets a smaller answer")]
                = 4000) -> str:
        """Start here before changing a symbol: one answer with what you would otherwise ask
        for in 5-6 calls. A method: definition, body, what overrides it, callers (call lines),
        callees and tests. A type: declaration, members, subtypes, files using it, tests. A
        field: accessors (Lombok too), writers and readers, tests. Library and JDK symbols:
        their declaration and javadoc, and their uses here. Trimmed to the budget; each cut
        section names the tool that shows all of it."""
        return service.context(symbol, project_root, token_budget)

    @tool
    def steps(
        query: Annotated[str, Field(description="A step definition (method), a .feature path, "
                                                "or a step's text (with or without keyword); "
                                                "empty with check=True")] = "",
        project_root: ProjectRoot = "",
        check: Annotated[bool, Field(description="The whole project: undefined and ambiguous "
                                                 "steps, step definitions not understood or "
                                                 "unused")] = False,
        offset: Offset = 0,
        limit: Limit(100) = 100,
    ) -> str:
        """Cucumber-JVM. For a step definition: its expression and every feature step it runs,
        with the command to run those scenarios. For a .feature: each step and the method that
        runs it (undefined/ambiguous marked). For a step's text: the method(s) it runs. tests
        (transitive) and changed also list the scenarios affected. A {names} filter matches
        outline rows by their filled-in name; reports show the template name: keep the
        filter as printed."""
        return service.steps(query, project_root, check, offset, limit)

    @tool
    def compare(
        other_root: Annotated[str, Field(description="The other checkout: a worktree or clone "
                                                     "of this project, compiled")] = "",
        project_root: ProjectRoot = "",
        scope: Annotated[str, Field(description="Only this package (prefix or glob) or, with "
                                                "'/', source path glob")] = "",
        api_only: Annotated[bool, Field(description="Only public/protected definitions")] = True,
        limit: Annotated[int, Field(ge=1, le=1000, description="Rows per section")] = 50,
        all_worktrees: Annotated[bool, Field(
            description="Instead of other_root: every other git worktree of this repository, "
                        "one line each (at most 2 missing indexes built per call)")] = False,
    ) -> str:
        """Compares this checkout (A) with another (B), e.g. two git worktrees: definitions only
        in one side or with another signature (maybe-renamed pairs too) and, when they share git
        history, what each changed since the merge base, members changed on both sides
        (conflicts), and what merging one into the other would break: symbols one side removed
        or changed in files only it touched, with the other side's uses. Builds B's auxiliary
        index if missing (not while a build runs there). Read-only: never merges."""
        return service.compare(other_root, project_root, scope, api_only, limit, all_worktrees)

    @tool
    def source(symbol: Symbol, project_root: ProjectRoot = "",
               max_lines: Annotated[int, Field(ge=1, le=5000,
                                               description="Maximum source lines")] = 200,
               git_ref: Annotated[str, Field(description=(
                   "Read it from this git branch, tag or commit (`origin/main`) instead of "
                   "the checkout: found by its type's file and parsed, without the index, "
                   "so also code never compiled here"))] = "") -> str:
        """The exact source of a definition (method, field, class) with line numbers, so you do
        not have to read the whole file; also for JDK and library symbols (from their sources
        jars), and as it is in another git ref (git_ref)."""
        return service.source(symbol, project_root, max_lines, git_ref)

    @tool
    def implementations(
        symbol: Symbol,
        project_root: ProjectRoot = "",
        direct_only: Annotated[bool, Field(
            description="Only direct subtypes / overrides")] = False,
        offset: Offset = 0,
        limit: Limit(200) = 200,
    ) -> str:
        """Subtypes of a class or interface, or overrides of a method, at any depth; indirect
        ones say which result they go through. Anonymous classes are included."""
        return service.implementations(symbol, project_root, direct_only, limit, offset)

    @tool
    def supertypes(symbol: Symbol, project_root: ProjectRoot = "") -> str:
        """Supertypes of a type, or the methods a method overrides, direct ones first."""
        return service.supertypes(symbol, project_root)

    @tool
    def members(symbol: Symbol, project_root: ProjectRoot = "",
                inherited: Annotated[bool, Field(
                    description="Include members inherited from supertypes")] = True,
                offset: Offset = 0, limit: Limit(300) = 300) -> str:
        """Members of a type: its own, then those inherited from each supertype (not private,
        not constructors, not overridden), marking overrides and generated (Lombok) ones;
        library supertypes are listed from their sources jars, nearest first, without what a
        nearer type overrides. Library types themselves work too (e.g. `VerticalLayout`)."""
        return service.members(symbol, project_root, inherited, offset, limit)

    @tool
    def family(symbol: Symbol, project_root: ProjectRoot = "", offset: Offset = 0,
               limit: Limit(100) = 100,
               show_uses: Annotated[bool, Field(description="List the uses too")] = True) -> str:
        """The override family of a method — what it overrides, its overrides, and the other
        overrides of its ancestors — with the uses of each. references on an abstract method
        does not see calls through the overrides; this does. For a library method it adds,
        from the sources jars, the library methods it overrides and the overrides in library
        subtypes this project calls (Collection#size: List#size, ArrayList#size…)."""
        return service.family(symbol, project_root, offset, limit, show_uses)

    @tool
    def overloads(symbol: Symbol, project_root: ProjectRoot = "", offset: Offset = 0,
                  limit: Limit(100) = 100) -> str:
        """Every overload of a method in its type, with the uses of each in the project; for
        a library or JDK method, its overloads come from the sources jar."""
        return service.overloads(symbol, project_root, offset, limit)

    @tool
    def outline(
        file: Annotated[str, Field(description="Source path (relative to the project root or "
                                               "absolute), or a type name")],
        project_root: ProjectRoot = "",
        offset: Offset = 0,
        limit: Limit(500) = 500,
    ) -> str:
        """Definitions in a file (like documentSymbol), nested, with line and signature."""
        return service.outline(file, project_root, offset, limit)

    @tool
    def callers(
        symbol: Symbol,
        project_root: ProjectRoot = "",
        depth: Annotated[int, Field(ge=1, le=5, description="Levels of callers")] = 1,
        include_overridden: Annotated[bool, Field(
            description="Also calls to the methods each one in the tree overrides (in an "
                        "interface or base class), which may dispatch to it. Without it, a "
                        "method called only through its interface has no callers, and the "
                        "tree stops at a caller that is such a method")] = False,
        max_nodes: Annotated[int, Field(ge=1, le=2000, description=(
            "Stop after this many callers in the whole tree"))] = 200,
        offset: Offset = 0,
        limit: Limit(100) = 100,
        scope: Annotated[str, Field(description="Only results in this package (prefix or glob) "
                                                "or, with '/', source path glob")] = "",
    ) -> str:
        """Incoming calls / uses grouped by the enclosing method (like incomingCalls). Exact:
        only this symbol, not same-named members of other types; so `shape.draw()` calls
        Shape#draw, not Circle#draw, which overrides it. include_overridden adds those calls,
        marked `via`, at every level of the tree; without it, the answer says how many it left
        out and which callers it did not follow. impact follows them by default and keeps
        only the calls that may run on the right objects. Calls in lambdas and anonymous
        classes count for the enclosing method; field initializers for the field; initializer
        blocks for the class. offset/limit page the direct callers."""
        return service.callers(symbol, project_root, depth, include_overridden, max_nodes,
                               offset, limit, scope)

    @tool
    def callees(
        symbol: Symbol,
        project_root: ProjectRoot = "",
        include_fields: Annotated[bool, Field(
            description="Also fields read or written in the body")] = False,
        include_types: Annotated[bool, Field(
            description="Also types mentioned in the body")] = False,
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
        project_root: ProjectRoot = "",
        offset: Offset = 0,
        limit: Limit(100) = 100,
        scope: Annotated[str, Field(description="Only results in this package (prefix or glob) "
                                                "or, with '/', source path glob")] = "",
    ) -> str:
        """Classes, methods, fields and parameters carrying an annotation, with the annotation's
        line (which holds its attribute values)."""
        return service.annotated(annotation, project_root, offset, limit, scope)

    @tool
    def typed(
        symbol: Annotated[str, Field(description="A type, e.g. `EntityManager`, `Account`")],
        project_root: ProjectRoot = "",
        categories: Annotated[list[TYPED_CATEGORIES] | None, Field(
            description="Only these kinds of use: field, parameter, return type, throws, "
                        "local variable, resource (try-with-resources), supertype, annotation "
                        "(`@Label`), method body, initializer, qualifier (`Map` in "
                        "`Map.Entry`), import, other")]
        = None,
        offset: Offset = 0,
        limit: Limit(100) = 100,
        scope: Annotated[str, Field(description="Only results in this package (prefix or glob) "
                                                "or, with '/', source path glob")] = "",
        modifiers: Annotated[list[str] | None, Field(
            description="Only uses whose declaration has all these modifiers, e.g. "
                        "['static'] with categories=['field']")] = None,
        subtypes: Annotated[bool, Field(
            description="Also uses of its subtypes (project and library ones, e.g. "
                        "ConcurrentHashMap for Map)")] = False,
    ) -> str:
        """Where a type is used, by kind: fields, parameters, return types, local variables,
        try-with-resources, supertype clauses, method bodies (new, casts, static calls) and
        imports."""
        return service.typed(symbol, project_root, categories, offset, limit, scope, modifiers,
                             subtypes)

    @tool
    def tests(symbol: Symbol, project_root: ProjectRoot = "",
              transitive: Annotated[bool, Field(
                  description="Also tests that reach it through callers (uses the index)")]
              = False,
              depth: Annotated[int, Field(ge=1, le=8, description="Caller levels when "
                                                                  "transitive")] = 3) -> str:
        """Test classes (in test compile tasks or src/*test*/) that use a symbol, directly or
        (transitive) through callers, with a ready filter: `--tests` for Gradle (./gradlew when
        present), `-Dtest` for Maven. It does not run them."""
        return service.tests(symbol, project_root, transitive, depth)

    @tool
    def impact(symbol: Symbol, project_root: ProjectRoot = "",
               depth: Annotated[int, Field(ge=1, le=10, description="Caller levels")] = 3,
               include_overridden: Annotated[bool, Field(
                   description="Count calls to the methods it overrides (may dispatch here)")]
               = True,
               group_by: Literal["module", "package"] = "module",
               offset: Offset = 0, limit: Limit(100) = 100) -> str:
        """Transitive callers of a symbol, by depth, grouped by module or package, marking
        tests: what a change can affect, and the entry points it reaches (screens, endpoints
        with their routes, jobs…, one row per type, searched to depth 6 even when the rows
        stop sooner). Where the chain stops at an override of a library method (a listener,
        run(), a servlet's service()) it lists it with where its object is created: the
        library calls it, so those are its real callers; and callers whose calls are all
        inside an anonymous class or a lambda (they run when whoever receives it calls it).
        Not seen: calls through reflection and code compiled without the plugin, so few or
        no callers is not proof that nothing calls it. Uses the auxiliary index (built once per
        checkout, then updated incrementally)."""
        return service.impact(symbol, project_root, depth, include_overridden, group_by,
                              offset, limit)

    @tool
    def path(source: Annotated[str, Field(description="Where the chain starts (a caller)")],
             target: Annotated[str, Field(description="Where it ends (the callee)")],
             project_root: ProjectRoot = "",
             max_depth: Annotated[int, Field(ge=1, le=12)] = 6,
             limit: Annotated[int, Field(ge=1, le=20, description="Max paths")] = 3,
             avoid: Annotated[list[str] | None, Field(
                 description="Methods or types (all their members) the paths must not go "
                             "through: to look for paths other than the ones found")] = None
             ) -> str:
        """Shortest call chains (fewest calls) from one symbol to another, with the lines of
        each call, and how many there are in all. Longer ones are not listed: when every
        path found goes through a method, the answer says so; `avoid` it to look for
        others. A call of a method that an override on the way overrides counts where it may
        run that override (`A ⇒ B`): from B's hierarchy or from files that may hold a B, not
        from files naming only another subtype. Not seen: reflection and code compiled
        without the plugin, so no path is not proof that none exists."""
        return service.path(source, target, project_root, max_depth, limit, avoid)

    @tool
    def changed(project_root: ProjectRoot = "",
                base: Annotated[str, Field(description="git revision to compare with")] = "HEAD",
                staged: Annotated[bool, Field(description="Only staged changes")] = False,
                paths: Annotated[list[str] | None, Field(description="Limit to these paths")]
                = None,
                depth: Annotated[int, Field(ge=1, le=6, description="Caller levels")] = 2) -> str:
        """From `git diff` (read-only) to the definitions whose lines changed, their callers and
        the tests to run. Hunks that only delete code cannot be mapped and are listed; files
        edited after their last compilation are refused until recompiled."""
        return service.changed(project_root, base, staged, paths, depth)

    @tool
    def deps(project_root: ProjectRoot = "",
             level: Literal["module", "package"] = "module",
             scope: Annotated[str, Field(description="From: module or package (prefix or glob)")]
             = "",
             target: Annotated[str, Field(description="To: module/package, or a symbol prefix "
                                                      "such as `org/apache/commons/` with sites")] = "",
             cycles: Annotated[bool, Field(description="Report cycles")] = True,
             sites: Annotated[bool, Field(description="List the call sites from scope to "
                                                      "target instead of counts")] = False,
             offset: Offset = 0, limit: Limit(100) = 100) -> str:
        """Dependencies between modules or packages (reference counts) and their cycles; with
        sites, every place where `scope` uses `target`."""
        return service.deps(project_root, level, scope, target, cycles, sites, offset, limit)

    @tool
    def dead_code(project_root: ProjectRoot = "",
                  scope: Annotated[str, Field(description="Package (prefix or glob) or source "
                                                          "path glob")] = "",
                  public_is_api: Annotated[bool, Field(
                      description="Treat public members as API used from outside")] = False,
                  include_tests: Annotated[bool, Field(description="Also test code")] = False,
                  offset: Offset = 0, limit: Limit(100) = 100) -> str:
        """Candidates for dead code: definitions nobody references, leaving out overrides,
        framework-managed members (non-JDK annotations), main and serialization hooks, no-arg
        constructors, Lombok accessors in use and names found in string literals. Candidates,
        not verdicts: reflection can still reach them."""
        return service.dead_code(project_root, scope, public_is_api, include_tests, offset,
                                 limit)

    @tool
    def persistence(
        symbol: Annotated[str, Field(description="An entity, a field of one, or empty for the "
                                                 "list of entities")] = "",
        project_root: ProjectRoot = "",
        scope: Annotated[str, Field(description="With no symbol: only entities in this "
                                                "package or path")] = "",
        offset: Offset = 0, limit: Limit(100, 2000) = 100,
    ) -> str:
        """The persistence model the active profiles describe (jdo, jpa…). An entity: its table
        (or its superclass's), inheritance, discriminator, identity, version, persistent
        fields with their columns (explicit or from the field name; for mappedBy collections
        the other side's key, for join tables their columns), relations (also through an
        interface), fetch groups and unique/index members. A field: its column and relation,
        its accessors, who writes and reads it (setters, Lombok's too, and assignments), and
        the lines that use it by name (forms, relation paths, step tables, queries, keys),
        most likely to matter first; offset/limit page them."""
        return service.persistence(symbol, project_root, scope, offset, limit)

    @tool
    def check_refs(
        project_root: ProjectRoot = "",
        files: Annotated[list[str] | None, Field(
            description="Globs or folders of the docs to check instead of the scope (any doc: "
                        "a record, a plan). Default: the scope, see below")] = None,
        since: Annotated[str, Field(
            description="A commit or branch (HEAD, main): of the scope, the docs changed since "
                        "its merge base with HEAD (uncommitted changes included; only their "
                        "edited lines and mentions of changed code) and those that mention "
                        ".java files changed since then (only those mentions)")] = "",
        exclude: Annotated[list[str] | None, Field(
            description="Globs to leave out, added to [check_refs] exclude of "
                        ".scipq.toml")] = None,
        offset: Offset = 0, limit: Limit(200, 2000) = 200,
        format: Annotated[Literal["text", "md"], Field(
            description="text (by file) or md (a table to hand over)")] = "text",
    ) -> str:
        """The code that docs mention, checked against the index: Type#member, Type.method(…),
        qualified type names, {@link}s and File.java:N paths. Reports types and members not
        there (with the closest names and where that name lives now), parameter lists no
        overload has, missing files, and line numbers that no longer fall in the symbol named
        next to them; counts by kind, then by file.
        Scope: without files, the files agents read to work: CLAUDE.md and AGENTS.md (any
        folder), .claude/**/*.md, .github/**/*.md and README*.md (not archive folders,
        third-party READMEs, or a link to a file already read). A project opts in to more or fewer with `[check_refs] files =
        ["docs/guides", "**/CLAUDE.md", …]` (globs or folders relative to the checkout: *
        stays in its folder, ** crosses folders; it replaces the default) in .scipq.toml at
        the checkout root, or in its [[project]] entry of ~/.config/scipq/config.toml. The
        output's `scope:` line says which set was read and how many other .md files were not.
        When: after changing code, since="HEAD" (or the branch you started from) finds the
        docs in scope your change left wrong, and lists the docs out of it that mention the
        changed code (a name several files have counts only with its folder or package); before committing a doc, files=[that doc];
        before relying on an older doc's line numbers or names, files=[that doc].
        In a git checkout, line numbers are followed from the version the doc was written
        against. What to do: "line N is now M" (the code only moved): fix the number;
        "changed since the doc named X (date, commit)": re-read what the doc claims, the code
        is different; "so in the doc's version too": the doc was wrong from the start, check
        what it meant; "deleted (date, commit)": the file is gone; "planned?" / "never had":
        nothing to fix in the code, maybe in the doc. [check_refs] other_versions lists the
        words a doc writes before a path for another branch's version (v4). Library types,
        placeholders (Foo, getX), Object/Class/enum members, mentions the line says are gone
        ("removed") and archive folders (archive/, old/…) are left alone."""
        return service.check_refs(project_root, files, exclude, offset, limit, format,
                                  since)

    @tool
    def doctor(project_root: ProjectRoot = "") -> str:
        """What can make the index wrong or the tools weak here, as a checklist with what to do:
        the plugin's setup, tracked .java files without shards, classes built without the
        plugin (an IDE build) or shards whose javac failed, stale files and running builds,
        the Gradle build cache, the cache (and indexes of deleted worktrees), configuration
        warnings, the JDK for library sources, and what refactoring would use."""
        return service.doctor(project_root)

    @tool
    def concepts(
        query: Annotated[str, Field(description="Words for what you look for, separated by "
                                                "spaces; a word's forms and synonyms as "
                                                "alternatives with | (no spaces around it): "
                                                "'alta|crear cliente', "
                                                "'retry|reintento payment|pago', 'audit log'")],
        project_root: ProjectRoot = "",
        kind: Annotated[Literal["any", "type", "method", "field"], Field(
            description="Only this kind of definition")] = "any",
        scope: Annotated[str, Field(description="Package (prefix or glob), module or source "
                                                "path glob")] = "",
        include_tests: Annotated[bool, Field(description="Also test code")] = False,
        include_generated: Annotated[bool, Field(
            description="Also files whose header says they are generated")] = False,
        offset: Offset = 0, limit: Limit(20, 200) = 20,
    ) -> str:
        """Definitions about something, when you do not know their names. Words, not meaning,
        in names (camelCase split), owners, annotation texts (columns, paths, step texts…),
        signatures and javadoc. Space-separated words are different words: results rank by
        how many of them they hold, and how surely (not all are required). `|` joins the
        alternatives of ONE word: its forms, synonyms, the other language
        (pago|pagar|payment), each a prefix, accents ignored (a singular finds its plural); a
        word alone is reduced to a light stem. Synonyms written as separate words
        (`pago payment`) ask for definitions with both. If the top results do not fit, the
        code uses other words: retry with the words of the names you got, abbreviations or
        English, and fewer words; then follow a name with find or refs."""
        return service.concepts(query, project_root, kind, scope, include_tests, offset, limit,
                                include_generated)

    @tool
    def census(project_root: ProjectRoot = "",
               category: Annotated[Literal["annotations", "entities"], Field(
                   description="annotations: which are used and how much; entities: persistent "
                               "types and their tables (needs a jpa/jdo profile)")]
               = "annotations",
               scope: Annotated[str, Field(description="Source path glob")] = "",
               offset: Offset = 0, limit: Limit(50, 2000) = 50) -> str:
        """Censuses over the project: annotations in use, or entities with their tables."""
        return service.census(project_root, scope, limit, category, offset)

    @tool
    def cache_status(project_root: ProjectRoot = "") -> str:
        """Where the auxiliary index of this checkout is, its size and row counts."""
        return service.cache_status(project_root)

    @tool
    def index_status(project_root: ProjectRoot = "") -> str:
        """The state of the index: shards per compile task, stale and not-yet-compiled
        sources, git-tracked .java files without a shard, and what to recompile."""
        return service.index_status(project_root)

    @tool
    def refactor(
        plan: Annotated[dict[str, Any] | str | None, Field(
            description='{"steps": [...]}: macros (rename, change_signature, replace_calls, '
                        'move, annotate, create) or select/do primitives; refactor_help '
                        "explains them")] = None,
        project_root: ProjectRoot = "",
        dry_run: Annotated[bool, Field(description="Only resolve: sites, nodes, captures; no "
                                                   "patch")] = False,
        plan_id: Annotated[str, Field(description="With verify: the id a previous call "
                                                  "printed")] = "",
        verify: Annotated[bool, Field(description="After applying the patch and compiling: "
                                                  "check the new index, list tests to run")]
        = False,
    ) -> str:
        """Runs a refactoring plan and returns a patch (stored in the cache, never applied):
        sites from the index, syntax checked with JavaParser in a short-lived JVM, plus checks
        a compiler does not make (columns of persistent fields, shadowing, library
        overrides). Apply it with git apply after asking the user, compile, then verify."""
        return service.refactor(plan, project_root, dry_run, plan_id, verify)

    @tool
    def refactor_help(
        topic: Annotated[Literal["all", "flow", "macros", "select", "actions", "placeholders",
                                 "checks", "examples"], Field(
            description="What to explain")] = "all",
    ) -> str:
        """The refactoring plan format, with examples. Read it before writing a plan."""
        from . import refactor as R
        return R.help_topic(topic)

    @tool
    def batch(
        queries: Annotated[list[dict[str, Any]], Field(
            description="[{'tool': 'references', 'args': {'symbol': 'Account#setState'}}, …] — "
                        "any tool above except setup and batch, with its arguments (in 'args' "
                        "or next to 'tool')")],
        project_root: ProjectRoot = "",
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

        @prompt(name="refactor", description="Refactor Java code with a verified plan")
        def refactor_prompt(what: str = "") -> str:
            return ("Refactor with scipq: " + (what + ". " if what else "") +
                    "Call refactor_help, write a plan, run refactor with dry_run=true and "
                    "adjust it until the sites are the right ones, then run it for real. Show "
                    "me the summary, the review list and the diff, and ask me before applying "
                    "the patch with git apply. After I agree and the code is compiled, run "
                    "refactor with plan_id and verify=true and tell me which tests to run.")

    _slim_schemas(mcp)
    return mcp


def _slim_schemas(mcp: Any) -> None:
    """Drop the per-property `title`s pydantic adds to every input schema: the descriptions say
    it all, and tool definitions are paid for in context in every session."""
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        return
    for t in tools.values():
        schema = getattr(t, "parameters", None)
        if isinstance(schema, dict):
            schema.pop("title", None)
            for prop in schema.get("properties", {}).values():
                if isinstance(prop, dict):
                    prop.pop("title", None)


def serve(root: str | None = None, shards_dir: str | None = None,
          freshness: str = "full", always: bool = False, cache_dir: str | None = None,
          index_external: str = "all", config_file: str | None = None,
          tools: str = "all", build_workers: int | None = None) -> None:
    import os
    import sys

    from .setup import java_project

    active, why = (True, "--always") if always else java_project(root or os.getcwd())
    if not active:
        print(f"scipq-mcp: {why}; no tools exposed (start with --always to override)",
              file=sys.stderr)
    if active:
        from .cachedir import CacheError, prune

        try:  # only entries of checkouts that no longer exist, only in our cache dir
            prune(cache_dir)
        except (OSError, CacheError):
            pass
    build(Service(root, shards_dir, freshness, cache_dir, index_external, config_file,
                  build_workers=build_workers), active, tool_selection(tools)).run("stdio")


__all__ = ["build", "serve", "__version__"]

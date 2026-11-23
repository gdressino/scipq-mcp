# scipq-mcp

An [MCP](https://modelcontextprotocol.io) server for navigating Java code — find symbols,
references, definitions, implementations, supertypes, outlines, callers and callees — that
reads the per-file [SCIP](https://github.com/scip-code/scip) shards `javac` writes at compile
time, **without loading an index into memory**.

Language servers and index-backed tools keep the whole project in the heap: on a 10,000-file
multi-module project that is gigabytes per checkout, multiplied by every worktree you have
open. scipq-mcp keeps nothing between calls. Each call:

1. greps the shards for the exact bytes the answer must contain (in parallel, PCRE when
   available),
2. decodes only the matching protobuf messages of those few shards, with a small stdlib-only
   decoder,
3. answers in compact text: paths relative to the project root, 1-based lines, the line of
   code.

On that 10,000-file project: 0.1–0.3 s per query, ~65 MB resident (≈17 MB for scipq-mcp itself,
the rest is the MCP SDK), one server for any number of checkouts.

## Read-only by design

scipq-mcp **reads** shards, sources and git metadata (`git diff`, `git ls-files`). It never
compiles, never runs tests and never writes to your repository; the only thing it writes is its
own cache. The index is produced by the build you
already run; scipq-mcp consumes it. When the index is not enough (stale shards, files that
never compiled, a checkout that was never built) it says so and tells you what to build,
without running it.

## How the index is produced

The [scip-javac](https://github.com/scip-code/scip-java) compiler plugin writes one shard per
source file while `javac` runs:

```
<root>/build/scip-targetroot/<module>/<task>/META-INF/scip/<source path from the root>.scip
```

Gradle compiles incrementally, so after an edit only the shards of what was recompiled are
rewritten — the index is as fresh as your last build, at no extra cost.

### Gradle

The package ships an init script that adds the plugin to every `JavaCompile` task:

```sh
scipq-mcp gradle-init > ~/.gradle/init.d/scip-shards.init.gradle
# edit PROJECTS in that file to limit it to your projects (empty = every build)
./gradlew compileJava compileTestJava
```

Or for a single build: `./gradlew build --init-script scip-shards.init.gradle`. Turn it off for
one run with `-Pscip=false`. It only affects local builds that load the script (CI does not see
`~/.gradle/init.d`). It works with Lombok and other annotation processors, and with the Gradle
build cache (the shards are declared as task outputs).

### Maven

Add the plugin configuration that `setup` prints (also in
`src/scipq_mcp/templates/maven-compiler-plugin.xml`) to `maven-compiler-plugin`, and keep any
other annotation processors (e.g. Lombok) listed in `annotationProcessorPaths`. Shards land in
`build/scip-targetroot/<artifactId>/<compile|testCompile>/` under the directory that holds
`.mvn/` (create an empty `.mvn/` at the repository root if there is none).

### Other builds

Any build that runs `javac` can produce the same layout: put
`org.scip-code:scip-javac` on the annotation processor path and pass
`-Xplugin:scip -sourceroot:<project root> -targetroot:<root>/build/scip-targetroot/<module>/<task>`.

### First use from an agent

An agent that has the server installed but a project without shards gets an answer that says
so and points to the **`setup`** tool. `setup` inspects the build (Gradle or Maven, the root
project name, the wrapper, init scripts already in `~/.gradle/init.d` and whether they apply)
and returns the exact steps with the init script or pom snippet to add. The server
instructions tell the agent to ask the user before changing the build. In Claude Code, the
`setup` prompt (`/mcp__scipq__setup`) does the same on demand.

## Install

Requirements: Python ≥ 3.11, Linux or macOS. GNU grep and GNU find are used when present
(much faster on large projects); otherwise scipq-mcp falls back to pure Python. On macOS,
`brew install grep findutils` provides them (`ggrep` is picked up automatically).

```sh
uvx scipq-mcp --help            # run without installing
pipx install scipq-mcp          # or install it
```

### Claude Code

```sh
claude mcp add scipq -- uvx scipq-mcp
```

### Other MCP clients

```json
{
  "mcpServers": {
    "scipq": { "command": "uvx", "args": ["scipq-mcp"] }
  }
}
```

The server serves the project it is started in (any directory inside it). Every tool also
takes `project_root`, so one server can answer for several checkouts or git worktrees.

Installed for every project (user scope), it stays out of the way where it does not apply:
when the directory it starts in is not a Java project (no Gradle or Maven build files, no
shards, no `.java` sources), it exposes **no tools** and a one-line instruction, so it costs
no context. A Java project that was never compiled does get the tools (`setup` explains what
to do). Start it with `--always` to expose the tools anyway, e.g. to query other checkouts
through `project_root`.

Server options: `--root DIR` (default project), `--shards-dir` (default
`build/scip-targetroot`), `--freshness full|results|off` (see below), `--always`,
`--cache-dir DIR`, `--index-external all|methods|none`, `--config FILE`,
`--tools all|core|name,name…` (fewer tools, less context; `core` keeps the navigation basics),
`--build-workers N` (index build; default min(4, CPUs/4), always at low CPU/IO priority).

## Tools

| Tool | What it answers | Like |
|---|---|---|
| `setup` | how this project produces shards, whether they are fresh, the steps to enable them | — |
| `find_symbols(name, match)` | definitions by name or part of it (exact, prefix, contains, camel humps; case-insensitive) | workspace/symbol |
| `references(symbol, exclude_imports, by_name)` | every use, grouped by file, with the line of code and the enclosing method (imports left out for types; large results open with a summary); optionally possible uses by name in strings | findReferences |
| `definition(symbol)` | location, kind, signature, javadoc, what it overrides or extends | definition + hover |
| `source(symbol)` | the exact source of a definition, with line numbers | — |
| `implementations(symbol, direct_only)` | subtypes of a type or overrides of a method, at any depth; anonymous classes included | implementation |
| `supertypes(symbol)` | supertypes of a type, or the methods a method overrides, direct first | type hierarchy |
| `members(symbol, inherited)` | own and inherited members of a type, marking overrides and generated ones | — |
| `family(symbol)` | a method's override family (ancestors, descendants, siblings) and the uses of each | — |
| `overloads(symbol)` | every overload of a method and the uses of each | — |
| `outline(file)` | definitions in a file, nested, with signatures; takes a path or a type name | documentSymbol |
| `callers(symbol, depth, include_overridden)` | incoming calls grouped by enclosing method | incomingCalls |
| `callees(symbol, include_fields, include_types)` | methods called from a body | outgoingCalls |
| `annotated(annotation)` | definitions carrying an annotation, with its line (attribute values) | — |
| `typed(symbol, categories)` | where a type is used: fields, parameters, return types, locals, supertypes, bodies, imports | — |
| `tests(symbol)` | test classes that use a symbol directly, with a ready `--tests` / `-Dtest` filter | — |
| `index_status()` | shards per task, stale and uncompiled files, tracked files without a shard, what to recompile | — |
| `batch(queries)` | several of the above in one call | — |

All tools are read-only and take an optional `project_root`. Lists take `offset`/`limit` and
say `next offset N` when there is more.

### Library and JDK symbols

`definition`, `source` and `members` also answer for symbols the project only uses — the JDK and
its dependencies — from the `-sources.jar` files in the Gradle (`~/.gradle/caches`) and Maven
(`~/.m2/repository`) caches and the JDK's `lib/src.zip`: declaration, javadoc, overloads, the
body, the members a project type inherits from library supertypes (nearest first), `members`
of a library type itself, and `typed(subtypes=true)` through library hierarchies. Which version
the build uses is not in the shards: with several cached, the highest is used and the answer
says which. JDK sources follow the build's target version when a matching JDK is installed.
A class → jar map is kept in the user cache (`libraries.sqlite`), built the first time it is
needed (it lists every cached sources jar once, ~10–20 s) and refreshed incrementally. Gradle
downloads sources jars when an IDE asks for them; without one, the answer says so.

### Whole-project tools (auxiliary index)

Questions that follow call chains or look at everything use an on-disk index, built from the
shards the first time one of these tools runs (~15 s and ~170 MB for 10,000 files, in a child
process so the server stays small) and updated incrementally afterwards (unchanged: ~0.1 s).

| Tool | What it answers |
|---|---|
| `impact(symbol, depth, include_overridden, group_by)` | transitive callers by depth, grouped by module or package, tests marked |
| `tests(symbol, transitive=true)` | test classes that reach a symbol through callers, with the command to run them |
| `path(source, target)` | shortest call chains between two symbols, with the lines |
| `changed(base, staged, paths)` | from `git diff` to the definitions whose lines changed, their callers and the tests to run |
| `deps(level, scope, target, sites)` | dependencies between modules or packages, cycles, or every call site from one scope to another |
| `dead_code(scope, public_is_api)` | candidates nobody references, leaving out framework entry points, overrides, accessors in use and names used in strings |
| `census(category)` | annotations in use, or entities and their tables |
| `cache_status()` | where the index of this checkout is and how big |

The index lives in the user cache — `$XDG_CACHE_HOME/scipq` (`~/.cache/scipq`), or
`~/Library/Caches/scipq` on macOS — one directory per checkout (worktrees get their own), private
(`0700`, owner and symlink checked). `--cache-dir DIR` moves it. It is disposable: deleted, it is
rebuilt. `scipq-mcp cache status|prune|clear` manages it; the server prunes the entries of
checkouts that no longer exist when it starts. `--index-external methods|none` keeps fewer
references to JDK and library symbols (smaller, slower to build).

### Profiles and project configuration

Framework knowledge is declarative. Built-in profiles — `junit`, `spring`, `jaxrs`, `vaadin`,
`cucumber`, `jpa`, `jdo` — list entry points (annotations, supertypes, type names) and the
persistence model; with no configuration, the profiles whose annotations appear in the project
are used. A configuration file (`--config FILE`, default `$XDG_CONFIG_HOME/scipq/config.toml`)
picks profiles and project settings per checkout:

```toml
[[project]]
match = { root_project = "shop" }   # or { path = "~/src/shop*" } (worktrees), { git_remote = "…" }
profiles = ["jpa", "spring", "junit"]

[project.tests]
filter = "./run-tests.sh {task} {tests}"   # {module} {task}; {tests} {classes} per module,
                                           # or {fqcn} {simple} for one line per class

[project.by_name]
globs = ["*.java", "*.xml"]

[[project.entry_points]]
type_names = ["*Screen"]
```

When several entries match, `path` beats `root_project`, which beats `git_remote`. Your own
profiles go in `$XDG_CONFIG_HOME/scipq/profiles/*.toml` (same format as
`src/scipq_mcp/profiles/`). `index_status` shows which entry and profiles apply.

### Naming symbols

Pass a name or an exact SCIP symbol:

| Input | Means |
|---|---|
| `Account` | a type (or member) named `Account` |
| `Account#setState`, `Account.setState` | member `setState` of `Account` |
| `com.example.model.Account` | qualified; `/`, `.` and `#` are all accepted as separators |
| `setState()` | methods only, any overload |
| `setState(+1)` | the second overload (SCIP numbers overloads `()`, `(+1)`, `(+2)`…) |
| `Account#<init>` | constructors |
| `com/example/model/Account#setState().` | the exact symbol, as printed by `find_symbols` |
| `PersistenceManager#evict` | JDK and library symbols, found among the references |

A name that matches one symbol is resolved, and the answer starts with
`-- Account#setState → com/example/model/Account#setState().`. An ambiguous name returns the
candidates with their signatures instead.

## Freshness

What scipq-mcp sees is the **last compilation**. Every answer ends with a warning when:

- a source was edited after its shard was written (its results may be off), naming those that
  appear in the answer;
- a `.java` file was created after the last compilation of its module and has no shard yet.

`--freshness full` (default) checks every shard against its source in parallel with the query
(~0.1 s on 10,000 files, mostly hidden behind the query); `results` checks only the files in
the answer; `off` disables it.

## Semantics and limits

- Only compiled code is visible. Shards of deleted sources are ignored.
- Occurrences only distinguish definitions from references: scip-javac does not record
  read/write access.
- Symbols from the JDK or dependencies have references but no definition.
- `implementations` reports every level: scip-javac records the whole chain of supertypes on
  each type and method. A result is *direct* unless it also implements another result.
- Callers: uses inside lambdas and anonymous classes count for the enclosing method; field
  initializers for the field; initializer blocks (static or instance) for the class.
- Lombok: generated members are located at the node that generates them (the field, or a
  class-level `@Getter`/`@Setter`) and marked *generated*; the references inside generated
  bodies are left out.
- `references(by_name=true)` lists, separately, lines where the name (or its bean property
  name) appears inside string literals or other files: map keys, query strings, templates. The
  index cannot see those; check them by hand.

## Command line

Besides `serve` (the default) and `gradle-init`, `scipq-mcp query` runs any tool from the shell,
for debugging or scripts:

```sh
scipq-mcp query --root ~/src/project refs 'Account#setState'
scipq-mcp query impl Entity --direct
scipq-mcp query callers 'Entity#setState()' --depth 2
```

## Development

```sh
uv venv && uv pip install -e . pytest
.venv/bin/python -m pytest
```

`tests/fixtures/sample` is a small two-module Java project; its shards are checked in, so the
tests need no JDK. To regenerate them (JDK 17+): `tests/fixtures/build-sample.sh`.

Design notes and the roadmap: [docs/design.md](docs/design.md).

### Releasing

Releases are published by GitHub Actions with
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/): no API token exists in
the repository, its secrets, or anyone's machine.

1. Describe the changes under `## Unreleased` in `CHANGELOG.md` and commit.
2. `scripts/release.sh [VERSION]` — sets the version, dates the changelog, runs the tests,
   commits and tags `vVERSION`.
3. `git push origin main vVERSION` — the `release` workflow tests, builds and publishes.

## License

MIT

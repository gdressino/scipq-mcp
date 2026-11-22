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

scipq-mcp **reads** shards, sources and (in later versions) git metadata. It never compiles,
never runs tests and never writes to your repository. The index is produced by the build you
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

### Other builds

Any build that runs `javac` can produce the same layout: put
`org.scip-code:scip-javac` on the annotation processor path and pass
`-Xplugin:scip -sourceroot:<project root> -targetroot:<root>/build/scip-targetroot/<module>/<task>`.
A Maven template is planned.

## Install

Requirements: Python ≥ 3.10, Linux or macOS. GNU grep and GNU find are used when present
(faster); otherwise scipq-mcp falls back to pure Python.

```sh
uvx scipq-mcp --help            # run without installing
pipx install scipq-mcp          # or install it
```

> Not on PyPI yet. Until then: `uvx --from git+https://github.com/gdressino/scipq-mcp scipq-mcp`.

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

Server options: `--root DIR` (default project), `--shards-dir` (default
`build/scip-targetroot`), `--freshness full|results|off` (see below).

## Tools

| Tool | What it answers | Like |
|---|---|---|
| `find_symbols(name)` | definitions whose trailing names match | workspace/symbol |
| `references(symbol, offset, limit, include_definitions)` | every use, grouped by file, with the line of code | findReferences |
| `definition(symbol)` | location, kind, signature, javadoc, what it overrides or extends | definition + hover |
| `implementations(symbol, direct_only)` | subtypes of a type or overrides of a method, at any depth; anonymous classes included | implementation |
| `supertypes(symbol)` | supertypes of a type, or the methods a method overrides, direct first | type hierarchy |
| `outline(file)` | definitions in a file, nested, with signatures; takes a path or a type name | documentSymbol |
| `callers(symbol, depth, include_overridden)` | incoming calls grouped by enclosing method | incomingCalls |
| `callees(symbol, include_fields, include_types)` | methods called from a body | outgoingCalls |

All tools are read-only and take an optional `project_root`.

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
- Lombok: generated members are reported at their field and marked *generated*; the
  references inside generated bodies are left out.

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

## License

MIT

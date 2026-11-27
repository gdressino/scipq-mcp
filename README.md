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
own cache. Even refactoring only produces a patch, stored in that cache, for you (or the agent,
with your permission) to apply with `git apply`. The index is produced by the build you
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

For every project you open (user scope): the server stays silent where there is no Java
project (see below), so this is the usual choice.

```sh
claude mcp add --scope user scipq -- uvx scipq-mcp
```

For one project, shared with the team (project scope): this writes `.mcp.json` at the project
root, to commit; each member approves it the first time Claude Code starts there.

```sh
claude mcp add --scope project scipq -- uvx scipq-mcp
```

Without `--scope`, the server is added for this project and only for you (local scope).
`claude mcp list` lists the servers and checks them; `claude mcp get scipq` shows the scope
and command of this one.

Which version runs depends on what follows `uvx` (in any scope):

- `uvx scipq-mcp`: the version uv downloaded the first time, kept in its cache; it is not
  updated by itself.
- `uvx scipq-mcp@latest`: always the newest on PyPI. Each session that starts checks PyPI
  (about a second) and runs the newest; sessions already open keep theirs until they are
  restarted (or the server is reconnected with `/mcp`).
- `uvx scipq-mcp@X.Y.Z`: that version, until you change it (see the note on worktrees below).

When the same name is added in several scopes, local wins, then project, then user. To change
what a server runs, remove it from its scope and add it again:

```sh
claude mcp remove --scope user scipq
claude mcp add --scope user scipq -- uvx scipq-mcp@latest
```

`doctor` says which version is running, on its first line.

### Other MCP clients

The same server definition goes in the client's user-level configuration (every project) or in
its project-level one, where the client has it (e.g. Cursor: `~/.cursor/mcp.json` and
`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "scipq": { "command": "uvx", "args": ["scipq-mcp"] }
  }
}
```

Codex reads TOML, in `~/.codex/config.toml`:

```toml
[mcp_servers.scipq]
command = "uvx"
args = ["scipq-mcp"]
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

Each agent session starts its own server process (~70 MB at rest). The whole process runs at
low CPU and I/O priority (nice 10, best-effort I/O 7 on Linux), so it yields to builds and
tests. With `uvx`, every running server holds uv's cache: `uv cache clean` waits until those
sessions are closed. With several worktrees, consider pinning the version
(`uvx scipq-mcp@X.Y.Z`) instead of `@latest`: a new version with another index schema rebuilds
each checkout's index on its first whole-project query, and pinned sessions upgrade when you
choose, not all at once mid-day (with `@latest`, sessions started before and after a release
run different versions).

## Tools

| Tool | What it answers | Like |
|---|---|---|
| `setup` | how this project produces shards, whether they are fresh, the steps to enable them | — |
| `context(symbol, token_budget)` | start here before changing a symbol: definition and body, overrides, callers with the call lines, callees, tests (a type: members, subtypes, users; a field: accessors, writers, readers), trimmed to a budget; each cut section names the tool for the rest | — |
| `find_symbols(name, match, scope)` | definitions by name or part of it (exact, prefix, contains, camel humps; case-insensitive); wildcards in any name (`Cache*#getPlace`); `Type#`: the members it declares; in a package or path | workspace/symbol |
| `references(symbol, exclude_imports, by_name)` | every use, grouped by file, with the line of code and the enclosing method (imports left out for types; large results open with a summary); optionally possible uses by name in strings | findReferences |
| `definition(symbol)` | location, kind, signature, javadoc, what it overrides or extends | definition + hover |
| `source(symbol, git_ref)` | the exact source of a definition, with line numbers; with `git_ref` (`origin/main`, a tag, a commit), as it is there, read with git and without the index, so also code never compiled here | — |
| `implementations(symbol, direct_only)` | subtypes of a type or overrides of a method, at any depth; anonymous classes included | implementation |
| `supertypes(symbol)` | supertypes of a type, or the methods a method overrides, direct first | type hierarchy |
| `members(symbol, inherited)` | own and inherited members of a type, marking overrides and generated ones | — |
| `family(symbol)` | a method's override family (ancestors, descendants, siblings) and the uses of each; for a library method, also the overrides in library subtypes the project calls | — |
| `overloads(symbol)` | every overload of a method (also library ones) and the uses of each | — |
| `outline(file)` | definitions in a file, nested, with signatures; takes a path or a type name | documentSymbol |
| `callers(symbol, depth, include_overridden)` | incoming calls grouped by enclosing method; with `include_overridden`, also the calls through the methods each one overrides (an interface's), which may dispatch to it, else it says how many it left out | incomingCalls |
| `callees(symbol, include_fields, include_types)` | methods called from a body | outgoingCalls |
| `annotated(annotation)` | definitions carrying an annotation, with its line (attribute values) | — |
| `typed(symbol, categories)` | where a type is used: fields, parameters, return types, locals, supertypes, bodies, imports | — |
| `tests(symbol)` | test classes that use a symbol directly, with a ready `--tests` / `-Dtest` filter | — |
| `index_status()` | shards per task, stale and uncompiled files and why they are (a git checkout after the build, a task FROM-CACHE), tracked files without a shard, what to recompile; how far the auxiliary index is behind the shards | — |
| `compare(other_root, scope, api_only)` | this checkout against another (e.g. two worktrees): definitions only in one side or with another signature, maybe-renamed pairs; with shared git history, members changed on both sides since the merge base and what merging one into the other would break (the other side's uses of what one removed or changed) | — |
| `steps(query, check)` | Cucumber-JVM: a step definition's feature steps and the scenarios to run; a .feature's steps and the method each runs; a step's text and its method; `check=True`: undefined, ambiguous, not understood and unused step definitions | — |
| `concepts(query, kind, scope)` | definitions about something when you do not know their names: words of names, owners, annotation texts, call labels, signatures and javadoc, ranked | workspace/symbol by meaning (words, not embeddings) |
| `persistence(symbol)` | the persistence model (jdo, jpa…): entities, tables, inheritance, columns, relations, fetch groups; for a field, who writes and reads it, also by name | — |
| `check_refs(files, since, exclude)` | the code docs mention (`Type#m`, `Type.m(…)`, `File.java:N`…), checked: missing types and members, wrong parameter lists, moved lines; by default in what agents read (`CLAUDE.md`, `AGENTS.md`, `.claude/**`, `.github/**`, `README*.md`), or the project's `[check_refs] files`; `since=REF`: only what changed since then or mentions changed code | — |
| `doctor()` | what can make the index wrong here, with what to do: plugin setup, files without shards, IDE builds, failed javac, stale files, running builds, build cache, cache, config, JDKs | — |
| `refactor(plan, dry_run)` / `refactor(plan_id, verify)` | runs a refactoring plan (see below) into a verified patch; after it is applied and compiled, checks the new index and lists what to test | rename / change signature / move |
| `refactor_help(topic)` | the plan format, with examples | — |
| `batch(queries)` | several of the above in one call (any tool but `setup`) | — |

All tools are read-only and take an optional `project_root`. Lists take `offset`/`limit` and
say `next offset N` when there is more.

### Refactoring

`refactor(plan)` turns a plan the agent writes into a patch. A plan is JSON data, never code:
macros (`rename`, `change_signature`, `replace_calls`, `move`, `annotate`, `create`) or
primitives that select nodes (the index's sites of a symbol, or every node of a kind inside a
declaration, optionally matching a structural pattern like `$x.size() == 0`) and act on them
(rename, replace with a template, wrap, remove, insert, add/remove/reorder arguments and
parameters, annotations, modifiers, members, imports). `refactor_help` documents the format.

- **Where**: from the index, so a rename touches that method's override family and its Lombok
  accessors, and nothing else with the same name; uses by name the index cannot see (strings,
  XML, `.feature` files, column names) are listed for review.
- **How**: every change is an edit of the original text (comments and formatting outside the
  edits are kept byte for byte); [JavaParser](https://javaparser.org) checks that each edited
  file parses at the project's Java version with a node of the expected kind over each edit.
  It runs in a short-lived JVM, at the lowest priority, that reads the sources from scipq-mcp
  and never touches the checkout.
- **Refused**: stale files or a running build; a method overriding a library method; a name
  that already exists in the hierarchy; a local or an enclosing (anonymous) class that would
  capture a renamed member; a persistent field whose column, or a class whose table or
  class-name discriminator, follows its name (JDO/JPA), unless the plan keeps the column
  (`keep_column`) or declares a migration.
- **Then**: the patch (with full blob ids, for `git apply -3`) goes to the cache. Apply it with
  `git apply`, compile, and run `refactor(plan_id=…, verify=true)`: every renamed identifier
  must resolve to the renamed symbol, and it lists what the change reaches and the tests to
  run.

JavaParser is not bundled. scipq-mcp uses the newest `javaparser-core` jar it finds in its
cache, `~/.m2/repository` or Gradle's cache (3.28+ for Java 25); `scipq-mcp tools install
javaparser` downloads the pinned version from Maven Central and checks its SHA-256, and
`mvn dependency:get -Dartifact=com.github.javaparser:javaparser-core:3.28.2` works too. A JDK
11+ (`javac`) is needed to compile the small worker once.

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
process so the server stays small) and updated incrementally afterwards (unchanged: ~0.1 s;
1,000 recompiled files: a few seconds). When a recompilation rewrote a fifth of the shards or
more (a merge, a branch switch), it is rebuilt instead, which is faster. `index_status` says
how far behind it is and about how long catching up will take.

| Tool | What it answers |
|---|---|
| `impact(symbol, depth, include_overridden, group_by)` | transitive callers by depth, grouped by module or package, tests marked |
| `tests(symbol, transitive=true)` | test classes that reach a symbol through callers, with the command to run them |
| `path(source, target, avoid)` | shortest call chains between two symbols, with the lines, how many there are and what they all go through; `avoid` methods or types to look for others; an override is followed only where the call may run it |
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
`servlet`, `cucumber`, `jpa`, `jdo` — list entry points (annotations, supertypes, type names,
classes named in XML descriptors such as `web.xml`) and the persistence model. `impact` and
`changed` list the entry points a change reaches, by kind (screens, endpoints, jobs…); with no configuration, the profiles whose annotations appear in the project
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

[project.dead_code]
exclude = ["ws-client/src/*"]              # generated code: never dead-code candidates

[[project.entry_points]]
kind = "screen"                            # shown by impact and changed
type_names = ["*Screen"]                   # with supertypes too, a type must match both
supertypes = ["com.vaadin.ui.Component"]

[[project.entry_points]]
kind = "job"                               # started by reflection: kept by dead_code too
supertypes = ["com.example.jobs.Job"]

[[project.entry_points.xml]]               # classes named in XML files
files = ["**/WEB-INF/web.xml"]
elements = ["servlet-class"]
```

When several entries match, `path` beats `root_project`, which beats `git_remote`. Your own
profiles go in `$XDG_CONFIG_HOME/scipq/profiles/*.toml` (same format as
`src/scipq_mcp/profiles/`). `index_status` and `setup` show which files, entry and profiles
apply, and where the tests filter came from.

### Team setup

A repository can carry its settings in **`.scipq.toml`** at the project root: the same keys as a
`[[project]]` entry, without `match` (the file applies to its own checkout), read from the
working tree, so each branch and worktree uses its own. `scipq-mcp init-config` prints one to
start from, with the profiles detected from the checkout's shards; it writes nothing
(`scipq-mcp init-config > .scipq.toml`).

```toml
profiles = ["spring", "jpa", "junit"]

[tests]
filter = "./gradlew {module}:test --tests '{fqcn}'"

[by_name]
globs = ["*.xml", "*.jrxml"]
exclude = ["ws-client/src/soap/*"]   # generated: names there would hide dead code elsewhere

[dead_code]
exclude = ["ws-client/src/soap/*"]   # and its own unused members are not candidates

[check_refs]   # the docs check_refs reads when the call names none
files = ["**/CLAUDE.md", ".claude/**/*.md", "docs/guides"]   # replaces the default
other_versions = ["v4"]              # a doc's "v4 `File.java:N`" is another branch's line
```

`check_refs` checks by default what agents read to work: `CLAUDE.md` and `AGENTS.md` in any
folder, `.claude/**/*.md`, `.github/**/*.md` and `README*.md`. Dated records (audits, the write-up of a fix) describe the code as
it was and need not follow it, so they are left out unless `[check_refs] files` or the call's
`files` names them; the output's `scope:` line says what was read and how many other Markdown
files were not. Globs are relative to the checkout: `*` stays within a folder, `**` crosses
folders, and a folder means the Markdown under it; archive folders (`archive/`, `old/`…) are
read only when a glob names them.

`init-config` proposes a `[check_refs]` scope (the folders with Markdown outside the
default, those named by date or ticket marked as likely records) and both excludes for
directories that look generated: named like generated
output (`*-out`, `generated`, `dist`…, without Java sources), or where nearly all files start
with a "generated / do not edit" header; in a module that mixes its own code with generated
packages, only those packages. Check the list before committing it.

Precedence, key by key, lowest first: built-in profiles, your profiles, the repository's
`.scipq.toml` (the team's defaults), the matching `[[project]]` entry of your configuration
(your overrides).

The repository does not run anything by itself. `.scipq.toml` is treated as untrusted input:
it is ignored when larger than 256 KiB or a link out of the checkout, lists and strings are
capped, globs must be relative and stay inside the checkout, keys such as the cache directory
cannot be set from it, and `tests.filter` is only text the tools print for you to run.

Cucumber: `tests(transitive=true)` and `changed` list the scenarios affected (through the
step definitions reached, or changed `.feature` lines) and a command to run them, from
`[cucumber]`: `run` with `{names}` (an anchored regex of the scenario names, `\Q…\E` each) or
`{features}` (`path:line,…`), and `run_features` with `{tags}` (one tag of its own per feature),
used when whole features are affected, and `run_all` (the whole suite) when there are too many
scenarios to name. Scenarios carrying one of `exclude_tags` (default `@pending`, `@ignore`,
`@wip`, `@skip`) are reported: the build leaves them out. `include_steps` lists steps that load
another feature's Background (the quoted argument is its path, resolved against the resource
roots or by suffix): the included steps count where the including step runs, transitively, and a
changed line of the included file selects those scenarios.

```toml
[cucumber]
run = "./gradlew :app:cucumber -Pname='{names}'"
run_features = "./gradlew :app:cucumber -Ptags='{tags}'"
run_all = "./gradlew :app:cucumber"
include_steps = ["the background from {string}"]
```

A name filter matches outline rows by their name with the Examples values filled in, while
JUnit and Gradle reports list them under the template name (with `<placeholders>`): that is
expected. Scenarios elsewhere with the same name also run; the output says how many.

Everyone on the team also needs the shards: the init script from `scipq-mcp gradle-init` in
`~/.gradle/init.d/` (or the Maven snippet from `setup`), then a compilation.

### Naming symbols

Pass a name or an exact SCIP symbol:

| Input | Means |
|---|---|
| `Account` | a type (or member) named `Account` |
| `Account#setState`, `Account.setState` | member `setState` of `Account` |
| `com.example.model.Account` | qualified; `/`, `.` and `#` are all accepted as separators |
| `setState()` | methods only: the overload without parameters when there are several (if none is without parameters, any overload) |
| `setState(State)`, `pick(String, int)` | the overload taking those parameter types (simple or qualified names, a qualified one also by its package; generics and parameter names are ignored; a type variable matches any type) |
| `setState(+1)` | the second overload in SCIP's numbering: `()`, `(+1)`, `(+2)`… in declaration order, whatever their parameters |
| `Account#<init>` | constructors |
| `com/example/model/Account#setState().` | the exact symbol, as printed by `find_symbols` |
| `PersistenceManager#evict` | JDK and library symbols, found among the references |

A name that matches one symbol is resolved, and the answer starts with
`-- Account#setState → com/example/model/Account#setState().`. An ambiguous name returns the
candidates with their signatures instead. Answers write a method that has overloads with its
parameter list (`Service#process(String)`), which reads back as the same overload; the `--` line
gives the exact symbol. `Type#m` on a type that only inherits `m` resolves to the
declaration it inherits, as Java does (the nearest supertype declaring it, then library
types and `Object`), and the `--` line says so; `Type#m()` on a type that declares only
overloads with parameters resolves to an inherited one without. Library overloads are told
apart by their sources jar.

## Freshness

What scipq-mcp sees is the **last compilation**. Every answer ends with a warning when:

- a source was edited after its shard was written (its results may be off), naming those that
  appear in the answer;
- a `.java` file was created after the last compilation of its module and has no shard yet.

When it can tell from file dates, the warning (and `index_status`, in full) says why: a git
checkout, reset or merge after the last build, read from HEAD's reflog (the index then shows
the commit compiled before it), and whether javac compiled the files since: class files
newer than the change mean the task ran without writing the shards (FROM-CACHE without them,
an IDE build, `-Pscip=false`); older ones, that nothing compiled them. A method whose
parameters changed since is shown where it is now, with its declaration as it is now, by
`definition`, `context` and `source`.

A file written again with the same text (a `git checkout` and back, re-applying a patch, an
editor saving it unchanged) is newer than its shard, but Gradle, which compares contents, does
not recompile it. The auxiliary index keeps the SHA-1 of each source as it was compiled, so
such a file is not reported stale (`index_status` counts them apart); without the auxiliary
index, the dates decide.

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
scipq-mcp query refactor --plan @plan.json --dry-run
scipq-mcp tools status                  # which JavaParser and JDK refactoring would use
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

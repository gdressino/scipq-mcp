# Changelog

One section per version published on PyPI. `scripts/release.sh` turns `## Unreleased` into the
version and date when releasing.

## Unreleased

### Added

- `context(symbol, token_budget=4000)`: what an agent needs before changing a symbol, in one
  answer trimmed to a budget (≈4 characters per token) and never padded. A method: definition
  and body (long ones keep their start and end), what overrides it, callers with the call
  line (tests last), callees with their signatures, the tests that use it with the command to
  run them, a count of overloads, possible uses by name for accessors, and an impact summary
  when the auxiliary index already exists (context never builds it). A type: declaration,
  members, subtypes, the files using it, tests. A field: accessors (Lombok included), writers
  and readers (told apart by the line's text), possible uses by name, tests. Library and JDK
  symbols: declaration and javadoc from the sources jar, and their uses in the project.
  Freshness warnings come first. Part of `--tools core`.
- `.scipq.toml` at the project root: the team's settings travel with the repository (the keys
  of a `[[project]]` entry, without `match`), read from the working tree. Precedence, lowest
  first: built-in profiles, user profiles, `.scipq.toml`, the user's matching `[[project]]`.
  Treated as untrusted input: size and list caps, relative globs inside the checkout only,
  links out of it ignored, nothing executed. `setup` and `index_status` show which files apply
  and where the tests filter came from.
- `scipq-mcp init-config`: prints a `.scipq.toml` template with the profiles detected from the
  checkout's shards, and a `[by_name] exclude` for directories that look generated (by name,
  e.g. `*-out`, `generated`, `dist`, or by a "generated / do not edit" header), whose names
  would hide dead code; writes nothing. README: "Team setup".

### Changed

- Library sources: jars stay open and parsed files are kept in memory for the life of the
  server (opening the JDK's `src.zip` alone takes ~50 ms), and classes missing from every jar
  are remembered; masking comments and literals in `.java` files is twice as fast. A cold
  `typed(subtypes=true)` reads hundreds of library files.
- `typed(subtypes=true)`: one section per kind of use, with the uses of every subtype merged
  and tagged with it (`[HashMap]`).
- `overloads` works for library and JDK methods (from the sources jar), with the uses of each
  overload in the project.
- `index_status` shows the profiles chosen from the project's annotations when the auxiliary
  index exists (it said "none" before they were chosen).
- README: one server process per agent session, `uv cache clean` waits for them, and pin the
  version when several worktrees share a machine.
- The auxiliary index is built from scratch into a temporary file, without journal or fsync
  (it is a disposable cache), switched to WAL there and moved into place when complete: half
  the writes, and no fsync of the whole new file by the first query. Three checkouts building
  at once on a 10,000-file project: ~26 s each, build and first answer, instead of ~110 s.
- dead_code and by-name searches skip documentation (`docs/`, `doc/`, `*.md`, `*.adoc`),
  hidden directories (`.github/`, `.idea/`, tools' settings…) and lockfiles
  (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `*.lock`): they mention names without
  using them.
- dead_code says how its list splits (`N candidates + M low confidence = T`); `impact` and
  `callers` say which line they show (where each caller is defined, or the call).
- Shards written in the last minute: on Linux, the answer tells a build still running in this
  checkout (a `gradle`/`mvn` command whose directory is inside it: a warning) from one that
  just finished (a note). Builds started from an IDE are not seen; elsewhere the conditional
  warning stays.

### Fixed

- Library sources: a class missing from every jar is looked for again as soon as a jar that may
  hold it appears (a download), and a cache removed while the server runs is recreated.
- `overloads` of a library method while another session builds the library map answers with
  an error line instead of failing; `context` on a library symbol then still lists its uses.
- `context` and `index_status` never open an auxiliary index of another schema or mode (that
  would discard it); `context` says it needs one instead.
- `context` on a field: the accessors of a `boolean isX` field (`isX()`, `setX()`) and of a
  record component (its implicit `x()`, which has no definition in the shards, and its uses);
  callers counted once each, and marked when truncated; no per-module tally in a single
  module.
- Build detection: Maven Wrapper, `-p`/`--project-dir`/`-f`/`--file`, and builds run from a
  parent directory count as a build running in the checkout.
- `changed` when the build is a subdirectory of its git repository (a monorepo): diff paths are
  relative to the project root (`git diff --relative`); every edited file was "not indexed".
- Test commands (`changed`, `tests(transitive=true)`, configured filters) for the root project
  (`:test`, not `:_root:test`), Maven (`mvn -pl … -Dtest=…`) and other test source sets
  (`:core:integrationTest`), as `tests` already did; one implementation for all of them.
- A shard removed while a query runs (a recompilation) no longer fails it with "grep failed":
  the other results stand; other grep errors (PCRE limits) fall back to Python for that chunk.
- `callees`, `outline`, `members`, `overloads` use the newest shard of a source with shards in
  two tasks, as every other query does.
- `--index-external methods|none`: references to symbols defined in a shard that could not be
  read (being written) or that vanished are no longer pruned for good; pruning waits for a
  clean update.
- `batch`: a query that fails unexpectedly (e.g. `depth: "2"`) reports its own error instead
  of failing the whole batch; library-map waits and cache database errors are error lines.
- A name resolution that fails in its thread reports its error instead of an IndexError.
- dead_code: two sessions refreshing the text index at once no longer collide.
- Index schema 4: overrides and annotations are kept per shard, so removing one of a source's
  two shards keeps the other's rows (dead_code could report overrides as dead). Existing
  indexes are rebuilt once.

## 0.2.1 — 2026-11-22

### Added

- `members` of library types themselves (e.g. `VerticalLayout`), and library supertypes listed
  nearest first without the members a nearer type overrides; the library type hierarchy comes
  from the sources jars (imports, package and `java.lang` resolve the names).
- `typed(subtypes=true)`: uses of a type's subtypes too — project ones and library ones such as
  `ConcurrentHashMap` for `Map`.
- JDK sources follow the build's target: `JavaLanguageVersion.of(N)`, `sourceCompatibility`,
  `release`, `org.gradle.java.home`, `maven.compiler.release`, `java.version` pick a matching
  JDK (sdkman, `~/.gradle/jdks`, `/usr/lib/jvm`, macOS locations); otherwise the answer notes
  the mismatch.

### Changed

- The server and `query` yield to builds and tests: the whole process (decoding and library
  indexing included) and everything it starts (index build, grep, find) run at nice 10 and, on
  Linux, best-effort I/O priority 7; the index build uses min(4, CPUs/4) workers by default
  (`--build-workers`).
- The shared library map is built under a lock: other sessions wait for it (up to 45 s) and
  then say it is being built, instead of answering without sources.
- Shards cut short by a compilation in progress are skipped with a warning (and retried by the
  index), instead of failing or vanishing silently. A shard cut short that a query never reads
  (its relationships were lost, so the prefilter skips it) cannot be detected, so answers also
  warn when shards were written in the last 60 s.
- dead_code: candidates whose only trace is their bean property name (`getDescription` and
  "description") are listed apart as low confidence instead of being dropped; `offset` and
  `limit` page through them after the other candidates.
- dead_code: names in other files come from a text index (FTS5) in the auxiliary database,
  updated by file mtime: that check takes ~0.4 s instead of ~7 s. A whole-project run takes
  ~0.6 s on a 1,400-file project and ~4 s on a 10,000-file one (~7 s the first time).
- `typed`: a field's type is the declared one: an initializer such as
  `ConcurrentHashMap.newKeySet()` counts as an initializer, and `Map` in `Map.Entry` is a new
  `qualifier` category.
- Library members and definitions are reported at the line of their name, as project symbols
  are, not at their first annotation.

### Fixed

- JDK sources: when the shared library map already knew another JDK's `src.zip` (another
  checkout's toolchain), this project's JDK was not indexed and its symbols had no sources.
- `members`: an inherited library method is hidden only by one with the same parameter types
  (`setSpacing(boolean)` no longer hides `setSpacing(String)`), and a project override hides
  only the overload it overrides.

## 0.2.0 — 2026-11-22

### Added

- Auxiliary index (SQLite, in the user cache, one per checkout, private, disposable, updated
  incrementally; big builds in a child process with parallel workers) and the tools that use
  it: `impact`, `path`, `changed`, `deps`, `dead_code`, `census`, `cache_status`, and
  `tests(transitive=true)`.
- `scipq-mcp cache status|prune|clear`; `--cache-dir`, `--index-external`.
- Profiles (`junit`, `spring`, `jaxrs`, `vaadin`, `cucumber`, `jpa`, `jdo`) and a TOML project
  configuration (`--config`, `$XDG_CONFIG_HOME/scipq/config.toml`): entry points (annotations,
  supertypes, type names, packages) for dead code, entity tables, globs for uses by name, the
  command that runs tests (`{module}`, `{task}`, `{tests}`, `{fqcn}`…).
- `scope` on references, callers, typed and annotated (package, module, directory or path
  glob); `modifiers` on typed (e.g. static fields); javadoc `{@link}`/`@see` mentions in
  `references(by_name=true)`.
- `--tools all|core|names` to expose fewer tools.
- Library and JDK sources: `definition`, `source` and `members` read external symbols from the
  `-sources.jar` files in the Gradle/Maven caches and the JDK's `src.zip` (declaration, javadoc,
  overloads, bodies, inherited members), with the version used.
- dead_code says how many candidates names in other files suppressed, and where; it searches
  tracked or non-ignored files under 2 MB (`[project.by_name] exclude` for more).

### Fixed

- Types that scip-javac reports at a string holding their name (e.g. `@Label("Invoice")`
  above `class Invoice`) are located at their declaration; their annotations are found.
- `callers` counts distinct callers (and tree nodes separately).
- changed: annotations or javadoc above a class are reported without expanding the change to
  every user of the class.

## 0.1.0 — 2026-11-22

First release.

### Added

- MCP server (stdio) for Java code navigation over the per-file SCIP shards that the
  scip-javac compiler plugin writes, with no index held in memory: each call greps the shards
  and decodes only the matching protobuf messages.
- Tools: `setup`, `find_symbols`, `references`, `definition`, `source`, `implementations`,
  `supertypes`, `members`, `family`, `overloads`, `outline`, `callers`, `callees`,
  `annotated`, `typed`, `tests`, `index_status` and `batch`. All read-only, all with an optional
  `project_root` (one server for several checkouts or worktrees), lists paginated.
- Names or exact SCIP symbols; JDK and library symbols resolve through their references.
  `find_symbols` also finds by part of a name (prefix, substring, camel humps, any case), and a
  name that matches nothing suggests the names that contain it.
- Freshness warnings on every answer (sources edited after their shard, files not compiled
  yet) naming the task to recompile.
- `setup` tool and prompt: diagnoses a Gradle or Maven build and returns the steps to produce
  the shards. Gradle init script template (`scipq-mcp gradle-init`) and Maven
  compiler-plugin snippet.
- Idle outside Java projects: no tools are exposed there unless started with `--always`.
- `scipq-mcp query …` runs any tool from the command line.

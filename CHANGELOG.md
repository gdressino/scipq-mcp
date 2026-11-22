# Changelog

One section per version published on PyPI. `scripts/release.sh` turns `## Unreleased` into the
version and date when releasing.

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

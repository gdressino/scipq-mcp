# Changelog

One section per version published on PyPI. `scripts/release.sh` turns `## Unreleased` into the
version and date when releasing.

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

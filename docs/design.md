# scipq-mcp design

Status: v1 and v2 implemented; v3 proposed.

## Principles

1. **Read-only.** scipq-mcp reads shards, sources and git metadata. The only processes it
   starts are `grep`, `find` and read-only `git` (`ls-files`, `diff`, `rev-parse`, `show`). It
   never compiles, runs tests, or writes to the repository. The only thing it writes is its own
   cache (v2). When the index is not enough it says so and names what to build.
2. **No resident index.** v1 keeps nothing between calls. v2 adds an *on-disk*, disposable
   auxiliary database for queries that must look at everything; it is never loaded into memory.
3. **Generic core, declarative profiles.** The package knows nothing about any particular
   project. Framework knowledge (JPA, Spring, Vaadin, JUnit, Cucumber…) lives in declarative
   profiles; project specifics in a user config file.

## v1 (implemented)

Every call lists the shards (`os.scandir`), prefilters them with `grep` using byte patterns that
only occur where the answer is (e.g. a symbol followed by the protobuf tag that only follows
`SymbolInformation.symbol`), decodes only the matching messages, and renders compact text.
A freshness check (`find -printf %T@` in child processes, or Python) runs in parallel.

## v2: auxiliary database

Implemented as below, with these measurements on a 10,000-file, 7-module project (4.1 M
occurrences): first build ~16 s in a child process (8 workers; the server process stays at
~25 MB), 172 MB on disk with `external = all` (131 MB with `methods`, but 3x slower to build),
1.58 M reference rows (occurrences aggregated per symbol, caller and shard), unchanged update
~0.1 s; impact at depth 3 ~10 ms of SQL.

Needed by queries that are global or transitive: `impact` with depth 3 and fan-out 20 would be
~400 grep passes (~40 s), and dead code or dependency graphs must read every shard (~10 s).

### Location

- Default: the user cache directory — `$XDG_CACHE_HOME/scipq` or `~/.cache/scipq` on Linux,
  `~/Library/Caches/scipq` on macOS (resolved without extra dependencies). Never
  `$XDG_RUNTIME_DIR` (tmpfs, i.e. RAM).
- Override: `scipq-mcp --cache-dir DIR` (server and `cache` subcommands). No environment
  variable.
- One directory per checkout: `<cache>/<basename(root)>-<sha256(realpath(root))[:12]>/` holding
  `index.sqlite`, `update.lock`. Worktrees have different real paths, so they never share one.
- Safety (matters most when `--cache-dir` points at a shared place such as `/tmp`): the cache
  directory and each entry are created `0700`; on open, `lstat` must show a directory (not a
  symlink) owned by the current uid, or scipq-mcp refuses; `meta.root_realpath` must equal the
  checkout's real path.

### Lifecycle

- Disposable: missing → rebuilt from the shards; deleted while in use → the next query rebuilds.
- Incremental, at query time, only for queries that need it (v1 queries never touch it):
  `stat` every shard (~0.1 s on 10k files), reprocess new or changed ones (by `mtime_ns`,
  `size`), delete rows of removed ones, mark orphans (source deleted).
- Concurrency: several server processes (sessions) may share a checkout. SQLite in WAL mode
  with `busy_timeout`; updates run under an exclusive `flock` on `update.lock`, so only one
  process reprocesses while the others wait and then read.
- `cache status` lists every entry (root, size, last use, whether the root still exists);
  `cache prune [--older-than DAYS]` (default 30) removes entries whose root is gone or unused for
  that long; `cache clear` removes the entry of one checkout (or all with `--all`). At startup
  the server prunes only entries whose root no longer exists, only inside its own cache dir.
  A recycled worktree path is brought up to date by the mtime check and the stored real path.

### Schema (version 1)

```sql
CREATE TABLE meta    (key TEXT PRIMARY KEY, value TEXT);
  -- schema_version, root_realpath, shards_dir, created_at, last_used_at
CREATE TABLE shards  (id INTEGER PRIMARY KEY, path TEXT UNIQUE, source TEXT, module TEXT,
                      mtime_ns INTEGER, size INTEGER, orphan INTEGER);
CREATE TABLE symbols (id INTEGER PRIMARY KEY, symbol TEXT UNIQUE,
                      name TEXT, parent_id INTEGER, kind TEXT);    -- interned, parsed once
CREATE TABLE defs    (symbol_id INTEGER, shard_id INTEGER, line INTEGER, col INTEGER,
                      body_start INTEGER, body_end INTEGER,       -- enclosing range, lines
                      kind INTEGER, flags INTEGER,                -- generated, local, test
                      display_name TEXT, signature TEXT);
CREATE TABLE refs    (symbol_id INTEGER, shard_id INTEGER, line INTEGER, col INTEGER,
                      caller_id INTEGER,                          -- innermost non-local definition
                      flags INTEGER);                             -- in anonymous class, import line
CREATE TABLE rels    (symbol_id INTEGER, target_id INTEGER, kind INTEGER);  -- implementation…
CREATE INDEX refs_symbol ON refs(symbol_id);
CREATE INDEX refs_caller ON refs(caller_id);
CREATE INDEX defs_symbol ON defs(symbol_id);
CREATE INDEX rels_target ON rels(target_id);
CREATE INDEX shards_source ON shards(source);
-- v3: CREATE VIRTUAL TABLE concepts USING fts5(symbol_id UNINDEXED, words, signature, doc, attrs);
```

Callers are attributed while indexing (same rules as v1), so transitive queries are recursive
CTEs over `refs(caller_id → symbol_id)`. To measure on a 10k-file project: build time from
scratch, incremental time after touching one file, size on disk.

### v2 tools

| Tool | Parameters | Answers |
|---|---|---|
| `impact` | `symbol, depth=3, include_overridden=true, group_by=module\|package, offset, limit` | transitive callers grouped by module/package; entry points marked (v3) |
| `tests` | `symbol, transitive=false, depth=3` | v1 behaviour plus tests that reach the symbol through callers |
| `path` | `from, to, max_depth=6, limit=3` | shortest call paths from one symbol to another |
| `changed` | `base="HEAD", staged=false, paths=[]` | `git diff -U0` → definitions covering changed lines → their callers and tests; refuses to guess for stale shards |
| `dead_code` | `scope (module/package glob), profiles, offset, limit` | *candidates*: no references, not overriding anything external, not an entry point or framework-annotated per profiles, not used by name |
| `deps` | `level=module\|package, scope, cycles=true` | dependency edges with counts, and cycles (Tarjan) |
| `census` | `profile category` | counts and lists per profile category (entities, endpoints, screens…) |
| `cache_status` | — | the entry for this checkout: size, rows, last update, pending shards |

## Profiles and configuration (v2/v3)

Format: TOML, read with `tomllib`. This makes Python ≥ 3.11 the minimum (3.10 reached end of
life in October 2026); proposed.

### Profile (built-ins: `jpa`, `jdo`, `spring`, `jaxrs`, `vaadin`, `cucumber`, `junit`)

```toml
[profile]
name = "jpa"
description = "Jakarta / javax Persistence"

# What ends a call chain and keeps code alive (dead_code, impact, entry points).
[[entry_points]]
kind = "entity"
annotations = ["jakarta.persistence.Entity", "javax.persistence.Entity"]

[[entry_points]]
kind = "ui"                       # e.g. in the vaadin profile
supertypes = ["com.vaadin.flow.component.Component"]
annotations = ["com.vaadin.flow.router.Route"]

[[entry_points]]
kind = "test"                     # e.g. in the junit profile
annotations = ["org.junit.jupiter.api.Test", "org.junit.Test"]
type_names = ["*Test", "*IT"]     # globs on simple names

# Persistence model (v3 item 8). Attribute values come from the annotation's source text.
[persistence]
entity = ["jakarta.persistence.Entity", "javax.persistence.Entity"]
table = { annotation = "jakarta.persistence.Table", attribute = "name" }
column = { annotation = "jakarta.persistence.Column", attribute = "name" }
relations = ["jakarta.persistence.OneToMany", "jakarta.persistence.ManyToOne",
             "jakarta.persistence.OneToOne", "jakarta.persistence.ManyToMany"]

# Uses by name in strings (references(by_name)).
[by_name]
globs = ["*.java"]
patterns = []                     # extra regexes, {name} is the member name

# Annotation attributes indexed for concept search (v3 item 4).
[concepts]
attributes = ["io.swagger.v3.oas.annotations.media.Schema#description"]

# Where tests live and how to run them (printed, never run).
[tests]
tasks = ["*Test*"]                # compile task directories
paths = ["*/src/test/*"]          # fallback for single-root layouts
gradle_filter = "{task} --tests '{class}'"
maven_filter = "-pl {module} test -Dtest='{simple}'"
```

### Project configuration

`scipq-mcp --config FILE`, else `~/.config/scipq/config.toml` (`$XDG_CONFIG_HOME`). Later, if a
team wants it, a versioned `.scipq.toml` at the repository root.

```toml
[[project]]
match = { root_project = "shop" }   # or { path = "~/src/shop*" } (worktrees), or { git_remote = "…" }
profiles = ["jpa", "spring", "junit"]

[project.tests]
filter = "./run-tests.sh --tests '*{simple}*'"   # replaces the profile's filter

[[project.entry_points]]
kind = "screen"
type_names = ["*Screen", "*Crud"]

[project.by_name]
globs = ["*.java", "*.xml", "*.jrxml"]
patterns = ['\$F\{{name}\}']

[project.concepts]
attributes = ["com.example.meta.Label#text"]
```

Resolution: built-in profile ← user profiles (`~/.config/scipq/profiles/*.toml`) ← project
section. Unknown keys are reported, not ignored silently.

## v3 (order agreed)

1. `context(symbol, token_budget)`: definition with body, signatures of callers and callees,
   overrides and tests, trimmed to the budget.
2. Comparing checkouts (`project_root` A and B): API diff, what a merge breaks, clashes between
   worktrees; warns when either side is stale.
3. Cucumber-JVM: step text → glue method (cucumber expressions → regex), method → steps and
   `.feature` files, diff → affected features and the filter to run them.
4. Concept search with FTS5: identifiers split into words, signatures, javadoc and the profile's
   annotation attributes.
5. `doctor`: init script present, source sets or tasks without shards, class files much newer
   than their shards (compiled without the plugin), cache size.
6. `check_refs(files)`: resolve every `Type#member` mentioned in Markdown and report the missing
   or changed ones.
7. Entry points per profile on top of `impact`.
8. Persistence model per profile: entities ↔ tables, fields ↔ columns, relations, and who writes
   a field (setter uses, Lombok included, plus assignments found in the line).
9. Rename plan as a diff, never applied: exact columns, the override family, Lombok accessors,
   uses by name flagged for review; refuses when affected files are stale.
10. Generalize: Kotlin, and reading an aggregated `index.scip` from other indexers.

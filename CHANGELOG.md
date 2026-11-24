# Changelog

One section per version published on PyPI. `scripts/release.sh` turns `## Unreleased` into the
version and date when releasing.

## Unreleased

### Added

- `check_refs(since=…)`: a commit or branch (`HEAD`, `main`); of the scope, the docs
  changed since its merge base with HEAD (uncommitted changes included), only on their
  edited lines (the older problems are counted), and those that mention, as code, a `.java`
  file changed since then (only those mentions; a name several files have, such as
  `Config.java`, only with its folder or package). It lists the docs read for those
  mentions and the docs out of the scope that mention the changed code (by folder when
  many, in full with `format="md"`). CLI: `check-refs --since REF`.
- `init-config` proposes a `[check_refs]` section: the default scope, and the folders with
  Markdown outside it, those mostly named by date or ticket or in a log folder (`logs/`,
  `journal/`…) marked as likely records, the folder holding `skills/*/SKILL.md` as one agent
  bundle, and no generated output folders.

- `impact` says where the call chain is handed to a library: overrides of library methods
  it reaches (listeners, `run()`, a servlet's `service()`, Vaadin's `onAttach`…), with where
  their objects are created as the type the chain runs on (`new X(…)` of it or its
  subclasses, not a subclass constructor's `super(…)`; or that the framework builds them),
  since those are their real callers; types already listed as entry points are left out;
  and the callers whose calls are all inside an anonymous class or a lambda.

- `impact` marks the rows whose calls are all inside a lambda (one line or a block, cast,
  nested in another), a method reference or an anonymous class, and lists every such
  caller; a helper method of an entry point's type says it "is in a screen"
  (the type is the entry point), not that it is one itself.

### Fixed

- `check_refs` gives the same result in every run: of several files with the same name,
  the one checked depended on Python's hash order; now git decides (the one whose version
  the doc was written against had the name there).
- `check_refs` binds a line number alone after a path (`` `X.java:12`, `:30` ``) to the right
  file more often. A project type named right before it (`Engine.run :587`, `` `T.m`:237 ``)
  sends it to that type's file, a library's type ends the run; a parenthesis with its own list
  of lines (`(X.java:74, :853, :866)`) ends it; after `main X.java:N` (or `MAIN <sha>:path`)
  they are all main's. Far from its path (another sentence or table cell) it is reported only
  when git shows the doc's version had the name at that line, and never on a name like
  `get`, `add` or `setValue`. A type further back in the sentence (a call the doc describes)
  is not taken for the line's file.
- `check_refs` no longer places a blank or brace-only line by its text, no longer gives
  "now at" for names like `add` or `get`, does not take an older version of the file for a
  name written further from the line number, and reads a path cut at a `/` over two lines
  whole. The count per file is of problems, as in the header.
- `impact` rows show the same representative call in any build of the index.
- `check_refs` reads the versions of the docs one at a time (peak memory on a large
  checkout from 1.4 GB to about 450 MB).

### Changed

- `check_refs` reads by default only what agents read to work: `CLAUDE.md` and `AGENTS.md`
  in any folder and the `.md` files they name (links, `@imports`, paths), `.claude/**/*.md`,
  `.github/**/*.md`, `README*.md` and folders with a `SKILL.md`, instead of every Markdown
  file of the checkout; READMEs of third-party code (under `vendor/`, `node_modules/`, or
  with a LICENSE of their own inside a source tree) and a second link to a file already
  read are left out. A project sets its own scope with `[check_refs] files`
  (globs or folders; it replaces the default) in `.scipq.toml` or its `[[project]]` entry;
  `files` in the call still checks any doc. The output's `scope:` line says which set was
  read and from where, and how many other Markdown files were not (and how many are under
  archive folders or excluded). Archive folders are left out of any scope, the call's
  `files` too, unless a glob names them.
- `check_refs` globs follow the usual rules: `*` and `?` stay within a folder, `**` crosses
  folders (`doc/*.md` no longer takes `doc/logs/*.md`); a folder, or `dir/*`, in `exclude`
  leaves out everything under it.
- `check_refs` binds names to line numbers more finely. A name after an arrow, a
  preposition ("de X (File:N)") or before a parenthesis that goes on in prose still names
  the line but never makes the doc "wrong in its own version"; a member written alone in
  backticks (`` `admitir` ``) names the line when nothing else does; only `+` joins a list of
  names that must all be there ("y"/"and" may join clauses); "en main … y en la rama" ends
  in the branch; "en main" in an earlier clause ("… en main no. — evidencia: `X`") does not
  count. Quoted code: before the path (`` `a.b()` (File:N) ``), up to an ellipsis, inherited
  by "idéntico en File:N", and matched over line breaks.
- `check_refs` reads a paragraph or list item wrapped over several lines as one (names on
  the line before a path, line lists on the next one; names on the mention's own line come
  first), and reports each problem at the line of its mention. A line number alone after a
  path (`` `X.java:12`, `:30` ``) is checked as a mention of that file, with the path's names
  when it has none of its own (it never claims the doc was wrong in its own version).
- `check_refs` pairs calls and line ranges respectively when the lines together do not
  hold the names: "`execute()` … y `close()` … (`F.java:403-421` y `:383-401`)" checks
  `execute` at 403-421 and `close` at 383-401 (names without parentheses too:
  `` `opts.onOpen`/`onClose` ``), and says which one moved or changed; a name declared in the
  doc's version and not now says so instead of listing calls of that name. "el `x()` de la
  línea 106" binds x to line 106; a `;` does not end a version label's stretch.

## 0.7.0 — 2026-11-23

### Changed

- Tool descriptions say more about how to use the imprecise tools: `concepts` explains that
  spaces separate different words and `|` joins the alternatives of one word (synonyms
  written apart ask for both), and what to try when the results do not fit; `impact` names
  what it cannot see (reflection, library callbacks, code built without the plugin);
  `check_refs` says what to do with each kind of finding.

- `check_refs` follows line numbers through git history. The version of the code a doc was
  written against is the first commit, on any branch and through the doc's renames, whose
  copy of the doc has that line; the version just before that commit (doc and code
  committed together); or, when the lines named do not hold the name there, the newest of
  that commit's ancestors' versions of the file from the three weeks before it (merges
  included) that does. From it to the checkout: a block whose text stands unchanged gives
  its new number ("line 120 is now 134", a repeated line placed by its surroundings); lines
  edited since, inside the range too, say so with the date (in the commit's own time zone),
  the commit and where the name is now (within the named member's lines); lines that did not
  hold the name in the doc's own version say that (and where that line went), but only when
  the name is written right at the mention (its code span, `name (File:N)`, `File:N —
  name`, a neighbouring table cell that starts with code and is not a call chain): a name
  from further away claims nothing, though lines that stand intact elsewhere still give
  their new number. Lists are checked over all their lines. A
  line that holds the name now only through a call or a comment is reported when the doc's
  version had the declaration there. A name the file never had is not reported; a missing file is told deleted (when, which commit)
  or never there since the doc was written (planned, or another repository's file), in this
  checkout's history; a missing member, gone from its type's file since a date and commit, or not there when the
  doc was written either (planned, when no close name or other type explains it). A
  statement the doc quotes right after a line number (`obj.setOwner(user)`, also as
  `File:N — `code`` with nested spans), which the doc's version had at those lines and the
  file now has once, elsewhere, gives the new number (or, when its line was edited, that
  the lines changed and where the code is now); when the lines still have it, they are
  right.
- `check_refs` reads more of how docs write line numbers: lists (`:40,42-43`, `:12/20`,
  `:12 + 80-84`, `:30→:35`, `` `:40` y `:45` ``, `:12 y 18`) hold the name at any of their
  lines; a name written after another path or after a bare `:N` belongs to that one; the
  word glued before a path's parenthesis counts only when it looks like code (`` `foo` ``,
  `foo()`, `camelCase`), not after a negation, nor after an arrow unless the file declares
  it; a list of names (`a()` + `b()` (File:N)) needs each of them at the lines; when the
  glued names cannot be checked in that file, the clause's are; a mention that opens its line takes the call that ended the line before;
  `getX` holds where the field `x` is declared; implicit members (`toString`, `close`…) are
  required at a line only when the file declares them; a type's name holds anywhere in its
  body; `main <sha> path`, `<sha> path`, `path (main)`, "en main …" earlier in the clause
  and the labels of `[check_refs] other_versions` (`v4 path`, `v4 Type.member`, over the
  rest of their sentence or list, up to another version word) are another version (a user configuration with project keys outside `[[project]]` now warns); name templates
  (`com.example.Run<N>Test`, `com.example.Run*`) are not type names; a table cell next to the
  path counts only when it starts with code, up to its first clause; a shorter parameter
  list than every overload is shorthand (`put(Date)`). `Type.member` alone in a code span is
  checked too, and reported only when git shows the type had that member.

## 0.6.0 — 2026-11-23

### Added

- `concepts(query)`: definitions found by the words they are about, for when their names are
  not known. The auxiliary index (schema 11) keeps one FTS5 row per definition: its name split
  into words, its owner, its annotations' texts, its signature, its javadoc, and the labels
  other code gives a type where it registers it (the strings of a call that names `X.class`).
  Each word is searched as a prefix with accents ignored; the caller writes a word's forms
  and synonyms as alternatives (`pago|pagar|payment`), and a word alone is reduced to a
  light stem (plurals and common Spanish and English endings off). Ranked by how many of the
  query's words a definition holds and how surely (its name, then annotations and labels,
  then signature or javadoc), then bm25, types before methods before fields, constants and
  private members last, words in the owner or package as a bonus; tests, files whose header says they are
  generated and `[concepts] exclude` paths are left out unless asked.
- `doctor()`: a checklist of what can make the index wrong or the tools weak: the plugin's
  setup (which init scripts apply to this project, and why not: a PROJECTS list, a root
  project name or prefix), tracked `.java` files without shards (next to compiled ones, or
  where nothing compiles; files that declare nothing to compile or that a build file
  excludes are told apart), classes built after the shards (an IDE build, the plugin
  off) and shards without classes (javac failed; named), stale files and running builds
  (with their pid), the Gradle build cache, indexes of deleted checkouts and cache entries
  left without an index (`cache prune` removes both), configuration warnings, the JDK for
  library sources, and what refactoring would use.
- `check_refs(files, exclude, format)`: the code Markdown mentions (docs, skills, agent
  instructions), checked against the index at once: `Type#member`, `Type.method(…)`,
  qualified names (packages may have upper-case segments), `{@link}`s and `File.java:N`
  paths (abbreviated with `.../` or with folders left out). Reports types and members not
  there (with the closest names, and where a type or member of that name lives now),
  parameter lists no overload has, names none of the same-named types has, missing files,
  lines past the end and line numbers that no longer fall in the symbol named next to them
  (the names of the mention's own part of the line, the one glued to the path first: the
  lines must fall in that member, javadoc and annotations included, or still mention it,
  ±2; else "line 70 no longer mentions foo (foo is at 245–254; called at 62)"; other
  versions of a file, `git show <sha>:path`, are left alone); counts by kind, then by file with
  repeats merged, or as a Markdown table. Left alone: library types (from the library
  index, under the packages the project uses), placeholders (`Foo`, `getX`), constants as
  receivers, members of nested builders, Object/Class/enum and JDO-enhancer members, Python
  snippets and names, examples ("por ejemplo", "e.g."), and mentions the line says are gone
  ("removed", "borrado"); paths are also resolved from the document's folder and against
  files that exist but are not indexed. A library type found only in an old cached version
  of its artifact is reported ("only in ui-data 2.10.5; 25.1.5 has none"). Folders of closed docs (`archive/`, `done/`, `archivo/`…) are left
  out unless `files` names them; `[check_refs] exclude` in `.scipq.toml` adds globs; a
  folder in `files` means its Markdown files.
- `impact` and `changed` list the entry points the change reaches, by kind (screens,
  endpoints, jobs, servlets, steps…), one row per type with its reached members, their depth
  and what identifies them (an endpoint's verb and route from the type's and the method's
  paths, an annotation's first string, the declared supertype, the descriptor with its URL
  patterns, and which element names them: servlet, filter or listener). Overloads show
  once (`save(+1)`). The `jdo` and `jpa` entity rules keep entities alive for `dead_code`
  without listing them. `impact` looks for them to depth 6 even when its rows stop sooner, and says
  when the symbol is an entry point itself. A private method, or a controller's method
  without a route, is not one. A rule with `reached = false` keeps code alive for
  `dead_code` without being listed: the `spring` profile's beans; its events, schedules and
  startup hooks (`@EventListener`, `@Scheduled`, `@PostConstruct`, runners) are kinds of
  their own; a startup hook that only registers a lambda, a method reference or an
  anonymous class is not one.
  Rules may take the types an annotation names (`referenced_by`: the `vaadin` profile's
  layouts in `@Route(layout = …)`), and profiles may list `[lifecycle] methods` the framework
  calls on an object it was given (Vaadin's `onAttach`): `impact` goes on from them to
  whoever creates that object, reaching the screen that adds a component. An `[[entry_points]]` rule may
  combine `supertypes` and `type_names` (a type must match both) and name classes listed in
  XML files (`[[entry_points.xml]] files, elements`); `dead_code` keeps those classes too.
  A `servlet` profile (annotations and `web.xml`), and Vaadin 7/8 UIs and views in `vaadin`.
- `persistence(symbol)`: the persistence model the active profiles describe. No symbol: the
  entities with their tables, inheritance and field counts. An entity: its table (or its
  superclass's, with SUPERCLASS_TABLE), inheritance, discriminator, identity, persistent
  fields with their columns (explicit, from `@Column(name)` or `@Persistent(column)`, or from
  the field name), relations (with mappedBy and join tables), fetch groups and unique/index
  members. Where a field is stored: its column (the default one named), the other side's key
  for a mappedBy collection, the join table and its columns; relations typed with an
  interface that one entity implements, and the mappedBy field on the other side; a version
  declared on the class (strategy, column, field), converters, unique constraint names,
  length, nullability, default values and the default fetch group. A field: its column and relation, its accessors, who writes and
  reads it (setters, Lombok's too, and assignments) and the lines that use it by name,
  ranked: strings in its type's own files (forms, annotations), paths through relations to
  the type, step tables that name the type, other strings read as code, other mentions; map
  keys and paths through other types' fields of that name are counted. The `jdo` and `jpa`
  profiles describe keys, versions, transient markers, inheritance, discriminators, mappedBy
  and join tables; `[persistence] column` takes several annotations.

### Changed

- An `[[entry_points]]` rule that gives both `supertypes` and `type_names` now needs both to
  hold for the entry points `impact` and `changed` list (`dead_code` still keeps a class
  that matches either). `doctor` and the configuration warnings flag such rules unless
  they say `match = "all"` (both) or `match = "any"` (either).

### Fixed

- Refactoring: `change_signature` accepts `no_calls` again; removing every parameter of a
  varargs method is a plan error, not a crash; `verify` counts a patched file as compiled
  only when its shard is newer than the patched file; renaming a method family edits every
  copy of a member defined in several modules; a move warns about package-private members
  of sibling types whose names start with the moved type's; annotations with arguments no
  longer hide a member's `private` or its parameter types.
- A file is generated only when its banner (the comments before `package`) carries a
  generator's notice, or its type is annotated `@Generated`: prose in a type's javadoc
  ("the CSS generated by the editor", "generado por") no longer hides hand-written files
  from `concepts`, `dead_code` and refactoring.
- `impact`, `changed`, `tests(transitive)` and `callers(include_overridden)` no longer count
  `super.m()` calls in other classes as possible dispatches to an override of `m`: they are
  bound to the caller's superclass.
- `impact` no longer takes every call of a method an override overrides as a call of the
  override: a call counts when it comes from the overriding type's hierarchy, from a file
  that names that type or a subtype, or from a file that names only the base type (a
  generic call on any implementation); calls on objects of another type are left out, and
  counted in a note. The same holds for code reached through such a dispatch.
- `path` goes through dispatch as `impact` does: a call of a project method that a method
  on the way overrides shows the overridden method, then the override.
- `impact` follows template methods at every level: when an override of type X is reached,
  the base class's own calls to the method it overrides (`this.m()`) reach it too, also
  when the override is the symbol asked about; other objects' calls to the base method, and
  sibling classes' calls into that code on their own objects (`this.`, `super.` or no
  receiver), are left out. Abstract classes and interfaces are not entry points by supertype or name rules.

## 0.5.0 — 2026-11-23

### Fixed

- A source written again with the content it was compiled from (a checkout and back,
  re-applying a patch) is no longer reported stale forever: Gradle finds the build up to date
  by content and never rewrites the shard. The auxiliary index keeps each source's SHA-1 as
  compiled (index schema 5: rebuilt once) and the freshness check compares it for the files
  that look newer than their shards; `index_status` counts them apart.
- `compare`: renaming a parameter is not a signature change (scip-javac's signatures carry
  parameter names); `all_worktrees` finds each worktree's build when it lives in a
  subdirectory of the repository; moved types are matched without rescanning every symbol.
- Cucumber: a `Scenario` with `Examples` (Gherkin 6+) gets one scenario per row; a changed tag
  line belongs to the scenario below it, a Rule's header or Background to the Rule's
  scenarios.
- `context`: reads of a record component named `set…` are not listed as writes.
- `.scipq.toml` and user configuration: a string where a list of globs is expected counts as
  one glob (it was split into characters, one of them `*`).

### Added

- Refactoring (V3 item 9): `refactor(plan, dry_run)` runs a plan (JSON: macros `rename`,
  `change_signature`, `replace_calls`, `move`, `annotate`, `create`, or `select`/`do`
  primitives with templates, node kinds and structural `where` patterns) into a patch stored
  in the cache, never applied: sites from the index (override families, Lombok and record
  accessors, constructors, imports, static imports, javadoc links), text edits of the original
  ranges, every edited file re-parsed by JavaParser with the expected node over each edit.
  Refuses stale files, library overrides, name clashes, shadowing by locals or enclosing
  classes, and renames that would change a JDO/JPA column, table or class-name discriminator
  (`keep_column`, `migration`); lists uses by name to review. `refactor(plan_id,
  verify=true)` checks the index after the patch is applied and compiled. `refactor_help`
  documents the format; an MCP prompt `refactor` walks through it.
- Refactoring checked on a large project (a rename over 823 files, a change of signature, an
  API migration with templates, a move, a persistent-field rename): moves done in several
  steps are computed together, with package-private access across the new boundaries
  reported; overrides in anonymous classes are renamed; `select` takes `symbols` and
  `overloads`, and `family` with library methods; `change_signature` keeps varargs
  arguments, imports the types it adds and reports a new parameter hiding a field; javadoc
  links of other overloads, or of types whose names end like the renamed one, are left
  alone; the review list keeps a persistent field's query paths and fixture tables (and
  drops prose); dry runs show the checks' refusals; `verify` catches a failed build from its
  class files and lists the tests that named the old names in text; unknown plan keys are
  refused.
- `scipq-mcp tools install javaparser` (pinned version and SHA-256) and `tools status`.
  JavaParser is not bundled: the newest jar in the cache, the Maven repository or Gradle's
  cache is used, in a short-lived JVM at the lowest priority.

## 0.4.0 — 2026-11-23

### Added

- Cucumber-JVM (V3): `steps(query, check)`. Step definitions come from the auxiliary index
  (annotations of `io.cucumber.java.<language>`, and old `cucumber.api.java`), their text read
  from the source (literals concatenated, `value =`); Cucumber Expressions (`{int}`,
  `{string}`, `{word}`, `{float}`, `{}`…, `@ParameterType` ones, optional `(s)`,
  alternatives `a/b`, escapes) and regexes become Python regexes. `.feature` files are read
  with Gherkin's keywords for their `# language:` (en, es, pt, fr, de, it, nl; English by
  default), the keyword removed exactly; each Examples row is a scenario, tags are inherited
  (feature, rule, examples), and a Background's steps belong to every scenario of its feature
  or rule. For a step definition: its steps and the scenarios to run; for a .feature: each
  step's method (undefined, ambiguous); for a step's text: its method; `check=True`: undefined,
  ambiguous, not understood and unused step definitions.
- `tests(transitive=true)` and `changed` list the Cucumber scenarios affected (glue reached,
  changed `.feature` lines) and how to run them: `[cucumber] run` with `{names}` /
  `{features}`, `run_features` with `{tags}` (whole features with a tag of their own); more
  than 150 scenarios: whole features by tag, or the whole suite (`run_all`). Scenarios carrying
  an `exclude_tags` tag are reported; a name filter's extra runs (scenarios elsewhere with the
  same name) are noted, and so is the template name reports show for outline rows.
- `[cucumber] include_steps`: steps that load another feature's Background (the quoted path,
  resolved against the resource roots or by suffix). The included steps count where the
  including step runs (every scenario for a Background, one for a scenario), transitively;
  changed lines of an included file select those scenarios; a missing file is reported.
- `steps(check=True)` lists apart the undefined steps that only occur in scenarios the build
  leaves out and those that run in no scenario (a Background no include step loads), so the
  first list is what needs fixing; step counts say how many distinct lines they are.

## 0.3.0 — 2026-11-22

### Added

- `compare(other_root)`: this checkout against another one (a worktree or a clone), from both
  auxiliary indexes (B's is built if missing). Public/protected definitions only in one side
  or with another signature, and maybe-renamed pairs; when both share git history, the API
  differences classified by who did what since the merge base (added in A, removed in B,
  changed in B…), the files each side changed (and how many are .java), members changed on
  both sides (conflicts likely), and what merging one into the other would break: the symbols
  one side removed or changed in files only it touched, against the other side's uses,
  overrides and concrete subtypes (for a new abstract method) in files that other side
  modified or added since the base — a file it did not touch ends up with the first side's
  version, already adapted. The API section covers public/protected
  members; what a merge breaks covers every visibility. Annotations are ignored, a type's
  header changing breaks nothing, and moved types (same simple name and members, another
  package) are paired like renames. `all_worktrees=True`: every other worktree against this
  one, one line each (at most 2 missing indexes built per call; locked, prunable, uncompiled
  and compiling worktrees reported, not built). B's index is never built while a build runs
  there. Freshness of both sides first. Read-only.

- `[dead_code] exclude` (in `.scipq.toml` or a `[[project]]` entry): path globs whose
  definitions are never dead-code candidates, e.g. generated web-service clients; the answer
  says how many were left out.
- `init-config` proposes generated directories for `[dead_code] exclude` too, and finds them
  at the right level: the highest directory where at least 90% of the files carry a
  "generated" header, so a module mixing its own code with generated packages gets only those
  packages. Name-based detection (`*-out`, `generated`, `gen`, `dist`) applies only to
  directories without Java sources, and no longer to `reports` (report templates are where
  names are used as text).

### Fixed

- Library sources (definition, source, members, overloads of JDK and dependency symbols):
  every variable of `int a, b;` (about 900 fields of the JDK's java.base were missing), nested
  `@interface` types (they were fields), records' compact constructors (they were fields),
  enum constants with arguments or bodies (they were fields or methods), and nested type names
  resolved at their own level (`Node.OfInt` could resolve to `Node.Builder.OfInt`). Checked
  against a full Java parser on java.base (79,192 members), Vaadin flow-server and Guava: no
  difference left, names, lines and overload order included.
- Library sources, second pass over 1,040 sources jars: type arguments in field initializers
  (`new HashMap<java.lang.String, X>()`, `new Accessor<boolean[], Boolean>()`,
  `Entry.<A, B>of()`) no longer split a field in two; identifiers with non-ASCII letters
  (`año`, and combining marks as in i18n annotation names); a method named like its class
  with a return type is a method, not a constructor. Checked against a full Java grammar on
  the newest version of every cached sources jar (1,040 artifacts, 2.57 M members): no
  difference left except in two files that grammar cannot parse.
- `members` of library enums lists their constants, and of library records their components
  and implicit accessors (`def` of an implicit accessor works too).
- `typed`: annotation uses are an `annotation` kind (they were fields, return types or
  initializers), and `throws X` a `throws` kind (it was a method body).
- `context` on a field tells writes from reads at each reference's column (also with the
  operator on the next line): in `this.a = other.a;` the second `a` is a read; two uses on one
  line are one row with `(×2)`.
- `init-config`: a generator notice counts only in a file's header (a comment before the type,
  or `@Generated` on it), not `@AutoGenerated` members of a visual designer nor Eclipse's
  "Auto-generated method stub"; `[dead_code] exclude` is proposed only for directories with
  Java sources.
- Records whose header spans several lines get their whole declaration.
- `annotated` counts an annotation after a string containing `//` on the same line.

## 0.2.2 — 2026-11-22

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

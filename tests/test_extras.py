import os

import pytest

from scipq_mcp import extras as X
from scipq_mcp.queries import Navigator
from scipq_mcp.service import Service
from scipq_mcp.workspace import Workspace, build_task

SET_STATE = "com/example/model/Entity#setState()."


@pytest.fixture
def nav(sample_root):
    return Navigator(Workspace(sample_root))


def test_source_keeps_indentation(nav):
    src = X.source(nav, SET_STATE)
    assert [n for n, _ in src.lines] == [14, 15, 16]
    assert src.lines[1][1] == "        this.state = state;"
    gen = X.source(nav, "com/example/model/Snapshot#getChangedAt().")
    assert gen.definition.generated and gen.lines == [(9, "    protected Date changedAt;")]


def test_members(nav):
    groups, external = X.members(nav, "com/example/model/SubAccount#")
    own = groups[0]
    assert {m.symbol for m in own.members} == {"com/example/model/SubAccount#`<init>`().",
                                               "com/example/model/SubAccount#uid()."}
    entity = [g for g in groups if g.owner == "com/example/model/Entity#"][0]
    names = {m.symbol.rsplit("#", 1)[1] for m in entity.members}
    assert names == {"setState().", "getState().", "findById()."}  # uid() is overridden
    assert entity.hidden == 1
    assert external == []


def test_annotated(nav):
    items = X.annotated(nav, "java/lang/Override#")
    assert {a.definition.symbol for a in items if a.definition and not
            a.definition.symbol.startswith("local")} == {
        "com/example/model/Account#uid().", "com/example/model/Entity#uid().",
        "com/example/model/Group#uid().", "com/example/model/SubAccount#uid().",
        "com/example/model/Session#close()."}
    getter = X.annotated(nav, "lombok/Getter#")
    assert [a.definition.symbol for a in getter] == ["com/example/model/Snapshot#"]


def test_typed(nav):
    uses = X.typed(nav, "com/example/model/Entity#")
    cats = {}
    for u in uses:
        cats.setdefault(u.category, set()).add((os.path.basename(u.location.source),
                                                u.location.line))
    assert cats["field"] == {("Service.java", 10), ("Group.java", 7)}
    assert ("Group.java", 9) in cats["parameter"]
    assert cats["return type"] == {("Entity.java", 22), ("Session.java", 4), ("Store.java", 10)}
    assert cats["supertype"] == {("Account.java", 3), ("Group.java", 6)}
    assert cats["local variable"] == {("ServiceTest.java", 9)}
    assert ("ServiceTest.java", 9) in cats["method body"]  # Entity.findById(…) initializer
    assert len(cats["import"]) == 3


def test_family_and_overloads(nav):
    fam = {m.symbol: m.relation for m in X.family(nav, "com/example/model/Account#uid().")}
    assert fam == {"com/example/model/Account#uid().": "this",
                   "com/example/model/Entity#uid().": "ancestor",
                   "com/example/model/Identifiable#uid().": "ancestor",
                   "com/example/model/SubAccount#uid().": "descendant",
                   "com/example/model/Group#uid().": "sibling"}
    over = X.overloads(nav, "com/example/app/Service#process().")
    assert [m.symbol for m in over] == ["com/example/app/Service#process().",
                                        "com/example/app/Service#process(+1)."]
    assert [len(m.uses) for m in over] == [1, 0]


def test_uses_by_name(sample_copy):
    path = os.path.join(sample_copy, "app/src/main/java/com/example/app/Keys.java")
    with open(path, "w") as fh:
        fh.write('class Keys { String k = "changedAt"; String other = "changedAtX"; }\n')
    hits = X.uses_by_name(sample_copy, X.property_names(
        "com/example/model/Snapshot#getChangedAt()."), ["*.java"], set())
    assert [(h.source, h.line, h.name) for h in hits] == [
        ("app/src/main/java/com/example/app/Keys.java", 1, "changedAt")]
    assert X.property_names("com/example/model/Snapshot#isArchived().") == ["isArchived",
                                                                            "archived"]


def test_tests_and_tasks(nav):
    uses = X.tests_using(nav, SET_STATE)
    assert [(t.test_class, t.module, t.lines) for t in uses] == [
        ("com.example.app.ServiceTest", "app/compileTestJava", [11])]
    assert build_task("core/compileJava") == ":core:compileJava"
    assert build_task("_root/compileJava") == ":compileJava"
    assert build_task("a/b/compileQaTestJava") == ":a:b:compileQaTestJava"
    assert X.test_task("app/compileTestJava") == ":app:test"
    assert X.test_task("app/compileQaTestJava") == ":app:qaTest"


def test_index_status(nav):
    st = X.index_status(nav)
    assert st.shards == 19
    assert st.orphans == ["core/src/main/java/com/example/model/Gone.java"]
    assert dict(st.tasks) == {"app/compileJava": 3, "app/compileTestJava": 1,
                              "core/compileJava": 15}


def test_generated_member_located_at_generating_node(nav):
    d = nav.definition("com/example/model/Snapshot#isArchived().")
    assert d.generated and d.location.line == 10 and d.generated_by is not None


def test_pagination_and_batch(sample_root):
    svc = Service(sample_root)
    assert "(showing 3–4)" in svc.implementations("Identifiable#uid", limit=2, offset=2)
    assert "(showing 1–1; next offset 1)" in svc.find("setState", limit=1)
    out = svc.batch([{"tool": "references", "args": {"symbol": "Entity#setState"}},
                     {"tool": "definition", "args": {"symbol": "Entity"}},
                     {"tool": "nope"}])
    assert "### 1. references Entity#setState" in out and "8 references" in out
    assert "### 2. definition Entity" in out and "class at core/" in out
    assert "unknown tool 'nope'" in out


def test_setup(sample_root, tmp_path, monkeypatch):
    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "gradle-home"))
    ready = Service(sample_root).setup()
    assert "The tools are ready" in ready
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'shop'\n")
    out = Service(str(tmp_path)).setup()
    assert "Build: Gradle, root project 'shop'" in out
    assert 'PROJECTS = ["shop"]' in out and "-Xplugin:scip" in out
    (tmp_path / "settings.gradle").unlink()
    (tmp_path / "pom.xml").write_text("<project/>")
    assert "maven-compiler-plugin" in Service(str(tmp_path)).setup()
    assert "Call the `setup` tool" in Service(str(tmp_path)).references("X")


def test_single_root_layout(sample_copy):
    """All shards under build/scip-targetroot/META-INF/scip (one -targetroot for every task)."""
    import shutil
    base = os.path.join(sample_copy, "build", "scip-targetroot")
    flat = os.path.join(sample_copy, "build", "flat")
    for d, _, files in os.walk(base):
        for n in files:
            src = os.path.join(d, n)
            rel = os.path.relpath(src, base).split("/META-INF/scip/", 1)[1]
            dst = os.path.join(flat, "META-INF", "scip", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    shutil.rmtree(base)
    os.rename(flat, base)
    svc = Service(sample_copy)
    assert "8 references in 5 files" in svc.references("Entity#setState")
    assert "WARNING" not in svc.references("Entity#setState")
    group = os.path.join(sample_copy, "core/src/main/java/com/example/model/Group.java")
    os.utime(group, (1_700_000_500, 1_700_000_500))
    out = svc.references("Entity#setState")
    assert "in these results: core/src/main/java/com/example/model/Group.java" in out
    assert "recompile the project to refresh" in out
    assert "com.example.app.ServiceTest" in svc.tests("Entity#setState")


def test_tests_ignore_production_code_in_test_packages(nav):
    # TestSupport lives in src/main/java/.../app/test/: production code, not a test.
    assert [t.test_class for t in X.tests_using(nav, SET_STATE)] == ["com.example.app.ServiceTest"]


def test_annotated_ignores_javadoc_comments_and_class_literals(nav):
    items = X.annotated(nav, "java/lang/Deprecated#")
    assert [(os.path.basename(a.location.source), a.location.line, a.definition.symbol)
            for a in items] == [("Store.java", 9, "com/example/model/Store#load().")]
    line = "    @javax.annotation.Nonnull String"
    assert X.is_annotation_use(line, line.index("Nonnull"))
    doc = " * {@link Session} and {@code @Session}"
    assert not X.is_annotation_use(doc, doc.rindex("Session"))


def test_try_with_resources_is_not_a_field(nav):
    uses = {(u.category, u.location.line) for u in X.typed(nav, "com/example/model/Session#")}
    assert ("field", 7) in uses          # private final Session shared
    assert ("resource", 11) in uses      # try (Session session = …)
    assert not any(c == "field" and line == 11 for c, line in uses)
    # …and it is not taken for a caller: the call belongs to the method.
    root = nav.callers("com/example/model/Session#find().")
    assert [c.symbol for c in root.children] == ["com/example/model/Store#load()."]


def test_records(nav):
    d = nav.definition("com/example/model/Point#")
    assert d.kind == "record"
    assert d.info.signature == "public record Point(int x, int y)"
    svc = Service(nav.ws.root)
    out = svc.references("Point#x")
    assert "includes the uses of its accessor Point#x()" in out
    assert "6: return new Point(x() * k, y * k);" in out
    assert "(implicit, not declared in source)" in svc.references("Point#x()")


def test_supertypes_direct_from_declaration(nav):
    _d, supers = nav.supertypes("com/example/model/Session#")
    assert [(s.symbol, s.direct) for s in supers] == [("java/lang/AutoCloseable#", True)]
    _d, supers = nav.supertypes("com/example/model/SubAccount#")
    assert [s.symbol for s in supers if s.direct] == ["com/example/model/Account#"]


def test_fqn_overloaded_names(nav):
    r = nav.resolve("com.example.app.Service.process()")
    assert r.symbol is None and {d.symbol for d in r.candidates} == {
        "com/example/app/Service#process().", "com/example/app/Service#process(+1)."}
    assert nav.resolve("com.example.app.Service#process(+1)").symbol == \
        "com/example/app/Service#process(+1)."


def test_find_by_fragment(sample_root):
    svc = Service(sample_root)
    out = svc.find("changed")
    assert "no exact name; names containing it" in out and "Snapshot#getChangedAt()" in out
    assert "Account#Builder#build()." in svc.find("bui", match="prefix")
    assert "Snapshot#getChangedAt()." in svc.find("gCA", match="camel")
    assert "Account#uid()." in svc.find("Account#UID", match="contains")
    assert "names containing it" in svc.references("ChangedAt")


def test_type_references_drop_imports_by_default(sample_root):
    svc = Service(sample_root)
    assert "import com.example.model.Entity;" not in svc.references("Entity")
    assert "import com.example.model.Entity;" in svc.references("Entity", exclude_imports=False)


def test_ambiguity_tip(sample_root):
    out = Service(sample_root).references("setState")
    assert "tip: add the owner type" in out


def test_scope_filters(sample_root):
    svc = Service(sample_root)
    assert "in com.example.app: 6 references in 3 files" in svc.references(
        "Entity#setState", scope="com.example.app")
    out = svc.callers("Entity#setState", scope="core/*")
    assert "Account#<init>(String)" in out and "Service#" not in out
    assert "Store#load(long)" in svc.annotated("Deprecated", scope="com.example.model")
    assert "nothing" in svc.annotated("Deprecated", scope="com.example.app") or \
        "0 uses" in svc.annotated("Deprecated", scope="com.example.app")


def test_typed_modifiers(nav):
    svc = Service(nav.ws.root)
    out = svc.typed("Entity", categories=["field"], modifiers=["static"])
    assert "Service#DEFAULT" in out and "Group#members" not in out


def test_javadoc_mentions(nav):
    hits = X.javadoc_mentions(nav.ws.root, "com/example/model/Session#")
    assert [(os.path.basename(h.source), h.line) for h in hits] == [("Store.java", 4)]
    assert "javadoc mentions" in Service(nav.ws.root).references("Session", by_name=True)


def test_typed_declared_type_only(nav):
    """A field's type is what is declared before its name: not its initializer, and not a
    type that only qualifies a nested type's name (`Map` in `Map.Entry`)."""
    def cats(symbol):
        out = {}
        for u in X.typed(nav, symbol):
            if os.path.basename(u.location.source) == "Registry.java":
                out.setdefault(u.category, set()).add(u.location.line)
        return out

    assert cats("java/util/Map#") == {"field": {10}, "qualifier": {12}, "import": {4}}
    # (`new ConcurrentHashMap<>()` on line 10 references the constructor, not the type)
    assert cats("java/util/concurrent/ConcurrentHashMap#") == {"initializer": {11},
                                                              "import": {6}}
    assert cats("java/util/Set#")["field"] == {11}


def test_typed_with_subtypes_has_one_section_per_category(nav):
    out = Service(nav.ws.root).typed("com/example/model/Entity#", subtypes=True)
    headers = [line for line in out.splitlines() if line.endswith(":") and
               not line.startswith(" ")]
    assert len(headers) == len(set(headers)) and "field:" in headers
    assert "[Account]  Service#seed" in out and "[Group]  Service#activateAll(Group)" in out


def test_index_status_profiles(sample_copy, tmp_path):
    svc = Service(sample_copy, cache_dir=str(tmp_path / "cache"))
    assert "once the auxiliary index exists" in svc.index_status()
    svc.dead_code()
    assert "auto (annotations found in the project)" in svc.index_status()


def test_test_commands_for_every_layout():
    from scipq_mcp import render
    cmds = render.test_commands({"_root/compileTestJava": ["a.BTest"],
                                 "app/testCompile": ["x.ATest", "x.CTest"],
                                 "core/compileIntegrationTestJava": ["c.ITest"],
                                 "a/b/compileTestJava": ["d.DTest"],
                                 ".": ["e.ETest"]}, "./gradlew")
    assert cmds[0] == "./gradlew :test --tests 'a.BTest'"
    assert cmds[1] == "mvn -pl app test -Dtest='ATest,CTest'"
    assert cmds[2] == "./gradlew :core:integrationTest --tests 'c.ITest'"
    assert cmds[3] == "./gradlew :a:b:test --tests 'd.DTest'"
    assert cmds[4].startswith("./gradlew test --tests 'e.ETest'")
    t = render.format_test_filter("run {module} {task} {tests}", "_root/compileTestJava",
                                  ["a.BTest"])
    assert t == "run  :test --tests 'a.BTest'"
    t = render.format_test_filter("run {module} {task}", "core/compileIntegrationTestJava", ["c"])
    assert t == "run core :core:integrationTest"


def test_is_annotation_use():
    line = '@Url("https://example.com") @Inject Foo foo;'
    assert X.is_annotation_use(line, line.index("Inject"))
    assert not X.is_annotation_use("int x; // @Inject later", 17)
    assert not X.is_annotation_use(' * {@link Inject}', 10)
    assert not X.is_annotation_use('String s = "@Inject";', 13)
    assert X.is_annotation_use("@Inject", 1)


def test_typed_annotations_and_throws(nav):
    uses = [u for u in X.typed(nav, "com/example/model/Label#")
            if u.location.source.endswith("Invoice.java")]
    assert [(u.category, u.location.line) for u in uses] == [("annotation", 7)]
    assert X._THROWS_BEFORE.search("public void load(String p) throws IOException, ")
    assert X._THROWS_BEFORE.search("        throws ")              # on the next line
    assert not X._THROWS_BEFORE.search("throw new ")


def test_doctor(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service
    with open(os.path.join(sample_copy, "gradle.properties"), "w") as fh:
        fh.write("org.gradle.caching=true\n")
    classes = os.path.join(sample_copy, "core/build/classes/java/main/com/example/model")
    os.makedirs(classes)
    for name in ("Entity.class", "Account.class"):
        open(os.path.join(classes, name), "wb").close()    # now: after every shard
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).doctor()
    assert out.startswith("doctor: ") and "WARN [build] the Gradle build cache is on" in out
    assert "WARN [plugin] core/compileJava: classes compiled at" in out
    assert "shards have no class file" in out                 # Store, Session… are missing
    assert "core/src/main/java/com/example/model/" in out.split("have no class file")[1]
    assert "[refactor]" in out and "[cache]" in out and "version " in out


def test_doctor_files_with_nothing_to_compile_and_empty_cache_entries(sample_copy, tmp_path):
    import os
    import subprocess
    import time
    from scipq_mcp import cachedir
    from scipq_mcp.service import Service
    model = os.path.join(sample_copy, "core/src/main/java/com/example/model")
    with open(os.path.join(model, "Old.java"), "w") as fh:
        fh.write("// package com.example.model;\n/*\npublic class Old {}\n*/\n")
    with open(os.path.join(model, "package-info.java"), "w") as fh:
        fh.write("/** The model. */\npackage com.example.model;\n")
    with open(os.path.join(model, "Fresh.java"), "w") as fh:
        fh.write("package com.example.model;\n\npublic class Fresh {}\n")
    with open(os.path.join(model, "Skipped.java"), "w") as fh:
        fh.write("package com.example.model;\n\npublic class Skipped {}\n")
    with open(os.path.join(sample_copy, "core", "build.gradle"), "w") as fh:
        fh.write('sourceSets { main { java { exclude("com/example/model/Skipped.java") } } }\n')
    subprocess.run(["git", "init", "-q"], cwd=sample_copy, check=True)
    subprocess.run(["git", "add", "-A"], cwd=sample_copy, check=True)
    cache = tmp_path / "c"
    empty = cache / "gone-0123456789ab"
    empty.mkdir(parents=True)
    old = time.time() - 3 * 86400
    os.utime(empty, (old, old))
    out = Service(sample_copy, cache_dir=str(cache)).doctor()
    assert "2 tracked .java files declare nothing to compile" in out
    assert "1 tracked .java files are excluded by the build" in out and "Skipped.java" in out
    assert "1 tracked .java files sit next to compiled ones but have no shard" in out
    coverage = next(x for x in out.splitlines() if "sit next to compiled ones" in x)
    assert "Fresh.java" in coverage and "Old.java" not in coverage
    assert "1 cache entries without an index" in out and "gone-0123456789ab" in out
    assert [e["dir"] for e in cachedir.prune(str(cache))] == [str(empty)]
    assert not empty.exists()


def test_check_refs(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service
    with open(os.path.join(sample_copy, "NOTES.md"), "w", encoding="utf-8") as fh:
        fh.write("""# Notes
- `Entity#setState(int)` changes it; see core/src/main/java/com/example/model/Entity.java:14.
- `Entity#setState(String)` takes a reason.
- Call `Entity.findById(id)` and then `Entity.reload()`.
- `com.example.model.Ghost` and `Nope#run` are missing.
- `Entity.setState(...)` lives at core/src/main/java/com/example/model/Entity.java:3.
- Old file: core/.../model/Gone.java; core/.../model/Account.java is fine; `Session.find(1)`.
- Prose: Logger.info("x"), List.of(1), Account.class and Group.feature are not members.
- `com.example.Old` was removed; `Gone#run` no longer exists.
- Placeholders and the like: `Foo.bar()`, `Entity.getX()`, `LOG.info(x)`, `Entity.hashCode()`,
  `Entity.dnGetState()`, `java.util.Lizt`, `java/util/Lizt.java`, `X.java`.
- Partial and wrong packages: `model.Account#uid`, `com.example.Account`, `Outer$Inner#run()`.
- Again: `Entity.reload()`.
- Not indexed but there: `tools/Probe.java`, `Migrate.java`.
- Paths here: `M/` = `core/src/main/java/com/example/model/`; `M/Account.java` and `X_main.java`.
- Cut names: …Old.setValue(1) is not a type.

| `Entity.setState()` | core/src/main/java/com/example/model/Entity.java:3 | a row |
- `app/src/main/java/com/example/app/Service.java:2` — `a.setState(1)`
- Constants, nested types, examples: `Service.DEFAULT.getState()`, `Service.NOPE.get()`,
  `Account.build()`, por ejemplo `Fulano.prefix()`; `Gone.java` borrado.
```python
svc = Client.__init__(url=None)  # Python: Entity.get_connection(x)
```
""")
    os.makedirs(os.path.join(sample_copy, "tools", "mig"))
    for f in ("tools/Probe.java", "tools/mig/Migrate.java"):
        with open(os.path.join(sample_copy, f), "w") as fh:
            fh.write("class X {}\n")
    os.makedirs(os.path.join(sample_copy, "docs", "archive"))
    with open(os.path.join(sample_copy, "docs", "archive", "old.md"), "w") as fh:
        fh.write("`Entity#ancient`\n")
    svc = Service(sample_copy, cache_dir=str(tmp_path / "c"))
    out = svc.check_refs()          # the default scope: what agents read, not NOTES.md
    assert "check_refs: no file in scope\nscope: the default, files agents read (CLAUDE.md" \
        in out and "0 files; not checked: 1 other .md file of the checkout and 1 under " \
        "archive folders (archive/, old/…) (set [check_refs] files in .scipq.toml" in out, out
    with open(os.path.join(sample_copy, ".scipq.toml"), "w") as fh:
        fh.write('[check_refs]\nfiles = ["**/*.md"]\n')
    svc = Service(sample_copy, cache_dir=str(tmp_path / "c"))
    out = svc.check_refs()
    assert "10 problems: " in out and "2 members not found" in out, out
    assert "\nscope: [check_refs] files of .scipq.toml (**/*.md): 1 file; not checked: 1 " \
        "under archive folders (archive/, old/…) (name them in files)\n" in out, out
    assert "Service.NOPE.get()  — Service has no member NOPE" in out
    for x in ("M/Account", "X_main", "no type Old", "Probe", "Migrate", "DEFAULT", "Account.build", "Fulano", "borrado", "__init__", "get_connection", "ancient"):
        assert x not in out, x
    assert "no setState(String) in Entity: its overloads take (int)" in out
    assert "line 4 ×2 (also 13)  Entity.reload()  — Entity has no member reload" in out
    assert "no type Nope" in out and "com.example.model.Ghost  — no such type" in out
    assert "Entity.java:3  — line 3 no longer mentions setState (setState is at 14–16" in out
    assert "line 6 ×2 (also 18)  core/src/main/java/com/example/model/Entity.java:3" in out  # row
    assert "Service.java:2  — line 2 no longer mentions setState (called at" in out
    assert "Gone.java  — no such file" in out and "Account.java  —" not in out
    assert "Old" not in out and "Gone#run" not in out             # said to be gone
    for x in ("Foo", "getX", "LOG", "hashCode", "dnGetState", "Lizt", "X.java", "uid"):
        assert x not in out, x
    assert "com.example.Account  — no such type (Account is at com.example.model.Account)" \
        in out
    assert "no type Outer$Inner" in out
    md = svc.check_refs(fmt="md")
    assert "| file | line | mention | problem |" in md and "| NOTES.md | 4, 13 |" in md
    with open(os.path.join(sample_copy, "docs", "a.md"), "w") as fh:
        fh.write("`Entity#nothing`; next to it: `../tools/Probe.java`\n")
    with open(os.path.join(sample_copy, "docs", "b.txt"), "w") as fh:
        fh.write("`Entity#nothing`\n")
    only = svc.check_refs(files=["docs"])           # archive folders: only when named
    assert "in 1 files" in only and "Entity has no member nothing" in only
    assert "ancient" not in only and "scope: the files given (docs): 1 file; not checked: 1 " \
        "under archive folders (archive/, old/…) (name them in files)" in only, only
    assert "Entity has no member ancient" in svc.check_refs(files=["docs/archive"])
    assert "in 1 files" in svc.check_refs(files=["docs/*.md"])     # * stays in its folder
    assert "in 2 files" in svc.check_refs(files=["docs/**/*.md", "docs/archive/*.md"])
    with open(os.path.join(sample_copy, ".scipq.toml"), "w") as fh:
        fh.write('[check_refs]\nfiles = ["*.md", "docs"]\nexclude = ["docs/**"]\n')
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs()
    assert "NOTES.md" in out and "docs/a.md" not in out, out
    assert "scope: [check_refs] files of .scipq.toml (*.md, docs) minus exclude (docs/**): " \
        "1 file; 2 .md files excluded\n" in out, out
    with open(os.path.join(sample_copy, "docs", "README.md"), "w") as fh:
        fh.write("`Entity#readme`\n")
    os.makedirs(os.path.join(sample_copy, ".claude", "skills", "x"))
    with open(os.path.join(sample_copy, ".claude", "skills", "x", "SKILL.md"), "w") as fh:
        fh.write("`Entity#skill`\n")
    with open(os.path.join(sample_copy, "claude.md"), "w") as fh:      # names without case
        fh.write("`Entity#lower`\n")
    os.remove(os.path.join(sample_copy, ".scipq.toml"))
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs()
    assert "Entity has no member readme" in out and "Entity has no member skill" in out and \
        "Entity has no member lower" in out and "NOTES.md" not in out, out
    assert ": 3 files; not checked: 2 other .md files of the checkout and 1 under" in out, out


def test_impact_lists_the_entry_points_reached(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service
    os.makedirs(os.path.join(sample_copy, "app/src/main/webapp/WEB-INF"))
    with open(os.path.join(sample_copy, "app/src/main/webapp/WEB-INF/web.xml"), "w") as fh:
        fh.write("<web-app><servlet><servlet-name>s</servlet-name>"
                 "<servlet-class>com.example.app.Service</servlet-class></servlet></web-app>")
    with open(os.path.join(sample_copy, ".scipq.toml"), "w") as fh:
        fh.write('profiles = ["servlet"]\n\n[[entry_points]]\nkind = "job"\n'
                 'type_names = ["*Support"]\n')
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).impact("Entity#setState")
    assert "entry points reached (by kind): job 1 type, servlet 1 type (2 members)" in out
    assert "servlet  d1  Service  app/src/main/java/com/example/app/Service.java  servlet in " \
        "app/src/main/webapp/WEB-INF/web.xml  — activate(), activateAll()" in out
    assert "job  d1  TestSupport  " in out and "named like *Support  — reset()" in out


def test_entry_points_routes_supertypes_depth_and_the_symbol_itself(refactor_root, tmp_path):
    import os
    from scipq_mcp.service import Service
    with open(os.path.join(refactor_root, ".scipq.toml"), "w") as fh:
        fh.write('profiles = ["jaxrs", "jdo"]\n\n[[entry_points]]\nkind = "screen"\n'
                 'supertypes = ["com.example.shop.Widget"]\n\n[[entry_points]]\n'
                 'kind = "alive"\nreached = false\ntype_names = ["Steps"]\n')
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.impact("Steps#base", depth=3)
    # The endpoint is five calls away and the screen three: searched deeper than the rows.
    assert "entry points reached (by kind, searched to depth 6; the rows stop at 3): " \
        "endpoint 1 type, screen 1 type" in out
    assert "endpoint  d5  OrderResource  " in out and "— create() POST /orders" in out
    assert "screen  d3  MainScreen  " in out and "extends BaseScreen (a Widget)  — show()" in out
    assert "render" not in out.split("shop:")[0]          # private: reached through show()
    assert "helper" not in out.split("shop:")[0]          # in the resource, but not a route
    assert "alive" not in out                             # reached = false: dead_code only
    assert "  d4  " not in out and "  d5  Steps#first()" not in out    # rows stop at 3
    out = svc.impact("OrderDao#openFilter")
    assert "find() GET /orders/{id}" in out
    out = svc.impact("OrderResource#create")
    assert out.splitlines()[1].startswith(
        "OrderResource#create() is itself an entry point: endpoint POST /orders")


def test_persistence_model(refactor_root, tmp_path):
    from scipq_mcp.service import Service
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.persistence()
    assert "4 entities" in out and "Order  ORDERS  3 fields, 1 relations" in out
    order = svc.persistence("Order")
    assert "table: ORDERS" in order and 'column = "CLASS"' in order
    assert "state  int  column STATE (from the field name)" in order
    assert "total  long  column TOTAL_AMOUNT (@Column)" in order
    assert "lines  List<Line>  no column here: key ORDER in LINES (Line#order)  → many Line " \
        "(mappedBy order)" in order
    assert "not persistent: note (@NotPersistent)" in order
    assert "fetch group withLines: lines, state" in order
    state = svc.persistence("Order#state")
    assert "table ORDERS, column STATE (from the field name)" in state and \
        "in fetch groups: withLines" in state
    assert "Order#setState(int)" in state and "a.setState(b.getState())" in state   # Lombok's
    assert 'return "state == :p";' in state                     # by name: a JDOQL filter
    line = svc.persistence("Line#order")
    assert "relation to Order (other side: Order#lines, mappedBy)" in line
    assert "is not persistent" in svc.persistence("Counter")


def test_persistence_joins_versions_and_interfaces(refactor_root, tmp_path):
    from scipq_mcp.service import Service
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    c = svc.persistence("Customer")
    assert "version: VERSION_NUMBER, column VERSION, field version" in c
    assert "code  String  column CODE (@Column)  [length=20 allowsNull=false]" in c
    assert "nickname  String  column NICKNAME (from the field name)  [@Convert(Upper)]" in c
    assert "version  long  column VERSION (@Version of the class)  [version]" in c
    assert "history  List<Order>  join table CUSTOMER_ORDERS (CUSTOMER_ID → this, ORDER_ID → " \
        "the element)" in c
    assert "payments  List<Payment>  no column here: key PAYER in PAYMENTS (Payment#payer)" in c
    assert "@Unique(code) CUSTOMER_CODE" in c
    p = svc.persistence("Payment")
    assert "payer  Buyer  column PAYER (from the field name)  → Customer (implements Buyer) " \
        "(other side: Customer#payments, mappedBy)" in p


def test_persistence_uses_by_name_are_ranked(refactor_root, tmp_path):
    from scipq_mcp.service import Service
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.persistence("Customer#code")
    body = out[out.index("by its name"):]
    order = ["@Form(fields", '"payer.code', '"customer.code', "| code | nickname |",
             'bind("code")', "| code | price |", "1 map or parameter key", "1 path through"]
    at = [body.index(x) for x in order]
    assert at == sorted(at), body
    assert "9, most likely" in body
    page = svc.persistence("Customer#code", limit=2)
    assert "next offset 2" in page and '"payer.code' not in page


def test_super_calls_of_other_overrides_do_not_dispatch_here(refactor_root, tmp_path):
    # Circle#draw() calls super.draw() (Shape#draw): bound to Shape, never to Square.
    from scipq_mcp.service import Service
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.impact("Shapes#Square#draw", depth=1)
    assert "paint" in out and "Circle" not in out
    out = svc.callers("Shapes#Square#draw", include_overridden=True)
    assert "paint" in out and "Circle" not in out


def test_impact_dispatches_through_template_methods(refactor_root, tmp_path):
    # SalesPage#body() overrides Page#body(), which Page#render() calls on this; StockPage's
    # super.render() runs StockPage's own body, so it is not reached.
    import os
    from scipq_mcp.service import Service
    with open(os.path.join(refactor_root, ".scipq.toml"), "w") as fh:
        fh.write('[[entry_points]]\nkind = "page"\ntype_names = ["*Page"]\n')
    out = Service(refactor_root, cache_dir=str(tmp_path / "c")).impact("Pages#sales", depth=4)
    assert "d1  SalesPage#body()" in out and "d2  Page#render()" in out
    assert "d3  Pages#show(Page)" in out and "d3  Page#frame()" in out
    assert "StockPage" not in out           # its super.render()/frame(), refresh(): its own
    assert "page  d1  SalesPage" in out and "page  d2  Page " not in out    # abstract
    # Page#render() overrides View#render(): View#measure() runs it on the same object, and
    # StockPage#refreshAll()'s measure() runs on a StockPage (bound to SalesPage, not Page).
    deep = Service(refactor_root, cache_dir=str(tmp_path / "c")).impact("Pages#sales", depth=5)
    assert "View#measure()" in deep and "StockPage" not in deep
    # measure() on a View may be a SalesPage's; on a StockPage it is not.
    assert "Gallery#show(View)" in deep and "Dash#show()" not in deep
    assert "callers of methods these override were left out" in deep
    # From the override itself: the base's code runs on it, the sibling's does not.
    out = Service(refactor_root, cache_dir=str(tmp_path / "c")).impact("SalesPage#body",
                                                                       depth=4)
    assert "Page#render()" in out and "Pages#show(Page)" in out and "StockPage" not in out


def test_impact_lifecycle_creators_layouts_and_startup_lambdas(refactor_root, tmp_path):
    import os
    from scipq_mcp.service import Service
    with open(os.path.join(refactor_root, ".scipq.toml"), "w") as fh:
        fh.write('profiles = ["vaadin", "spring"]\n')
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.impact("Pages#badge")
    # onAttach() runs on a Badge someone created: its creators are reached.
    assert "d1  Badge#onAttach()" in out and "d2  OrdersView#<init>()" in out
    assert 'screen  d2  OrdersView  ' in out and '@Route("orders")' in out
    assert "screen  d2  AppShell  " in out and "named in @Route(layout)" in out
    # onAttach() inherited from BaseBadge, reached for a CountBadge: CountBadge's creators.
    out = svc.impact("Pages#count")
    assert "BaseBadge#onAttach()" in out and "OrdersView#<init>()" in out
    out = svc.impact("Pages#lazy")
    row = out.split("Startup.java")[1].split("\n")[0]
    assert "startup  d1  Startup  " in out and "warm()" in row
    assert "register()" not in row and "factory()" not in row   # a lambda, an anonymous class
    assert "Startup#register()  shop/src/main/java/com/example/shop/Startup.java:13  → " \
        "Pages#lazy()  (from a lambda or anonymous class)" in out


def test_init_scripts_that_filter_by_name_prefix(tmp_path, monkeypatch):
    from scipq_mcp import setup as SETUP
    init = tmp_path / "gh" / "init.d"
    init.mkdir(parents=True)
    (init / "a.init.gradle").write_text(
        'allprojects { if (!rootProject.projectDir.name.startsWith("shop")) return\n'
        '  // -Xplugin:scip\n}\n')
    (init / "b.init.gradle").write_text('if (root.name != "other") return // scip-javac\n')
    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "gh"))
    got = dict(SETUP._init_scripts("shop-web"))
    assert got[str(init / "a.init.gradle")] == "yes"
    assert got[str(init / "b.init.gradle")].startswith("NO: only for root project 'other'")
    # projectDir.name is the directory's name, not the settings' project name.
    (init / "a.init.gradle").write_text(
        'if (!root.projectDir.name.startsWith("shop")) return // scip-javac\n')
    got = dict(SETUP._init_scripts("store-app", "shop-web"))
    assert got[str(init / "a.init.gradle")] == "yes"
    got = dict(SETUP._init_scripts("shop-app", "store-web"))
    assert got[str(init / "a.init.gradle")] == "NO: only for directories named shop*"
    # A task's name is not the project's; == with return leaves that project out.
    (init / "a.init.gradle").write_text(
        'tasks.withType(JavaCompile) { if (it.name == "compileJava") { } } // scip-javac\n')
    assert dict(SETUP._init_scripts("shop"))[str(init / "a.init.gradle")].startswith("unknown")
    (init / "a.init.gradle").write_text('if (rootProject.name == "legacy") return // scip-javac\n')
    assert dict(SETUP._init_scripts("shop"))[str(init / "a.init.gradle")] == "yes"
    assert dict(SETUP._init_scripts("legacy"))[str(init / "a.init.gradle")].startswith("NO")


def test_check_refs_library_types_only_in_old_versions(tmp_path):
    import sqlite3
    from scipq_mcp.checkrefs import Model
    con = sqlite3.connect(tmp_path / "libraries.sqlite")
    con.executescript("""
        CREATE TABLE jars (id INTEGER PRIMARY KEY, path TEXT, mtime_ns INTEGER, size INTEGER,
                           coordinates TEXT);
        CREATE TABLE classes (name TEXT, jar_id INTEGER, entry TEXT);
        INSERT INTO jars VALUES (1, 'a', 0, 0, 'org.ui:ui-data:2.10.5'),
                                (2, 'b', 0, 0, 'org.ui:ui-data:25.1.5');
        INSERT INTO classes VALUES ('org/ui/data/OldRenderer', 1, ''),
                                   ('org/ui/data/Grid', 1, ''), ('org/ui/data/Grid', 2, '');
    """)
    con.commit()
    con.close()
    m = Model.__new__(Model)
    m.external_names, m._stale, m._cache_base = set(), {}, str(tmp_path)
    assert m.stale_library("OldRenderer") == "only in ui-data 2.10.5; 25.1.5 has none"
    assert m.stale_library("Grid") == ""


def _git(root, *args, date=None):
    import os
    import subprocess
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, env=env)


def test_check_refs_follows_lines_through_git_history(sample_copy, tmp_path):
    # The doc was written against an older Entity.java: without the class javadoc (3 lines
    # fewer) and with a reset() that a later commit removed; a file it names was deleted.
    import os
    import shutil
    import pytest
    from scipq_mcp.service import Service
    if shutil.which("git") is None:
        pytest.skip("no git")
    model = os.path.join(sample_copy, "core/src/main/java/com/example/model")
    entity = os.path.join(model, "Entity.java")
    with open(entity) as fh:
        now = fh.read()
    lines = now.split("\n")
    old = lines[:2] + lines[5:-2] + ["", "    public void reset() {", "        setState(0);",
                                       "    }"] + lines[-2:]
    old = [x.replace("return state;", "return this.state;") for x in old]   # edited later
    with open(entity, "w") as fh:
        fh.write("\n".join(old))
    with open(os.path.join(model, "Legacy.java"), "w") as fh:
        fh.write("package com.example.model;\nclass Legacy {}\n")
    e = "core/src/main/java/com/example/model/Entity.java"
    with open(os.path.join(sample_copy, "NOTES.md"), "w") as fh:
        fh.write(f"- `getState` ({e}:15) reads it.\n"
                 f"- `setState(0)` ({e}:25) resets it.\n"
                 f"- The old helper core/src/main/java/com/example/"
                 f"model/Legacy.java; a planned core/src/main/java/com/example/model/Planned.java\n"
                 f"- `Entity#reset()` was there; `Entity#rewind()` is planned.\n"
                 f"- `findById` ({e}:19-25) and, further, `Entity.reset`.\n"
                 f"- `getState` ({e}:15-17) returns it.\n"
                 f"- The class starts at {e}:4, and somewhere below it the `findById(1)` helper.\n"
                 f"| `{e}:12` | `this.state = state` |\n"
                 f"- Never there: core/src/main/java/com/example/other/Legacy.java\n")
    _git(sample_copy, "init", "-q")
    _git(sample_copy, "config", "diff.mnemonicPrefix", "true")     # c/ w/ instead of a/ b/
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-qm", "a", date="2026-01-02T10:00:00Z")
    with open(entity, "w") as fh:
        fh.write(now)
    os.remove(os.path.join(model, "Legacy.java"))
    os.utime(entity, (1_700_000_000, 1_700_000_000))
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-qm", "b", date="2026-01-05T10:00:00Z")
    with open(os.path.join(sample_copy, "NOTES.md"), "a") as fh:
        fh.write(f"- Not committed: `getState` ({e}:12).\n")
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs(files=["NOTES.md"])
    assert f"{e}:15  — line 15 is now 18 (moved since the doc's version of 2026-01-02)" in out, out
    assert f"{e}:25  — line 25 changed since the doc named setState (2026-01-02, " in out
    assert "model/Legacy.java  — no such file (deleted 2026-01-05, " in out
    assert "other/Legacy.java  — no such file, nor deleted since" in out
    assert "Planned.java  — no such file, nor deleted since the doc was written" in out
    assert "Entity has no member reset (reset is in TestSupport; gone from Entity.java since " \
        "2026-01-05, " in out
    assert "Entity has no member rewind (not there when the doc was written either: planned?)" \
        in out
    assert "lines 19-25 changed since the doc named findById (2026-01-02, " in out
    assert "Entity.reset  — Entity has no member reset" in out          # without parentheses
    assert "line 12 no longer mentions getState" in out          # an uncommitted doc line
    assert "lines 15-17 changed since the doc named getState (2026-01-02, " in out
    assert f"{e}:4  — line 4 is now 7 (moved since the doc's version of 2026-01-02)" in out
    assert f"{e}:12  — line 12 is now 15 (the code quoted; moved since the doc's version of " \
        "2026-01-02)" in out
    assert "3 line numbers that only moved" in out and "3 lines changed since" in out


def test_check_refs_reads_line_lists_getters_and_neighbouring_numbers(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service
    e = "core/src/main/java/com/example/model/Entity.java"
    with open(os.path.join(sample_copy, "NOTES.md"), "w") as fh:
        fh.write(f"- `{e}:3,14` — `setState(int)`; `{e}:3 + 14-16` — `setState(int)`.\n"
                 f"- `getState()` ({e}:7) is the field's getter.\n"
                 f"- `{e}:22` returns null, while `:15` is the body of `setState(…)`.\n"
                 f"- `{e}:23` returns null, and `setState()` (`:15`) writes it.\n"
                 f"- `main abc1234 {e}:3 — setState(int)` was elsewhere then.\n"
                 f"- `{e}:3` — `setState(int)` is not there.\n"
                 f"- Reports: `TEST-com.example.model.Run<N>Test.xml`, `com.example.model.Ent*`.\n"
                 f"- {e}:22-23: returns null, then `getState()` (:19) reads it.\n"
                 f"- `{e}:23` (main) called `setState(int)` there.\n"
                 f"- `Entity` is abstract: `{e}:22`.\n"
                 f"| `findById(1)` | `{e}:22` | `setState(int)` elsewhere |\n"
                 f"| a remark | `{e}:22` | the same as `setState(int)` |\n"
                 f"- v4 `{e}:3` — `setState(int)` there; `Entity.nothing` and `Entity.state`.\n"
                 f"- Calls `Entity.getState()`\n(`{e}:4`).\n"
                 f"- `setState(1)` runs and `add` ({e}:23) adds.\n"
                 f"- Elsewhere: (v4 `Entity.rewind()`, en main `Entity#wind`); the main `Entity#run`.\n"
                 f"- A field of the type: `com.example.model.Ghost.total`.\n"
                 f"- {e}:19 — the only caller of `findById`.\n"
                 f"- `getState()` reads and `findById(1)` finds\n  (`{e}:2` and `:10`).\n"
                 f"- `{e}:14` (`setState`), `:3` (`findById`).\n"
                 f"- En main el alta (`{e}:3` `setState(int)`) era otra.\n"
                 f"- `setState(int)` and `getState()` ({e}:15/:18).\n"
                 f"- `getState()` + `findById(1)` ({e}:18-19) read it.\n"
                 f"- `{e}:3` returns, `getState()` vacío (`:19`).\n"
                 f"- Files: v4 `{e}:3` (setState), `{e}:4` — `setState(int)`; v5 `{e}:24` — "
                 f"`setState(int)`.\n"
                 f"- En v4 el alta\n  usa `{e}:25` — `setState(int)`.\n")
    with open(os.path.join(sample_copy, ".scipq.toml"), "w") as fh:
        fh.write('[check_refs]\nother_versions = ["v4", ""]\nfiles = "NOTES.md"\n')
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs()
    assert "11 problems: 9 lines no longer in the symbol named, 1 members not found, 1 types " \
        "not found" in out, out
    assert "line 19 no longer mentions findById" in out          # a member alone in backticks
    # respectively, over a wrapped line: getState at 2 (no), findById at 10 (no)
    assert f"{e}:2  — line 2 no longer mentions getState" in out
    assert f"{e}:10  — line 10 no longer mentions findById" in out
    assert f"{e}:3  — line 3 no longer mentions findById" in out      # a `:N` after a path
    assert "com.example.model.Ghost  — no such type" in out
    assert "line 18 no longer mentions findById" in out          # each name of a list
    assert "Entity#run  — Entity has no member run" in out and "wind" not in out
    assert "line 24 no longer mentions setState" in out and "line 4 no longer mentions setState" \
        not in out
    assert "line 6  " in out and "line 3 no longer mentions setState" in out
    assert "line 4 no longer mentions getState" in out                 # the line before
    assert "line 23 no longer mentions setState" in out                # `add` names nothing here

def test_check_refs_continuations_bind_to_their_file(sample_copy, tmp_path):
    # The doc was written against an Entity.java without its class javadoc (3 lines fewer:
    # setState at 11, getState at 15, findById at 19); a `:N` alone is the path's only when
    # nothing says otherwise, and git must confirm it when it is far from the path.
    import os
    import shutil
    import pytest
    from scipq_mcp.service import Service
    if shutil.which("git") is None:
        pytest.skip("no git")
    entity = os.path.join(sample_copy, "core/src/main/java/com/example/model/Entity.java")
    with open(entity) as fh:
        now = fh.read()
    lines = now.split("\n")
    with open(entity, "w") as fh:
        fh.write("\n".join(lines[:2] + lines[5:]))
    e = "core/src/main/java/com/example/model/Entity.java"
    with open(os.path.join(sample_copy, "NOTES.md"), "w") as fh:
        fh.write(f"- `getState()` (`{e}:15`); `Session.find` :4 calls the finder.\n"  # 1
                 f"- `{e}:11` sets it, via `Session.find(1)` luego, and `findById` (:19).\n"
                 f"- `getState()` (`{e}:15`) reads it. Later `get` (:19) too.\n"      # 3
                 f"- (main `{e}:11` `setState`, :15 `getState`) was other code.\n"
                 f"- (`{e}:11`, :15 getState) and then `findById` :19 elsewhere.\n"    # 5
                 f"- `getState()` (`core/src/main/java/com/example/\n"
                 f"  model/Entity.java:15`) reads it.\n"
                 f"- Checked in `/home/someone/other-worktree/{e}:15` (`getState`).\n"   # 8
                 f"- `git show abc1234:{e}:15` (`getState`), :19 `findById`.\n"
                 f"- Then `Entity.findById` (:19) returns it.\n"                       # 10
                 f"- It casts the id to a Session (:19), see `findById` in `{e}:11`.\n"
                 f"- (`{e}:11` y 30) and later `findById` (:19).\n")                   # 12
    _git(sample_copy, "init", "-q")
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-qm", "a", date="2026-01-02T10:00:00Z")
    with open(entity, "w") as fh:
        fh.write(now)
    os.utime(entity, (1_700_000_000, 1_700_000_000))
    _git(sample_copy, "commit", "-qam", "b", date="2026-01-05T10:00:00Z")
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs(files=["NOTES.md"])
    # 1: Session's line 4 is fine; 3: far from the path, and only `get`; 5: past its
    # parenthesis; 6: a path cut at a "/" over two lines
    # 8: an absolute path to another checkout of the repository: shown as the file it is
    assert f"line 1 ×5 (also 3, 5, 6 …)  {e}:15  — line 15 is now 18" in out, out
    assert "/home/someone" not in out
    assert "line 9  " not in out                                      # another version's
    assert "line 10  Entity.java:19  — line 19 is now 22" in out      # T.m (:N), no path
    assert "line 12  " not in out                                     # past its own list
    assert f"line 2  {e}:19  — line 19 is now 22" in out             # a call described, not X's
    assert ":4" not in out and "line 4  " not in out                   # line 4: main's
    assert out.count(":19  —") == 2


def test_check_refs_history_follows_doc_renames_and_the_parent_version(sample_copy, tmp_path):
    # The doc is committed together with the code change it was written before (its line
    # numbers are the parent's), then moved to another folder.
    import os
    import shutil
    import pytest
    from scipq_mcp.service import Service
    if shutil.which("git") is None:
        pytest.skip("no git")
    model = os.path.join(sample_copy, "core/src/main/java/com/example/model")
    entity = os.path.join(model, "Entity.java")
    with open(entity) as fh:
        now = fh.read()
    lines = now.split("\n")
    with open(entity, "w") as fh:
        fh.write("\n".join(lines[:2] + lines[5:]))
    _git(sample_copy, "init", "-q")
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-qm", "a", date="2025-11-02T10:00:00Z")    # months before
    with open(entity, "w") as fh:
        fh.write(now)
    os.utime(entity, (1_700_000_000, 1_700_000_000))
    e = "core/src/main/java/com/example/model/Entity.java"
    s = "app/src/main/java/com/example/app/Service.java"
    os.makedirs(os.path.join(sample_copy, "notes"))
    with open(os.path.join(sample_copy, "notes", "N.md"), "w") as fh:
        fh.write(f"- `getState()` ({e}:15) reads it.\n"
                 f"- `uid()` ({e}:10) was never there.\n"
                 f"- The javadoc is at {e}:4, and further on the class has `uid()`.\n")
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-qm", "b", date="2026-01-03T10:00:00Z")
    os.makedirs(os.path.join(sample_copy, "docs"))
    _git(sample_copy, "mv", "notes/N.md", "docs/N.md")
    _git(sample_copy, "commit", "-qm", "c", date="2026-03-01T10:00:00Z")
    with open(os.path.join(sample_copy, "docs", "N.md"), "a") as fh:
        fh.write(f"| `{s}:20` | `e.setState(4)` |\n")        # not committed: no claim
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs(files=["docs/"])
    assert f"{e}:15  — line 15 is now 18 (moved since the doc's version of 2025-11-02)" in out, out
    assert "line 10 no longer mentions uid" in out and "so in the doc's version too: " \
        "2026-01-03" in out
    assert f"{e}:4 " not in out         # a name further away: no claim that the doc was wrong
    assert f"{s}:20" not in out


def test_check_refs_default_scope_is_what_agents_read(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service

    def put(rel, text):
        os.makedirs(os.path.dirname(os.path.join(sample_copy, rel)) or sample_copy, exist_ok=True)
        with open(os.path.join(sample_copy, rel), "w") as fh:
            fh.write(text)
    put("AGENTS.md", "Read [the ADR](doc/adr.md) and @doc/steps.md first; not doc/other.md.\n"
                     "- `Entity#a1`\n")
    os.symlink("AGENTS.md", os.path.join(sample_copy, "CLAUDE.md"))   # one file, read once
    put("doc/adr.md", "- `Entity#a2`\n")
    put("doc/steps.md", "- `Entity#a3`\n")
    put("doc/logs/x.md", "- `Entity#a4`\n")
    put("tools/skills/fmt/SKILL.md", "- `Entity#a5`\n")
    put("tools/skills/fmt/references/r.md", "- `Entity#a6`\n")
    put("app/src/main/webapp/lib/LICENSE", "MIT\n")
    put("app/src/main/webapp/lib/README.md", "- `Entity#a7`\n")       # someone else's
    put("sdk/LICENSE", "MIT\n")
    put("sdk/README.md", "- `Entity#a8`\n")                           # our own package
    put("app/src/main/webapp/lib/old/README.md", "- `Entity#a9`\n")   # theirs, and archived
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs()
    for x in ("a1", "a8"):
        assert f"Entity has no member {x}" in out, (x, out)
    for x in ("a2", "a3", "a4", "a5", "a6", "a7"):     # named, skills, logs: only when asked
        assert f"member {x}" not in out, x
    assert out.count("Entity has no member a1") == 1
    assert "scope: the default, files agents read (CLAUDE.md, AGENTS.md, .claude/**, " \
        ".github/**, README*.md): 2 files; not checked: 5 other .md files of the checkout" \
        in out and "; 2 files of third-party code" in out and "archive folders" not in out, out


def test_check_refs_since_a_commit(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service

    def put(rel, text):
        os.makedirs(os.path.dirname(os.path.join(sample_copy, rel)) or sample_copy, exist_ok=True)
        with open(os.path.join(sample_copy, rel), "w") as fh:
            fh.write(text)
    put("CLAUDE.md", "- `Entity#nothing1`; and `Service#nothing2`\n")    # cites Entity
    put(".github/x.md", "- Entity, in prose\n- `Service#nothing3`; `Registry#nothing7`\n")
    put(".claude/s.md", "- `com.example.model.Registry#nothing6`\n")    # Registry, qualified
    put("AGENTS.md", "- `Service#old1`\n- fine\n")
    put("NOTES.md", "- `Entity#nothing5`\n")                           # out of scope
    put("tools/other/Registry.java", "class Registry {}\n")            # a second Registry
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs(since="HEAD")
    assert "check_refs: since needs a git checkout" in out, out
    _git(sample_copy, "init", "-q")
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-qm", "a", date="2026-01-02T10:00:00Z")
    svc = Service(sample_copy, cache_dir=str(tmp_path / "c"))
    nope = svc.check_refs(since="nope")
    assert nope.startswith("check_refs: since: 'nope' is not a commit here\nscope: the "
                           "default, files agents read (") and "): 4 files" in nope, nope
    out = svc.check_refs(since="HEAD")
    assert "check_refs: nothing changed since then in scope or mentions the changed code" \
        in out and "0 .java files changed" in out, out
    model = os.path.join(sample_copy, "core/src/main/java/com/example/model")
    for f in ("Entity.java", "Registry.java"):
        with open(os.path.join(model, f), "a") as fh:
            fh.write("// touched\n")
    put("AGENTS.md", "- `Service#old1`\n- `Service#nothing4`\n")      # line 2 edited
    out = svc.check_refs(since="HEAD")
    assert "Entity has no member nothing1" in out and "nothing4" in out, out
    assert "nothing6" in out                                  # written with its package
    for x in ("nothing2", "nothing3", "nothing5", "nothing7", "old1"):
        assert x not in out, x
    assert "since HEAD (merge base " in out and "): 2 .java files changed; read 1 doc of the " \
        "scope changed since then (the lines edited, and mentions of the changed code) and " \
        "2 docs that mention the changed code (only those mentions): .claude/s.md, CLAUDE.md" \
        in out, out
    assert "1 older problem of the changed docs, on lines not edited since then, not shown" in out
    assert "1 .md file out of scope mentions the changed code: NOTES.md" in out
    assert "check_refs: 5 mentions in 3 files;" in out
    assert "scope: the default, files agents read" in out and ": 4 files; not checked: 1 " \
        "other .md file of the checkout (set" in out
    out = svc.check_refs(files=["NOTES.md", ".github"], since="HEAD")
    assert "nothing5" in out and "nothing3" not in out, out


def test_declarations_and_blocks_in_older_text():
    from scipq_mcp.checkrefs import _at_declaration, _block_at
    old = ["class A {", "    /** Doc. */", "    void run() {", "        go();", "    }",
           "    void go() {}", "}"]
    assert _at_declaration(old, 2, 5, ("run",)) and _at_declaration(old, 4, 4, ("run",))
    assert not _at_declaration(old, 7, 7, ("run",))
    cur = ["class A {", "    void go() {}", "", "    /** Doc. */", "    void run() {",
           "        go();", "    }", "}"]
    assert _block_at(old, cur, 3, 5, 3) == (5, 7)
    assert _block_at(old, cur, 7, 7, 7) is None              # a lone brace says nothing



def test_line_maps_through_diff_hunks():
    from scipq_mcp.checkrefs import _edited_to, _hunks, _map_line
    diff = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n"
            "@@ -2,0 +3,2 @@\n+x\n+y\n@@ -10,2 +12 @@\n-a\n-b\n+c\n@@ -20,2 +20,0 @@\n-d\n-e\n"
            "diff --git a/B.java b/B.java\nnew file mode 100644\n")
    h = _hunks(diff)
    assert h["B.java"] is None
    assert [_map_line(h["A.java"], n) for n in (1, 2, 3, 9, 10, 11, 12, 19, 20, 22)] == \
        [1, 2, 5, 11, None, None, 13, 20, None, 21]
    assert _edited_to(h["A.java"], 10, 10) == (12, 12)


def test_path_goes_through_dispatch(refactor_root, tmp_path):
    # Gallery#show() calls View#measure() → render(), which SalesPage's chain overrides.
    from scipq_mcp.service import Service
    out = Service(refactor_root, cache_dir=str(tmp_path / "c")).path("Gallery#show",
                                                                     "Pages#sales")
    assert "Gallery#show(View)" in out and "SalesPage#body()" in out and "Page#body()" in out


def test_impact_names_library_callbacks_and_deferred_calls(refactor_root, tmp_path):
    # Ticker#run() overrides Runnable#run(): the JDK calls it, so its callers are whoever
    # creates a Ticker; Panel calls refresh() from an anonymous Runnable.
    from scipq_mcp.service import Service
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.impact("Ticker#tick")
    assert "Ticker#run() overrides Runnable#run(): Ticker created at 1 place: " \
        "shop/src/main/java/com/example/shop/Clock.java:5" in out, out
    out = svc.impact("Panel#refresh")
    assert "called from inside an anonymous class or a lambda" in out and \
        "Panel#setup()  shop/src/main/java/com/example/shop/Panel.java:15" in out, out
    assert "→ Panel#refresh()  (from a lambda or anonymous class)" in out


def test_calls_inside_lambdas_are_told_apart():
    from scipq_mcp.graph import _in_lambda
    from scipq_mcp.javasrc import mask

    def inside(code, name="go(", k=0):
        at = -1
        for _ in range(k + 1):
            at = code.index(name, at + 1)
        return _in_lambda(mask(code), at)
    head = "class A {\n  void m() {\n"
    yes = ["b.addListener(e -> go(target));",
           "b.addListener(e -> list.forEach(x -> go(x)));",          # run in place, but later
           "b.addListener(e -> {\n    load();\n    go();\n});",
           "Runnable r = () -> { reload.run(); go(); };",
           "b.add((Listener) ev -> {\n  d.open((Confirm.Listener) c -> {\n"
           "    if (c.ok()) {\n      go();\n    }\n  });\n});",
           "Supplier<X> s = flag ? () -> go() : () -> other();",
           "x -> { for (int i = 0; i < 3; i++) { go(); } }"]
    no = ["go();",
          "opt.ifPresent(x -> step(x, go(y)));",                   # run in place
          "list.stream().map(x -> go(x)).forEach(System.out::println);",
          "list.forEach(x -> log(x));\n    go();",
          "foo(x -> a(x), go());",
          "switch (k) {\n  case A -> go();\n  default -> stop();\n}",
          "switch (k) {\n  case A -> { go(); }\n}",
          'log("a -> b"); go();',
          "new Runnable() {\n  public void run() {\n    go();\n  }\n};",
          "if (ready) {\n  go();\n}"]
    for code in yes:
        assert inside(head + code + "\n  }\n}"), code
    for code in no:
        assert not inside(head + code + "\n  }\n}"), code
    assert inside(head + "b.add(this::go);\n  }\n}", "go)")       # a method reference kept
    assert not inside(head + "list.forEach(this::go);\n  }\n}", "go)")


def test_project_helpers_that_run_the_lambda_they_are_given():
    # A helper that opens a transaction and calls the lambda runs it in place (the call is
    # direct); one that keeps it, hands it to another thread or stores it does not.
    from scipq_mcp.graph import _runs_its_argument
    from scipq_mcp.javasrc import mask
    runs = ["static void inTx(Consumer<Pm> work) {\n  Pm pm = open();\n  try { work.accept(pm); }"
            " finally { pm.close(); }\n}",
            "<T> T get(Function<Pm, T> f) {\n  return f.apply(open());\n}",
            "<T> T get(String name, Function<Pm, T> f) {\n  if (cur != null) return f.apply(cur);"
            "\n  return f.apply(open(name));\n}"]
    keeps = ["Task(Runnable r) {\n  this.r = r;\n}",
             "void later(Runnable r) {\n  executor.submit(() -> r.run());\n}",
             "void pass(Runnable r) {\n  other(r);\n}",
             "Runnable wrap(Runnable r) {\n  return r;\n}"]
    for body in runs:
        assert _runs_its_argument(mask(body)), body
    for body in keeps:
        assert not _runs_its_argument(mask(body)), body
    # handed as it is to an overload: as that one does, for that argument
    one = "<T> T get(Function<Pm, T> f) {\n  return get(DEFAULT, f);\n}"
    assert _runs_its_argument(mask(one), lambda k, i: i == 1)
    assert not _runs_its_argument(mask(one), lambda k, i: False)
    # only the parameter that gets the lambda counts; annotations before the method
    two = "void later(Runnable task, Duration d) {\n  queue.add(task);\n  log(d.toMillis());\n}"
    assert not _runs_its_argument(mask(two), arg=0)
    tx = "@Transactional(readOnly = true)\nvoid inTx(Runnable r) {\n  r.run();\n}"
    assert _runs_its_argument(mask(tx), arg=0)
    kept = "void later(Runnable r) {\n  submit(() -> helper(r));\n}"
    assert not _runs_its_argument(mask(kept), lambda k, i: True, arg=0)


def test_repeated_lines_are_placed_only_in_their_method():
    from scipq_mcp.checkrefs import _block_at
    old = ["class A {", "  void one() {", "    try (Pm pm = open()) {", "      a();", "    }",
           "  }", "  void two() {", "    try (Pm pm = open()) {", "      b();", "    }", "  }",
           "}"]
    # one() lost its try; two() moved down: line 3 (one's) is nowhere now
    cur = ["class A {", "  int f;", "  void one() {", "    a();", "  }", "  void two() {",
           "    try (Pm pm = open()) {", "      b();", "    }", "  }", "}"]
    assert _block_at(old, cur, 3, 3, 3) is None
    assert _block_at(old, cur, 8, 8, 8) == (7, 7)           # two()'s, in two()
    assert _block_at(old, cur, 4, 4, 4) == (4, 4)           # a unique line: by its text
    # several copies in one method: by their order, when as many then as now
    two = ["class A {", "  void m() {", "    x = 1;", "    y();", "    x = 1;", "  }", "}"]
    two_now = ["class A {", "  int f;", "  void m() {", "    x = 1;", "    y();", "    x = 1;",
               "  }", "}"]
    assert _block_at(two, two_now, 5, 5, 5) == (6, 6)
    assert _block_at(two, ["class A {", "  void m() {", "    x = 1;", "  }", "}"], 5, 5, 5) is None


def test_lines_no_longer_in_their_method_say_where_it_went():
    from scipq_mcp.checkrefs import _in_its_method
    old = ["class A {", "  void save() {", "    pm.makePersistent(x);", "  }", "  void other() {",
           "  }", "}"]
    moved = ["class A {", "  void save() {", "    store(x);", "  }", "  void store(X x) {",
             "    pm.makePersistent(x);", "  }", "}"]
    assert _in_its_method(old, moved, 3, 3, "makePersistent") == \
        ": the same code is now at 6, in store, which save calls at 3"
    edited = ["class A {", "  void save() {", "    helper(x);", "  }", "  void helper(X x) {",
              "    pm.makePersistent(y);", "  }", "}"]
    assert _in_its_method(old, edited, 3, 3, "makePersistent") == \
        ": save no longer names makePersistent; helper, which it calls at 3, does at 6"
    gone = ["class A {", "  void other() {", "  }", "}"]
    assert _in_its_method(old, gone, 3, 3, "makePersistent") == \
        ": save, which held them, is no longer in the file"


def test_family_of_an_implicit_member_has_no_library_note(sample_root, tmp_path):
    svc = Service(sample_root, cache_dir=str(tmp_path / "c"))
    out = svc.family("Point#x()")          # a record's implicit accessor
    assert "Point#x() — family of 1 methods" in out and "sources were not found" not in out


def test_doctor_build_cache_with_shards_as_outputs(sample_copy, tmp_path, monkeypatch):
    import os
    from scipq_mcp.service import Service
    with open(os.path.join(sample_copy, "gradle.properties"), "w") as fh:
        fh.write("org.gradle.caching=true\n")
    with open(os.path.join(sample_copy, "settings.gradle"), "w") as fh:
        fh.write("rootProject.name = 'sample'\n")
    home = tmp_path / "gradle-home"
    (home / "init.d").mkdir(parents=True)
    script = home / "init.d" / "scip.init.gradle"
    body = ('gradle.rootProject { root ->\n    if (root.name != "sample") return\n'
            '    // scip-javac\n    root.allprojects { p -> p.tasks.withType(JavaCompile)'
            '.configureEach { c ->\n        def targetroot = new File(root.projectDir, '
            '"build/scip-targetroot")\n        %s\n        c.options.compilerArgs.add('
            '"-Xplugin:scip")\n    } }\n}\n')
    script.write_text(body % "c.outputs.dir(targetroot)")
    monkeypatch.setenv("GRADLE_USER_HOME", str(home))
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).doctor()
    assert "     [build] the Gradle build cache is on (org.gradle.caching=true): the init " \
           "script declares the shards as outputs" in out
    assert "WARN [build] the Gradle build cache" not in out
    script.write_text(body % "// c.outputs.dir(targetroot)")   # commented out: not declared
    out = Service(sample_copy, cache_dir=str(tmp_path / "c2")).doctor()
    assert "WARN [build] the Gradle build cache is on" in out and "c.outputs.dir" in out


def test_source_of_a_file_changed_after_its_compilation(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service
    path = os.path.join(sample_copy, "core/src/main/java/com/example/model/Entity.java")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # Three new lines above setState, not compiled yet.
    text = text.replace("    private int state;\n",
                        "    private int state;\n    private int a;\n    private int b;\n"
                        "    private int c;\n", 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.utime(path, (1_700_000_500, 1_700_000_500))      # after its shard
    svc = Service(sample_copy, cache_dir=str(tmp_path / "c"))
    out = svc.source("Entity#setState")
    assert "changed after its compilation: shown as it is now, at lines 17–19 (compiled at " \
           "14–16)" in out, out
    assert "\n17      public void setState(int state) {\n19      }" in out.replace(
        "\n18          this.state = state;", "")
    assert "may be shifted" in out                     # the warning says the lines are read now
    assert "body (as it is now; compiled at 14–16, before the file changed):" in \
        svc.context("Entity#setState")
    # Gone from the file: the compiled lines, said to be what they are.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace("setState(int state)", "changeState(int state)"))
    os.utime(path, (1_700_000_600, 1_700_000_600))
    out = svc.source("Entity#setState")
    assert "was not found in it as it is now: these are lines 14–16 of the current file" in out

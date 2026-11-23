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
    assert "Account#<init>()" in out and "Service#" not in out
    assert "Store#load()" in svc.annotated("Deprecated", scope="com.example.model")
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
    assert "[Account]  Service#seed" in out and "[Group]  Service#activateAll()" in out


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
    out = svc.check_refs()
    assert "9 problems: " in out and "2 members not found" in out, out
    assert "Service.NOPE.get()  — Service has no member NOPE" in out
    for x in ("M/Account", "X_main", "no type Old", "Probe", "Migrate", "DEFAULT", "Account.build", "Fulano", "borrado", "__init__", "get_connection", "ancient"):
        assert x not in out, x
    assert "1 files under archive folders" in out
    assert "no setState(String) in Entity: its overloads take (int)" in out
    assert "line 4 ×2 (also 13)  Entity.reload()  — Entity has no member reload" in out
    assert "no type Nope" in out and "com.example.model.Ghost  — no such type" in out
    assert "line 3 is not in Entity#setState() any more (now lines 9–16)" in out
    assert "line 6 ×2 (also 18)  core/src/main/java/com/example/model/Entity.java:3" in out  # row
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
    only = svc.check_refs(files=["docs"])
    assert "in 2 files" in only and "Entity has no member nothing" in only   # archive too
    assert "Entity has no member ancient" in only
    with open(os.path.join(sample_copy, ".scipq.toml"), "w") as fh:
        fh.write('[check_refs]\nexclude = ["docs/**"]\n')
    assert "NOTES.md" in Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs() and \
        "docs/a.md" not in Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs()


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
    assert "Order#setState()" in state and "a.setState(b.getState())" in state   # Lombok's
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
    assert "d3  Pages#show()" in out and "d3  Page#frame()" in out
    assert "StockPage" not in out           # its super.render()/frame(), refresh(): its own
    assert "page  d1  SalesPage" in out and "page  d2  Page " not in out    # abstract
    # Page#render() overrides View#render(): View#measure() runs it on the same object, and
    # StockPage#refreshAll()'s measure() runs on a StockPage (bound to SalesPage, not Page).
    deep = Service(refactor_root, cache_dir=str(tmp_path / "c")).impact("Pages#sales", depth=5)
    assert "View#measure()" in deep and "StockPage" not in deep
    # measure() on a View may be a SalesPage's; on a StockPage it is not.
    assert "Gallery#show()" in deep and "Dash#show()" not in deep
    assert "callers of methods these override were left out" in deep
    # From the override itself: the base's code runs on it, the sibling's does not.
    out = Service(refactor_root, cache_dir=str(tmp_path / "c")).impact("SalesPage#body",
                                                                       depth=4)
    assert "Page#render()" in out and "Pages#show()" in out and "StockPage" not in out


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


def test_path_goes_through_dispatch(refactor_root, tmp_path):
    # Gallery#show() calls View#measure() → render(), which SalesPage's chain overrides.
    from scipq_mcp.service import Service
    out = Service(refactor_root, cache_dir=str(tmp_path / "c")).path("Gallery#show",
                                                                     "Pages#sales")
    assert "Gallery#show()" in out and "SalesPage#body()" in out and "Page#body()" in out

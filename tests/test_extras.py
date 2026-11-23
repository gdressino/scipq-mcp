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
    assert "[refactor]" in out and "[cache]" in out and "version " in out


def test_check_refs(sample_copy, tmp_path):
    import os
    from scipq_mcp.service import Service
    with open(os.path.join(sample_copy, "NOTES.md"), "w", encoding="utf-8") as fh:
        fh.write("""# Notes
- `Entity#setState(int)` changes it; see core/src/main/java/com/example/model/Entity.java:14.
- `Entity#setState(String)` takes a reason.
- Call `Entity.findById(id)` and then `Entity.reload()`.
- `com.example.model.Ghost` was removed; `Nope#run` too.
- `Entity.setState(...)` lives at core/src/main/java/com/example/model/Entity.java:3.
- Old file: core/.../model/Gone.java; core/.../model/Account.java is fine; `Session.find(1)`.
- Prose: Logger.info("x"), List.of(1), Account.class and Group.feature are not members.
""")
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).check_refs()
    assert "6 problems" in out, out
    assert "no setState(String) in Entity: its overloads take (int)" in out
    assert "Entity has no member reload" in out and "no type Nope" in out
    assert "com.example.model.Ghost  — no such type" in out
    assert "line 3 is not in Entity#setState() any more (now lines 14–16)" in out
    assert "Gone.java  — no such file" in out and "Account.java  —" not in out


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
    assert "entry points reached (by kind):" in out
    assert "servlet  d1  Service#activate()" in out and "in app/src/main/webapp/WEB-INF/web.xml" \
        in out
    assert "job  d1  TestSupport#reset()" in out and "named like *Support" in out


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

import os

import pytest

from scipq_mcp.queries import AmbiguousName, Navigator
from scipq_mcp.service import Service
from scipq_mcp.workspace import Workspace

ENTITY = "com/example/model/Entity#"
SET_STATE = "com/example/model/Entity#setState()."


@pytest.fixture(params=["grep", "python"])
def nav(request, sample_root):
    ws = Workspace(sample_root)
    if request.param == "python":
        ws._grep = None  # pure-Python prefilter
    return Navigator(ws)


def lines(refs):
    return sorted((r.location.source.rsplit("/", 1)[-1], r.location.line) for r in refs)


def test_find(nav):
    assert [d.symbol for d in nav.find("Entity")] == [ENTITY]
    assert {d.symbol for d in nav.find("setState")} == {
        SET_STATE, "com/example/app/Report#setState()."}
    assert {d.symbol for d in nav.find("Builder.number")} == {
        "com/example/model/Account#Builder#number.", "com/example/model/Account#Builder#number()."}
    assert [d.symbol for d in nav.find("Builder.number()")] == [
        "com/example/model/Account#Builder#number()."]
    assert [d.symbol for d in nav.find("Builder")] == ["com/example/model/Account#Builder#"]
    assert nav.find("Gone") == []  # orphan shard: its source was deleted


def test_resolve(nav):
    assert nav.resolve("Entity#setState").symbol == SET_STATE
    r = nav.resolve("setState")
    assert r.symbol is None and len(r.candidates) == 2
    assert nav.resolve("NoSuchThing").symbol is None
    with pytest.raises(AmbiguousName):
        nav.require("setState")


def test_references(nav):
    refs, defs = nav.references(SET_STATE)
    assert lines(refs) == [("Account.java", 8), ("Group.java", 11), ("Service.java", 20),
                           ("Service.java", 25), ("Service.java", 33), ("Service.java", 38),
                           ("ServiceTest.java", 11), ("TestSupport.java", 8)]
    assert [d.location.line for d in defs] == [14]
    # The orphan Gone.java also calls setState: it must not show up.
    assert all("Gone" not in r.location.source for r in refs)


def test_references_exclude_generated_code(nav):
    # Lombok's getter body reads the field at the field's own position: not a real use.
    refs, defs = nav.references("com/example/model/Snapshot#changedAt.")
    assert refs == [] and len(defs) == 1
    refs, _ = nav.references("com/example/model/Snapshot#getChangedAt().")
    assert lines(refs) == [("Service.java", 50)]
    refs, _ = nav.references("com/example/app/Report#getChangedAt().")
    assert lines(refs) == [("Report.java", 18)]


def test_type_references_do_not_include_members(nav):
    refs, _ = nav.references(ENTITY)
    assert all(r.symbol == ENTITY for r in refs)
    assert ("Account.java", 3) in lines(refs)


def test_definition(nav):
    d = nav.definition(SET_STATE)
    assert d.location.source == "core/src/main/java/com/example/model/Entity.java"
    assert d.location.line == 14
    assert "Changes the lifecycle state." in d.info.documentation[0]
    g = nav.definition("com/example/model/Snapshot#getChangedAt().")
    assert g.generated and g.location.line == 9  # Lombok: reported at the field
    assert nav.definition("java/lang/String#") is None


def test_implementations(nav):
    impls = {i.definition.symbol: i for i in nav.implementations(ENTITY)}
    assert set(impls) == {"com/example/model/Account#", "com/example/model/Group#",
                          "com/example/model/SubAccount#"}
    assert impls["com/example/model/Account#"].direct
    assert not impls["com/example/model/SubAccount#"].direct
    assert impls["com/example/model/SubAccount#"].via == ["com/example/model/Account#"]
    over = {i.definition.symbol: i.direct for i in
            nav.implementations("com/example/model/Identifiable#uid().")}
    assert over == {"com/example/model/Entity#uid().": True,
                    "com/example/model/Account#uid().": False,
                    "com/example/model/Group#uid().": False,
                    "com/example/model/SubAccount#uid().": False}


def test_anonymous_class_implementation(nav):
    impls = nav.implementations("com/example/model/Validator#validate().")
    assert len(impls) == 1
    d = impls[0].definition
    assert d.symbol.startswith("local ")
    assert d.container == "com/example/app/Service#activateAll()."


def test_supertypes(nav):
    d, supers = nav.supertypes("com/example/model/SubAccount#")
    assert [(s.symbol, s.direct) for s in supers] == [
        ("com/example/model/Account#", True), ("com/example/model/Entity#", False),
        ("com/example/model/Identifiable#", False)]
    d, supers = nav.supertypes("com/example/model/SubAccount#uid().")
    assert [s.symbol for s in supers if s.direct] == ["com/example/model/Account#uid()."]


def test_outline(nav):
    defs = nav.outline("core/src/main/java/com/example/model/Account.java")
    assert [d.location.line for d in defs] == [3, 4, 6, 12, 17, 17, 18, 20, 25]
    snap = nav.outline("core/src/main/java/com/example/model/Snapshot.java")
    assert {d.symbol.rsplit("#", 1)[-1] for d in snap if d.generated} == {
        "getChangedAt().", "isArchived()."}


def test_callers(nav):
    root = nav.callers(SET_STATE, depth=1)
    callers = {c.symbol: [s.location.line for s in c.sites] for c in root.children}
    assert callers == {
        "com/example/model/Account#`<init>`().": [8],
        "com/example/model/Group#add().": [11],
        "com/example/app/Service#notArchived.": [20],       # lambda in a field initializer
        "com/example/app/Service#activate().": [25],
        "com/example/app/Service#activateAll().": [33, 38],  # 33 is inside an anonymous class
        "com/example/app/ServiceTest#main().": [11],
        "com/example/app/test/TestSupport#reset().": [8],
    }
    anon = [c for c in root.children if c.symbol.endswith("activateAll().")][0]
    assert anon.sites[0].via_local == "validate"


def test_callers_depth_and_initializers(nav):
    root = nav.callers(SET_STATE, depth=2)
    ctor = [c for c in root.children if c.symbol.endswith("Account#`<init>`().")][0]
    assert {c.symbol for c in ctor.children} == {
        "com/example/model/Account#Builder#build().", "com/example/model/SubAccount#`<init>`()."}
    root = nav.callers("com/example/model/Entity#findById().")
    assert {c.symbol for c in root.children} == {
        "com/example/app/Service#",  # static initializer block
        "com/example/app/ServiceTest#main().", "com/example/model/Session#find()."}


def test_callers_include_overridden(nav):
    plain = nav.callers("com/example/model/SubAccount#uid().")
    assert all(c.via == "" for c in plain.children)
    wide = nav.callers("com/example/model/SubAccount#uid().", include_overridden=True)
    assert {(c.symbol, c.via) for c in wide.children} >= {
        ("com/example/app/ServiceTest#main().", "com/example/model/Account#uid().")}


def test_callees(nav):
    d, out = nav.callees("com/example/app/Service#activateAll().")
    assert [(c.symbol, [s.line for s in c.sites]) for c in out] == [
        (SET_STATE, [33, 38]), ("com/example/model/Validator#validate().", [37])]


def test_freshness(sample_copy):
    ws = Workspace(sample_copy)
    assert not ws.freshness()
    src = os.path.join(sample_copy, "core/src/main/java/com/example/model/Group.java")
    os.utime(src, (1_700_000_500, 1_700_000_500))
    new = os.path.join(sample_copy, "core/src/main/java/com/example/model/Fresh.java")
    with open(new, "w") as fh:
        fh.write("package com.example.model;\nclass Fresh {}\n")
    os.utime(new, (1_700_000_500, 1_700_000_500))
    for check in (Workspace(sample_copy).freshness, Workspace(sample_copy)._freshness_python):
        f = check()
        assert f.stale == ["core/src/main/java/com/example/model/Group.java"]
        assert f.uncompiled == ["core/src/main/java/com/example/model/Fresh.java"]
    assert Workspace(sample_copy).freshness_of(
        ["core/src/main/java/com/example/model/Group.java"]).stale == [
        "core/src/main/java/com/example/model/Group.java"]

    text = Service(sample_copy).references("Entity#setState")
    assert "WARNING: 1 source changed after their last compilation" in text
    assert "in these results: core/src/main/java/com/example/model/Group.java" in text
    assert "created after the last compilation" in text
    assert "WARNING" not in Service(sample_copy, freshness="off").references("Entity#setState")


def test_service_text(sample_root):
    svc = Service(sample_root)
    text = svc.references("Entity#setState")
    assert text.startswith("-- Entity#setState → com/example/model/Entity#setState().")
    assert "  25: e.setState(1);" in text
    assert "matches 2 symbols" in svc.references("setState")
    page = svc.references("Entity#setState", offset=2, limit=2)
    assert "(showing 3–4; next offset 4)" in page
    assert svc.outline("Account").startswith("-- Account → com/example/model/Account#")
    assert "error:" in Service("/").references("x")


def test_project_root_from_subdirectory(sample_root):
    sub = os.path.join(sample_root, "app", "src")
    assert "8 references" in Service().references("Entity#setState", project_root=sub)


def test_a_shard_removed_during_a_query_does_not_fail_it(sample_copy):
    import os
    from scipq_mcp.workspace import Workspace
    ws = Workspace(sample_copy)
    files = ws.files()
    victim = os.path.join(ws.scip_roots()[files[0][0]][0], files[0][1])
    os.unlink(victim)  # e.g. a recompilation cleaned it after the listing
    hits = ws.grep([b"com/example/model/Entity#"])
    assert hits and all(os.path.exists(h.path) for h in hits)


def test_read_header_spans_lines(tmp_path):
    from scipq_mcp.queries import _read_header
    p = tmp_path / "Range.java"
    p.write_text("package a;\n\npublic record Range(\n        int lo, // inclusive {\n"
                 "        int hi) implements Comparable<Range> {\n}\n")
    assert _read_header(str(p), 2) == ("public record Range( int lo, int hi) "
                                       "implements Comparable<Range>")


def test_parameter_lists_pick_overloads(refactor_root):
    nav = Navigator(Workspace(refactor_root))
    base = "com/example/shop/Overloaded#pick"
    # m() is the overload without parameters, also when SCIP numbers it (+1).
    r = nav.resolve("Overloaded#pick()")
    assert r.symbol == base + "(+1)." and "without parameters" in r.note
    assert "SCIP numbers it pick(+1), and SCIP's pick() is another one: public int pick(int n)" \
        in r.note
    assert nav.resolve("Overloaded#pick(int)").symbol == base + "()."
    assert nav.resolve("Overloaded#pick(String s, int n)").symbol == base + "(+2)."
    assert nav.resolve("Overloaded#pick(Map<String, List<Integer>>, String...)").symbol == \
        base + "(+3)."
    assert nav.resolve("Overloaded#pick(Map, String[])").symbol == base + "(+3)."
    assert nav.resolve("Overloaded#pick(Object, long)").symbol == base + "(+4)."  # <T> pick(T, …)
    r = nav.resolve("Overloaded#pick(int, int)")
    assert r.symbol is None and "no overload of 'Overloaded#pick' takes (int, int)" in r.note
    assert len(r.candidates) == 5
    assert nav.resolve("Overloaded#pick").symbol is None        # no list: every overload
    assert nav.resolve("Overloaded#pick(+1)").symbol == base + "(+1)."
    assert [d.symbol for d in nav.find("pick()")] == [base + "(+1)."]
    # m() without an overload free of parameters still means the method, whatever it takes.
    assert nav.resolve("Units#m(String)").symbol == "com/example/shop/Units#m(+1)."
    assert nav.resolve("Upper#of()").symbol == "com/example/shop/Upper#of()."


@pytest.mark.skipif(not __import__("scipq_mcp.libsrc").libsrc.jdk_src_zip(),
                    reason="no JDK src.zip on this machine")
def test_parameter_lists_pick_library_overloads(refactor_root, tmp_path):
    from scipq_mcp import libsrc as L
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"), library_roots=[L.jdk_src_zip()])
    out = svc.definition("Map#of()")   # Map.of() in Overloaded; the list picks among 11+
    assert out.startswith("-- Map#of() → java/util/Map#of(). (external, the overload without "
                          "parameters)")
    out = svc.definition("Map#of(Object, Object)")
    assert "java/util/Map#of(+1)." in out.splitlines()[0]
    assert "the overload taking (Object, Object)" in out.splitlines()[0]


def test_overloaded_methods_are_written_with_their_parameters(refactor_root, tmp_path):
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.overloads("Overloaded#pick")
    for label in ("Overloaded#pick(int)", "Overloaded#pick()", "Overloaded#pick(String, int)",
                  "Overloaded#pick(Map, String[])", "Overloaded#pick(T, long)"):
        assert label in out, (label, out)
    assert "pick(+1)" not in out
    # Each label reads back as the same overload.
    for label, sym in (("Overloaded#pick(int)", "pick()."), ("Overloaded#pick()", "pick(+1)."),
                       ("Overloaded#pick(T, long)", "pick(+4).")):
        assert f"→ com/example/shop/Overloaded#{sym}" in svc.definition(label)
    # A method without overloads keeps its plain name.
    out = svc.callers("Units#m(String)")
    assert "callers of Units#m(String)" in out and "\nUnits#use(Bar, FooBar)  " in out
    assert "Upper#of(String) — " in svc.references("Upper#of")


def test_m_without_parameters_across_types_and_inherited(sample_root, refactor_root):
    nav = Navigator(Workspace(refactor_root))
    # Several types: only those with an overload without parameters.
    r = nav.find("pick()")
    assert [d.symbol for d in r] == ["com/example/shop/Overloaded#pick(+1)."]
    nav = Navigator(Workspace(sample_root))
    # Service declares process(int) and process(String) only: `process()` is still the method.
    r = nav.resolve("Service#process()")
    assert r.symbol is None and len(r.candidates) == 2


def test_qualified_parameter_types_compare_the_package(refactor_root):
    nav = Navigator(Workspace(refactor_root))
    base = "com/example/shop/Overloaded#pick"
    assert nav.resolve("Overloaded#pick(java.util.Map, String[])").symbol == base + "(+3)."
    assert nav.resolve("Overloaded#pick(java.lang.String, int)").symbol == base + "(+2)."
    r = nav.resolve("Overloaded#pick(java.awt.Map, String[])")
    assert r.symbol is None and "no overload" in r.note


def test_overloads_of_a_library_method_by_its_name(refactor_root, tmp_path):
    from scipq_mcp import libsrc as L
    if not L.jdk_src_zip():
        pytest.skip("no JDK src.zip on this machine")
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"), library_roots=[L.jdk_src_zip()])
    out = svc.overloads("HashSet#<init>")
    assert "5 overloads" in out and "HashSet#<init>()  " in out and \
        "HashSet#<init>(int, float)" in out


def test_a_generic_method_is_the_caller_not_its_type_parameter(refactor_root, tmp_path):
    nav = Navigator(Workspace(refactor_root))
    refs, _ = nav.references("com/example/shop/Overloaded#pick().")
    assert {r.container for r in refs} == {"com/example/shop/Overloaded#pick(+1).",
                                          "com/example/shop/Overloaded#use()."}
    # Inside <T> pick(T value, long n): the method, as in the index.
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"))
    out = svc.callers("Overloaded#pick(int)", depth=2)
    assert "[T]" not in out and "Overloaded#pick()  " in out and "Overloaded#use()" in out


def test_methods_taking_parameters_are_written_with_them(sample_root, tmp_path):
    svc = Service(sample_root, cache_dir=str(tmp_path / "c"))
    out = svc.callers("Entity#setState")
    assert out.splitlines()[1].startswith("callers of Entity#setState(int)")
    assert "Entity#getState()" in svc.references("Entity#getState")   # no parameters
    out = svc.definition("Entity#setState(int)")
    assert "→ com/example/model/Entity#setState()." in out


def test_constructors_are_not_inherited(nav):
    # Account declares only Account(String); Entity has the implicit one without parameters.
    r = nav.resolve("Account#<init>()")
    assert r.symbol == "com/example/model/Account#`<init>`()." and "inherited" not in r.note


def test_m_without_parameters_when_none_is_says_so(nav):
    r = nav.resolve("Account#<init>()")
    assert "(none without parameters: the constructor, which takes (String))" in r.note
    r = nav.resolve("Service#process()")
    assert r.symbol is None and "no overload of 'Service#process' is without parameters" in r.note


def test_a_type_variable_written_reads_back_as_that_overload():
    from scipq_mcp.queries import by_params
    items = ["a/Box#put().", "a/Box#put(+1)."]
    declared = {"a/Box#put().": (None,), "a/Box#put(+1).": ("String",)}.get
    assert by_params(items, (None,), declared) == ["a/Box#put()."]        # put(T)
    assert by_params(items, ("String",), declared) == ["a/Box#put(+1)."]


def test_methods_whose_parameters_are_not_read_are_written_with_an_ellipsis(refactor_root,
                                                                            tmp_path,
                                                                            monkeypatch):
    from scipq_mcp import libsrc as L
    from scipq_mcp import service as SV
    if not L.jdk_src_zip():
        pytest.skip("no JDK src.zip on this machine")
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"), library_roots=[L.jdk_src_zip()])
    svc.definition("java.util.Map#of()")      # maps the JDK's sources
    out = svc.callees("Overloaded#use()")
    assert "Map#of()" in out and "(…)" not in out
    monkeypatch.setattr(SV.OverloadLabels, "LIBRARY_SECONDS", 0.0)
    out = svc.callees("Overloaded#use()")
    assert "Map#of(…)" in out and "Overloaded#pick(String, int)" in out
    assert "methods are written m(…), their parameters not read" in out
    assert "past the time for names" in out

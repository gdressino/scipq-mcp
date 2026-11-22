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
                           ("ServiceTest.java", 11)]
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
        "com/example/app/ServiceTest#main()."}


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
    assert "7 references" in Service().references("Entity#setState", project_root=sub)

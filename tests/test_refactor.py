import json
import os
import re
import shutil
import subprocess

import pytest

from scipq_mcp.service import Service


def plan(svc, steps, **kw):
    return svc.refactor(json.dumps({"steps": steps}), **kw)


def patch_of(out: str) -> str:
    m = re.search(r"^patch: (.+)$", out, re.M)
    assert m, out
    return open(m.group(1), encoding="utf-8").read()


def git_apply(root: str, out: str) -> None:
    if not shutil.which("git"):
        pytest.skip("git not available")
    path = re.search(r"^patch: (.+)$", out, re.M).group(1)
    git = ["git", "-C", root, "-c", "user.email=t@example.com", "-c", "user.name=t"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(git + args, check=True, capture_output=True)
    subprocess.run(git + ["apply", "--check", path], check=True, capture_output=True)


def test_plan_errors_need_no_jvm(sample_copy, tmp_path):
    svc = Service(sample_copy, cache_dir=str(tmp_path / "c"))
    assert "a plan is" in svc.refactor("{}")
    assert "not valid JSON" in svc.refactor("{steps")
    assert "needs a macro" in plan(svc, [{"symbol": "Entity"}])
    assert "a Java identifier" in plan(svc, [{"rename": {"symbol": "Entity#setState",
                                                         "to": "class"}}])
    assert "matches" in plan(svc, [{"rename": {"symbol": "setState", "to": "x"}}])  # ambiguous


def test_rename_method_with_its_family(sample_copy, javaparser_cache):
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"rename": {"symbol": "Entity#setState", "to": "changeState"}}])
    assert "6 files changed" in out and "9 nodes in 6 files" in out
    diff = patch_of(out)
    assert "-        e.setState(1);\n+        e.changeState(1);" in diff
    assert "Report.java" not in diff          # an unrelated setState of another class
    assert re.search(r"^index [0-9a-f]{40}\.\.[0-9a-f]{40} 100644$", diff, re.M)
    git_apply(sample_copy, out)
    assert "the patch is not applied" in svc.refactor(
        plan_id=re.search(r"plan ([0-9a-f]{12}):", out).group(1), verify=True)


def test_rename_fields_lombok_and_hand_written_accessors(sample_copy, javaparser_cache):
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"rename": {"symbol": "Snapshot#changedAt", "to": "modifiedAt"}},
                     {"rename": {"symbol": "Entity#state", "to": "status"}}])
    diff = patch_of(out)
    assert "+        return s.getModifiedAt().getTime();" in diff   # Lombok's getter follows
    assert "+        this.status = state;" in diff                   # the parameter stays
    assert "public int getState()" in diff                           # written by hand: kept
    assert 'pass "accessors": true' in out


def test_signature_template_and_imports(sample_copy, javaparser_cache):
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [
        {"change_signature": {"symbol": "Entity#setState", "parameters": [
            {"add": "String reason", "default": '"manual"'}, {"from": 0}]}},
        {"replace_calls": {"symbol": "Entity#findById",
                           "template": "Objects.requireNonNull(Entity.findById($0))",
                           "imports": ["java.util.Objects"]}}])
    diff = patch_of(out)
    assert '+    public void setState(String reason, int state) {' in diff
    assert '+        e.setState("manual", 1);' in diff
    assert "+        return Objects.requireNonNull(Entity.findById(id));" in diff
    assert "+import java.util.Objects;" in diff
    assert diff.count("+import java.util.Objects;") == 3   # every file it changed, once


def test_primitives_compose(sample_copy, javaparser_cache):
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [
        {"select": {"symbol": "Entity#setState", "role": "references", "node": "statement",
                    "files": "**/Group.java"},
         "do": {"wrap": "if (e != null) {\n    $this\n}"}},
        {"select": {"symbol": "Service#activate", "role": "definition", "node": "declaration"},
         "do": {"add_annotation": "@Deprecated"}},
        {"select": {"symbol": "Store", "role": "definition", "node": "declaration"},
         "do": {"add_member": "public int size() {\n    return 0;\n}"}},
        {"select": {"in": "Store#load", "kind": "TryStmt"},
         "do": {"insert_before": 'System.out.println("loading " + id);'}},
        {"select": {"in": "Group", "kind": "MethodCallExpr", "where": "$x.size()"},
         "do": {"replace": "$x.size() - 0"}}])
    diff = patch_of(out)
    assert "+        if (e != null) {\n+            e.setState(2);\n+        }" in diff
    assert "+    @Deprecated\n     public void activate(Entity e) {" in diff
    assert "+    public int size() {\n+        return 0;\n+    }" in diff
    assert '+        System.out.println("loading " + id);\n         try (' in diff
    assert '+        return "G-" + (members.size() - 0);' in diff    # parenthesized
    assert "+        return members.size() - 0;" in diff


def test_move_and_rename_types(sample_copy, javaparser_cache):
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"move": {"symbol": "Session", "to": "com.example.store"}},
                     {"rename": {"symbol": "Group", "to": "Team"}}])
    diff = patch_of(out)
    assert "rename to core/src/main/java/com/example/store/Session.java" in diff
    assert "+package com.example.store;" in diff
    assert "+import com.example.model.Entity;" in diff        # what it used without import
    assert "+import com.example.store.Session;" in diff       # its old neighbours now import it
    assert "rename to core/src/main/java/com/example/model/Team.java" in diff
    assert "+import com.example.model.Team;" in diff
    git_apply(sample_copy, out)


def test_conflicts_and_bad_templates_are_refused(sample_copy, javaparser_cache):
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"rename": {"symbol": "Entity#state", "to": "status"}},
                     {"select": {"symbol": "Entity#state", "role": "references", "node": "expr",
                                 "files": "**/Entity.java"}, "do": {"replace": "this.state"}}])
    assert "REFUSED" in out and "change the same code" in out
    out = plan(svc, [{"replace_calls": {"symbol": "Entity#findById", "template": "a +"}}])
    assert "REFUSED" in out and "does not parse as expression" in out
    # An edit after the last compilation (a new date alone, same content, is not one).
    with open(os.path.join(sample_copy, "core/src/main/java/com/example/model/Entity.java"),
              "a", encoding="utf-8") as fh:
        fh.write("// edited\n")
    out = plan(svc, [{"rename": {"symbol": "Entity#setState", "to": "changeState"}}])
    assert "REFUSED" in out and "changed after their last compilation" in out


def test_dry_run(sample_copy, javaparser_cache):
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"select": {"in": "Group", "kind": "MethodCallExpr", "where": "$x.size()"},
                      "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert "dry run" in out and "2 nodes in 1 files" in out and "$x" in out
    assert "patch:" not in out


def test_persistence_shadowing_and_overrides(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    out = plan(svc, [{"rename": {"symbol": "Order#state", "to": "status"}}])
    assert "REFUSED" in out and "keep_column" in out
    out = plan(svc, [{"rename": {"symbol": "Counter#count", "to": "total"}}])
    assert "REFUSED" in out and "would capture the renamed field" in out
    out = plan(svc, [{"rename": {"symbol": "Panel#refresh", "to": "redraw"}}])
    assert "REFUSED" in out and "anonymous class whose supertype has `redraw`" in out
    out = plan(svc, [{"rename": {"symbol": "Order", "to": "Purchase"}}])
    assert "REFUSED" in out and "discriminator" in out
    assert "REFUSED" not in plan(svc, [{"rename": {"symbol": "Order", "to": "Purchase",
                                                   "migration": True}}])


def test_persistent_rename_with_keep_column(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    out = plan(svc, [
        {"rename": {"symbol": "Order#state", "to": "status", "keep_column": "STATE"}},
        {"rename": {"symbol": "Line#order", "to": "purchase", "keep_column": "ORDER_ID"}},
        {"rename": {"symbol": "Prices#round", "to": "roundHalfUp"}}])
    diff = patch_of(out)
    assert '+    @Column(name = "STATE")\n+    private int status;' in diff
    assert '@Persistent(name = "status")})' in diff                 # the fetch group
    assert '+    @Persistent(mappedBy = "purchase")' in diff         # the other side
    assert "+import javax.jdo.annotations.Column;" in diff          # Line had no Column import
    assert "+    int getStatus();" in diff                          # the interface Lombok's
    assert "a.setStatus(b.getStatus())" in diff and "Order::getStatus" in diff  # implements
    assert "+import static com.example.shop.Prices.roundHalfUp;" in diff
    assert "{@link Prices#roundHalfUp(double)}" in diff and "@see Prices#roundHalfUp" in diff
    assert '"state == :p"' in out                                   # JDOQL: to review
    review = os.path.join(os.path.dirname(re.search(r"^patch: (.+)$", out, re.M).group(1)),
                          "review.txt")
    assert '"state == :p"' in open(review, encoding="utf-8").read()  # the whole list, kept
    git_apply(refactor_root, out)


def test_unmatched_sites_and_overload_warnings(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    out = plan(svc, [{"replace_calls": {"symbol": "Order#getState",
                                        "template": "$recv.currentState()"}}])
    assert "a method reference (Order::getState), not a call" in out
    out = plan(svc, [{"change_signature": {"symbol": "Overloads#m(+1)",
                                           "parameters": [{"from": 1}, {"from": 0}]}}])
    assert "calls with null or lambdas may become ambiguous" in out


def test_plan_keys_are_checked(sample_copy, tmp_path):
    svc = Service(sample_copy, cache_dir=str(tmp_path / "c"))
    out = plan(svc, [{"change_signature": {"symbol": "Entity#setState", "parameters": [],
                                           "import": ["x.Y"]}}])
    assert "does not take import" in out
    out = plan(svc, [{"select": {"symbol": "Entity", "nodes": "call"}, "do": {"remove": True}}])
    assert "select does not take nodes" in out
    assert "unknown keys" in plan(svc, [{"rename": {"symbol": "Entity", "to": "E"}, "x": 1}])


def test_generated_paths():
    from scipq_mcp.refactor import _is_generated
    assert _is_generated("app/build/generated/sources/annotationProcessor/java/main/a/B.java")
    assert _is_generated("target/generated-sources/a/B.java")
    assert not _is_generated("core/src/main/java/com/acme/build/Task.java")
    assert not _is_generated("core/src/main/java/com/acme/out/Task.java")


def test_moves_together_and_package_access(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    out = plan(svc, [{"move": {"symbol": "com.example.shop.util.Reader",
                               "to": "com.example.shop.util.io"}},
                     {"move": {"symbol": "com.example.shop.util.Writer",
                               "to": "com.example.shop.util.io"}}])
    diff = patch_of(out)
    reader = diff.split("rename to shop/src/main/java/com/example/shop/util/io/Reader.java")[1] \
        .split("diff --git")[0]
    assert "+import com.example.shop.util.Format;" in reader      # the neighbour that stays
    assert "import com.example.shop.util.Writer" not in reader    # moved with it: same
    assert "import com.example.shop.util.io.Writer" not in reader  # package, no import
    assert "import com.example.shop.util.Helper" not in reader    # a subpackage's type
    assert "WILL NOT COMPILE: Writer#header() is package-private" in out
    assert "util/Audit.java" in out


def test_javadoc_links_of_overloads_and_name_boundaries(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    diff = patch_of(plan(svc, [{"rename": {"symbol": "com/example/shop/Units#m().",
                                           "to": "count"}},
                               {"rename": {"symbol": "Bar", "to": "Baz"}}]))
    assert "{@link #count()} and {@link #m(String)}" in diff     # the other overload's stays
    assert "{@link Baz} and {@link FooBar}" in diff               # FooBar is not Bar


def test_varargs_and_hidden_fields_in_signatures(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    diff = patch_of(plan(svc, [{"change_signature": {"symbol": "Log#log", "parameters": [
        {"add": "int level", "default": "0"}, {"from": 0}, {"from": 1}]}}]))
    assert '+        return log(0, "%s %s", a, b) + log(0, "none");' in diff
    out = plan(svc, [{"change_signature": {"symbol": "Counter#next", "parameters": [
        {"add": "int count", "default": "0"}]}}])
    assert "the new parameter `count` hides the `count` the body already uses" in out


def test_anonymous_overrides_library_families_and_kinds_in_files(refactor_root, sample_copy,
                                                                  javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    diff = patch_of(plan(svc, [{"rename": {"symbol": "Hook#before", "to": "beforeAll"}}]))
    assert diff.count("void beforeAll()") == 2 and "+        beforeAll();" in diff
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"select": {"symbol": "java/util/List#size().", "family": True,
                                 "node": "call", "role": "references"},
                      "do": {"replace": "$recv.toArray().length"}}])
    assert "REFUSED" not in out and "members.toArray().length" in patch_of(out)
    out = plan(svc, [{"select": {"files": "**/Group.java", "kind": "MethodCallExpr",
                                 "where": "$x.size()"}, "do": {"replace": "$x.toArray().length"}}])
    assert "2 nodes in 1 files" in out


def test_create_and_what_verify_expects(sample_copy, javaparser_cache):
    from scipq_mcp import refactor as R
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"create": {"path": "core/src/main/java/com/example/model/Made.java",
                                 "template": "package com.example.model;\n\npublic class Made {}\n"}},
                     {"replace_calls": {"symbol": "Entity#findById",
                                        "template": "Entity.findById($0 + 0)"}}])
    diff = patch_of(out)
    assert "new file mode 100644" in diff and "+public class Made {}" in diff
    meta, _p = R.load_meta(sample_copy, re.search(r"plan ([0-9a-f]{12}):", out).group(1),
                           javaparser_cache)
    assert meta["symbols"] == []      # a replace leaves its method in place


def test_dry_run_runs_the_checks_and_verify_sees_failed_builds(refactor_root, sample_copy,
                                                               javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    out = plan(svc, [{"rename": {"symbol": "Counter#count", "to": "total"}}], dry_run=True)
    assert "dry run" in out and "would capture the renamed field" in out
    # A class folder where a compiled class is missing: the build failed after the shards.
    from scipq_mcp.queries import Navigator
    from scipq_mcp.refactor import _class_files
    ws = Service(sample_copy).workspace(None)
    ws.files()
    nav = Navigator(ws)
    src = "core/src/main/java/com/example/model/Entity.java"
    classes = os.path.join(sample_copy, "core/build/classes/java/main/com/example/model")
    os.makedirs(classes)
    assert "no com/example/model/Entity.class" in _class_files(nav, [src])[0]
    with open(os.path.join(classes, "Entity.class"), "wb") as fh:
        fh.write(b"\xca\xfe")
    assert _class_files(nav, [src]) == []
    os.utime(os.path.join(classes, "Entity.class"), (1_000_000_000, 1_000_000_000))
    assert "older than the source" in _class_files(nav, [src])[0]


def test_library_overloads_symbol_lists_and_stored_results(sample_copy, javaparser_cache):
    from scipq_mcp import refactor as R
    svc = Service(sample_copy, cache_dir=javaparser_cache)
    out = plan(svc, [{"replace_calls": {"symbol": "java.util.ArrayList#<init>", "overloads": True,
                                        "template": "new java.util.ArrayList<>($args)"}},
                     {"select": {"symbols": ["java.util.List#size", "java.util.Nope#size"],
                                 "node": "call", "role": "references"},
                      "do": {"replace": "$recv.toArray().length"}}])
    assert "REFUSED" not in out, out
    assert "java.util.Nope#size: no symbol in this project matches it; skipped" in out
    meta, patch_path = R.load_meta(sample_copy, re.search(r"plan ([0-9a-f]{12}):", out).group(1),
                                   javaparser_cache)
    assert meta["templates"]       # verify reports what this new code resolves to


def test_an_earlier_step_can_open_access_for_a_move(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    out = plan(svc, [{"select": {"symbol": "com.example.shop.util.Writer#header",
                                 "role": "definition", "node": "declaration"},
                      "do": {"set_modifiers": {"add": ["public"]}}},
                     {"move": {"symbol": "com.example.shop.util.Reader",
                               "to": "com.example.shop.util.io"}},
                     {"move": {"symbol": "com.example.shop.util.Writer",
                               "to": "com.example.shop.util.io"}}])
    assert "WILL NOT COMPILE" not in out and "+    public static String header() {" in \
        patch_of(out)


def test_review_regressions_without_a_jvm():
    from scipq_mcp import compare as CMP
    from scipq_mcp import refactor as R
    from scipq_mcp import symbols as S
    # change_signature's no_calls is a known key.
    R.load_plan({"steps": [{"change_signature": {"symbol": "A#m", "parameters": [],
                                                 "no_calls": True}}]})
    # Annotations with arguments do not hide the modifiers or the parameter list.
    assert S.is_private('@SuppressWarnings("unchecked") private void helper()')
    assert not S.is_private('@Named("private") public void open()')
    assert R._param_types('@Deprecated(since = "2") void m(int a, int b)') == ("int", "int")
    assert CMP.shape('@Deprecated(since = "2") void m(int a)') == "void m(int)"


def _jdk_service(root, cache):
    from scipq_mcp import libsrc as L
    if not L.jdk_src_zip():
        pytest.skip("no JDK src.zip on this machine")
    return Service(root, cache_dir=cache, library_roots=[L.jdk_src_zip()])


def _lines(out: str) -> list[int]:
    return sorted(int(m) for m in re.findall(r"Sizes\.java:(\d+)  BinaryExpr", out))


def test_where_types_check_captures(refactor_root, javaparser_cache):
    svc = _jdk_service(refactor_root, javaparser_cache)
    sel = {"in": "Sizes#empties", "kind": "BinaryExpr"}
    out = plan(svc, [{"select": {**sel, "where": "$x.size() == 0"},
                      "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert _lines(out) == [22, 23, 24, 25, 26, 27, 29]     # box.size() (24) too
    assert "where matches the code's shape only" in out
    typed = {**sel, "where": {"pattern": "$x.size() == 0",
                              "types": {"$x": "java.util.Collection"}}}
    out = plan(svc, [{"select": typed, "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    # fields, parameters, a method's return type, `var` (HashSet): not Box
    assert _lines(out) == [22, 23, 25, 26, 27, 29], out
    assert "1 sites left out by node/where" in out and "shape only" not in out
    assert "1 sites left out by where types: Sizes.Box 1" in out
    out = plan(svc, [{"select": {**sel, "where": {"pattern": "$x.size() == 0",
                                                  "types": {"x": "Box"}}},
                      "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert _lines(out) == [24]                              # a project type, simple name
    out = plan(svc, [{"select": {**sel, "where": {"pattern": "$x == 0", "types": {"$x": "int"}}},
                      "do": {"replace": "$x < 1"}}], dry_run=True)
    assert _lines(out) == [22, 23, 24, 25, 26, 27, 29, 30]  # library and project returns, length()
    out = plan(svc, [{"select": {**sel, "where": {"pattern": "$a && $b",
                                                  "types": {"$a": "boolean"}}},
                      "do": {"replace": "$b && $a"}}], dry_run=True)
    assert _lines(out) == [31]                              # x && y: the innermost
    assert "6 sites left out: the type of $a could not be told" in out
    out = plan(svc, [{"select": {**sel, "where": {"pattern": "$x.size() == 0",
                                                  "types": {"$y": "List"}}},
                      "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert "$y is not a capture of the pattern" in out


def test_family_of_a_library_method_reaches_library_subtypes(refactor_root, javaparser_cache):
    svc = _jdk_service(refactor_root, javaparser_cache)
    out = svc.family("java.util.Collection#size()")
    for name in ("List#size()", "Set#size()", "ArrayList#size()", "HashSet#size()",
                 "Sizes.Bag#size()"):
        assert re.search(r"descendant +" + re.escape(name), out), (name, out)
    assert "AbstractCollection" not in out          # in the hierarchy, called nowhere here
    assert "Box#size" not in out
    out = svc.family("java.util.List#size()")
    assert re.search(r"ancestor +Collection#size\(\)", out)
    assert re.search(r"descendant +ArrayList#size\(\)", out) and "Set#size" not in out
    sel = {"symbol": "java.util.Collection#size()", "family": True,
           "node": "ancestor:BinaryExpr", "where": "$x.size() == 0"}
    out = plan(svc, [{"select": sel, "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert _lines(out) == [22, 23, 25, 26, 27, 29], out
    assert re.search(r"the family of Collection#size\(\) adds 5: .*List#size\(\)", out), out


def test_type_ancestors_for_where_types(refactor_root, tmp_path):
    from scipq_mcp import libsrc as L
    from scipq_mcp.queries import Navigator
    from scipq_mcp.workspace import Workspace
    if not L.jdk_src_zip():
        pytest.skip("no JDK src.zip on this machine")
    svc = Service(refactor_root, cache_dir=str(tmp_path / "c"), library_roots=[L.jdk_src_zip()])
    nav = Navigator(Workspace(refactor_root))
    ours = lambda s: s.startswith("com/example/")   # noqa: E731
    # A project type: SCIP lists every supertype, the library's included.
    assert "java/util/Collection#" in svc._type_ancestors(nav, "com/example/shop/Sizes#Bag#",
                                                          ours)
    assert "java/util/Collection#" in svc._type_ancestors(nav, "java/util/HashSet#", ours)
    # A library type without sources: unknown, not "no supertypes".
    assert svc._type_ancestors(nav, "org/nowhere/Thing#", ours) is None
    from scipq_mcp.refactor import CaptureTypes
    ct = CaptureTypes(None, {"ancestors": lambda t: None if t.startswith("org/") else []})
    assert ct.is_a("org/nowhere/Thing#", "java.util.Collection") is None
    assert ct.is_a("int", "int") is True and ct.is_a("int", "long") is False
    assert ct.is_a("java/util/Map#", "Map") is True
    assert ct.is_a("java/util/Map#", "java.util.Collection") is False


def test_capture_type_of_new_and_casts():
    from scipq_mcp.refactor import CaptureTypes, _top_level
    text = "x = new a.Outer.Inner<Map<K, V>>(new B()) {}; y = (Outer.Inner<List<C>>) z;"
    occs = []
    for name, sym in (("Outer", "a/Outer#"), ("Inner", "a/Outer#Inner#`<init>`()."),
                      ("Map", "java/util/Map#"), ("B", "b/B#`<init>`()."),
                      ("Outer", "a/Outer#"), ("Inner", "a/Outer#Inner#"),
                      ("List", "java/util/List#"), ("C", "c/C#")):
        at = text.find(name, occs[-1][1] if occs else 0)
        occs.append((at, at + len(name), sym))
    lo = text.find("new")
    start = lo + 3
    assert CaptureTypes._head_type(text, occs, start, _top_level(text, start, len(text), "([{")) \
        == "a/Outer#Inner#"
    lo = text.find("(Outer")
    start = lo + 1
    assert CaptureTypes._head_type(text, occs, start, _top_level(text, start, len(text), ")")) \
        == "a/Outer#Inner#"


def test_where_types_need_a_known_type_and_files_are_bounded(refactor_root, javaparser_cache,
                                                             monkeypatch):
    from scipq_mcp import refactor as RF
    svc = _jdk_service(refactor_root, javaparser_cache)
    sel = {"in": "Sizes#empties", "kind": "BinaryExpr"}
    out = plan(svc, [{"select": {**sel, "where": {"pattern": "$x.size() == 0",
                                                  "types": {"$x": "java.util.Colection"}}},
                      "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert "no type 'java.util.Colection' in this project or its libraries" in out
    out = plan(svc, [{"select": {**sel, "where": {"pattern": "$x.size() == 0",
                                                  "types": {"$x": "AbstractCollection"}}},
                      "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert _lines(out) == [26, 29]          # ArrayList and HashSet: a library supertype
    monkeypatch.setattr(RF, "MAX_FILES", 3)
    out = plan(svc, [{"select": {"files": "**/*.java", "kind": "BinaryExpr",
                                 "where": "$x.size() == 0"},
                      "do": {"replace": "$x.isEmpty()"}}], dry_run=True)
    assert "more than 3 .java files; narrow the globs" in out


def test_plan_says_which_overload_a_parameter_list_picked(refactor_root, javaparser_cache):
    svc = Service(refactor_root, cache_dir=javaparser_cache)
    out = plan(svc, [{"replace_calls": {"symbol": "Overloaded#pick()", "template": "pick(0)"}}],
               dry_run=True)
    assert "Overloaded#pick() → com/example/shop/Overloaded#pick(+1). (the overload without " \
           "parameters; SCIP numbers it pick(+1), and SCIP's pick() is another one: public int " \
           "pick(int n))" in out

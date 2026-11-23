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
    os.utime(os.path.join(sample_copy, "core/src/main/java/com/example/model/Entity.java"))
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

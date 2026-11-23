import sqlite3
import os
import shutil
import subprocess

import pytest

from scipq_mcp import cachedir as C
from scipq_mcp.graph import Graph, cycles
from scipq_mcp.index import Index
from scipq_mcp.service import Service
from scipq_mcp.workspace import Workspace

SET_STATE = "com/example/model/Entity#setState()."


@pytest.fixture
def cache(tmp_path):
    return str(tmp_path / "cache")


@pytest.fixture
def graph(sample_root, cache):
    ix = Index(Workspace(sample_root), cache)
    ix.update()
    g = Graph(ix)
    yield g
    g.close()


def test_build_and_incremental_update(sample_copy, cache):
    ix = Index(Workspace(sample_copy), cache)
    first = ix.update()
    assert first.built_from_scratch and first.processed == 18 and first.shards == 19
    assert ix.update().processed == 0
    shard = os.path.join(sample_copy, "build/scip-targetroot/core/compileJava/META-INF/scip/"
                                      "core/src/main/java/com/example/model/Group.java.scip")
    os.utime(shard, (1_700_000_900, 1_700_000_900))
    again = Index(Workspace(sample_copy), cache).update()
    assert (again.processed, again.removed, again.built_from_scratch) == (1, 0, False)
    os.unlink(shard)
    gone = Index(Workspace(sample_copy), cache).update()
    assert gone.removed == 1
    st = ix.status()
    assert st["shards"] == 18 and st["refs"] > 0


def test_index_recreated_when_deleted(sample_root, cache):
    ix = Index(Workspace(sample_root), cache)
    ix.update()
    os.unlink(ix.path)
    assert Index(Workspace(sample_root), cache).update().built_from_scratch


def test_impact_matches_callers(graph):
    nodes, truncated = graph.impact(SET_STATE, depth=1)
    assert not truncated
    assert {n.symbol for n in nodes} == {
        "com/example/model/Account#`<init>`().", "com/example/model/Group#add().",
        "com/example/app/Service#notArchived.", "com/example/app/Service#activate().",
        "com/example/app/Service#activateAll().", "com/example/app/ServiceTest#main().",
        "com/example/app/test/TestSupport#reset()."}
    tests = {n.symbol for n in nodes if n.test}
    assert tests == {"com/example/app/ServiceTest#main()."}
    deep, _ = graph.impact(SET_STATE, depth=3)
    assert "com/example/model/Account#Builder#build()." in {n.symbol for n in deep}


def test_impact_through_overridden_methods(graph):
    with_parents, _ = graph.impact("com/example/model/SubAccount#uid().", depth=1)
    plain, _ = graph.impact("com/example/model/SubAccount#uid().", depth=1,
                            include_overridden=False)
    assert "com/example/app/ServiceTest#main()." in {n.symbol for n in with_parents}
    assert "com/example/app/ServiceTest#main()." not in {n.symbol for n in plain}


def test_path(graph):
    paths = graph.path("com/example/app/Service#seed.", SET_STATE)
    assert [s for s, _ in paths[0]] == [
        "com/example/app/Service#seed.", "com/example/model/Account#Builder#build().",
        "com/example/model/Account#`<init>`().", SET_STATE]


def test_deps_and_cycles(graph):
    assert set(graph.deps("module")) == {("app", "core")}
    assert ("com.example.app", "com.example.model") in graph.deps("package")
    assert cycles([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")]) == [["a", "b", "c"]]
    sites, total = graph.uses_between("com.example.app", "com/example/model/")
    assert total >= len(sites) > 0
    assert any(c == "com/example/app/Service#activate()." and t == SET_STATE
               for c, t, _src, _l in sites)
    # Slashes or dots, and external targets with an explicit target.
    assert graph.deps("package", "com/example/app/", "com/example/model/") == \
        graph.deps("package", "com.example.app", "com.example.model")
    assert ("com.example.model", "java.util") in graph.deps("package", "com.example.model",
                                                           "java.util")


def test_dead_code(graph, sample_root):
    rows = {r[0] for r in graph.dead_code(root=sample_root)}
    assert "com/example/model/Point#scaled()." in rows
    assert "com/example/app/Service#" not in rows        # only `new Service()` mentions it
    assert "com/example/model/Point#x." not in rows      # read through the accessor x()
    assert "com/example/model/Account#uid()." not in rows  # an override
    assert "com/example/model/Snapshot#changedAt." not in rows  # Lombok getter in use
    assert not any("ServiceTest" in r for r in rows)     # tests are entry points
    assert "com/example/model/Store#load()." in rows
    scoped = {r[0] for r in graph.dead_code(scope="com.example.app", root=sample_root)}
    assert scoped and all(r.startswith("com/example/app/") for r in scoped)


def test_census_and_annotations(graph):
    census = {a: n for a, n, _f in graph.annotation_census()}
    # 6 @Override in the sources; the one on an anonymous class method has no global symbol.
    assert census["java/lang/Override#"] == 5 and census["lombok/Getter#"] == 1
    assert [x[0] for x in graph.annotated_with("java/lang/Deprecated#")] == [
        "com/example/model/Store#load()."]


def test_changed(tmp_path, sample_root, cache):
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    shutil.copytree(sample_root, repo)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                    capture_output=True)
    run("init", "-q")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    src = repo / "core/src/main/java/com/example/model/Entity.java"
    text = src.read_text().replace("this.state = state;", "this.state = state; // changed")
    src.write_text(text)
    # Simulate a compilation after the edit: the shard is newer than the source.
    shard = repo / ("build/scip-targetroot/core/compileJava/META-INF/scip/core/src/main/java/"
                    "com/example/model/Entity.java.scip")
    st = os.stat(src)
    os.utime(shard, (st.st_atime + 5, st.st_mtime + 5))
    # A javadoc line counts for the member it documents, not for the class (same line count,
    # so the shard still matches the source).
    text = src.read_text().replace("Changes the lifecycle state.", "Changes the state, now.")
    src.write_text(text)
    st = os.stat(src)
    os.utime(shard, (st.st_atime + 5, st.st_mtime + 5))
    out = Service(str(repo), cache_dir=cache).changed()
    assert "1 .java files, 1 definitions" in out, out
    assert "Entity#setState()" in out and "changed 10–15 (2 lines)" in out, out
    assert "Entity  " not in out and "class body" not in out
    assert "--tests 'com.example.app.ServiceTest'" in out
    # A class annotation changed: reported, not expanded to every user of the class.
    snap = repo / "core/src/main/java/com/example/model/Snapshot.java"
    snap.write_text(snap.read_text().replace("@Getter\n", "@Getter // reviewed\n", 1))
    snap_shard = repo / ("build/scip-targetroot/core/compileJava/META-INF/scip/core/src/main/"
                         "java/com/example/model/Snapshot.java.scip")
    st2 = os.stat(snap)
    os.utime(snap_shard, (st2.st_atime + 5, st2.st_mtime + 5))
    out = Service(str(repo), cache_dir=cache).changed()
    assert "Snapshot  core/src/main/java/com/example/model/Snapshot.java:8  class annotation " \
           "(framework behavior, not its API) changed (7)" in out, out
    new_test = repo / "app/src/test/java/com/example/app/NewTest.java"
    new_test.write_text("package com.example.app;\nclass NewTest {}\n")
    assert "untracked .java included" in Service(str(repo), cache_dir=cache).changed()
    os.utime(src, (st.st_atime + 50, st.st_mtime + 50))  # edited again, not recompiled
    assert "stale" in Service(str(repo), cache_dir=cache).changed()


def test_service_tools(sample_root, cache):
    svc = Service(sample_root, cache_dir=cache)
    assert "impact of Entity#setState()" in svc.impact("Entity#setState")
    assert "reached by 1 test classes" in svc.tests("Entity#setState", transitive=True)
    assert "1 shortest call path" in svc.path("Service#seed", "Entity#setState")
    assert "app → core" in svc.deps()
    assert "dead-code candidates" in svc.dead_code()
    assert "@Override" in svc.census()
    assert "auxiliary index:" in svc.cache_status()


def test_cache_dir_security(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(C.CacheError):
        C.ensure_private_dir(str(link))
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    C.ensure_private_dir(str(loose))
    assert (os.stat(loose).st_mode & 0o777) == 0o700
    a, b = C.entry_name("/x/proj"), C.entry_name("/y/proj")
    assert a != b and a.startswith("proj-") and b.startswith("proj-")


def test_prune_and_entries(sample_copy, cache):
    Index(Workspace(sample_copy), cache).update()
    entries = C.entries(cache)
    assert len(entries) == 1 and entries[0]["exists"]
    assert C.prune(cache) == []
    shutil.rmtree(sample_copy)
    removed = C.prune(cache)
    assert len(removed) == 1 and C.entries(cache) == []


def test_type_reported_inside_annotation_string(sample_root, cache, tmp_path):
    # scip-javac places Invoice at the "Invoice" string of @Label (line 7), above the
    # declaration (line 10): definitions, annotations and the census use the declaration.
    from scipq_mcp.queries import Navigator
    nav = Navigator(Workspace(sample_root))
    assert nav.definition("com/example/model/Invoice#").location.line == 10
    ix = Index(Workspace(sample_root), cache)
    ix.update()
    g = Graph(ix)
    assert [x[0] for x in g.annotated_with("com/example/model/Stored#")] == [
        "com/example/model/Invoice#"]
    g.close()
    home = tmp_path / "home"
    (home / "profiles").mkdir(parents=True)
    (home / "profiles" / "fixture.toml").write_text(
        '[profile]\nname = "fixture"\n[persistence]\nentity = ["com.example.model.Stored"]\n'
        'table = { annotation = ["com.example.model.Stored"], attribute = "table" }\n')
    (home / "config.toml").write_text(f'[[project]]\nmatch = {{ path = "{sample_root}" }}\n'
                                      'profiles = ["fixture"]\n')
    import os as _os
    old = _os.environ.get("XDG_CONFIG_HOME")
    _os.environ["XDG_CONFIG_HOME"] = str(tmp_path)
    try:
        (tmp_path / "scipq").symlink_to(home)
        out = Service(sample_root, cache_dir=cache).census(category="entities")
    finally:
        if old is None:
            del _os.environ["XDG_CONFIG_HOME"]
        else:
            _os.environ["XDG_CONFIG_HOME"] = old
    assert "Invoice  INVOICES  core/src/main/java/com/example/model/Invoice.java:10" in out


def test_dead_code_scopes(sample_root, cache):
    svc = Service(sample_root, cache_dir=cache)
    for scope in ("com.example.app", "com/example/app", "app", "app/src", "*/app/*"):
        out = svc.dead_code(scope=scope)
        assert "Report.java" in out and "Point.java" not in out, (scope, out)
    assert "matches no definition" in svc.dead_code(scope="nope")


def test_dead_code_names_in_other_files(sample_copy, cache):
    svc = Service(sample_copy, cache_dir=cache)
    out = svc.dead_code(scope="com.example.model")
    assert "public int getSize()" in out and "low confidence" not in out
    # A JSON file using the bean property: the getter becomes a low-confidence candidate.
    with open(os.path.join(sample_copy, "data.json"), "w") as fh:
        fh.write('{"size": 1}\n')
    out = svc.dead_code(scope="com.example.model")
    main, _, weak = out.partition("low confidence")
    assert "public int getSize()" not in main
    assert 'Group#getSize()  ("size" in data.json)' in weak
    # The exact method name in a Java string literal: omitted, and the answer says where.
    with open(os.path.join(sample_copy, "app/src/main/java/com/example/app/Reflect.java"),
              "w") as fh:
        fh.write('class Reflect { String m = "getSize"; }\n')
    out = svc.dead_code(scope="com.example.model")
    assert "getSize" not in out.replace("app/src/main/java/com/example/app/Reflect.java", "")
    assert "app/src/main/java/com/example/app/Reflect.java 1" in out


def test_truncated_shard_is_skipped_and_retried(sample_copy, cache):
    shard = os.path.join(sample_copy, "build/scip-targetroot/core/compileJava/META-INF/scip/"
                                      "core/src/main/java/com/example/model/Group.java.scip")
    data = open(shard, "rb").read()
    with open(shard, "wb") as fh:  # a compilation is writing it right now
        fh.write(data[: len(data) // 2])
    out = Service(sample_copy, cache_dir=cache).references("Entity#setState")
    assert "Group.java" not in out.split("WARNING")[0]
    assert "1 shard could not be read" in out and "Group.java" in out
    ix = Index(Workspace(sample_copy), cache)
    st = ix.update()
    assert st.unreadable == 1
    with open(shard, "wb") as fh:  # the compilation finished
        fh.write(data)
    assert Index(Workspace(sample_copy), cache).update().processed >= 1
    assert "Group.java" in Service(sample_copy, cache_dir=cache).references("Entity#setState")


def test_recently_written_shards_are_reported(sample_copy, cache):
    """A shard cut short can drop subtypes and references without any error (the query never
    reads it): shards written in the last minute make the answer say so."""
    shard = os.path.join(sample_copy, "build/scip-targetroot/core/compileJava/META-INF/scip/"
                                      "core/src/main/java/com/example/model/Account.java.scip")
    data = open(shard, "rb").read()
    with open(shard, "wb") as fh:  # being written now: its mtime is now
        fh.write(data[: len(data) // 3])
    out = Service(sample_copy, cache_dir=cache).implementations("Entity")
    assert "1 shard written in the last 60 s" in out
    os.utime(shard, (1_700_000_100, 1_700_000_100))
    assert "written in the last" not in Service(sample_copy, cache_dir=cache).implementations(
        "Entity")


def test_low_priority_commands():
    from scipq_mcp.index import default_workers, low_priority
    cmd = low_priority(["grep", "x"])
    assert cmd[-2:] == ["grep", "x"]
    step = 10 - os.getpriority(os.PRIO_PROCESS, 0)
    if shutil.which("nice") and step > 0:
        assert cmd[:3] == [shutil.which("nice"), "-n", str(step)]
    assert 1 <= default_workers() <= 4


def test_whole_process_runs_at_low_priority(tmp_path):
    """serve and query lower their own priority at startup; children are not niced again."""
    import subprocess
    import sys
    script = tmp_path / "prio.py"
    script.write_text(
        "import os, shutil\n"
        "from scipq_mcp.index import lower_own_priority, low_priority\n"
        "lower_own_priority()\n"
        "print(os.getpriority(os.PRIO_PROCESS, 0))\n"
        "print(shutil.which('nice') in low_priority(['true']))\n")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         check=True).stdout.split()
    assert int(out[0]) >= 10 and out[1] == "False"


def test_dead_code_pages_low_confidence_too():
    from scipq_mcp import render
    rows = [(f"a/B#m{i}().", "method", "B.java", i, f"void m{i}()") for i in range(3)]
    weak = [(f"a/B#getP{i}().", "method", "B.java", 10 + i, "", "x.json", f"p{i}")
            for i in range(40)]
    first = render.dead_code(rows, 0, 5, weak=weak)
    assert "showing 1–5 of 43" in first and "next offset 5" in first
    assert "B#getP1()" in first and "B#getP2()" not in first
    rest = render.dead_code(rows, 5, 5000, weak=weak)
    assert "B#m0()" not in rest and "B#getP2()" in rest and "B#getP39()" in rest
    assert "next offset" not in rest
    later = render.dead_code(rows, 0, 2, weak=weak)
    assert "follow at offset 3" in later


def test_recent_shards_with_and_without_a_build_running():
    from scipq_mcp import render
    from scipq_mcp.workspace import Freshness, _is_build_command
    assert _is_build_command(b"/usr/bin/java\0-cp\0w.jar\0org.gradle.wrapper.GradleWrapperMain\0"
                             b"build\0")
    assert _is_build_command(b"/opt/maven/bin/mvnd\0compile\0")
    assert not _is_build_command(b"bash\0-c\0grep org.gradle.wrapper.GradleWrapperMain x\0")
    done = render.freshness(Freshness(recent=3, newest_age=12, build_running=False))
    assert done.startswith("note: 3 shards") and "probably just finished" in done
    running = render.freshness(Freshness(recent=3, build_running=True))
    assert running.startswith("WARNING: a gradle/mvn command is running")
    unknown = render.freshness(Freshness(recent=1, build_running=None))
    assert unknown.startswith("WARNING: 1 shard written") and "if a compilation" in unknown


def test_scratch_build_goes_through_a_temporary_file(sample_copy, cache):
    ix = Index(Workspace(sample_copy), cache)
    assert ix.update().built_from_scratch
    assert os.path.exists(ix.path) and not os.path.exists(ix.path + ".building")
    con = ix.connect()
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("SELECT count(*) FROM shards").fetchone()[0] > 0
    finally:
        con.close()
    assert not ix.update().built_from_scratch
    con = sqlite3.connect(ix.path)  # another schema: rebuilt from scratch, in place
    con.execute("UPDATE meta SET value = 'old' WHERE key = 'schema_version'")
    con.commit()
    con.close()
    assert Index(Workspace(sample_copy), cache).update().built_from_scratch


def test_dead_code_ignores_names_in_documentation(sample_copy, cache):
    os.makedirs(os.path.join(sample_copy, "docs", "notes"))
    with open(os.path.join(sample_copy, "docs", "notes", "dump.json"), "w") as fh:
        fh.write('{"size": 1, "getSize": 2}\n')
    out = Service(sample_copy, cache_dir=cache).dead_code(scope="com.example.model")
    assert "public int getSize()" in out.partition("low confidence")[0]


def test_build_directories_from_the_command_line(tmp_path):
    from scipq_mcp.workspace import _build_dirs, _is_build_command, _overlaps
    root = str(tmp_path / "repo")
    os.makedirs(os.path.join(root, "sub"))
    assert _is_build_command(b"java\0-cp\0m.jar\0org.apache.maven.wrapper.MavenWrapperMain\0"
                             b"compile\0")
    dirs = _build_dirs(b"java\0org.gradle.wrapper.GradleWrapperMain\0-p\0repo/sub\0build\0",
                       str(tmp_path))
    assert any(_overlaps(d, root) for d in dirs)
    assert _overlaps(str(tmp_path), root)          # a build in a parent directory
    assert not _overlaps(str(tmp_path / "other"), root)


def test_changed_when_the_build_is_a_subdirectory_of_the_repository(tmp_path, sample_root,
                                                                     cache):
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo = tmp_path / "mono"
    shutil.copytree(sample_root, repo / "java")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                    capture_output=True)
    run("init", "-q")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    src = repo / "java/core/src/main/java/com/example/model/Entity.java"
    src.write_text(src.read_text().replace("this.state = state;", "this.state = state; // x"))
    shard = repo / ("java/build/scip-targetroot/core/compileJava/META-INF/scip/core/src/main/"
                    "java/com/example/model/Entity.java.scip")
    st = os.stat(src)
    os.utime(shard, (st.st_atime + 5, st.st_mtime + 5))
    out = Service(str(repo / "java"), cache_dir=cache).changed()
    assert "Entity#setState()" in out and "not indexed" not in out, out


def test_external_pruning_waits_for_unreadable_shards(sample_copy, cache):
    shard = os.path.join(sample_copy, "build/scip-targetroot/core/compileJava/META-INF/scip/"
                                      "core/src/main/java/com/example/model/Entity.java.scip")
    data = open(shard, "rb").read()
    with open(shard, "wb") as fh:  # being written: Entity# looks undefined right now
        fh.write(data[: len(data) // 3])

    def entity_refs(ix):
        con = ix.connect()
        try:
            return con.execute("SELECT count(*) FROM refs r JOIN symbols s ON s.id = r.symbol_id "
                               "WHERE s.symbol = 'com/example/model/Entity#'").fetchone()[0]
        finally:
            con.close()

    ix = Index(Workspace(sample_copy), cache, external="methods")
    assert ix.update().unreadable == 1
    kept = entity_refs(ix)
    assert kept > 0                      # not pruned as external while unreadable
    with open(shard, "wb") as fh:
        fh.write(data)
    ix.update()
    assert entity_refs(ix) >= kept       # defined again (plus its own): none lost


def test_by_name_skips_hidden_directories_and_lockfiles(tmp_path):
    from scipq_mcp.graph import candidate_files
    for p in (".github/workflows/ci.yaml", "web/.config/package-lock.json", "yarn.lock",
              "app/src/main/resources/app.yaml", "web/package-lock.json"):
        os.makedirs(os.path.dirname(os.path.join(tmp_path, p)) or str(tmp_path), exist_ok=True)
        with open(os.path.join(tmp_path, p), "w") as fh:
            fh.write("x\n")
    assert candidate_files(str(tmp_path), ["*.yaml", "*.json", "*.lock"]) == [
        "app/src/main/resources/app.yaml"]


def test_git_diff_names_with_spaces_and_accents(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    from scipq_mcp.compare import changed_files
    from scipq_mcp.graph import git_diff_hunks
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    run = lambda *a: subprocess.run(git + list(a), check=True, capture_output=True)  # noqa
    run("init", "-q")
    names = ["src/My Report.java", "src/Año.java", "docs/notes (draft).md"]
    for n in names:
        os.makedirs(os.path.dirname(tmp_path / n), exist_ok=True)
        (tmp_path / n).write_text("a\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    for n in names:
        (tmp_path / n).write_text("a\nb\n")
    assert set(git_diff_hunks(str(tmp_path), "HEAD", False, [])) == set(names)
    assert changed_files(str(tmp_path), "HEAD") == set(names)


@pytest.mark.parametrize("gnu_find", [True, False])
def test_a_file_written_again_unchanged_is_not_stale(sample_copy, tmp_path, monkeypatch,
                                                     gnu_find):
    """A build up to date by content (Gradle) leaves the shard older than a file written
    again with the same text: the index's hash of the compiled source tells it apart."""
    from scipq_mcp import workspace as W
    from scipq_mcp.service import Service
    if not gnu_find:   # the pure-Python check (BSD find on macOS)
        monkeypatch.setattr(W, "_gnu_find", lambda: None)
    elif W._gnu_find() is None:
        pytest.skip("GNU find not available")
    svc = Service(sample_copy, cache_dir=str(tmp_path / "cache"))
    svc.census()                                       # builds the auxiliary index
    src = os.path.join(sample_copy, "core/src/main/java/com/example/model/Entity.java")
    text = open(src, encoding="utf-8").read()
    open(src, "w", encoding="utf-8").write(text)       # same content, newer mtime
    ws = svc.workspace(None)
    assert ws.freshness().stale == []
    assert ws.freshness_of(["core/src/main/java/com/example/model/Entity.java"]).stale == []
    assert "not counted: 1 files" in svc.index_status()
    open(src, "w", encoding="utf-8").write(text + "// edited\n")
    assert ws.freshness().stale == ["core/src/main/java/com/example/model/Entity.java"]
    # Without the auxiliary index, mtimes decide as before.
    other = Service(sample_copy, cache_dir=str(tmp_path / "empty"))
    open(src, "w", encoding="utf-8").write(text)
    assert other.workspace(None).freshness().stale == [
        "core/src/main/java/com/example/model/Entity.java"]


def test_concepts(sample_copy, tmp_path):
    from scipq_mcp.service import Service
    svc = Service(sample_copy, cache_dir=str(tmp_path / "cache"))
    out = svc.concepts("lifecycle states")      # javadoc words; a plural finds the singular
    assert out.index("Entity#setState()") < out.index("Entity#getState()")
    assert "— Changes the lifecycle state." in out
    assert "Store#load()" in svc.concepts("deprecated")                 # its annotation
    assert "Session#find()" in svc.concepts("session find", kind="method")
    assert "Session#find()" not in svc.concepts("session find", kind="type")
    assert "ServiceTest" not in svc.concepts("service test") and \
        "ServiceTest" in svc.concepts("service test", include_tests=True)
    assert "no words to search" in svc.concepts("de la")
    # Alternatives: each group is one word of the query, its forms searched as written.
    out = svc.concepts("lifecycle|lifecycles state|status")
    assert "definitions about lifecycle* state*|status*" in out   # lifecycles: one word
    assert out.index("Entity#setState()") < out.index("Entity#getState()")
    assert "write a word's forms" not in out and "write a word's forms" in svc.concepts("state")


def test_concept_stems_and_prefix_families():
    from scipq_mcp.graph import concept_terms, prefix_family, stem
    assert stem("facturas") == "factura" and prefix_family("factura") == ["factura"]
    assert stem("clases") == "clase" and stem("states") == "state"    # -s, not -es
    assert stem("funciones") == stem("función") == "func"
    assert stem("exportación") == stem("exportar") == stem("exporter") == "export"
    fam = prefix_family("export")
    assert "exportac" in fam and "exporter" in fam and "export" not in fam   # not "exporte"
    assert prefix_family(stem("entities")) == ["entity", "entitie"]
    assert concept_terms("factura|facturas pedido|pedidos") == [["factura"], ["pedido"]]
    assert prefix_family(stem("series")) == ["sery", "serie"]
    assert concept_terms("envío de la facturaPendiente") == [["envio"], ["factura"], ["pendiente"]]
    # Forms given as alternatives are searched as written (one word of the query each).
    assert concept_terms("Pago|pagar|payment de pedidos") == [
        ["pago", "pagar", "payment"], ["pedido"]]
    assert concept_terms("exportación|export") == [["export"]]   # export covers both


def test_generated_headers_only_in_the_banner_or_an_annotation():
    from scipq_mcp.graph import generated_header
    assert generated_header(b"/*\n * This file is generated by jOOQ.\n */\npackage a;\n\n"
                            b"public class T {}\n")
    assert generated_header("//\n// Este archivo ha sido generado por la arquitectura JAXB\n"
                            "//\npackage a;\n\npublic class T {}\n".encode())
    assert generated_header(b'package a;\n\nimport x.Y;\n\n@Generated("jsonschema2pojo")\n'
                            b'public class T {}\n')
    # Prose in the type's javadoc is not a generator's notice.
    assert not generated_header("package a;\n\n/**\n * Sirve el CSS generado por el editor;\n"
                                " * see {@code @Generated} too.\n */\npublic class T {}\n"
                                .encode())
    assert not generated_header(b"/**\n * The CSS generated by the theme editor.\n */\n"
                                b"public class T {}\n")

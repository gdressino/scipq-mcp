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
    assert first.built_from_scratch and first.processed == 14 and first.shards == 15
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
    assert st["shards"] == 14 and st["refs"] > 0


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
    sites = graph.uses_between("com.example.app", "com.example.model")
    assert any(c == "com/example/app/Service#activate()." and t == SET_STATE
               for c, t, _src, _l in sites)


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
    out = Service(str(repo), cache_dir=cache).changed()
    assert "1 files, 1 definitions" in out
    assert "Entity#setState()" in out and "changed 15" in out
    assert "--tests 'com.example.app.ServiceTest'" in out
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

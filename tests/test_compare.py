import os
import shutil
import subprocess

import pytest

from scipq_mcp import compare as CMP
from scipq_mcp.service import Service


def test_api_diff_and_renames():
    E = CMP.Entry
    a = {"p/A#run().": E("method", "public void run()", "A.java", 3),
         "p/A#old().": E("method", "public int old(String s)", "A.java", 5),
         "p/A#size().": E("method", "public int size()", "A.java", 7)}
    b = {"p/A#run().": E("method", "public void run(int n)", "A.java", 3),
         "p/A#fresh().": E("method", "public int fresh(String s)", "A.java", 5),
         "p/A#size().": E("method", "public int size()", "A.java", 9)}
    d = CMP.api_diff(a, b)
    assert set(d.changed) == {"p/A#run()."}          # a moved line is not a change
    assert d.renamed == [("p/A#old().", "p/A#fresh().")]


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_compare_worktrees(tmp_path, sample_root):
    a = tmp_path / "a"
    shutil.copytree(sample_root, a)
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    run = lambda root, *x: subprocess.run(git + ["-C", str(root), *x], check=True,  # noqa
                                          capture_output=True)
    run(a, "init", "-q")
    run(a, "add", "-A")
    run(a, "commit", "-qm", "base")
    b = tmp_path / "b"
    run(a, "worktree", "add", "-q", "-b", "feature", str(b))
    # B removes Group (source and shard), which A uses.
    for p in ("core/src/main/java/com/example/model/Group.java",
              "build/scip-targetroot/core/compileJava/META-INF/scip/core/src/main/java/"
              "com/example/model/Group.java.scip"):
        os.unlink(b / p)
    run(b, "commit", "-qam", "drop Group")
    svc = Service(str(a), cache_dir=str(tmp_path / "cache"))
    out = svc.compare(str(b))
    assert "removed in B" in out and "Group#add()" in out   # classified against the base
    assert "since the merge base" in out and "B 2 files (1 .java)" in out
    # A did not touch Service.java: after the merge it is B's version, adapted by B.
    assert "merging B into A would break, in files A changed since the base: none found" in out
    # A edits Service.java (which uses Group): that version survives the merge and breaks.
    svc_java = a / "app/src/main/java/com/example/app/Service.java"
    svc_java.write_text(svc_java.read_text() + "// edited in A\n")
    out = svc.compare(str(b))
    brk = out.split("merging B into A would break, in files A changed since the base", 1)[1]
    assert "Group" in brk and "Service.java" in brk.split("merging A into B")[0]
    assert "merging A into B would break, in files B changed since the base: none found" in out
    every = svc.compare(all_worktrees=True)
    assert "feature" in every and "merging it here breaks" in every, every


def test_classify_against_the_merge_base():
    E = CMP.Entry
    d = CMP.ApiDiff(only_a={"p/A#n().": E("method", "public void n()", "A.java", 1),
                            "p/B#gone().": E("method", "public void gone()", "B.java", 1)},
                    only_b={"p/C#c().": E("method", "public void c()", "C.java", 1)},
                    changed={"p/D#d().": (E("method", "public int d()", "D.java", 1),
                                          E("method", "public long d()", "D.java", 1))})
    g = CMP.classify(d, files_a={"A.java"}, files_b={"B.java", "C.java", "D.java"})
    assert set(g["added in A"]) == {"p/A#n()."} and set(g["removed in B"]) == {"p/B#gone()."}
    assert set(g["added in B"]) == {"p/C#c()."} and set(g["changed in B"]) == {"p/D#d()."}

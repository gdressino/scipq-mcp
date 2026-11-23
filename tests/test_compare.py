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
    out = Service(str(a), cache_dir=str(tmp_path / "cache")).compare(str(b))
    every = Service(str(a), cache_dir=str(tmp_path / "cache")).compare(all_worktrees=True)
    assert "feature" in every and "merging it here breaks" in every, every
    assert "only in A" in out and "Group#add()" in out
    assert "since the merge base" in out
    brk = out.split("merging B into A would break uses in A:", 1)[1]
    assert "Group" in brk and "Service.java" in brk
    assert "merging A into B would break uses in B: none found" in out

import os
import shutil
import subprocess

import pytest

from scipq_mcp import gitref as G
from scipq_mcp.queries import QueryError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="no git")


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(sample_copy):
    """The sample in a git repository whose `other` branch adds a class never compiled here,
    and changes Account."""
    _git(sample_copy, "init", "-q", "-b", "main")
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-q", "-m", "base")
    _git(sample_copy, "checkout", "-q", "-b", "other")
    model = os.path.join(sample_copy, "core/src/main/java/com/example/model")
    with open(os.path.join(model, "Ledger.java"), "w") as fh:
        fh.write("package com.example.model;\n\n/** Entries. */\npublic class Ledger {\n"
                 "    static final int LIMIT = 500;\n\n"
                 "    public int total(int a, int b) {\n        return a + b;\n    }\n\n"
                 "    public int total() {\n        return LIMIT;\n    }\n}\n")
    _git(sample_copy, "add", "-A")
    _git(sample_copy, "commit", "-q", "-m", "ledger")
    _git(sample_copy, "checkout", "-q", "main")
    return sample_copy


def test_wanted_names():
    w = G.wanted("com.example.Account#setState(int)")
    assert (w.package, w.types, w.member, w.method, w.params) == (
        ["com", "example"], ["Account"], "setState", True, ("int",))
    assert G.wanted("Outer.Inner#x").types == ["Outer", "Inner"]
    assert G.wanted("Account.setState").member == "setState"
    assert G.wanted("Account#Account(String)").member == "<init>"
    w = G.wanted("com/example/Account#setState(+1).")
    assert (w.types, w.member, w.overload) == (["Account"], "setState", 1)
    assert G.wanted("Account#pick(+2)").overload == 2
    with pytest.raises(QueryError):
        G.wanted("#x")


def test_source_of_code_only_in_another_ref(repo):
    from scipq_mcp.service import Service
    svc = Service(repo)
    out = svc.source("Ledger#total(int, int)", git_ref="other")
    assert "read from git at other" in out and \
        "core/src/main/java/com/example/model/Ledger.java" in out
    assert "Ledger#total(int, int) — core/src/main/java/com/example/model/Ledger.java:7–9" in out
    assert "\n8          return a + b;" in out and "return LIMIT" not in out
    both = svc.source("Ledger#total", git_ref="other")
    assert "2 overloads" in both and "return LIMIT" in both and "return a + b" in both
    assert "static final int LIMIT = 500;" in svc.source("Ledger#LIMIT", git_ref="other")
    whole = svc.source("com.example.model.Ledger", git_ref="other", max_lines=3)
    assert "Ledger — core/src/main/java/com/example/model/Ledger.java:4–14" in whole
    assert "… truncated; the body ends at line 14" in whole
    # Not in main, and not compiled here.
    assert "error: no file declares Ledger" in svc.source("Ledger", git_ref="main")
    assert "no such branch" in svc.source("Ledger", git_ref="nope")
    assert "is not a branch" in svc.source("Ledger", git_ref="--output=x")
    assert "Ledger has no totl" in svc.source("Ledger#totl", git_ref="other")
    assert "none takes (String); all 2 there" in svc.source("Ledger#total(String)",
                                                            git_ref="other")


def test_wanted_constructor_with_a_dot_and_parameters():
    w = G.wanted("Account.Account(String)")
    assert (w.types, w.member, w.params) == (["Account"], "<init>", ("String",))
    with pytest.raises(QueryError, match="parameter"):
        G.wanted("com/x/A#m().(arg)")


def test_git_ref_without_shards_and_without_the_checkouts_warning(repo, tmp_path):
    from scipq_mcp.service import Service
    # A file edited in the checkout: its warning is about the compilation, not the ref.
    account = os.path.join(repo, "core/src/main/java/com/example/model/Account.java")
    os.utime(account, (1_700_000_500, 1_700_000_500))
    out = Service(repo).source("Ledger#LIMIT", git_ref="other")
    assert "LIMIT = 500" in out and "WARNING" not in out and "note:" not in out
    # No shards at all: git alone reads it.
    bare = tmp_path / "bare"
    (bare / "src").mkdir(parents=True)
    (bare / "src" / "Tool.java").write_text("class Tool {\n    int x = 1;\n}\n")
    _git(bare, "init", "-q", "-b", "main")
    _git(bare, "add", "-A")
    _git(bare, "commit", "-q", "-m", "tool")
    out = Service(str(bare)).source("Tool#x", git_ref="main")
    assert "int x = 1;" in out and "src/Tool.java" in out
    # A type declared in a file named otherwise is found by the one file declaring it.
    (bare / "src" / "Tools.java").write_text("class Tools {}\nclass Helper {\n    int y;\n}\n")
    _git(bare, "add", "-A")
    _git(bare, "commit", "-q", "-m", "helper")
    out = Service(str(bare)).source("Helper#y", git_ref="main")
    assert "int y;" in out and "src/Tools.java" in out

import os
import shutil
import subprocess

import pytest

from scipq_mcp import edits as E


def test_positions_count_utf16_like_javac():
    t = E.Text('a = "😀"; foo(y);\nnext\n')
    # JavaParser/javac columns are UTF-16 units: the emoji is two of them.
    s, e = t.span([1, 11, 1, 16])
    assert t.text[s:e] == "foo(y)"
    assert t.position(t.text.index("foo")) == (1, 11)
    assert t.end_position(t.text.index("😀") + 1) == (1, 7)
    assert t.scip(1, 0) == t.text.index("next")


def test_nested_edits_compose_inside_out():
    src = "x = old(a.get(1), b);"
    call = (src.index("old"), src.index(";"))
    arg0 = (src.index("a.get"), src.index(", b"))
    get = (src.index("get"), src.index("get") + 3)
    edits = [
        E.Edit(*call, [E.Piece("Runner.run("), E.Slot(*arg0), E.Piece(")")], 1, "replace",
               kind="Expression"),
        E.Edit(*get, [E.Piece("fetch", "Identifier")], 2, "rename"),
    ]
    assert E.check_overlaps(edits) == []
    r = E.render(src, edits)
    assert r.text == "x = Runner.run(a.fetch(1));"
    assert not r.dropped
    kinds = {p.kind: r.text[p.start:p.end] for p in r.placed}
    assert kinds == {"Identifier": "fetch", "Expression": "Runner.run(a.fetch(1))"}
    # `, b` was not kept in a slot: it is removed text.
    assert any(src[s:e] == ", b)" for s, e in r.removed)


def test_edits_dropped_overlapping_and_insertions():
    src = "f(a, b);"
    whole = E.Edit(0, 7, [E.Piece("g()")], 1, "replace")
    inner = E.Edit(2, 3, [E.Piece("z")], 2, "rename")
    r = E.render(src, [whole, inner])
    assert r.text == "g();" and r.dropped == [inner]
    assert E.check_overlaps([E.Edit(0, 3, [], 1, "x"), E.Edit(2, 5, [], 2, "y")])
    assert E.check_overlaps([E.Edit(2, 3, [E.Piece("p")], 1, "x"),
                             E.Edit(2, 3, [E.Piece("q")], 2, "y")])
    same = [E.Edit(2, 3, [E.Piece("p")], 1, "x"), E.Edit(2, 3, [E.Piece("p")], 2, "y")]
    assert len(E.dedupe(same)) == 1
    # An insertion at an edit's start goes before it; inside a slot, it stays inside.
    ins = E.Edit(2, 2, [E.Piece("q, ")], 3, "insert")
    reorder = E.Edit(2, 6, [E.Slot(5, 6), E.Piece(", "), E.Slot(2, 3)], 4, "reorder")
    assert E.render(src, [ins, reorder]).text == "f(q, b, a);"


def test_templates_and_reindent():
    slots = {"$recv": E.Slot(0, 1), "$0": E.Slot(2, 3), "$args": ""}
    parts = E.template_parts('Util.run($recv, $0, "$0 kept")', slots)
    assert [type(p).__name__ for p in parts] == ["Piece", "Slot", "Piece", "Slot", "Piece"]
    with pytest.raises(E.EditError):
        E.template_parts("$nope", slots)
    with pytest.raises(E.EditError):
        E.template_parts("$recv.x()", {"$recv": None})
    assert E.reindent("if (x) {\n    y();\n}", "\t\t", "\t") == "if (x) {\n\t\t\ty();\n\t\t}"
    assert E.reindent("if (x) {\n  y();\n}", "    ", "    ", first=True) == \
        "    if (x) {\n        y();\n    }"
    assert E.indent_unit("class A {\n\tint x;\n\t\tint y;\n}\n") == "\t"
    assert E.indent_unit("class A {\n  int x;\n    int y;\n}\n") == "  "


def test_patches_apply_with_git(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    root = tmp_path / "repo"
    (root / "a").mkdir(parents=True)
    (root / "a" / "Old.java").write_bytes(b"class Old {\r\n}\r\n")
    (root / "a" / "ñ.java").write_bytes(b"class N {}")          # no final newline
    git = ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(git + args, check=True, capture_output=True)
    old = E.read_source(str(root), "a/Old.java")
    n = E.read_source(str(root), "a/ñ.java")
    patch = E.file_patch(old, "a/New.java", old.text.replace("Old", "New"))
    patch += E.file_patch(n, n.path, "class N { int x; }")
    patch += E.file_patch(None, "b/Made.java", "class Made {}\n", E.Source("b/Made.java", b"", ""))
    p = tmp_path / "p.diff"
    p.write_bytes(patch)
    subprocess.run(git + ["apply", "--check", str(p)], check=True, capture_output=True)
    subprocess.run(git + ["apply", str(p)], check=True, capture_output=True)
    assert (root / "a" / "New.java").read_bytes() == b"class New {\r\n}\r\n"   # CRLF kept
    assert not os.path.exists(root / "a" / "Old.java")
    assert (root / "a" / "ñ.java").read_bytes() == b"class N { int x; }"
    assert (root / "b" / "Made.java").read_text() == "class Made {}\n"
    # Full blob ids: a 3-way apply works on top of a later commit.
    assert E.blob_id(b"class Old {\r\n}\r\n") in patch.decode()

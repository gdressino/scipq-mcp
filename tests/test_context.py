from scipq_mcp import context as C
from scipq_mcp.service import Service


def test_method_context(sample_copy, tmp_path):
    out = Service(sample_copy, cache_dir=str(tmp_path / "cache")).context("Entity#setState")
    head, _, rest = out.partition("\nbody:\n")
    assert "method at core/src/main/java/com/example/model/Entity.java:14" in head
    assert "Changes the lifecycle state." in head
    assert "impact needs the auxiliary index" in head  # never built by context
    assert "this.state = state;" in rest
    callers = rest.split("callers (", 1)[1]
    assert callers.startswith("7 in 5 files, 1 in tests)")
    # non-test callers first, the test one last and marked
    rows = [x for x in callers.split("\n\n")[0].splitlines()[1:]]
    assert rows[-1].endswith("[test]") and "ServiceTest#main()" in rows[-1]
    assert "gradle :app:test --tests 'com.example.app.ServiceTest'" in out


def test_context_uses_an_existing_index_for_impact(sample_copy, tmp_path):
    svc = Service(sample_copy, cache_dir=str(tmp_path / "cache"))
    svc.impact("Entity#setState")  # builds it
    out = svc.context("Entity#setState")
    assert "impact (depth 3):" in out and "→ impact(Entity#setState())" in out


def test_type_and_field_context(sample_copy, tmp_path):
    svc = Service(sample_copy, cache_dir=str(tmp_path / "cache"))
    t = svc.context("Entity")
    assert "members (6):" in t and "subtypes (3, 2 direct):" in t
    assert "used in 9 files" in t
    f = svc.context("Account#number")
    assert "writes (1; told apart by the line's text" in f and "this.number = number;" in f
    assert "reads (1):" in f
    lombok = svc.context("Snapshot#archived")
    assert "Snapshot#isArchived()" in lombok and "(generated)" in lombok


def test_context_fits_the_budget_and_does_not_pad(sample_copy, tmp_path):
    svc = Service(sample_copy, cache_dir=str(tmp_path / "cache"))
    small = svc.context("Entity#setState", token_budget=250)
    assert len(small) <= 250 * C.CHARS_PER_TOKEN + 200
    assert "→ callers(Entity#setState())" in small
    big = svc.context("Group#getSize", token_budget=32000)
    assert len(big) < 1500 and "callers: none in this project" in big


def test_cut_keeps_head_and_tail():
    s = C.Section("body", "body:", [f"{k:>3}  line {k}" for k in range(1, 101)], "source(x)",
                  C.BODY_TAIL)
    out = C._cut(s, 400)
    assert out[0] == "body:" and out[1].endswith("line 1") and out[-1].endswith("line 100")
    assert any("lines omitted → source(x)" in x for x in out)
    assert C._cut(s, 10) == ["body → source(x)"]


def test_is_write_at_the_reference_column():
    def w(line, name, nth=0, nxt=""):
        k = -1
        for _ in range(nth + 1):
            k = line.index(name, k + 1)
        return C.is_write_at(line, k, k + len(name), nxt)

    assert w("this.count = 0;", "count") and w("count++;", "count") and w("++this.count;", "count")
    assert w("total += count; count -= 1", "count", 1) and not w("total += count;", "count")
    assert not w("if (count == 0) return;", "count") and not w("x = count;", "count")
    assert w("this.state = other.state;", "state") and not w("this.state = other.state;",
                                                             "state", 1)
    line = "if (cache == null) cache = new Cache(); return cache;"
    assert [w(line, "cache", k) for k in range(3)] == [False, True, False]
    assert w("        state", "state", nxt="            = null;")  # the operator on the next line


def test_accessor_symbols():
    got = C.accessor_symbols("a/B#isActive.")
    assert "a/B#isActive()." in got and "a/B#setActive()." in got   # Lombok on boolean isX
    assert "a/B#name()." in C.accessor_symbols("a/B#name.")         # record component


def test_context_never_touches_an_index_of_another_schema(sample_copy, tmp_path):
    import sqlite3
    from scipq_mcp.index import Index
    from scipq_mcp.workspace import Workspace
    svc = Service(sample_copy, cache_dir=str(tmp_path / "cache"))
    svc.impact("Entity#setState")
    ix = Index(Workspace(sample_copy), str(tmp_path / "cache"))
    con = sqlite3.connect(ix.path)
    con.execute("UPDATE meta SET value = 'old' WHERE key = 'schema_version'")
    con.commit()
    con.close()
    out = svc.context("Entity#setState")
    assert "impact needs the auxiliary index" in out
    con = sqlite3.connect(ix.path)  # still there, untouched
    assert con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "old"
    assert con.execute("SELECT count(*) FROM shards").fetchone()[0] > 0
    con.close()


def test_record_component_context_includes_its_implicit_accessor(sample_copy, tmp_path):
    out = Service(sample_copy, cache_dir=str(tmp_path / "cache")).context("Point#x")
    assert "Point#x()  (implicit: a record component)" in out
    assert "return new Point(x() * k, y * k);  (x)" in out

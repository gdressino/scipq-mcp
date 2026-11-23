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


def test_is_write():
    assert C.is_write("this.count = 0;", "count") and C.is_write("count++;", "count")
    assert C.is_write("total += count; count -= 1", "count")
    assert not C.is_write("if (count == 0) return;", "count")
    assert not C.is_write("x = count;", "count")

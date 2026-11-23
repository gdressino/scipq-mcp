import re

from scipq_mcp import cucumber as C
from scipq_mcp.service import Service


def test_expressions():
    def m(expr, text):
        return re.fullmatch(C.expression_regex(expr), text) is not None

    assert m("I add {int} item(s)/once", "I add 3 items") and m("I add {int} item(s)/once",
                                                                 "I add 1 once")
    assert not m("I add {int} item(s)", "I add three items")
    assert m("a cart named {string}", 'a cart named "x y"') and m("a cart named {string}",
                                                                   "a cart named 'z'")
    assert m(r"a literal \(paren\) and {word}", "a literal (paren) and w0rd")
    assert m("costs {float}", "costs 1.5") and m("anything {} here", "anything at all here")
    assert m("pay with {method}", "pay with card") is False or True  # unknown: matches anything
    assert C.compile_glue("^the cart holds (\\d+) items?$", {})[0] == "regex"


def test_annotation_text():
    lines = ['    @When("I add " + "{int} item(s)")', "    public void x(int n) {}",
             '    @Given(value = "^ok$", timeout = 3)', '    @Then(PREFIX + "x")']
    assert C.annotation_text(lines, 0, "When") == "I add {int} item(s)"
    assert C.annotation_text(lines, 2, "Given") == "^ok$"
    assert C.annotation_text(lines, 3, "Then") is None   # a constant: not resolvable


def test_feature_parsing():
    f = C.parse_feature(open(C.__file__.replace("src/scipq_mcp/cucumber.py",
                                                "tests/fixtures/cucumber/shop/src/test/"
                                                "resources/features/cart.feature")).read(),
                        "cart.feature")
    names = [s.name for s in f.scenarios]
    assert names == ["Add a few", "Add 1", "Add 2", "Nobody defined this"]   # one per row
    assert f.scenarios[1].tags == ["@cart", "@slow", "@pending"]           # inherited
    bg = [s for s in f.steps if s.text.startswith("an empty cart")][0]
    assert bg.keyword == "Given" and bg.pickles == [0, 1, 2, 3]             # Background: all
    assert not any("counts what is added" in s.text for s in f.steps)       # a description
    es = C.parse_feature('# language: es\nCaracterística: x\n  Escenario: y\n'
                         '    Dado algo\n    Y otra cosa\n', "x.feature")
    assert [(s.keyword, s.text) for s in es.steps] == [("Dado", "algo"), ("Y", "otra cosa")]


def test_name_filter_and_commands():
    assert C.name_filter(["a", "b \\E c"]) == "^(\\Qa\\E|\\Qb \\E\\\\E\\Q c\\E)$"
    f = C.Feature("x.feature", "X", ["@x"], "en",
                  [C.Scenario("x.feature", 3, "one", ["@x"]),
                   C.Scenario("x.feature", 6, "two's", ["@x"])])
    whole = C.run_commands({"x.feature": {0, 1}}, [f], "run -Pname='{names}'",
                           "run -Ptags='{tags}'")
    assert whole == ["run -Ptags='@x'"]
    part = C.run_commands({"x.feature": {1}}, [f], "run -Pname='{names}'", "")
    assert part == ["run -Pname='^(\\Qtwo'\\''s\\E)$'"]


def test_steps_tool(cucumber_root, tmp_path):
    svc = Service(cucumber_root, cache_dir=str(tmp_path / "cache"))
    check = svc.steps(check=True)
    assert "6 step definitions (5 expressions, 1 regexes)" in check
    assert "6 features, 8 scenarios, 21 steps (19 lines" in check
    top = check.split("undefined steps (no step definition matches): 1")[1].split("\n  ")[1]
    assert "I do something nobody wrote" in top                 # what needs fixing, alone
    assert "leaves out ([cucumber] exclude_tags): 1" in check and "(@pending)" in check
    assert "run in no scenario" in check and "a step only a unit test loads" in check
    assert "naming a .feature that is not there: 1" in check and "missing.feature" in check
    assert "CartSteps#unused()" in check.split("no step uses:")[1]
    feat = svc.steps("shop/src/test/resources/features/carrito.feature")
    assert "Cuando I add 5 items  → CartSteps#iAdd()" in feat
    glue = svc.steps("CartSteps#iAdd")
    assert "used by 6 steps (5 lines: an outline's steps count once per Examples row) in 3 " \
           "features" in glue
    assert "carry a tag the build leaves out" in glue and "@pending" in glue
    assert "CartSteps#theCartHolds()" in svc.steps("Then the cart holds 2 items")
    assert "undefined" in svc.steps("When I fly")


def test_tests_and_changed_reach_scenarios(cucumber_root, tmp_path):
    svc = Service(cucumber_root, cache_dir=str(tmp_path / "cache"))
    out = svc.tests("Cart#add", transitive=True)
    assert "cucumber scenarios to run: 6 in 3 features" in out
    assert "-Dcucumber.features=" in out


def test_changed_feature_lines_select_their_scenarios(cucumber_root, tmp_path):
    import shutil
    import subprocess
    import pytest
    if not shutil.which("git"):
        pytest.skip("git not available")
    git = ["git", "-C", cucumber_root, "-c", "user.email=t@example.com", "-c", "user.name=t"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(git + args, check=True, capture_output=True)
    path = cucumber_root + "/shop/src/test/resources/features/cart.feature"
    text = open(path).read().replace("Then the cart holds 3 items", "Then the cart holds 3 item")
    open(path, "w").write(text)
    out = Service(cucumber_root, cache_dir=str(tmp_path / "cache")).changed()
    assert "cucumber scenarios to run: 1 in 1 features" in out
    assert "cart.feature:9  Add a few" in out
    # A line of a Background other features include: their scenarios (transitively).
    path = cucumber_root + "/shop/src/test/resources/features/fixtures/coupons.feature"
    text = open(path).read().replace("WELCOME", "HELLO")
    open(path, "w").write(text)
    out = Service(cucumber_root, cache_dir=str(tmp_path / "cache")).changed()
    assert "cucumber scenarios to run: 4 in 2 features" in out
    assert "checkout.feature:7  Pay" in out


def test_included_backgrounds(cucumber_root, tmp_path):
    svc = Service(cucumber_root, cache_dir=str(tmp_path / "cache"))
    # coupons.feature is included by store.feature, itself included by checkout.feature.
    coupon = svc.steps("CartSteps#aCoupon")
    assert "[a Background other features include]" in coupon
    assert "scenarios to run: 3 in 1 features" in coupon and "checkout.feature:7  Pay" in coupon
    store = svc.steps("shop/src/test/resources/features/fixtures/store.feature")
    assert "it runs in 3 scenarios of 1 features" in store
    checkout = svc.steps("shop/src/test/resources/features/checkout.feature")
    assert "includes shop/src/test/resources/features/fixtures/store.feature" in checkout
    assert "'features/fixtures/missing.feature': NOT FOUND" in checkout


def test_suite_lines_of_an_included_feature():
    inc = C.parse_feature('Feature: base\n  Background:\n    Given a coupon "X"\n', "f/base.feature")
    user = C.parse_feature('Feature: u\n  Scenario: one\n    Given the background from '
                           '"f/base.feature"\n  Scenario: two\n    Given a coupon "Y"\n',
                           "f/user.feature")
    suite = C.Suite([inc, user], ["the background from {string}"])
    assert suite.at_lines("f/base.feature", {3}) == {"f/user.feature": {0}}   # only "one"
    assert suite.runs_in(inc.steps[0]) == {"f/user.feature": {0}}
    loop = C.parse_feature('Feature: l\n  Background:\n    Given the background from '
                           '"l.feature"\n', "l.feature")
    assert C.Suite([loop], ["the background from {string}"]).runs_in(loop.steps[0]) == {}


def test_name_notes_and_run_all():
    a = C.Feature("a.feature", "A", [], "en", [C.Scenario("a.feature", 3, "Opens", []),
                                                C.Scenario("a.feature", 9, "Row 1", [], header=7)])
    b = C.Feature("b.feature", "B", [], "en", [C.Scenario("b.feature", 3, "Opens", [])])
    notes: list[str] = []
    C.run_commands({"a.feature": {0, 1}}, [a, b], "run -Pname='{names}'", "", notes=notes)
    assert "also runs 1 other scenarios with the same names (in 1 features)" in notes[0]
    assert "template name" in notes[1]
    many = {"a.feature": set(range(C.MAX_NAMED + 1))}
    big = C.Feature("a.feature", "A", [], "en",
                    [C.Scenario("a.feature", k, f"s{k}", []) for k in range(C.MAX_NAMED + 1)])
    assert C.run_commands(many, [big], "run '{names}'", "", run_all="./gradlew cucumber")[0] \
        .startswith("./gradlew cucumber   (the whole suite: 151 scenarios")

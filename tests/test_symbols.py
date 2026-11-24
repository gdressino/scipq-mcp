import re

from scipq_mcp import symbols as S


def test_parse_and_short():
    assert S.short("com/example/model/Account#Builder#number().") == "Account.Builder#number()"
    assert S.short("com/example/model/Account#`<init>`(+1).") == "Account#<init>(+1)"
    assert S.short("com/example/model/Entity#state.") == "Entity#state"
    assert S.short("com/example/model/") == "com.example.model"
    assert S.short("local 3") == "local 3"
    assert S.short("semanticdb maven maven/a 1.0 com/x/Y#z().") == "Y#z()"
    assert S.kind_of("com/example/model/Account#") == "type"
    assert S.kind_of("com/example/model/Account#`<init>`().") == "constructor"
    assert S.kind_of("com/example/model/Account#uid().") == "method"
    assert S.kind_of("com/example/model/Account#number.") == "term"
    assert S.package_of("com/example/model/Account#uid().") == "com.example.model"
    assert S.owner_type("com/example/model/Account#Builder#build().") == \
        "com/example/model/Account#Builder#"
    assert S.top_level_type_name("com/example/model/Account#Builder#build().") == "Account"


def test_query_matching():
    sym = "com/example/model/Account#Builder#number()."
    field = "com/example/model/Account#Builder#number."
    for q in ("number", "Builder#number", "Account.Builder.number", "com.example.model.Account.Builder.number"):
        assert S.matches(sym, S.parse_query(q)), q
    assert S.matches(sym, S.parse_query("number()"))
    assert not S.matches(field, S.parse_query("number()"))
    assert not S.matches(sym, S.parse_query("number(+1)"))
    assert S.matches("com/example/app/Service#process(+1).", S.parse_query("process(+1)"))
    assert S.matches("com/example/model/Account#`<init>`().", S.parse_query("Account#<init>"))
    assert not S.matches("com/example/model/", S.parse_query("model"))
    assert not S.matches("local 1", S.parse_query("local"))


def test_looks_like_symbol():
    assert S.looks_like_symbol("com/example/model/Entity#setState().")
    assert S.looks_like_symbol("com/example/model/Entity#")
    assert not S.looks_like_symbol("Entity#setState")
    assert not S.looks_like_symbol("com.example.model.Entity")


def test_definition_regex_selects_definitions_only():
    q = S.parse_query("Entity#setState")
    rx = re.compile(S.definition_regex(q))
    sym = b"com/example/model/Entity#setState()."
    as_definition = b"\x0a" + bytes([len(sym)]) + sym + b"\x28\x1a"   # SymbolInformation
    as_reference = b"\x12" + bytes([len(sym)]) + sym + b"\x42\x02"     # Occurrence
    assert rx.search(as_definition)
    assert not rx.search(as_reference)
    other = b"com/example/model/SubEntity#setState()."
    assert not rx.search(b"\x0a" + bytes([len(other)]) + other + b"\x28")
    # From the package root, the byte before the symbol is its length, which may be a digit.
    sym_digit_len = b"org/example/inventory/support/Formatter#formatAmount()."
    assert bytes([len(sym_digit_len)]) == b'7'  # a digit
    q = S.parse_query("org.example.inventory.support.Formatter.formatAmount()")
    assert re.compile(S.definition_regex(q)).search(b"\x0a" + bytes([len(sym_digit_len)]) + sym_digit_len + b"\x28")


def test_reference_search_for_types_skips_members():
    mode, pat = S.reference_search("com/example/model/Entity#")
    assert mode == "regex"
    rx = re.compile(pat)
    assert rx.search(b"com/example/model/Entity#\x42")
    assert not rx.search(b"com/example/model/Entity#setState().\x42")


def test_parameter_lists_in_queries():
    q = S.parse_query("Overloaded#pick(Map<String, List<Integer>> m, final String... rest)")
    assert q.names == ("Overloaded", "pick") and q.method_only
    assert q.params == ("Map", "String[]") and q.disambiguator is None
    assert S.parse_query("pick()").params == ()
    assert S.parse_query("pick(+1)").params is None and S.parse_query("pick(+1)").disambiguator == "+1"
    assert S.parse_query("pick").params is None
    assert S.parse_query("a.b.Type#pick(java.lang.String, int[] xs, T)").params == (
        "java.lang.String", "int[]", None)        # the package is kept, to be compared
    assert S.same_params(("java.util.Date",), ("Date",), ("java/util/Date#",))
    assert not S.same_params(("java.util.Date",), ("Date",), ("java/sql/Date#",))
    assert S.same_params(("java.util.Date",), ("Date",), None)   # unknown: by its name
    assert S.parse_query("Account#<init>(String)").names == ("Account", "<init>")
    assert S.same_params(("String", None), ("String", "int"))
    assert S.same_params(("Object",), (None,))     # a declared type variable
    assert not S.same_params(("String",), ("String", "int"))
    assert not S.same_params((), None)
    assert S.overload_symbol("java/lang/Long#`<init>`().", 2) == "java/lang/Long#`<init>`(+2)."
    assert S.overload_symbol("a/B#m(+3).", 0) == "a/B#m()."


def test_ellipsis_means_any_overload():
    for t in ("pick(...)", "pick(…)", "pick(*)"):
        q = S.parse_query(t)
        assert q.method_only and q.params is None and q.names == ("pick",), t


def test_annotations_with_scip_strings_are_left_out():
    # scip-javac writes an annotation's string unescaped: `(\d+)` with one backslash.
    sig = '@Then("^the cart holds (\\d+) items?$")\npublic void theCartHolds(int count)'
    assert S.without_annotations(sig).split() == ["public", "void", "theCartHolds(int", "count)"]
    assert S.is_parameter("a/B#m().[T]") and S.is_parameter("a/B#m().(x)") and \
        S.is_parameter("a/B#[T]")
    assert not S.is_parameter("a/B#m().") and not S.is_parameter("local 3")


def test_annotations_nested_to_any_depth_are_left_out():
    sig = ('@ApiResponses({@ApiResponse(responseCode = "200", description = "ok (done", '
           'content = @Content(schema = @Schema(implementation = Res.class)))})\n'
           '@Operation(summary = "x")\npublic Res login(@Param(d = "a)", r = true) Req account, '
           '@Context HttpServletRequest request)')
    assert " ".join(S.without_annotations(sig).split()) == \
        "public Res login( Req account, HttpServletRequest request)"
    assert S.without_annotations("public @interface Foo") == "public @interface Foo"
    assert S.without_annotations("@A('(') int g(char c)").split() == ["int", "g(char", "c)"]


def test_review_fixes_in_names():
    # C-style arrays in a written parameter list.
    assert S.written_params("int a[], String s[][]") == ("int[]", "String[][]")
    # A string spanning lines (scip-javac writes \n as a line break), then 'quotes'.
    sig = "@Operation(description = \"a (b).\n\n'estado' x\")\npublic R run(String id, boolean f)"
    assert S.without_annotations(sig).split() == ["public", "R", "run(String", "id,", "boolean",
                                                  "f)"]


def test_an_annotation_never_closed_ends_with_its_line():
    sig = '@ApiOperation(value = "a "b")\npublic void run(String x)'
    assert S.without_annotations(sig).split() == ["public", "void", "run(String", "x)"]

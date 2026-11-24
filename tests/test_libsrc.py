import os
import zipfile

import pytest

from scipq_mcp import libsrc as L
from scipq_mcp.javasrc import JavaSource
from scipq_mcp.service import Service

WIDGET = '''package org.acme.ui;

import java.util.List;

/**
 * A widget.
 * Has columns.
 */
@Deprecated
public class Widget<T> extends Base implements Comparable<Widget<T>> {
    /** Default width. */
    public static final int WIDTH = compute(3);
    private final List<String> names = List.of("a", "b");

    /** Makes one. */
    public Widget() {
    }

    /**
     * Adds a column from a provider.
     */
    public Column<T> addColumn(Provider<T> provider) {
        return addColumn(provider, "x");
    }

    protected <C extends Column<T>> C addColumn(Provider<T> provider, String key) {
        String s = "{ not a brace }";
        return null;
    }

    public Column<T> addColumn(Renderer<T> renderer) { return null; }

    void hidden() {}

    public enum Mode {
        /** Fast. */
        FAST(1),
        SLOW(2) { int x() { return 1; } };
        Mode(int n) {}
    }
}
'''


@pytest.fixture
def libroot(tmp_path):
    jar_dir = tmp_path / "gradle" / "files-2.1" / "org.acme" / "acme-ui" / "1.2.0" / "abc"
    jar_dir.mkdir(parents=True)
    jar = jar_dir / "acme-ui-1.2.0-sources.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("org/acme/ui/Widget.java", WIDGET)
    older = tmp_path / "gradle" / "files-2.1" / "org.acme" / "acme-ui" / "1.10.0" / "def"
    older.mkdir(parents=True)
    with zipfile.ZipFile(older / "acme-ui-1.10.0-sources.jar", "w") as z:
        z.writestr("org/acme/ui/Widget.java", WIDGET.replace("A widget.", "A newer widget."))
    return str(tmp_path / "gradle" / "files-2.1")


def test_parser_members():
    src = JavaSource(WIDGET)
    start, body_open, body_close = src.find_type(["Widget"])
    assert src.text[start:].startswith("@Deprecated\npublic class Widget") or \
        src.text[start:].startswith("public class Widget")
    members = src.members(body_open, body_close, type_name="Widget")
    kinds = [(m.kind, m.name) for m in members]
    assert kinds == [("field", "WIDTH"), ("field", "names"), ("constructor", "Widget"),
                     ("method", "addColumn"), ("method", "addColumn"), ("method", "addColumn"),
                     ("method", "hidden"), ("type", "Mode")]
    assert members[0].javadoc == "Default width."
    assert members[3].javadoc == "Adds a column from a provider."
    mode = src.find_type(["Widget", "Mode"])
    enum_members = src.members(mode[1], mode[2], is_enum=True, type_name="Mode")
    assert [m.kind for m in enum_members] == ["enum constants", "constructor"]


def test_lookup_picks_highest_version_and_overload(libroot, tmp_path):
    ix = L.LibraryIndex(str(tmp_path / "cache"), [libroot])
    d = L.lookup(ix, "org/acme/ui/Widget#addColumn(+2).")
    assert d.lib.coordinates == "org.acme:acme-ui:1.10.0" and d.lib.others == 1
    assert d.member.declaration == "public Column<T> addColumn(Renderer<T> renderer)"
    assert len(d.overloads) == 3
    t = L.lookup(ix, "org/acme/ui/Widget#")
    assert t.type_javadoc.startswith("A newer widget.")
    c = L.lookup(ix, "org/acme/ui/Widget#Mode#FAST.")
    assert c.member.declaration == "FAST(1)" and c.member.javadoc == "Fast."
    assert L.lookup(ix, "org/acme/ui/Missing#") is None


def test_definition_source_and_members_from_library(libroot, tmp_path, sample_root):
    svc = Service(sample_root, cache_dir=str(tmp_path / "cache"), library_roots=[libroot])
    out = svc.definition("org/acme/ui/Widget#addColumn().")
    assert "external — org.acme:acme-ui:1.10.0" in out
    assert "→ 22: public Column<T> addColumn(Provider<T> provider)" in out
    assert "Adds a column from a provider." in out
    src = svc.source("org/acme/ui/Widget#addColumn(+1).")
    assert 'String s = "{ not a brace }";' in src
    lib, members = L.type_members(L.LibraryIndex(str(tmp_path / "cache"), [libroot]),
                                  "org/acme/ui/Widget#")
    assert "hidden" not in {m.name for m in members}  # package-private
    assert "names" not in {m.name for m in members}   # private


BASE = '''package org.acme.ui;

import org.acme.core.*;

public abstract class Base implements Marker {
    /** Base version. */
    public Column<String> addColumn(Provider<String> p) { return null; }
    public void reset() {}
    public void resize(String size) {}
    public void resize(int width) {}
}
'''
PANEL = '''package org.acme.ui;

public class Panel extends Base {
    @Override
    public void resize(int width) {}
    private void resize(long width) {}
    public void resize(double width) {}
}
'''
MARKER = '''package org.acme.core;

public interface Marker extends java.io.Serializable {
    default String tag() { return ""; }
}
'''


@pytest.fixture
def hierarchy_root(tmp_path):
    jar_dir = tmp_path / "gradle" / "files-2.1" / "org.acme" / "acme-all" / "2.0.0" / "x"
    jar_dir.mkdir(parents=True)
    with zipfile.ZipFile(jar_dir / "acme-all-2.0.0-sources.jar", "w") as z:
        z.writestr("org/acme/ui/Widget.java", WIDGET)
        z.writestr("org/acme/ui/Base.java", BASE)
        z.writestr("org/acme/ui/Panel.java", PANEL)
        z.writestr("org/acme/core/Marker.java", MARKER)
    return str(tmp_path / "gradle" / "files-2.1")


def test_library_hierarchy_and_members(hierarchy_root, tmp_path, sample_root):
    ix = L.LibraryIndex(str(tmp_path / "cache"), [hierarchy_root])
    assert L.library_supertypes(ix, "org/acme/ui/Widget#") == [
        "org/acme/ui/Base#", "java/lang/Comparable#"]
    assert L.library_supertypes(ix, "org/acme/ui/Base#") == ["org/acme/core/Marker#"]
    assert L.library_ancestors(ix, "org/acme/ui/Widget#")[:3] == [
        "org/acme/ui/Base#", "java/lang/Comparable#", "org/acme/core/Marker#"]
    groups = L.ordered_members(ix, ["org/acme/ui/Widget#", "org/acme/ui/Base#",
                                    "org/acme/core/Marker#"])
    base = [g for g in groups if g[0] == "org/acme/ui/Base#"][0]
    assert [m.name for m in base[2]] == ["reset", "resize", "resize"] and base[3] == 1
    svc = Service(sample_root, cache_dir=str(tmp_path / "cache"), library_roots=[hierarchy_root])
    out = svc.members("org/acme/ui/Widget#")
    assert "Widget (library)" in out and "from Base (3, 1 overridden by nearer types" in out
    assert "from Marker (1" in out and "default String tag()" in out


def test_build_java_target(tmp_path):
    (tmp_path / "build.gradle.kts").write_text(
        "java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }\n")
    assert L.build_java_target(str(tmp_path)) == (21, "")
    (tmp_path / "build.gradle.kts").unlink()
    (tmp_path / "pom.xml").write_text("<properties><maven.compiler.release>17"
                                      "</maven.compiler.release></properties>")
    assert L.build_java_target(str(tmp_path))[0] == 17
    (tmp_path / "pom.xml").unlink()
    sub = tmp_path / "core"
    sub.mkdir()
    (sub / "build.gradle").write_text("sourceCompatibility = JavaVersion.VERSION_11\n")
    (tmp_path / "gradle.properties").write_text("org.gradle.java.home=/opt/jdk-11\n")
    assert L.build_java_target(str(tmp_path)) == (11, "/opt/jdk-11")


@pytest.mark.skipif(not L.jdk_src_zip(), reason="no JDK src.zip on this machine")
def test_typed_with_library_subtypes(sample_root, tmp_path):
    svc = Service(sample_root, cache_dir=str(tmp_path / "cache"),
                  library_roots=[L.jdk_src_zip()])  # just the JDK: no full cache scan
    out = svc.typed("java/util/List#", subtypes=True)
    assert "with" in out and "ArrayList" in out   # Group: new ArrayList<>()


def test_library_map_waits_for_another_builder(libroot, tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(L, "LOCK_WAIT_SECONDS", 0.5)
    ix = L.LibraryIndex(str(tmp_path / "cache"), [libroot])
    other = L.LibraryIndex(str(tmp_path / "cache"), [libroot])
    held = threading.Event()
    release = threading.Event()

    def builder():
        with other._lock():
            held.set()
            release.wait(5)

    t = threading.Thread(target=builder)
    t.start()
    held.wait(5)
    with pytest.raises(L.LibraryBusy):
        ix.find_class("org/acme/ui/Widget")   # not in the map yet, and the lock is busy
    release.set()
    t.join()
    assert ix.find_class("org/acme/ui/Widget").coordinates == "org.acme:acme-ui:1.10.0"


def test_another_jdk_in_the_shared_map_does_not_hide_this_one(tmp_path):
    """A map built with one JDK's src.zip (an older version, another checkout) must still
    index this project's JDK when it asks for a JDK class."""
    def jdk(name, doc):
        home = tmp_path / name / "lib"
        home.mkdir(parents=True)
        with zipfile.ZipFile(home / "src.zip", "w") as z:
            z.writestr("java.base/java/util/Map.java",
                       f"package java.util;\n/** {doc} */\npublic interface Map<K, V> {{\n"
                       "    V get(Object key);\n}\n")
        return str(home / "src.zip")

    old, new = jdk("jdk-old", "Old map."), jdk("jdk-new", "New map.")
    cache = str(tmp_path / "cache")
    assert L.LibraryIndex(cache, [old]).find_class("java/util/Map").jar == old
    found = L.LibraryIndex(cache, [old], jdk_zip=new).find_class("java/util/Map")
    assert found is not None and found.jar == new


def test_overrides_need_the_same_parameter_types(hierarchy_root, tmp_path):
    ix = L.LibraryIndex(str(tmp_path / "cache"), [hierarchy_root])
    (_t, _l, panel, _h), (_t2, _l2, base, hidden) = L.ordered_members(
        ix, ["org/acme/ui/Panel#", "org/acme/ui/Base#"])
    # resize(int) is overridden by Panel; resize(String) is not
    assert [m.declaration for m in base if m.name == "resize"] == [
        "public void resize(String size)"]
    assert hidden == 1
    first = panel[0]
    assert (first.start_line, first.line) == (4, 5)  # reported at the name, not @Override
    assert L.member_symbol("org/acme/ui/Panel#", panel[-1]) == "org/acme/ui/Panel#resize(+2)."
    # Overridden in the project (SCIP symbols): hidden too.
    *_rest, (_t3, _l3, base2, hidden2) = L.ordered_members(
        ix, ["org/acme/ui/Panel#", "org/acme/ui/Base#"], {"org/acme/ui/Base#reset()."})
    assert "reset" not in {m.name for m in base2} and hidden2 == 2


def test_param_types():
    assert L.param_types("public void f(final java.util.List<String> a, int[] b, "
                         "Object... c)") == ("List", "int[]", "Object[]")
    assert L.param_types("public <C extends Component> C add(C c, Map.Entry<K, V> e)") == (
        None, "Entry")
    assert L.param_types("void g(@Nullable T t, String s)", {"T"}) == (None, "String")
    assert L.param_types("int count") is None


def test_overloads_of_a_library_method(libroot, tmp_path, sample_root):
    svc = Service(sample_root, cache_dir=str(tmp_path / "cache"), library_roots=[libroot])
    out = svc.overloads("org/acme/ui/Widget#addColumn(+1).")
    assert "3 overloads" in out
    # Overloaded methods are written with their parameter list: it reads back as the same one.
    assert "this       Widget#addColumn(Provider, String)" in out
    assert "overload   Widget#addColumn(Renderer)" in out
    assert "acme-ui:1.10.0 org/acme/ui/Widget.java:" in out
    assert "Column<T> addColumn(Renderer<T> renderer)" in out


def test_mask_agrees_with_the_reference_scanner():
    from scipq_mcp.javasrc import _mask_slow, mask
    cases = ['a "b\\" c', '"\\"\n x', "'\"' + \"'\"", 'x /* a\n b */ y // c\n"d"',
             '"""\n t "q" \n"""', '"""\nopen', '"a\\\\" + b', "'\\''", '"open\nnext "x"',
             "/* never closed", 's = "\\\\\\""; t', WIDGET, BASE, PANEL]
    for c in cases:
        assert mask(c) == _mask_slow(c), c


def test_library_index_reuses_open_jars(libroot, tmp_path):
    ix = L.LibraryIndex(str(tmp_path / "cache"), [libroot])
    lib = ix.find_class("org/acme/ui/Widget")
    a, b = ix.source(lib), ix.source(lib)
    assert a is b and len(ix._zips) == 1
    assert ix.find_class("org/acme/ui/Nope") is None and "org/acme/ui/Nope" in ix._absent
    ix.close()


def test_a_jar_that_appears_after_a_miss_is_found(libroot, tmp_path):
    ix = L.LibraryIndex(str(tmp_path / "cache"), [libroot])
    assert ix.find_class("org/acme/ui/Gadget") is None
    jar_dir = os.path.join(libroot, "org.acme", "acme-extra", "1.0.0", "h")
    os.makedirs(jar_dir)
    with zipfile.ZipFile(os.path.join(jar_dir, "acme-extra-1.0.0-sources.jar"), "w") as z:
        z.writestr("org/acme/ui/Gadget.java", "package org.acme.ui;\npublic class Gadget {}\n")
    found = ix.find_class("org/acme/ui/Gadget")   # downloaded since: no 10-minute wait
    assert found is not None and found.coordinates == "org.acme:acme-extra:1.0.0"


def test_library_cache_removed_while_running(libroot, tmp_path):
    import shutil
    cache = tmp_path / "cache"
    ix = L.LibraryIndex(str(cache), [libroot])
    assert ix.find_class("org/acme/ui/Widget") is not None
    shutil.rmtree(cache)
    assert ix.find_class("org/acme/ui/Widget") is not None


TRICKY = '''package com.example.lib;

public class Outer {
    private int left, right = 2, grid[];
    private final java.util.Map<String, Integer> sizes = new java.util.HashMap<String, Integer>(),
        names = null;
    boolean ok = left < right, other;

    public @interface Marker {
        String value() default "";
    }

    public record Range(int lo, int hi) {
        public Range {
            if (lo > hi) throw new IllegalArgumentException();
        }
    }

    public enum Mode {
        FAST {
            int speed() { return 2; }
        },
        SLOW(1) {
            int speed() { return 1; }
        },
        IDLE;

        int speed() { return 0; }
    }

    static class Node {
        static class Builder {
            static class OfInt { int fromBuilder; }
        }
        static class OfInt { int direct; }
    }
}
'''


def test_parser_tricky_members():
    src = JavaSource(TRICKY)

    def members(*names, enum=False):
        _start, lo, hi = src.find_type(list(names))
        return [(m.kind, m.name) for m in src.members(lo, hi, is_enum=enum,
                                                      type_name=names[-1])]

    outer = members("Outer")
    # every declarator of a field declaration
    assert [n for k, n in outer if k == "field"] == ["left", "right", "grid", "sizes", "names",
                                                     "ok", "other"]
    # a nested @interface is a type, not a field
    assert ("type", "Marker") in outer and ("field", "Marker") not in outer
    # a record's compact constructor
    assert members("Outer", "Range") == [("constructor", "Range")]
    # enum constants with arguments and bodies are not members
    assert members("Outer", "Mode", enum=True) == [("enum constants", ""), ("method", "speed")]
    # a nested name resolves at its own level, not deeper
    assert members("Outer", "Node", "OfInt") == [("field", "direct")]
    assert members("Outer", "Node", "Builder", "OfInt") == [("field", "fromBuilder")]
    lines = {m.name: m.line for m in src.members(*src.find_type(["Outer"])[1:],
                                                 type_name="Outer")}
    assert lines["right"] == 4 and lines["names"] == 6


def test_parser_unicode_names_method_named_like_its_class_and_type_arguments():
    src = JavaSource('''package a;
public class Ապա {
    int año = 1, ñandú;
    public static class loadlib {
        public loadlib() {}
        public Object loadlib(Object args) { return args; }
        public <T> loadlib(T t) {}
    }
    private java.util.Map<String, Long> m = new java.util.HashMap<java.lang.String, Long>(),
        n = Entry.<java.lang.String, Long>of(1, 2);
    private Object acc = new Accessor<boolean[], Boolean>() { }, other;
    boolean cmp = a < b, c = d > e;
}
''')
    _s, lo, hi = src.find_type(["Ապա"])
    assert [n for k, n in [(m.kind, m.name) for m in src.members(lo, hi, type_name="Ապա")]
            if k == "field"] == ["año", "ñandú", "m", "n", "acc", "other", "cmp", "c"]
    _s, lo, hi = src.find_type(["Ապա", "loadlib"])
    assert [m.kind for m in src.members(lo, hi, type_name="loadlib")] == [
        "constructor", "method", "constructor"]


SHAPES = '''package org.acme.shapes;

public class Shapes {
    /** How to align. */
    public enum Align {
        /** At the start. */
        START,
        CENTER("c") {
            @Override public String toString() { return "center"; }
        },
        @Deprecated END;

        Align() {}
        Align(String s) {}
        public boolean isEdge() { return this != CENTER; }
    }

    public record Box<T>(@Deprecated int width, java.util.List<T> items, String... tags) {
        public int width() { return width; }
    }
}
'''


def test_library_enum_constants_and_record_components(tmp_path, sample_root):
    jar_dir = tmp_path / "gradle" / "files-2.1" / "org.acme" / "shapes" / "1.0" / "h"
    jar_dir.mkdir(parents=True)
    with zipfile.ZipFile(jar_dir / "shapes-1.0-sources.jar", "w") as z:
        z.writestr("org/acme/shapes/Shapes.java", SHAPES)
    root = str(tmp_path / "gradle" / "files-2.1")
    ix = L.LibraryIndex(str(tmp_path / "cache"), [root])
    _lib, align = L.type_members(ix, "org/acme/shapes/Shapes#Align#")
    assert [(m.kind, m.name) for m in align if m.kind == "enum constant"] == [
        ("enum constant", "START"), ("enum constant", "CENTER"), ("enum constant", "END")]
    assert align[0].javadoc == "At the start." and ("method", "isEdge") in [
        (m.kind, m.name) for m in align]
    _lib, box = L.type_members(ix, "org/acme/shapes/Shapes#Box#")
    got = [(m.kind, m.declaration) for m in box]
    assert ("record component", "int width") in got
    assert ("record component", "java.util.List<T> items") in got
    assert ("method", "public java.util.List<T> items()") in got     # implicit accessor
    assert ("method", "public String[] tags()") in got
    assert sum(1 for k, d in got if k == "method" and "width()" in d) == 1  # declared one
    d = L.lookup(ix, "org/acme/shapes/Shapes#Box#items().")
    assert d is not None and "implicit accessor" in d.member.javadoc
    svc = Service(sample_root, cache_dir=str(tmp_path / "cache"), library_roots=[root])
    assert "enum constant: START" in svc.members("org/acme/shapes/Shapes#Align#")


def test_declared_supertypes_skip_type_parameter_bounds():
    assert L._declared_names("public class Nodes<T extends Node> extends ArrayList<T>") == [
        "ArrayList"]
    assert L._declared_names("class A<T extends Comparable<T>> extends B<T> implements C, "
                             "D<T>") == ["B", "C", "D"]
    assert L._declared_names("public interface Map<K, V>") == []

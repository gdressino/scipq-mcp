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

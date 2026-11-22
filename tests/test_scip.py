import glob
import os

from scipq_mcp import scip


def shards(root):
    return sorted(glob.glob(os.path.join(root, "build/scip-targetroot/**/*.scip"), recursive=True))


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 2**31, 2**40):
        enc = scip.encode_varint(n)
        assert scip._varint(enc, 0) == (n, len(enc))


def test_documents_have_paths(sample_root):
    paths = shards(sample_root)
    assert paths
    for p in paths:
        docs = scip.read_documents(p)
        assert len(docs) == 1
        assert p.endswith(docs[0].relative_path + ".scip")


def test_ranges_are_zero_based(sample_root):
    path = [p for p in shards(sample_root) if p.endswith("Entity.java.scip")][0]
    doc = scip.read_documents(path)[0]
    d = [o for o in doc.occurrences if o.symbol == "com/example/model/Entity#setState()."
         and o.is_definition][0]
    assert (d.range.start_line, d.range.start_char) == (13, 16)  # line 14 in the editor
    assert d.enclosing is not None and d.enclosing.contains(d.range)


def test_selective_decoding_matches_full_decoding(sample_root):
    for path in shards(sample_root):
        full = scip.read_documents(path)[0]
        part = scip.read_documents(path)[0]
        syms = {o.symbol for o in full.occurrences}
        for s in syms:
            assert [o for o in full.occurrences if o.symbol == s] == part.occurrences_of([s])
        assert [o for o in full.occurrences if o.is_definition] == \
            scip.read_documents(path)[0].definitions()
        infos = {i.symbol: i for i in full.symbols}
        for s, info in infos.items():
            assert scip.read_documents(path)[0].symbol_infos_of([s]) == [info]


def test_symbol_information(sample_root):
    path = [p for p in shards(sample_root) if p.endswith("SubAccount.java.scip")][0]
    doc = scip.read_documents(path)[0]
    info = {i.symbol: i for i in doc.symbols}["com/example/model/SubAccount#uid()."]
    assert info.kind_name == "method"
    assert info.signature.endswith("public String uid()")
    rels = {r.symbol for r in info.relationships if r.is_implementation}
    assert rels == {"com/example/model/Account#uid().", "com/example/model/Entity#uid().",
                    "com/example/model/Identifiable#uid()."}


def test_deprecated_packed_ranges():
    # Occurrence{range: [3, 4, 9] (packed, field 1), symbol: "a/B#", roles: 1,
    #            enclosing_range: [2, 0, 5, 1] (packed, field 7)}
    sym = b"a/B#"
    body = (b"\x0a\x03\x03\x04\x09" + b"\x12" + bytes([len(sym)]) + sym + b"\x18\x01"
            + b"\x3a\x04\x02\x00\x05\x01")
    occ = scip.decode_occurrence(body, (0, len(body)))
    assert occ.symbol == "a/B#" and occ.is_definition
    assert occ.range == scip.Range(3, 4, 3, 9)
    assert occ.enclosing == scip.Range(2, 0, 5, 1)

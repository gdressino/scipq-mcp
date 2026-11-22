"""Minimal, dependency-free reader for the subset of SCIP (scip.proto) that scipq-mcp needs.

Field numbers follow https://github.com/scip-code/scip/blob/main/scip.proto:

    Index:             2 documents
    Document:          1 relative_path, 2 occurrences, 3 symbols
    Occurrence:        1 range (packed, deprecated), 2 symbol, 3 symbol_roles,
                       7 enclosing_range (packed, deprecated),
                       8/9 single/multi-line range, 10/11 single/multi-line enclosing range
    SingleLineRange:   1 line, 2 start_character, 3 end_character
    MultiLineRange:    1 start_line, 2 start_character, 3 end_line, 4 end_character
    SymbolInformation: 1 symbol, 3 documentation, 4 relationships, 5 kind, 6 display_name,
                       7 signature_documentation (a Document; its field 5 is the text),
                       8 enclosing_symbol
    Relationship:      1 symbol, 2 is_reference, 3 is_implementation, 4 is_type_definition,
                       5 is_definition

proto3 omits fields set to their default value: a missing line means line 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

# SymbolRole bits.
DEFINITION = 0x1
IMPORT = 0x2
WRITE_ACCESS = 0x4
READ_ACCESS = 0x8
GENERATED = 0x10
TEST = 0x20

# SymbolInformation.Kind values that matter for display (scip.proto, enum Kind).
KIND_NAMES = {
    7: "class",
    9: "constructor",
    11: "enum",
    12: "enum member",
    15: "field",
    21: "interface",
    26: "method",
    37: "parameter",
    55: "type alias",
    58: "type parameter",
    61: "variable",
    66: "abstract method",
    80: "static method",
    79: "static field",
    4: "annotation",
    8: "constant",
    35: "package",
    53: "trait",
    33: "object",
}


class DecodeError(ValueError):
    pass


def _varint(b: memoryview, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7
        if s > 63:
            raise DecodeError("varint too long")


def fields(b: memoryview, start: int, end: int) -> Iterator[tuple[int, int, object]]:
    """Yields (field number, wire type, value): an int for varints, a (start, end) span for
    length-delimited values. Fixed-width values are skipped and yielded as None."""
    i = start
    while i < end:
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
            yield num, wire, v
        elif wire == 2:
            n, i = _varint(b, i)
            yield num, wire, (i, i + n)
            i += n
        elif wire == 1:
            i += 8
            yield num, wire, None
        elif wire == 5:
            i += 4
            yield num, wire, None
        else:
            raise DecodeError(f"unsupported wire type {wire}")
    if i != end:
        raise DecodeError("truncated message")


def _text(b: memoryview, span) -> str:
    return bytes(b[span[0]:span[1]]).decode("utf-8", errors="replace")


def _packed(b: memoryview, span) -> list[int]:
    out = []
    i, end = span
    while i < end:
        v, i = _varint(b, i)
        out.append(v)
    return out


@dataclass(frozen=True, slots=True)
class Range:
    """Half-open, 0-based range."""

    start_line: int
    start_char: int
    end_line: int
    end_char: int

    def contains(self, other: "Range") -> bool:
        return ((self.start_line, self.start_char) <= (other.start_line, other.start_char)
                and (other.end_line, other.end_char) <= (self.end_line, self.end_char))

    def size(self) -> tuple[int, int]:
        return (self.end_line - self.start_line, self.end_char - self.start_char)


def _single(b, span) -> Range:
    line = sc = ec = 0
    for n, _, v in fields(b, *span):
        if n == 1:
            line = v
        elif n == 2:
            sc = v
        elif n == 3:
            ec = v
    return Range(line, sc, line, ec)


def _multi(b, span) -> Range:
    sl = sc = el = ec = 0
    for n, _, v in fields(b, *span):
        if n == 1:
            sl = v
        elif n == 2:
            sc = v
        elif n == 3:
            el = v
        elif n == 4:
            ec = v
    return Range(sl, sc, el, ec)


def _from_ints(v: list[int]) -> Range | None:
    if len(v) == 3:
        return Range(v[0], v[1], v[0], v[2])
    if len(v) == 4:
        return Range(v[0], v[1], v[2], v[3])
    return None


@dataclass(slots=True)
class Occurrence:
    symbol: str
    roles: int
    range: Range
    enclosing: Range | None

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & DEFINITION)


@dataclass(slots=True)
class Relationship:
    symbol: str
    is_reference: bool = False
    is_implementation: bool = False
    is_type_definition: bool = False
    is_definition: bool = False


@dataclass(slots=True)
class SymbolInfo:
    symbol: str
    documentation: list[str] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    kind: int = 0
    display_name: str = ""
    signature: str = ""
    enclosing_symbol: str = ""

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, f"kind {self.kind}" if self.kind else "")


def _range_msg(b, i: int, end: int, multi: bool) -> Range:
    """SingleLineRange {1 line, 2 start, 3 end} or MultiLineRange {1 start_line,
    2 start_character, 3 end_line, 4 end_character}; absent fields are 0."""
    f1 = f2 = f3 = f4 = 0
    while i < end:
        key = b[i]
        i += 1
        if key & 0x80:
            key, i = _varint(b, i - 1)
        if key & 7 != 0:
            raise DecodeError("unexpected wire type in a range")
        v = b[i]
        i += 1
        if v & 0x80:
            v, i = _varint(b, i - 1)
        k = key >> 3
        if k == 1:
            f1 = v
        elif k == 2:
            f2 = v
        elif k == 3:
            f3 = v
        elif k == 4:
            f4 = v
    if multi:
        return Range(f1, f2, f3, f4)
    return Range(f1, f2, f1, f3)


def decode_occurrence(b, span) -> Occurrence:
    i, end = span
    symbol, roles = "", 0
    rng = enc = None
    packed_rng: list[int] = []
    packed_enc: list[int] = []
    while i < end:
        key = b[i]
        i += 1
        if key & 0x80:
            key, i = _varint(b, i - 1)
        wire = key & 7
        if wire == 2:
            n = b[i]
            i += 1
            if n & 0x80:
                n, i = _varint(b, i - 1)
            num = key >> 3
            if num == 2:
                symbol = b[i:i + n].decode("utf-8", errors="replace")
            elif num == 8:
                rng = _range_msg(b, i, i + n, False)
            elif num == 9:
                rng = _range_msg(b, i, i + n, True)
            elif num == 10:
                enc = _range_msg(b, i, i + n, False)
            elif num == 11:
                enc = _range_msg(b, i, i + n, True)
            elif num == 1:
                packed_rng.extend(_packed(b, (i, i + n)))
            elif num == 7:
                packed_enc.extend(_packed(b, (i, i + n)))
            i += n
        elif wire == 0:
            v = b[i]
            i += 1
            if v & 0x80:
                v, i = _varint(b, i - 1)
            num = key >> 3
            if num == 3:
                roles = v
            elif num == 1:
                packed_rng.append(v)
            elif num == 7:
                packed_enc.append(v)
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise DecodeError(f"unsupported wire type {wire}")
    if rng is None:
        rng = _from_ints(packed_rng) or Range(0, 0, 0, 0)
    if enc is None and packed_enc:
        enc = _from_ints(packed_enc)
    return Occurrence(symbol, roles, rng, enc)


def decode_relationship(b: memoryview, span) -> Relationship:
    r = Relationship("")
    for n, _, v in fields(b, *span):
        if n == 1:
            r.symbol = _text(b, v)
        elif n == 2:
            r.is_reference = bool(v)
        elif n == 3:
            r.is_implementation = bool(v)
        elif n == 4:
            r.is_type_definition = bool(v)
        elif n == 5:
            r.is_definition = bool(v)
    return r


def decode_symbol_info(b: memoryview, span) -> SymbolInfo:
    s = SymbolInfo("")
    for n, _, v in fields(b, *span):
        if n == 1:
            s.symbol = _text(b, v)
        elif n == 3:
            s.documentation.append(_text(b, v))
        elif n == 4:
            s.relationships.append(decode_relationship(b, v))
        elif n == 5:
            s.kind = v
        elif n == 6:
            s.display_name = _text(b, v)
        elif n == 7:
            for dn, _, dv in fields(b, *v):
                if dn == 5:
                    s.signature = _text(b, dv)
        elif n == 8:
            s.enclosing_symbol = _text(b, v)
    return s


def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def string_field(num: int, text: str) -> bytes:
    """Wire encoding of a length-delimited string field: tag, length, bytes."""
    data = text.encode()
    return encode_varint((num << 3) | 2) + encode_varint(len(data)) + data


class Document:
    """A Document inside a shard, decoded lazily and selectively.

    `index()` walks only the top-level fields (tag + length, no payload). Queries then use
    byte search to find where a symbol is encoded and decode just the occurrences or symbol
    informations that contain those positions."""

    __slots__ = ("buf", "span", "_path", "_index", "_occ_starts", "_sym_starts",
                 "_occurrences", "_symbols", "_definitions")

    def __init__(self, buf, span: tuple[int, int]):
        self.buf = buf
        self.span = span
        self._path: str | None = None
        self._index: list[tuple[int, int, int]] | None = None
        self._occ_starts: list[int] | None = None
        self._sym_starts: list[int] | None = None
        self._occurrences: list[Occurrence] | None = None
        self._symbols: list[SymbolInfo] | None = None
        self._definitions: list[Occurrence] | None = None

    def raw(self) -> bytes:
        return bytes(self.buf[self.span[0]:self.span[1]])

    def index(self) -> list[tuple[int, int, int]]:
        """[(field number, payload start, payload end)] of the length-delimited top-level
        fields, in file order."""
        if self._index is None:
            b = self.buf
            i, end = self.span
            out = []
            append = out.append
            while i < end:
                key = b[i]
                i += 1
                if key & 0x80:
                    key, i = _varint(b, i - 1)
                wire = key & 7
                if wire == 2:
                    n = b[i]
                    i += 1
                    if n & 0x80:
                        n, i = _varint(b, i - 1)
                    append((key >> 3, i, i + n))
                    i += n
                elif wire == 0:
                    _, i = _varint(b, i)
                elif wire == 1:
                    i += 8
                elif wire == 5:
                    i += 4
                else:
                    raise DecodeError(f"unsupported wire type {wire}")
            self._index = out
        return self._index

    def _spans(self, num: int) -> list[tuple[int, int]]:
        return [(a, z) for n, a, z in self.index() if n == num]

    def find_all(self, needle: bytes) -> list[int]:
        b = self.buf
        start, end = self.span
        out = []
        i = b.find(needle, start, end)
        while i >= 0:
            out.append(i)
            i = b.find(needle, i + 1, end)
        return out

    def _containing(self, num: int, positions: list[int]) -> list[tuple[int, int]]:
        """Spans of top-level field `num` that contain any of the sorted `positions`."""
        if not positions:
            return []
        out = []
        k = 0
        for n, a, z in self.index():
            while k < len(positions) and positions[k] < a:
                k += 1
            if k == len(positions):
                break
            if n == num and positions[k] < z:
                out.append((a, z))
        return out

    @property
    def relative_path(self) -> str:
        if self._path is None:
            spans = self._spans(1)
            self._path = _text(self.buf, spans[0]) if spans else ""
        return self._path

    # --- full decoding (one document at a time: outline, callees) --------------------------

    @property
    def occurrences(self) -> list[Occurrence]:
        if self._occurrences is None:
            b = self.buf
            self._occurrences = [decode_occurrence(b, sp) for sp in self._spans(2)]
        return self._occurrences

    @property
    def symbols(self) -> list[SymbolInfo]:
        if self._symbols is None:
            b = self.buf
            self._symbols = [decode_symbol_info(b, sp) for sp in self._spans(3)]
        return self._symbols

    # --- selective decoding ---------------------------------------------------------------

    def occurrences_of(self, symbols) -> list[Occurrence]:
        """Occurrences of any of the given symbols."""
        if self._occurrences is not None:
            want = set(symbols)
            return [o for o in self._occurrences if o.symbol in want]
        positions = sorted(p for s in set(symbols) for p in self.find_all(string_field(2, s)))
        want = set(symbols)
        b = self.buf
        return [o for o in (decode_occurrence(b, sp) for sp in self._containing(2, positions))
                if o.symbol in want]

    def symbol_infos_of(self, symbols) -> list[SymbolInfo]:
        if self._symbols is not None:
            want = set(symbols)
            return [s for s in self._symbols if s.symbol in want]
        positions = sorted(p for s in set(symbols) for p in self.find_all(string_field(1, s)))
        want = set(symbols)
        b = self.buf
        return [x for x in (decode_symbol_info(b, sp) for sp in self._containing(3, positions))
                if x.symbol in want]

    def symbol_infos_containing(self, needle: bytes) -> list[SymbolInfo]:
        """SymbolInformation messages whose bytes contain `needle` (e.g. a relationship)."""
        b = self.buf
        return [decode_symbol_info(b, sp) for sp in self._containing(3, self.find_all(needle))]

    def symbol_infos_matching(self, regex) -> list[SymbolInfo]:
        """SymbolInformation messages that contain a match of the compiled bytes `regex`."""
        start, end = self.span
        positions = [m.start() for m in regex.finditer(self.buf, start, end)]
        b = self.buf
        return [decode_symbol_info(b, sp) for sp in self._containing(3, positions)]

    def occurrences_matching(self, regex) -> list[Occurrence]:
        """Occurrences that contain a match of the compiled bytes `regex`."""
        start, end = self.span
        positions = [m.start() for m in regex.finditer(self.buf, start, end)]
        b = self.buf
        return [decode_occurrence(b, sp) for sp in self._containing(2, positions)]

    def definitions(self) -> list[Occurrence]:
        """Definition occurrences. Peeks at symbol_roles without decoding the ranges of the
        (much more numerous) references."""
        if self._definitions is not None:
            return self._definitions
        if self._occurrences is not None:
            self._definitions = [o for o in self._occurrences if o.roles & DEFINITION]
            return self._definitions
        b = self.buf
        out = []
        for a, z in self._spans(2):
            roles = _peek_roles(b, a, z)
            if roles is None:
                o = decode_occurrence(b, (a, z))
                if o.roles & DEFINITION:
                    out.append(o)
            elif roles & DEFINITION:
                out.append(decode_occurrence(b, (a, z)))
        self._definitions = out
        return out


def _peek_roles(b, a: int, z: int) -> int | None:
    """symbol_roles of an Occurrence laid out as scip-javac writes it (symbol, then roles,
    then the rest), or None when the layout differs and a full decode is needed."""
    if a >= z or b[a] != 0x12:
        return None
    n = b[a + 1]
    i = a + 2
    if n & 0x80:
        n, i = _varint(b, a + 1)
    i += n
    if i >= z:
        return 0
    tag = b[i]
    if tag == 0x18:
        v = b[i + 1]
        if v & 0x80:
            v, _ = _varint(b, i + 1)
        return v
    if tag in (0x42, 0x4a, 0x52, 0x5a, 0x22, 0x28, 0x32):
        return 0  # roles omitted: proto3 default 0
    return None


def read_documents(path: str) -> list[Document]:
    """Documents of an Index file (scip-javac writes one per shard)."""
    with open(path, "rb") as fh:
        buf = fh.read()
    return [Document(buf, v) for n, w, v in fields(buf, 0, len(buf)) if n == 2 and w == 2]

"""Text edits on original source ranges, composed inside-out, and git patches.

Refactoring never re-prints a syntax tree: every change is a replacement of a range of the
original text. An edit's new text is made of literal pieces and slots, a slot being a range of
the original whose text (with the edits inside it applied) goes there, so a template like
`Runner.of($recv).run($0)` keeps the receiver and the argument exactly as written, comments
included. What is outside every edit is copied byte for byte.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass, field


class EditError(Exception):
    pass


# --- positions ---------------------------------------------------------------------------------

class Text:
    """A file's text, its line starts, and conversions between offsets (code points, as Python
    indexes str) and the positions of SCIP and JavaParser (UTF-16 columns)."""

    def __init__(self, text: str):
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.eol = "\r\n" if "\r\n" in text[:20000] and text.count("\r\n") * 2 >= \
            text.count("\n") else "\n"

    def line(self, k: int) -> str:
        """Line k (0-based) without its end of line."""
        if k < 0 or k >= len(self.starts):
            return ""
        end = self.starts[k + 1] - 1 if k + 1 < len(self.starts) else len(self.text)
        return self.text[self.starts[k]:end].rstrip("\r")

    def offset(self, line1: int, col1: int) -> int:
        """JavaParser position (1-based line, 1-based UTF-16 column) -> offset."""
        if line1 - 1 >= len(self.starts):
            return len(self.text)
        start = self.starts[line1 - 1]
        units = col1 - 1
        line = self.text[start:start + units + 4]
        if all(ord(c) <= 0xFFFF for c in line):
            return start + units
        k = 0
        for i, ch in enumerate(self.text[start:]):
            if k >= units or ch == "\n":
                return start + i
            k += 2 if ord(ch) > 0xFFFF else 1
        return len(self.text)

    def scip(self, line0: int, col0: int) -> int:
        return self.offset(line0 + 1, col0 + 1)

    def span(self, rng) -> tuple[int, int]:
        """A JavaParser range [bl, bc, el, ec] (end inclusive) -> (start, end) offsets."""
        bl, bc, el, ec = rng
        return self.offset(bl, bc), self.offset(el, ec + 1)

    def position(self, off: int) -> tuple[int, int]:
        """Offset -> JavaParser position (1-based line, 1-based UTF-16 column)."""
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        start = self.starts[lo]
        col = sum(2 if ord(c) > 0xFFFF else 1 for c in self.text[start:off])
        return lo + 1, col + 1

    def end_position(self, end: int) -> tuple[int, int]:
        """The inclusive JavaParser end position of a range ending (exclusive) at `end`."""
        line, col = self.position(end - 1)
        return (line, col + 1) if ord(self.text[end - 1]) > 0xFFFF else (line, col)

    def line_of(self, off: int) -> int:
        """0-based line of an offset."""
        return self.position(off)[0] - 1

    def line_start(self, off: int) -> int:
        return self.starts[self.line_of(off)]

    def line_end(self, off: int) -> int:
        """Offset just past the end of line of `off` (its "\\n" included)."""
        k = self.line_of(off)
        return self.starts[k + 1] if k + 1 < len(self.starts) else len(self.text)

    def indent_at(self, off: int) -> str:
        line = self.line(self.line_of(off))
        return line[:len(line) - len(line.lstrip(" \t"))]

    def alone_on_lines(self, start: int, end: int) -> bool:
        """Only whitespace before `start` on its line and after `end` on its line."""
        before = self.text[self.line_start(start):start]
        after_end = self.line_end(max(start, end - 1))
        after = self.text[end:after_end]
        return not before.strip() and not after.strip()


def indent_unit(text: str) -> str:
    """The file's indentation unit: a tab, or 2, 4 or 8 spaces."""
    tabs = spaces = 0
    widths: Counter = Counter()
    for line in text.split("\n"):
        if not line.strip():
            continue
        lead = line[:len(line) - len(line.lstrip(" \t"))]
        if lead.startswith("\t"):
            tabs += 1
        elif lead:
            spaces += 1
            widths[len(lead)] += 1
    if tabs > spaces:
        return "\t"
    total = sum(widths.values())
    for u in (8, 4, 2):
        if total and sum(n for w, n in widths.items() if w % u == 0) >= 0.9 * total:
            return " " * u
    return "    "


def reindent(template: str, base: str, unit: str, first: bool = False) -> str:
    """A template's lines indented for where it goes: `base` (the indentation of the line it
    lands on) plus its own levels, counted in its own unit and written in the file's. The
    first line gets `base` only when `first` (inserted at a line start)."""
    lines = template.strip("\n").split("\n")
    if len(lines) == 1:
        return (base if first else "") + lines[0].strip()
    rest = [ln for ln in lines[1:] if ln.strip()]
    leads = [ln[:len(ln) - len(ln.lstrip(" \t"))] for ln in rest]
    own = lines[0][:len(lines[0]) - len(lines[0].lstrip(" \t"))]
    if any("\t" in x for x in leads + [own]):
        def level(lead: str) -> int:
            return lead.count("\t") + lead.replace("\t", "").count(" ") // 4
    else:
        widths = sorted({len(x) - len(own) for x in leads if len(x) > len(own)})
        step = widths[0] if widths else 4
        step = min(step, 4) if step else 4

        def level(lead: str) -> int:
            return max(0, (len(lead) - len(own)) // step)
    out = []
    for k, ln in enumerate(lines):
        if not ln.strip():
            out.append("")
            continue
        lead = ln[:len(ln) - len(ln.lstrip(" \t"))]
        prefix = base + unit * (level(lead) if k else 0)
        out.append((prefix if (k or first) else "") + ln.strip())
    return "\n".join(out)


# --- edits ---------------------------------------------------------------------------------------

@dataclass
class Slot:
    """The original text of [start, end), with the edits inside it applied."""
    start: int
    end: int


@dataclass
class Piece:
    text: str
    kind: str = ""        # check: a node of this JavaParser kind covers exactly this piece


@dataclass
class Edit:
    start: int
    end: int              # == start: an insertion
    parts: list           # Piece | Slot
    step: int
    what: str             # for messages
    kind: str = ""        # check: a node of this kind covers exactly the whole new text
    paren_ok: bool = False  # an expression: may be parenthesized if it merges with its context
    paren: bool = False
    seq: int = 0

    @property
    def insertion(self) -> bool:
        return self.start == self.end

    def same(self, other: "Edit") -> bool:
        return (self.start, self.end, self.kind) == (other.start, other.end, other.kind) and \
            [(type(p).__name__, getattr(p, "text", None), getattr(p, "start", None),
              getattr(p, "end", None)) for p in self.parts] == \
            [(type(p).__name__, getattr(p, "text", None), getattr(p, "start", None),
              getattr(p, "end", None)) for p in other.parts]


@dataclass
class Placed:
    """Where an edit (or a checked piece) ended up in the new text."""
    edit: Edit
    start: int
    end: int
    kind: str


@dataclass
class Rendered:
    text: str
    placed: list[Placed] = field(default_factory=list)
    dropped: list[Edit] = field(default_factory=list)   # inside replaced code, not in a slot
    removed: list[tuple[int, int]] = field(default_factory=list)  # original text not kept


def _contains(a: Edit, b: Edit) -> bool:
    if a.insertion or a is b:
        return False
    if b.insertion:
        return a.start < b.start < a.end
    return a.start <= b.start and b.end <= a.end and (a.start, a.end) != (b.start, b.end)


def check_overlaps(edits: list[Edit]) -> list[str]:
    """Edits that cannot be composed: partial overlaps, or two different edits of one range."""
    problems = []
    real = sorted([e for e in edits if not e.insertion], key=lambda e: (e.start, -e.end))
    for i, a in enumerate(real):
        for b in real[i + 1:]:
            if b.start >= a.end:
                break
            if (a.start, a.end) == (b.start, b.end):
                if not a.same(b):
                    problems.append(f"{a.what} and {b.what} change the same code")
            elif b.end > a.end:
                problems.append(f"{a.what} and {b.what} overlap")
    return problems


def dedupe(edits: list[Edit]) -> list[Edit]:
    out: list[Edit] = []
    for e in edits:
        if not any(o.same(e) for o in out if (o.start, o.end) == (e.start, e.end)):
            out.append(e)
    return out


def render(original: str, edits: list[Edit]) -> Rendered:
    """The new text: `edits` (no overlaps, see check_overlaps) applied inside-out."""
    for k, e in enumerate(edits):
        e.seq = k
    res = Rendered("")
    used: set[int] = set()
    out: list[str] = []
    length = [0]

    def emit(s: str) -> None:
        out.append(s)
        length[0] += len(s)

    def inside(e: Edit, lo: int, hi: int, top: bool) -> bool:
        if e.insertion:
            return lo <= e.start < hi or (top and e.start == hi)
        return lo <= e.start and e.end <= hi

    def render_range(lo: int, hi: int, cands: list[Edit], top: bool) -> None:
        mine = [e for e in cands if inside(e, lo, hi, top)]
        maximal = [e for e in mine if not any(_contains(o, e) for o in mine)]
        maximal.sort(key=lambda e: (e.start, 0 if e.insertion else 1, e.end, e.step, e.seq))
        pos = lo
        for e in maximal:
            if e.start > pos:
                emit(original[pos:e.start])
                pos = e.start
            render_edit(e, [o for o in mine if _contains(e, o)])
            used.add(e.seq)
            pos = max(pos, e.end)
        if hi > pos:
            emit(original[pos:hi])

    def render_edit(e: Edit, inner: list[Edit]) -> None:
        begin = length[0]
        if e.paren:
            emit("(")
        kept = []
        for p in e.parts:
            if isinstance(p, Slot):
                render_range(p.start, p.end, inner, False)
                kept.append((p.start, p.end))
            else:
                b = length[0]
                emit(p.text)
                if p.kind:
                    res.placed.append(Placed(e, b, length[0], p.kind))
        if e.paren:
            emit(")")
        if e.kind:
            res.placed.append(Placed(e, begin + (1 if e.paren else 0),
                                     length[0] - (1 if e.paren else 0), e.kind))
        # Original text of the edit not kept in a slot is gone (its comments too).
        pos = e.start
        for a, b in sorted(kept):
            if a > pos:
                res.removed.append((pos, a))
            pos = max(pos, b)
        if e.end > pos:
            res.removed.append((pos, e.end))

    render_range(0, len(original), edits, True)
    res.text = "".join(out)
    res.dropped = [e for e in edits if e.seq not in used]
    return res


# --- placeholders in templates ---------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\$\$?[A-Za-z_]\w*|\$\d+")


def placeholders(template: str) -> list[tuple[int, int, str]]:
    """($-names outside string literals and comments) as (start, end, name)."""
    from .javasrc import mask
    masked = mask(template)
    return [(m.start(), m.end(), m.group(0)) for m in _PLACEHOLDER.finditer(masked)]


def template_parts(template: str, slots: dict[str, Slot | str | None]) -> list:
    """A template as edit parts: each known $-name becomes its slot (or text). Unknown names
    raise EditError, a name bound to None too (e.g. $recv of a call without a receiver)."""
    parts: list = []
    pos = 0
    for a, b, name in placeholders(template):
        if name not in slots:
            known = ", ".join(sorted(slots)) or "none"
            raise EditError(f"unknown placeholder {name} (here: {known})")
        value = slots[name]
        if value is None:
            raise EditError(f"{name} has no value here")
        if a > pos:
            parts.append(Piece(template[pos:a]))
        parts.append(value if isinstance(value, Slot) else Piece(value))
        pos = b
    if pos < len(template):
        parts.append(Piece(template[pos:]))
    return parts


# --- files and patches -------------------------------------------------------------------------

@dataclass
class Source:
    path: str            # relative to the checkout
    data: bytes
    text: str
    encoding: str = "utf-8"
    bom: bool = False
    mode: str = "100644"

    def encode(self, text: str) -> bytes:
        body = text.encode(self.encoding)
        return (b"\xef\xbb\xbf" if self.bom else b"") + body


def read_source(root: str, path: str) -> Source:
    full = os.path.join(root, path)
    with open(full, "rb") as fh:
        data = fh.read()
    mode = "100755" if os.stat(full).st_mode & 0o111 else "100644"
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    try:
        return Source(path, data, body.decode("utf-8"), "utf-8", bom, mode)
    except UnicodeDecodeError:
        return Source(path, data, body.decode("latin-1"), "latin-1", bom, mode)


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _quote(path: str) -> str:
    """A path as git writes it in patch headers (C-quoted when unusual)."""
    if all(32 <= ord(c) < 127 and c not in '"\\' for c in path):
        return path
    out = []
    for b in path.encode("utf-8"):
        c = chr(b)
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\t":
            out.append("\\t")
        elif c == "\n":
            out.append("\\n")
        elif 32 <= b < 127:
            out.append(c)
        else:
            out.append(f"\\{b:03o}")
    return '"' + "".join(out) + '"'


def _hunks(a: list[str], b: list[str]) -> list[str]:
    out: list[str] = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(3):
        i1, i2 = group[0][1], group[-1][2]
        j1, j2 = group[0][3], group[-1][4]

        def rng(start: int, n: int) -> str:
            return f"{start + 1 if n else start},{n}"
        out.append(f"@@ -{rng(i1, i2 - i1)} +{rng(j1, j2 - j1)} @@\n")
        for tag, x1, x2, y1, y2 in group:
            if tag == "equal":
                out += [" " + ln for ln in a[x1:x2]]
                continue
            out += ["-" + ln for ln in a[x1:x2]]
            out += ["+" + ln for ln in b[y1:y2]]
    # A last line without "\n" gets git's marker.
    fixed = []
    for ln in out:
        if ln.endswith("\n"):
            fixed.append(ln)
        else:
            fixed.append(ln + "\n\\ No newline at end of file\n")
    return fixed


def _lines(text: str) -> list[str]:
    """Lines as git sees them: split at "\n" only (not at form feeds, U+0085, U+2028…)."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def file_patch(old: Source | None, new_path: str, new_text: str | None,
               new_source: Source | None = None) -> bytes:
    """A `git diff`-style patch of one file: changed, renamed (old.path != new_path), created
    (old None) or deleted (new_text None). Full blob ids, for `git apply -3`."""
    enc = old or new_source or Source(new_path, b"", "")
    old_bytes = old.data if old else b""
    new_bytes = enc.encode(new_text) if new_text is not None else b""
    a_path = old.path if old else new_path
    head = [f"diff --git {_quote('a/' + a_path)} {_quote('b/' + new_path)}\n"]
    mode = old.mode if old else "100644"
    if old is None:
        head.append(f"new file mode {mode}\n")
        head.append(f"index {'0' * 40}..{blob_id(new_bytes)}\n")
    elif new_text is None:
        head.append(f"deleted file mode {mode}\n")
        head.append(f"index {blob_id(old_bytes)}..{'0' * 40}\n")
    else:
        if old.path != new_path:
            sim = int(100 * difflib.SequenceMatcher(None, old.text, new_text,
                                                    autojunk=False).quick_ratio())
            head += [f"similarity index {sim}%\n", f"rename from {_quote(old.path)}\n",
                     f"rename to {_quote(new_path)}\n"]
            if old_bytes == new_bytes:
                return "".join(head).encode("utf-8")
        head.append(f"index {blob_id(old_bytes)}..{blob_id(new_bytes)} {mode}\n")
    head.append(f"--- {_quote('a/' + a_path) if old else '/dev/null'}\n")
    head.append(f"+++ {_quote('b/' + new_path) if new_text is not None else '/dev/null'}\n")
    a_lines = _lines(old.text) if old else []
    b_lines = _lines(new_text) if new_text is not None else []
    body = "".join(_hunks(a_lines, b_lines))
    return "".join(head).encode("utf-8") + body.encode(enc.encoding)

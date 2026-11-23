"""A small, forgiving Java source reader for library sources (no SCIP there): finds a type by its
nested names and lists its members with their declarations, line ranges and javadoc.

It masks comments and string/char/text-block literals (keeping offsets and newlines), then walks
braces. It does not build an AST and does not need to: declarations at member level are
`header { body }` or `header ;`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TYPE_DECL = re.compile(r"\b(class|interface|enum|record|@\s*interface)\s+([A-Za-z_$][\w$]*)")
_ANNOTATION = re.compile(r"@\s*[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?")
_IDENT_BEFORE_PAREN = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_CONTROL = {"if", "for", "while", "switch", "catch", "synchronized", "return", "new", "else",
            "do", "try", "throw"}


@dataclass
class Member:
    kind: str           # "method", "constructor", "field", "type", "enum constants"
    name: str
    declaration: str    # header text, whitespace collapsed (annotations included)
    start_line: int     # 1-based, of the declaration (after its javadoc)
    end_line: int       # 1-based, last line of the body or of the declaration
    javadoc: str        # cleaned text, "" when none
    modifiers: tuple[str, ...] = ()
    name_line: int = 0  # 1-based line of the member's name (after its annotations)
    overload: int = 0   # among same-name methods of the type, in declaration order (SCIP +N)

    @property
    def line(self) -> int:
        """Where the member is reported: its name, as for project symbols."""
        return self.name_line or self.start_line


def mask(text: str) -> str:
    """Comments and literal contents replaced by spaces; offsets and newlines preserved."""
    out = list(text)
    i, n = 0, len(text)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            blank(i + 1, j - 1)
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _match_brace(masked: str, open_pos: int) -> int:
    depth = 0
    for k in range(open_pos, len(masked)):
        ch = masked[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(masked) - 1


class JavaSource:
    def __init__(self, text: str):
        self.text = text
        self.masked = mask(text)
        self._line_starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_of(self, pos: int) -> int:
        lo, hi = 0, len(self._line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def lines(self, first: int, last: int) -> list[str]:
        return self.text.split("\n")[first - 1:last]

    # types -------------------------------------------------------------------------------

    def find_type(self, names: list[str]) -> tuple[int, int, int] | None:
        """(declaration start, body '{', body '}') of the nested type names[0].names[1]…"""
        lo, hi = 0, len(self.masked)
        decl = None
        for name in names:
            found = None
            for m in _TYPE_DECL.finditer(self.masked, lo, hi):
                if m.group(2) != name:
                    continue
                brace = self.masked.find("{", m.end(), hi)
                if brace < 0:
                    continue
                line_start = self.masked.rfind("\n", 0, m.start()) + 1
                start = line_start if not self.masked[line_start:m.start()].strip(
                    " \t").endswith((";", "}")) else m.start()
                found = (start, brace, _match_brace(self.masked, brace))
                break
            if found is None:
                return None
            decl = found
            lo, hi = found[1] + 1, found[2]
        return decl

    # members -----------------------------------------------------------------------------

    def members(self, body_open: int, body_close: int, is_enum: bool = False,
                type_name: str = "") -> list[Member]:
        """Members at the top level of a type body. A constructor is a method named like the
        type (`type_name`)."""
        self._type_name = type_name
        m, t = self.masked, self.text
        out: list[Member] = []
        i = body_open + 1
        seg = i
        depth = 0
        paren = 0
        first_segment = True
        while i < body_close:
            ch = m[i]
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
            elif ch == "{" and paren == 0:
                header = m[seg:i]
                if _top_level_equals(header) or (is_enum and first_segment and "," in header):
                    i = _match_brace(m, i) + 1  # an initializer or enum constant body
                    continue
                end = _match_brace(m, i)
                mem = self._member(seg, i, end, True)
                if mem:
                    out.append(mem)
                i = end + 1
                seg = i
                first_segment = False
                continue
            elif ch == ";" and paren == 0:
                if is_enum and first_segment:
                    out.append(Member("enum constants", "", " ".join(t[seg:i].split()),
                                      self.line_of(_skip_space(m, seg)), self.line_of(i), ""))
                else:
                    mem = self._member(seg, i, i, False)
                    if mem:
                        out.append(mem)
                seg = i + 1
                first_segment = False
            i += 1
        return out

    def _member(self, seg: int, header_end: int, end: int, has_body: bool) -> Member | None:
        m, t = self.masked, self.text
        start = _skip_space(m, seg)
        if start >= header_end:
            return None
        header_masked = m[start:header_end]
        javadoc = _javadoc(t[seg:start])
        declaration = " ".join(t[start:header_end].split())
        # Annotations blanked keeping offsets, so positions in `bare` are positions in the file.
        bare = _ANNOTATION.sub(lambda a: re.sub(r"[^\n]", " ", a.group()), header_masked)
        words = bare.split()
        mods = tuple(w for w in words if w in _MODIFIERS)
        if not words or words == ["static"]:
            return None  # initializer block
        td = _TYPE_DECL.search(bare)
        if td and bare.find("(") in (-1,) or (td and td.start() < (bare.find("(") if "(" in bare
                                                                   else len(bare))):
            return Member("type", td.group(2), declaration, self.line_of(start),
                          self.line_of(end), javadoc, mods, self.line_of(start + td.start(2)))
        paren = _top_level_paren(bare)
        if paren >= 0 and _top_level_equals(bare[:paren]):
            paren = -1  # `Type name = new Foo(…)`: a field with an initializer
        if paren >= 0:
            names = list(_IDENT_BEFORE_PAREN.finditer(bare[:paren + 1]))
            name = names[-1].group(1) if names else ""
            if not name or name in _CONTROL:
                return None
            kind = "constructor" if name == getattr(self, "_type_name", "") else "method"
            return Member(kind, name, declaration, self.line_of(start), self.line_of(end),
                          javadoc, mods, self.line_of(start + names[-1].start(1)))
        lhs = _split_top_level_equals(bare)
        idents = list(re.finditer(r"[A-Za-z_$][\w$]*", lhs))
        if not idents:
            return None
        return Member("field", idents[-1].group(), declaration, self.line_of(start),
                      self.line_of(end), javadoc, mods, self.line_of(start + idents[-1].start()))


_MODIFIERS = {"public", "protected", "private", "static", "final", "abstract", "default",
              "synchronized", "native", "transient", "volatile", "strictfp", "sealed",
              "non-sealed"}


def _skip_space(s: str, i: int) -> int:
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _top_level_equals(header: str) -> bool:
    depth = 0
    for ch in header:
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
        elif ch == "=" and depth <= 0:
            return True
    return False


def _split_top_level_equals(header: str) -> str:
    depth = 0
    for k, ch in enumerate(header):
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
        elif ch == "=" and depth <= 0:
            return header[:k]
    return header


def enum_constant(text: str, name: str) -> tuple[str, str, int] | None:
    """(declaration, javadoc, offset within text) of one constant in an enum constant list."""
    masked = mask(text)
    m = re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", masked)
    if not m:
        return None
    depth, k = 0, m.start()
    while k < len(masked):
        ch = masked[k]
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        elif ch == "," and depth == 0:
            break
        k += 1
    return " ".join(text[m.start():k].split()), _javadoc(text[:m.start()][-4000:]), m.start()


def _top_level_paren(s: str) -> int:
    depth = 0
    for k, ch in enumerate(s):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "(" and depth <= 0:
            return k
    return -1


def _javadoc(prefix: str) -> str:
    """The last /** … */ in the text before a declaration, cleaned."""
    k = prefix.rfind("/**")
    if k < 0:
        return ""
    end = prefix.find("*/", k)
    if end < 0:
        return ""
    body = prefix[k + 3:end]
    lines = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("*"):
            line = line[1:]
            if line.startswith(" "):
                line = line[1:]
        lines.append(line.rstrip())
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)

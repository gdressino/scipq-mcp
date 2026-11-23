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
# An annotation use (`@interface` declares an annotation type: not one).
_ANNOTATION = re.compile(r"@\s*(?!interface\b)[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?")
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


# Comments and literals, leftmost first (as a scanner would see them): line and block comments
# (to the end when unterminated), text blocks, and string/char literals (which stop at the end
# of the line; an escape may continue them on the next one).
_LITERALS = re.compile(r'//[^\n]*|/\*.*?(?:\*/|\Z)|""".*?(?:"""|\Z)|"(?:\\.|[^"\\\n])*"?'
                       r"|'(?:\\.|[^'\\\n])*'?", re.S)
_NOT_NEWLINE = re.compile(r"[^\n]")


def _blank(s: str) -> str:
    return " " * len(s) if "\n" not in s else _NOT_NEWLINE.sub(" ", s)


def _mask_match(m: re.Match) -> str:
    t = m.group()
    if t[0] == "/":
        return _blank(t)
    if t.startswith('"""'):
        return t[0] + _blank(t[1:-1]) + t[-1] if len(t) > 1 else t
    q = t[0]
    body = t[1:-1]
    if len(t) > 1 and t[-1] == q and (len(body) - len(body.rstrip("\\"))) % 2 == 0:
        return q + _blank(body) + q  # closed (the last quote is not escaped)
    return q + _blank(t[1:])


def mask(text: str) -> str:
    """Comments and literal contents replaced by spaces; offsets and newlines preserved."""
    return _LITERALS.sub(_mask_match, text)


def strip_comments(text: str) -> str:
    """Comments replaced by spaces (offsets and newlines preserved); literals kept."""
    return _LITERALS.sub(lambda m: _blank(m.group()) if m.group()[0] == "/" else m.group(), text)


def _mask_slow(text: str) -> str:
    """The reference scanner `mask` must agree with (kept for tests)."""
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
                between = self.masked[lo:m.start()]
                if between.count("{") != between.count("}"):
                    continue  # declared deeper (e.g. Builder.OfInt when looking for OfInt)
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
        type (`type_name`). Safe to call from several threads on one instance."""
        m, t = self.masked, self.text
        out: list[Member] = []
        i = body_open + 1
        if is_enum:
            # The constants come first, up to the first `;` outside parentheses and braces
            # (constants may have arguments and bodies), or to the end of the body.
            k, depth = i, 0
            while k < body_close:
                ch = m[k]
                if ch in "({":
                    depth += 1
                elif ch in ")}":
                    depth -= 1
                elif ch == ";" and depth == 0:
                    break
                k += 1
            if m[i:k].strip():
                out.append(Member("enum constants", "", " ".join(t[i:k].split())[:500],
                                  self.line_of(_skip_space(m, i)), self.line_of(k), ""))
            i = k + 1
        seg = i
        paren = 0
        while i < body_close:
            ch = m[i]
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
            elif ch == "{" and paren == 0:
                header = m[seg:i]
                if _top_level_equals(header):
                    i = _match_brace(m, i) + 1  # an initializer: `int[] a = {1, 2};`
                    continue
                end = _match_brace(m, i)
                out.extend(self._member(seg, i, end, type_name))
                i = end + 1
                seg = i
                continue
            elif ch == ";" and paren == 0:
                out.extend(self._member(seg, i, i, type_name))
                seg = i + 1
            i += 1
        return out

    def _member(self, seg: int, header_end: int, end: int, type_name: str) -> list[Member]:
        m, t = self.masked, self.text
        start = _skip_space(m, seg)
        if start >= header_end:
            return []
        header_masked = m[start:header_end]
        javadoc = _javadoc(t[seg:start])
        declaration = " ".join(t[start:header_end].split())
        # Annotations blanked keeping offsets, so positions in `bare` are positions in the file.
        bare = _ANNOTATION.sub(lambda a: re.sub(r"[^\n]", " ", a.group()), header_masked)
        words = bare.split()
        mods = tuple(w for w in words if w in _MODIFIERS)
        if not words or words == ["static"]:
            return []  # initializer block
        td = _TYPE_DECL.search(bare)
        if td and bare.find("(") in (-1,) or (td and td.start() < (bare.find("(") if "(" in bare
                                                                   else len(bare))):
            return [Member("type", td.group(2), declaration, self.line_of(start),
                           self.line_of(end), javadoc, mods, self.line_of(start + td.start(2)))]
        paren = _top_level_paren(bare)
        if paren >= 0 and _top_level_equals(bare[:paren]):
            paren = -1  # `Type name = new Foo(…)`: a field with an initializer
        if paren >= 0:
            names = list(_IDENT_BEFORE_PAREN.finditer(bare[:paren + 1]))
            name = names[-1].group(1) if names else ""
            if not name or name in _CONTROL:
                return []
            kind = "constructor" if name == type_name else "method"
            return [Member(kind, name, declaration, self.line_of(start), self.line_of(end),
                           javadoc, mods, self.line_of(start + names[-1].start(1)))]
        if end > header_end and type_name and words[-1] == type_name:
            # A record's compact constructor: `public Point { … }`.
            k = bare.rfind(type_name)
            return [Member("constructor", type_name, declaration, self.line_of(start),
                           self.line_of(end), javadoc, mods, self.line_of(start + k))]
        out = []
        for name, off in _declarators(bare):  # `int a = 1, b, c[];`: one field each
            out.append(Member("field", name, declaration, self.line_of(start),
                              self.line_of(end), javadoc, mods, self.line_of(start + off)))
        return out


_MODIFIERS = {"public", "protected", "private", "static", "final", "abstract", "default",
              "synchronized", "native", "transient", "volatile", "strictfp", "sealed",
              "non-sealed"}


_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _declarators(bare: str) -> list[tuple[str, int]]:
    """(name, offset) of each variable of a field declaration such as `T a = f(1, 2), b[];`:
    split at commas outside parentheses, brackets, braces and type arguments. In an
    initializer, `<` opens type arguments only after a name and before a type (`new X<A, B>()`),
    so comparisons do not count."""
    parts: list[tuple[int, int]] = []
    depth = gdepth = 0
    start, init = 0, False
    n = len(bare)
    for k, ch in enumerate(bare):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "<":
            if not init:
                depth += 1
            else:
                prev = bare[:k].rstrip()[-1:]
                nxt = bare[k + 1:].lstrip()[:1]
                if (prev.isalnum() or prev in "_$.") and (nxt[:1].isupper() or nxt in ("?", ">",
                                                                                   "@")):
                    gdepth += 1
        elif ch == ">":
            if not init:
                depth -= 1
            elif gdepth and bare[k - 1:k] != "-":
                gdepth -= 1
        elif ch == "=" and depth == 0 and not init and bare[k + 1:k + 2] != "=" and \
                bare[k - 1:k] not in ("!", "<", ">", "="):
            parts.append((start, k))
            init = True
        elif ch == "," and depth == 0 and gdepth == 0:
            if not init:
                parts.append((start, k))
            start, init = k + 1, False
    if not init:
        parts.append((start, n))
    out = []
    for a, b in parts:
        ids = list(_IDENT.finditer(bare, a, b))
        if ids:
            out.append((ids[-1].group(), ids[-1].start()))
    return out


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

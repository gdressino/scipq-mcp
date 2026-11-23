"""A small, forgiving Java source reader for library sources (no SCIP there): finds a type by its
nested names and lists its members with their declarations, line ranges and javadoc.

It masks comments and string/char/text-block literals (keeping offsets and newlines), then walks
braces. It does not build an AST and does not need to: declarations at member level are
`header { body }` or `header ;`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Identifiers: Unicode letters, and any non-ASCII character inside one (Java accepts combining
# marks there: `اذاً`, `અને`); in masked Java code nothing else is non-ASCII.
_TYPE_DECL = re.compile(r"\b(class|interface|enum|record|@\s*interface)\s+((?:[^\W\d]|[$\u0080-\U0010FFFF])[\w$\u0080-\U0010FFFF]*)")
# An annotation use (`@interface` declares an annotation type: not one).
_ANNOTATION = re.compile(r"@\s*(?!interface\b)[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?")
_IDENT_BEFORE_PAREN = re.compile(r"((?:[^\W\d]|[$\u0080-\U0010FFFF])[\w$\u0080-\U0010FFFF]*)\s*\(")
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

    def enum_constants(self, body_open: int, body_close: int) -> list[Member]:
        """Each constant of an enum body (kind "enum constant"), with its javadoc and line."""
        m, t = self.masked, self.text
        i = k = body_open + 1
        depth = 0
        pieces, start = [], i
        while k < body_close:
            ch = m[k]
            if ch in "({":
                depth += 1
            elif ch in ")}":
                depth -= 1
            elif ch == ";" and depth == 0:
                break
            elif ch == "," and depth == 0:
                pieces.append((start, k))
                start = k + 1
            k += 1
        pieces.append((start, k))
        out = []
        for a, b in pieces:
            bare = _ANNOTATION.sub(lambda x: re.sub(r"[^\n]", " ", x.group()), m[a:b])
            ident = _IDENT.search(bare)
            if not ident:
                continue
            pos = a + ident.start()
            decl = " ".join(t[pos:b].split())
            decl = decl.split("{", 1)[0].strip()[:200]
            line = self.line_of(pos)
            out.append(Member("enum constant", ident.group(), decl, line, self.line_of(b),
                              _javadoc(t[a:pos]), ("public", "static", "final"), line))
        return out

    def record_components(self, start: int, body_open: int, name: str
                          ) -> list[tuple[str, str, int]]:
        """[(type, name, offset)] of a record header `record Name<T>(int lo, T hi)`."""
        m, t = self.masked, self.text
        rec = re.search(r"\brecord\s+" + re.escape(name) + r"\b", m[start:body_open])
        if not rec:
            return []
        k = start + rec.end()
        open_paren = m.find("(", k, body_open)
        if open_paren < 0:
            return []
        depth, close = 0, -1
        for j in range(open_paren, body_open):
            if m[j] == "(":
                depth += 1
            elif m[j] == ")":
                depth -= 1
                if depth == 0:
                    close = j
                    break
        if close < 0:
            return []
        out = []
        seg, depth = open_paren + 1, 0
        for j in range(open_paren + 1, close + 1):
            ch = m[j]
            if ch in "(<[":
                depth += 1
            elif ch in ")>]" and j < close:
                depth -= 1
            if (ch == "," and depth == 0) or j == close:
                bare = _ANNOTATION.sub(lambda x: re.sub(r"[^\n]", " ", x.group()), m[seg:j])
                ids = list(_IDENT.finditer(bare))
                if ids:
                    last = ids[-1]
                    typ = " ".join(_ANNOTATION.sub(" ", t[seg:seg + last.start()]).split())
                    out.append((typ.replace("...", "[]"), last.group(), seg + last.start()))
                seg = j + 1
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
            # A constructor is named like its type and has no return type before the name
            # (`public Varargs loadlib(…)` in class loadlib is a method).
            kind = "constructor" if name == type_name and not _return_type_before(
                bare[:names[-1].start(1)]) else "method"
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


_IDENT = re.compile(r"(?:[^\W\d]|[$\u0080-\U0010FFFF])[\w$\u0080-\U0010FFFF]*")


def _return_type_before(head: str) -> bool:
    """Whether a method header (annotations blanked) has a type before the name."""
    head = re.sub(r"<[^<>]*(?:<[^<>]*>[^<>]*)*>", " ", head)  # method type parameters
    return any(w not in _MODIFIERS for w in head.split())


_TYPE_ARGS_TEXT = re.compile(r"[\w$.,\s\[\]?&<>@]*")


def _type_arguments_at(text: str, k: int) -> bool:
    """Whether the `<` at k (in an expression) opens type arguments, as in `new Map<K, V>()`,
    `new HashMap<java.lang.String, X>()`, `new Accessor<boolean[], Boolean>()` or
    `Entry.<A, B>of()`, rather than a comparison like `a < b`."""
    prev = text[:k].rstrip()[-1:]
    if not (prev.isalnum() or prev in "_$."):
        return False
    depth, end = 0, -1
    for j in range(k, min(len(text), k + 400)):
        c = text[j]
        if c == "<":
            depth += 1
        elif c == ">" and text[j - 1:j] != "-":
            depth -= 1
            if depth == 0:
                end = j
                break
        elif c in "(){};=!|+*/%^":
            return False
    if end < 0:
        return False
    inner = text[k + 1:end]
    if not _TYPE_ARGS_TEXT.fullmatch(inner):
        return False
    first = inner.lstrip()[:1]
    return prev == "." or first.isupper() or first in ("?", "@", "") or "." in inner or \
        "[]" in inner.replace(" ", "")


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
            elif _type_arguments_at(bare, k):
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
    m = re.search(r"(?<![\w$\u0080-\U0010FFFF])" + re.escape(name) + r"(?![\w$\u0080-\U0010FFFF])", masked)
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

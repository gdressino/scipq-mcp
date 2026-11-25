"""The source of a type or member as it is in a git ref (`origin/main`, a tag, a commit),
read with git and parsed like the checkout's files, without the index: also code that was
never compiled here (a class added on another branch). Read-only: `git rev-parse`,
`ls-tree`, `grep` and `show`."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

from . import symbols as S
from .queries import QueryError

GIT_TIMEOUT = 60
MAX_CANDIDATES = 10


@dataclass
class Wanted:
    """What a name asks for: package, nested types, and a member (with how to pick it)."""
    package: list[str]
    types: list[str]
    member: str = ""            # "" for the type itself; "<init>" for constructors
    method: bool = False        # written with a parameter list
    params: tuple | None = None  # the parameter types written; None: any overload
    overload: int | None = None  # SCIP's numbering (+N), 0 for the first


def wanted(text: str) -> Wanted:
    """`Account`, `com.example.Account#setState`, `Account.setState(int)`, `Outer.Inner#x`,
    `Account#<init>`, `pick(+1)`, or an exact SCIP symbol."""
    t = text.strip().replace("`", "")
    # A SCIP symbol; a parameter's ends with `().(name)`, a type parameter's with `[T]`.
    if S.looks_like_symbol(t) or ("/" in t and re.search(r"\)\.\([^()]*\)$|\[[\w$]+\]$", t)):
        descs = S.parse(t)
        pkg = [d.name for d in descs if d.suffix == S.NAMESPACE]
        types = [d.name for d in descs if d.suffix == S.TYPE]
        last = descs[-1] if descs else None
        if last is None or not types:
            raise QueryError(f"{text!r} names no type")
        if last.suffix == S.METHOD:
            n = int(last.disambiguator[1:]) if (last.disambiguator or "").startswith("+") else 0
            return Wanted(pkg, types, last.name, True, None, n)
        if last.suffix == S.TERM:
            return Wanted(pkg, types, last.name)
        if last.suffix != S.TYPE:
            raise QueryError(f"{text!r} is a parameter or a type parameter: ask for its method "
                             "or type")
        return Wanted(pkg, types)
    method, params, overload = False, None, None
    m = re.search(r"\(([^()]*)\)\s*$", t)
    if m:
        method, inner, t = True, m.group(1).strip(), t[:m.start()]
        k = re.fullmatch(r"\+(\d+)", inner)
        if k:
            overload = int(k.group(1))
        elif inner not in ("...", "…", "*"):
            params = S.written_params(inner) if inner else ()
    left, _, member = t.partition("#")
    parts = [p for p in re.split(r"[./]", left) if p]
    pkg = []
    while len(parts) > 1 and parts[0][:1].islower():
        pkg.append(parts.pop(0))
    if not member and len(parts) > 1 and (parts[-1][:1].islower() or parts[-1] == "<init>"
                                          or (method and parts[-1] == parts[-2])):
        member = parts.pop()     # `Account.setState`, `Account.Account(String)`
    if not parts or not re.fullmatch(r"[\w$]+", parts[0]):
        raise QueryError(f"{text!r} names no type (write `Type`, `Type#member` or "
                         "`com.example.Type#member`)")
    if member == parts[-1]:
        member = "<init>"        # `Account#Account(String)`
    return Wanted(pkg, parts, member, method or member == "<init>", params, overload)


def _run(root: str, *args: str) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of a git command run in root."""
    git = shutil.which("git")
    if git is None:
        raise QueryError("git is not installed")
    try:
        r = subprocess.run([git, "-c", "core.quotePath=false", *args], cwd=root,
                           capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise QueryError(f"git: {e}") from e
    return (r.returncode, r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _git(root: str, *args: str) -> str:
    code, out, err = _run(root, *args)
    if code != 0:
        raise QueryError("git: " + (err.strip().splitlines() or ["failed"])[-1])
    return out


def commit_of(root: str, ref: str) -> str:
    # Names, `HEAD~2`, `main^`: never an option, a path (`:`) or a pattern.
    if not ref or ref.startswith("-") or re.search(r"[\s\x00-\x1f:?*\[\\]", ref):
        raise QueryError(f"git_ref {ref!r} is not a branch, tag or commit name")
    # The ref cannot start with "-" (checked above): no `--end-of-options`, which older gits
    # do not know.
    code, out, err = _run(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    if code == 0:
        return out.strip()
    if code == 1 and not err.strip():      # --quiet: not found
        raise QueryError(f"git_ref {ref!r}: no such branch, tag or commit here (for a "
                         "remote branch, `git fetch` first)")
    raise QueryError("git: " + (err.strip().splitlines() or ["rev-parse failed"])[-1])


def find_file(root: str, commit: str, w: Wanted, hint: str = "") -> str:
    """The path (relative to root) of the file declaring w's top-level type at the commit:
    named after it (`Account.java`), else the one file declaring it."""
    top = w.types[0]
    files = [p for p in _git(root, "ls-tree", "-r", "--name-only", "-z", commit).split("\0")
             if p == f"{top}.java" or p.endswith(f"/{top}.java")]
    if w.package:
        tail = "/".join(w.package) + f"/{top}.java"
        files = [p for p in files if p == tail or p.endswith("/" + tail)]
    if not files:   # declared in a file named otherwise: the one file declaring it
        word = "[^[:alnum:]_$]"
        code, out, err = _run(root, "grep", "-l", "-z", "-E",
                              f"(^|{word})(class|interface|enum|record)[[:space:]]+"
                              f"{re.escape(top)}({word}|$)", commit, "--", "*.java")
        if code not in (0, 1):     # 1: no match
            raise QueryError("git: " + (err.strip().splitlines() or ["grep failed"])[-1])
        files = [x.split(":", 1)[1] for x in out.split("\0") if ":" in x]
        if w.package:
            pdir = "/".join(w.package) + "/"
            files = [p for p in files if p.startswith(pdir) or "/" + pdir in p]
    if not files:
        raise QueryError(f"no file declares {'.'.join(w.package + [top])} at that ref")
    if len(files) > 1:
        if hint in files:
            return hint
        shown = ", ".join(sorted(files)[:MAX_CANDIDATES])
        more = f" (+{len(files) - MAX_CANDIDATES} more)" if len(files) > MAX_CANDIDATES else ""
        raise QueryError(f"{len(files)} files declare {top} at that ref: {shown}{more} — "
                         "add the package (`com.example." + top + "`)")
    return files[0]


@dataclass
class Block:
    title: str
    first: int                   # 1-based
    lines: list[str]
    end_line: int                # last line of the definition, 1-based


def blocks(text: str, w: Wanted) -> tuple[list[Block], str]:
    """The definitions w names in a file's text, and a note ("" or why several)."""
    from .extras import _annotations_before
    from .javasrc import JavaSource
    from .libsrc import _type_variables, param_types
    src = JavaSource(text)
    decl = src.find_type(w.types)
    if decl is None:
        raise QueryError(f"the file has no type {'.'.join(w.types)}")
    start, body_open, body_close = decl
    tname = ".".join(w.types)
    if not w.member:
        a = src.line_of(_annotations_before(src.masked, start))
        b = src.line_of(body_close)
        return [Block(tname, a, src.lines(a, b), b)], ""
    header = " ".join(src.masked[start:body_open].split())
    is_enum = " enum " in f" {header} "
    members = src.members(body_open, body_close, is_enum=is_enum, type_name=w.types[-1])
    fields = [] if w.method or w.member == "<init>" else _fields(src, members, w, start,
                                                                   body_open, body_close,
                                                                   is_enum)
    if fields:
        return fields, ""
    if w.member == "<init>" or w.method or any(m.kind == "method" and m.name == w.member
                                               for m in members):
        kind = "constructor" if w.member == "<init>" else "method"
        same = [m for m in members if m.kind == kind and (kind == "constructor"
                                                          or m.name == w.member)]
        note = ""
        if w.overload is not None:   # SCIP numbers them in declaration order
            same = same[w.overload:w.overload + 1]
        elif w.params is not None:
            tvars = _type_variables(header, w.types[-1])
            exact = [m for m in same if S.same_params(w.params, param_types(m.declaration,
                                                                              tvars))]
            if not exact and same:
                note = (f"none takes ({', '.join(t or 'T' for t in w.params)}); "
                        f"{'the one' if len(same) == 1 else f'all {len(same)}'} there")
            else:
                same = exact
        if len(same) > 1 and not note:
            note = f"{len(same)} overloads"
        picked = same
    else:
        picked, note = [], ""
    if not picked and w.member == "<init>":
        raise QueryError(f"{tname} declares no constructor"
                         + (f" #{w.overload}" if w.overload else "") + " at that ref")
    if not picked:
        like = sorted({m.name for m in members if w.member.lower() in m.name.lower()})[:8]
        raise QueryError(f"{tname} has no {w.member} at that ref"
                         + (f" (named like it: {', '.join(like)})" if like else ""))
    out = []
    for m in picked:
        label = w.types[-1] if m.kind == "constructor" else m.name
        if m.kind in ("method", "constructor"):
            label += f"({', '.join(t or 'T' for t in param_types(m.declaration) or ())})"
        title = f"{tname}#{label}"
        out.append(Block(title, m.start_line, src.lines(m.start_line, m.end_line), m.end_line))
    return out, note


def _fields(src, members, w: Wanted, start: int, body_open: int, body_close: int,
            is_enum: bool) -> list[Block]:
    """A field, enum constant or record component named w.member."""
    tname = ".".join(w.types)
    picked = [m for m in members if m.kind == "field" and m.name == w.member]
    if not picked and is_enum:
        picked = [m for m in src.enum_constants(body_open, body_close) if m.name == w.member]
    if picked:
        return [Block(f"{tname}#{m.name}", m.start_line, src.lines(m.start_line, m.end_line),
                      m.end_line) for m in picked[:1]]
    for _typ, comp, pos in src.record_components(start, body_open, w.types[-1]):
        if comp == w.member:
            ln = src.line_of(pos)
            return [Block(f"{tname}#{comp}", ln, src.lines(ln, ln), ln)]
    return []


def source_at(root: str, text: str, ref: str, max_lines: int, hint: str = "") -> str:
    """The rendered answer of `source` with git_ref."""
    w = wanted(text)
    commit = commit_of(root, ref)
    path = find_file(root, commit, w, hint)
    content = _git(root, "show", f"{commit}:./{path}")
    found, note = blocks(content, w)
    out = [f"-- read from git at {ref} ({commit[:10]}), {path}: as it is there, without the "
           "index (it need not be compiled here)" + (f"; {note}" if note else "")]
    room = max(1, max_lines)
    for b in found:
        if room <= 0:
            out.append(f"… {len(found) - found.index(b)} more not shown (raise max_lines)")
            break
        shown = b.lines[:room]
        room -= len(shown)
        width = len(str(b.first + len(shown) - 1))
        out.append(f"{b.title} — {path}:{b.first}" + (f"–{b.end_line}" if b.end_line != b.first
                                                      else "") + f" at {ref}")
        out += [f"{b.first + k:>{width}}  {t.rstrip()}" for k, t in enumerate(shown)]
        if len(shown) < len(b.lines):
            out.append(f"… truncated; the body ends at line {b.end_line} (raise max_lines)")
    return "\n".join(out)

"""Comparing two checkouts of a project (worktrees of one repository, or clones): their API
from each one's auxiliary index and, when they share git history, what each side changed since
the merge base, where both changed the same members, and what merging one into the other would
break (symbols one side removed or changed in files only it touched, used by the other side).

It reads the indexes and runs read-only git commands; it never merges or checks out anything.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

from . import symbols as S
from .graph import Graph, scope_matches
from .index import REL_IMPLEMENTATION

_API = re.compile(r"\b(public|protected)\b")
# Annotations are not part of the signature compared (an added @Deprecated breaks nothing).
_ANNOTATION = re.compile(r"@\s*(?!interface\b)[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?\s*")


@dataclass
class Entry:
    kind: str
    signature: str
    source: str
    line: int        # 1-based


@dataclass
class ApiDiff:
    only_a: dict[str, Entry] = field(default_factory=dict)
    only_b: dict[str, Entry] = field(default_factory=dict)
    changed: dict[str, tuple[Entry, Entry]] = field(default_factory=dict)
    renamed: list[tuple[str, str]] = field(default_factory=list)   # (symbol in A, in B)
    moved: list[tuple[str, str]] = field(default_factory=list)     # types in another package


@dataclass
class Use:
    caller: str
    source: str
    lines: list[int]  # 1-based
    new: bool = False  # in a file this side changed since the merge base
    how: str = ""      # "" a use; "overrides it"; "concrete subtype" (must implement it)


@dataclass
class Break:
    symbol: str
    how: str          # "removed", "signature changed" or "abstract method added"
    old: Entry | None  # the other side's definition (None: it has none)
    new: Entry | None  # this side's
    uses: list[Use]

    @property
    def source(self) -> str:
        return (self.old or self.new).source


def is_api(e: Entry) -> bool:
    return _API.search(e.signature) is not None


def api_of(g: Graph, scope: str = "", api_only: bool = True) -> dict[str, Entry]:
    """{symbol: Entry} of the checkout's non-local, non-test definitions; with api_only the
    public and protected ones (interface members are public in their signatures)."""
    sh = g.shards()
    out: dict[str, Entry] = {}
    for sym, kind, shard_id, line, sig in g.con.execute(
            "SELECT s.symbol, d.kind, d.shard_id, d.line, d.signature FROM defs d "
            "JOIN symbols s ON s.id = d.symbol_id"):
        if shard_id not in sh or S.is_local_like(sym) or sh[shard_id][2]:
            continue
        src = sh[shard_id][0]
        if scope and not scope_matches(scope, S.package_of(sym), src):
            continue
        sig = " ".join(_ANNOTATION.sub(" ", sig or "").split())
        if api_only and not _API.search(sig):
            continue
        out[sym] = Entry(kind, sig, src, line + 1)
    return out


def api_diff(a: dict[str, Entry], b: dict[str, Entry]) -> ApiDiff:
    d = ApiDiff()
    for sym, e in a.items():
        other = b.get(sym)
        if other is None:
            d.only_a[sym] = e
        elif other.signature != e.signature:
            d.changed[sym] = (e, other)
    for sym, e in b.items():
        if sym not in a:
            d.only_b[sym] = e
    # Same owner, kind and signature but another name: probably a rename.
    def key(sym: str, e: Entry) -> tuple:
        name = S.name_of(sym)
        return (S.parent(sym), e.kind, re.sub(r"\b" + re.escape(name) + r"\b", "§",
                                              e.signature) if name else e.signature)
    in_b: dict[tuple, list[str]] = {}
    for sym, e in d.only_b.items():
        in_b.setdefault(key(sym, e), []).append(sym)
    for sym, e in d.only_a.items():
        cands = in_b.get(key(sym, e), [])
        if len(cands) == 1:
            d.renamed.append((sym, cands[0]))
    # A type in another package (same simple name, mostly the same member names): moved.
    def members(side: dict[str, Entry], t: str) -> set[str]:
        return {S.name_of(s) for s in side if S.parent(s) == t}
    types_b: dict[str, list[str]] = {}
    for sym in d.only_b:
        if S.is_type(sym) and "#" not in sym[:-1]:  # top-level types
            types_b.setdefault(S.name_of(sym), []).append(sym)
    for sym in d.only_a:
        if not (S.is_type(sym) and "#" not in sym[:-1]):
            continue
        for other in types_b.get(S.name_of(sym), []):
            ma, mb = members(a, sym), members(b, other)
            if not ma and not mb or len(ma & mb) * 2 >= max(len(ma), len(mb)):
                d.moved.append((sym, other))
                break
    return d


def uses_of(g: Graph, symbols: list[str], skip_source: dict[str, str]) -> dict[str, list[Use]]:
    """{symbol: uses} from the checkout's references, leaving out those in `skip_source[sym]`
    (the file that defines it: the merge brings the other side's version of it)."""
    ids = g.ids(symbols)
    if not ids:
        return {}
    by_id = {v: k for k, v in ids.items()}
    sh = g.shards()
    names = {}
    out: dict[str, list[Use]] = {}
    rows = g.callers_of(list(ids.values()))
    caller_ids = {c for _t, c, _s, _x in rows}
    if caller_ids:
        q = ",".join("?" * len(caller_ids))
        names = dict(g.con.execute(f"SELECT id, symbol FROM symbols WHERE id IN ({q})",
                                   list(caller_ids)))
    for target, caller, shard_id, sites in rows:
        if shard_id not in sh:
            continue
        sym = by_id[target]
        src = sh[shard_id][0]
        if src == skip_source.get(sym):
            continue
        lines = sorted({int(x.split(":", 1)[0]) + 1 for x in sites.split(",") if x})
        out.setdefault(sym, []).append(Use(names.get(caller, ""), src, lines))
    for uses in out.values():
        uses.sort(key=lambda u: (u.source, u.lines[:1]))
    return out


# --- git ------------------------------------------------------------------------------------

def _git(root: str, *args: str) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run([git, "-C", root, *args], capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def worktrees(root: str) -> list[dict]:
    """`git worktree list --porcelain`: [{path, head, branch, locked, prunable}]."""
    text = _git(root, "worktree", "list", "--porcelain")
    out: list[dict] = []
    for block in (text or "").split("\n\n"):
        w: dict = {"locked": False, "prunable": False, "branch": ""}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                w["path"] = value
            elif key == "HEAD":
                w["head"] = value
            elif key == "branch":
                w["branch"] = value.removeprefix("refs/heads/")
            elif key in ("locked", "prunable"):
                w[key] = True
            elif key == "bare":
                w["bare"] = True
        if "path" in w and not w.get("bare"):
            out.append(w)
    return out


def head(root: str) -> tuple[str, str]:
    """(commit, branch or "") of a checkout."""
    return (_git(root, "rev-parse", "HEAD") or "",
            _git(root, "rev-parse", "--abbrev-ref", "HEAD") or "")


def merge_base(a_root: str, b_root: str) -> str:
    """The merge base of both checkouts' HEADs, or "" (no git, or no shared history)."""
    b_head = head(b_root)[0]
    if not b_head:
        return ""
    return _git(a_root, "merge-base", "HEAD", b_head) or ""


def changed_files(root: str, base: str) -> set[str]:
    """Files changed since `base` in a checkout (commits and working tree, deleted ones too),
    and untracked ones; relative to the project root."""
    out: set[str] = set()
    for args in (("diff", "--name-only", "-z", "--relative", base),
                 ("ls-files", "-z", "--others", "--exclude-standard")):
        names = _git(root, "-c", "core.quotePath=false", *args)
        if names:
            out |= {n for n in names.split("\0") if n}
    return out


def overriders(g: Graph, symbols: list[str]) -> dict[str, list[Use]]:
    """{method or type: the definitions that override or extend it}, from the relationships."""
    ids = g.ids(symbols)
    if not ids:
        return {}
    by_id = {v: k for k, v in ids.items()}
    sh = g.shards()
    out: dict[str, list[Use]] = {}
    q = ",".join("?" * len(ids))
    for target, sym, shard_id, line, sig in g.con.execute(f"""
            SELECT r.target_id, s.symbol, d.shard_id, d.line, d.signature FROM rels r
            JOIN symbols s ON s.id = r.symbol_id JOIN defs d ON d.symbol_id = r.symbol_id
            WHERE r.kind = ? AND r.target_id IN ({q})""", (REL_IMPLEMENTATION, *ids.values())):
        if shard_id in sh:
            out.setdefault(by_id[target], []).append(
                Use(sym, sh[shard_id][0], [line + 1], how=sig or ""))
    return out


def _concrete(sig: str) -> bool:
    return not re.search(r"\b(abstract|interface)\b", sig or "")


def breaks(removed_or_changed: list[tuple[str, str, Entry | None, Entry | None]],
           changed_here: set[str], changed_there: set[str], g_there: Graph
           ) -> list[Break]:
    """What merging this side into the other would break: of the symbols this side removed,
    changed or added abstract (how, the other side's entry, this side's), those in files only
    this side touched, with what the other side has that depends on them: its uses, its
    overrides (a changed or removed method), its concrete subtypes (a new abstract method).

    Only in files the other side modified or added since the merge base: a file it did not
    touch ends up as this side's version, which this side already adapted (it compiles)."""
    picked = []
    for sym, how, old, new in removed_or_changed:
        src = (old or new).source
        if src in changed_here and src not in changed_there:
            picked.append((sym, how, old, new))
    direct = [p for p in picked if p[1] != "abstract method added"]
    uses = uses_of(g_there, [p[0] for p in direct], {p[0]: p[2].source for p in direct})
    over = overriders(g_there, [p[0] for p in direct if S.is_method(p[0])])
    owners = overriders(g_there, sorted({S.parent(p[0]) for p in picked
                                         if p[1] == "abstract method added"}))
    out = []
    for sym, how, old, new in picked:
        if how == "abstract method added":
            us = [Use(u.caller, u.source, u.lines, how="concrete subtype")
                  for u in owners.get(S.parent(sym), []) if _concrete(u.how)]
        else:
            us = list(uses.get(sym, [])) + [Use(u.caller, u.source, u.lines, how="overrides it")
                                            for u in over.get(sym, [])]
        us = [u for u in us if u.source in changed_there]
        if not us:
            continue
        for u in us:
            u.new = True
        out.append(Break(sym, how, old, new, us))
    out.sort(key=lambda b: (-len(b.uses), b.symbol))
    return out


GROUPS = ("added in A", "removed in B", "added in B", "removed in A", "changed in A",
          "changed in B", "changed on both sides", "unclear")


def classify(diff: ApiDiff, files_a: set[str], files_b: set[str]) -> dict[str, dict]:
    """With a merge base: who did what. A symbol only in A is "added in A" when only A touched
    its file since the base, "removed in B" when only B did; likewise for B and for another
    signature; files both touched are "changed on both sides", neither "unclear"."""
    out: dict[str, dict] = {g: {} for g in GROUPS}

    def who(src: str) -> str:
        a, b = src in files_a, src in files_b
        return "A" if a and not b else "B" if b and not a else "both" if a else "none"

    for sym, e in diff.only_a.items():
        w = who(e.source)
        out[{"A": "added in A", "B": "removed in B", "both": "changed on both sides"}.get(
            w, "unclear")][sym] = e
    for sym, e in diff.only_b.items():
        w = who(e.source)
        out[{"B": "added in B", "A": "removed in A", "both": "changed on both sides"}.get(
            w, "unclear")][sym] = e
    for sym, (ea, eb) in diff.changed.items():
        w = who(ea.source)
        out[{"A": "changed in A", "B": "changed in B", "both": "changed on both sides"}.get(
            w, "unclear")][sym] = (ea, eb)
    return out


def merge_effects(diff: ApiDiff, b_side: bool) -> list[tuple[str, str, Entry | None,
                                                            Entry | None]]:
    """What one side did, as seen from the other: for B (b_side) the symbols only A has
    (B removed them), those with another signature (B's version new), and B's new abstract
    methods; the other way around for A. Types whose signature changed are left out (their
    header changing does not break their uses)."""
    gone = diff.only_a if b_side else diff.only_b
    added = diff.only_b if b_side else diff.only_a
    moved_or_renamed = {x for pair in diff.renamed + diff.moved for x in pair}
    out = [(s, "removed", e, None) for s, e in gone.items()]
    for s, (ea, eb) in diff.changed.items():
        if S.is_type(s):
            continue
        out.append((s, "signature changed", ea, eb) if b_side else
                   (s, "signature changed", eb, ea))
    for s, e in added.items():
        if s not in moved_or_renamed and S.is_method(s) and re.search(r"\babstract\b",
                                                                      e.signature):
            out.append((s, "abstract method added", None, e))
    return out

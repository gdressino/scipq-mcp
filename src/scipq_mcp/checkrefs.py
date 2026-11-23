"""check_refs: the code that Markdown (docs, skills, agent instructions) mentions, checked
against the index.

Mentions: `Type#member`, `Type.method(…)`, `Type.member`, `com.example.Type`, {@link …}, and
paths `dir/File.java` or `dir/File.java:120`. Each is resolved with the auxiliary index at
once (types by simple name, members by type, supertypes), not with a search per mention.
Reported: types and members that are not there (with the closest names, and where a type or
member of that name lives), parameter lists no overload has, names no type of that simple name
has, missing files, and line numbers that no longer fall in the symbol named next to them.
Left alone: library types (in the library index even when the project never uses them),
placeholders (Foo, X, getX…), JDO enhancer members, Object/Class/enum members, constants used
as receivers, and mentions the line itself says are gone ("removed", "no existe").
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass

from . import symbols as S
from .index import REL_IMPLEMENTATION

# Type#member(…) / Type.member(…) / Type.member — Type starts upper case, may be qualified or
# nested (Outer.Inner).
_HASH = re.compile(r"(?<![\w$.…])((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*)#"
                   r"([A-Za-z_$][\w$]*)(\(([^()]*)\))?")
_DOT_CALL = re.compile(r"(?<![\w$.…])((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*)\."
                       r"([a-z_$][\w$]*)\(([^()]*)\)?")
# Not members: file extensions and class literals (Foo.feature, Foo.class).
_NOT_MEMBERS = {"class", "java", "feature", "md", "xml", "sql", "json", "properties", "txt",
                "groovy", "jrxml", "yml", "yaml", "html", "kt", "gradle", "jar", "log"}
# A dotted chain that starts lower case (a package): com.example.Type, com.x.IVA.model.Type,
# com.example.Outer.Inner; type arguments (<…>) end it.
_FQN = re.compile(r"(?<![\w$./])([a-z_][\w$]*(?:\.[\w$]+){2,})(?![\w$(])")
_PATH = re.compile(r"(?<![\w$./-])((?:(?:[\w$.-]+|\.\.\.|…)/)*[\w$-]+\.java)"
                   r"(?::(\d+)(?:-(\d+))?)?")
_CODE = re.compile(r"`[^`\n]+`|\{@link(?:plain)?\s[^}]+\}")
_LIB_ROOTS = {"java/lang/Object#", "java/lang/Enum#", "java/lang/Record#"}
_PLACEHOLDER_TYPES = {"Foo", "Bar", "Baz", "Qux", "Xyz", "X", "Y", "Z", "T", "A", "B", "C",
                      "MyClass", "MiClase", "Clase", "Archivo", "Tipo", "Example", "Ejemplo",
                      "Something", "Algo", "Whatever", "Type", "Class", "File"}
_PLACEHOLDER_MEMBERS = {"x", "y", "z", "getX", "getY", "setX", "setY", "currentX", "currentY",
                        "foo", "bar", "baz", "metodo", "método", "method", "campo", "field",
                        "algo", "something", "doSomething", "hacerAlgo", "whatever"}
# Members every class, Class object or enum has (java.lang.Object, Class, Enum).
_BUILTIN_MEMBERS = {
    "getClass", "hashCode", "equals", "toString", "clone", "finalize", "wait", "notify",
    "notifyAll", "name", "ordinal", "values", "valueOf", "compareTo", "getDeclaringClass",
    "describeConstable", "getName", "getSimpleName", "getCanonicalName", "getTypeName",
    "newInstance", "forName", "isInstance", "cast", "getResource", "getResourceAsStream",
    "getMethod", "getMethods", "getField", "getFields", "getDeclaredField",
    "getDeclaredFields", "getDeclaredMethod", "getDeclaredMethods", "getConstructor",
    "getDeclaredConstructor", "isAssignableFrom", "getAnnotation", "getAnnotations",
    "isAnnotationPresent", "getPackage", "getPackageName", "getSuperclass", "getInterfaces",
    "getModifiers", "isEnum", "isInterface", "isRecord", "getEnumConstants",
    "getRecordComponents", "getClassLoader", "length"}
_ENHANCER = re.compile(r"(dn|jdo)[A-Z_]\w*")
_GONE = re.compile(r"no existen?\b|\bni existen?\b|ya no (existe|est[aá])|no est[aá] m[aá]s|does(?: not|n't) "
                   r"exist|no longer|\bremoved\b|\bdeleted\b|\brenamed\b|\bdropped\b|"
                   r"eliminad[oa]s?\b|borrad[oa]s?\b|renombrad[oa]s?\b|retirad[oa]s?\b|"
                   r"quitad[oa]s?\b|se (borr|elimin|quit|retir|renombr)(ó|o|aron)\b|"
                   r"\bobsolet[oe]s?\b", re.I)
# What follows these is an example, not a name in the code.
_EXAMPLE = re.compile(r"por ejemplo|p\. ?ej\.|\bej\.|\be\.g\.|for example|for instance|"
                      r"\bsuch as\b|\bcomo\b `|hypothetical|hipot[eé]tic", re.I)
# Fenced blocks in another language (```python …): not Java names.
_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)")
_JAVA_FENCES = {"", "java", "jshell", "kotlin", "kt", "groovy", "scala", "text", "txt",
                "plain", "diff", "log"}
# Folders of closed or historical docs, left out unless files names them.
ARCHIVE_DIRS = {"archive", "archived", "archives", "archivo", "archivados", "descartados",
                "discarded", "obsolete", "done", "old"}
_JDK_ROOTS = ("java/", "javax/", "jakarta/", "jdk/", "sun/", "com/sun/")
KINDS = {"no-type": "types not found", "no-member": "members not found",
         "params": "parameter lists no overload has", "ambiguous": "names in several types",
         "no-file": "files not found", "past-end": "lines past the end",
         "line-moved": "lines no longer in the symbol named"}


@dataclass
class Problem:
    path: str
    line: int
    mention: str
    what: str
    kind: str = ""


class Model:
    """The project's definitions from the auxiliary index, for name lookups."""

    def __init__(self, g, cache_base: str | None = None):
        self.g = g
        self.types: dict[str, list[str]] = {}       # simple name -> type symbols (project)
        self.members: dict[str, dict[str, list[str]]] = {}   # type -> name -> signatures
        self.member_syms: dict[tuple[str, str], list[str]] = {}
        self.where: dict[str, tuple[str, int, int | None, int | None]] = {}
        self.types_in: dict[str, list[str]] = {}    # source -> types defined there
        self.nested: dict[str, list[str]] = {}      # type -> types declared inside it
        self.by_member: dict[str, list[str]] = {}   # member name -> types declaring it
        sh = g.shards()
        for sym, shard_id, line, b0, b1, sig in g.con.execute(
                "SELECT s.symbol, d.shard_id, d.line, d.body_start, d.body_end, d.signature "
                "FROM defs d JOIN symbols s ON s.id = d.symbol_id"):
            if shard_id not in sh or S.is_local_like(sym):
                continue
            if sym not in self.where:
                self.where[sym] = (sh[shard_id][0], line + 1, b0, b1)
            if S.is_type(sym):
                self.types.setdefault(S.name_of(sym), []).append(sym)
                self.types_in.setdefault(sh[shard_id][0], []).append(sym)
                if S.parent(sym).endswith("#"):
                    self.nested.setdefault(S.parent(sym), []).append(sym)
            parent = S.parent(sym)
            if parent.endswith("#") and S.kind_of(sym) in ("method", "term", "constructor",
                                                           "type"):
                mname = S.name_of(S.parent(sym)) if S.kind_of(sym) == "constructor" \
                    else S.name_of(sym)
                self.members.setdefault(parent, {}).setdefault(mname, []).append(sig or "")
                self.member_syms.setdefault((parent, mname), []).append(sym)
                self.by_member.setdefault(mname, []).append(parent)
        for k in self.types:
            self.types[k] = sorted(set(self.types[k]))
        self.external = {x for (x,) in g.con.execute(
            "SELECT symbol FROM symbols WHERE symbol LIKE '%#' AND id NOT IN "
            "(SELECT symbol_id FROM defs)")}
        self.external_names = {S.name_of(x) for x in self.external}
        self.roots = {"/".join(S.package_of(t).split(".")[:2]) for ts in self.types.values()
                      for t in ts if S.package_of(t)}
        self._supers: dict[str, list[str]] = {}
        self._cache_base = cache_base
        self._library: set[str] | None = None
        self._packages: set[str] | None = None
        self._stale: dict[str, str] = {}

    def library_names(self) -> set[str]:
        """Simple names of the library classes the project could mean: those in the library
        index (sources jars and the JDK seen on this machine) under the JDK's packages or a
        package root the project uses (com/vaadin, org/springframework…), even when the
        project never names that class. Loaded on first need."""
        if self._library is None:
            self._library = set()
            used = {"/".join(x.split("/")[:2]) for x in self.external if "/" in x}
            from .cachedir import default_base
            path = os.path.join(self._cache_base or default_base(), "libraries.sqlite")
            if os.path.exists(path):
                try:
                    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
                    for (name,) in con.execute("SELECT DISTINCT name FROM classes"):
                        if name.startswith(("java/", "javax/", "jakarta/")) or \
                                "/".join(name.split("/")[:2]) in used:
                            self._library.add(re.split(r"[/$]", name)[-1])
                    con.close()
                except sqlite3.Error:
                    pass
            self._library |= _jar_class_names(used - {"java/lang", "java/util"})
        return self._library

    def is_library(self, simple: str) -> bool:
        return simple in self.external_names or simple in self.library_names()

    def stale_library(self, simple: str) -> str:
        """For a library class the project does not use: a note when only old versions of
        its artifact have it ("only in ui-data 1.2; 3.0 has none"), else ""."""
        if simple in self.external_names:
            return ""
        if simple in self._stale:
            return self._stale[simple]
        from .cachedir import default_base
        from .libsrc import _version_key
        note = ""
        path = os.path.join(self._cache_base or default_base(), "libraries.sqlite")
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            have: dict[str, set[str]] = {}
            for (coords,) in con.execute(
                    "SELECT DISTINCT j.coordinates FROM classes c JOIN jars j ON j.id = c.jar_id "
                    "WHERE c.name LIKE ? OR c.name LIKE ?", (f"%/{simple}", f"%${simple}")):
                ga, _, ver = coords.rpartition(":")
                if not ga or coords.startswith("JDK"):
                    have = {}
                    break
                have.setdefault(ga, set()).add(coords)
            stale = []
            for ga, with_it in have.items():
                every = [c for (c,) in con.execute(
                    "SELECT DISTINCT coordinates FROM jars WHERE coordinates LIKE ?",
                    (ga + ":%",))]
                newest = max(every, key=_version_key)
                if newest in with_it:
                    stale = []
                    break
                stale.append((ga.rsplit(":", 1)[-1], max(with_it, key=_version_key)
                              .rsplit(":", 1)[-1], newest.rsplit(":", 1)[-1]))
            con.close()
            if stale:
                art, old, new = stale[0]
                note = f"only in {art} {old}; {new} has none"
        except sqlite3.Error:
            note = ""
        self._stale[simple] = note
        return note

    def supertypes(self, t: str) -> list[str]:
        if t not in self._supers:
            ids = self.g.ids([t])
            if t not in ids:
                self._supers[t] = []
            else:
                self._supers[t] = [x for x in self.g.names([y for (y,) in self.g.con.execute(
                    "SELECT DISTINCT target_id FROM rels WHERE symbol_id = ? AND kind = ?",
                    (ids[t], REL_IMPLEMENTATION))]).values() if x.endswith("#")]
        return self._supers[t]

    def resolve_type(self, written: str) -> tuple[list[str], bool]:
        """(project type symbols the written name may be, whether it is a library type).
        Nested types may be written Outer.Inner or Outer$Inner; a partial package
        (model.Account) matches by suffix."""
        parts = written.replace("$", ".").split(".")
        simple = parts[-1]
        cands = self.types.get(simple, [])
        if len(parts) > 1:
            pkg = [p for p in parts if p[:1].islower()]
            outer = [p for p in parts[:-1] if p[:1].isupper()]
            want_pkg = "/".join(pkg)
            cands = [c for c in cands
                     if (not pkg or _pkg_path(c) == want_pkg or
                         _pkg_path(c).endswith("/" + want_pkg))
                     and all(f"/{o}#" in c or c.startswith(f"{o}#") or f"#{o}#" in c
                             for o in outer)]
        library = not cands and self.is_library(simple)
        return cands, library

    def member(self, t: str, name: str) -> tuple[list[str] | None, bool]:
        """(signatures of `name` in the type or its project supertypes, None when absent;
        whether a library supertype might declare it)."""
        seen, todo, lib = set(), [t], False
        found: list[str] = []
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            if x in self.members:
                found += self.members[x].get(name, [])
                todo += self.supertypes(x)
            elif x not in _LIB_ROOTS:
                lib = True
        return (found or None), lib

    def is_package(self, path: str) -> bool:
        """Whether a/b/c is a package of the project (or a prefix of one)."""
        if self._packages is None:
            self._packages = {_pkg_path(t) for ts in self.types.values() for t in ts}
        return any(p == path or p.startswith(path + "/") for p in self._packages)

    def in_nested(self, t: str, name: str) -> bool:
        """A member of a type declared inside it (a builder's fluent methods)."""
        return any(self.member(x, name)[0] is not None for x in self.nested.get(t, []))

    def names_in(self, t: str) -> list[str]:
        out = set(self.members.get(t, {}))
        for s in self.supertypes(t):
            out |= set(self.members.get(s, {}))
        return sorted(out)

    def elsewhere(self, name: str, but: str = "") -> str:
        """" (a name is in A, B)" for the types that declare a member of that name."""
        owners = sorted({S.name_of(t) for t in self.by_member.get(name, []) if t != but})
        return f" ({name} is in {', '.join(owners[:3])}{' …' if len(owners) > 3 else ''})" \
            if owners else ""

    def type_hint(self, simple: str) -> str:
        """" (Account is at com.example.core.Account)" when a type of that name exists."""
        at = [_qualified(t) for t in self.types.get(simple, [])]
        return f" ({simple} is at {', '.join(at[:2])}{' …' if len(at) > 2 else ''})" if at \
            else ""


def _jar_class_names(roots: set[str], max_jars: int = 400) -> set[str]:
    """Simple names of the classes in the binary jars of these package roots (org/datanucleus)
    in the Gradle and Maven caches: library classes whose sources are not downloaded."""
    import glob
    import zipfile
    home = os.path.expanduser("~")
    gradle = os.environ.get("GRADLE_USER_HOME") or os.path.join(home, ".gradle")
    out: set[str] = set()
    seen = 0
    for r in sorted(roots):
        group = r.replace("/", ".")
        paths = glob.glob(os.path.join(gradle, "caches", "modules-2", "files-2.1", group + "*",
                                       "*", "*", "*", "*.jar")) + \
            glob.glob(os.path.join(home, ".m2", "repository", r, "**", "*.jar"), recursive=True)
        for jar in paths:
            if jar.endswith(("-sources.jar", "-javadoc.jar")) or seen >= max_jars:
                continue
            seen += 1
            try:
                with zipfile.ZipFile(jar) as z:
                    for n in z.namelist():
                        if n.endswith(".class") and "$" not in n:
                            out.add(n.rsplit("/", 1)[-1][:-len(".class")])
            except (OSError, zipfile.BadZipFile):
                continue
    return out


def _pkg_path(sym: str) -> str:
    return S.package_of(sym).replace(".", "/")


def _qualified(sym: str) -> str:
    return ".".join(d.name for d in S.parse(sym) if d.suffix in (S.NAMESPACE, S.TYPE))


def _expand_globs(root: str, globs: list[str]) -> list[str]:
    """A folder (docs, docs/, docs/**) means the Markdown files under it."""
    out = []
    for g_ in globs:
        g2 = g_.rstrip("/")
        if g2.endswith("/**") or g2.endswith("/*"):
            g2 = g2.rsplit("/", 1)[0]
            out.append(f"{g2}/**/*.md")
        elif not any(c in g2 for c in "*?[") and os.path.isdir(os.path.join(root, g2)):
            out.append(f"{g2}/**/*.md")
        else:
            out.append(g_)
    return out


def _listed(root: str) -> list[str]:
    """The checkout's files: tracked and untracked but not ignored (git), else a walk."""
    git = shutil.which("git")
    listed = None
    if git:
        try:
            proc = subprocess.run([git, "-C", root, "-c", "core.quotePath=false", "ls-files",
                                   "-z", "--cached", "--others", "--exclude-standard"],
                                  capture_output=True, timeout=60)
            if proc.returncode == 0:
                listed = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
        except (OSError, subprocess.SubprocessError):
            listed = None
    if listed is None:
        listed = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in (".git", "build", "target", "node_modules")]
            for n in names:
                listed.append(os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/"))
    return listed


def _files(root: str, globs: list[str], exclude: list[str],
           listed: list[str] | None = None) -> list[str]:
    listed = _listed(root) if listed is None else listed

    def match(p: str, gl: str) -> bool:
        if fnmatch.fnmatch(p, gl) or (gl.startswith("**/") and fnmatch.fnmatch(p, gl[3:])):
            return True
        # dir/**/*.md also matches dir/x.md
        return "/**/" in gl and fnmatch.fnmatch(p, gl.replace("/**/", "/"))
    globs = _expand_globs(root, globs)
    exclude = [x.rstrip("/") + "/*" if os.path.isdir(os.path.join(root, x.rstrip("/")))
               else x for x in exclude]
    return sorted(p for p in listed if any(match(p, g_) for g_ in globs)
                  and not any(match(p, x) for x in exclude))


def _params(text: str) -> tuple:
    from .refactor import _link_params
    return _link_params(text or "")


def _closest(name: str, names: list[str]) -> str:
    near = difflib.get_close_matches(name, names, n=2, cutoff=0.7)
    return f" (did you mean {', '.join(near)}?)" if near else ""


def _placeholder_type(written: str) -> bool:
    simple = written.replace("$", ".").split(".")[-1]
    return simple in _PLACEHOLDER_TYPES or len(simple) <= 2 or \
        re.fullmatch(r"(Some|Any|Mi|My|Foo|Bar|Baz|Xyz|Xxx)[A-Z]\w*", simple) is not None


def check(g, root: str, globs: list[str], exclude: list[str], cache_base: str | None = None,
          skip_archives: bool = True) -> tuple[list[Problem], int, int, int]:
    """(problems, mentions checked, files read, files left out as archives). With
    skip_archives, files under folders of closed or historical docs (archive/, done/…) are
    left out."""
    m = Model(g, cache_base)
    sources = {v[0] for v in m.where.values()}
    by_suffix: dict[str, list[str]] = {}
    for s in sources:
        parts = s.split("/")
        for k in range(len(parts)):
            by_suffix.setdefault("/".join(parts[k:]), []).append(s)
    problems: list[Problem] = []
    checked = 0
    listed = _listed(root)
    files = _files(root, globs, exclude, listed)
    # Java files in the checkout the index does not have (a build without the plugin, a
    # tool's own folder): they exist.
    for f in listed:
        if f.endswith(".java") and f not in sources:
            parts = f.split("/")
            for k in range(len(parts)):
                by_suffix.setdefault("/".join(parts[k:]), [])
                if f not in by_suffix["/".join(parts[k:])]:
                    by_suffix["/".join(parts[k:])].append(f)
    unindexed = {f for f in listed if f.endswith(".java") and f not in sources}
    archived = 0
    if skip_archives:
        kept = [f for f in files if not ARCHIVE_DIRS & {d.lower() for d in f.split("/")[:-1]}]
        archived, files = len(files) - len(kept), kept
    line_counts: dict[str, int] = {}
    text_cache: dict[str, list[str]] = {}

    def count_lines(src: str) -> int:
        if src not in line_counts:
            try:
                with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                    line_counts[src] = fh.read().count("\n") + 1
            except OSError:
                line_counts[src] = 0
        return line_counts[src]

    for path in files:
        try:
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        fence = None                 # the language of the fenced block we are in
        # Path abbreviations the document declares: `I/` = `core/src/…/billing/`
        abbrev = {k: v for k, v in re.findall(r"`([\w.-]+/)`\s*=\s*`([^`\s]+/)`", text)}
        for ln, line in enumerate(text.split("\n"), 1):
            f = _FENCE.match(line)
            if f:
                fence = None if fence is not None else f.group(2).lower()
                continue
            if fence is not None and fence not in _JAVA_FENCES:
                continue
            code = [mo.span() for mo in _CODE.finditer(line)]
            gone = _GONE.search(line) is not None
            examples = [e.end() for e in _EXAMPLE.finditer(line)]
            at = [0]

            def in_code(pos: int) -> bool:
                return any(a <= pos < b for a, b in code)

            def add(mention: str, what: str, kind: str) -> None:
                hypothetical = any(e <= at[0] <= e + 60 for e in examples) and \
                    kind in ("no-type", "no-member", "no-file")
                if not gone and not hypothetical:
                    problems.append(Problem(path, ln, mention, what, kind))
            named_here: list[tuple[str, int]] = []   # (symbol, where the line names it)
            spans = []
            for mo in _HASH.finditer(line):
                spans.append(mo.span())
                if mo.group(2) in _NOT_MEMBERS:
                    continue
                checked += 1
                at[0] = mo.start()
                named_here += [(x, mo.start()) for x in _check_member(
                    m, mo.group(0), mo.group(1), mo.group(2),
                    mo.group(4) if mo.group(3) else None, True, add)]
            for mo in _DOT_CALL.finditer(line):
                if any(a <= mo.start() < b for a, b in spans) or mo.group(2) in _NOT_MEMBERS:
                    continue
                spans.append(mo.span())
                checked += 1
                at[0] = mo.start()
                named_here += [(x, mo.start()) for x in _check_member(
                    m, mo.group(0).rstrip(), mo.group(1), mo.group(2), mo.group(3),
                    in_code(mo.start()), add, call=True)]
            for mo in _FQN.finditer(line):
                if any(a <= mo.start() < b for a, b in spans) or not in_code(mo.start()):
                    continue
                checked += 1
                at[0] = mo.start()
                named_here += [(x, mo.start()) for x in _check_qualified(m, mo.group(1), add)]
            for mo in _PATH.finditer(line):
                checked += 1
                at[0] = mo.start()
                _check_path(m, root, mo, by_suffix, sources | unindexed, named_here, line,
                            code, count_lines, add, text_cache, os.path.dirname(path), abbrev)
    return problems, checked, len(files), archived


def _check_qualified(m: Model, chain: str, add) -> list[str]:
    """A qualified name: some split of it into package and type must be a project type (a
    package may have upper-case segments: com.x.IVA.model.Type)."""
    segs = chain.split(".")
    uppers = [i for i, x in enumerate(segs) if x[:1].isupper()]
    if not uppers:
        return []
    for i in uppers:
        pkg = "/".join(segs[:i])
        cands = [c for c in m.types.get(segs[i], []) if _pkg_path(c) == pkg]
        if cands:
            # Outer.Inner: the nested part must be there too, else the outer type is enough.
            for j in range(i + 1, len(segs)):
                if not segs[j][:1].isupper():
                    break
                inner = [c for c in m.types.get(segs[j], []) if c.startswith(cands[0])]
                if not inner:
                    break
                cands = inner
            return cands
    if "/".join(segs[:2]) not in m.roots or m.is_library(segs[uppers[0]]):
        return []           # a library's or another project's package
    if m.is_package("/".join(segs)):
        return []           # a package with an upper-case segment (com.x.IVA)
    last = max(i for i in uppers if all(x[:1].isupper() for x in segs[uppers[0]:i + 1]))
    fqn = ".".join(segs[:last + 1])
    simple = segs[uppers[0]]
    add(fqn, "no such type" + (m.type_hint(simple) or _closest(simple, list(m.types))),
        "no-type")
    return []


_HEADER_LINE = re.compile(r"\s*(@|\*|/\*\*|/\*|//|\*/)")


def _with_header(root: str, src: str, lo: int, cache: dict) -> int:
    """The first line of a definition including its javadoc and annotations above it."""
    if src not in cache:
        try:
            with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                cache[src] = fh.read().split("\n")
        except OSError:
            cache[src] = []
    lines = cache[src]
    k = lo - 1                       # 0-based index of the first line
    for _ in range(60):
        if k - 1 < 0 or k - 1 >= len(lines) or not _HEADER_LINE.match(lines[k - 1]):
            break
        k -= 1
    return k + 1


def _check_path(m: Model, root: str, mo, by_suffix, sources, named_here, line, code,
                count_lines, add, text_cache: dict, doc_dir: str = "",
                abbrev: dict | None = None) -> None:
    written, n0 = mo.group(1), mo.group(2)
    for k, v in (abbrev or {}).items():
        if written.startswith(k):
            written = v + written[len(k):]
            break
    hits = _find_path(written, by_suffix, sources)
    if not hits:
        for base_dir in ("", doc_dir):      # from the checkout, or from the document
            p = os.path.normpath(os.path.join(base_dir, written)).replace(os.sep, "/")
            if os.path.isfile(os.path.join(root, p)):
                hits = [p]
                break
    base = os.path.basename(written)
    if not hits:
        # Folders left out (core/Account.java for core/src/…/Account.java): the written ones
        # appear in that order in the one source of that name.
        dirs = [d for d in written.split("/")[:-1] if d not in ("", ".", "..", "...", "…")]
        same = sorted({s for s in sources if os.path.basename(s) == base})
        if dirs:
            hits = [s for s in same if _in_order(dirs, s.split("/")[:-1])]
    if not hits:
        bare = written.lstrip("./")
        stem = base[:-len(".java")]
        if bare.startswith(_JDK_ROOTS) or _placeholder_type(stem) or \
                (stem not in m.types and m.is_library(stem)) or \
                re.search(r"(?i)(?:_(?:main|old|new|copy|bak|orig|tmp)$|^(?:main|old|copy)_)",
                          stem):
            return          # a library's, a placeholder, a scratch copy (X_main.java)
        pkg_like = re.match(r"(?:.*/)?((?:org|com|net|io|ar|es)/[\w$]+)/", bare)
        if pkg_like and pkg_like.group(1) not in m.roots and "src/" not in bare:
            return          # another project's or a library's package path
        same = sorted({s for s in sources if os.path.basename(s) == base})
        add(mo.group(0), "no such file" + (f" (a {base} is at {', '.join(same[:2])})"
                                          if same else ""), "no-file")
        return
    if n0 is None:
        return
    n = int(n0)
    fits = [s for s in hits if n <= count_lines(s)]
    if not fits:
        add(mo.group(0), f"line {n} is past the end ("
            + ", ".join(f"{s}: {count_lines(s)} lines" for s in hits[:3]) + ")", "past-end")
        return
    # What the mention's own part of the line names (its sentence, table cell or · item; a
    # name glued to the path first: `name (File:N)`, `File:N — name`, `File:N (name)`):
    # Type#member mentions and method names in its code spans that the files' types declare.
    # The line number must fall in one of them, or be a call of it.
    a, b = _segment(line, mo.start(), mo.end())
    glued = _glued_names(line[a:b], mo.group(0))
    near = [(x, y) for x, y in code if a <= x < b]
    bare_names = glued or {x for p, q in near for x in
                           re.findall(r"(?<![\w$.])([a-z_$][\w$]*)\s*\(", line[p:q])}
    constructor = re.search(r"\bconstructor\b", line[a:b], re.I) is not None
    spans_here = []
    for src in fits:
        syms = [x for x, at in named_here if a <= at < b and m.where.get(x, ("",))[0] == src
                and (not glued or S.name_of(x) in glued)]
        for t in m.types_in.get(src, []):
            for name in bare_names:
                syms += [x for x in m.member_syms.get((t, name), []) if S.is_method(x)]
            if constructor:
                syms += m.member_syms.get((t, S.name_of(t)), [])
        lines = text_cache.get(src) or []
        if not lines:
            _with_header(root, src, 1, text_cache)
            lines = text_cache.get(src) or []
        called = {S.name_of(x) for x in syms if any(
            re.search(r"(?<![\w$])" + re.escape(S.name_of(x)) + r"\s*\(", lines[k])
            for k in range(max(0, n - 3), min(len(lines), n + 2)))}
        if called:
            return          # the line is a call of the name: the doc points at the call
        for sym in syms:
            w = m.where.get(sym)
            if w is None or w[0] != src:
                continue
            lo = min((w[2] + 1) if w[2] is not None else w[1], w[1])
            hi = (w[3] + 1) if w[3] is not None else w[1]
            spans_here.append((sym, _with_header(root, src, lo, text_cache), hi))
    if spans_here and not any(lo - 2 <= n <= hi + 1 for _s, lo, hi in spans_here):
        sym, lo, hi = min(spans_here, key=lambda x: min(abs(n - x[1]), abs(n - x[2])))
        add(mo.group(0), f"line {n} is not in {S.short(sym)} any more (now lines {lo}–{hi})",
            "line-moved")


_SEPARATORS = re.compile(r" · |;\s|\|(?!\|)|(?<=[.!?])\s+(?=[A-Z¿¡`(])")


def _segment(line: str, start: int, end: int) -> tuple[int, int]:
    """The part of a doc line a mention belongs to: between separators (" · ", "; ", a table
    cell's "|", a sentence end)."""
    a, b = 0, len(line)
    for sep in _SEPARATORS.finditer(line):
        if sep.end() <= start:
            a = sep.end()
        elif sep.start() >= end:
            b = sep.start()
            break
    return a, b


def _glued_names(segment: str, path_mention: str) -> set[str]:
    """Member names written right next to a path: `name (File:N)`, `File:N — name`,
    `File:N (name)`."""
    p = re.escape(path_mention)
    out = set()
    for rx in (r"([A-Za-z_$][\w$]*)\s*(?:\(\s*\))?`?\s*\(\s*`?" + p,
               p + r"`?\s*(?:—|–|:|-)\s*`?(?:[\w$]+[#.])?([A-Za-z_$][\w$]*)",
               p + r"`?\s*\(\s*`?(?:[\w$]+[#.])?([A-Za-z_$][\w$]*)"):
        for m_ in re.finditer(rx, segment):
            name = m_.group(1)
            if name[:1].islower() or name == "constructor":
                out.add(name)
    return out - {"constructor", "el", "la", "the", "line", "linea", "líneas", "lines"}


def _in_order(want: list[str], have: list[str]) -> bool:
    k = 0
    for d in have:
        if k < len(want) and d == want[k]:
            k += 1
    return k == len(want)


def _find_path(written: str, by_suffix: dict, sources: set[str]) -> list[str]:
    """Sources a written path names: a suffix of a path, or with `.../` standing for any
    folders (core/.../model/Account.java)."""
    w = written.lstrip("./") if not written.startswith("...") else written
    if "..." not in w and "…" not in w:
        return by_suffix.get(w, [])
    parts = [p for p in re.split(r"(?:^|/)(?:\.\.\.|…)(?:/|$)", w) if p]
    if not parts:
        return []
    tail = parts[-1]
    out = []
    for s in by_suffix.get(tail, []):
        pos = 0
        ok = True
        for part in parts[:-1]:
            k = s.find(part, pos)
            if k < 0:
                ok = False
                break
            pos = k + len(part)
        if ok:
            out.append(s)
    return out


def _check_member(m: Model, mention: str, written_type: str, name: str, params,
                  strict: bool, add, call: bool = False) -> list[str]:
    """Checks Type#name / Type.name(…); the symbols it resolved to."""
    if _placeholder_type(written_type) or name in _PLACEHOLDER_MEMBERS or \
            (len(name) == 1 and name.isalpha()) or re.fullmatch(r"__\w+__", name) or \
            re.fullmatch(r"_?[a-z0-9]+(_[a-z0-9]+)+", name):
        return []           # placeholders, and Python's dunders and snake_case
    parts = written_type.replace("$", ".").split(".")
    simple = parts[-1]
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", simple) and len(parts) > 1:
        # Type.CONSTANT.call(): the receiver is a constant of Type; check that instead.
        return _check_member(m, mention, ".".join(parts[:-1]), simple, None, strict, add)
    cands, lib = m.resolve_type(written_type)
    if not cands:
        # A constant or a field as the receiver (LOG.info, MAX.x), a library type, or prose
        # (an unknown capitalized word followed by .call(): only code spans, links and #
        # forms are sure to be code).
        if lib and strict and m.stale_library(simple):
            add(mention, f"no type {written_type} in the libraries' current versions ("
                + m.stale_library(simple) + ")", "no-type")
            return []
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", simple) or lib or not strict or \
                _is_library_like(written_type):
            return []
        add(mention, f"no type {written_type}" + (m.type_hint(simple) if "." in written_type
                                                   else "")
            + _closest(simple, list(m.types)), "no-type")
        return []
    if name in _BUILTIN_MEMBERS or _ENHANCER.fullmatch(name):
        return cands[:1]
    if len(cands) > 1:
        found = [c for c in cands if m.member(c, name)[0] is not None]
        if not found:
            if any(m.member(c, name)[1] for c in cands) or simple in m.external_names:
                return cands[:1]
            add(mention, f"none of the {len(cands)} types named {written_type} has {name} ("
                + ", ".join(S.package_of(c) for c in cands[:3]) + ")"
                + m.elsewhere(name), "ambiguous")
            return []
        cands = found
    t = cands[0]
    sigs, lib_super = m.member(t, name)
    if sigs is None:
        if lib_super or simple in m.external_names or m.in_nested(t, name):
            return [t]          # maybe inherited from, or meant as, a library type
        add(mention, f"{S.short(t)} has no member {name}"
            + (_closest(name, m.names_in(t)) or m.elsewhere(name, t)), "no-member")
        return [t]
    if params is not None and params.strip() and not call:
        want = _params(params)
        from .refactor import _param_types
        have = [_param_types(s) for c in cands for s in (m.member(c, name)[0] or [])
                if "(" in s]
        if have and want not in have:
            shown = "; ".join("(" + ", ".join(h) + ")" for h in have[:3])
            add(mention, f"no {name}({', '.join(want)}) in {S.short(t)}: its overloads take "
                f"{shown}", "params")
    return [x for c in cands for x in m.member_syms.get((c, name), [])] or [t]


def _is_library_like(written: str) -> bool:
    return written.split(".")[0] in ("System", "Math", "String", "Integer", "Long", "List",
                                     "Map", "Set", "Arrays", "Collections", "Objects",
                                     "Optional", "Stream", "Collectors", "Files", "Paths",
                                     "Path", "LocalDate", "LocalDateTime", "Instant",
                                     "Duration", "BigDecimal", "Thread", "Class")


def _cut(text: str, width: int = 80) -> str:
    """A long mention cut in the middle, so that its end (a line number) stays."""
    return text if len(text) <= width else text[:width // 2 - 1] + "…" + text[-(width // 2):]


def render(problems: list[Problem], checked: int, nfiles: int, offset: int, limit: int,
           fmt: str = "text", archived: int = 0) -> str:
    if not nfiles:
        return "check_refs: no file matches (files: globs relative to the checkout)"
    note = (f"\n({archived} files under archive folders ({', '.join(sorted(ARCHIVE_DIRS))}) "
            "left out: name them in files to check them)") if archived else ""
    # The same mention with the same problem in one file: once, with its lines.
    merged: dict[tuple, list[int]] = {}
    first: dict[tuple, Problem] = {}
    for p in problems:
        key = (p.path, p.mention, p.what)
        merged.setdefault(key, []).append(p.line)
        first.setdefault(key, p)
    kinds: dict[str, int] = {}
    for key in merged:
        k = first[key].kind or "other"
        kinds[k] = kinds.get(k, 0) + 1
    head = (f"check_refs: {checked} mentions in {nfiles} files; "
            f"{len(merged) or 'no'} problem{'s' if len(merged) != 1 else ''}")
    if not merged:
        return head + note
    head += ": " + ", ".join(f"{n} {KINDS.get(k, k)}" for k, n in
                             sorted(kinds.items(), key=lambda kv: -kv[1]))
    rows = []
    by_file: dict[str, list[tuple]] = {}
    for key in merged:
        by_file.setdefault(key[0], []).append(key)
    if fmt == "md":
        rows.append("| file | line | mention | problem |")
        rows.append("|---|---|---|---|")
        for f in sorted(by_file):
            for key in by_file[f]:
                lines = merged[key]
                cell = ", ".join(str(x) for x in lines[:6]) + (" …" if len(lines) > 6 else "")
                rows.append(f"| {f} | {cell} | `{_cut(key[1])}` | {key[2].replace('|', '/')} |")
        shown = rows[:2] + rows[2:][offset:offset + limit]
        more = offset + limit < len(rows) - 2
        return "\n".join([head, ""] + shown + ([f"\nnext offset {offset + limit}"] if more
                                                else [])) + note
    for f in sorted(by_file):
        n = sum(len(merged[k]) for k in by_file[f])
        rows.append(f"{f} ({n})")
        for key in sorted(by_file[f], key=lambda k: merged[k][0]):
            lines = merged[key]
            at = f"line {lines[0]}" + (
                f" ×{len(lines)} (also {', '.join(map(str, lines[1:4]))}"
                f"{' …' if len(lines) > 4 else ''})" if len(lines) > 1 else "")
            rows.append(f"  {at}  {_cut(key[1])}  — {key[2]}")
    lines_out = [head + " (by file):"] + rows[offset:offset + limit]
    if offset + limit < len(rows):
        lines_out.append(f"next offset {offset + limit}")
    return "\n".join(lines_out) + note

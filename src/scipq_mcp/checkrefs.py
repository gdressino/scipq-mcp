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

In a git checkout, line numbers that fail are followed from the version of the code the doc
was written against (see _since_written), and missing files are looked up among the deleted.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field

from . import symbols as S
from .index import REL_IMPLEMENTATION

# Type#member(…) / Type.member(…) / Type.member — Type starts upper case, may be qualified or
# nested (Outer.Inner).
_HASH = re.compile(r"(?<![\w$.…])((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*)#"
                   r"([A-Za-z_$][\w$]*)(\(([^()]*)\))?")
_DOT_MEMBER = re.compile(r"((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*)\.([a-z_$][\w$]*)")
_DOT_CALL = re.compile(r"(?<![\w$.…])((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*)\."
                       r"([a-z_$][\w$]*)\(([^()]*)\)?")
# Not members: file extensions and class literals (Foo.feature, Foo.class).
_NOT_MEMBERS = {"class", "java", "feature", "md", "xml", "sql", "json", "properties", "txt",
                "groovy", "jrxml", "yml", "yaml", "html", "kt", "gradle", "jar", "log", "csv",
                "xls", "xlsx", "pdf", "png", "jpg", "svg", "zip", "sh", "py", "js", "ts", "css",
                "toml", "ini", "conf", "jasper", "war", "ear", "tsv", "doc", "docx", "gz"}
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
# Members a line calls without naming them (string concatenation, try-with-resources,
# for-each, collections): never the name a line number must hold.
_IMPLICIT = {"toString", "hashCode", "equals", "compareTo", "close", "iterator", "getClass",
             "finalize", "valueOf", "values", "ordinal"}
# More line numbers of the same file after a path's: ,42-43 /50 , :61 + 80-84 y :61 →:70
_MORE_LINES = re.compile(r"(?:[,/]:?(\d+)(?:-(\d+))?|`?(?:,\s*|\s+(?:y|and|e)\s+|\s*(?:\+|→|->)\s*)"
                         r"`?:(\d+)(?:-(\d+))?|\s*(?:\+|→|->)\s*(\d+)(?:-(\d+))?"
                         r"|\s+(?:y|and)\s+(\d+)(?:-(\d+))?(?=\)|,|;|\s*$))(?![\w.])")
# A line number alone after a path, for the same file: `:30`, (:41, :45)
_BARE = re.compile(r"(?:(?<=[\s`(,/])|^):(\d+)(?:-(\d+))?(?![\w.])")
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
         "never-file": "files the repository never had then",
         "never-member": "members their type never had then",
         "line-moved": "lines no longer in the symbol named",
         "line-shifted": "line numbers that only moved (new number given)",
         "line-changed": "lines changed since the doc named them",
         "line-off": "line numbers already off in the doc's own version"}


@dataclass
class Problem:
    path: str
    line: int
    mention: str
    what: str
    kind: str = ""
    # line-moved: the source, its lines named and the name looked for
    src: str = ""
    lines: tuple = ()
    name: str = ""
    names: tuple = ()
    span: tuple = ()            # the named member's lines now (with its javadoc)
    tight: bool = False         # the names are written right at the mention
    more: tuple = ()            # more lines listed with it (:40,42-43)
    hint: str = ""
    loose: bool = False         # Type.member without parentheses: only when git confirms
    # lines -> whether they would hold the name: to try the lines a newer version moved to
    recheck: object = field(default=None, repr=False, compare=False)


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
        self.generated: set[str] = set()            # Lombok's and other generated members
        for sym, shard_id, line, b0, b1, sig, flags in g.con.execute(
                "SELECT s.symbol, d.shard_id, d.line, d.body_start, d.body_end, d.signature, "
                "d.flags FROM defs d JOIN symbols s ON s.id = d.symbol_id"):
            if shard_id not in sh or S.is_local_like(sym):
                continue
            if flags & 1:
                self.generated.add(sym)
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

    def type_hint(self, simple: str, near: str = "") -> str:
        """" (Account is at com.example.core.Account)" when a type of that name exists, the
        ones sharing most of the written package first."""
        at = sorted((_qualified(t) for t in self.types.get(simple, [])),
                    key=lambda q: -len(os.path.commonprefix([q, near])))
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
          skip_archives: bool = True, other_versions: list[str] = ()
          ) -> tuple[list[Problem], int, int, int]:
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
    # A mention right after a branch's name or label (main `X.foo()`, v4 `X.foo`): that
    # branch's code, not this one's.
    elsewhere = re.compile(r"(?<![\w-])(?:main|master" + "".join(
        "|" + re.escape(v) for v in other_versions) + r")\s+`?$")
    # A configured label covers the rest of its sentence or list, up to another version
    # word (v4 `A`, `B`; v5 `C`), across a line break too.
    label = re.compile(r"(?<![\w/.-])(?:" + "|".join(map(re.escape, other_versions)) +
                       r")(?![\w/-])", re.I) if other_versions else None
    # (not a folder: src/main/java)
    version_word = re.compile(r"(?<![\w/.-])(?:v\d+(?:\.\d+)*|main|master)(?![\w/-])", re.I)
    sentence_end = re.compile(r"[.!?](?=\s+[A-Z¿¡`(-])|;\s| · |\|")

    def labelled(text: str, pos: int, before: str) -> bool:
        """The text up to pos belongs to a labelled stretch (another version)."""
        if label is None:
            return False
        ends = [x.end() for x in sentence_end.finditer(text, 0, pos)]
        start = max(ends, default=0)
        stretch = text[start:pos]
        if not ends and not re.match(r"\s*(?:[-*#>|]|\d+\.)", text):
            tail = before[max([x.end() for x in sentence_end.finditer(before)], default=0):]
            stretch = tail + " " + stretch      # the sentence began on the line before
        last = list(label.finditer(stretch))
        if not last:
            return False
        after = stretch[last[-1].end():]
        return not any(w.group(0).lower() != last[-1].group(0).lower()
                       for w in version_word.finditer(after))

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
        prev = ""
        for ln, line in enumerate(text.split("\n"), 1):
            before_line, prev = prev, line
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

            def add(mention: str, what: str, kind: str, **extra) -> None:
                hypothetical = any(e <= at[0] <= e + 60 for e in examples) and \
                    kind in ("no-type", "no-member", "no-file")
                if not gone and not hypothetical and \
                        not elsewhere.search(line[max(0, at[0] - 40):at[0]]) and \
                        not labelled(line, at[0], before_line):
                    problems.append(Problem(path, ln, mention, what, kind, **extra))
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
            # A code span that is just `Type.member` (a field, or a method named without
            # its parentheses).
            # Without parentheses it may as well be a key (a YAML section's, an annotation
            # attribute): reported only when git shows the type had that member.
            for p_, q_ in code:
                mo = _DOT_MEMBER.fullmatch(line, p_ + 1, q_ - 1)
                if mo is None or mo.group(2) in _NOT_MEMBERS or \
                        m.is_package(mo.group(0).replace(".", "/")):
                    continue
                spans.append(mo.span())
                checked += 1
                at[0] = mo.start()
                named_here += [(x, mo.start()) for x in _check_member(
                    m, mo.group(0), mo.group(1), mo.group(2), None, False,
                    lambda *a, **e: add(*a, loose=True, **e))]
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
                if any(a <= mo.start() < b for a, b in spans) or not in_code(mo.start()) or \
                        re.match(r"<[^<>\s,]*>[\w$]|[*{]", line[mo.end():]):
                    continue        # a name template: com.x.RunGroup<N>Test, com.x.Run*
                checked += 1
                at[0] = mo.start()
                named_here += [(x, mo.start()) for x in _check_qualified(m, mo.group(1), add)]
            for mo in _PATH.finditer(line):
                checked += 1
                at[0] = mo.start()
                _check_path(m, root, mo, by_suffix, sources | unindexed, named_here, line,
                            code, count_lines, add, text_cache, os.path.dirname(path), abbrev,
                            other_versions, before_line)
    _since_written(root, problems)
    _gone(root, problems)
    return [p for p in problems if p.kind not in ("drop", "weak", "quoted") and
            (not p.loose or "gone from" in p.what)], checked, len(files), archived


def _git(root: str, *args: str, data: bytes | None = None) -> bytes | None:
    try:
        r = subprocess.run(["git", "-C", root, *args], input=data, capture_output=True,
                           timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _hunks(diff: str) -> dict[str, list[tuple[int, int, int, int]] | None]:
    """Per file of a `git diff -U0`, its hunks (old start, old count, new start, new count);
    None for a file that was not there before."""
    out: dict[str, list[tuple[int, int, int, int]] | None] = {}
    for chunk in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
        head = chunk.split("\n", 1)[0]
        path = head.split(" b/", 1)[1] if " b/" in head else ""
        if re.search(r"^new file mode", chunk[:500], re.M):
            out[path] = None
            continue
        out[path] = [(int(a), int(b or 1), int(c), int(d or 1))
                     for a, b, c, d in re.findall(
                         r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", chunk, re.M)]
    return out


def _map_line(hunks: list[tuple[int, int, int, int]], n: int) -> int | None:
    """Line n of the old version in the new one; None when a hunk changed or removed it."""
    shift = 0
    for a, b, c, d in hunks:
        if b == 0:                  # lines inserted after line a
            if n > a:
                shift = c + d - 1 - a
            continue
        if n < a:
            break
        if n < a + b:
            return None
        # Lines removed (d = 0): git gives the new line before the gap, as for insertions.
        shift = c + d - a - b if d else c + 1 - a - b
    return n + shift


def _gone(root: str, problems: list[Problem]) -> None:
    """Missing files (that no other file of that name replaces) and members: when the
    repository dropped them (on any branch) since the doc's first version, or that it did
    not have them then (a plan, another repository's)."""
    files = [p for p in problems if p.kind == "no-file" and p.name]
    members = [p for p in problems if p.kind == "no-member" and p.src and p.name]
    if not (files or members) or not os.path.isdir(root) or shutil.which("git") is None:
        return
    log = _git(root, "log", "--all", "--format=C %ct", "--name-only", "--",
               *sorted({p.path for p in files + members}))
    if not log:
        return              # docs git does not have: written now, against this checkout
    prefix = (_git(root, "rev-parse", "--show-prefix") or b"").decode().strip()
    first: dict[str, int] = {}          # doc -> its first version's time
    ts = 0
    for row in log.decode(errors="replace").splitlines():
        if row.startswith("C "):
            ts = int(row.split()[1])
        elif row.startswith(prefix) and row:
            doc = row[len(prefix):]
            first[doc] = min(first.get(doc, ts), ts)

    files = [p for p in files if p.path in first]
    if files:
        names = {p.name for p in files}
        # In this checkout's history (another branch may still have it, or never had it).
        log = _git(root, "log", "--no-renames", "--diff-filter=D", "--format=C %H %cs",
                   "--name-only", f"--since={min(first[p.path] for p in files) - _READ_BEFORE}")
        last: dict[str, tuple[str, str]] = {}
        commit = None
        for row in (log or b"").decode(errors="replace").splitlines():
            if row.startswith("C "):
                _c, sha, d = row.split()
                commit = (sha, d)
            elif row and commit and os.path.basename(row) in names:
                last.setdefault(os.path.basename(row), commit)     # newest first
        for p in files:
            if p.name in last:
                sha, d = last[p.name]
                p.what = f"no such file (deleted {d}, {sha[:8]})"
            elif log is not None:
                p.what = "no such file, nor deleted since the doc was written (planned, or " \
                         "another repository's?)"
                p.kind = "never-file"
    texts: dict[str, str] = {}
    for p in members:
        if p.path not in first:
            continue
        if p.src not in texts:
            try:
                with open(os.path.join(root, p.src), encoding="utf-8", errors="replace") as fh:
                    texts[p.src] = fh.read()
            except OSError:
                texts[p.src] = ""
        if re.search(r"(?<![\w$])" + re.escape(p.name) + r"(?![\w$])", texts[p.src]):
            continue        # the file still names it (a call, a comment): no history to tell
        # The newest commit that changed how often the file names it: where it went away.
        out = _git(root, "log", "-1", "--format=%H %cs", f"-S{p.name}",
                   f"--since={first[p.path] - _READ_BEFORE}", "--", p.src)
        if out is None:
            continue
        if out.strip():
            sha, d = out.decode().split()
            p.what = _also(p.what, f"gone from {os.path.basename(p.src)} since {d}, {sha[:8]}")
        elif p.hint:            # a close name, or the name in another type: the doc's slip
            p.what = _also(p.what, "not there when the doc was written either")
        else:
            p.what = _also(p.what, "not there when the doc was written either: planned?")
            p.kind = "never-member"


def _also(what: str, more: str) -> str:
    """A message with one more remark in its parentheses."""
    return what[:-1] + "; " + more + ")" if what.endswith(")") else what + f" ({more})"


def _blobs(root: str, names: list[str]) -> dict[str, str]:
    """git cat-file --batch: the text of each object name (a blob id or <commit>:<path>)."""
    out = _git(root, "cat-file", "--batch", data="\n".join(names).encode() + b"\n") or b""
    texts: dict[str, str] = {}
    pos, k = 0, 0
    while pos < len(out) and k < len(names):
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        head = out[pos:nl].split()
        if len(head) < 3 or head[1] != b"blob":
            pos, k = nl + 1, k + 1          # missing, or not a file
            continue
        size = int(head[2])
        texts[names[k]] = out[nl + 1:nl + 1 + size].decode(errors="replace")
        pos, k = nl + 1 + size + 1, k + 1
    return texts


def _holds(lines: list[str], n: int, end: int, names: tuple) -> bool:
    """The code lines n..end (±2) of an older version name one of names (a getter: its
    field), or fall in the declaration of one (with its javadoc and annotations); with no
    name to look for, any lines do."""
    names = tuple(x for x in names if x)
    if not names:
        return True
    last = _statement_end(lines, n, end)
    span = "\n".join(c for c in lines[max(0, n - 3):last + 2]
                     if not re.match(r"\s*(//|\*|/\*)", c))
    notes = "\n".join(c for c in lines[max(0, n - 3):last + 2]
                      if re.match(r"\s*(//|\*|/\*)", c))
    for x in names:
        q = re.escape(x)
        if re.search(r"(?<![\w$])" + q + r"(?![\w$])", span) or \
                re.search(r"(?<![\w$])" + q + r"\(|[.#]" + q + r"(?![\w$])", notes):
            return True         # in a comment, only written as code
        for y in (x, _property(x)):
            if y and any(lo - 2 <= end and n <= hi + 1 for lo, _d, hi in _declared(lines, y)):
                return True
    return False


def _statement_end(lines: list[str], n: int, end: int) -> int:
    """end, or the line where a call open at the lines n..end closes (up to 8 lines on)."""
    depth = sum(_no_strings(c).count("(") - _no_strings(c).count(")")
                for c in lines[max(0, n - 1):end])
    k = end
    while depth > 0 and k < min(len(lines), end + 8):
        depth += _no_strings(lines[k]).count("(") - _no_strings(lines[k]).count(")")
        k += 1
    return k


def _property(name: str) -> str:
    """The field an accessor is named after: getOwner, setOwner -> owner; isOpen -> open."""
    acc = re.match(r"(?:get|set|is|has)([A-Z][\w$]*)$", name)
    return acc.group(1)[:1].lower() + acc.group(1)[1:] if acc else ""


_MODIFIERS = r"(?:public|protected|private|static|final|abstract|synchronized|default|native)"


def _declared(lines: list[str], x: str) -> list[tuple[int, int, int]]:
    """Where the text of a Java file declares the method, field or type x: (first line with
    its javadoc and annotations, the declaration's line, last line of its body), 1-based."""
    q = re.escape(x)
    decl = re.compile(r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:" + _MODIFIERS + r"\s+)*"
                      r"(?:(?:<[^>]*>\s*)?[\w$.]+(?:<[^;=]*>)?(?:\[\])*\s+" + q + r"\s*[(=;]"
                      r"|(?:class|interface|enum|record|@interface)\s+" + q + r"\b)")
    out = []
    for k, text in enumerate(lines):
        if x not in text or not decl.match(text) or \
                re.match(r"\s*(?:return|new|else|throw)\b", text):
            continue
        depth, opened, j = 0, False, k
        for j in range(k, min(len(lines), k + 3000)):
            code = _no_strings(lines[j].split("//")[0])
            if not opened and ";" in code and "{" not in code:
                break           # abstract, interface method or field
            depth += code.count("{") - code.count("}")
            opened = opened or "{" in code
            if opened and depth <= 0:
                break
        top = k
        while top > 0 and _HEADER_LINE.match(lines[top - 1]):
            top -= 1
        out.append((top + 1, k + 1, j + 1))
    return out


# How far back from the commit that first had a doc line its author may have read the code
# (a doc written over days, committed later or on another branch), and how many versions.
_READ_BEFORE = 21 * 86400
_VERSIONS_BACK = 8


def _renamed_from(root: str, paths: list[str]) -> dict[str, str]:
    """Older paths of files (paths from the repository's top): old path -> current one, from
    the commits that added each path (a rename there, R100, keeps its history)."""
    found: dict[str, str] = {}
    frontier = {x: x for x in paths}
    for _ in range(3):              # a doc renamed twice, three times
        adds: dict[str, list[str]] = {}
        commit = None
        out = _git(root, "log", "--all", "--no-renames", "--diff-filter=A", "--format=C %H",
                   "--name-only", "--", *[f":(top){x}" for x in frontier])
        for row in (out or b"").decode(errors="replace").splitlines():
            if row.startswith("C "):
                commit = row[2:]
            elif row in frontier and commit:
                adds.setdefault(commit, []).append(row)
        nxt: dict[str, str] = {}
        for c in adds:
            shown = _git(root, "show", "--format=", "-M", "--name-status", c) or b""
            for row in shown.decode(errors="replace").splitlines():
                parts = row.split("\t")
                if len(parts) == 3 and parts[0].startswith("R") and parts[2] in frontier and \
                        parts[1] not in found and parts[1] not in paths:
                    nxt[parts[1]] = frontier[parts[2]]
        if not nxt:
            break
        found.update(nxt)
        frontier = nxt
    return found


def _block_at(old: list[str], cur: list[str], n: int, end: int, near: int):
    """Where the lines n..end of an older version stand, unchanged, in the current one (the
    occurrence nearest to near): (first, last) or None. Only a block with some text of its
    own (not a lone brace or blank) counts."""
    block = [x.strip() for x in old[n - 1:end]]
    if len(block) != end - n + 1 or not any(len(x) > 3 and x not in ("} else {", "*/", "/**")
                                             for x in block):
        return None
    k0 = next(k for k, x in enumerate(block) if len(x) > 3)
    hits = [i - k0 for i, x in enumerate(cur) if x.strip() == block[k0] and i - k0 >= 0 and
            [y.strip() for y in cur[i - k0:i - k0 + len(block)]] == block]
    if not hits:
        return None
    # A repeated line: the copy whose surroundings match the old ones best, then the
    # nearest.
    around = {x.strip() for x in old[max(0, n - 7):n - 1] + old[end:end + 6] if len(x.strip()) > 3}

    def context(h: int) -> int:
        return len(around & {x.strip() for x in cur[max(0, h - 6):h] +
                             cur[h + len(block):h + len(block) + 6]})
    i = max(hits, key=lambda h: (context(h), -abs(h + 1 - near)))
    return i + 1, i + len(block)


def _since_written(root: str, problems: list[Problem]) -> None:
    """Line numbers written against an older version of the code. The version is the first
    commit (on any branch, through the doc's renames) whose copy of the doc has that line,
    the version just before it (doc and code committed together), or, when the lines named
    do not hold the name there, the newest of its ancestors' versions of the file from the
    weeks before that does. From it to the checkout, lines whose text stands unchanged get
    their new number (when they still hold the name); lines edited since say so; lines that
    did not hold the name in that version either say that. Lines that hold the name now only
    through a call or a comment ("weak") are reported when that version had the
    declaration there."""
    todo = [p for p in problems if p.recheck is not None]
    if not todo or not os.path.isdir(root) or shutil.which("git") is None:
        return
    prefix = _git(root, "rev-parse", "--show-prefix")
    if prefix is None:
        return
    top = prefix.decode().strip()
    days: dict[str, str] = {}               # commit -> its day, in its own time zone

    def commits(out: bytes | None):
        """Rows of a log with --format=C %H %ct %cs: (commit, time) and each other row."""
        commit = None
        for row in (out or b"").decode(errors="replace").splitlines():
            if row.startswith("C "):
                _c, sha, ts, day = row.split()
                commit = (sha, int(ts))
                days[sha] = day
            elif row and commit:
                yield commit, row
    doc_of = {top + d: d for d in {p.path for p in todo}}
    doc_of.update({old: doc_of[now] for old, now in _renamed_from(root, list(doc_of)).items()})
    log = _git(root, "log", "--all", "--full-history", "--date-order", "--reverse", "--raw",
               "--no-abbrev", "--no-renames", "--format=C %H %ct %cs", "--",
               *[f":(top){x}" for x in sorted(doc_of)])
    if not log:
        return
    versions: dict[str, list[tuple[str, int, str]]] = {}   # doc -> (commit, time, blob)
    for commit, row in commits(log):
        if row.startswith(":") and "\t" in row:
            meta, path = row.split("\t", 1)
            blob = meta.split()[3]
            if blob.strip("0") and path in doc_of:
                versions.setdefault(doc_of[path], []).append((*commit, blob))
    for vs in versions.values():
        vs.sort(key=lambda v: v[1])         # oldest first, across the doc's paths
    blobs = sorted({b for vs in versions.values() for *_x, b in vs})
    doc_texts = {b: set(t.split("\n")) for b, t in _blobs(root, blobs).items()}
    doc_lines: dict[str, list[str]] = {}
    written: list[tuple[Problem, str, int]] = []           # (problem, commit, time)
    for p in todo:
        if p.path not in doc_lines:
            try:
                with open(os.path.join(root, p.path), encoding="utf-8", errors="replace") as fh:
                    doc_lines[p.path] = fh.read().split("\n")
            except OSError:
                doc_lines[p.path] = []
        text = doc_lines[p.path][p.line - 1] if p.line <= len(doc_lines[p.path]) else None
        first = next(((c, t) for c, t, b in versions.get(p.path, [])
                      if text is not None and text in doc_texts.get(b, ())), None)
        if first:
            written.append((p, *first))
        elif p.kind == "weak":
            p.kind = "drop"
    if not written:
        return
    # The version just before each of those commits (the doc written, then committed with
    # the code it describes).
    parents: dict[str, tuple[str, int]] = {}
    for (sha, _ts), row in commits(_git(root, "log", "--no-walk", "--format=C %H %ct %cs%n%P",
                                        *sorted({c for _p, c, _t in written}))):
        for parent in row.split()[:1]:
            parents[sha] = (parent, 0)
    for (sha, ts), _row in commits(_git(root, "log", "--no-walk", "--format=C %H %ct %cs%n",
                                        *sorted({x for x, _t in parents.values()}))):
        for k, v in parents.items():
            if v[0] == sha:
                parents[k] = (sha, ts)
    olds = _blobs(root, sorted({f"{c}:{top}{p.src}" for p, c, _t in written} |
                               {f"{parents[c][0]}:{top}{p.src}" for p, c, _t in written
                                if c in parents}))

    def held(p: Problem, old: str | None, slack: int = 0) -> bool:
        if old is None:
            return False
        text = old.split("\n")
        if p.kind == "quoted":          # the doc's version had the code quoted at the line
            return any(p.names[0] in re.sub(r"\s+", "", x)
                       for x in text[max(0, p.lines[0] - 2):p.lines[0] + 1])
        if p.kind == "weak":            # the doc pointed at the declaration then
            return _at_declaration(text, *p.lines, p.names or (p.name,))
        return any(_holds(text, max(1, lo - slack), hi + slack, p.names or (p.name,))
                   for lo, hi in [p.lines, *p.more])
    base: dict[tuple[str, int], list[Problem]] = {}
    holding: set[int] = set()           # problems whose version held the name there
    later: list[tuple[Problem, str, int]] = []
    for p, c, t in written:
        first = [(c, t)] + ([parents[c]] if c in parents else [])
        hit = next((v for v in first if held(p, olds.get(f"{v[0]}:{top}{p.src}"))), None)
        if hit:
            base.setdefault(hit, []).append(p)
            holding.add(id(p))
        elif p.kind in ("weak", "quoted"):
            p.kind = "drop"     # it was not the declaration (the code) then either
        elif (near := next((v for v in first if held(p, olds.get(f"{v[0]}:{top}{p.src}"),
                                                     slack=4)), None)):
            base.setdefault(near, []).append(p)     # a few lines off in the doc's version
        else:
            later.append((p, c, t))
    if later:
        # The file's versions in the weeks before, among the commit's ancestors (-m: a
        # merge's version is one too, a branch that just took main's).
        log = _git(root, "log", "--all", "--full-history", "-m", "--no-renames",
                   "--format=C %H %ct %cs", "--name-only",
                   f"--since={min(t for *_x, t in later) - _READ_BEFORE}",
                   f"--until={max(t for *_x, t in later)}", "--",
                   *sorted({p.src for p, _c, _t in later}))
        touched: dict[str, list[tuple[str, int]]] = {}     # src -> (commit, time)
        for commit, row in commits(log):
            if row.startswith(top) and commit not in touched.get(row[len(top):], ()):
                touched.setdefault(row[len(top):], []).append(commit)
        ancestors: dict[str, set[str]] = {}
        for c, t in {(c, t) for _p, c, t in later}:
            out = _git(root, "rev-list", f"--since={t - _READ_BEFORE}", c) or b""
            ancestors[c] = set(out.decode().split())
        backs = [sorted((x for x in touched.get(p.src, []) if x[0] in ancestors.get(c, ())
                         and x[0] != c), key=lambda x: -x[1])[:_VERSIONS_BACK]
                 for p, c, t in later]
        olds.update(_blobs(root, sorted({f"{c2}:{top}{p.src}" for (p, _c, _t), back in
                                         zip(later, backs) for c2, _t2 in back})))
        for (p, c, t), back in zip(later, backs):
            seen = [(c, t)] + ([parents[c]] if c in parents else []) + back
            texts = [(c2, t2, old) for c2, t2 in seen
                     if (old := olds.get(f"{c2}:{top}{p.src}")) is not None]
            if not p.hint and p.name and texts and not any(
                    re.search(r"(?<![\w$])" + re.escape(p.name) + r"(?![\w$])", old)
                    for *_x, old in texts):
                p.kind = "drop"     # the file never had the name: not what the doc meant
                continue
            hit = next(((c2, t2) for c2, t2, old in texts if held(p, old)), None)
            if hit:
                holding.add(id(p))
            # Held nowhere: the version the doc was committed with.
            base.setdefault(hit or (c, t), []).append(p)
    current: dict[str, list[str]] = {}

    def now_lines(src: str) -> list[str]:
        if src not in current:
            try:
                with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                    current[src] = fh.read().split("\n")
            except OSError:
                current[src] = []
        return current[src]
    for (sha, ts), group in base.items():
        diff = _git(root, "diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", sha,
                    "--", *sorted({p.src for p in group}))
        if diff is None:
            continue
        hunks = _hunks(diff.decode(errors="replace"))
        day = days.get(sha) or time.strftime("%Y-%m-%d", time.gmtime(ts))
        for p in group:
            h = hunks.get(top + p.src, [])
            if h is None:
                continue        # the file was not there (or not at that path) back then
            n, end = p.lines
            span = f"line {n}" if n == end else f"lines {n}-{end}"
            if p.kind == "quoted":
                at = [k + 1 for k, y in enumerate(now_lines(p.src))
                      if p.names[0] in re.sub(r"\s+", "", y)]
                if len(at) == 1 and abs(at[0] - n) > 2:
                    p.what = f"line {n} is now {at[0]} (the code quoted; moved since the " \
                             f"doc's version of {day})"
                    p.kind = "line-shifted"
                else:
                    p.kind = "drop"
                continue
            if id(p) not in holding:
                old = (olds.get(f"{sha}:{top}{p.src}") or "").split("\n")
                n2, end2 = _map_line(h, n), _map_line(h, p.lines[1])
                went = _block_at(old, now_lines(p.src), n, p.lines[1], n2 or n) or \
                    ((n2, end2) if n2 is not None and end2 is not None and
                     end2 - n2 == p.lines[1] - n else None)
                if p.kind == "weak":
                    p.kind = "drop"
                elif not p.tight:
                    # A name from further away: no claim about it; the doc's lines, though,
                    # stand elsewhere now.
                    if went and went[0] != n and "…" not in p.mention:
                        p.what = (f"line {n} is now {went[0]}" if n == p.lines[1] else
                                  f"lines {n}-{p.lines[1]} are now {went[0]}-{went[1]}") + \
                            f" (moved since the doc's version of {day})"
                        p.kind = "line-shifted"
                    else:
                        p.kind = "drop"
                else:           # off already in the doc's own version
                    p.what = _also(p.what, f"so in the doc's version too: {day}, {sha[:8]}" + (
                        "" if not went or went[0] == n else f"; that line is now {went[0]}"
                        if n == p.lines[1] else f"; those lines are now {went[0]}-{went[1]}"))
                    p.kind = "line-off"
                continue
            n2, end2 = _map_line(h, n), _map_line(h, end)
            edited = n2 is None or end2 is None or end2 - n2 != end - n or any(
                (b and a <= end and n < a + b) or (not b and n <= a < end) for a, b, _c, _d in h)
            if (n2, end2) == (n, end) and not edited:
                if p.kind == "weak":
                    p.kind = "drop"
                continue
            old = (olds.get(f"{sha}:{top}{p.src}") or "").split("\n")
            near = n2 if n2 is not None else (_edited_to(h, n, end) or (n,))[0]
            # The other lines listed with it, where they went.
            also = [r for a, b in p.more if (r := _block_at(old, now_lines(p.src), a, b,
                                                           _map_line(h, a) or a) or (
                (_map_line(h, a), _map_line(h, b)) if _map_line(h, a) is not None and
                _map_line(h, b) is not None else None))]
            # The block by its text (a repeated line: by its surroundings); git's alignment
            # when the text is not found.
            moved = _block_at(old, now_lines(p.src), n, end, near) or \
                (None if edited else (n2, end2))
            if moved and p.recheck(*moved, also):
                m2, e2 = moved
                p.what = (f"line {n} is now {m2}" if n == end else
                          f"lines {n}-{end} are now {m2}-{e2}") + \
                    f" (moved since the doc's version of {day})"
                p.kind = "line-shifted"
            elif edited:
                mapped = n2 is not None and end2 is not None
                now = (n2, end2) if mapped else _edited_to(h, n, end)
                if now and p.recheck(*now, also):
                    at = now if mapped else \
                        _naming(root, p.src, *now, p.names or (p.name,), current) or now
                    if p.span and p.span[0] <= at[1] and at[0] <= p.span[1]:
                        at = (max(at[0], p.span[0]), min(at[1], p.span[1]))   # its own lines
                    where = f"; now at {at[0]}" if at[0] == at[1] else \
                        f"; now in {at[0]}-{at[1]}"
                else:
                    where = f": {p.hint}" if p.hint else ""
                p.what = (f"{span} changed since the doc named {p.name or 'them'} ({day}, "
                          f"{sha[:8]})" + where)
                p.kind = "line-changed"
            elif p.kind == "weak" and not p.tight:
                p.kind = "drop"         # a name from further away: not enough to say it
            elif p.kind == "weak":
                p.what = f"{span} no longer {'holds' if n == end else 'hold'} {p.name}'s " \
                    "declaration" + \
                    (f" ({p.hint})" if p.hint else "")
                p.kind = "line-moved"
    for p, _c, _t in written:
        if p.kind == "line-moved" and not p.tight:
            p.kind = "drop"     # git settled nothing, and the name is from further away


def _at_declaration(lines: list[str], n: int, end: int, names: tuple) -> bool:
    """The lines n..end of an older version start at the declaration of one of names (its
    javadoc, annotations or first line), or lie in it."""
    return any(top - 2 <= n <= decl + 2 or (top <= n and end <= hi + 1)
               for x in names if x for top, decl, hi in _declared(lines, x))


def _naming(root: str, src: str, lo: int, hi: int, names: tuple, cache: dict):
    """The first and last of the lines lo..hi of the checkout's src that name one of names."""
    names = tuple(x for x in names if x)
    if not names:
        return None
    if src not in cache:
        try:
            with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                cache[src] = fh.read().split("\n")
        except OSError:
            cache[src] = []
    rx = re.compile(r"(?<![\w$])(?:" + "|".join(map(re.escape, names)) + r")(?![\w$])")
    hits = [k for k in range(lo, min(hi, len(cache[src])) + 1) if rx.search(cache[src][k - 1])]
    return (hits[0], hits[-1]) if hits else None


def _edited_to(hunks: list[tuple[int, int, int, int]], n: int, end: int):
    """The new lines of the hunks that changed lines n..end (when they still are few)."""
    news = [(c, c + d - 1) for a, b, c, d in hunks if b and d and a <= end and n < a + b]
    if not news:
        return None
    lo, hi = min(x for x, _y in news), max(y for _x, y in news)
    return (lo, hi) if hi - lo < 40 else None


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
    if "/".join(segs[:2]) not in m.roots or \
            (segs[uppers[0]] not in m.types and m.is_library(segs[uppers[0]])):
        return []           # a library's or another project's package
    if m.is_package("/".join(segs)):
        return []           # a package with an upper-case segment (com.x.IVA)
    last = max(i for i in uppers if all(x[:1].isupper() for x in segs[uppers[0]:i + 1]))
    fqn = ".".join(segs[:last + 1])
    simple = segs[uppers[0]]
    add(fqn, "no such type" + (m.type_hint(simple, fqn) or _closest(simple, list(m.types))),
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
                abbrev: dict | None = None, other_versions=(), prev_line: str = "") -> None:
    written, n0 = mo.group(1), mo.group(2)
    for k, v in (abbrev or {}).items():
        if written.startswith(k):
            written = v + written[len(k):]
            break
    if re.match(r"[ab]/", written) and not _find_path(written, by_suffix, sources):
        written = written[2:]           # a git diff's a/ and b/ prefixes
    if re.search(r"/tmp\b|\bscratch\b", line[max(0, mo.start() - 40):mo.start()]):
        return                          # a scratch copy, not the checkout's file
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
                                          if same else ""), "no-file", name="" if same else base)
        return
    if n0 is None:
        return
    n = int(n0)
    # Another version of the file (`git show <sha>:path`, `main <sha>:path`): not this one.
    before = line[max(0, mo.start() - 80):mo.start()].rstrip("`")
    if re.search(r"(?:[0-9a-f]{7,40}|[\w./~^-]*(?:main|master|HEAD|origin/[\w./-]+)[\w~^]*):$"
                 r"|\bgit show\b[^`]*$|(?:^|[\s(`])/(?:home|Users|tmp|mnt|var)/\S*$"
                 r"|(?<![\w-])(?:[0-9a-f]{7,40}|main|master" +
                 "".join("|" + re.escape(v) for v in other_versions) + r")\s+`?$", before):
        return      # another version: <ref>:path, git show, an absolute path elsewhere
    if re.match(r"`?\s*\((?:en |in |de |on |at )?(?:main|master|[0-9a-f]{7,40})\)", line[mo.end():]):
        return      # File.java:12 (main): another branch's
    clause = line[_segment(line, mo.start(), mo.end(), parens=False)[0]:mo.start()]
    if re.search(r"\b(?:en|in|on|de)\s+`?(?:main|master" + "".join(
            "|" + re.escape(v) for v in other_versions) + r")\b(?!\.)", clause, re.I):
        return      # "en main los sub-forms … (File.java:422 …)": that branch's code
    # What the mention's own part of the line names: the name glued to the path (`name
    # (File:N)`, `File:N — name`, `File:N (name)`), else the names of its parentheses, its
    # sentence, cell or item (a table cell holding only the path: the cells next to it),
    # and calls written next to the path. The lines N..M (±2) must still mention one of them.
    oa, ob = _segment(line, mo.start(), mo.end(), parens=False)
    a, b = _segment(line, mo.start(), mo.end())
    extra_spans: list[tuple[int, int]] = []
    if line.lstrip().startswith("|") and not any(a <= at < b for _x, at in named_here) and \
            not re.search(r"[a-z_$][\w$]*\s*\(|`[^`]+`", line[a:b].replace(mo.group(0), "")):
        extra_spans = [(x, x + len(re.split(r";\s| · ", line[x:y])[0]))
                       for x, y in _neighbour_cells(line, mo.start())
                       if re.match(r"\s*(?:`|[A-Za-z_$][\w$.#]*\()", line[x:y])]  # code, not a remark
    glued = _glued_names(line[oa:ob], mo.group(0))
    glued = {x[1:] if x[:1] == "?" else x for x in glued
             if x[:1] != "?" or any(m.member_syms.get((t, x[1:])) for f in hits
                                    for t in m.types_in.get(f, []))}
    if re.search(r"(?:→|->)\s*$", line[max(0, oa - 4):oa]):
        # "a() → X (File:N)": the clause starts after the arrow; X is where the call goes
        glued = {x for x in glued if not re.match(r"`?(?:[\w$]+[.#])*" + re.escape(x) + r"\b",
                                                  line[oa:ob].lstrip())}
    if not any(x in m.by_member or x in m.types for x in glued):
        glued = set()       # glued words that name nothing in the code (`onClose`, a key)
    end_n = int(mo.group(3)) if mo.group(3) else n
    in_part = [(a, b)] + extra_spans

    def inside(pos: int) -> bool:
        return any(x <= pos < y for x, y in in_part)
    # A name in the segment belongs to the path mention, or the line number alone after one
    # (`:30`, about another line of that file), that comes last before it, else to the first
    # one after it; a name glued to another of them is that one's.
    paths = [p for p in _PATH.finditer(line) if oa <= p.start() < ob]
    bare = [b_ for b_ in _BARE.finditer(line, oa, ob) if any(p.end() <= b_.start()
                                                              for p in paths)] + \
        [x for x in re.finditer(r"\b(?:l[ií]neas?|lines?)\s+\d+", line) if oa <= x.start() < ob]
    anchors = sorted([p.start() for p in paths] + [b_.start() for b_ in bare])

    def ours(pos: int) -> bool:
        before = [at for at in anchors if at < pos]
        after = [at for at in anchors if at > pos]
        return (before[-1] if before else after[0] if after else mo.start()) == mo.start()
    others = {x for p in [*paths, *bare] if p.start() != mo.start()
              for x in (y.lstrip("?") for y in _glued_names(line[oa:ob], p.group(0)))} - glued
    for x0, y0 in extra_spans:          # `getX()` vacío (`:106`) in the next cell
        others |= {x for b_ in _BARE.finditer(line, x0, y0)
                   for x in (y.lstrip("?") for y in _glued_names(line[x0:y0], b_.group(0)))} - glued
    in_segment = sorted({S.name_of(x) for x, at in named_here if inside(at) and ours(at)} |
                        {g.group(1) for p, q in code if inside(p) for g in
                         re.finditer(r"(?<![\w$.])([a-z_$][\w$]*)\s*\(", line[p:q])
                         if ours(p + g.start())})
    in_segment = [x for x in in_segment if x not in others]
    names: list[str] = sorted(glued) or in_segment
    # When the glued names cannot be checked in the file, those of the mention's clause.
    in_clause = {S.name_of(x) for x, at in named_here if oa <= at < ob and ours(at)} | \
        {g.group(1) for p, q in code if oa <= p < ob for g in
         re.finditer(r"(?<![\w$.])([a-z_$][\w$]*)\s*\(", line[p:q]) if ours(p + g.start())}
    fallback = sorted(x for x in in_clause if x not in names and x not in others)
    names += sorted(c for c in _glued_calls(line, mo, code, oa, ob)
                    if c not in names and c not in others)
    # Names written right at the mention: only then may git say the doc was wrong.
    flow_cell = any(re.search(r"→|->", line[x:y]) for x, y in extra_spans)  # a call chain
    tight = bool(glued) or bool(extra_spans and names and not flow_cell) or bool(
        {c[4:] if c.startswith("new ") else c for c in
         _glued_calls(line, mo, code, oa, ob, tight=True)} & set(names))
    if not names and re.fullmatch(r"\s*[(\[]?\s*`?", line[:mo.start()]):
        # `Type.query(…)`\n(`Type.java:719`): the paragraph goes on from the line before.
        last = re.search(r"`[^`]*?([a-z_$][\w$]*)\s*\([^`]*`\s*$", prev_line)
        if last:
            names = [last.group(1)]
    names = [x for x in names if not x.startswith("new ")] + \
        [x[4:] for x in names if x.startswith("new ")]
    seg = line[oa:ob]

    def as_code(x: str) -> bool:
        """The doc writes x as code: x( with no space, .x or #x (a word alone in backticks
        may be a branch or a role: `main`, `helper`)."""
        q = re.escape(x)
        return len(x) > 2 and re.search(r"(?<![\w$])" + q + r"\(|\." + q + r"(?![\w$])|#" +
                                        q + r"(?![\w$])", seg) is not None

    spans_text = " ".join(line[p:q] for p, q in code if oa <= p < ob)

    def receiver_only(x: str) -> bool:
        """x appears only as a receiver or a local of the doc's code spans (pm.foo(),
        q = …); a member some type declares never is one."""
        q = re.escape(x)
        return x not in m.by_member and \
            re.search(r"(?<![\w$.])" + q + r"\s*(?:\.|=[^=])", spans_text) is not None and \
            re.search(r"(?<![\w$])" + q + r"\(|[.#]" + q + r"(?![\w$])", spans_text) is None
    names = [x for x in names if not receiver_only(x)]
    # More lines of the same file listed after it (:40,42-43, :12/20, :12 + 80-84, :30, :35):
    # the name may be at any of them.
    more: list[tuple[int, int]] = []
    k = mo.end()
    while (nx := _MORE_LINES.match(line, k)):
        g = [x for x in nx.groups() if x is not None]
        more.append((int(g[0]), int(g[1]) if len(g) > 1 else int(g[0])))
        k = nx.end()

    def verdict(n: int, end_n: int, srcs: list[str], also: list[tuple[int, int]] = (),
                strict: bool = False):
        """None when the lines n..end_n of one of srcs still mention a name the doc gives
        (or no name can be checked there); else (name, where it is now, source). strict:
        they must start at the declaration of a name the file declares, or lie in it."""
        declaring = [f for f in srcs if any(m.member_syms.get((t, x)) for t in
                                            m.types_in.get(f, []) for x in names)]
        failed = None
        for src in declaring or srcs:    # same-named files: the one with the member named
            for lo, hi in [(n, end_n), *also]:
                bad = _verdict_in(m, root, src, lo, hi, names, as_code, text_cache, strict)
                if bad == "unchecked" and fallback:
                    bad = _verdict_in(m, root, src, lo, hi, fallback, as_code, text_cache,
                                      strict)
                if bad is None or bad == "unchecked":
                    return None
                failed = failed or bad + (src,)
        return failed
    fits = [s for s in hits if n <= count_lines(s)]
    if not fits:
        src = hits[0]
        add(mo.group(0), f"line {n} is past the end ("
            + ", ".join(f"{s}: {count_lines(s)} lines" for s in hits[:3]) + ")", "past-end",
            src=src, lines=(n, end_n), names=tuple(names), tight=tight,
            recheck=lambda n2, m2, also=(): n2 <= count_lines(src) and
            verdict(n2, m2, [src], also) is None)
        return
    bad = verdict(n, end_n, fits, more)
    if bad is None:
        # A statement the doc quotes for one line (`obj.setOwner(user)`), which the file
        # has once, and not there: the line moved (another call of that name is there now).
        cell_next = next((x for x, y in _neighbour_cells(line, mo.start()) if x > mo.start()),
                         -1) if line.lstrip().startswith("|") else -1
        quotes = [re.sub(r"\s+|;$", "", line[p + 1:q - 1]) for p, q in code
                  if not p <= mo.start() < q and (
                      re.fullmatch(r"`?\s*(?:—|–|-|:|\(|=)?\s*", line[mo.end():p]) or
                      (cell_next >= 0 and re.fullmatch(r"\s*", line[cell_next:p])))][:1]
        quotes = [x for x in quotes if len(x) >= 12 and re.search(r"[\w$)]\.[\w$]+\(|=", x)
                  and "…" not in x and "..." not in x and not _PATH.search(x)]
        for src in fits[:1] if quotes and n == end_n and not more else ():
            flat = [re.sub(r"\s+", "", x) for x in text_cache.get(src) or []]
            if any(quotes[0] in y for y in flat[max(0, n - 3):n + 2]):
                break           # it is there
            # git decides: the doc's version had it at that line, and it stands once elsewhere
            add(mo.group(0), "", "quoted", src=src, lines=(n, n), names=tuple(quotes),
                recheck=lambda n2, m2, also=(): True)
        # It holds through a call or a comment while the file declares the name elsewhere:
        # git may show the doc pointed at the declaration (then the lines moved).
        weak = verdict(n, end_n, fits, more, strict=True)
        if weak is not None:
            x, where, src = weak
            add(mo.group(0), "", "weak", src=src, lines=(n, end_n), name=x,
                names=tuple(dict.fromkeys(names + fallback)), hint="; ".join(where),
                tight=tight, more=tuple(more),
                span=_span_of(m, root, src, x, text_cache),
                recheck=lambda n2, m2, also=(): verdict(n2, m2, [src], also, strict=True) is None)
        return
    x, where, src = bad
    extra = {"src": src, "lines": (n, end_n), "name": x, "hint": "; ".join(where),
             "names": tuple(dict.fromkeys(names + fallback)), "tight": tight,
             "more": tuple(more),
             "recheck": lambda n2, m2, also=(): verdict(n2, m2, [src], also) is None,
             "span": _span_of(m, root, src, x, text_cache)}
    if where:
        add(mo.group(0), f"line {n} no longer mentions {x} ({'; '.join(where)})",
            "line-moved", **extra)
    else:
        add(mo.group(0), f"line {n} no longer calls {x} (no code in this file calls it now)",
            "line-moved", **extra)


def _span_of(m: Model, root: str, src: str, x: str, text_cache: dict) -> tuple:
    """The lines of the members x of src, overloads together (with their javadoc and
    annotations), or ()."""
    spans = []
    for t in m.types_in.get(src, []):
        for y in m.member_syms.get((t, x), []):
            w = m.where.get(y)
            if w and w[0] == src:
                lo = min((w[2] + 1) if w[2] is not None else w[1], w[1])
                hi = (w[3] + 1) if w[3] is not None else w[1]
                spans.append((_with_header(root, src, lo, text_cache), hi))
    return (min(a for a, _b in spans), max(b for _a, b in spans)) if spans else ()


def _verdict_in(m: Model, root: str, src: str, n: int, end_n: int, names: list[str], as_code,
                text_cache: dict, strict: bool = False):
    """None when the lines n..end_n (±2) of src mention one of names, or fall in its lines;
    "unchecked" when no name can be checked in src; else (the first name, where it is now)."""
    members = {x: [y for t in m.types_in.get(src, [])
                   for y in m.member_syms.get((t, x), []) if y not in m.generated]
               for x in names}
    # toString, close…: a line may call them without naming them, unless this file
    # declares them (the doc is about the declaration).
    known = [x for x in names if (members[x] or x in m.by_member or x in m.types) and
             (x not in _IMPLICIT or members[x])]
    if not known:
        return "unchecked"
    lines = text_cache.get(src) or []
    if not lines:
        _with_header(root, src, 1, text_cache)
        lines = text_cache.get(src) or []
    span = lines[max(0, n - 3):min(len(lines), end_n + 2)]
    code_part = "\n".join(c for c in span if not re.match(r"\s*(//|\*|/\*)", c))
    comments = "\n".join(c for c in span if re.match(r"\s*(//|\*|/\*)", c))

    def mentions(x: str) -> bool:
        q = re.escape(x)
        if re.search(r"(?<![\w$])" + q + r"(?![\w$])", code_part) or \
                re.search(r"(?<![\w$])" + q + r"\(|[.#]" + q + r"(?![\w$])", comments):
            return True         # in a comment, only written as code
        fields = [y for y in members.get(x, []) if S.kind_of(y) == "term"]
        if fields and x.lower() in code_part.lower():
            return True             # the field x, written as getX/setX
        # getX, setX: the lines declare the field x
        prop = _property(x)
        return bool(prop) and any(
            S.kind_of(y) == "term" and (w := m.where.get(y)) and w[0] == src and
            n - 2 <= w[1] <= end_n + 2
            for t in m.types_in.get(src, []) for y in m.member_syms.get((t, prop), []))

    if ("code", src) not in text_cache:       # the code lines, without strings
        text_cache[("code", src)] = "\n".join(_no_strings(c) for c in lines
                                              if not re.match(r"\s*(//|\*|/\*)", c))
    code_text = text_cache[("code", src)]

    def anywhere(x: str) -> bool:
        """The file declares x or calls it somewhere (code, not comments)."""
        return bool(members.get(x)) or (x in code_text and re.search(
            r"(?<![\w$])" + re.escape(x) + r"\s*(?:\(|=[^=])", code_text) is not None)
    # A name this file neither declares nor mentions counts only when the doc writes it as
    # code: a prose word ("en main", "de sólo lectura") never does.
    known = [x for x in known if anywhere(x) or as_code(x)]
    if not known:
        return "unchecked"

    def contains(x: str) -> bool:
        """N..M falls in X's own lines (with its javadoc and annotations); a type's are its
        whole body."""
        for y in members.get(x, []) + [t for t in m.types_in.get(src, []) if S.name_of(t) == x]:
            w = m.where.get(y)
            if not w or w[0] != src:
                continue
            lo = min((w[2] + 1) if w[2] is not None else w[1], w[1])
            hi = (w[3] + 1) if w[3] is not None else w[1]
            if n <= hi + 1 and end_n >= _with_header(root, src, lo, text_cache) - 2:
                return True
        return False
    def at_declaration(x: str) -> bool:
        """N..M start at X's declaration (its javadoc, annotations or first line), or lie
        in it."""
        for y in members.get(x, []):
            w = m.where.get(y)
            if not w or w[0] != src:
                continue
            lo = min((w[2] + 1) if w[2] is not None else w[1], w[1])
            hi = (w[3] + 1) if w[3] is not None else w[1]
            head = _with_header(root, src, lo, text_cache)
            if head - 2 <= n <= w[1] + 2 or (head <= n and end_n <= hi + 1):
                return True
        return False
    if strict:
        types_here = {S.name_of(t) for t in m.types_in.get(src, [])}
        known = [x for x in known if members.get(x) and x not in types_here]
        if not known:
            return "unchecked"
        if any(at_declaration(x) for x in known):
            return None
    elif any(mentions(x) or contains(x) for x in known):
        return None
    x = known[0]
    where = []
    for y in members.get(x, [])[:1]:
        w = m.where.get(y)
        if w:
            lo = min((w[2] + 1) if w[2] is not None else w[1], w[1])
            hi = (w[3] + 1) if w[3] is not None else w[1]
            where.append(f"{x} is at {lo}–{hi}" if hi > lo else f"{x} is at {lo}")
    call = re.compile(r"(?<![\w$])" + re.escape(x) + r"\s*\(")
    calls = [k + 1 for k, c in enumerate(lines)
             if x in c and call.search(_no_strings(c))
             and not re.match(r"\s*(//|\*|/\*)", c)
             and not (members.get(x) and m.where.get(members[x][0], ("", 0))[1] == k + 1)]
    if calls:
        where.append("called at " + ", ".join(map(str, calls[:4]))
                     + (" …" if len(calls) > 4 else ""))
    elif not where:         # not a call here: a key, a name in a string or a comment
        word = re.compile(r"(?<![\w$])" + re.escape(x) + r"(?![\w$])")
        named = [k + 1 for k, c in enumerate(lines) if x in c and word.search(c)]
        if named:
            where.append("named at " + ", ".join(map(str, named[:4]))
                         + (" …" if len(named) > 4 else ""))
    return x, where


def _neighbour_cells(line: str, pos: int) -> list[tuple[int, int]]:
    """The spans of the table cells right before and after the one at pos."""
    bars = [k for k, ch in enumerate(line) if ch == "|"]
    for k in range(len(bars) - 1):
        if bars[k] < pos < bars[k + 1]:
            out = []
            if k > 0:
                out.append((bars[k - 1] + 1, bars[k]))
            if k + 2 < len(bars):
                out.append((bars[k + 1] + 1, bars[k + 2]))
            return out
    return []


# Between items: " · ", "; ", a table cell, a sentence end, a comma before the next code span
# (a list of evidences: `A.java:1 — …`, `B.java:2 — …`).
_SEPARATORS = re.compile(r" · |;\s|\|(?!\|)|(?<=[.!?])\s+(?=[A-Z¿¡`(])|,\s+(?=`)|\s→\s|"
                         r",?\s+(?:y|and|e)\s+(?=`?:\d)|,\s*(?=`?:\d)")


def _no_strings(text: str) -> str:
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)


def _glued_calls(line: str, mo, code: list, a: int, b: int, tight: bool = False) -> set[str]:
    """Calls written right next to a path: in its own code span, after `File:N —` or
    `File:N (`, before ` (File:N)`, or in the next table cell when that is code; not
    across ";", " · ", nor into the next mention's parentheses. `new X(…)` is "new X".
    tight: only those right at it (its span, its cell, the first words after it)."""
    pieces = []
    own = next(((p, q) for p, q in code if p <= mo.start() < q), None)
    if own:
        # Up to the next line number or path in the same span (`A.java:12 (x), :30 (y)`).
        head = line[own[0]:mo.start()]
        cuts = [c.end() for c in _PATH.finditer(head)] + [c.end() for c in _BARE.finditer(head)]
        head = head[max(cuts, default=0):]
        tail = re.split(r"(?:,|\s(?:y|and|e)\b|\s*\+)\s*`?:\d|\s·\s|;", line[mo.end():own[1]])[0]
        nxt = _PATH.search(tail)
        tail = tail[:nxt.start()] if nxt else tail
        pieces.append(head[-40:] + " " + tail[:40] if tight else head + tail)
    after = re.split(r";|\s·\s|\||\s→\s", line[mo.end():b])[0]
    after = re.split(r"(?:,|\s(?:y|and|e))\s*`?:\d", after)[0]
    nxt = [x.start() for x in (*_PATH.finditer(after), *_BARE.finditer(after)) if x.start() > 0]
    after = after[:min(nxt)] if nxt else after      # up to the next path or line number
    if re.match(r"`?\s*(?:—|–|-|:|\()", after):
        pieces.append(after[:40] if tight else after[:160])
    before = line[a:mo.start()]
    bm = re.search(r"(`[^`]+`)\s*\(\s*`?$", before)
    if bm:
        pieces.append(bm.group(1))
    if line.lstrip().startswith("|"):
        cells = line.split("|")
        pos = 0
        for k, cell in enumerate(cells):
            if pos <= mo.start() < pos + len(cell) + 1:
                if cell.replace("`", "").strip() == mo.group(0).replace("`", "").strip() and \
                        k + 1 < len(cells) and re.match(r"\s*`", cells[k + 1]):
                    pieces += re.findall(r"`([^`]+)`", re.split(r";\s| · ", cells[k + 1])[0])
                break
            pos += len(cell) + 1
    out = set()
    for piece in pieces:
        piece = _no_strings(piece)
        for g in re.finditer(r"\bnew\s+([A-Z][\w$]*)\s*[(<]|(?<![\w$])([a-z_$][\w$]*)\(",
                             piece):
            out.add(f"new {g.group(1)}" if g.group(1) else g.group(2))
    return out - {"if", "for", "while", "switch", "catch", "return", "synchronized", "new"}


def _segment(line: str, start: int, end: int, parens: bool = True) -> tuple[int, int]:
    """The part of a doc line a mention belongs to: between separators (" · ", "; ", a table
    cell's "|", a sentence end), and inside its parentheses (with parens)."""
    a, b = 0, len(line)
    for sep in _SEPARATORS.finditer(line):
        if sep.end() <= start:
            a = sep.end()
        elif sep.start() >= end:
            b = sep.start()
            break
    if not parens:
        return a, b
    # Inside parentheses (… (File:N → `name`) …): what they hold.
    depth, open_at = 0, -1
    for k in range(start - 1, a - 1, -1):
        if line[k] == ")":
            depth += 1
        elif line[k] == "(":
            if depth == 0:
                open_at = k
                break
            depth -= 1
    if open_at >= 0 and not re.match(r"[\w$]", line[open_at - 1:open_at] or " "):
        depth = 0
        for k in range(end, b):
            if line[k] == "(":
                depth += 1
            elif line[k] == ")":
                if depth == 0:
                    return open_at + 1, k
                depth -= 1
    return a, b


def _glued_names(segment: str, path_mention: str) -> set[str]:
    """Member names written right next to a path: `name (File:N)`, `File:N — name`,
    `File:N (name)`."""
    p = re.escape(path_mention)
    out = set()
    for k, rx in enumerate((r"([`.#]?)([A-Za-z_$][\w$]*)(\s*\([^()]*\))?(`?)\s*\(\s*`?" + p,
                            p + r"`?\s*(?:—|–|:|-)\s*(`?)(?:[\w$]+[#.])?([A-Za-z_$][\w$]*)"
                                r"(\s*\()?",
                            p + r"`?\s*\(\s*`?(?:[\w$]+[#.])?([A-Za-z_$][\w$]*)",
                            # `getX()` vacío (`:106`): one word between
                            r"`(?:[\w$]+[#.])?([a-z_$][\w$]*)(?:\([^()]*\))?`\s+[^\s`()]+\s*\(\s*`?"
                            + p)):
        for m_ in re.finditer(rx, segment):
            name = m_.group(2) if k < 2 else m_.group(1)
            if k in (0, 3):
                head = segment[:m_.start(2 if k == 0 else 1)]
                if re.search(r"(?:→|->)\s*`?(?:[\w$]+[.#])*$|(?<![\w$])(?:sin|without|not?)"
                             r"\s+(?:\w+\s+)?`?(?:[\w$]+[.#])*$", head, re.I):
                    continue        # "→ X (File:N)", "sin filtrar X (…)": not X's line
                # `a()` + `b()` + `c()` (File:N): the whole list
                while (lm := re.search(r"`([^`]+)`\s*(?:\+|,|\by\b|\band\b)\s*`?(?:[\w$]+[.#])*$",
                                       head)):
                    out.update(re.findall(r"([a-z_$][\w$]*)\s*\(", lm.group(1)))
                    head = head[:lm.start()]
            tail = segment[m_.start(1):m_.end(1) + 2] if k == 2 else ""
            if k == 2 and not (re.search(r"[a-z][A-Z]|[_$]", name) or
                               segment[max(0, m_.start(1) - 1):m_.start(1)] in ("`", ".", "#") or
                               re.match(r"[\w$]+\s*[(=]", tail)):
                name = "?" + name   # "(aforo)": a word; code only if the file declares it
            # After a dash, prose follows unless the word looks like code; before the path's
            # parenthesis, likewise (`foo` (File:N), foo() (File:N), not "por eval (File:N)").
            if k == 1 and not (m_.group(1) or m_.group(3) or re.search(r"[a-z][A-Z]", name)):
                continue
            if k == 0 and not (m_.group(1) or m_.group(3) or m_.group(4) or
                               re.search(r"[a-z][A-Z]|[_$]", name)):
                continue
            if name[:1].islower() or name[:1] == "?" or name == "constructor" or \
                    (k == 0 and m_.group(1) == "`" and m_.group(4)):    # `Type` (File:N)
                out.add(name)
    return out - {"constructor", "el", "la", "the", "line", "linea", "líneas", "lines", "null",
                  "true", "false", "this", "super", "new", "return", "if", "for", "while"}


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
        own = [x for c in cands for x in m.member_syms.get((c, name), [])]
        return own or cands[:1]     # UUnidad.toString(): its own, when it declares one
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
        hint = _closest(name, m.names_in(t)) or m.elsewhere(name, t)
        add(mention, f"{S.short(t)} has no member {name}" + hint, "no-member",
            src=m.where.get(t, ("",))[0], name=name, hint=hint)
        return [t]
    syms = [x for c in cands for x in m.member_syms.get((c, name), [])]
    prims = {"boolean", "int", "long", "short", "byte", "char", "float", "double"}
    typed = params is not None and bool(params.strip()) and all(
        (lambda w: bool(w) and len(w) <= 2 and (w[0].rstrip("[].") in prims or (
            re.fullmatch(r"[A-Z][\w$]*(?:<[^()]*>)?(?:\[\]|\.\.\.)?", w[0]) and
            not re.fullmatch(r"[A-Z0-9_]+", w[0]) and
            (w[0].split("<")[0].rstrip("[].") in m.types or m.is_library(
                w[0].split("<")[0].rstrip("[].")))))
        )(x.split()) for x in params.split(","))
    if params is not None and params.strip() and (not call or typed):
        want = _params(params)
        from .refactor import _param_types
        have = [_param_types(s) for c in cands for s in (m.member(c, name)[0] or [])
                if "(" in s]
        # A shorter list than every overload is shorthand (put(Date): the value's type).
        if have and want not in have and not any(h[:len(want)] == want for h in have) and \
                not all(len(want) < len(h) for h in have):
            shown = "; ".join(dict.fromkeys("(" + ", ".join(h) + ")" for h in have[:6]))
            add(mention, f"no {name}({', '.join(want)}) in {S.short(t)}: its overloads take "
                f"{shown}", "params")
        # The overload written is the one meant (save(boolean)).
        exact = [x for x in syms if _param_types(_sig_of(m, x)) == want]
        syms = exact or syms
    return syms or [t]


def _sig_of(m: Model, sym: str) -> str:
    parent, name = S.parent(sym), S.name_of(sym)
    sigs = m.members.get(parent, {}).get(name, [])
    syms = m.member_syms.get((parent, name), [])
    return sigs[syms.index(sym)] if sym in syms and syms.index(sym) < len(sigs) else ""


def _is_library_like(written: str) -> bool:
    return written.split(".")[0] in ("System", "Math", "String", "Integer", "Long", "List",
                                     "Map", "Set", "Arrays", "Collections", "Objects",
                                     "Optional", "Stream", "Collectors", "Files", "Paths",
                                     "Path", "LocalDate", "LocalDateTime", "Instant",
                                     "Duration", "BigDecimal", "Thread", "Class")


def _cut(text: str, width: int = 80) -> str:
    """A long mention cut in the middle at a folder, so that its end (the file name and
    line number) stays whole."""
    if len(text) <= width:
        return text
    head, tail = text[:width // 2], text[-(width // 2):]
    if "/" in tail:
        tail = tail[tail.index("/"):]
    if "/" in head:
        head = head[:head.rindex("/")]
    return head + "/…" + tail if "/" in text else text[:width // 2 - 1] + "…" + text[-(width // 2):]


def _rank(p: Problem) -> int:
    """Of several problems for one mention, the one to show: a new line number, then a hint
    of where the name is."""
    return 3 if p.kind == "line-shifted" else 2 if " is at " in p.what else \
        1 if "called at" in p.what else 0


def render(problems: list[Problem], checked: int, nfiles: int, offset: int, limit: int,
           fmt: str = "text", archived: int = 0) -> str:
    if not nfiles:
        return "check_refs: no file matches (files: globs relative to the checkout)"
    note = (f"\n({archived} files under archive folders ({', '.join(sorted(ARCHIVE_DIRS))}) "
            "left out: name them in files to check them)") if archived else ""
    # The same mention with the same problem in one file: once, with its lines.
    # One row per mention on a line: the message with a hint (is at / called at) first.
    best: dict[tuple, Problem] = {}
    for p in problems:
        k = (p.path, p.line, p.mention)
        old = best.get(k)
        if old is None or _rank(p) > _rank(old):
            best[k] = p
    problems = [p for p in problems if best[(p.path, p.line, p.mention)] is p]
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

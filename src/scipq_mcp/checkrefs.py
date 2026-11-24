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

import bisect
import difflib
import functools
import os
import re
import shutil
import sqlite3
import subprocess
import threading
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
_PATH = re.compile(r"(?<![\w$./~-])((?:~?/)?(?:(?:[\w$.-]+|\.\.\.|…)/)*[\w$-]+\.java)"
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
# Names too common to tell one file or one line from another (add, get, setValue…).
_GENERIC = {"get", "set", "add", "put", "run", "call", "apply", "accept", "close", "size",
            "remove", "contains", "equals", "init", "build", "create", "execute", "update",
            "save", "load", "value", "getValue", "setValue", "toString", "isEmpty", "clear",
            "commit", "begin", "refresh", "test", "list", "find", "next", "open", "start",
            "stop", "show", "handle", "process", "of", "map", "filter", "forEach", "stream",
            "getId", "setId", "getName", "setName", "length", "append", "format", "parse"}


def _specific(names) -> list[str]:
    return [x for x in names if x and len(x) > 3 and x not in _GENERIC]


# A type named in prose, or alone in backticks, before a `:N`: Engine.run :587, IPOForm (:226)
# Words before a type named as the object of the sentence, not as the place of the line:
# "castea el itemId a T (:592)", "el ClickListener de btn (:81)" ("de T" alone is T's).
_OBJECT_BEFORE = re.compile(r"(?i)(?<![\w$])(?:a|al|el|la|los|las|un|una|en|con|por|para|"
                            r"sobre|the|an|to|into|from|with|by|for|on)\s+`?$")
# Where a sentence or a table cell ends: a `:N` past it is not plainly the path's.
_SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z¿¡`(*-])|\s·\s|\|")
_TYPE_WORD = re.compile(r"(?<![\w$.#/])([A-Z][\w$]*[a-z][\w$]*)(?:[.#]([a-z_$][\w$]*))?"
                        r"(?![\w$])(?!\.java)")
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
# What check_refs reads when neither the call nor [check_refs] files names files: what agents
# read to work, by convention (file names compared without case).
AGENT_FILES = ["**/CLAUDE.md", "**/AGENTS.md", ".claude/**/*.md", ".github/**/*.md",
               "**/README*.md"]
AGENT_FILES_SHORT = "CLAUDE.md, AGENTS.md, .claude/**, .github/**, README*.md"
# Folders of someone else's code kept in the repository: their READMEs are not the project's.
_VENDOR_DIRS = {"node_modules", "bower_components", "vendor", "vendors", "third_party",
                "third-party", "thirdparty", "external", "externals"}
# Output folders (graph-out, generated, dist): what is there was written by a tool.
_GENERATED = re.compile(r"(?:^|[-_.])(?:out|output|generated|gen|dist)$", re.I)


def _generated(f: str) -> bool:
    return any(_GENERATED.search(d) for d in f.split("/")[:-1])


_SOURCE_TREE = {"src", "public", "static", "webapp", "resources", "assets", "web"}
_LICENSE = re.compile(r"(?i)(?:license|licence|copying)(?:\.\w+)?$")
# A file or folder named by date or by ticket: a record (an audit, a fix's write-up), not a
# guide.
_DATED = re.compile(r"(?<!\d)(?:19|20)\d\d[-_.]?(?:0[1-9]|1[0-2])(?:[-_.]?[0-3]\d)?(?!\d)|"
                    r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}-\d{2,}(?!\d)")
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
    cont: bool = False          # a `:N` alone after a path: only when git confirms the file
    alts: tuple = ()            # other files it may be (same-named ones): git picks
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


@functools.lru_cache(maxsize=1024)
def _glob_rx(gl: str, nocase: bool = False) -> re.Pattern:
    """A glob relative to the checkout as a regex: * and ? stay within a folder, ** crosses
    folders (dir/**/x also matches dir/x), [abc] is a class."""
    gl = gl[2:] if gl.startswith("./") else gl
    out, i = [], 0
    while i < len(gl):
        if gl.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif gl.startswith("**", i):
            out.append(".*")
            i += 2
        elif gl[i] == "*":
            out.append("[^/]*")
            i += 1
        elif gl[i] == "?":
            out.append("[^/]")
            i += 1
        elif gl[i] == "[" and "]" in gl[i + 2:]:
            j = gl.index("]", i + 2)
            body = gl[i + 1:j]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body)
                       .replace("\\", "\\\\") + "]")
            i = j + 1
        else:
            out.append(re.escape(gl[i]))
            i += 1
    return re.compile("".join(out), re.I if nocase else 0)


def _match(p: str, gl: str, nocase: bool = False, dirs: bool = False) -> bool:
    """p matches the glob; with dirs, also when one of its folders does (a folder, or
    docs/*, means everything under it)."""
    rx = _glob_rx(gl.rstrip("/") if dirs else gl, nocase)
    if rx.fullmatch(p):
        return True
    parts = p.split("/")
    return dirs and any(rx.fullmatch("/".join(parts[:k])) for k in range(1, len(parts)))


def _files(root: str, globs: list[str], exclude: list[str],
           listed: list[str] | None = None, nocase: bool = False) -> list[str]:
    listed = _listed(root) if listed is None else listed
    globs = _expand_globs(root, globs)
    return sorted(p for p in listed if any(_match(p, g_, nocase) for g_ in globs)
                  and not any(_match(p, x, dirs=True) for x in exclude))


def _is_md(p: str) -> bool:
    return p.lower().endswith(".md")


def _vendored(f: str, license_dirs: set[str]) -> bool:
    """A README of someone else's code: under vendor/, node_modules/…, or a folder with a
    LICENSE of its own inside a source tree (a library copied into src/…/public/)."""
    parts = f.split("/")[:-1]
    return bool(_VENDOR_DIRS & {d.lower() for d in parts}) or any(
        "/".join(parts[:k]) in license_dirs and _SOURCE_TREE & {d.lower() for d in parts[:k - 1]}
        for k in range(2, len(parts) + 1))


@dataclass
class Scope:
    """What check_refs read and why: the files named in the call, [check_refs] files of a
    configuration file, or the default (what agents read)."""
    how: str = "default"                # "given", "config" or "default"
    origin: str = ""                    # the configuration file, for "config"
    globs: tuple = ()
    exclude: tuple = ()
    read: int = 0                       # files checked
    total: int = 0                      # files in scope (more than read, with since)
    archived: int = 0                   # .md files (or in scope) under archive folders
    others: int = 0                     # other .md files of the checkout, not checked
    vendored: int = 0                   # READMEs of someone else's code left out
    excluded: int = 0                   # .md files exclude leaves out
    since: str = ""                     # the ref given, and the commit it meant
    base: str = ""
    changed_docs: int = 0               # docs in scope changed since then
    changed_code: int = 0               # .java files changed since then
    citing: int = 0                     # docs in scope that mention changed code
    citing_outside: tuple = ()          # .md files out of scope that mention changed code
    citing_docs: tuple = ()             # docs in scope read for those mentions
    older: int = 0                      # problems in changed docs on lines not edited since
    error: str = ""


def _changed_since(root: str, ref: str) -> tuple[str, list[str]] | str:
    """(the merge base of ref and HEAD, the files changed from it to the working tree,
    deleted and untracked ones included), or why not."""
    if _git(root, "rev-parse", "--git-dir") is None:
        return "since needs a git checkout"
    if ref.startswith("-") or _git(root, "rev-parse", "--verify", "--quiet",
                                   ref + "^{commit}") is None:
        return f"since: {ref!r} is not a commit here"
    base = (_git(root, "merge-base", ref, "HEAD") or b"").decode().strip()
    if not base:
        return f"since: {ref!r} has no common history with HEAD"
    out = _git(root, "diff", "--name-only", "--no-renames", "--relative", "-z", base, "--")
    new = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    if out is None:
        return f"since: git diff from {ref!r} failed"
    names = [p for p in (out + b"\0" + (new or b"")).decode("utf-8", "replace").split("\0") if p]
    return base, sorted(set(names))


class _Changed:
    """The .java files changed since a commit, as a doc names them: their types and file
    names written as code; a name several files declare (Configuracion.java) only with its
    folder or package (banval/Configuracion, banval.Configuracion)."""

    def __init__(self, m, java: set[str], listed: list[str]):
        self.java = java
        same = {}
        for f in listed:
            if f.endswith(".java"):
                same[os.path.basename(f)] = same.get(os.path.basename(f), 0) + 1
        plain: set[str] = set()
        qualified: set[tuple[str, str]] = set()

        def add(name: str, src: str) -> None:
            files = {m.where[x][0] for x in m.types.get(name, []) if x in m.where}
            if same.get(name + ".java", 0) > 1 or len(files) > 1:
                qualified.add((os.path.basename(os.path.dirname(src)), name))
            else:
                plain.add(name)
        for f in java:
            add(os.path.basename(f)[:-len(".java")], f)
        for sym, v in m.where.items():
            if v[0] in java and sym.endswith("#"):
                add(S.parse(sym)[-1].name, v[0])
        alt = "|".join(map(re.escape, sorted(plain, key=len, reverse=True)))
        self.word = re.compile(r"(?<![\w$])(?:" + alt + r")(?![\w$])") if plain else None
        # written as code: in backticks, or File.java, Type#m, Type.m(, Type::m
        self.code = re.compile(r"(?<![\w$])(?:" + alt +
                               r")(?:\.java\b|#|::|\.[a-z_$][\w$]*\()") if plain else None
        self.qual = re.compile(r"(?<![\w$])(?:" + "|".join(
            re.escape(d) + r"[/.]" + re.escape(n) for d, n in sorted(qualified)) +
            r")(?![\w$])") if qualified else None

    def cites(self, text: str) -> bool:
        if self.qual is not None and self.qual.search(text):
            return True
        return self.word is not None and (self.code.search(text) is not None or any(
            self.word.search(c) for c in re.findall(r"`[^`\n]+`", text)))

    def names(self, mention: str) -> bool:
        return any(rx is not None and rx.search(mention) for rx in (self.qual, self.word))


def _edited_lines(root: str, base: str, docs: list[str]) -> dict[str, set[int] | None]:
    """The lines of docs edited from base to the working tree (None: the whole doc, new)."""
    out: dict[str, set[int] | None] = {d: None for d in docs}     # untracked: new
    if not docs:
        return out
    diff = _git(root, "diff", "-U0", "--no-renames", "--relative", base, "--", *docs)
    tracked = _git(root, "ls-files", "-z", "--", *docs) or b""
    for d in tracked.decode("utf-8", "replace").split("\0"):
        if d in out:
            out[d] = set()
    for f, h in _hunks((diff or b"").decode("utf-8", "replace")).items():
        if f in out:
            out[f] = None if h is None else {k for _a, _b, c, d in h for k in range(c, c + d)}
    return out


def _listing(paths, full: bool = False, cap: int = 10) -> str:
    """Paths in full when few (or full), else by folder with their counts."""
    paths = sorted(paths)
    if full or len(paths) <= cap:
        return ", ".join(paths)
    by: dict[str, int] = {}
    for p in paths:
        by[os.path.dirname(p) or "."] = by.get(os.path.dirname(p) or ".", 0) + 1
    rows = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
    return "by folder: " + ", ".join(f"{d}/ ({n})" for d, n in rows[:cap]) + \
        (f", … ({len(rows) - cap} more folders)" if len(rows) > cap else "")


def scope_line(sc: Scope, full: bool = False) -> str:
    """The `scope:` line of the output: what was read, from where, and what was not."""
    shown = ", ".join(sc.globs[:4]) + (", …" if len(sc.globs) > 4 else "")
    if sc.how == "given":
        head = f"scope: the files given ({shown})"
    elif sc.how == "config":
        head = (f"scope: [check_refs] files of {sc.origin} ({shown or 'empty'})")
    else:
        head = f"scope: the default, files agents read ({AGENT_FILES_SHORT})"
    if sc.exclude:
        head += " minus exclude (" + ", ".join(sc.exclude[:3]) + \
            (", …" if len(sc.exclude) > 3 else "") + ")"
    head += f": {sc.total} file{'s' if sc.total != 1 else ''}"
    left = ([f"{sc.others} other .md file{'s' if sc.others != 1 else ''} of the checkout"]
            if sc.others and sc.how != "given" else []) + \
        ([f"{sc.archived} under archive folders (archive/, old/…)"] if sc.archived else [])
    if left:
        head += "; not checked: " + " and ".join(left) + (
            " (set [check_refs] files in .scipq.toml to widen the scope, or name them in files)"
            if sc.how == "default" else " (name them in files)")
    if sc.excluded:
        head += f"; {sc.excluded} .md file{'s' if sc.excluded != 1 else ''} excluded"
    if sc.vendored:
        head += (f"; {sc.vendored} file{'s' if sc.vendored != 1 else ''} of third-party code "
                 "(vendor/, a LICENSE of its own) left out")
    out = [head]
    if sc.since:
        def n_(k: int, one: str, many: str) -> str:
            return f"{k} {one if k == 1 else many}"
        out.append(
            f"since {sc.since} (merge base {sc.base[:10]}): "
            f"{n_(sc.changed_code, '.java file', '.java files')} changed; read "
            f"{n_(sc.changed_docs, 'doc', 'docs')} of the scope changed since then (the lines "
            f"edited, and mentions of the changed code) and "
            f"{n_(sc.citing, 'doc that mentions', 'docs that mention')} the changed code "
            "(only those mentions)" + (f": {_listing(sc.citing_docs, full)}"
                                       if sc.citing_docs else ""))
        if sc.older:
            out.append(f"{n_(sc.older, 'older problem', 'older problems')} of the changed "
                       "docs, on lines not edited since then, not shown (name the docs in "
                       "files to see them)")
        if sc.citing_outside:
            n = len(sc.citing_outside)
            out.append(f"{n_(n, '.md file', '.md files')} out of scope "
                       f"mention{'s' if n == 1 else ''} the changed code: "
                       + _listing(sc.citing_outside, full) +
                       " (name them, or their folders, in files to check them)")
    return "\n".join(out)


# Folders that hold records by their name: a log, a journal, audits…
_RECORD_DIRS = {"log", "logs", "journal", "journals", "bitacora", "bitácora", "bitacoras",
                "bitácoras", "diario", "audit", "audits", "auditoria", "auditorías",
                "auditorias", "incidents", "postmortems", "handoffs", "worklog", "worklogs",
                "minutes", "reports", "informes"}


# Files no agent follows (a license) and records by their name (a changelog).
_NOT_GUIDES = re.compile(r"(?i)(?:license|licence|copying|notice)(?:[-_.][\w.-]*)?\.md$")
_RECORD_FILES = re.compile(r"(?i)(?:changelog|changes|history|release[-_]?notes)(?:[-_.][\w.-]*)?"
                           r"\.md$")


def md_folders(root: str, listed: list[str] | None = None, limit: int | None = None
               ) -> list[tuple[str, int, int, str]]:
    """Folders with Markdown files out of the default scope: (folder, files, how many look
    like records, "records" / "bundle" / ""), most files first, for init-config to suggest a
    scope. Records: named by date or ticket, or in a folder named like a log (logs/,
    journal/…); a bundle: the folder holding skills/*/SKILL.md (its agents/, commands/…)."""
    listed = _listed(root) if listed is None else listed
    default = set(_files(root, AGENT_FILES, [], listed, nocase=True))
    bundles = {os.path.dirname(os.path.dirname(os.path.dirname(p))) for p in listed
               if os.path.basename(p).upper() == "SKILL.MD" and
               os.path.basename(os.path.dirname(os.path.dirname(p))) == "skills"}
    groups: dict[str, list[int]] = {}
    for p in listed:
        if not _is_md(p) or p in default:
            continue
        parts = p.split("/")[:-1]
        if ARCHIVE_DIRS & {d.lower() for d in parts} or _generated(p) or \
                _NOT_GUIDES.match(os.path.basename(p)):
            continue
        key = next((b for b in sorted(bundles, key=len, reverse=True)
                    if b and p.startswith(b + "/")), None) or "/".join(parts[:2]) or "."
        g_ = groups.setdefault(key, [0, 0])
        g_[0] += 1
        g_[1] += bool(_DATED.search(p) or _RECORD_DIRS & {d.lower() for d in parts} or
                      _RECORD_FILES.match(os.path.basename(p)))
    return sorted(((k, v[0], v[1], "bundle" if k in bundles else
                    "records" if v[1] * 2 > v[0] else "") for k, v in groups.items()),
                  key=lambda x: (-x[1], x[0]))[:limit]


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
          skip_archives: bool = True, other_versions: list[str] = (),
          scope: Scope | None = None, since: str = ""
          ) -> tuple[list[Problem], int, Scope]:
    """(problems, mentions checked, what was read). With skip_archives, files under folders of
    closed or historical docs (archive/, done/…) are left out unless a glob names the folder.
    With since (a commit or branch), only the docs changed from its merge base with HEAD and
    those that mention .java files changed since then; in the latter, only those mentions."""
    sc = scope or Scope(how="given", globs=tuple(globs))
    sc.exclude = tuple(exclude)
    m = Model(g, cache_base)
    other_versions = [v.strip() for v in other_versions if v and v.strip()]
    sources = {v[0] for v in m.where.values()}
    by_suffix: dict[str, list[str]] = {}
    for s in sorted(sources):
        parts = s.split("/")
        for k in range(len(parts)):
            by_suffix.setdefault("/".join(parts[k:]), []).append(s)
    problems: list[Problem] = []
    checked = 0
    listed = _listed(root)
    files = _files(root, globs, exclude, listed, nocase=sc.how == "default")
    if sc.how != "given":
        lic = {os.path.dirname(f) for f in listed if _LICENSE.match(os.path.basename(f))} - {""}
        third = [f for f in files if _vendored(f, lic)]
        sc.vendored, files = len(third), [f for f in files if f not in set(third)]
    # Each file once (CLAUDE.md as a link to AGENTS.md), none from outside the checkout.
    real_root, seen_real, aliases = os.path.realpath(root), set(), set()
    for f in list(files):
        r = os.path.realpath(os.path.join(root, f))
        if r in seen_real or not (r == real_root or r.startswith(real_root + os.sep)):
            aliases.add(f)
        seen_real.add(r)
    files = [f for f in files if f not in aliases]
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
    named = {d for g_ in globs for d in g_.lower().split("/")} & ARCHIVE_DIRS

    def in_archive(f: str) -> bool:
        return skip_archives and bool((ARCHIVE_DIRS - named) &
                                      {d.lower() for d in f.split("/")[:-1]})
    def is_excluded(f: str) -> bool:
        return any(_match(f, x, dirs=True) for x in exclude)
    archived = {f for f in files if in_archive(f)}
    files = [f for f in files if f not in archived]
    in_scope = set(files)
    excluded = {f for f in listed if _is_md(f) and is_excluded(f)}
    if skip_archives and sc.how != "given":     # the checkout's, not only the globs' ones
        archived |= {f for f in listed if _is_md(f) and f not in in_scope and in_archive(f)}
    archived -= excluded
    sc.total, sc.archived, sc.excluded = len(in_scope), len(archived), len(excluded)
    sc.others = sum(1 for f in listed if _is_md(f) and f not in in_scope and f not in archived
                    and f not in excluded and f not in aliases) - sc.vendored
    citing_only: set[str] = set()       # docs read only for their mentions of changed code
    java_set: set[str] = set()
    if since:
        got = _changed_since(root, since)
        if isinstance(got, str):
            sc.error = got
            return [], 0, sc
        sc.since, (sc.base, changed) = since, got
        java_set = {f for f in changed if f.endswith(".java")}
        sc.changed_code = len(java_set)
        changed_set = set(changed)
        ch = _Changed(m, java_set, listed)

        def cites(f: str) -> bool:
            try:
                with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                    return ch.cites(fh.read())
            except OSError:
                return False
        keep = []
        for f in files:
            if f in changed_set:
                sc.changed_docs += 1
                keep.append(f)
            elif cites(f):
                sc.citing += 1
                citing_only.add(f)
                keep.append(f)
        files = keep
        sc.citing_docs = tuple(sorted(citing_only))
        sc.citing_outside = tuple(f for f in listed if _is_md(f) and f not in in_scope and
                                  not in_archive(f) and not is_excluded(f) and
                                  not _generated(f) and not _RECORD_FILES.match(
                                      os.path.basename(f)) and cites(f))
        edited = _edited_lines(root, sc.base, [f for f in files if f in changed_set])
    sc.read = len(files)
    line_counts: dict[str, int] = {}
    text_cache: dict[str, list[str]] = {}
    # A mention right after a branch's name or label (main `X.foo()`, v4 `X.foo`): that
    # branch's code, not this one's.
    elsewhere = re.compile(r"(?:\b(?:en|in|on|de)\s+(?:main|master)|(?<![\w-])(?:main|master)\s+"
                           r"[0-9a-f]{7,40}|(?:^|[(\[,;—–]\s*)(?:main|master)|"
                           r"(?<![\w-])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?:…|\.\.\.)?(?=:)|"
                           r"\bgit\s+show\s+[^\s`]*" +
                           "".join("|(?<![\\w-])" + re.escape(v) for v in other_versions) +
                           r")(?:\s+|(?<=[0-9a-f….]):)`?$", re.I)
    # A configured label covers the rest of its sentence or list, up to another version
    # word (v4 `A`, `B`; v5 `C`), across a line break too.
    label = re.compile(r"(?<![\w/.-])(?:" + "|".join(map(re.escape, other_versions)) +
                       r")(?![\w/-])", re.I) if other_versions else None
    # (not a folder: src/main/java)
    version_word = re.compile(r"(?<![\w/.-])(?:v\d+(?:\.\d+)*|main|master)(?![\w/-])", re.I)
    sentence_end = re.compile(r"[.!?](?=\s+[A-Z¿¡`(-])| · |\|")   # a ";" goes on

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
        # Path abbreviations the document declares: `I/` = `core/src/…/billing/`
        abbrev = {k: v for k, v in re.findall(r"`([\w.-]+/)`\s*=\s*`([^`\s]+/)`", text)}
        prev = ""
        # A paragraph or list item wrapped over several lines is read as one line; problems
        # are reported at the physical line of their mention.
        for lns, starts, line in _logical_lines(text):
            before_line, prev = prev, line

            def ln_of(pos: int, lns=lns, starts=starts) -> int:
                return lns[bisect.bisect_right(starts, pos) - 1]
            code = [mo.span() for mo in _CODE.finditer(line)]
            gone_lines = {lns[k] for k, a_ in enumerate(starts)
                          if _GONE.search(line[a_:starts[k + 1] if k + 1 < len(starts) else None])}
            examples = [e.end() for e in _EXAMPLE.finditer(line)]
            at = [0, 0]         # where the mention is, and its head's (a `:N`'s path)

            def in_code(pos: int) -> bool:
                return any(a <= pos < b for a, b in code)

            def add(mention: str, what: str, kind: str, **extra) -> None:
                hypothetical = any(e <= at[0] <= e + 60 and ln_of(e) == ln_of(at[0])
                                   for e in examples) and \
                    kind in ("no-type", "no-member", "no-file")
                ln = ln_of(at[0])
                if ln not in gone_lines and not hypothetical and not any(
                        elsewhere.search(line[max(0, x - 40):x]) or
                        labelled(line, x, before_line) for x in {at[0], at[1] or at[0]}):
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
                if mo is None or mo.group(2) in _NOT_MEMBERS or mo.group(1)[:1].islower() or \
                        m.is_package(mo.group(0).replace(".", "/")):
                    continue        # com.x.Type.member: the qualified-name check's
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
            paths = list(_PATH.finditer(line))
            heads: dict[int, list[str]] = {}
            taken = {b.start() for mo in paths for b in _BARE.finditer(line, *mo.span())}
            if not paths:
                for one in _typed_numbers(m, line, code, taken, heads):
                    checked += 1
                    at[0], at[1] = one.start(), one.head_at
                    _check_path(m, root, one, by_suffix, sources | unindexed, named_here, line,
                                code, count_lines, add, text_cache, os.path.dirname(path),
                                abbrev, other_versions, before_line, starts, heads)
            for k, mo in enumerate(paths):
                # `X.java:12` and the line numbers alone after it (`:30`, :45-48), each a
                # mention of that file, up to the next path or the end of the parenthesis
                # holding it; after another type's name (`Engine.run :587`), that type's
                stop = paths[k + 1].start() if k + 1 < len(paths) else len(line)
                ones = [mo]
                if mo.group(2):
                    head, last = mo, mo.end()
                    # (`X.java:12`) alone: what follows is still X's; a parenthesis with more
                    # in it (its own list, other words) ends the run
                    close = _group_end(line, mo.start(), code, mo.span())
                    for b_ in _BARE.finditer(line, mo.end(), min(stop, close)):
                        th, right_after = _type_between(m, line, last, b_.start(),
                                                        mo.group(1), code)
                        # Near its path (the same sentence or cell), or right after the type
                        # it belongs to; else git must show the doc's version had it there.
                        near = right_after or not _SENTENCE_END.search(line, last, b_.start())
                        last = b_.end()
                        if th == "same":
                            head = mo
                        elif th is False:
                            head = None     # a library's, or a name several files have
                        elif th is not None:
                            th.also = mo.group(1)     # it may still be the path's
                            head = th
                            heads[th.start()] = th.names    # the member named with it
                        if head is not None:
                            ones.append(_Continuation(line, b_, head, strict=not near))
                            taken.add(b_.start())
                if k == len(paths) - 1:
                    ones += _typed_numbers(m, line, code, taken, heads)
                for one in ones:
                    checked += 1
                    at[0] = one.start()
                    at[1] = getattr(one, "head_at", 0)
                    _check_path(m, root, one, by_suffix, sources | unindexed, named_here, line,
                                code, count_lines, add, text_cache, os.path.dirname(path),
                                abbrev, other_versions, before_line, starts, heads)
    if citing_only:
        problems = [p for p in problems if p.path not in citing_only or p.src in java_set or
                    ch.names(p.mention)]
    _since_written(root, problems)
    _gone(root, problems)
    problems = [p for p in problems if p.kind not in ("drop", "weak", "quoted") and
                (not p.loose or "gone from" in p.what) and
                not (p.cont and p.kind == "line-moved")]
    if since:
        # In the docs changed since then: the lines edited, and mentions of changed code.
        def new(p: Problem) -> bool:
            lines = edited.get(p.path, ())
            return p.path not in edited or lines is None or p.line in lines or \
                p.src in java_set or ch.names(p.mention)
        sc.older = len({(p.path, p.mention, p.what) for p in problems if not new(p)})
        problems = [p for p in problems if new(p)]
    return problems, checked, sc


# Output that does not depend on the user's git configuration (prefixes, quoting, colors).
_GIT_CONFIG = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c",
               "core.quotePath=false", "-c", "diff.relative=false", "-c", "color.ui=false",
               "-c", "log.showSignature=false")


_BLOCK_START = re.compile(r"\s*(?:[-*+]\s|\d+[.)]\s|#|\||>|<)")


def _logical_lines(text: str):
    """(physical line numbers, where each starts in the joined text, the joined text) of a
    Markdown text: a paragraph or a list item wrapped over several lines is one; table rows,
    headings and lines of fenced code are one each; fenced blocks in other languages are
    left out."""
    out = []
    block: list[tuple[int, str]] = []

    def flush() -> None:
        if block:
            lns, starts, joined = [], [], ""
            for ln, raw in block:
                piece = raw if not lns else raw.lstrip()
                if lns:         # a path cut at a "/" goes on without a space
                    joined += "" if joined.endswith("/") and re.match(r"[\w$.-]", piece) \
                        else " "
                lns.append(ln)
                starts.append(len(joined))
                joined += piece
            out.append((lns, starts, joined))
            block.clear()
    fence = None
    for ln, raw in enumerate(text.split("\n"), 1):
        f = _FENCE.match(raw)
        if f:
            flush()
            fence = None if fence is not None else f.group(2).lower()
            continue
        if fence is not None:
            if fence in _JAVA_FENCES:
                out.append(([ln], [0], raw))
            continue
        if not raw.strip():
            flush()
            continue
        if _BLOCK_START.match(raw) or (block and _BLOCK_START.match(block[0][1]) and
                                       block[0][1].lstrip()[:1] in "#|<"):
            flush()
        block.append((ln, raw))
        if raw.lstrip()[:1] in "#|":
            flush()             # a heading or a table row stands alone
    flush()
    return out


def _git(root: str, *args: str, data: bytes | None = None) -> bytes | None:
    try:
        r = subprocess.run(["git", *_GIT_CONFIG, "-C", root, *args], input=data,
                           capture_output=True, timeout=120)
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
                last.setdefault(row, commit)        # newest first, by path
        for p in files:
            # The doc's path (its end, after a "…/"), or its file name when it wrote just that
            hit = next((c for row, c in last.items() if row == p.src or row.endswith("/" + p.src)),
                       None)
            if hit:
                sha, d = hit
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


def _blob_stream(root: str, names: list[str]):
    """git cat-file --batch, one object at a time: (name, text) of each blob, so that many
    versions of a big doc never sit in memory together."""
    if not names:
        return
    try:
        proc = subprocess.Popen(["git", *_GIT_CONFIG, "-C", root, "cat-file", "--batch"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return
    feeder = threading.Thread(target=lambda: (proc.stdin.write(
        "\n".join(names).encode() + b"\n"), proc.stdin.close()), daemon=True)
    feeder.start()
    try:
        for name in names:
            head = proc.stdout.readline().split()
            if len(head) < 3 or head[1] != b"blob":
                continue
            size = int(head[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)             # the newline after the contents
            yield name, body.decode(errors="replace")
    finally:
        proc.stdout.close()
        proc.wait()
        feeder.join()


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


_ENCLOSING: dict[int, tuple] = {}
_HEADS = {"if", "for", "while", "switch", "catch", "synchronized", "try", "return", "new"}


def _enclosing(lines: list[str], n: int) -> str | None:
    """The name of the method or constructor whose body holds line n (1-based) of a source,
    by its braces (strings and comments masked); None at class level or when not found."""
    got = _ENCLOSING.get(id(lines))
    if got is None or got[0] is not lines:
        from .javasrc import mask
        got = (lines, mask("\n".join(lines)).split("\n"))
        if len(_ENCLOSING) > 64:
            _ENCLOSING.clear()
        _ENCLOSING[id(lines)] = got
    masked = got[1]
    depth = 0
    for j in range(min(n, len(masked)) - 1, -1, -1):
        row = masked[j] if j < n - 1 else masked[j][:0]
        for k in range(len(row) - 1, -1, -1):
            c = row[k]
            if c == "}":
                depth += 1
            elif c == "{":
                if depth:
                    depth -= 1
                    continue
                head = " ".join(masked[max(0, j - 3):j] + [row[:k]]).rstrip()
                if re.search(r"\b(?:class|interface|enum|record)\b[^;{}()]*$", head):
                    return None
                mo = re.search(r"([\w$]+)\s*\([^;{}]*\)\s*(?:throws\s+[\w$.,\s<>]+)?$", head)
                if mo and mo.group(1) not in _HEADS and not head.endswith("->"):
                    return mo.group(1)
    return None


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
    if len(hits) > 1 or sum(1 for i, x in enumerate(old) if x.strip() == block[k0] and
                            [y.strip() for y in old[i - k0:i - k0 + len(block)]] == block) > 1:
        # A repeated text (a try-with-resources in every method): only a copy in the same
        # method is that block; else it is not known where it went.
        was = _enclosing(old, n)
        hits = [h for h in hits if was is not None and _enclosing(cur, h + 1) == was]
        mine = [i - k0 for i, x in enumerate(old) if x.strip() == block[k0] and i - k0 >= 0 and
                [y.strip() for y in old[i - k0:i - k0 + len(block)]] == block and
                _enclosing(old, i - k0 + 1) == was]
        if not hits or len(hits) != len(mine) or n - 1 not in mine:
            return None     # as many copies in that method then and now, or it is not known
        return hits[mine.index(n - 1)] + 1, hits[mine.index(n - 1)] + len(block)
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
    doc_lines: dict[str, list[str]] = {}
    for p in todo:
        if p.path not in doc_lines:
            try:
                with open(os.path.join(root, p.path), encoding="utf-8", errors="replace") as fh:
                    doc_lines[p.path] = fh.read().split("\n")
            except OSError:
                doc_lines[p.path] = []
    texts_of = {id(p): doc_lines[p.path][p.line - 1] if p.line <= len(doc_lines[p.path])
                else None for p in todo}
    wanted = {x for x in texts_of.values() if x is not None}
    # Which of the lines looked for each version of a doc has (CRLF docs too), one at a time
    blobs = sorted({b for vs in versions.values() for *_x, b in vs})
    doc_texts = {b: wanted & {x.rstrip("\r") for x in t.split("\n")}
                 for b, t in _blob_stream(root, blobs)}
    written: list[tuple[Problem, str, int]] = []           # (problem, commit, time)
    for p in todo:
        text = texts_of[id(p)]
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
    first_parent: dict[str, str] = {}
    out = _git(root, "log", "--no-walk", "--format=%H %P", *sorted({c for _p, c, _t in written}))
    for row in (out or b"").decode(errors="replace").splitlines():
        if len(row.split()) > 1:
            first_parent[row.split()[0]] = row.split()[1]
    times: dict[str, int] = {}
    out = _git(root, "log", "--no-walk", "--format=%H %ct %cs", *sorted(set(first_parent.values())))
    for row in (out or b"").decode(errors="replace").splitlines():
        if len(row.split()) == 3:
            sha, ts, day = row.split()
            times[sha], days[sha] = int(ts), day
    parents = {c: (pa, times[pa]) for c, pa in first_parent.items() if pa in times}
    olds = _blobs(root, sorted({f"{c}:{top}{f}" for p, c, _t in written
                                for f in (p.src, *p.alts)} |
                               {f"{parents[c][0]}:{top}{f}" for p, c, _t in written
                                if c in parents for f in (p.src, *p.alts)}))

    def held(p: Problem, old: str | None, slack: int = 0) -> bool:
        if old is None:
            return False
        text = old.split("\n")
        if p.kind == "quoted":          # the doc's version had the code quoted at the lines
            return p.names[0] in "".join(re.sub(r"\s+", "", x)
                                         for x in text[max(0, p.lines[0] - 3):p.lines[1] + 2])
        if p.kind == "weak":            # the doc pointed at the declaration then
            return _at_declaration(text, *p.lines, p.names or (p.name,))
        return any(_holds(text, max(1, lo - slack), hi + slack, p.names or (p.name,))
                   for lo, hi in [p.lines, *p.more]) or (
            p.cont and _at_declaration(text, *p.lines, tuple(_specific(p.names or (p.name,)))))
    base: dict[tuple[str, int], list[Problem]] = {}
    holding: set[int] = set()           # problems whose version held the name there
    later: list[tuple[Problem, str, int]] = []
    for p, c, t in written:
        first = [(c, t)] + ([parents[c]] if c in parents else [])
        hit = next((v for v in first if held(p, olds.get(f"{v[0]}:{top}{p.src}"))), None)
        for alt in p.alts if not hit else ():      # a same-named file held it then: that one
            hit = next((v for v in first if held(p, olds.get(f"{v[0]}:{top}{alt}"))), None)
            if hit:
                was = os.path.basename(p.src) + ":"
                if p.mention.startswith(was):      # the name it was shown with
                    p.mention = os.path.basename(alt) + ":" + p.mention[len(was):]
                p.src = alt
                break
        if hit:
            base.setdefault(hit, []).append(p)
            holding.add(id(p))
        elif p.kind in ("weak", "quoted"):
            p.kind = "drop"     # it was not the declaration (the code) then either
        elif (near := next((v for v in first if held(p, olds.get(f"{v[0]}:{top}{p.src}"),
                                                     slack=4)), None)):
            base.setdefault(near, []).append(p)     # a few lines off in the doc's version
        elif p.tight:
            later.append((p, c, t))
        else:
            # A name from further away (what the lines are about, not what they say): an
            # older version that had it there proves nothing; the doc's own version decides.
            base.setdefault((c, t), []).append(p)
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
            if p.cont and p.kind != "quoted" and (id(p) not in holding or
                                                  not _specific(p.names or (p.name,))):
                p.kind = "drop"     # nothing shows the `:N` was this file's
                continue
            n, end = p.lines
            span = f"line {n}" if n == end else f"lines {n}-{end}"
            if p.kind == "quoted":
                old = (olds.get(f"{sha}:{top}{p.src}") or "").split("\n")
                was = [k for k in _code_at(old, p.names[0]) if n - 2 <= k <= end + 1]
                at = _code_at(now_lines(p.src), p.names[0])
                if was and len(at) == 1 and abs(at[0] - was[0]) > 2:
                    d = at[0] - was[0]
                    # The doc's lines decide (intact: moved); the code quoted only places them.
                    cur = now_lines(p.src)
                    same = _block_at(old, cur, n, end, n + d) == (n + d, end + d) \
                        if n != end else 0 < n <= len(old) and 0 < n + d <= len(cur) and \
                        old[n - 1].strip() == cur[n + d - 1].strip()
                    if same and n != end:
                        p.what = f"lines {n}-{end} are now {n + d}-{end + d} (the code quoted; " \
                                 f"moved since the doc's version of {day})"
                        p.kind = "line-shifted"
                    elif same:      # the very line, elsewhere
                        p.what = f"line {n} is now {n + d} (the code quoted; moved since " \
                                 f"the doc's version of {day})"
                        p.kind = "line-shifted"
                    else:           # the code quoted went on, its line or lines changed
                        span = f"line {n}" if n == end else f"lines {n}-{end}"
                        p.what = f"{span} changed since the doc's version ({day}, " \
                                 f"{sha[:8]}); the code quoted is now at {at[0]}"
                        p.kind = "line-changed"
                else:
                    p.kind = "drop"
                continue
            if id(p) not in holding:
                old = (olds.get(f"{sha}:{top}{p.src}") or "").split("\n")
                n2, end2 = _map_line(h, n), _map_line(h, p.lines[1])
                went = None if _trivial(old, n, p.lines[1]) else \
                    _block_at(old, now_lines(p.src), n, p.lines[1], n2 or n) or \
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
            if moved and p.recheck(*moved, also, at=p.src):
                m2, e2 = moved
                others = ", ".join(f"{a}" + (f"-{b}" if b != a else "") + " → " +
                                   f"{r[0]}" + (f"-{r[1]}" if r[1] != r[0] else "")
                                   for (a, b), r in zip(p.more, also))
                p.what = (f"line {n} is now {m2}" if n == end else
                          f"lines {n}-{end} are now {m2}-{e2}") + \
                    f" (moved since the doc's version of {day}" + (
                        f"; the others listed with it: {others}" if others and
                        len(also) == len(p.more) else "") + ")"
                p.kind = "line-shifted"
            elif edited or _repeats(old, now_lines(p.src), n, end):
                # (a text that repeats and could not be placed: not known where it went)
                mapped = n2 is not None and end2 is not None
                now = (n2, end2) if mapped else _edited_to(h, n, end)
                if now and p.recheck(*now, also, at=p.src) and _specific(p.names or (p.name,)):
                    at = now if mapped else \
                        _naming(root, p.src, *now, p.names or (p.name,), current) or now
                    if p.span and p.span[0] <= at[1] and at[0] <= p.span[1]:
                        at = (max(at[0], p.span[0]), min(at[1], p.span[1]))   # its own lines
                    where = f"; now at {at[0]}" if at[0] == at[1] else \
                        f"; now in {at[0]}-{at[1]}"
                elif not p.span and p.name and _declared(old, p.name):
                    # declared there then, not now: its name elsewhere is someone else's
                    where = f": {p.name} is no longer declared in {os.path.basename(p.src)}"
                else:
                    where = _in_its_method(old, now_lines(p.src), n, end, p.name) or (
                        f": {p.hint}" if p.hint else "")
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


def _copies(lines: list[str], block: list[str]) -> list[int]:
    """The 1-based lines where the stripped block starts in lines."""
    if not block:
        return []
    return [i + 1 for i, x in enumerate(lines) if x.strip() == block[0] and
            [y.strip() for y in lines[i:i + len(block)]] == block]


def _repeats(old: list[str], cur: list[str], n: int, end: int) -> bool:
    """The text of lines n..end of old is in old or cur more than once."""
    block = [x.strip() for x in old[n - 1:end]]
    return any(len(x) > 3 for x in block) and \
        (len(_copies(old, block)) > 1 or len(_copies(cur, block)) > 1)


def _in_its_method(old: list[str], cur: list[str], n: int, end: int, name: str) -> str:
    """For lines n..end of old that are no longer where they were: the method that held
    them, as it is now. Where its calls of name are, or where the same code went when only
    one copy is left, in a method it calls (an extract-method); "" when not known."""
    was = _enclosing(old, n)
    spans = _declared(cur, was) if was else []
    if was and not spans and _declared(old, was):
        return f": {was}, which held them, is no longer in the file"
    if not spans:
        return ""
    lo, hi = spans[0][1], spans[0][2]
    block = [x.strip() for x in old[n - 1:end]]
    there = [c for c in _copies(cur, block) if not lo <= c <= hi]
    left = [c for c in _copies(cur, block) if lo <= c <= hi]
    if not left and len(there) == 1 and (other := _enclosing(cur, there[0])) and other != was:
        call = re.compile(r"(?<![\w$])" + re.escape(other) + r"\s*\(")
        at = [k + 1 for k in range(lo, min(hi, len(cur))) if call.search(cur[k])]
        return f": the same code is now at {there[0]}, in {other}" + (
            f", which {was} calls at {at[0]}" if at else "")
    if name:
        for verb, rx in (("calls", r"\s*\("), ("names", r"(?![\w$])")):
            word = re.compile(r"(?<![\w$])" + re.escape(name) + rx)
            at = [k + 1 for k in range(lo - 1, min(hi, len(cur))) if word.search(cur[k])]
            if at:
                return f": {was} {verb} {name} at " + ", ".join(map(str, at[:4])) + (
                    " …" if len(at) > 4 else "")
        # moved to a method it calls (an extract-method): that one, and where
        word = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
        for k in range(lo - 1, min(hi, len(cur))):
            for c in re.findall(r"(?<![\w$.])([a-z_$][\w$]*)\s*\(", cur[k]):
                if c in (was, name) or c in _HEADS:
                    continue
                for _top, d, e in _declared(cur, c)[:1]:
                    got = [j + 1 for j in range(d - 1, min(e, len(cur))) if word.search(cur[j])]
                    if got:
                        return (f": {was} no longer names {name}; {c}, which it calls at "
                                f"{k + 1}, does at {got[0]}")
        return f": {was} no longer names {name}"
    return ""


def _trivial(lines: list[str], n: int, end: int) -> bool:
    """Lines n..end of a text are blank or only braces and punctuation: no text to place
    them by (a `}` is like any other)."""
    return all(not re.search(r"[\w$]", lines[k - 1]) for k in range(n, end + 1)
               if 0 < k <= len(lines))


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
                abbrev: dict | None = None, other_versions=(), prev_line: str = "",
                starts=(0,), heads: dict | None = None) -> None:
    written, n0 = mo.group(1), mo.group(2)
    shown = getattr(mo, "shown", None) or mo.group(0)     # a `:N` after a path: path:N
    also = getattr(mo, "also", None)    # after another type's name: its file or the path's
    if getattr(mo, "strict", False) or also:
        add_ = add

        def add(mention, *a, **e):
            if also and e.get("src"):       # the file the lines were checked in
                mention = f"{os.path.basename(e['src'])}:{mo.group(2)}" + (
                    f"-{mo.group(3)}" if mo.group(3) else "")
            add_(mention, *a, cont=getattr(mo, "strict", False), **e)

    def physical(pos: int) -> int:
        return bisect.bisect_right(starts, pos)
    for k, v in (abbrev or {}).items():
        if written.startswith(k):
            written = v + written[len(k):]
            break
    if re.match(r"[ab]/", written) and not _find_path(written, by_suffix, sources):
        written = written[2:]           # a git diff's a/ and b/ prefixes
    if re.search(r"/tmp\b|\bscratch\b", line[max(0, mo.start() - 40):mo.start()]):
        return                          # a scratch copy, not the checkout's file
    hits = list(_find_path(written, by_suffix, sources))    # a copy: by_suffix's lists stay
    if also:
        hits += [h for h in _find_path(also, by_suffix, sources) if h not in hits]
    if written.startswith(("/", "~/")):
        if not hits:
            return          # an absolute path to a file this repository does not have
        if len(hits) == 1:  # shown as the checkout's path it stands for (`shown` itself
            absolute, add_abs = mo.group(1), add    # tells a path from a `:N` after it)

            def add(mention, *a, **e):
                add_abs(mention.replace(absolute, hits[0], 1), *a, **e)
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
        add(shown, "no such file" + (f" (a {base} is at {', '.join(same[:2])})"
                                          if same else ""), "no-file", name="" if same else base,
            src="" if same else re.split(r"(?:\.\.\.|…)/", bare)[-1])
        return
    if n0 is None:
        return
    n = int(n0)
    # Another version of the file (`git show <sha>:path`, `main <sha>:path`): not this one.
    before = line[max(0, mo.start() - 80):mo.start()].rstrip("`")
    if re.search(r"(?:[0-9a-f]{7,40}|[\w./~^-]*(?:main|master|HEAD|origin/[\w./-]+)[\w~^]*):$"
                 r"|\bgit show\b[^`]*$|(?:^|[\s(`])/(?:home|Users|tmp|mnt|var)/\S*$"
                 r"|(?<![\w-])[0-9a-f]{7,40}\s+`?$|\b(?:en|in|on|de)\s+(?:main|master)\s+`?$" +
                 "".join("|(?<![\\w-])" + re.escape(v) + r"\s+`?$" for v in other_versions),
                 before, re.I):
        return      # another version: <ref>:path, git show, an absolute path elsewhere
    if re.match(r"`?\s*\((?:en |in |de |on |at )?(?:main|master|[0-9a-f]{7,40})\)",
                line[mo.end():]) and not any(m.member_syms.get((t, "main")) for f in hits
                                             for t in m.types_in.get(f, [])):
        return      # File.java:12 (main): another branch's (unless the file has a main)
    clause = line[_segment(line, mo.start(), mo.end(), parens=False)[0]:mo.start()]
    clause = re.split(r"[.;:]\s|\s[—–]\s", clause)[-1]     # "… en main no. — evidencia: `X`"
    said = list(re.finditer(r"\b(?:en|in|on|de)\s+`?(?:(main|master" + "".join(
        "|" + re.escape(v) for v in other_versions) + r")|HEAD|la rama|the branch|this branch|"
        r"esta rama|el checkout|the checkout)\b(?!\.)", clause, re.I))
    if said and said[-1].group(1):
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
    loose = {x[1:].lstrip("?") for x in glued if x[:1] == "~"}  # glued, not surely the line's
    glued = {x for x in glued if x[:1] != "~"} | {x[1:] for x in glued if x[:1] == "~"}
    # `a()` + `b()` (File:N): a list of names, each of them at the lines
    need_all = {x[1:] for x in glued if x[:1] == "+"}
    glued = {x for x in glued if x[:1] != "+"} | need_all
    glued = {x[1:] if x[:1] == "?" else x for x in glued
             if x[:1] != "?" or any(m.member_syms.get((t, x[1:])) for f in hits
                                    for t in m.types_in.get(f, []))}
    if re.search(r"(?:→|->)\s*$", line[max(0, oa - 4):oa]):
        # "a() → X (File:N)": the clause starts after the arrow; X is where the call goes
        def declared_here(x: str) -> bool:
            head = re.match(r"`?((?:[\w$]+[.#])*)" + re.escape(x) + r"\b", line[oa:ob].lstrip())
            owner = head.group(1).rstrip(".#").split(".")[-1] if head else ""
            return any(m.member_syms.get((t, x)) and (not owner or S.name_of(t) == owner)
                       for f in hits for t in m.types_in.get(f, []))
        loose |= {x for x in glued if not declared_here(x) and re.match(
            r"`?(?:[\w$]+[.#])*" + re.escape(x) + r"\b", line[oa:ob].lstrip())}
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
    for g_ in re.finditer(r"`?(?:[\w$]+[.#])*([a-z_$][\w$]*)(?:\([^()]*\))?`?\s+(?:de la |en la "
                          r"|at |on |in |of )?(?:l[ií]nea|line)\s+(\d+)", line[oa:ob]):
        if not n0 or int(g_.group(2)) != int(n0):
            others.add(g_.group(1))     # "el `setDashboard()` de la línea 106": that line's
    for x0, y0 in extra_spans:          # `getX()` vacío (`:106`) in the next cell
        others |= {x for b_ in _BARE.finditer(line, x0, y0)
                   for x in (y.lstrip("?") for y in _glued_names(line[x0:y0], b_.group(0)))} - glued
    found = [(S.name_of(x), at) for x, at in named_here if inside(at) and ours(at)] + \
        [(g.group(1), p + g.start()) for p, q in code if inside(p) for g in
         re.finditer(r"(?<![\w$.])([a-z_$][\w$]*)\s*\(", line[p:q]) if ours(p + g.start())] + \
        [(g.group(1), p + g.start(1)) for p, q in code if inside(p) and ours(p) for g in
         [re.fullmatch(r"`[A-Z][A-Z0-9_]+\.([a-z_$][\w$]*)`", line[p:q])] if g]
    # (`CONSTANT.member`: the member, the constant's type is not the line's file)
    # A paragraph wrapped over lines: the names on the mention's own line first.
    here = [x for x, at in found if physical(at) == physical(mo.start())]
    in_segment = sorted(set(here or [x for x, _at in found]))
    bare = not in_segment
    if bare:                # "único llamador de `admitir`": a member written alone
        in_segment = sorted({line[p + 1:q - 1] for p, q in code if inside(p) and ours(p) and
                             re.fullmatch(r"[a-z_$][\w$]*", line[p + 1:q - 1]) and
                             line[p + 1:q - 1] in m.by_member})
    in_segment = [x for x in in_segment if x not in others]
    def distance(x: str) -> int:
        at = line.rfind(x, oa, mo.start())
        return mo.start() - at if at >= 0 else max(0, line.find(x, mo.end(), ob)) + 10_000
    names: list[str] = sorted(glued, key=lambda x: (distance(x), x)) or in_segment
    # When the glued names cannot be checked in the file, those of the mention's clause.
    clause_found = [(S.name_of(x), at) for x, at in named_here if oa <= at < ob and ours(at)] + \
        [(g.group(1), p + g.start()) for p, q in code if oa <= p < ob for g in
         re.finditer(r"(?<![\w$.])([a-z_$][\w$]*)\s*\(", line[p:q]) if ours(p + g.start())]
    in_clause = {x for x, at in clause_found if physical(at) == physical(mo.start())} or \
        {x for x, _at in clause_found}
    fallback = sorted(x for x in in_clause if x not in names and x not in others)
    names += sorted(c for c in _glued_calls(line, mo, code, oa, ob)
                    if c not in names and c not in others)
    if heads is not None:
        if shown == mo.group(0):
            heads[mo.start()] = list(names)     # for the `:N` that follow it
        elif getattr(mo, "also", None) and heads.get(getattr(mo, "head_at", -1)):
            names = list(heads[mo.head_at])     # `T.m`:351: m, not the mention's before
        elif not names:
            names = list(heads.get(getattr(mo, "head_at", -1), []))   # the list's names
    # Names written right at the mention: only then may git say the doc was wrong.
    flow_cell = any(re.search(r"→|->", line[x:y]) for x, y in extra_spans)  # a call chain
    glued_here = {c[4:] if c.startswith("new ") else c
                  for c in _glued_calls(line, mo, code, oa, ob, tight=True)}
    tight = shown == mo.group(0) and (      # a `:N` alone after a path is a guess
        bool(glued - loose) or
        bool(extra_spans and set(names) - loose and not flow_cell and not (bare and not glued)) or
        bool(glued_here & set(names)))
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
                strict: bool = False, follow: bool = False):
        """None when the lines n..end_n of one of srcs still mention a name the doc gives
        (or no name can be checked there); else (name, where it is now, source). strict:
        they must start at the declaration of a name the file declares, or lie in it."""
        declaring = [f for f in srcs if any(m.member_syms.get((t, x)) for t in
                                            m.types_in.get(f, []) for x in names)]
        failed = None
        for src in declaring or srcs:    # same-named files: the one with the member named
            if need_all and not strict:
                # A list of names over a list of lines: each name at one of the lines.
                fails = [bad for x in names if x in need_all and all(
                    (bad := _verdict_in(m, root, src, lo, hi, [x], as_code, text_cache))
                    not in (None, "unchecked") for lo, hi in [(n, end_n), *also])]
                if not fails:
                    return None
                failed = failed or fails[0] + (src,)
                continue
            for lo, hi in [(n, end_n), *also]:
                bad = _verdict_in(m, root, src, lo, hi, names, as_code, text_cache, strict,
                                  need_all, follow)
                if bad == "unchecked" and fallback:
                    bad = _verdict_in(m, root, src, lo, hi, fallback, as_code, text_cache,
                                      strict)
                if bad is None or bad == "unchecked":
                    return None
                failed = failed or bad + (src,)
        return failed
    fits = [s for s in hits if n <= count_lines(s)]
    if not fits and shown != mo.group(0):
        return              # a `:N` past this file's end is likely another file's
    if not fits:
        src = hits[0]
        add(shown, f"line {n} is past the end ("
            + ", ".join(f"{s}: {count_lines(s)} lines" for s in hits[:3]) + ")", "past-end",
            src=src, lines=(n, end_n), names=tuple(names), tight=tight,
            recheck=lambda n2, m2, also=(), at=None: n2 <= count_lines(at or src) and
            verdict(n2, m2, [at or src], also) is None)
        return
    # As many calls written before the path as line ranges listed with it ("`execute()` …
    # y `close()` … (`F.java:403-421` y `:383-401`)"): respectively, each call at its lines.
    ordered = list(dict.fromkeys(
        g.group(1) for p, q in code if oa <= p < mo.start() and ours(p) for g in
        re.finditer(r"(?<![\w$])([a-z_$][\w$]*)\s*\(", line[p:q])
        if g.group(1) not in _IMPLICIT or any(m.member_syms.get((t, g.group(1)))
                                               for f in fits for t in m.types_in.get(f, []))))
    # A guess: only when the lines together do not hold the names, and it claims nothing
    # about the doc's own version.
    if not ordered:         # `opts.onOpen`/`onClose` (…:403-421, :383-401): the names alone
        ordered = list(dict.fromkeys(
            g.group(1) for p, q in code if oa <= p < mo.start() and ours(p)
            for g in [re.fullmatch(r"(?:[\w$]+\.)*([a-z_$][\w$]*)", line[p + 1:q - 1])] if g))
    if more and len(ordered) == len(more) + 1 and verdict(n, end_n, fits, more) is not None:
        src = fits[0]
        for x, (lo, hi) in zip(ordered, [(n, end_n), *more]):
            bad = _verdict_in(m, root, src, lo, hi, [x], as_code, text_cache)
            if bad in (None, "unchecked"):
                continue
            span = f"{lo}" if lo == hi else f"{lo}-{hi}"
            one = shown if lo == n else f"{mo.group(1)}:{span}"
            what = f"line {lo} no longer mentions {x} ({'; '.join(bad[1])})" if bad[1] else \
                f"line {lo} no longer calls {x} (no code in this file calls it now)"
            add(one, what, "line-moved", src=src, lines=(lo, hi), name=x, names=(x,),
                hint="; ".join(bad[1]), tight=False,
                recheck=lambda n2, m2, also=(), at=None, x=x: _verdict_in(
                    m, root, at or src, n2, m2, [x], as_code, text_cache, follow=True)
                in (None, "unchecked"), span=_span_of(m, root, src, x, text_cache))
        return
    # A statement the doc quotes right after the line number (`obj.setOwner(user)`): when
    # the lines have it, they are right; else git decides whether the doc's version had it
    # there, and where it stands now.
    quote = _quote_after(line, mo, code)
    if quote and not more:
        if not text_cache.get(fits[0]):
            _with_header(root, fits[0], 1, text_cache)      # reads the file into the cache
        flat = [re.sub(r"\s+", "", x) for x in text_cache.get(fits[0]) or []]
        if quote not in "".join(flat[max(0, n - 3):end_n + 2]):      # over line breaks
            add(shown, "", "quoted", src=fits[0], lines=(n, end_n), names=(quote,),
                recheck=lambda n2, m2, also=(), at=None: True)
    bad = verdict(n, end_n, fits, more)
    if bad is None:
        # It holds through a call or a comment while the file declares the name elsewhere:
        # git may show the doc pointed at the declaration (then the lines moved).
        weak = verdict(n, end_n, fits, (), strict=True)     # its own lines start there?
        if weak is not None:
            x, where, src = weak
            every_name = list(dict.fromkeys(names + fallback))
            add(shown, "", "weak", src=src, alts=tuple(f for f in fits if f != src),
                lines=(n, end_n), name=x,
                names=tuple(every_name), hint="; ".join(where),
                tight=tight, more=tuple(more),
                span=_span_of(m, root, src, x, text_cache),
                recheck=lambda n2, m2, also=(), at=None: _verdict_in(
                    m, root, at or src, n2, m2, every_name, as_code, text_cache, strict=True)
                in (None, "unchecked"))
        return
    x, where, src = bad
    extra = {"src": src, "lines": (n, end_n), "name": x, "hint": "; ".join(where),
             "names": tuple(dict.fromkeys(names + fallback)), "tight": tight,
             "more": tuple(more),
             "alts": tuple(f for f in fits if f != src),
             "recheck": lambda n2, m2, also=(), at=None: verdict(n2, m2, [at or src], also,
                                                        follow=True) is None,
             "span": _span_of(m, root, src, x, text_cache)}
    if where:
        add(shown, f"line {n} no longer mentions {x} ({'; '.join(where)})",
            "line-moved", **extra)
    else:
        add(shown, f"line {n} no longer calls {x} (no code in this file calls it now)",
            "line-moved", **extra)


class _Continuation:
    """A line number alone after a path (`X.java:12`, `:30`) as a mention of that file: a
    match-like object for _check_path."""

    def __init__(self, line: str, bare, head, strict: bool = False):
        self._line, self._b, self._head = line, bare, head
        self.head_at = head.start()
        self.strict = strict
        self.also = getattr(head, "also", None)
        self.shown = f"{getattr(head, 'label', head.group(1))}:{bare.group(1)}" + (
            f"-{bare.group(2)}" if bare.group(2) else "")

    def group(self, k: int = 0):
        return (self._line[self._b.start():self._b.end()], self._head.group(1),
                self._b.group(1), self._b.group(2))[k]

    def start(self) -> int:
        return self._b.start()

    def end(self) -> int:
        return self._b.end()

    def span(self) -> tuple[int, int]:
        return self._b.span()


class _TypeHead:
    """A project type named before a `:N` (`Engine.run :587`, IPOForm (:226)): the file that
    declares it, as the head of the numbers that follow."""

    def __init__(self, src: str, at: int, name: str, member: str | None):
        self._src, self._at = src, at
        self.label = name + ".java"
        self.names = [member] if member else []
        self.also = None

    def group(self, k: int = 0):
        return (self.label, self._src, None, None)[k]

    def start(self) -> int:
        return self._at


def _group_end(line: str, pos: int, code: list, alone=None) -> int:
    """The end of the parenthesis that holds pos (outside code spans), else the line's; the
    line's too when that parenthesis has no numbers but alone's (a span: `(X.java:12)`,
    `(con T, X.java:12)`): one with its own list (`(:102 → X.java:189)`, `(X.java:12 y 30)`)
    ends the run."""
    def outside(k: int) -> bool:
        return not any(a <= k < b for a, b in code)
    depth, opens = 0, []
    for k in range(pos):
        if line[k] in "()" and outside(k):
            if line[k] == "(":
                opens.append(k)
            elif opens:
                opens.pop()
            depth += 1 if line[k] == "(" else -1 if depth else 0
    if not depth:
        return len(line)
    end = _group_close(line, pos, outside)
    if alone is not None and opens and not re.search(
            r"\d", line[opens[-1] + 1:alone[0]] + line[alone[1]:end]):
        return len(line)        # no other numbers in it: what follows may be the path's
    return end


def _typed_numbers(m, line: str, code: list, taken: set, heads: dict) -> list:
    """Line numbers alone right after a project type's member (`T.addRows` (:1745),
    `` `T.refresh`:237 ``) that no path took: mentions of that type's file; a list right
    after (:10, :20) too."""
    out, head, last = [], None, 0
    for b in _BARE.finditer(line):
        if b.start() in taken:
            head, last = None, b.end()
            continue
        th, right = _type_between(m, line, max(last, b.start() - 160), b.start(), "", code)
        if isinstance(th, _TypeHead) and right and th.names:
            head = th
            heads[th.start()] = th.names
        elif th is not None or not re.fullmatch(r"[\s`,;()]*(?:and|y|e)?[\s`,(]*",
                                                line[last:b.start()]):
            head = None
        last = b.end()
        if head is not None:
            out.append(_Continuation(line, b, head))
    return out


def _group_close(line: str, pos: int, outside) -> int:
    inner = 0
    for k in range(pos, len(line)):
        if line[k] in "()" and outside(k):
            if line[k] == "(":
                inner += 1
            elif not inner:
                return k
            else:
                inner -= 1
    return len(line)


def _type_between(m, line: str, a: int, b: int, written: str, code: list):
    """The type line[a:b] names last for the `:N` at b, and whether that `:N` comes right
    after it: "same" (the path's own type), a _TypeHead (another project type, in one file,
    when the `:N` follows it closely: `Engine.run :587`, `X.m`:237, IPOForm (commit :226)),
    False (a library type, or a name several files declare) or None (none). A type further
    back is a call the doc describes (`Storage.get()` fresco (:792)), not the line's file."""
    def span_at(k: int):
        return next(((p, q) for p, q in code if p <= k < q), None)
    stem = os.path.basename(written)[:-len(".java")]
    out, close = None, False
    for t in _TYPE_WORD.finditer(line, a, b):
        sp = span_at(t.start())
        if sp and t.start() != sp[0] + 1:
            continue        # inside quoted code (`pm.get(Helper.id(o))`): not the subject
        name = t.group(1)
        before = line[max(0, t.start() - 12):t.start()]
        library = name not in m.types and m.is_library(name)
        if not t.group(2) and (_OBJECT_BEFORE.search(before) or
                               library and re.search(r"(?i)\b(?:de|del|of)\s+`?$", before)):
            continue        # "castea … a T (:592)", "el catch de E (:158)": not where it is
        gap = line[t.end():b]
        pure = re.fullmatch(r"[\s`()]*" if t.group(2) else r"[\s`(]*(?:[\w-]+\s+[\s`(]*)?",
                            gap) is not None
        if name == stem:
            out, close = "same", pure
            continue
        if _placeholder_type(name):
            continue
        if name in m.types:
            srcs = sorted({m.where[x][0] for x in m.types[name] if x in m.where})
            if any(os.path.basename(x) == os.path.basename(written) for x in srcs):
                out, close = "same", pure      # a nested type of the path's file
            elif pure:
                out, close = (_TypeHead(srcs[0], t.start(), name, t.group(2))
                              if len(srcs) == 1 else False), True
        elif m.is_library(name):
            out, close = False, pure
    return out, close


def _code_at(lines: list[str], code: str) -> list[int]:
    """The lines (1-based) where code, without spaces, starts; a statement over several lines
    too."""
    flat = [re.sub(r"\s+", "", x) for x in lines]
    starts, k = [], 0
    for x in flat:
        starts.append(k)
        k += len(x)
    joined = "".join(flat)
    out, at = [], joined.find(code)
    while at >= 0:
        out.append(bisect.bisect_right(starts, at))
        at = joined.find(code, at + 1)
    return sorted(set(out))


def _quote_after(line: str, mo, code: list) -> str | None:
    """The statement a doc quotes right after a line number, without spaces: the code span
    that follows it (`File:N` `code`, `File:N — code`, `File:N navega `code``), the next table
    cell's, or the text after the path's own span when spans nest (`File:N — `code` …`)."""
    own = next(((p, q) for p, q in code if p <= mo.start() < q), None)
    text = None
    if own and re.fullmatch(r"`?\s*(?:—|–|-|:)\s*`", line[mo.end():own[1]]):
        nxt = line.find("`", own[1])        # nested: `path — `code` …`
        text = line[own[1]:nxt] if nxt > own[1] else None
    if text is None:
        cell = next((x for x, _y in _neighbour_cells(line, mo.start()) if x > mo.start()), -1) \
            if line.lstrip().startswith("|") else -1
        start = own[1] if own else mo.end()
        text = next((line[p + 1:q - 1] for p, q in code if p >= start and (
            re.fullmatch(r"`?\s*(?:—|–|-|:|\(|=)?\s*(?:[^\s`|]+\s+)?", line[start:p]) or
            (cell >= 0 and re.fullmatch(r"\s*", line[cell:p])))), None)
    if text is None:
        start = own[0] if own else mo.start()
        text = next((line[p + 1:q - 1] for p, q in reversed(code) if q <= start and
                     re.fullmatch(r"\s*(?:en|in|at|de|of)?\s*\(?\s*`?", line[q:start])), None)
    if text is None:
        # "`a.b()` (A.java:12; idéntico en B.java:30)": the same code
        opened = line.rfind("(", 0, mo.start())
        if opened > 0 and re.search(r"\b(?:id[ée]ntic[oa]s?|igual|same|identical|también|also)\b",
                                    line[opened:mo.start()], re.I):
            text = next((line[p + 1:q - 1] for p, q in reversed(code) if q <= opened and
                         re.fullmatch(r"\s*", line[q:opened])), None)
    if text is None:
        return None
    x = re.sub(r"\s+|;$", "", re.split(r"…|\.\.\.", text)[0])      # up to an ellipsis
    if len(x) < 12 or not re.search(r"[\w$)]\.[\w$]+\(|=", x) or _PATH.search(x):
        return None
    return x


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
                text_cache: dict, strict: bool = False, need_all: set = frozenset(),
                follow: bool = False):
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
    # follow: lines a doc's older version names, now elsewhere: a call open there goes on
    last = _statement_end(lines, n, end_n) if follow else end_n
    span = lines[max(0, n - 3):min(len(lines), last + 2)]
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
    elif need_all & set(known):         # a list of names: each of them
        missing = [x for x in known if x in need_all and not (mentions(x) or contains(x))]
        if not missing:
            return None
        known = missing + [x for x in known if x not in missing]
    elif any(mentions(x) or contains(x) for x in known):
        return None
    x = known[0]
    where = []
    for y in members.get(x, [])[:1]:
        w = m.where.get(y)
        if w and S.kind_of(y) == "term":
            # a field: where it is declared, and the lines that use it (what a doc names)
            word = re.compile(r"(?<![\w$])" + re.escape(x) + r"(?![\w$])")
            used = [k + 1 for k, c in enumerate(lines) if x in c and k + 1 != w[1] and
                    word.search(_no_strings(c)) and not re.match(r"\s*(//|\*|/\*)", c)]
            where.append(f"{x} is declared at {w[1]}" + (
                f"; named at {', '.join(map(str, used[:4]))}{' …' if len(used) > 4 else ''}"
                if used else ""))
            return x, where
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
    if nxt:     # "… + tx.commit() en :1373": the last item is the next number's
        cut = re.search(r"(?:\s\+\s|,\s|\s(?:y|and|e)\s)[^+,]*?\s(?:en|at|in|on)\s*`?$", after)
        after = after[:cut.start()] if cut else after
    if re.match(r"`?\s*(?:—|–|-|:|\()", after):
        pieces.append(after[:40] if tight else after[:160])
    before = line[a:mo.start()]
    bm = re.search(r"(`[^`]+`)\s*\(\s*`?$", before)
    # "`code` (File:N lo deja en null…)": the parenthesis says what the line does
    prose = re.search(r"[^\W\d_]{2,}\s+[^\W\d_]{2,}", line[mo.end():].split(")", 1)[0])
    if bm and not (tight and prose):
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
            loose = False
            if k in (0, 3):
                head = segment[:m_.start(2 if k == 0 else 1)]
                if re.search(r"(?<![\w$])(?:sin|without|not?)\s+(?:\w+\s+)?`?(?:[\w$]+[.#])*$",
                             head, re.I):
                    continue        # "sin filtrar X (…)": not X's line
                # "→ X (File:N)", "de X (…)", "X (File:N fija …)": X may be what the line
                # does or what it belongs to; a name, but no claim that the doc was wrong
                rest = segment[m_.end():].split(")", 1)[0]
                loose = bool(re.search(r"(?:→|->)\s*`?(?:[\w$]+[.#])*$|(?<![\w$])(?:de|del|of|"
                                       r"por|by|from)\s+`?(?:[\w$]+[.#])*$", head, re.I) or
                             re.search(r"[^\W\d_]{2,}\s+[^\W\d_]{2,}", rest))
                # `a()` + `b()` + `c()` (File:N): the whole list ("y", "and" or a comma may
                # join clauses: "devuelve `a()` y `B.c` (File:N) …")
                while (lm := re.search(r"`([^`]+)`\s*\+\s*`?(?:[\w$]+[.#])*$", head)):
                    found = re.findall(r"([a-z_$][\w$]*)\s*\(", lm.group(1))
                    out.update("+" + x for x in found)
                    if found:
                        out.add("+" + name)     # the list's last one, too
                    head = head[:lm.start()]
            tail = segment[m_.start(1):m_.end(1) + 2] if k == 2 else ""
            if k == 2 and not (re.search(r"[a-z][A-Z]|[_$]", name) or
                               segment[max(0, m_.start(1) - 1):m_.start(1)] in ("`", ".", "#") or
                               re.match(r"[\w$]+\s*[(=]", tail)):
                if re.match(r"\s+[^\W\d_]{2,}\s+[^\W\d_]+", segment[m_.end(1):]):
                    # "(detach ya no cierra …)", "(form bindeado a …)": a sentence's subject
                    # may be the line's member, but no claim that the doc was wrong
                    out.add("~?" + name)
                    continue
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
                out.add("~" + name if loose and name[:1] != "?" else name)
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
    if written.startswith(("/", "~/")):
        # An absolute path (this checkout's, or another worktree's of the same repository):
        # the longest of its tails the checkout has, with a folder at least.
        parts = written.split("/")
        return next((by_suffix[t] for k in range(1, len(parts) - 1)
                     if (t := "/".join(parts[k:])) in by_suffix), [])
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
                not any(len(want) < len(h) and all(w in h or "Object" in h for w in want)
                        for h in have):
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
    """Of several problems for one mention, the one to show: what the code the doc quotes
    says, a new line number, then a hint of where the name is."""
    return 4 if "the code quoted" in p.what else 3 if p.kind == "line-shifted" else \
        2 if " is at " in p.what or " is declared at " in p.what else \
        1 if "called at" in p.what or "named at" in p.what else 0


def render(problems: list[Problem], checked: int, scope: Scope, offset: int, limit: int,
           fmt: str = "text") -> str:
    full = fmt == "md"
    if scope.error:
        return f"check_refs: {scope.error}\n" + scope_line(scope).split("\n")[0]
    nfiles = scope.read
    if not nfiles:
        why = ("nothing changed since then in scope or mentions the changed code"
               if scope.since else "no file in scope" if scope.how != "given" else
               "no file matches (files: globs or folders relative to the checkout)")
        return f"check_refs: {why}\n" + scope_line(scope, full)
    sline = scope_line(scope, full)
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
        return head + "\n" + sline
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
                cell = ", ".join(str(x) for x in lines)       # all of them: a table to use
                rows.append(f"| {f} | {cell} | `{_cut(key[1])}` | {key[2].replace('|', '/')} |")
        shown = rows[:2] + rows[2:][offset:offset + limit]
        more = offset + limit < len(rows) - 2
        return "\n".join([head, sline, ""] + shown +
                         ([f"\nnext offset {offset + limit}"] if more else []))
    for f in sorted(by_file):
        rows.append(f"{f} ({len(by_file[f])})")      # problems, as the head counts them
        for key in sorted(by_file[f], key=lambda k: merged[k][0]):
            lines = merged[key]
            at = f"line {lines[0]}" + (
                f" ×{len(lines)} (also {', '.join(map(str, lines[1:4]))}"
                f"{' …' if len(lines) > 4 else ''})" if len(lines) > 1 else "")
            rows.append(f"  {at}  {_cut(key[1])}  — {key[2]}")
    lines_out = [head, sline, "by file:"] + rows[offset:offset + limit]
    if offset + limit < len(rows):
        lines_out.append(f"next offset {offset + limit}")
    return "\n".join(lines_out)

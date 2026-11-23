"""Library sources: definitions, javadoc and members of JDK and dependency symbols, read on
demand from the `-sources.jar` files that Gradle and Maven keep in their caches and from the
JDK's `lib/src.zip`.

SCIP shards hold no definitions for external symbols. A small map from class file path to the
jars that contain it lives in the user cache (`libraries.sqlite`, shared by every checkout,
refreshed incrementally at most every few minutes and only when a query needs it). Only the
directory of each zip is read to build it; the one `.java` a query needs is read from the jar.
Which version a project uses is not in the shards: when several versions are cached the highest
is used, and the answer says which jar it came from.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
import zipfile
from dataclasses import dataclass

from . import symbols as S
from .cachedir import default_base, ensure_private_dir
from .javasrc import JavaSource, Member, enum_constant

REFRESH_SECONDS = 600


@dataclass
class LibClass:
    jar: str
    entry: str          # path inside the jar
    coordinates: str    # group:artifact:version, or "JDK <version>"
    others: int = 0     # other jars with the same class (other versions)


def default_roots() -> list[str]:
    """Gradle's module cache, Maven's local repository and the JDK's src.zip."""
    roots = []
    gradle = os.environ.get("GRADLE_USER_HOME") or os.path.join(os.path.expanduser("~"),
                                                                ".gradle")
    roots.append(os.path.join(gradle, "caches", "modules-2", "files-2.1"))
    roots.append(os.path.join(os.path.expanduser("~"), ".m2", "repository"))
    src_zip = jdk_src_zip()
    if src_zip:
        roots.append(src_zip)
    return roots


_TARGET_PATTERNS = [
    re.compile(r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)"),
    re.compile(r"(?:source|target)Compatibility\s*=\s*(?:JavaVersion\.VERSION_(?:1_)?(\d+)|"
               r"['\"]?(?:1\.)?(\d+)['\"]?)"),
    re.compile(r"\brelease(?:\.set\(|\s*=\s*)\s*(\d+)"),
    re.compile(r"<maven\.compiler\.(?:release|source)>\s*(?:1\.)?(\d+)\s*<"),
    re.compile(r"<release>\s*(\d+)\s*</release>"),
    re.compile(r"<java\.version>\s*(?:1\.)?(\d+)\s*<"),
]


def build_java_target(root: str) -> tuple[int | None, str]:
    """(Java version the build compiles for, an explicit JDK home from gradle.properties), read
    as text from the build files: JavaLanguageVersion.of(N), source/targetCompatibility,
    release, maven.compiler.release, java.version."""
    home = ""
    try:
        with open(os.path.join(root, "gradle.properties"), encoding="utf-8",
                  errors="replace") as fh:
            m = re.search(r"^\s*org\.gradle\.java\.home\s*=\s*(.+?)\s*$", fh.read(), re.M)
            home = m.group(1) if m else ""
    except OSError:
        pass
    files = ["build.gradle", "build.gradle.kts", "pom.xml"]
    for d in ("buildSrc", "gradle", "build-logic"):
        dd = os.path.join(root, d)
        for sub_root, _dirs, names in os.walk(dd) if os.path.isdir(dd) else ():
            files += [os.path.relpath(os.path.join(sub_root, n), root) for n in names
                      if n.endswith((".gradle", ".gradle.kts"))]
    try:
        for entry in sorted(os.listdir(root)):
            for n in ("build.gradle", "build.gradle.kts", "pom.xml"):
                if os.path.isfile(os.path.join(root, entry, n)):
                    files.append(os.path.join(entry, n))
    except OSError:
        pass
    versions: dict[int, int] = {}
    for f in files:
        try:
            with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for rx in _TARGET_PATTERNS:
            for m in rx.finditer(text):
                v = next((g for g in m.groups() if g), None)
                if v and 5 <= int(v) <= 40:
                    versions[int(v)] = versions.get(int(v), 0) + 1
    target = max(versions, key=lambda v: (versions[v], v)) if versions else None
    return target, home


def _jdk_homes() -> list[str]:
    home = os.path.expanduser("~")
    parents = [os.path.join(home, ".sdkman", "candidates", "java"),
               os.path.join(home, ".gradle", "jdks"), "/usr/lib/jvm",
               os.path.join(home, "Library", "Java", "JavaVirtualMachines"),
               "/Library/Java/JavaVirtualMachines", os.path.join(home, ".jdks")]
    out = []
    for p in parents:
        try:
            names = os.listdir(p)
        except OSError:
            continue
        for n in names:
            d = os.path.join(p, n)
            for cand in (d, os.path.join(d, "Contents", "Home")):
                if os.path.isfile(os.path.join(cand, "release")):
                    out.append(os.path.realpath(cand))
    for env in (os.environ.get("JAVA_HOME", ""),):
        if env and os.path.isfile(os.path.join(env, "release")):
            out.append(os.path.realpath(env))
    return sorted(set(out))


def _jdk_version(home: str) -> tuple[int, str]:
    try:
        with open(os.path.join(home, "release"), encoding="utf-8") as fh:
            m = re.search(r'JAVA_VERSION="([^"]+)"', fh.read())
    except OSError:
        return 0, ""
    if not m:
        return 0, ""
    v = m.group(1)
    major = v.split(".")[1] if v.startswith("1.") else v.split(".")[0]
    return (int(major) if major.isdigit() else 0), v


def _src_zip_of(home: str) -> str:
    for p in (os.path.join(home, "lib", "src.zip"), os.path.join(home, "src.zip")):
        if os.path.isfile(p):
            return p
    return ""


def jdk_for_project(root: str) -> tuple[str, str]:
    """(src.zip to use for JDK symbols, a note when it may not match the build)."""
    target, explicit = build_java_target(root)
    if explicit and _src_zip_of(explicit):
        return _src_zip_of(explicit), ""
    current = jdk_src_zip()
    if target is None:
        return current, ""
    best = None
    for home in _jdk_homes():
        major, full = _jdk_version(home)
        z = _src_zip_of(home)
        if major == target and z and (best is None or _version_key(":" + full) >
                                      _version_key(":" + best[1])):
            best = (z, full)
    if best:
        return best[0], ""
    if current:
        cur_home = os.path.dirname(os.path.dirname(os.path.realpath(current)))
        major, _full = _jdk_version(cur_home)
        if major and major != target:
            return current, (f"JDK {major} sources used; the build targets Java {target} and "
                             f"no JDK {target} with src.zip was found")
    return current, ""


def jdk_src_zip() -> str:
    homes = [os.environ.get("JAVA_HOME", "")]
    javac = shutil.which("javac") or shutil.which("java")
    if javac:
        homes.append(os.path.dirname(os.path.dirname(os.path.realpath(javac))))
    for home in homes:
        if home:
            p = os.path.join(home, "lib", "src.zip")
            if os.path.isfile(p):
                return p
    return ""


def _coordinates(path: str) -> str:
    parts = path.split(os.sep)
    if "files-2.1" in parts:  # …/files-2.1/<group>/<artifact>/<version>/<hash>/<file>
        k = parts.index("files-2.1")
        if len(parts) > k + 3:
            return f"{parts[k + 1]}:{parts[k + 2]}:{parts[k + 3]}"
    if "repository" in parts:  # …/repository/<g/r/o/u/p>/<artifact>/<version>/<file>
        k = parts.index("repository")
        rest = parts[k + 1:-1]
        if len(rest) >= 3:
            return f"{'.'.join(rest[:-2])}:{rest[-2]}:{rest[-1]}"
    if path.endswith("src.zip"):
        home = os.path.dirname(os.path.dirname(os.path.realpath(path)))
        version = ""
        try:
            with open(os.path.join(home, "release"), encoding="utf-8") as fh:
                m = re.search(r'JAVA_VERSION="([^"]+)"', fh.read())
                version = m.group(1) if m else ""
        except OSError:
            pass
        return "JDK " + (version or os.path.basename(home))
    return os.path.basename(path)


def _version_key(coordinates: str):
    version = coordinates.rsplit(":", 1)[-1]
    out = []
    for part in re.split(r"[.\-_+]", version):
        out.append((0, int(part)) if part.isdigit() else (1, part))
    return out


class LibraryIndex:
    def __init__(self, cache_base: str | None = None, roots: list[str] | None = None,
                 jdk_zip: str | None = None):
        base = ensure_private_dir(cache_base or default_base())
        self.path = os.path.join(base, "libraries.sqlite")
        self.roots = list(roots if roots is not None else default_roots())
        if jdk_zip:  # this project's JDK instead of the one on PATH
            self.roots = [r for r in self.roots if not r.endswith("src.zip")] + [jdk_zip]
        self.jdk_zip = jdk_zip or next((r for r in self.roots if r.endswith("src.zip")), "")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS jars (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
                mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, coordinates TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS classes (name TEXT NOT NULL, jar_id INTEGER NOT NULL,
                entry TEXT NOT NULL, PRIMARY KEY (name, jar_id)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS classes_jar ON classes(jar_id);
        """)
        return con

    def _jars_on_disk(self) -> dict[str, tuple[int, int]]:
        out = {}
        for root in self.roots:
            if os.path.isfile(root):
                st = os.stat(root)
                out[root] = (st.st_mtime_ns, st.st_size)
                continue
            for d, _dirs, files in os.walk(root):
                for n in files:
                    if n.endswith("-sources.jar"):
                        p = os.path.join(d, n)
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        out[p] = (st.st_mtime_ns, st.st_size)
        return out

    def refresh(self, force: bool = False) -> int:
        """Updates the class map if it is older than REFRESH_SECONDS. Returns jars (re)read."""
        con = self._connect()
        try:
            last = con.execute("SELECT value FROM meta WHERE key='scanned_at'").fetchone()
            roots_key = "\n".join(self.roots)
            same_roots = con.execute("SELECT value FROM meta WHERE key='roots'").fetchone()
            if not force and last and same_roots and same_roots[0] == roots_key and \
                    time.time() - float(last[0]) < REFRESH_SECONDS:
                return 0
            known = {p: jid for jid, p in con.execute("SELECT id, path FROM jars")}
            disk = self._jars_on_disk()
            for p, jid in known.items():
                if p not in disk and not os.path.exists(p):
                    con.execute("DELETE FROM classes WHERE jar_id=?", (jid,))
                    con.execute("DELETE FROM jars WHERE id=?", (jid,))
            n = self._index_jars(con, disk)
            con.execute("INSERT OR REPLACE INTO meta VALUES ('scanned_at', ?)",
                        (str(time.time()),))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('roots', ?)", (roots_key,))
            con.commit()
            return n
        finally:
            con.close()

    def _index_jars(self, con: sqlite3.Connection, jars: dict[str, tuple[int, int]]) -> int:
        known = {p: (jid, mt, size) for jid, p, mt, size in con.execute(
            "SELECT id, path, mtime_ns, size FROM jars")}
        n = 0
        for p, (mt, size) in jars.items():
            old = known.get(p)
            if old is not None and old[1:] == (mt, size):
                continue
            if old is not None:
                con.execute("DELETE FROM classes WHERE jar_id=?", (old[0],))
                con.execute("DELETE FROM jars WHERE id=?", (old[0],))
            try:
                with zipfile.ZipFile(p) as z:
                    names = [i.filename for i in z.infolist() if i.filename.endswith(".java")]
            except (OSError, zipfile.BadZipFile):
                continue
            jid = con.execute("INSERT INTO jars (path, mtime_ns, size, coordinates) "
                              "VALUES (?, ?, ?, ?)", (p, mt, size, _coordinates(p))).lastrowid
            rows = []
            for entry in names:
                name = entry[:-5]
                if p.endswith("src.zip") and "/" in name:
                    name = name.split("/", 1)[1]  # java.base/java/lang/String -> java/lang/…
                rows.append((name, jid, entry))
            con.executemany("INSERT OR IGNORE INTO classes VALUES (?, ?, ?)", rows)
            n += 1
        con.commit()
        return n

    def _likely_jars(self, class_path: str) -> dict[str, tuple[int, int]]:
        """Jars that probably hold the class without listing every jar: the JDK for java/ and
        javax/, and the cached artifacts whose group is a prefix of the package (or the other
        way around), e.g. com.vaadin.flow.component.grid -> group com.vaadin."""
        pkg = class_path.rsplit("/", 1)[0].replace("/", ".")
        out = {}
        for root in self.roots:
            if os.path.isfile(root):
                if class_path.startswith(("java/", "javax/", "jdk/", "sun/", "com/sun/")):
                    st = os.stat(root)
                    out[root] = (st.st_mtime_ns, st.st_size)
                continue
            try:
                groups = os.listdir(root)
            except OSError:
                continue
            if root.endswith("files-2.1"):
                cands = [g for g in groups if pkg == g or pkg.startswith(g + ".")
                         or g.startswith(pkg + ".")]
                dirs = [os.path.join(root, g) for g in cands]
            else:  # Maven: the group is a directory path
                parts = pkg.split(".")
                dirs = []
                for k in range(len(parts), 1, -1):
                    d = os.path.join(root, *parts[:k])
                    if os.path.isdir(d):
                        dirs.append(d)
                        break
            for d in dirs:
                for dd, _ds, files in os.walk(d):
                    for n in files:
                        if n.endswith("-sources.jar"):
                            p = os.path.join(dd, n)
                            try:
                                st = os.stat(p)
                            except OSError:
                                continue
                            out[p] = (st.st_mtime_ns, st.st_size)
        return out

    def _query(self, con: sqlite3.Connection, class_path: str):
        return list(con.execute(
            "SELECT j.path, c.entry, j.coordinates FROM classes c JOIN jars j "
            "ON j.id = c.jar_id WHERE c.name = ?", (class_path,)))

    def find_class(self, class_path: str) -> LibClass | None:
        """The jar holding `com/example/Foo` (highest version first). Looks at the likely jars
        first; lists every cached jar only when those do not have it."""
        con = self._connect()
        try:
            rows = self._query(con, class_path)
            if not rows:
                self._index_jars(con, self._likely_jars(class_path))
                rows = self._query(con, class_path)
        finally:
            con.close()
        if not rows:
            self.refresh()
            con = self._connect()
            try:
                rows = self._query(con, class_path)
            finally:
                con.close()
        # Only this project's JDK among the JDK source zips the shared map knows.
        rows = [r for r in rows if not r[0].endswith("src.zip") or r[0] == self.jdk_zip]
        if not rows:
            return None
        rows.sort(key=lambda r: (not r[0].endswith("src.zip"), _version_key(r[2])), reverse=True)
        jar, entry, coords = rows[0]
        return LibClass(jar, entry, coords, len(rows) - 1)

    def stats(self) -> dict:
        con = self._connect()
        try:
            return {"jars": con.execute("SELECT count(*) FROM jars").fetchone()[0],
                    "classes": con.execute("SELECT count(*) FROM classes").fetchone()[0],
                    "path": self.path}
        finally:
            con.close()


@dataclass
class LibDefinition:
    symbol: str
    lib: LibClass
    member: Member | None       # None: the type itself
    type_declaration: str
    type_javadoc: str
    source: JavaSource
    overloads: list[Member]     # same-name methods (for methods)
    body: tuple[int, int]       # lines of the member (or type)
    note: str = ""              # e.g. the JDK used does not match the build's target


def class_path_of(symbol: str) -> tuple[str, list[str]]:
    """("com/example/Foo", ["Foo", "Inner"]) for com/example/Foo#Inner#bar()."""
    d = S.parse(symbol)
    pkg = [x.name for x in d if x.suffix == S.NAMESPACE]
    types = []
    for x in d:
        if x.suffix == S.TYPE:
            types.append(x.name)
        elif types and x.suffix != S.NAMESPACE:
            break
    if not types:
        return "", []
    return "/".join(pkg + [types[0]]), types


def lookup(index: LibraryIndex, symbol: str) -> LibDefinition | None:
    """The library definition of an external symbol, read from its sources jar."""
    class_path, types = class_path_of(symbol)
    if not class_path:
        return None
    lib = index.find_class(class_path)
    if lib is None:
        return None
    try:
        with zipfile.ZipFile(lib.jar) as z:
            text = z.read(lib.entry).decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    src = JavaSource(text)
    decl = src.find_type(types)
    if decl is None:
        return None
    start, body_open, body_close = decl
    type_decl = " ".join(src.text[start:body_open].split())
    type_doc = _javadoc_before(src, start)
    last = S.parse(symbol)[-1]
    members = src.members(body_open, body_close, is_enum=" enum " in f" {type_decl} ",
                          type_name=types[-1])
    if last.suffix == S.TYPE:
        return LibDefinition(symbol, lib, None, type_decl, type_doc, src, [],
                             (src.line_of(start), src.line_of(body_close)))
    name = last.name
    if last.suffix == S.METHOD:
        want_kind = "constructor" if name == S.CONSTRUCTOR else "method"
        same = [m for m in members if m.kind == want_kind and
                (m.name == name or want_kind == "constructor")]
        # SCIP numbers overloads in declaration order: (), (+1), (+2)…
        k = 0
        if last.disambiguator.startswith("+") and last.disambiguator[1:].isdigit():
            k = int(last.disambiguator[1:])
        member = same[k] if k < len(same) else (same[0] if same else None)
        if member is None:
            return None
        return LibDefinition(symbol, lib, member, type_decl, type_doc, src, same,
                             (member.start_line, member.end_line))
    member = next((m for m in members if m.name == name and m.kind in ("field", "type")), None)
    if member is None and name:
        consts = next((m for m in members if m.kind == "enum constants"), None)
        if consts is not None:
            seg_lo = body_open + 1  # from the enum's brace: the first constant's javadoc too
            seg_hi = src._line_starts[min(consts.end_line, len(src._line_starts) - 1)]
            hit = enum_constant(src.text[seg_lo:seg_hi], name)
            if hit:
                decl, doc, off = hit
                line = src.line_of(seg_lo + off)
                member = Member("enum constant", name, decl, line, line, doc)
    if member is None:
        return None
    return LibDefinition(symbol, lib, member, type_decl, type_doc, src, [],
                         (member.start_line, member.end_line))


def type_members(index: LibraryIndex, type_symbol: str) -> tuple[LibClass, list[Member]] | None:
    """Non-private members of a library type."""
    class_path, types = class_path_of(type_symbol)
    lib = index.find_class(class_path) if class_path else None
    if lib is None:
        return None
    try:
        with zipfile.ZipFile(lib.jar) as z:
            text = z.read(lib.entry).decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    src = JavaSource(text)
    decl = src.find_type(types)
    if decl is None:
        return None
    start, body_open, body_close = decl
    head = src.text[start:body_open]
    is_interface = re.search(r"\binterface\b", head) is not None
    out = []
    for m in src.members(body_open, body_close, is_enum=re.search(r"\benum\b", head) is not None,
                         type_name=types[-1]):
        if m.kind == "enum constants" or "private" in m.modifiers:
            continue
        if not is_interface and not ({"public", "protected"} & set(m.modifiers)):
            continue  # package-private: not visible to a subclass elsewhere
        out.append(m)
    return lib, out


def _javadoc_before(src: JavaSource, pos: int) -> str:
    from .javasrc import _javadoc

    # The javadoc sits before the type's annotations: look back over a generous window.
    lo = max(0, pos - 20000)
    prefix = src.text[lo:pos]
    end = prefix.rfind("*/")
    if end < 0 or prefix[end + 2:].strip() and not prefix[end + 2:].lstrip().startswith("@"):
        return ""
    return _javadoc(prefix)


# --- library type hierarchy -------------------------------------------------------------------

JAVA_LANG_TYPES = {
    "Object", "String", "Comparable", "Iterable", "Runnable", "AutoCloseable", "Cloneable",
    "CharSequence", "Enum", "Record", "Number", "Integer", "Long", "Short", "Byte", "Double",
    "Float", "Boolean", "Character", "Throwable", "Exception", "RuntimeException", "Error",
    "Thread", "ThreadLocal", "InheritableThreadLocal", "ClassLoader", "Appendable", "Readable",
    "Deprecated", "Override", "FunctionalInterface", "SafeVarargs", "SuppressWarnings",
    "IllegalArgumentException", "IllegalStateException", "UnsupportedOperationException",
    "IndexOutOfBoundsException", "NullPointerException", "ClassCastException",
    "ArithmeticException", "InterruptedException", "CloneNotSupportedException",
    "ReflectiveOperationException", "SecurityException", "StackOverflowError",
    "OutOfMemoryError", "AssertionError", "LinkageError", "VirtualMachineError",
}

_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;", re.M)
_PACKAGE_DECL = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)


def _declared_names(declaration: str) -> list[str]:
    """Type names after extends/implements in a declaration (generics stripped)."""
    out = []
    for kw in ("extends", "implements"):
        m = re.search(r"\b" + kw + r"\b(.*?)(?=\b(?:extends|implements|permits)\b|$)", declaration)
        if not m:
            continue
        depth, cur = 0, ""
        for ch in m.group(1) + ",":
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif ch == "," and depth == 0:
                name = cur.strip()
                if name:
                    out.append(name)
                cur = ""
                continue
            if depth == 0 and ch not in "<>":
                cur += ch
    return out


def library_supertypes(index: LibraryIndex, type_symbol: str) -> list[str]:
    """Direct supertypes of a library type, as SCIP symbols, from its declaration in the sources
    jar (simple names resolved through the file's imports, its package and java.lang)."""
    class_path, types = class_path_of(type_symbol)
    lib = index.find_class(class_path) if class_path else None
    if lib is None:
        return []
    con = index._connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS supertypes (symbol TEXT PRIMARY KEY, "
                    "jar TEXT NOT NULL, supers TEXT NOT NULL) WITHOUT ROWID")
        row = con.execute("SELECT jar, supers FROM supertypes WHERE symbol = ?",
                          (type_symbol,)).fetchone()
        if row and row[0] == lib.jar:
            return [x for x in row[1].split(" ") if x]
        try:
            with zipfile.ZipFile(lib.jar) as z:
                text = z.read(lib.entry).decode("utf-8", errors="replace")
        except (OSError, KeyError, zipfile.BadZipFile):
            return []
        src = JavaSource(text)
        decl = src.find_type(types)
        if decl is None:
            return []
        header = " ".join(src.masked[decl[0]:decl[1]].split())
        header = re.sub(r"@\s*[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?", " ", header)
        pkg_m = _PACKAGE_DECL.search(src.masked)
        pkg = pkg_m.group(1) if pkg_m else ""
        single = {}
        wildcards = []
        for m in _IMPORT.finditer(src.masked):
            if m.group(1):
                continue
            if m.group(3):
                wildcards.append(m.group(2))
            else:
                single[m.group(2).rsplit(".", 1)[-1]] = m.group(2)
        supers = []
        for name in _declared_names(header):
            supers.append(_resolve_type_name(index, name, pkg, single, wildcards, types))
        supers = [x for x in supers if x]
        con.execute("INSERT OR REPLACE INTO supertypes VALUES (?, ?, ?)",
                    (type_symbol, lib.jar, " ".join(supers)))
        con.commit()
        return supers
    finally:
        con.close()


def _resolve_type_name(index: LibraryIndex, name: str, pkg: str, single: dict,
                       wildcards: list[str], enclosing: list[str]) -> str:
    parts = name.split(".")
    head = parts[0]
    if head in single:  # imported (maybe a nested type: Map.Entry)
        base = single[head]
        return base.replace(".", "/").rsplit("/", 0)[0] + "#" + "".join(p + "#" for p in parts[1:])
    if len(parts) > 1 and parts[0][:1].islower():  # fully qualified
        pkg_parts = [p for p in parts if p[:1].islower()]
        type_parts = parts[len(pkg_parts):]
        return "/".join(pkg_parts) + "/" + "#".join(type_parts) + "#"

    def exists(class_path: str) -> bool:
        return index.find_class(class_path) is not None

    candidates = []
    if pkg:
        candidates.append(pkg.replace(".", "/") + "/" + head)
    candidates += [w.replace(".", "/") + "/" + head for w in wildcards]
    candidates.append("java/lang/" + head)
    for cp in candidates:
        if exists(cp):
            return cp + "#" + "".join(p + "#" for p in parts[1:])
    # Not found in any cached jar: java.lang is implicit, else a guess in the same package.
    if head in JAVA_LANG_TYPES:
        return "java/lang/" + head + "#" + "".join(p + "#" for p in parts[1:])
    if pkg:
        return pkg.replace(".", "/") + "/" + head + "#" + "".join(p + "#" for p in parts[1:])
    return ""


def library_ancestors(index: LibraryIndex, type_symbol: str, limit: int = 200) -> list[str]:
    """All library supertypes, breadth first (nearest first)."""
    out, seen, queue = [], {type_symbol}, [type_symbol]
    while queue and len(out) < limit:
        cur = queue.pop(0)
        for sup in library_supertypes(index, cur):
            if sup not in seen:
                seen.add(sup)
                out.append(sup)
                queue.append(sup)
    return out


def param_count(declaration: str) -> int:
    """Number of parameters of a method/constructor declaration (0 for fields)."""
    k = declaration.find("(")
    if k < 0:
        return -1
    depth, count, seen = 0, 0, False
    for ch in declaration[k + 1:]:
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            if depth == 0 and ch == ")":
                break
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
        elif not ch.isspace():
            seen = True
    return count + 1 if seen else 0


def ordered_members(index: LibraryIndex, types: list[str], skip_names: set[str] = frozenset()
                    ) -> list[tuple[str, LibClass | None, list[Member], int]]:
    """[(type, lib, members, hidden)] for library types in order (nearest first), keeping a
    member only in the most specific type that declares it (same name and parameter count);
    `hidden` counts those left out because a nearer type overrides them."""
    seen: set[tuple[str, int]] = set()
    out = []
    for t in types:
        got = type_members(index, t)
        if got is None:
            out.append((t, None, [], 0))
            continue
        lib, mems = got
        kept, hidden = [], 0
        for m in mems:
            key = (m.name, param_count(m.declaration))
            if m.name in skip_names or (m.kind != "constructor" and key in seen):
                hidden += 1
                continue
            if m.kind != "constructor":
                seen.add(key)
            kept.append(m)
        out.append((t, lib, kept, hidden))
    return out

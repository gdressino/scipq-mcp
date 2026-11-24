"""doctor: what could make the index wrong or the tools weak in this checkout, as a checklist.

Everything here reads: files, mtimes, /proc, the cache. Nothing is built or changed; each
finding says what to do.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import __version__
from . import cachedir
from . import jvm
from . import libsrc as L
from . import setup as SETUP
from .workspace import SUFFIX, Workspace, build_processes

CLASS_SLACK = 120.0   # seconds a class file may be newer than its task's newest shard


@dataclass
class Finding:
    level: str        # "ok", "warn", "info"
    area: str
    text: str
    hint: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, area: str, text: str, hint: str = "") -> None:
        self.findings.append(Finding(level, area, text, hint))


def _git_java(root: str) -> list[str] | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run([git, "-C", root, "-c", "core.quotePath=false", "ls-files", "-z",
                               "--", "*.java"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def _source_set(task: str) -> str:
    m = re.fullmatch(r"compile(\w*)Java", task)
    if not m:
        return ""
    return m.group(1)[:1].lower() + m.group(1)[1:] if m.group(1) else "main"


def _newest(paths) -> float:
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, os.stat(p).st_mtime)
        except OSError:
            pass
    return newest


def _class_dirs(root: str, module: str, sset: str) -> list[str]:
    mod_dir = os.path.join(root, module) if module else root
    return [d for d in (os.path.join(mod_dir, "build", "classes", "java", sset),
                        os.path.join(mod_dir, "target",
                                     "classes" if sset == "main" else "test-classes"))
            if os.path.isdir(d)]


def _newest_class(d: str, cap: int = 200000) -> float:
    newest, n = 0.0, 0
    for dirpath, _dirs, names in os.walk(d):
        for name in names:
            if name.endswith(".class"):
                n += 1
                try:
                    newest = max(newest, os.stat(os.path.join(dirpath, name)).st_mtime)
                except OSError:
                    pass
                if n >= cap:
                    return newest
    return newest


_TYPE_DECL = re.compile(r"\b(class|interface|enum|record)\s+[A-Za-z_$]")
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*|\"(?:[^\"\\\n]|\\.)*\"", re.S)


def _declares_type(root: str, source: str) -> bool:
    """Whether a source declares a type outside comments (a file commented out entirely
    compiles to nothing)."""
    try:
        with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
            text = fh.read(400000)
    except OSError:
        return True
    return bool(_TYPE_DECL.search(_COMMENTS.sub(" ", text)))


def _no_class_expected(root: str, source: str) -> bool:
    """package-info and module-info (a class only with annotations), or no type at all."""
    name = os.path.basename(source)
    if name == "module-info.java":
        return True
    if name == "package-info.java":
        try:
            with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
                head = _COMMENTS.sub(" ", fh.read(20000)).split("package", 1)[0]
        except OSError:
            return True
        return "@" not in head
    return not _declares_type(root, source)


_EXCLUDE_CALL = re.compile(r"""\bexclude\s*\(?\s*['"]([^'"]+\.java|[^'"]*\*[^'"]*)['"]""")


def _build_excludes(root: str) -> list[str]:
    """Source patterns build files exclude (`exclude("a/B.java")`, `exclude '**/Gen*'`)."""
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in
                   ("build", "target", "node_modules", "src", "out", "bin")]
        if d.count(os.sep) - root.count(os.sep) > 3:
            dirs[:] = []
        for n in names:
            if n in ("build.gradle", "build.gradle.kts"):
                try:
                    with open(os.path.join(d, n), encoding="utf-8", errors="replace") as fh:
                        out += _EXCLUDE_CALL.findall(fh.read())
                except OSError:
                    pass
    return out


def _excluded(path: str, pattern: str) -> bool:
    """An Ant-style pattern relative to a source folder, against a checkout path."""
    rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*") \
        .replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.search(r"(?:^|/)" + rx + "$", path) is not None


def _declares_shard_outputs(path: str) -> bool:
    """An init script that declares the shards' folder as an output of the compile tasks
    (`c.outputs.dir(targetroot)`): the build cache then stores and restores the shards with
    the classes (and its entries are not those of a build without the plugin, since the
    output properties are part of the cache key)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    code = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    return re.search(r"\boutputs\s*\.\s*dirs?\s*\(", code) is not None


def _package(root: str, source: str) -> str:
    try:
        with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
            m = re.search(r"^\s*package\s+([\w.]+)\s*;", fh.read(20000), re.M)
    except OSError:
        return ""
    return m.group(1) if m else ""


def run(ws: Workspace, settings, cache_base: str | None, freshness) -> Report:
    r = Report()
    root = ws.root

    # The server itself.
    here = os.path.dirname(os.path.abspath(__file__))
    r.add("info", "scipq-mcp", f"version {__version__}, Python {sys.version.split()[0]} "
          f"({sys.executable}); package at {here}",
          "an MCP client keeps the server it started: restart it after an upgrade")

    # How the shards are produced.
    d = SETUP.diagnose(root, os.path.relpath(ws.shards_dir, root))
    if d.build == "gradle":
        applying = [p for p, a in d.init_scripts if a.startswith("yes")]
        unknown = [p for p, a in d.init_scripts if a.startswith("unknown")]
        fresh = freshness is not None and not (freshness.stale or freshness.uncompiled)
        if applying:
            r.add("ok", "build", f"Gradle, init script for the shards (applies to "
                  f"{d.project_name!r}): {', '.join(applying[:2])}")
        elif unknown and ws.files() and fresh:
            r.add("ok", "build", f"Gradle, init script {unknown[0]} (a custom script: it "
                  "probably applies, the shards are up to date)")
            unknown = unknown[1:]
        for p, a in d.init_scripts:
            if a.startswith("yes") or (a.startswith("unknown") and p not in unknown):
                continue
            r.add("info" if applying or ws.files() else "warn", "build",
                  f"init script {p}: {a}",
                  "" if applying or ws.files() else "setup shows how to include this project")
        if not d.init_scripts:
            r.add("warn", "build", "Gradle without a scip init script in ~/.gradle/init.d "
                  "(the shards may come from the build itself, or be left over)",
                  "setup shows the init script")
    elif d.build == "maven":
        r.add("info", "build", "Maven: the compiler plugin must run scip-javac (setup shows "
              "the snippet)")
    tasks = [m for _r, m in ws.scip_roots()]
    files = ws.files()
    r.add("ok" if files else "warn", "shards", f"{len(files)} shards in {len(tasks)} compile "
          "tasks" + (f": {', '.join(tasks[:8])}{' …' if len(tasks) > 8 else ''}" if tasks
                     else ""))
    try:
        with open(os.path.join(root, "gradle.properties"), encoding="utf-8",
                  errors="replace") as fh:
            props = fh.read()
    except OSError:
        props = ""
    if re.search(r"^\s*org\.gradle\.caching\s*=\s*true", props, re.M):
        scripts = [p for p, a in d.init_scripts if a.startswith(("yes", "unknown"))] \
            if d.build == "gradle" else []
        if scripts and all(_declares_shard_outputs(p) for p in scripts):
            r.add("info", "build", "the Gradle build cache is on (org.gradle.caching=true): the "
                  "init script declares the shards as outputs of the compile tasks, so a task "
                  "restored FROM-CACHE restores its shards with its classes")
        else:
            r.add("warn", "build", "the Gradle build cache is on (org.gradle.caching=true): a "
                  "task restored FROM-CACHE does not run javac, so its shards are not "
                  "rewritten", "when files stay stale after a build, run that task with "
                  "--rerun; or declare the shards' folder as the task's output in the init "
                  "script (`c.outputs.dir(targetroot)`, as `scipq-mcp gradle-init` does)")

    # Tracked sources the index does not see.
    shard_sources = {rel[:-len(SUFFIX)] for _k, rel in files}
    shard_dirs = {s.rpartition("/")[0] for s in shard_sources}
    tracked = _git_java(root)
    if tracked is None:
        r.add("info", "coverage", "not a git repository: tracked files without a shard are not "
              "checked")
    else:
        missing = [p for p in tracked if p not in shard_sources]
        # Nothing to compile: commented out entirely, or package-info/module-info without
        # annotations (checked up to a bound: reading every file of a huge tree is slow).
        nothing = {p for p in missing[:3000] if _no_class_expected(root, p)}
        missing = [p for p in missing if p not in nothing]
        excluded = _build_excludes(root)
        by_build = [p for p in missing if any(_excluded(p, x) for x in excluded)]
        missing = [p for p in missing if p not in set(by_build)]
        if by_build:
            r.add("info", "coverage", f"{len(by_build)} tracked .java files are excluded by the "
                  f"build (exclude(…) in a build file): {', '.join(by_build[:3])}"
                  + (" …" if len(by_build) > 3 else ""))
        if nothing:
            r.add("info", "coverage", f"{len(nothing)} tracked .java files declare nothing to "
                  "compile (commented out, or package-info/module-info without annotations): "
                  "no shard expected")
        in_compiled = [p for p in missing if p.rpartition("/")[0] in shard_dirs]
        outside = [p for p in missing if p.rpartition("/")[0] not in shard_dirs]
        if not missing:
            r.add("ok", "coverage", f"all {len(tracked)} tracked .java files have a shard")
        if in_compiled:
            r.add("warn", "coverage", f"{len(in_compiled)} tracked .java files sit next to "
                  "compiled ones but have no shard (new since the last build, excluded by "
                  f"the build, or failing): {', '.join(in_compiled[:4])}"
                  + (" …" if len(in_compiled) > 4 else ""))
        if outside:
            tops: dict[str, int] = {}
            for p in outside:
                top = "/".join(p.split("/")[:2]) if "/" in p else "."
                tops[top] = tops.get(top, 0) + 1
            listed = ", ".join(f"{k} ({v})" for k, v in sorted(tops.items(),
                                                               key=lambda kv: -kv[1])[:6])
            r.add("info", "coverage", f"{len(outside)} tracked .java files in folders nothing "
                  f"compiles with the plugin (not a source set, or a module built without it): "
                  f"{listed}", "if one of them is code, add the plugin to that module's build")

    # Classes compiled after the shards (an IDE build, or the plugin turned off), and shards
    # without classes (javac failed after the plugin wrote them).
    by_task: dict[str, list[str]] = {}
    for k, rel in files:
        by_task.setdefault(ws.scip_roots()[k][1], []).append(
            os.path.join(ws.scip_roots()[k][0], rel))
    for task, shard_paths in sorted(by_task.items()):
        module, _, t = task.rpartition("/")
        sset = _source_set(t)
        if not sset:
            continue
        dirs = _class_dirs(root, module, sset)
        if not dirs:
            continue
        newest_shard = _newest(shard_paths)
        newest_class = max(_newest_class(x) for x in dirs)
        if newest_class > newest_shard + CLASS_SLACK:
            ago = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest_class))
            r.add("warn", "plugin", f"{task}: classes compiled at {ago}, after its newest shard "
                  f"({int((newest_class - newest_shard) / 60)} min later): built without the "
                  "plugin (an IDE build, -Pscip=false?)",
                  f"rebuild {task} with the plugin; until then the index is older than the code")
    no_class = 0
    lacking: list[str] = []
    sample = [(k, rel) for k, rel in files][:20000]
    for k, rel in sample:
        task = ws.scip_roots()[k][1]
        module, _, t = task.rpartition("/")
        sset = _source_set(t)
        dirs = _class_dirs(root, module, sset) if sset else []
        if not dirs:
            continue
        source = rel[:-len(SUFFIX)]
        if not ws.source_exists(source):
            continue
        pkg = _package(root, source) if no_class < 50 else ""
        cls = (pkg.replace(".", "/") + "/" if pkg else "") + \
            os.path.basename(source)[:-len(".java")] + ".class"
        if not any(os.path.exists(os.path.join(x, cls)) for x in dirs):
            if _no_class_expected(root, source):
                continue
            no_class += 1
            if len(lacking) < 5:
                lacking.append(f"{source} ({task})")
            if no_class >= 50:
                break
    if no_class:
        r.add("warn", "plugin", f"{'50+' if no_class >= 50 else no_class} shards have no class "
              "file: javac failed after the plugin wrote them, or the classes were cleaned: "
              + ", ".join(lacking) + (" …" if no_class > len(lacking) else ""),
              "look at the build output; refactor verify reports these too")

    # Freshness and builds running now.
    if freshness is not None:
        if freshness.stale or freshness.uncompiled:
            r.add("warn", "freshness", f"{len(freshness.stale)} stale and "
                  f"{len(freshness.uncompiled)} not compiled since their module's last build",
                  "recompile: " + ", ".join(sorted(t for t in freshness.tasks if t)[:6]))
        else:
            r.add("ok", "freshness", "no source is newer than its shard"
                  + (f" ({freshness.same_content} newer but with the content compiled)"
                     if freshness.same_content else ""))
    procs = build_processes(root)
    if procs is None:
        r.add("info", "builds", "cannot tell running builds on this system")
    elif procs[0]:
        r.add("warn", "builds", "a build runs in this checkout: " + "; ".join(
            f"pid {pid}: {cmd[:120]}" for pid, cmd in procs[0][:3]),
            "results may be incomplete until it finishes; refactor refuses meanwhile")
    else:
        r.add("ok", "builds", "no gradle/mvn command runs in this checkout (IDE builds through "
              "a daemon are not seen)")

    # The cache.
    base = cache_base or cachedir.default_base()
    entries = cachedir.entries(base)
    total = sum(e["bytes"] for e in entries)
    mine = [e for e in entries if e["root"] == os.path.realpath(root)]
    gone = [e for e in entries if e["root"] and not e["exists"]]
    r.add("info", "cache", f"{base}: {len(entries)} indexes, {total / 1e6:.0f} MB"
          + (f"; this checkout's {mine[0]['bytes'] / 1e6:.0f} MB" if mine else
             "; none for this checkout yet (built on first use)"))
    empty = cachedir.empty_entries(base)
    if empty:
        r.add("warn", "cache", f"{len(empty)} cache entries without an index (a first build "
              "interrupted): " + ", ".join(os.path.basename(x) for x in empty[:3])
              + (" …" if len(empty) > 3 else ""), "scipq-mcp cache prune")
    if gone:
        r.add("warn", "cache", f"{len(gone)} indexes of checkouts that no longer exist ("
              f"{sum(e['bytes'] for e in gone) / 1e6:.0f} MB): "
              + ", ".join(e["root"] for e in gone[:3]) + (" …" if len(gone) > 3 else ""),
              "scipq-mcp cache prune")

    # Configuration.
    if settings is not None:
        src = getattr(settings, "source", "") or "built-in defaults"
        r.add("info", "config", f"profiles: {', '.join(settings.profiles) or 'none'} ({src})")
        for w in getattr(settings, "warnings", [])[:10]:
            r.add("warn", "config", w)

    # Library and JDK sources.
    target, explicit = L.build_java_target(root)
    zip_path, note = L.jdk_for_project(root)
    java = shutil.which("java")
    java_home = os.path.dirname(os.path.dirname(os.path.realpath(java))) if java else ""
    sys_major = L._jdk_version(java_home)[0] if java_home else 0
    r.add("warn" if note else "info", "libraries",
          f"the build targets Java {target or '?'}"
          + (f" (org.gradle.java.home {explicit})" if explicit else "")
          + f"; JDK sources: {zip_path or 'none found'}"
          + (f"; the java on PATH is {sys_major}" if sys_major else "") + (f" — {note}" if note
                                                                         else ""))

    # Refactoring.
    level = jvm.language_level(root)
    jars = [j for j in jvm.find_jars(cache_base) if jvm._trusted(j)]
    need = _javaparser_for(level) if level else ""
    old = bool(jars and need and jvm._version_key(jars[0].version) < jvm._version_key(need))
    for line in jvm.status(cache_base):
        bad = "not found" in line or "needs a JDK" in line
        if old and line.startswith("JavaParser"):
            r.add("warn", "refactor", f"{line} — it does not know "
                  f"{level.replace('JAVA_', 'Java ')}; it needs {need}+",
                  "scipq-mcp tools install javaparser")
            continue
        r.add("warn" if bad else "ok", "refactor", line,
              jvm.install_hint().split("\n")[1].strip() if "JavaParser: not found" in line
              else "")
    return r


def _javaparser_for(level: str) -> str:
    """The JavaParser that surely parses a language level: up to Java 21 any recent one; 22
    to 25 with 3.28.0; 26 with 3.28.2."""
    m = re.fullmatch(r"JAVA_(\d+)", level)
    if not m:
        return ""
    v = int(m.group(1))
    return "3.28.2" if v >= 26 else "3.28.0" if v >= 22 else ""


def render(r: Report) -> str:
    marks = {"ok": "OK  ", "warn": "WARN", "info": "    "}
    order = {"warn": 0, "ok": 1, "info": 2}
    warns = sum(1 for f in r.findings if f.level == "warn")
    lines = [f"doctor: {warns} warning{'s' if warns != 1 else ''}" if warns else
             "doctor: nothing to fix"]
    for f in sorted(r.findings, key=lambda f: order[f.level] if f.level == "warn" else 1):
        lines.append(f"{marks[f.level]} [{f.area}] {f.text}")
        if f.hint:
            lines.append(f"       → {f.hint}")
    return "\n".join(lines)

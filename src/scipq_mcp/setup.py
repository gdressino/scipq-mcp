"""Read-only diagnosis of how a project produces (or could produce) SCIP shards, with the
exact steps to enable them. Nothing here writes files or runs a build: it tells the agent (or
the person) what to do."""

from __future__ import annotations

import datetime as _dt
import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import resources

from .workspace import DEFAULT_SHARDS_DIR, Workspace, WorkspaceError, find_project_root

SCIP_JAVAC_VERSION = "0.14.2"
INIT_SCRIPT_NAME = "scip-shards.init.gradle"


BUILD_MARKERS = ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts",
                 "pom.xml")
_SKIP = {".git", ".gradle", ".idea", ".vscode", "build", "target", "out", "node_modules",
         ".venv", "venv", "__pycache__", "dist"}


def java_project(start: str, max_entries: int = 20000, max_depth: int = 4) -> tuple[bool, str]:
    """(is it a Java project?, why). Cheap: build files or shards at the project root, else the
    first tracked .java file (git), else a bounded walk. Used to stay out of the way (no tools)
    in projects that are not Java."""
    start = os.path.abspath(os.path.expanduser(start))
    root = find_project_root(start)
    if os.path.isdir(os.path.join(root, DEFAULT_SHARDS_DIR)):
        return True, f"SCIP shards in {root}"
    for m in BUILD_MARKERS:
        if os.path.exists(os.path.join(root, m)):
            return True, f"{m} in {root}"
    if root == os.path.expanduser("~"):
        return False, "the home directory is not a project"
    git = shutil.which("git")
    if git and os.path.exists(os.path.join(root, ".git")):
        try:
            proc = subprocess.Popen([git, "-C", root, "ls-files", "-z", "--", "*.java"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            first = proc.stdout.read(4096) if proc.stdout else b""
            proc.kill()
            proc.wait()
            if first:
                return True, f"tracked .java files in {root}"
            return False, f"no .java files tracked in {root}"
        except OSError:
            pass
    seen = 0
    base_depth = root.rstrip(os.sep).count(os.sep)
    for d, dirs, files in os.walk(root):
        if d.count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        dirs[:] = [x for x in dirs if x not in _SKIP and not x.startswith(".")]
        if any(f.endswith(".java") for f in files):
            return True, f".java files under {root}"
        seen += len(files) + len(dirs)
        if seen > max_entries:
            break
    return False, f"no build files or .java sources in {root}"


def gradle_template() -> str:
    return resources.files("scipq_mcp").joinpath(f"templates/{INIT_SCRIPT_NAME}").read_text(
        encoding="utf-8")


def maven_snippet() -> str:
    return resources.files("scipq_mcp").joinpath("templates/maven-compiler-plugin.xml").read_text(
        encoding="utf-8")


@dataclass
class Diagnosis:
    root: str
    build: str                       # "gradle", "maven" or ""
    project_name: str = ""
    wrapper: str = ""                # "./gradlew", "./mvnw" or ""
    init_scripts: list[tuple[str, str]] = field(default_factory=list)  # (path, applies?)
    shard_count: int = 0
    tasks: list[str] = field(default_factory=list)
    newest: float = 0.0
    stale: int = 0
    uncompiled: int = 0


def _gradle_user_home() -> str:
    return os.environ.get("GRADLE_USER_HOME") or os.path.join(os.path.expanduser("~"), ".gradle")


def _settings_name(root: str) -> str:
    for n in ("settings.gradle", "settings.gradle.kts"):
        try:
            with open(os.path.join(root, n), encoding="utf-8", errors="replace") as fh:
                m = re.search(r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""", fh.read())
            if m:
                return m.group(1)
        except OSError:
            pass
    return os.path.basename(os.path.abspath(root))


def _init_scripts(project: str, dirname: str = "") -> list[tuple[str, str]]:
    """Init scripts in ~/.gradle/init.d that add scip-javac, and whether they apply here: a
    PROJECTS list, or a comparison of the root project's name (rootProject.name, from the
    settings) or of its directory's name (projectDir.name)."""
    out = []
    pattern = os.path.join(_gradle_user_home(), "init.d", "*")
    for path in sorted(glob.glob(pattern)):
        if not path.endswith((".gradle", ".gradle.kts")):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if "scip-javac" not in text and "-Xplugin:scip" not in text:
            continue
        m = re.search(r"def\s+PROJECTS\s*=\s*\[([^\]]*)\]", text)
        cmp_ = re.search(r"""((?:[\w$]+\s*\.\s*)*name)\s*([!=]=)\s*['"]([^'"]+)['"]""", text)
        pre = re.search(r"""((?:[\w$]+\s*\.\s*)*name)\s*\.\s*startsWith\s*\(\s*['"]([^'"]+)"""
                        r"""['"]""", text)

        def subject(expr: str) -> str:
            return (dirname or project) if "projectDir" in expr else project
        if m is not None:
            names = re.findall(r"""['"]([^'"]+)['"]""", m.group(1))
            applies = "yes (all projects)" if not names else (
                "yes" if project in names else f"NO: PROJECTS = {names} does not include {project!r}")
        elif cmp_ is not None:
            only, who = cmp_.group(3), subject(cmp_.group(1))
            what = "directory" if "projectDir" in cmp_.group(1) else "root project"
            applies = "yes" if only == who else f"NO: only for {what} {only!r}"
        elif pre is not None:
            prefix, who = pre.group(2), subject(pre.group(1))
            what = "directories" if "projectDir" in pre.group(1) else "projects"
            applies = "yes" if who.startswith(prefix) else f"NO: only for {what} named {prefix}*"
        else:
            applies = "unknown (custom script)"
        out.append((path, applies))
    return out


def diagnose(root: str, shards_dir: str | None = None) -> Diagnosis:
    root = os.path.abspath(root)
    if any(os.path.exists(os.path.join(root, f)) for f in
           ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts")):
        d = Diagnosis(root, "gradle", _settings_name(root))
        d.wrapper = "./gradlew" if os.path.exists(os.path.join(root, "gradlew")) else "gradle"
        d.init_scripts = _init_scripts(d.project_name, os.path.basename(os.path.abspath(root)))
    elif os.path.exists(os.path.join(root, "pom.xml")):
        d = Diagnosis(root, "maven", os.path.basename(root))
        d.wrapper = "./mvnw" if os.path.exists(os.path.join(root, "mvnw")) else "mvn"
    else:
        d = Diagnosis(root, "")
    try:
        ws = Workspace(root, shards_dir)
        files = ws.files()
        d.shard_count = len(files)
        d.tasks = [m for _r, m in ws.scip_roots()]
        f = ws.freshness()
        d.stale, d.uncompiled = len(f.stale), len(f.uncompiled)
        for k, rel in files[:: max(1, len(files) // 200)]:  # sample for the newest time
            try:
                d.newest = max(d.newest, os.stat(os.path.join(ws.scip_roots()[k][0], rel)).st_mtime)
            except OSError:
                pass
    except WorkspaceError:
        pass
    return d


def render(d: Diagnosis, include_template: bool = True) -> str:
    lines = [f"scipq-mcp setup — {d.root}"]
    if d.shard_count:
        when = _dt.datetime.fromtimestamp(d.newest).strftime("%Y-%m-%d %H:%M") if d.newest else "?"
        lines.append(f"Shards: {d.shard_count} in {len(d.tasks)} compile tasks "
                     f"({', '.join(d.tasks[:8])}{' …' if len(d.tasks) > 8 else ''}); "
                     f"last written around {when}.")
        if d.stale or d.uncompiled:
            lines.append(f"  {d.stale} sources changed since their last compilation, "
                         f"{d.uncompiled} new files not compiled yet: recompile to refresh.")
        lines.append("The tools are ready. Recompile after edits to keep the index current "
                     "(scipq-mcp never builds by itself).")
        for path, applies in d.init_scripts:
            if applies.startswith("yes"):
                lines.append(f"Init script: {path}")
        return "\n".join(lines)
    else:
        lines.append(f"Shards: none under {DEFAULT_SHARDS_DIR}. The navigation tools need them: "
                     "they come from compiling with the scip-javac plugin.")
    lines.append("")
    lines.append("scipq-mcp never writes files or runs builds. Do the steps yourself, and ask the "
                 "user before changing their build or ~/.gradle (it affects every build that "
                 "loads the init script).")
    if d.build == "gradle":
        lines += _gradle_steps(d, include_template)
    elif d.build == "maven":
        lines += _maven_steps(d, include_template)
    else:
        lines += [
            "Build: no Gradle or Maven files at the root.",
            "Any javac can write the shards: put org.scip-code:scip-javac:"
            f"{SCIP_JAVAC_VERSION} on the annotation processor path and pass",
            f"  -Xplugin:scip -sourceroot:<project root> "
            f"-targetroot:<project root>/{DEFAULT_SHARDS_DIR}/<module>/<task>",
        ]
    return "\n".join(lines)


def _gradle_steps(d: Diagnosis, include_template: bool) -> list[str]:
    home = _gradle_user_home()
    target = os.path.join(home, "init.d", INIT_SCRIPT_NAME)
    out = [f"Build: Gradle, root project {d.project_name!r}."]
    applying = [p for p, a in d.init_scripts if a.startswith("yes")]
    listing = [p for p, a in d.init_scripts if "PROJECTS" in a]
    for path, applies in d.init_scripts:
        out.append(f"Init script found: {path} — applies to this project: {applies}")
    step = 1
    if not applying:
        if listing:
            out.append(f"{step}. Add {d.project_name!r} to PROJECTS in {listing[0]}, or "
                       f"install the template below as {target}.")
        else:
            out.append(f"{step}. Save the init script below as {target}")
            out.append(f"   and set PROJECTS = [\"{d.project_name}\"] in it (empty = every "
                       "build on this machine). Alternatively, pass it to one build with "
                       f"--init-script <file>.")
        step += 1
    out.append(f"{step}. Compile, e.g.: {d.wrapper} compileJava compileTestJava "
               "(the first run with the plugin recompiles everything; later builds are "
               "incremental and keep the shards current).")
    step += 1
    out.append(f"{step}. Call the tools again (or setup, to check). Turn the plugin off for one "
               "build with -Pscip=false.")
    if include_template and not applying:
        out += ["", f"--- {INIT_SCRIPT_NAME} ---", gradle_template().rstrip(), "--- end ---"]
    return out


def _maven_steps(d: Diagnosis, include_template: bool) -> list[str]:
    out = [
        "Build: Maven.",
        "1. Add the configuration below to maven-compiler-plugin (in the parent pom for "
        "multi-module builds). annotationProcessorPaths replaces processor discovery on the "
        "classpath: keep your other processors (e.g. Lombok) listed there too.",
        f"2. Compile, e.g.: {d.wrapper} compile test-compile",
        "3. Call the tools again (or setup, to check).",
    ]
    if include_template:
        out += ["", "--- maven-compiler-plugin ---", maven_snippet().rstrip(), "--- end ---"]
    return out

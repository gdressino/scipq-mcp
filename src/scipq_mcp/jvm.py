"""The refactor worker: a short-lived JVM running JavaParser as a syntax oracle.

scipq-mcp never bundles or downloads JavaParser by itself. The jar is looked for in its own
tools directory (`scipq-mcp tools install javaparser`, run by the user), in the local Maven
repository and in Gradle's cache; the newest one found is used. The worker
(`worker/ScipqWorker.java`, part of this package) is compiled once per JDK and jar into the
tools directory. It is started for one tool call, at the lowest priority, with a memory cap; it
reads JSON requests on stdin, answers on stdout, never opens a file, and exits when its stdin
closes.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from dataclasses import dataclass

from . import cachedir
from . import libsrc as L

JAVAPARSER = "com.github.javaparser:javaparser-core"
# What `tools install javaparser` downloads, and its SHA-256 (checked against Maven Central's
# SHA-1 when pinned).
PINNED_VERSION = "3.28.2"
PINNED_SHA256 = "b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd"
CENTRAL = "https://repo1.maven.org/maven2/com/github/javaparser/javaparser-core"
WORKER_SOURCE = os.path.join(os.path.dirname(__file__), "worker", "ScipqWorker.java")
MIN_JDK = 11
TIMEOUT = 180.0  # seconds per request
HEAP = "512m"


class ToolError(Exception):
    """The worker cannot run: what is missing and how to get it."""


@dataclass
class Jar:
    path: str
    version: str
    origin: str   # "scipq-mcp tools", "Maven repository", "Gradle cache"


def _version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.-]", v))


def tools_dir(base: str | None = None, create: bool = False) -> str:
    d = os.path.join(base or cachedir.default_base(), "tools")
    if create:
        cachedir.ensure_private_dir(os.path.dirname(d))
        cachedir.ensure_private_dir(d)
    return d


def _sha(path: str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _trusted(j: Jar) -> bool:
    """Integrity: Gradle names the directory after the file's SHA-1; Maven may keep a .sha1
    next to it; ours must be the pinned file."""
    try:
        if j.origin == "Gradle cache":
            return _sha(j.path, "sha1") == os.path.basename(os.path.dirname(j.path))
        if j.origin == "Maven repository" and os.path.isfile(j.path + ".sha1"):
            with open(j.path + ".sha1", encoding="ascii", errors="replace") as fh:
                want = fh.read().split()[:1]
            return bool(want) and _sha(j.path, "sha1") == want[0].lower()
        if j.origin == "scipq-mcp tools":
            return j.version != PINNED_VERSION or _sha(j.path, "sha256") == PINNED_SHA256
    except OSError:
        return False
    return True


def find_jars(base: str | None = None) -> list[Jar]:
    """JavaParser jars on this machine, newest first."""
    home = os.path.expanduser("~")
    found: list[Jar] = []
    patterns = [
        (os.path.join(tools_dir(base), "javaparser-core-*.jar"), "scipq-mcp tools"),
        (os.path.join(os.environ.get("MAVEN_REPO") or os.path.join(home, ".m2", "repository"),
                      "com", "github", "javaparser", "javaparser-core", "*",
                      "javaparser-core-*.jar"), "Maven repository"),
        (os.path.join(os.environ.get("GRADLE_USER_HOME") or os.path.join(home, ".gradle"),
                      "caches", "modules-2", "files-2.1", "com.github.javaparser",
                      "javaparser-core", "*", "*", "javaparser-core-*.jar"), "Gradle cache"),
    ]
    for pattern, origin in patterns:
        for p in glob.glob(pattern):
            m = re.match(r"javaparser-core-(\d+(?:\.\d+)*)\.jar$", os.path.basename(p))
            if m:
                found.append(Jar(p, m.group(1), origin))
    found.sort(key=lambda j: _version_key(j.version), reverse=True)
    return found


def find_jdk() -> tuple[str, int]:
    """(JDK home with javac, major version): JAVA_HOME, then the newest installed, then the
    one on PATH. JavaParser has its own grammar: any JDK 11+ parses any language level."""
    homes = []
    env = os.environ.get("JAVA_HOME", "")
    if env:
        homes.append(os.path.realpath(env))
    homes += sorted(L._jdk_homes(), key=lambda h: L._jdk_version(h)[0], reverse=True)
    javac = shutil.which("javac")
    if javac:
        homes.append(os.path.dirname(os.path.dirname(os.path.realpath(javac))))
    for h in homes:
        major = L._jdk_version(h)[0]
        if major >= MIN_JDK and os.path.isfile(os.path.join(h, "bin", "javac")) \
                and os.path.isfile(os.path.join(h, "bin", "java")):
            return h, major
    raise ToolError(f"refactoring needs a JDK {MIN_JDK}+ with javac (JAVA_HOME, SDKMAN, "
                    "/usr/lib/jvm… or javac on PATH); none found")


def install_hint() -> str:
    return (f"JavaParser {PINNED_VERSION}+ is needed for refactoring, and scipq-mcp does not "
            "download it by itself. Ask the user to run one of:\n"
            f"  scipq-mcp tools install javaparser      (downloads {PINNED_VERSION} from Maven "
            "Central and checks its SHA-256)\n"
            f"  mvn -q dependency:get -Dartifact={JAVAPARSER}:{PINNED_VERSION}      (uses "
            "your Maven settings: mirrors, proxy)")


def install_javaparser(base: str | None = None) -> str:
    """Downloads the pinned JavaParser into the tools directory (run by the user)."""
    d = tools_dir(base, create=True)
    target = os.path.join(d, f"javaparser-core-{PINNED_VERSION}.jar")
    if os.path.isfile(target) and _sha(target, "sha256") == PINNED_SHA256:
        return f"already installed: {target}"
    url = f"{CENTRAL}/{PINNED_VERSION}/javaparser-core-{PINNED_VERSION}.jar"
    tmp = target + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "scipq-mcp"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out)
    got = _sha(tmp, "sha256")
    if got != PINNED_SHA256:
        os.unlink(tmp)
        raise ToolError(f"{url}: SHA-256 {got}, expected {PINNED_SHA256}; not installed")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return f"installed {target}"


def _compiled(jdk: str, jar: Jar, base: str | None) -> str:
    """The worker's class directory, compiling it on first use."""
    with open(WORKER_SOURCE, "rb") as fh:
        src = fh.read()
    key = hashlib.sha256(src + jar.path.encode() + jar.version.encode() + jdk.encode()) \
        .hexdigest()[:16]
    d = os.path.join(tools_dir(base, create=True), f"worker-{key}")
    if os.path.isfile(os.path.join(d, "ScipqWorker.class")):
        return d
    tmp = d + f".{os.getpid()}.tmp"
    os.makedirs(tmp, mode=0o700, exist_ok=True)
    proc = subprocess.run(_low([os.path.join(jdk, "bin", "javac"), "-encoding", "UTF-8",
                                "-nowarn", "-d", tmp, "-cp", jar.path, WORKER_SOURCE]),
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ToolError(f"compiling the refactor worker with {jdk} and JavaParser "
                        f"{jar.version} failed: {proc.stderr.strip()[-800:]}")
    try:
        os.replace(tmp, d)
    except OSError:  # compiled meanwhile by another process
        shutil.rmtree(tmp, ignore_errors=True)
    return d


def _low(cmd: list[str]) -> list[str]:
    nice = shutil.which("nice")
    return [nice, "-n", "19"] + cmd if nice else cmd


class Worker:
    """One worker process for the duration of a tool call (`with Worker(...) as w`)."""

    def __init__(self, level: str = "", base: str | None = None):
        self.base = base
        self.want_level = level
        self.proc: subprocess.Popen | None = None
        self.jar: Jar | None = None
        self.levels: list[str] = []
        self.version = ""
        self.level = ""
        self.note = ""

    def __enter__(self) -> "Worker":
        jars = [j for j in find_jars(self.base) if _trusted(j)]
        if not jars:
            raise ToolError(install_hint())
        jdk, _major = find_jdk()
        last_error = ""
        for jar in jars:  # newest first; an older one may still know the level
            try:
                classes = _compiled(jdk, jar, self.base)
            except (ToolError, OSError, subprocess.TimeoutExpired) as e:
                last_error = str(e)
                continue
            self._start(jdk, jar, classes)
            try:
                hello = self.request({"op": "hello"})
            except (ToolError, ValueError) as e:
                self.close()
                last_error = str(e)
                continue
            self.levels = hello.get("levels", [])
            self.version = hello.get("version") or jar.version
            self.jar = jar
            level = self._choose_level()
            if level:
                self.level = level
                return self
            self.close()
            last_error = (f"JavaParser {self.version} ({jar.path}) knows Java up to "
                          f"{self._max_level()}; this project compiles for "
                          f"{self.want_level.replace('JAVA_', 'Java ')}")
        raise ToolError(last_error + "\n" + install_hint())

    def _max_level(self) -> str:
        nums = [int(m.group(1)) for x in self.levels if (m := re.fullmatch(r"JAVA_(\d+)", x))]
        return f"Java {max(nums)}" if nums else "?"

    def _choose_level(self) -> str:
        if self.want_level:
            return self.want_level if self.want_level in self.levels else ""
        nums = [int(m.group(1)) for x in self.levels if (m := re.fullmatch(r"JAVA_(\d+)", x))]
        if not nums:
            return ""
        self.note = (f"the project's Java version is unknown: parsing at Java {max(nums)}, the "
                     f"newest JavaParser {self.version} knows")
        return f"JAVA_{max(nums)}"

    def _start(self, jdk: str, jar: Jar, classes: str) -> None:
        cmd = _low([os.path.join(jdk, "bin", "java"), f"-Xmx{HEAP}", "-XX:+UseSerialGC",
                    "-XX:TieredStopAtLevel=1", "-Xss4m", "-cp",
                    os.pathsep.join([jar.path, classes]), "ScipqWorker"])
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=tools_dir(self.base))

    def request(self, req: dict) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise ToolError("the refactor worker is not running" + self._stderr())
        if "level" not in req and self.level:
            req = dict(req, level=self.level)
        line = json.dumps(req) + "\n"
        result: list = []

        def talk():
            try:
                self.proc.stdin.write(line.encode("utf-8"))
                self.proc.stdin.flush()
                result.append(self.proc.stdout.readline())
            except OSError as e:
                result.append(e)
        t = threading.Thread(target=talk, daemon=True)
        t.start()
        t.join(TIMEOUT)
        if t.is_alive() or not result:
            self.close()
            raise ToolError(f"the refactor worker did not answer within {TIMEOUT:.0f} s "
                            "(stopped)")
        out = result[0]
        if isinstance(out, Exception) or not out:
            raise ToolError("the refactor worker stopped" + self._stderr())
        resp = json.loads(out.decode("utf-8"))
        if "error" in resp and len(resp) == 1:
            raise ToolError(f"refactor worker: {resp['error']}")
        return resp

    def _stderr(self) -> str:
        if self.proc is None or self.proc.poll() is None:
            return ""
        try:
            err = self.proc.stderr.read().decode("utf-8", errors="replace").strip()
        except OSError:
            err = ""
        return f": {err[-600:]}" if err else ""

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()
        finally:
            self.proc = None

    def __exit__(self, *exc) -> None:
        self.close()


def status(base: str | None = None) -> list[str]:
    """For setup/doctor: what refactoring would use, or what is missing."""
    lines = []
    jars = find_jars(base)
    good = [j for j in jars if _trusted(j)]
    if good:
        j = good[0]
        lines.append(f"JavaParser {j.version}: {j.path} ({j.origin})")
    else:
        lines.append("JavaParser: not found" + (" (jars with a bad checksum ignored)"
                                                 if jars else ""))
    try:
        jdk, major = find_jdk()
        lines.append(f"JDK {major} for the worker: {jdk}")
    except ToolError as e:
        lines.append(str(e))
    return lines


def language_level(root: str) -> str:
    """JavaParser's level for the Java version the project compiles for ("" when unknown)."""
    target, _home = L.build_java_target(root)
    if target is None:
        return ""
    return "JAVA_1_" + str(target) if target < 5 else f"JAVA_{target}"

"""Profiles and project configuration (TOML).

Profiles are declarative: entry points (annotations, supertypes, type name globs), persistence
model, globs for uses by name, annotation attributes for concept search, and where tests live.
Built-in profiles ship with the package; users can add or override them in
`$XDG_CONFIG_HOME/scipq/profiles/*.toml` (default `~/.config/scipq/profiles/`).

The project configuration — `scipq-mcp --config FILE`, else `$XDG_CONFIG_HOME/scipq/config.toml`
— maps checkouts to profiles and project-specific settings:

    [[project]]
    match = { root_project = "shop" }       # or { path = "~/src/shop*" }, { git_remote = "…" }
    profiles = ["jpa", "spring", "junit"]
    [project.tests]
    filter = "./run-tests.sh {module}:test --tests '{fqcn}'"

When several [[project]] entries match, the most specific kind wins: path > root_project >
git_remote (then file order). Without a matching entry, built-in profiles whose annotations
appear in the project are activated automatically.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib import resources

KNOWN_PROFILE_KEYS = {"profile", "entry_points", "persistence", "by_name", "concepts", "tests"}
KNOWN_PROJECT_KEYS = {"match", "profiles", "tests", "entry_points", "by_name", "concepts",
                      "persistence"}
KNOWN_MATCH_KEYS = {"path", "root_project", "git_remote"}
MATCH_ORDER = ("path", "root_project", "git_remote")


class ConfigError(Exception):
    pass


def config_home() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "scipq")


def to_symbol(name: str) -> str:
    """`org.junit.jupiter.api.Test` -> `org/junit/jupiter/api/Test#`; SCIP symbols pass."""
    n = name.strip()
    if "/" in n or n.endswith("#"):
        return n if n.endswith("#") else n + "#"
    parts = n.split(".")
    return "/".join(parts[:-1]) + ("/" if len(parts) > 1 else "") + parts[-1] + "#"


@dataclass
class Settings:
    """What the tools use, merged from the active profiles and the project entry."""
    profiles: list[str] = field(default_factory=list)
    source: str = ""                     # how they were chosen: "config (path=…)", "auto", …
    entry_annotations: set[str] = field(default_factory=set)   # SCIP symbols
    entry_supertypes: set[str] = field(default_factory=set)
    entry_type_names: list[str] = field(default_factory=list)  # globs on simple names
    entity_annotations: set[str] = field(default_factory=set)
    table: list[tuple[set[str], str]] = field(default_factory=list)   # (annotations, attribute)
    column: list[tuple[set[str], str]] = field(default_factory=list)
    by_name_globs: list[str] = field(default_factory=lambda: ["*.java"])
    by_name_patterns: list[str] = field(default_factory=list)
    concept_attributes: list[str] = field(default_factory=list)
    tests_filter: str = ""               # template: {module} {task} {fqcn} {simple} {classes}
    warnings: list[str] = field(default_factory=list)


def _read_toml(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e


def builtin_profiles() -> dict[str, dict]:
    out = {}
    for entry in resources.files("scipq_mcp").joinpath("profiles").iterdir():
        if entry.name.endswith(".toml"):
            data = tomllib.loads(entry.read_text(encoding="utf-8"))
            out[data.get("profile", {}).get("name", entry.name[:-5])] = data
    return out


def load_profiles(home: str | None = None) -> tuple[dict[str, dict], list[str]]:
    """Built-in profiles overridden by user ones (same name), and warnings."""
    profiles = builtin_profiles()
    warnings: list[str] = []
    d = os.path.join(home or config_home(), "profiles")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if not name.endswith(".toml"):
                continue
            path = os.path.join(d, name)
            try:
                data = _read_toml(path)
            except ConfigError as e:
                warnings.append(str(e))
                continue
            unknown = set(data) - KNOWN_PROFILE_KEYS
            if unknown:
                warnings.append(f"{path}: unknown keys {sorted(unknown)}")
            profiles[data.get("profile", {}).get("name", name[:-5])] = data
    return profiles, warnings


def root_project_name(root: str) -> str:
    """rootProject.name from settings.gradle(.kts), else the pom's artifactId, read as text."""
    for n in ("settings.gradle", "settings.gradle.kts"):
        try:
            with open(os.path.join(root, n), encoding="utf-8", errors="replace") as fh:
                m = re.search(r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""", fh.read())
            if m:
                return m.group(1)
        except OSError:
            pass
    try:
        with open(os.path.join(root, "pom.xml"), encoding="utf-8", errors="replace") as fh:
            text = re.sub(r"<parent>.*?</parent>", "", fh.read(), flags=re.S)
        m = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return ""


def git_remote(root: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:
        proc = subprocess.run([git, "-C", root, "config", "--get", "remote.origin.url"],
                              capture_output=True, text=True, timeout=5)
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _matches(match: dict, root: str, lazy: dict) -> str | None:
    """The match kind that applies ("path", "root_project", "git_remote") or None."""
    for kind in MATCH_ORDER:
        want = match.get(kind)
        if not want:
            continue
        if kind == "path":
            pattern = os.path.expanduser(str(want))
            if fnmatch.fnmatch(os.path.realpath(root), os.path.realpath(pattern)) or \
                    fnmatch.fnmatch(root, pattern):
                return kind
        elif kind == "root_project":
            if "root_project" not in lazy:
                lazy["root_project"] = root_project_name(root)
            if fnmatch.fnmatch(lazy["root_project"], str(want)):
                return kind
        else:
            if "git_remote" not in lazy:
                lazy["git_remote"] = git_remote(root)
            if lazy["git_remote"] and fnmatch.fnmatch(lazy["git_remote"], str(want)):
                return kind
    return None


def settings_for(root: str, config_file: str | None = None, home: str | None = None,
                 present_annotations=None) -> Settings:
    """Settings for a checkout. `present_annotations` (SCIP symbols used in the project) enables
    the automatic choice of built-in profiles when no [[project]] entry matches."""
    profiles, warnings = load_profiles(home)
    path = config_file or os.path.join(home or config_home(), "config.toml")
    try:
        conf = _read_toml(path)
    except ConfigError as e:
        conf, warnings = {}, warnings + [str(e)]
    best = None
    lazy: dict = {}
    for k, entry in enumerate(conf.get("project", [])):
        unknown = set(entry) - KNOWN_PROJECT_KEYS
        if unknown:
            warnings.append(f"{path}: [[project]] #{k + 1}: unknown keys {sorted(unknown)}")
        bad = set(entry.get("match", {})) - KNOWN_MATCH_KEYS
        if bad:
            warnings.append(f"{path}: [[project]] #{k + 1}: unknown match keys {sorted(bad)}")
        kind = _matches(entry.get("match", {}), root, lazy)
        if kind is None:
            continue
        rank = (MATCH_ORDER.index(kind), k)
        if best is None or rank < best[0]:
            best = (rank, entry, kind)
    s = Settings(warnings=warnings)
    if best is not None:
        entry, kind = best[1], best[2]
        s.profiles = list(entry.get("profiles", []))
        s.source = f"{path} ({kind} = {entry['match'][kind]!r})"
    elif present_annotations is not None:
        present = set(present_annotations)
        for name, data in profiles.items():
            anns = {to_symbol(a) for ep in data.get("entry_points", [])
                    for a in ep.get("annotations", [])}
            if anns & present:
                s.profiles.append(name)
        s.source = "auto (annotations found in the project)"
    for name in s.profiles:
        data = profiles.get(name)
        if data is None:
            s.warnings.append(f"unknown profile {name!r}; available: {sorted(profiles)}")
            continue
        _apply(s, data)
    if best is not None:
        _apply(s, best[1])
    return s


def _apply(s: Settings, data: dict) -> None:
    for ep in data.get("entry_points", []):
        s.entry_annotations |= {to_symbol(a) for a in ep.get("annotations", [])}
        s.entry_supertypes |= {to_symbol(t) for t in ep.get("supertypes", [])}
        s.entry_type_names += list(ep.get("type_names", []))
    p = data.get("persistence", {})
    s.entity_annotations |= {to_symbol(a) for a in p.get("entity", [])}
    for key, target in (("table", s.table), ("column", s.column)):
        spec = p.get(key)
        if spec:
            anns = spec.get("annotation", [])
            anns = [anns] if isinstance(anns, str) else anns
            target.append(({to_symbol(a) for a in anns}, spec.get("attribute", "name")))
    bn = data.get("by_name", {})
    for g in bn.get("globs", []):
        if g not in s.by_name_globs:
            s.by_name_globs.append(g)
    s.by_name_patterns += list(bn.get("patterns", []))
    s.concept_attributes += list(data.get("concepts", {}).get("attributes", []))
    t = data.get("tests", {})
    if t.get("filter"):
        s.tests_filter = t["filter"]

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
git_remote (then file order).

A checkout can carry the team's settings in `.scipq.toml` at its root (the same keys as a
[[project]] entry, without `match`), read from the working tree. Precedence, lowest first and
key by key: built-in profiles, user profiles, the repository's `.scipq.toml`, the matching
[[project]] entry of the user's configuration. Without profiles named in either file, built-in
profiles whose annotations appear in the project are activated automatically.

`.scipq.toml` comes with the repository, so it is untrusted input: it is size-capped, lists and
strings are capped, path globs must be relative and stay inside the checkout, and nothing in it
is ever executed (`tests.filter` is only printed).
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib import resources

KNOWN_PROFILE_KEYS = {"profile", "entry_points", "persistence", "by_name", "concepts", "tests",
                      "cucumber", "lifecycle"}
KNOWN_PROJECT_KEYS = {"match", "profiles", "tests", "entry_points", "by_name", "concepts",
                      "persistence", "dead_code", "cucumber", "check_refs", "lifecycle"}
KNOWN_MATCH_KEYS = {"path", "root_project", "git_remote"}
MATCH_ORDER = ("path", "root_project", "git_remote")

REPO_FILE = ".scipq.toml"
REPO_KEYS = KNOWN_PROJECT_KEYS - {"match"}
REPO_MAX_BYTES = 256 * 1024
REPO_MAX_ITEMS = 100     # per list (globs, annotations, entry points…)
REPO_MAX_PATTERNS = 20
REPO_MAX_TEXT = 400      # characters per string


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
    entry_packages: list[str] = field(default_factory=list)    # packages used from outside
    # The same, one per [[entry_points]] with its kind, for impact: {kind, annotations,
    # supertypes, type_names, xml: [(file globs, element names)]}.
    entry_specs: list[dict] = field(default_factory=list)
    test_annotations: set[str] = field(default_factory=set)    # entry points of kind "test"
    entity_annotations: set[str] = field(default_factory=set)
    table: list[tuple[set[str], str]] = field(default_factory=list)   # (annotations, attribute)
    column: list[tuple[set[str], str]] = field(default_factory=list)
    # The persistence model (persistence tool): marker annotations (transient, key, version,
    # join, flags) and attribute specs (inheritance, discriminator, mapped_by).
    persistence_markers: dict[str, set[str]] = field(default_factory=dict)
    persistence_attrs: dict[str, list[tuple[set[str], str]]] = field(default_factory=dict)
    by_name_globs: list[str] = field(default_factory=lambda: ["*.java"])
    by_name_exclude: list[str] = field(default_factory=list)    # path globs
    by_name_patterns: list[str] = field(default_factory=list)
    dead_code_exclude: list[str] = field(default_factory=list)  # path globs: never candidates
    # Cucumber: commands to run affected scenarios ({names}, {features}; {tags}), and tags the
    # build leaves out (an affected scenario carrying one is reported: it will not run).
    cucumber_run: str = ""
    cucumber_run_features: str = ""
    cucumber_exclude_tags: list[str] = field(default_factory=lambda: ["@pending", "@ignore",
                                                                      "@wip", "@skip"])
    cucumber_run_all: str = ""           # the whole suite, when too many scenarios to name
    # Step texts (Cucumber Expressions) whose quoted argument names another .feature whose
    # Background they load: its steps run where the including step does.
    cucumber_include_steps: list[str] = field(default_factory=list)
    concept_attributes: list[str] = field(default_factory=list)
    # Methods the framework calls on an object it was given (Vaadin's onAttach): impact
    # reaches whoever creates that object.
    lifecycle_methods: set[str] = field(default_factory=set)
    concepts_exclude: list[str] = field(default_factory=list)   # path globs (vendored code)
    check_refs_exclude: list[str] = field(default_factory=list)  # docs not to check (archives)
    # Words a doc writes before a path for another branch's version (v4 `path:N`), like main
    check_refs_versions: list[str] = field(default_factory=list)
    tests_filter: str = ""               # template: {module} {task} {fqcn} {simple} {classes}
    tests_filter_from: str = ""          # where it came from
    files: list[str] = field(default_factory=list)   # configuration applied, lowest first
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


def _inside(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    return path == root or path.startswith(root + os.sep)


def read_repo_config(root: str) -> tuple[dict, list[str]]:
    """The checkout's `.scipq.toml`, validated (see the module docstring), and warnings."""
    path = os.path.join(root, REPO_FILE)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {}, []
    except OSError as e:
        return {}, [f"{REPO_FILE}: {e}"]
    if stat.S_ISLNK(st.st_mode):
        if not _inside(path, root):
            return {}, [f"{REPO_FILE} is a link out of the checkout: ignored"]
        try:
            st = os.stat(path)
        except OSError as e:
            return {}, [f"{REPO_FILE}: {e}"]
    if not stat.S_ISREG(st.st_mode):
        return {}, [f"{REPO_FILE} is not a regular file: ignored"]
    if st.st_size > REPO_MAX_BYTES:
        return {}, [f"{REPO_FILE} is larger than {REPO_MAX_BYTES // 1024} KiB: ignored"]
    try:
        data = _read_toml(path)
    except ConfigError as e:
        return {}, [str(e).replace(path, REPO_FILE)]
    return sanitize_repo_config(data)


def sanitize_repo_config(data: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    out: dict = {}
    for key, value in data.items():
        if key == "match":
            warnings.append(f"{REPO_FILE}: `match` is ignored (the file applies to its own "
                            "checkout)")
        elif key not in REPO_KEYS:
            warnings.append(f"{REPO_FILE}: unknown key {key!r}")
        else:
            clean = _capped(value, key, warnings)
            if clean is not None:
                out[key] = clean
    for ep in out.get("entry_points", []) if isinstance(out.get("entry_points"), list) else []:
        if isinstance(ep, dict) and isinstance(ep.get("xml"), list):
            for x in ep["xml"]:
                if isinstance(x, dict) and "files" in x:
                    x["files"] = [g for g in _as_list(x["files"], "entry_points.xml.files",
                                                      warnings)
                                  if _relative_glob(g, "entry_points.xml.files", warnings)]
    cc = out.get("concepts")
    if isinstance(cc, dict) and "exclude" in cc:
        cc["exclude"] = [g for g in _as_list(cc["exclude"], "concepts.exclude", warnings)
                         if _relative_glob(g, "concepts.exclude", warnings)]
    cr = out.get("check_refs")
    if isinstance(cr, dict) and "exclude" in cr:
        cr["exclude"] = [g for g in _as_list(cr["exclude"], "check_refs.exclude", warnings)
                         if _relative_glob(g, "check_refs.exclude", warnings)]
    if isinstance(cr, dict) and "other_versions" in cr:
        cr["other_versions"] = _as_list(cr["other_versions"], "check_refs.other_versions",
                                        warnings)
    dc = out.get("dead_code")
    if isinstance(dc, dict) and "exclude" in dc:
        dc["exclude"] = [g for g in _as_list(dc["exclude"], "dead_code.exclude", warnings)
                         if _relative_glob(g, "dead_code.exclude", warnings)]
    bn = out.get("by_name")
    if isinstance(bn, dict):
        for k in ("globs", "exclude"):
            if k in bn:
                bn[k] = [g for g in _as_list(bn[k], f"by_name.{k}", warnings)
                         if _relative_glob(g, f"by_name.{k}", warnings)]
        if "patterns" in bn:
            bn["patterns"] = _as_list(bn["patterns"], "by_name.patterns", warnings)
        if len(bn.get("patterns", [])) > REPO_MAX_PATTERNS:
            warnings.append(f"{REPO_FILE}: by_name.patterns: only the first "
                            f"{REPO_MAX_PATTERNS} are used")
            bn["patterns"] = bn["patterns"][:REPO_MAX_PATTERNS]
    if "profiles" in out and not (isinstance(out["profiles"], list)
                                  and all(isinstance(p, str) for p in out["profiles"])):
        warnings.append(f"{REPO_FILE}: profiles must be a list of names: ignored")
        del out["profiles"]
    if "tests" in out and not isinstance(out["tests"].get("filter", ""), str):
        warnings.append(f"{REPO_FILE}: tests.filter must be a string: ignored")
        out["tests"].pop("filter", None)
    return out, warnings


def _as_list(value, where: str, warnings: list[str]) -> list:
    """A list of strings: a single string becomes a list of one (not its characters)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    warnings.append(f"{REPO_FILE}: {where} must be a list of strings: ignored")
    return []


def _capped(value, where: str, warnings: list[str]):
    """Strings, numbers, booleans, and lists and tables of them, within the caps."""
    if isinstance(value, str):
        if len(value) > REPO_MAX_TEXT:
            warnings.append(f"{REPO_FILE}: {where}: longer than {REPO_MAX_TEXT} characters: "
                            "ignored")
            return None
        return value
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        if len(value) > REPO_MAX_ITEMS:
            warnings.append(f"{REPO_FILE}: {where}: only the first {REPO_MAX_ITEMS} items are "
                            "used")
            value = value[:REPO_MAX_ITEMS]
        items = [_capped(v, where, warnings) for v in value]
        return [v for v in items if v is not None]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            c = _capped(v, f"{where}.{k}", warnings)
            if c is not None:
                out[k] = c
        return out
    warnings.append(f"{REPO_FILE}: {where}: unsupported value: ignored")
    return None


def _relative_glob(g, where: str, warnings: list[str]) -> bool:
    if not isinstance(g, str) or not g:
        return False
    parts = g.replace("\\", "/").split("/")
    if os.path.isabs(g) or g.startswith("~") or ".." in parts:
        warnings.append(f"{REPO_FILE}: {where}: {g!r} must be relative and inside the "
                        "checkout: ignored")
        return False
    return True


def repo_template(profiles: list[str], detected: bool, generated: list[str] = (),
                  generated_java: list[str] | None = None) -> str:
    """A `.scipq.toml` to start from (printed by `scipq-mcp init-config`)."""
    names = ", ".join(f'"{p}"' for p in profiles)
    if generated:
        dirs = "[" + ", ".join(f'"{d}/*"' for d in generated) + "]"
        java_dirs = "[" + ", ".join(f'"{d}/*"' for d in (
            generated if generated_java is None else generated_java)) + "]"
        by_name = ("# Directories that look generated (by name, or because nearly all their "
                   "files start\n# with a \"generated\" header). Check the list: drop any that "
                   "holds your own code.\n"
                   "[by_name]   # names written there would hide dead code elsewhere\n"
                   f"exclude = {dirs}\n"
                   "# globs = [\"*.xml\", \"*.jrxml\"]      # files where names are used as "
                   "text\n\n"
                   + ("[dead_code]   # and their own unused members are not candidates\n"
                      f"exclude = {java_dirs}" if java_dirs != "[]" else
                      "# [dead_code]\n# exclude = []   # none of them holds Java sources"))
    else:
        by_name = ("# [by_name]\n# globs = [\"*.xml\", \"*.jrxml\"]      # files where names are "
                   "used as text (reflection, reports)\n# exclude = [\"generated/*\"]\n\n"
                   "# [dead_code]\n# exclude = [\"generated/*\"]   # paths whose members are "
                   "never dead-code candidates")
    why = ("detected from the annotations this checkout uses" if detected else
           "no shards yet: compile with the init script (scipq-mcp gradle-init), then run "
           "init-config again to detect them")
    return f"""# .scipq.toml — scipq-mcp settings shared by everyone working on this repository.
# Read from the working tree (each branch or worktree uses its own). A personal [[project]]
# entry in ~/.config/scipq/config.toml overrides it key by key. Nothing here is executed:
# tests.filter is only printed for you to run. Paths and globs are relative to this directory.

profiles = [{names}]   # {why}

# [tests]
# filter = "./gradlew {{module}}:test --tests '{{fqcn}}'"
#   placeholders: {{module}} {{task}} {{fqcn}} {{simple}} {{classes}} {{tests}}

{by_name}

# [[entry_points]]                  # framework entry points beyond the profiles
# kind = "screen"
# annotations = ["com.example.ui.Screen"]
# supertypes = ["com.example.ui.BaseScreen"]
# type_names = ["*Screen"]
# packages = ["com.example.api"]    # public API used from outside: never dead code
"""


def _home_relative(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def settings_for(root: str, config_file: str | None = None, home: str | None = None,
                 present_annotations=None) -> Settings:
    """Settings for a checkout. `present_annotations` (SCIP symbols used in the project) enables
    the automatic choice of built-in profiles when neither the checkout's `.scipq.toml` nor a
    matching [[project]] entry names profiles."""
    profiles, warnings = load_profiles(home)
    repo, repo_warnings = read_repo_config(root)
    warnings = warnings + repo_warnings
    path = config_file or os.path.join(home or config_home(), "config.toml")
    try:
        conf = _read_toml(path)
    except ConfigError as e:
        conf, warnings = {}, warnings + [str(e)]
    best = None
    lazy: dict = {}
    top = set(conf) - {"project"}
    if top:     # [check_refs] at the top of the user's file applies to no project
        warnings.append(f"{path}: unknown keys {sorted(top)} (project settings go inside "
                        "[[project]] entries, or in the checkout's .scipq.toml)")
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
    personal = ""
    if best is not None:
        personal = (f"{_home_relative(path)} ([[project]] {best[2]} = "
                    f"{best[1]['match'][best[2]]!r})")
    if best is not None and "profiles" in best[1]:
        s.profiles = list(best[1].get("profiles", []))
        s.source = personal
    elif "profiles" in repo:
        s.profiles = list(repo["profiles"])
        s.source = f"{REPO_FILE} in the checkout"
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
        _apply(s, data, f"profile {name}")
    if repo:
        _apply(s, repo, REPO_FILE)
        s.files.append(f"{REPO_FILE} (the team's, from the checkout)")
    if best is not None:
        _apply(s, best[1], personal)
        s.files.append(personal + " (personal)")
    return s


def _apply(s: Settings, data: dict, origin: str = "") -> None:
    def strings(v) -> list[str]:
        return [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] \
            if isinstance(v, list) else []

    for ep in data.get("entry_points", []):
        if not isinstance(ep, dict):
            continue
        xml = []
        for x in ep.get("xml", []) if isinstance(ep.get("xml"), list) else []:
            if isinstance(x, dict):
                files = x.get("files", [])
                elements = x.get("elements", [])
                xml.append(([files] if isinstance(files, str) else list(files),
                            [elements] if isinstance(elements, str) else list(elements)))
        if ep.get("supertypes") and ep.get("type_names") and \
                ep.get("match") not in ("all", "any"):
            s.warnings.append(
                f"{origin or 'config'}: the entry_points rule {str(ep.get('kind', 'entry'))!r} "
                "gives supertypes and type_names: since 0.6 impact and changed need both to "
                "hold (dead_code keeps a class matching either); say match = \"all\" to keep "
                "that, or match = \"any\" for either")
        s.entry_specs.append({
            "kind": str(ep.get("kind", "entry")),
            "annotations": {to_symbol(a) for a in ep.get("annotations", [])},
            "supertypes": {to_symbol(t) for t in ep.get("supertypes", [])},
            "type_names": list(ep.get("type_names", [])),
            "xml": xml,
            # false: keeps code alive for dead_code, but is not an entry point to list
            "reached": ep.get("reached", True) is not False,
            "match": "any" if ep.get("match") == "any" else "all",
            # types named as X.class in an annotation's attribute: @Route(layout = X.class)
            "referenced_by": [(to_symbol(r["annotation"]), str(r.get("attribute", "value")))
                              for r in ep.get("referenced_by", [])
                              if isinstance(r, dict) and isinstance(r.get("annotation"), str)]})
        s.entry_annotations |= {to_symbol(a) for a in ep.get("annotations", [])}
        s.entry_packages += list(ep.get("packages", []))
        if ep.get("kind") == "test":
            s.test_annotations |= {to_symbol(a) for a in ep.get("annotations", [])}
        s.entry_supertypes |= {to_symbol(t) for t in ep.get("supertypes", [])}
        s.entry_type_names += list(ep.get("type_names", []))
    p = data.get("persistence", {})
    s.entity_annotations |= {to_symbol(a) for a in p.get("entity", [])}
    def specs(v) -> list[tuple[set[str], str]]:
        out = []
        for spec in (v if isinstance(v, list) else [v] if isinstance(v, dict) else []):
            if isinstance(spec, dict):
                anns = spec.get("annotation", [])
                anns = [anns] if isinstance(anns, str) else anns
                out.append(({to_symbol(a) for a in anns}, spec.get("attribute", "name")))
        return out
    s.table += specs(p.get("table"))
    s.column += specs(p.get("column"))
    for key in ("mapped", "transient", "key", "version", "join", "flags", "relations"):
        if p.get(key):
            s.persistence_markers.setdefault(key, set()).update(
                to_symbol(a) for a in strings(p.get(key)))
    for key in ("inheritance", "discriminator", "mapped_by"):
        if p.get(key):
            s.persistence_attrs.setdefault(key, []).extend(specs(p.get(key)))
    bn = data.get("by_name", {})

    for g in strings(bn.get("globs", [])):
        if g not in s.by_name_globs:
            s.by_name_globs.append(g)
    s.by_name_exclude += strings(bn.get("exclude", []))
    s.by_name_patterns += strings(bn.get("patterns", []))
    s.dead_code_exclude += [g for g in strings(data.get("dead_code", {}).get("exclude", []))
                            if g]
    s.concept_attributes += list(data.get("concepts", {}).get("attributes", []))
    s.concepts_exclude += strings(data.get("concepts", {}).get("exclude", []))
    s.check_refs_exclude += strings(data.get("check_refs", {}).get("exclude", []))
    s.check_refs_versions += strings(data.get("check_refs", {}).get("other_versions", []))
    s.lifecycle_methods |= set(strings(data.get("lifecycle", {}).get("methods", [])))
    cu = data.get("cucumber", {})
    if isinstance(cu.get("run"), str) and cu["run"]:
        s.cucumber_run = cu["run"]
    if isinstance(cu.get("run_features"), str) and cu["run_features"]:
        s.cucumber_run_features = cu["run_features"]
    if isinstance(cu.get("exclude_tags"), list):
        s.cucumber_exclude_tags = [t for t in cu["exclude_tags"] if isinstance(t, str)]
    if isinstance(cu.get("run_all"), str) and cu["run_all"]:
        s.cucumber_run_all = cu["run_all"]
    for t in cu.get("include_steps", []) if isinstance(cu.get("include_steps"), list) else []:
        if isinstance(t, str) and t and t not in s.cucumber_include_steps:
            s.cucumber_include_steps.append(t)
    t = data.get("tests", {})
    if t.get("filter"):
        s.tests_filter = t["filter"]
        s.tests_filter_from = origin

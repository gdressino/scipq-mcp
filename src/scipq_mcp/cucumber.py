"""Cucumber-JVM: step definitions (glue) from the auxiliary index, scenarios and steps from
`.feature` files, which glue method each step runs, and how to run the scenarios affected.

Glue methods carry an annotation from a language subpackage of `io.cucumber.java` (`en.Given`,
`es.Dado`…; `cucumber.api.java` in old versions). Their text is a Cucumber Expression (`I have
{int} cucumber(s)`) or, when it starts with `^` or ends with `$`, a regular expression; both
become a Python regex here. Feature files are read with Gherkin's keywords for the language they
declare (`# language: es`; English by default): a step's keyword is removed exactly. Each
Examples row of an outline is a scenario of its own (a pickle), and a Background's steps belong
to every scenario of its feature (or of its Rule).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import symbols as S
from .graph import Graph, candidate_files

_STEP_ANNOTATION = re.compile(r"^(?:io/cucumber/java|cucumber/api/java)/[a-z_]+/[^/#]+#$")
PARAMETER_TYPE = ("io/cucumber/java/ParameterType#", "cucumber/api/java/ParameterType#")

# Section keywords (lowercase, before the colon) of Gherkin's most common languages.
SECTIONS = {
    "feature": ("feature", "business need", "ability", "característica", "caracteristica",
                "necesidad del negocio", "requisito", "funcionalidade", "fonctionnalité",
                "funktionalität", "funktion", "funzionalità", "esigenza di business", "abilità",
                "functionaliteit", "eigenschap"),
    "background": ("background", "antecedentes", "contexto", "cenário de fundo",
                   "cenario de fundo", "fundo", "contexte", "grundlage", "hintergrund",
                   "voraussetzungen", "vorbedingungen", "achtergrond"),
    "rule": ("rule", "regla", "reglas de negocio", "regra", "règle", "regel", "regola"),
    "outline": ("scenario outline", "scenario template", "esquema del escenario",
                "esquema do cenário", "esquema do cenario", "delineação do cenário",
                "delineacao do cenario", "plan du scénario", "plan du scenario",
                "szenariogrundriss", "szenarien", "schema dello scenario", "abstract scenario",
                "scenario-sjabloon", "abstract scenario"),
    "scenario": ("scenario", "example", "escenario", "ejemplo", "cenário", "cenario", "exemplo",
                 "scénario", "exemple", "szenario", "beispiel", "esempio", "voorbeeld"),
    "examples": ("examples", "scenarios", "ejemplos", "exemplos", "cenários", "cenarios",
                 "exemples", "beispiele", "esempi", "voorbeelden"),
}
_SECTION_OF = {k: kind for kind, keys in SECTIONS.items() for k in keys}
_SECTION_LINE = re.compile(r"^\s*([^:#|@\"]{1,40}?)\s*:\s?(.*)$")

# Cucumber Expression parameter types and the text they match.
PARAMETERS = {
    "int": r"-?\d+", "long": r"-?\d+", "short": r"-?\d+", "byte": r"-?\d+",
    "biginteger": r"-?\d+",
    "float": r"[-+]?\d*[.,]?\d+(?:[eE][-+]?\d+)?", "double": r"[-+]?\d*[.,]?\d+(?:[eE][-+]?\d+)?",
    "bigdecimal": r"[-+]?\d*[.,]?\d+(?:[eE][-+]?\d+)?",
    "word": r"[^\s]+", "string": r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'", "": r".*",
}


@dataclass
class Glue:
    symbol: str
    source: str
    line: int                 # 1-based, of the method
    annotation: str           # e.g. "Dado"
    text: str                 # the expression or regex as written
    kind: str                 # "expression", "regex", or "unknown" (not understood)
    regex: re.Pattern | None = None


@dataclass
class Scenario:
    """A pickle: a scenario, or one Examples row of an outline."""
    source: str
    line: int                 # 1-based: its Scenario line, or its Examples row
    name: str                 # outlines: with the row's values in place of <col>
    tags: list[str]           # its own and inherited (feature, rule, examples)
    rule: str = ""
    feature: str = ""
    header: int = 0           # its Scenario / Scenario Outline line


@dataclass
class Step:
    source: str
    line: int                 # 1-based
    keyword: str              # "" when the language is unknown (see `match`)
    text: str                 # without its keyword
    pickles: list[int] = field(default_factory=list)  # Feature.scenarios it runs in


@dataclass
class Feature:
    source: str
    name: str
    tags: list[str]
    language: str
    scenarios: list[Scenario] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


# --- glue -------------------------------------------------------------------------------------

def _string_literals(text: str) -> list[str]:
    """The Java string literals in `text` (escapes resolved), in order."""
    out = []
    for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', text):
        s = m.group(1)
        s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda u: chr(int(u.group(1), 16)), s)
        s = re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t", "r": "\r"}.get(e.group(1),
                                                                        e.group(1)), s)
        out.append(s)
    return out


def annotation_text(lines: list[str], line: int, name: str) -> str | None:
    """The string value of `@name(…)` starting at 0-based `line`: literals concatenated with
    `+`, or the `value = …` attribute; None when it is not made of literals only."""
    from .javasrc import mask
    chunk = "\n".join(lines[line:line + 12])
    masked = mask(chunk)
    m = re.search(r"@\s*(?:[\w$]+\s*\.\s*)*" + re.escape(name) + r"\s*\(", masked)
    if not m:
        return None
    depth, end = 0, -1
    for k in range(m.end() - 1, len(masked)):
        if masked[k] == "(":
            depth += 1
        elif masked[k] == ")":
            depth -= 1
            if depth == 0:
                end = k
                break
    if end < 0:
        return None
    inner, inner_masked = chunk[m.end():end], masked[m.end():end]
    # Split attributes at top-level commas; keep `value = …` or the unnamed one.
    parts, depth, start = [], 0, 0
    for k, ch in enumerate(inner_masked):
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append((start, k))
            start = k + 1
    parts.append((start, len(inner)))
    for a, b in parts:
        attr = re.match(r"\s*([\w$]+)\s*=(?!=)", inner_masked[a:b])
        if attr and attr.group(1) != "value":
            continue
        # Literals blanked by mask(): anything left but quotes, `+` and spaces is a constant
        # or a call, not resolvable from the text.
        code = re.sub(r"^\s*value\s*=", "", inner_masked[a:b])
        if not re.fullmatch(r'[\s"+]*', code):
            return None
        lits = _string_literals(inner[a:b])
        return "".join(lits) if lits else None
    return None


def expression_regex(expr: str, custom: dict[str, str] | None = None) -> str:
    """A Cucumber Expression as an anchored Python regex: `{int}`, `{string}`… parameters,
    `(s)` optional text, `a/b` alternatives within a word, `\\` escapes."""
    custom = custom or {}
    tokens: list[tuple[str, str]] = []   # (kind, value): text, ws, slash, opt, param
    k, n = 0, len(expr)
    while k < n:
        c = expr[k]
        if c == "\\" and k + 1 < n:
            tokens.append(("text", expr[k + 1]))
            k += 2
        elif c.isspace():
            tokens.append(("ws", c))
            k += 1
        elif c == "/":
            tokens.append(("slash", c))
            k += 1
        elif c == "(":
            j, buf = k + 1, ""
            while j < n and expr[j] != ")":
                if expr[j] == "\\" and j + 1 < n:
                    buf += expr[j + 1]
                    j += 2
                    continue
                buf += expr[j]
                j += 1
            tokens.append(("opt", buf))
            k = j + 1
        elif c == "{":
            j = expr.find("}", k)
            if j < 0:
                tokens.append(("text", c))
                k += 1
                continue
            tokens.append(("param", expr[k + 1:j].strip()))
            k = j + 1
        else:
            tokens.append(("text", c))
            k += 1

    def render(toks) -> str:
        out = ""
        for kind, value in toks:
            if kind == "text":
                out += re.escape(value)
            elif kind == "opt":
                out += f"(?:{re.escape(value)})?"
            elif kind == "param":
                out += f"(?:{custom.get(value) or PARAMETERS.get(value, r'.*')})"
            else:  # a slash that is not an alternation (no word around it)
                out += re.escape(value)
        return out

    # Alternation: slashes split the alternatives of a whitespace-delimited stretch.
    out, word = "", []
    for tok in tokens + [("ws", "")]:
        if tok[0] != "ws":
            word.append(tok)
            continue
        if any(t[0] == "slash" for t in word):
            alts, cur = [], []
            for t in word:
                if t[0] == "slash":
                    alts.append(cur)
                    cur = []
                else:
                    cur.append(t)
            alts.append(cur)
            out += "(?:" + "|".join(render(a) for a in alts) + ")"
        else:
            out += render(word)
        word = []
        if tok[1]:
            out += re.escape(tok[1]) if tok[1] != " " else " "
    return out


def java_regex(pattern: str) -> str:
    """A Java regex as Python's: possessive quantifiers made greedy, a few \\p{…} classes."""
    p = re.sub(r"([*+?}])\+", r"\1", pattern)
    for k, v in {r"\p{L}": r"[^\W\d_]", r"\p{Alpha}": r"[^\W\d_]", r"\p{Digit}": r"\d",
                 r"\p{Alnum}": r"[^\W_]", r"\p{Punct}": r"[!-/:-@\[-`{-~]",
                 r"\p{Space}": r"\s", r"\p{Upper}": r"[A-Z]", r"\p{Lower}": r"[a-z]"}.items():
        p = p.replace(k, v)
    return p


def compile_glue(text: str, custom: dict[str, str]) -> tuple[str, re.Pattern | None]:
    is_regex = text.startswith("^") or text.endswith("$")
    try:
        if is_regex:
            body = java_regex(text)
            return "regex", re.compile(body if body.startswith("^") else "^(?:" + body + ")",
                                       re.S)
        return "expression", re.compile("^" + expression_regex(text, custom) + "$", re.S)
    except re.error:
        return "unknown", None


def _source_lines(root: str, source: str, cache: dict) -> list[str]:
    if source not in cache:
        try:
            with open(os.path.join(root, source), encoding="utf-8", errors="replace") as fh:
                cache[source] = fh.read().split("\n")
        except OSError:
            cache[source] = []
    return cache[source]


def glue(g: Graph, root: str) -> list[Glue]:
    """Project step definitions, with their text compiled."""
    sh = g.shards()
    rows = list(g.con.execute("""
        SELECT sa.symbol, sd.symbol, a.shard_id, a.line, d.line FROM annotations a
        JOIN symbols sa ON sa.id = a.annotation_id JOIN symbols sd ON sd.id = a.def_id
        LEFT JOIN defs d ON d.symbol_id = a.def_id AND d.shard_id = a.shard_id
        WHERE sa.symbol LIKE 'io/cucumber/java/%' OR sa.symbol LIKE 'cucumber/api/java/%'"""))
    cache: dict[str, list[str]] = {}
    custom: dict[str, str] = {}
    for ann, sym, shard_id, line, _dline in rows:  # parameter types first
        if ann in PARAMETER_TYPE and shard_id in sh:
            lines = _source_lines(root, sh[shard_id][0], cache)
            regex = annotation_text(lines, line, "ParameterType")
            if regex:
                text = "\n".join(lines[line:line + 12])
                nm = re.search(r'\bname\s*=\s*"([^"]*)"', text)
                custom[nm.group(1) if nm else S.name_of(sym)] = java_regex(regex)
    out, seen = [], set()
    for ann, sym, shard_id, line, dline in rows:
        if not _STEP_ANNOTATION.match(ann) or shard_id not in sh:
            continue
        src = sh[shard_id][0]
        key = (sym, ann, line)
        if key in seen:
            continue
        seen.add(key)
        name = S.name_of(ann)
        text = annotation_text(_source_lines(root, src, cache), line, name)
        if text is None:
            out.append(Glue(sym, src, (dline or line) + 1, name, "", "unknown"))
            continue
        kind, rx = compile_glue(text, custom)
        out.append(Glue(sym, src, (dline or line) + 1, name, text, kind, rx))
    out.sort(key=lambda x: (x.source, x.line))
    return out


# --- features ---------------------------------------------------------------------------------

# Step keywords from Gherkin's language list (MIT), longest first; "* " in every language.
STEP_KEYWORDS = {
    "en": ("Given ", "When ", "Then ", "And ", "But "),
    "es": ("Dado ", "Dada ", "Dados ", "Dadas ", "Cuando ", "Entonces ", "Y ", "E ", "Pero "),
    "pt": ("Dado ", "Dada ", "Dados ", "Dadas ", "Quando ", "Então ", "Entao ", "E ", "Mas "),
    "fr": ("Soit ", "Sachant que ", "Sachant qu'", "Sachant ", "Etant donné que ",
           "Etant donné qu'", "Etant donné ", "Etant donnée ", "Etant donnés ", "Etant données ",
           "Étant donné que ", "Étant donné qu'", "Étant donné ", "Étant donnée ",
           "Étant donnés ", "Étant données ", "Quand ", "Lorsque ", "Lorsqu'", "Alors ",
           "Donc ", "Et que ", "Et qu'", "Et ", "Mais que ", "Mais qu'", "Mais "),
    "de": ("Angenommen ", "Gegeben sei ", "Gegeben seien ", "Wenn ", "Dann ", "Und ", "Aber "),
    "it": ("Dato ", "Data ", "Dati ", "Date ", "Quando ", "Allora ", "E ", "Ma "),
    "nl": ("Gegeven ", "Stel ", "Als ", "Wanneer ", "Dan ", "En ", "Maar "),
}
STEP_KEYWORDS = {k: tuple(sorted(v + ("* ",), key=len, reverse=True))
                 for k, v in STEP_KEYWORDS.items()}
_LANGUAGE = re.compile(r"^\s*#\s*language\s*:\s*([\w-]+)", re.I)


def _keyword(line: str, language: str) -> tuple[str, str] | None:
    """(keyword, text) of a step line in a known language, or None (not a step)."""
    for k in STEP_KEYWORDS.get(language, ()):
        if line.startswith(k):
            return k.strip(), line[len(k):].strip()
    return None


def parse_feature(text: str, source: str) -> Feature | None:
    lines = text.split("\n")
    language = "en"
    for raw in lines:
        if raw.strip() and not raw.strip().startswith("#"):
            break
        m = _LANGUAGE.match(raw)
        if m:
            language = m.group(1).lower()
    known = language in STEP_KEYWORDS
    feat = Feature(source, "", [], language)
    pending_tags: list[str] = []
    rule, rule_tags = "", []
    section = ""                      # background, scenario, outline, examples, rule, feature
    scenario_name, scenario_tags, scenario_line = "", [], 0
    outline_steps: list[tuple[int, str, str]] = []
    examples_tags: list[str] = []
    header: list[str] | None = None
    backgrounds: list[tuple[str, list[Step]]] = []   # (rule, its steps)
    current: Step | None = None
    in_doc = ""

    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if in_doc:  # a docstring: an argument of the previous step
            if line.startswith(in_doc):
                in_doc = ""
            continue
        if line.startswith(('"""', "```")):
            in_doc = line[:3]
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            pending_tags += [t for t in line.split() if t.startswith("@")]
            continue
        m = _SECTION_LINE.match(raw)
        kind = _SECTION_OF.get(m.group(1).strip().lower()) if m else None
        if kind:
            tags, pending_tags = pending_tags, []
            if kind == "feature":
                feat.name, feat.tags, section = m.group(2).strip(), tags, "feature"
            elif kind == "rule":
                rule, rule_tags, section = m.group(2).strip(), tags, "rule"
            elif kind == "background":
                section = "background"
                backgrounds.append((rule, []))
            elif kind in ("scenario", "outline"):
                section = kind
                scenario_name, scenario_tags, scenario_line = m.group(2).strip(), tags, i
                outline_steps = []
                if kind == "scenario":
                    feat.scenarios.append(Scenario(source, i, scenario_name,
                                                   feat.tags + rule_tags + tags, rule, feat.name,
                                                   i))
            elif kind == "examples":
                section, header, examples_tags = "examples", None, tags
            current = None
            continue
        if line.startswith("|"):  # a data table (an argument), or an Examples row
            if section == "examples" and outline_steps:
                cells = [c.strip() for c in line.strip("|").split("|")]
                if header is None:
                    header = cells
                    continue
                row = dict(zip(header, cells))

                def fill(t: str) -> str:
                    return re.sub(r"<([^<>]+)>", lambda x: row.get(x.group(1), x.group(0)), t)
                feat.scenarios.append(Scenario(
                    source, i, fill(scenario_name),
                    feat.tags + rule_tags + scenario_tags + examples_tags, rule, feat.name,
                    scenario_line))
                idx = len(feat.scenarios) - 1
                for ln, kw, txt in outline_steps:
                    feat.steps.append(Step(source, ln, kw, fill(txt), [idx]))
            continue
        if section not in ("background", "scenario", "outline"):
            continue  # a description line
        kt = _keyword(line, language) if known else ("", line)
        if kt is None:
            continue  # a description line under the scenario's title
        kw, txt = kt
        if section == "outline":
            outline_steps.append((i, kw, txt))
        else:
            current = Step(source, i, kw, txt)
            if section == "scenario":
                current.pickles = [len(feat.scenarios) - 1]
                feat.steps.append(current)
            else:
                backgrounds[-1][1].append(current)
                feat.steps.append(current)
    # A Background's steps run in every scenario of its scope (the feature, or its Rule).
    for bg_rule, bg_steps in backgrounds:
        idx = [k for k, sc in enumerate(feat.scenarios) if not bg_rule or sc.rule == bg_rule]
        for st in bg_steps:
            st.pickles = idx
    return feat if feat.name or feat.scenarios else None


def features(root: str) -> list[Feature]:
    out: list[Feature] = []
    for p in candidate_files(root, ["*.feature"]):
        try:
            with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                f = parse_feature(fh.read(), p)
        except OSError:
            continue
        if f is not None:
            out.append(f)
    return out


def matcher(glues: list[Glue]):
    """step -> the glue methods that run it. A step whose keyword is known is matched as is;
    in an unknown language, without its first one, two or three words (the shortest cut that
    matches)."""
    usable = [x for x in glues if x.regex is not None]
    cache: dict[tuple[str, str], list[Glue]] = {}

    def match(step: Step) -> list[Glue]:
        key = (step.keyword, step.text)
        if key in cache:
            return cache[key]
        if step.keyword:
            found = [x for x in usable if x.regex.fullmatch(step.text)]
        else:
            found = []
            for k in (1, 2, 3):
                parts = step.text.split(None, k)
                if len(parts) <= k:
                    break
                found = [x for x in usable if x.regex.fullmatch(parts[k])]
                if found:
                    break
        cache[key] = found
        return found
    return match


def pickles_at(feat: Feature, lines: set[int]) -> set[int]:
    """The pickles whose scenario contains one of these lines (1-based); a line before the
    first scenario (the feature's header, tags or Background) affects them all."""
    headers = sorted({sc.header for sc in feat.scenarios})
    out: set[int] = set()
    for ln in lines:
        before = [h for h in headers if h <= ln]
        if not before:
            return set(range(len(feat.scenarios)))
        out |= {k for k, sc in enumerate(feat.scenarios) if sc.header == before[-1]}
    return out


def text_steps(text: str) -> list[Step]:
    """A step typed by hand: with a keyword of any known language, or without one."""
    out = []
    for lang in STEP_KEYWORDS:
        kt = _keyword(text.strip(), lang)
        if kt:
            out.append(Step("", 0, kt[0], kt[1]))
    out.append(Step("", 0, "*", text.strip()))   # the text alone
    return out


# --- running the affected scenarios -------------------------------------------------------------

def own_tags(feats: list[Feature]) -> dict[str, str]:
    """{feature source: a feature-level tag no other feature uses}."""
    count: dict[str, int] = {}
    for f in feats:
        for t in set(f.tags) | {t for sc in f.scenarios for t in sc.tags}:
            count[t] = count.get(t, 0) + 1
    out = {}
    for f in feats:
        unique = [t for t in f.tags if count.get(t) == 1]
        if unique:
            out[f.source] = unique[0]
    return out


def name_filter(names: list[str]) -> str:
    """An anchored Java regex matching exactly these scenario (pickle) names."""
    def q(n: str) -> str:
        return "\\Q" + n.replace("\\E", "\\E\\\\E\\Q") + "\\E"
    return "^(" + "|".join(q(n) for n in sorted(set(names))) + ")$"


MAX_NAMED = 150  # scenarios a name filter lists at most


def run_commands(affected: dict[str, set[int]], feats: list[Feature], run: str,
                 run_features: str, gradle: str = "gradle") -> list[str]:
    """Commands to run the affected pickles ({feature source: pickle indexes}). When every
    affected feature is affected whole and has a tag of its own, `run_features` with {tags};
    otherwise `run` with {names} (an anchored regex of the scenario names) or {features}
    (path:line of each)."""
    by_source = {f.source: f for f in feats}
    own = own_tags(feats)
    whole = all(len(idx) == len(by_source[src].scenarios) and src in own
                for src, idx in affected.items())
    if whole and run_features:
        tags = " or ".join(own[src] for src in sorted(affected))
        return [_fill(run_features, tags=tags)]
    total = sum(len(v) for v in affected.values())
    if total > MAX_NAMED:  # a name filter that long is no use
        if run_features and all(src in own for src in affected):
            tags = " or ".join(own[src] for src in sorted(affected))
            return [_fill(run_features, tags=tags) + f"   ({len(affected)} whole features)"]
        return [f"{total} scenarios in {len(affected)} features: run the whole Cucumber suite"]
    names, locs = [], []
    for src, idx in sorted(affected.items()):
        for k in sorted(idx):
            sc = by_source[src].scenarios[k]
            names.append(sc.name)
            locs.append(f"{src}:{sc.line}")
    if run:
        return [_fill(run, names=name_filter(names), features=",".join(locs),
                      tags=" or ".join(own.get(s, "") for s in sorted(affected) if s in own))]
    return [f"{gradle} test -Dcucumber.features={','.join(locs)}   (the test task that runs "
            "Cucumber; or set [cucumber] run = \"… {names}\" / run_features = \"… {tags}\")"]


def _fill(template: str, **values: str) -> str:
    out = template
    for k, v in values.items():
        if "'{" + k + "}'" in out:  # inside single quotes for the shell
            v = v.replace("'", "'\\''")
        out = out.replace("{" + k + "}", v)
    return out

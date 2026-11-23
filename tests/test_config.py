import os

from scipq_mcp import config as CFG
from scipq_mcp.graph import Graph
from scipq_mcp.index import Index
from scipq_mcp.service import Service
from scipq_mcp.workspace import Workspace


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def test_builtin_profiles_and_symbols():
    profiles = CFG.builtin_profiles()
    assert {"junit", "spring", "jaxrs", "vaadin", "cucumber", "jpa", "jdo"} <= set(profiles)
    assert CFG.to_symbol("org.junit.jupiter.api.Test") == "org/junit/jupiter/api/Test#"
    assert CFG.to_symbol("lombok/Getter#") == "lombok/Getter#"


def test_match_precedence(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    (root / "settings.gradle").write_text("rootProject.name = 'shop'\n")
    home = tmp_path / "home"
    write(str(home / "config.toml"), f'''
[[project]]
match = {{ root_project = "shop" }}
profiles = ["jpa"]

[[project]]
match = {{ path = "{root}" }}
profiles = ["spring", "junit"]
[project.tests]
filter = "./t.sh {{task}} --tests '{{fqcn}}'"

[[project]]
match = {{ root_project = "other" }}
profiles = ["vaadin"]
bogus = 1
''')
    st = CFG.settings_for(str(root), home=str(home))
    assert st.profiles == ["spring", "junit"]          # path beats root_project
    assert "path =" in st.source
    assert st.tests_filter == "./t.sh {task} --tests '{fqcn}'"
    assert "org/junit/jupiter/api/Test#" in st.entry_annotations
    assert any("unknown keys ['bogus']" in w for w in st.warnings)
    (home / "config.toml").write_text('[[project]]\nmatch = { root_project = "shop" }\n'
                                      'profiles = ["jpa", "nope"]\n')
    st = CFG.settings_for(str(root), home=str(home))
    assert st.profiles == ["jpa", "nope"] and any("unknown profile 'nope'" in w
                                                  for w in st.warnings)
    assert "jakarta/persistence/Entity#" in st.entity_annotations


def test_user_profile_overrides_builtin(tmp_path):
    home = tmp_path / "home"
    write(str(home / "profiles" / "junit.toml"), '[profile]\nname = "junit"\n'
          '[[entry_points]]\nannotations = ["com.example.MyTest"]\n')
    profiles, warnings = CFG.load_profiles(str(home))
    assert profiles["junit"]["entry_points"][0]["annotations"] == ["com.example.MyTest"]
    assert warnings == []


def test_root_project_from_pom(tmp_path):
    (tmp_path / "pom.xml").write_text("<project><parent><artifactId>parent</artifactId></parent>"
                                      "<artifactId>billing</artifactId></project>")
    assert CFG.root_project_name(str(tmp_path)) == "billing"


def test_auto_profiles_and_dead_code(sample_root, tmp_path):
    ix = Index(Workspace(sample_root), str(tmp_path / "cache"))
    ix.update()
    g = Graph(ix)
    present = g.annotations_present()
    g.close()
    st = CFG.settings_for(sample_root, home=str(tmp_path / "nohome"),
                          present_annotations=present)
    assert st.source.startswith("auto") and st.profiles == []  # no framework in the fixture
    svc = Service(sample_root, cache_dir=str(tmp_path / "cache"),
                  config_file=str(tmp_path / "missing.toml"))
    assert "-- profiles: none" in svc.dead_code()


def test_tests_filter_template(sample_root, tmp_path):
    conf = tmp_path / "c.toml"
    conf.write_text(f'[[project]]\nmatch = {{ path = "{sample_root}" }}\n'
                    "[project.tests]\nfilter = \"./run-tests.sh {task} --tests '{fqcn}'\"\n")
    out = Service(sample_root, config_file=str(conf), cache_dir=str(tmp_path / "cache")
                  ).tests("Entity#setState")
    assert "./run-tests.sh :app:test --tests 'com.example.app.ServiceTest'" in out


def test_repo_config_and_precedence(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (root / ".scipq.toml").write_text('''
profiles = ["spring", "junit"]
[tests]
filter = "./gradlew {module}:test --tests '{fqcn}'"
[by_name]
globs = ["*.jrxml", "/etc/*", "../other/*", "src/**/*.xml"]
''')
    st = CFG.settings_for(str(root), home=str(home))
    assert st.profiles == ["spring", "junit"] and st.source == ".scipq.toml in the checkout"
    assert st.tests_filter_from == ".scipq.toml"
    assert "*.jrxml" in st.by_name_globs and "src/**/*.xml" in st.by_name_globs
    assert "/etc/*" not in st.by_name_globs and "../other/*" not in st.by_name_globs
    assert sum("must be relative" in w for w in st.warnings) == 2
    assert st.files == [".scipq.toml (the team's, from the checkout)"]
    # The personal [[project]] entry overrides it, key by key.
    write(str(home / "config.toml"), f'''
[[project]]
match = {{ path = "{root}" }}
[project.tests]
filter = "./mine.sh {{fqcn}}"
''')
    st = CFG.settings_for(str(root), home=str(home))
    assert st.profiles == ["spring", "junit"]              # still the team's
    assert st.tests_filter == "./mine.sh {fqcn}" and "(personal)" in st.files[-1]
    lines = "\n".join(__import__("scipq_mcp.render", fromlist=["x"]).settings_lines(st))
    assert "configuration (later ones win): .scipq.toml" in lines and "(from " in lines


def test_repo_config_is_untrusted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (root / ".scipq.toml").write_text('match = { path = "/" }\ncache_dir = "/tmp/x"\n'
                                      'profiles = ["junit"]\n[by_name]\npatterns = ['
                                      + ", ".join(f'"p{k}"' for k in range(50)) + "]\n"
                                      '[tests]\nfilter = "' + "x" * 1000 + '"\n')
    st = CFG.settings_for(str(root), home=str(home))
    w = "\n".join(st.warnings)
    assert "`match` is ignored" in w and "unknown key 'cache_dir'" in w
    assert len(st.by_name_patterns) == CFG.REPO_MAX_PATTERNS
    assert st.tests_filter == "" and "longer than" in w
    outside = tmp_path / "outside.toml"
    outside.write_text('profiles = ["jdo"]\n')
    (root / ".scipq.toml").unlink()
    (root / ".scipq.toml").symlink_to(outside)
    st = CFG.settings_for(str(root), home=str(home))
    assert st.profiles == [] and any("link out of the checkout" in x for x in st.warnings)


def test_setup_shows_the_configuration(sample_copy, tmp_path):
    with open(os.path.join(sample_copy, ".scipq.toml"), "w") as fh:
        fh.write('profiles = ["junit"]\n')
    out = Service(sample_copy, cache_dir=str(tmp_path / "c")).setup(include_template=False)
    assert "scipq configuration:" in out and "profiles: junit — .scipq.toml" in out


def test_dead_code_exclude(sample_copy, tmp_path):
    with open(os.path.join(sample_copy, ".scipq.toml"), "w") as fh:
        fh.write('[dead_code]\nexclude = ["core/src/main/java/com/example/model/Point*", '
                 '"/abs/*"]\n')
    st = CFG.settings_for(sample_copy, home=str(tmp_path / "home"))
    assert st.dead_code_exclude == ["core/src/main/java/com/example/model/Point*"]
    assert any("dead_code.exclude" in w and "/abs/*" in w for w in st.warnings)
    svc = Service(sample_copy, cache_dir=str(tmp_path / "c"))
    out = svc.dead_code()
    assert "Point#scaled()" not in out
    assert "unreferenced definitions in [dead_code] exclude paths left out" in out


def test_a_string_where_a_list_is_expected_is_one_item(tmp_path):
    from scipq_mcp import config as CFG
    out, warnings = CFG.sanitize_repo_config({"dead_code": {"exclude": "gen/*"},
                                              "by_name": {"globs": "*.xml"}})
    assert out["dead_code"]["exclude"] == ["gen/*"]
    assert out["by_name"]["globs"] == ["*.xml"]


def test_rules_with_supertypes_and_type_names_are_flagged(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / ".scipq.toml").write_text(
        '[[entry_points]]\nkind = "screen"\nsupertypes = ["com.example.Base"]\n'
        'type_names = ["*Screen"]\n')
    s = CFG.settings_for(str(root), home=str(tmp_path / "home"))
    assert any("'screen' gives supertypes and type_names" in w and "split it" in w
               for w in s.warnings)

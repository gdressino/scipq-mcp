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

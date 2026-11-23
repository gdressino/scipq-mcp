import subprocess
import sys


def test_gradle_init_template():
    out = subprocess.run([sys.executable, "-m", "scipq_mcp", "gradle-init"],
                         capture_output=True, text=True, check=True).stdout
    assert "-Xplugin:scip" in out and "build/scip-targetroot" in out


def test_query(sample_root):
    out = subprocess.run([sys.executable, "-m", "scipq_mcp", "query", "--root", sample_root,
                          "impl", "Entity"], capture_output=True, text=True, check=True).stdout
    assert "3 subtypes (2 direct)" in out


def test_init_config_prints_a_template(sample_root):
    import tomllib
    out = subprocess.run([sys.executable, "-m", "scipq_mcp", "init-config", "--root",
                          sample_root], capture_output=True, text=True, check=True).stdout
    assert tomllib.loads(out)["profiles"] == []   # the sample uses no framework annotations
    assert "Nothing here is executed" in out

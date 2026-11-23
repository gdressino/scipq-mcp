import os
import shutil

import pytest

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample")


@pytest.fixture(scope="session")
def sample_root() -> str:
    """The fixture project, read-only."""
    return FIXTURE


@pytest.fixture
def sample_copy(tmp_path) -> str:
    """A writable copy of the fixture project, with shards newer than their sources (as after
    a fresh compilation)."""
    dst = tmp_path / "sample"
    shutil.copytree(FIXTURE, dst)
    shards = dst / "build" / "scip-targetroot"
    for d, _, files in os.walk(dst):
        for n in files:
            path = os.path.join(d, n)
            os.utime(path, (1_700_000_000, 1_700_000_000))
    for d, _, files in os.walk(shards):
        for n in files:
            os.utime(os.path.join(d, n), (1_700_000_100, 1_700_000_100))
    return str(dst)


CUCUMBER = os.path.join(os.path.dirname(__file__), "fixtures", "cucumber")


@pytest.fixture
def cucumber_root(tmp_path) -> str:
    """A writable copy of the Cucumber fixture, shards newer than their sources."""
    dst = tmp_path / "cucumber"
    shutil.copytree(CUCUMBER, dst)
    for d, _, files in os.walk(dst):
        for n in files:
            os.utime(os.path.join(d, n), (1_700_000_000, 1_700_000_000))
    for d, _, files in os.walk(dst / "build" / "scip-targetroot"):
        for n in files:
            os.utime(os.path.join(d, n), (1_700_000_100, 1_700_000_100))
    return str(dst)


REFACTOR = os.path.join(os.path.dirname(__file__), "fixtures", "refactor")


def _fresh_copy(src: str, dst) -> str:
    shutil.copytree(src, dst)
    for d, _, files in os.walk(dst):
        for n in files:
            os.utime(os.path.join(d, n), (1_700_000_000, 1_700_000_000))
    for d, _, files in os.walk(dst / "build" / "scip-targetroot"):
        for n in files:
            os.utime(os.path.join(d, n), (1_700_000_100, 1_700_000_100))
    return str(dst)


@pytest.fixture
def refactor_root(tmp_path) -> str:
    """A writable copy of the refactoring fixture, shards newer than their sources."""
    return _fresh_copy(REFACTOR, tmp_path / "refactor")


@pytest.fixture
def javaparser_cache(tmp_path) -> str:
    """A cache directory whose tools hold a JavaParser jar (SCIPQ_TEST_JAVAPARSER, or one
    found on this machine), for the refactoring tests; skipped without one or a JDK."""
    from scipq_mcp import jvm

    try:
        jvm.find_jdk()
    except jvm.ToolError:
        pytest.skip("no JDK 11+ with javac")
    jar = os.environ.get("SCIPQ_TEST_JAVAPARSER", "")
    if not jar:
        found = [j for j in jvm.find_jars() if jvm._version_key(j.version) >=
                 jvm._version_key("3.28.0")]
        jar = found[0].path if found else ""
    if not jar or not os.path.isfile(jar):
        pytest.skip("no JavaParser 3.28+ (set SCIPQ_TEST_JAVAPARSER to its jar)")
    base = tmp_path / "cache"
    tools = base / "tools"
    tools.mkdir(parents=True, mode=0o700)
    os.chmod(base, 0o700)
    shutil.copy(jar, tools / os.path.basename(jar))
    return str(base)

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

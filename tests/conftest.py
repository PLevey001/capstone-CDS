from pathlib import Path

import pytest

from tests.filesystem_fixtures import missing_tools


def pytest_addoption(parser):
    parser.addoption("--require-image-tests", action="store_true",
                     help="Fail when required image tools/fixtures are missing or any test skips")


def pytest_sessionstart(session):
    if not session.config.getoption("--require-image-tests"):
        return
    missing = missing_tools()
    for name in ("test_filesystems.py", "fixtures/populate_ntfs.c"):
        if not (Path(__file__).parent / name).is_file():
            missing.append(name)
    if missing:
        raise pytest.UsageError("Required image tests cannot run; missing: " + ", ".join(missing))


def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption("--require-image-tests"):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter and reporter.stats.get("skipped"):
            reporter.write_sep("=", "Required image-test job must not skip tests")
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

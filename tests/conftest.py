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


IMAGE_TEST_MODULES = ("test_filesystems.py", "test_image_history.py", "test_image_registry.py", "test_image_evtx.py", "test_pipeline.py", "test_history.py")


def pytest_sessionfinish(session, exitstatus):
    if not session.config.getoption("--require-image-tests"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if not reporter:
        return
    # Scope the no-skip rule to the image tests this gate exists to protect. An
    # unrelated skip elsewhere is not evidence that image coverage was lost.
    skipped = set()
    for entry in reporter.stats.get("skipped", []):
        # pytest records a skip as the report itself or as (report, ...).
        report = entry[0] if isinstance(entry, tuple) else entry
        nodeid = getattr(report, "nodeid", "")
        if any(module in nodeid for module in IMAGE_TEST_MODULES):
            skipped.add(nodeid)
    if skipped:
        reporter.write_sep("=", "Required image tests must not skip: " + ", ".join(sorted(skipped)))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

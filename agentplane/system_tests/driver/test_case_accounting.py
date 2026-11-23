"""Exercise batch deadlines and JUnit accounting through pytest's real fixture phases."""

from collections.abc import Sequence
from typing import Literal
from xml.etree import ElementTree

import pytest
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ("pytester",)

# Nested pytest loads the real system-test hooks by module name.
# gazelle:include_dep //agentplane/system_tests:conftest
# gazelle:include_dep //agentplane/system_tests/driver:case_batch
# gazelle:include_dep @pypi//pytest_asyncio


def _run(
    pytester: pytest.Pytester,
    runfiles: Runfiles,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["pass", "case_timeout", "setup_failure", "cleanup_failure"],
    *,
    selection: str | None = None,
) -> tuple[Sequence[pytest.TestReport], ElementTree.Element]:
    # Exercise the production process-budget watchdog as well as CaseBatch. A broken
    # scenario timer must fail this nested run rather than hang the outer Bazel test.
    monkeypatch.setenv("TEST_TIMEOUT", "10")
    root_conftest = (runfiles.root() / "ducktape/conftest.py").read_text()
    pytester.makeconftest(root_conftest + '\npytest_plugins += ("agentplane.system_tests.conftest",)\n')
    pytester.makepyfile(
        test_scenarios=f"""
import asyncio
import time
from contextlib import nullcontext
from functools import partial

import pytest
import pytest_asyncio

from agentplane.system_tests.driver.case_batch import Case, CaseBatch, selected_items

OUTCOME = {outcome!r}
pytestmark = [
    pytest.mark.case_batch,
    pytest.mark.case_timeout(seconds=1) if OUTCOME == "case_timeout" else pytest.mark.case_timeout(1),
    pytest.mark.asyncio(loop_scope="module"),
]

@pytest.fixture(scope="module")
def shared_stack():
    # Deliberately longer than the scenario budget: startup must be suite work.
    time.sleep(1.2)
    if OUTCOME == "setup_failure":
        raise RuntimeError("shared setup failed")
    yield
    time.sleep(0.05)
    if OUTCOME == "cleanup_failure":
        raise RuntimeError("shared cleanup failed")

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def cases(request, shared_stack):
    finished = set()

    async def scenario(name, _worker):
        try:
            if OUTCOME == "case_timeout" and name == "test_first":
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(0.05)
        finally:
            finished.add(name)

    selected = selected_items(request)
    callbacks = {{item.nodeid: Case.for_item(item, partial(scenario, item.name)) for item in selected}}
    async with CaseBatch.running(callbacks, limit=2, worker_scope=nullcontext) as batch:
        yield batch
    assert finished == {{item.name for item in selected}}

async def test_first(cases, request):
    await cases.report(request)

async def test_second(cases, request):
    await cases.report(request)
"""
    )
    junit = undeclared_outputs_dir() / f"accounting-{outcome}-{selection or 'both'}.xml"
    arguments = ["--import-mode=importlib", "-o", "junit_duration_report=call", "--durations=0", f"--junitxml={junit}"]
    if selection is not None:
        arguments += ["-k", selection]
    recorder = pytester.inline_run(*arguments)
    reports = recorder.getreports("pytest_runtest_logreport")
    return reports, ElementTree.parse(junit).getroot()


@pytest.mark.parametrize("selection", [None, "second"], ids=["both-cases", "selected-case"])
def test_shared_phases_are_excluded_from_case_time(
    pytester: pytest.Pytester, runfiles: Runfiles, monkeypatch: pytest.MonkeyPatch, selection: str | None
) -> None:
    reports, xml = _run(pytester, runfiles, monkeypatch, "pass", selection=selection)
    assert all(report.passed for report in reports)
    calls = {report.nodeid.rsplit("::", 1)[-1]: report for report in reports if report.when == "call"}
    expected = {"test_second"} if selection else {"test_first", "test_second"}
    assert calls.keys() == expected
    setup = max(report.duration for report in reports if report.when == "setup")
    teardown = max(report.duration for report in reports if report.when == "teardown")
    assert setup >= 1
    assert teardown >= 0.04
    cases = {case.attrib["name"]: case for case in xml.findall(".//testcase")}
    assert cases.keys() == expected
    for name, report in calls.items():
        assert 0.04 <= report.duration < setup
        assert float(cases[name].attrib["time"]) == pytest.approx(report.duration, abs=0.001)
    suite = xml.find("testsuite")
    assert suite is not None
    assert float(suite.attrib["time"]) >= setup + teardown


def test_case_deadline_still_expires_after_shared_setup(
    pytester: pytest.Pytester, runfiles: Runfiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports, xml = _run(pytester, runfiles, monkeypatch, "case_timeout")
    calls = [report for report in reports if report.when == "call"]
    assert [report.outcome for report in calls] == ["failed", "passed"]
    failure = xml.find(".//failure")
    assert failure is not None
    assert "TimeoutError" in failure.attrib["message"]


@pytest.mark.parametrize("outcome", ["setup_failure", "cleanup_failure"])
def test_shared_phase_failure_still_fails_the_suite(
    pytester: pytest.Pytester,
    runfiles: Runfiles,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["setup_failure", "cleanup_failure"],
) -> None:
    reports, xml = _run(pytester, runfiles, monkeypatch, outcome)
    phase = "setup" if outcome == "setup_failure" else "teardown"
    assert any(report.failed and report.when == phase for report in reports)
    if outcome == "setup_failure":
        assert not any(report.when == "call" for report in reports)
    else:
        assert all(report.passed for report in reports if report.when == "call")
    errors = xml.findall(".//error")
    assert errors
    assert any(
        f"shared {phase if phase == 'setup' else 'cleanup'} failed" in error.attrib["message"] for error in errors
    )

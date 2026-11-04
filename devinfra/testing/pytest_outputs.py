"""Pytest binding for framework-neutral test output identities."""

from pathlib import Path

import pytest

from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def case_id_for_pytest_item(test_path: Path, nodeid: str, *, repo_root: Path) -> str:
    """Use the real source path and retain pytest's class and parameter IDs."""
    try:
        test_file = test_path.resolve().relative_to(repo_root.resolve())
    except ValueError as error:
        raise ValueError(f"pytest case is outside the repository: {test_path}") from error
    return "::".join((test_file.as_posix(), *nodeid.split("::")[1:]))


@pytest.fixture
def test_outputs(request: pytest.FixtureRequest) -> BoundTestOutputs:
    """Bind full repository-relative pytest node ID to the test's output directory."""
    assert isinstance(request.node, pytest.Item)
    return BoundTestOutputs(
        output_dir=undeclared_outputs_dir(),
        case_id=case_id_for_pytest_item(
            request.node.path, request.node.nodeid, repo_root=Path(__file__).resolve().parents[2]
        ),
    )

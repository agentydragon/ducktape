"""Pytest binding for framework-neutral test output identities."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from util.testing.output_writer import publish_file, publish_output
from util.testing.undeclared_outputs import undeclared_outputs_dir


@dataclass(frozen=True)
class BoundTestOutputs:
    output_dir: Path
    case_id: str

    def publish(
        self,
        *,
        checkpoint: str | None = None,
        role: str,
        data: bytes,
        suffix: str,
        label: str | None = None,
        title: str | None = None,
    ) -> Path:
        return publish_output(
            self.output_dir,
            case_id=self.case_id,
            checkpoint=checkpoint,
            role=role,
            data=data,
            suffix=suffix,
            label=label,
            title=title,
        )

    def publish_file(
        self,
        *,
        source: Path,
        role: str,
        checkpoint: str | None = None,
        label: str | None = None,
        title: str | None = None,
    ) -> Path:
        return publish_file(
            self.output_dir,
            case_id=self.case_id,
            checkpoint=checkpoint,
            role=role,
            source=source,
            label=label,
            title=title,
        )


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


@pytest.fixture
def case_id(test_outputs: BoundTestOutputs) -> str:
    return test_outputs.case_id

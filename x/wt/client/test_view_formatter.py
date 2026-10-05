from collections.abc import Callable

import pytest
import pytest_bazel

from x.wt.client.view_formatter import ViewFormatter
from x.wt.shared.protocol import CommitInfo, StatusResult


@pytest.fixture
def status_with_hash(
    sample_status_result: StatusResult, sample_commit_info: CommitInfo
) -> Callable[[str], StatusResult]:
    def _build(short_hash: str) -> StatusResult:
        commit_info = sample_commit_info.model_copy(update={"short_hash": short_hash})
        return sample_status_result.model_copy(update={"commit_info": commit_info})

    return _build


# Columns whose every cell looks like a number (`562161e0`, `2.10`) must still print as typed.
@pytest.mark.parametrize(
    ("names", "short_hashes"),
    [
        pytest.param(["alpha", "beta"], ["562161e0", "562161e0"], id="exponent_form_hashes"),
        pytest.param(["alpha", "beta"], ["00123456", "562161e0"], id="digit_only_hash_beside_exponent_form"),
        pytest.param(["1e5", "2.10"], ["abcdef12", "abcdef12"], id="float_like_names"),
    ],
)
def test_status_table_prints_names_and_hashes_verbatim(capsys, status_with_hash, names, short_hashes):
    items = [(name, status_with_hash(short_hash)) for name, short_hash in zip(names, short_hashes, strict=True)]

    ViewFormatter(github_repo=None).render_worktree_status_all(items)

    printed = [line.split()[:2] for line in capsys.readouterr().out.splitlines()]
    assert printed == [[name, short_hash] for name, short_hash in zip(names, short_hashes, strict=True)]


if __name__ == "__main__":
    pytest_bazel.main()

import json
from pathlib import Path

import pytest
import pytest_bazel

from x.agentplane.native.process import NativeProcess


def test_invalid_native_stdout_is_not_ignored(tmp_path: Path) -> None:
    process = NativeProcess(tmp_path, [], cwd=tmp_path, environment={})
    process.frames.put("not-json")
    with pytest.raises(json.JSONDecodeError):
        process.await_frame(lambda _frame: True, timeout=0.1)


if __name__ == "__main__":
    pytest_bazel.main()

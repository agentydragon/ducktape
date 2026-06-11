from __future__ import annotations

import pytest
import pytest_bazel

from arg0_runner.unified_patch import apply_unified_diff


class MemFS:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})

    def open_fn(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_fn(self, path: str, content: str) -> None:
        self.files[path] = content

    def remove_fn(self, path: str) -> None:
        if path in self.files:
            del self.files[path]


def test_update_single_hunk() -> None:
    fs = MemFS({"foo.txt": "line1\nline2\nline3\n"})
    patch = "--- a/foo.txt\n+++ b/foo.txt\n@@ -1,3 +1,3 @@\n line1\n-line2\n+LINE2\n line3\n"
    apply_unified_diff(patch, fs.open_fn, fs.write_fn, fs.remove_fn)
    assert fs.files["foo.txt"] == "line1\nLINE2\nline3\n"


def test_add_new_file() -> None:
    fs = MemFS({})
    patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    apply_unified_diff(patch, fs.open_fn, fs.write_fn, fs.remove_fn)
    assert fs.files["new.txt"] == "hello\nworld\n"


def test_delete_file() -> None:
    fs = MemFS({"old.txt": "a\nb\n"})
    patch = "--- a/old.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    apply_unified_diff(patch, fs.open_fn, fs.write_fn, fs.remove_fn)
    assert "old.txt" not in fs.files


def test_multiple_files_update_and_add() -> None:
    fs = MemFS({"foo.txt": "x\ny\n"})
    patch = (
        "diff --git a/foo.txt b/foo.txt\n"
        "index 0000000..1111111 100644\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " x\n"
        "-y\n"
        "+Y\n"
        "diff --git a/bar.txt b/bar.txt\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        "+++ b/bar.txt\n"
        "@@ -0,0 +1,1 @@\n"
        "+BAR\n"
    )
    apply_unified_diff(patch, fs.open_fn, fs.write_fn, fs.remove_fn)
    assert fs.files["foo.txt"] == "x\nY\n"
    assert fs.files["bar.txt"] == "BAR\n"


def test_context_mismatch_raises() -> None:
    fs = MemFS({"foo.txt": "a\nb\n"})
    bad_patch = (
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " z\n"  # wrong context
        "-a\n"
        "+A\n"
    )
    with pytest.raises(ValueError, match=r"context.*mismatch|patch.*failed|apply.*failed"):
        apply_unified_diff(bad_patch, fs.open_fn, fs.write_fn, fs.remove_fn)


if __name__ == "__main__":
    pytest_bazel.main()

"""Exercise the actual guest AWK parser on function-graph syntax and failure cases."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

_HEADER = "# tracer: function_graph\n# CPU  DURATION                  FUNCTION CALLS\n"
_ROOT = " 0)               |  cgroup_subtree_control_write() {\n"
_CLOSE = " 0) + 42.000 us   |  } /* cgroup_subtree_control_write */\n"
_NESTED = (
    " 0)               |    cgroup_apply_control() {\n"
    " 0)               |      cgroup_update_dfl_csses() {\n"
    " 0)               |        percpu_down_write() {\n"
    " 0)   2.000 us    |          rcu_sync_enter();\n"
    " 0) + 11.000 us   |        } /* percpu_down_write */\n"
    " 0) + 15.000 us   |      } /* cgroup_update_dfl_csses */\n"
    " 0) + 25.000 us   |    } /* cgroup_apply_control */\n"
)


def _parse(tmp_path: Path, trace: str, *, integrity: str = "clean") -> list[str]:
    runfiles = Runfiles.CreateOrRaise()
    busybox_tar = runfiles.Rlocation(os.environ["CGROUP_PILOT_BUSYBOX_RLOCATION"])
    script = runfiles.Rlocation(os.environ["CGROUP_PILOT_TRACE_AWK_RLOCATION"])
    assert busybox_tar is not None
    assert script is not None
    busybox = tmp_path / "busybox"
    with tarfile.open(busybox_tar) as archive:
        member = next(member for member in archive if member.name.endswith("bin/busybox"))
        member_data = archive.extractfile(member)
        assert member_data is not None
        busybox.write_bytes(member_data.read())
    busybox.chmod(0o755)
    source = tmp_path / "trace"
    source.write_text(trace)
    result = subprocess.run(
        [str(busybox), "awk", "-v", f"integrity={integrity}", "-f", str(script), str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_closed_nested_path_uses_one_root_and_real_graph_leaf_syntax(tmp_path: Path) -> None:
    unrelated = _ROOT + " 0)   1.000 us    |    cgroup_apply_control();\n" + _CLOSE
    records = _parse(tmp_path, _HEADER + unrelated + _ROOT + _NESTED + _CLOSE)
    assert len(records) == 5
    assert records[0].startswith("status=proven tree=2 root_start=6 root_end=14 ")
    assert (
        "path=cgroup_subtree_control_write>cgroup_apply_control>cgroup_update_dfl_csses>percpu_down_write>rcu_sync_enter"
        in records[0]
    )
    assert [record.split()[0] for record in records[1:]] == [
        "context=root",
        "context=update",
        "context=lock",
        "context=rcu",
    ]


def test_sibling_or_different_root_calls_do_not_make_a_nested_path(tmp_path: Path) -> None:
    sibling = (
        _ROOT
        + " 0)   1.000 us    |    cgroup_update_dfl_csses();\n"
        + " 0)               |    percpu_down_write() {\n"
        + " 0)   1.000 us    |      rcu_sync_enter();\n"
        + " 0) + 2.000 us    |    } /* percpu_down_write */\n"
        + _CLOSE
    )
    assert _parse(tmp_path, sibling + _ROOT + " 0)   1.000 us    |    rcu_sync_enter();\n" + _CLOSE) == [
        "status=unproven roots=2 incomplete_root=no too_deep=no path_too_long=no reason=no_closed_nested_path"
    ]


def test_unclosed_root_and_overrun_cannot_prove_path(tmp_path: Path) -> None:
    complete = _HEADER + _ROOT + _NESTED + _CLOSE
    assert "status=unproven" in _parse(tmp_path, complete.removesuffix(_CLOSE))[0]
    assert "incomplete_root=yes" in _parse(tmp_path, complete.removesuffix(_CLOSE))[0]
    assert _parse(tmp_path, complete, integrity="cpu0:overflow_or_drop") == [
        "status=unproven reason=trace_integrity_cpu0:overflow_or_drop"
    ]


if __name__ == "__main__":
    pytest_bazel.main()

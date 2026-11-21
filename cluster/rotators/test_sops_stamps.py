import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_bazel

from cluster.rotators.sops_stamps import remaining_hours, unencrypted_stamps


def test_unencrypted_stamps_absent_or_empty_file_is_empty(tmp_path: Path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert unencrypted_stamps(tmp_path / "absent.yaml") == {}
    assert unencrypted_stamps(empty) == {}


def test_remaining_hours_unstamped_file_is_none(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text("jwt: abc\n")
    assert remaining_hours(unencrypted_stamps(f)) is None


def test_remaining_hours_reads_unencrypted_expiry(tmp_path: Path):
    expires = datetime.now(UTC) + timedelta(hours=10)
    f = tmp_path / "t.yaml"
    f.write_text(
        textwrap.dedent(f"""\
            expires_unencrypted: "{expires:%Y-%m-%dT%H:%M:%SZ}"
            jwt: abc
            """)
    )
    remaining = remaining_hours(unencrypted_stamps(f))
    assert remaining is not None
    assert 9 < remaining < 11


if __name__ == "__main__":
    pytest_bazel.main()

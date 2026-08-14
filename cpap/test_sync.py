"""Tests for the git-backed card sync (partial clone + manifest skip logic)."""

import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest
import pytest_bazel

from cpap.card import FileEntry
from cpap.sync import IW, MANIFEST_FILENAME, SyncManifest, _discover_wifi_interface, _wpa_config, run_sync

NOW = datetime(2026, 6, 10, tzinfo=UTC)

STR_PATH = "STR.EDF"
NIGHT1_PATH = "DATALOG\\20260418\\202604~1.EDF"
NIGHT2_PATH = "DATALOG\\20260419\\202604~2.EDF"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Bare repo with an initial README commit (simulates Forgejo auto_init)."""
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(bare))
    # Let --filter=blob:none clones and lazy promisor fetches work over file://.
    _git("-C", str(bare), "config", "uploadpack.allowfilter", "true")
    _git("-C", str(bare), "config", "uploadpack.allowanysha1inwant", "true")
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    (seed / "README.md").write_text("# cpap-data\n")
    _git("-C", str(seed), "add", "README.md")
    _git("-C", str(seed), "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-m", "init")
    _git("-C", str(seed), "push", "origin", "main")
    return bare


@dataclass
class CardFile:
    content: bytes
    create_time: int
    truncate_to: int | None = None  # serve only this prefix (mid-write simulation); listing still claims full size


@dataclass
class FakeCard:
    """Duck-typed EZShareClient over an in-memory dict of card paths (backslash-separated)."""

    files: dict[str, CardFile]
    downloads: Counter[str] = field(default_factory=Counter)

    def walk(self) -> Iterator[FileEntry]:
        for path, f in self.files.items():
            yield FileEntry(
                name=path.rsplit("\\", 1)[-1],
                size=len(f.content),
                create_time=f.create_time,
                img_url=f"http://192.168.4.1/download?file={quote(path)}",
                is_dir=False,
            )

    def download(self, url: str, dest: Path) -> None:
        path = parse_qs(urlparse(url).query)["file"][0]
        f = self.files[path]
        self.downloads[path] += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f.content if f.truncate_to is None else f.content[: f.truncate_to])


def sync(card: FakeCard, remote: Path) -> None:
    # file:// URL (not a plain path) so git honors --depth/--filter instead of local-clone mode.
    run_sync(
        client=card,
        git_url=remote.as_uri(),
        branch="main",
        wifi_interface=None,
        wifi_ssid="unused",
        wifi_password=None,
        username=None,
        password=None,
        now=NOW,
    )


def test_wpa_config_quotes_password_and_uses_private_control_dir(tmp_path: Path) -> None:
    config = _wpa_config(ssid='Rai "CPAP"', password="pa\\ss\nword", control_dir=tmp_path)
    assert f"ctrl_interface=DIR={tmp_path}" in config
    assert 'ssid="Rai \\"CPAP\\""' in config
    assert 'psk="pa\\\\ss\\nword"' in config


def test_discovers_sole_wifi_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ([IW, "dev"],)
        return subprocess.CompletedProcess([IW, "dev"], 0, stdout="phy#0\n\tInterface wlx1234\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert _discover_wifi_interface() == "wlx1234"


def tree_paths(remote: Path) -> set[str]:
    return set(_git("-C", str(remote), "ls-tree", "-r", "--name-only", "main").splitlines())


def blob(remote: Path, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(remote), "show", f"main:{path}"], check=True, capture_output=True).stdout


def commit_count(remote: Path) -> int:
    return int(_git("-C", str(remote), "rev-list", "--count", "main"))


def test_initial_sync_commits_all_files(remote: Path) -> None:
    card = FakeCard(
        files={
            STR_PATH: CardFile(content=b"summary-v1", create_time=100),
            NIGHT1_PATH: CardFile(content=b"night-1", create_time=200),
            NIGHT2_PATH: CardFile(content=b"night-2", create_time=300),
        }
    )
    sync(card, remote)
    # README.md survives the no-checkout commit: proof that read-tree populated the index.
    assert tree_paths(remote) == {
        "README.md",
        "STR.EDF",
        "DATALOG/20260418/202604~1.EDF",
        "DATALOG/20260419/202604~2.EDF",
        MANIFEST_FILENAME,
    }
    assert blob(remote, "STR.EDF") == b"summary-v1"
    assert blob(remote, "DATALOG/20260418/202604~1.EDF") == b"night-1"
    manifest = SyncManifest.model_validate_json(blob(remote, MANIFEST_FILENAME))
    assert manifest.files["DATALOG/20260419/202604~2.EDF"].size == len(b"night-2")
    assert manifest.files["STR.EDF"].create_time == 100
    assert commit_count(remote) == 2
    assert _git("-C", str(remote), "log", "-1", "--format=%s").strip() == "cpap: sync 2026-06-10"


def test_rerun_is_noop(remote: Path) -> None:
    card = FakeCard(
        files={
            STR_PATH: CardFile(content=b"summary-v1", create_time=100),
            NIGHT1_PATH: CardFile(content=b"night-1", create_time=200),
        }
    )
    sync(card, remote)
    sync(card, remote)
    assert commit_count(remote) == 2
    assert card.downloads == Counter({STR_PATH: 1, NIGHT1_PATH: 1})


def test_changed_file_is_redownloaded_others_kept(remote: Path) -> None:
    card = FakeCard(
        files={
            STR_PATH: CardFile(content=b"summary-v1", create_time=100),
            NIGHT1_PATH: CardFile(content=b"night-1", create_time=200),
        }
    )
    sync(card, remote)
    card.files[STR_PATH] = CardFile(content=b"summary-v2 (longer)", create_time=101)
    sync(card, remote)
    assert blob(remote, "STR.EDF") == b"summary-v2 (longer)"
    assert card.downloads == Counter({STR_PATH: 2, NIGHT1_PATH: 1})
    # The untouched file must still be in the new tree even though run 2 never downloaded it.
    assert blob(remote, "DATALOG/20260418/202604~1.EDF") == b"night-1"
    assert commit_count(remote) == 3


def test_midwrite_download_self_heals(remote: Path) -> None:
    # The card lists the final size but serves a truncated prefix (file still being written).
    night = CardFile(content=b"full-night-data", create_time=400, truncate_to=4)
    card = FakeCard(files={NIGHT1_PATH: night})
    sync(card, remote)
    assert blob(remote, "DATALOG/20260418/202604~1.EDF") == b"full"
    manifest = SyncManifest.model_validate_json(blob(remote, MANIFEST_FILENAME))
    # Manifest records the 4 bytes actually stored, not the card's claimed 15.
    assert manifest.files["DATALOG/20260418/202604~1.EDF"].size == 4
    night.truncate_to = None  # write finished; card serves the full file now
    sync(card, remote)
    assert blob(remote, "DATALOG/20260418/202604~1.EDF") == b"full-night-data"
    assert card.downloads[NIGHT1_PATH] == 2


if __name__ == "__main__":
    pytest_bazel.main()

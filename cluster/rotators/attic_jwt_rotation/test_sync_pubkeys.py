import json
from pathlib import Path

import pytest_bazel

from cluster.rotators.attic_jwt_rotation import sync_pubkeys


class _Completed:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout


def test_sync_derives_pubkeys_in_secret_key_order(monkeypatch, tmp_path: Path):
    sops_file = tmp_path / "cache-keys.sops.yaml"
    sops_file.write_text("unused — sops decrypt is faked below\n")
    pubkeys_file = tmp_path / "attic-pubkeys.json"

    decrypted_yaml = "stringData:\n  main: main:secretA\n  gaffer: gaffer:secretB\n"
    pubkeys_by_secret = {"main:secretA": "main:pubA=", "gaffer:secretB": "gaffer:pubB="}

    def fake_run(args, input=None, **_kwargs):
        if args[:2] == ["sops", "-d"]:
            assert args[2] == str(sops_file)
            return _Completed(stdout=decrypted_yaml)
        assert args == ["nix", "key", "convert-secret-to-public"]
        assert input is not None
        return _Completed(stdout=pubkeys_by_secret[input] + "\n")

    monkeypatch.setattr(sync_pubkeys.subprocess, "run", fake_run)

    sync_pubkeys.sync(sops_file=sops_file, pubkeys_file=pubkeys_file)

    assert json.loads(pubkeys_file.read_text()) == ["main:pubA=", "gaffer:pubB="]
    assert pubkeys_file.read_text().endswith("\n")


if __name__ == "__main__":
    pytest_bazel.main()

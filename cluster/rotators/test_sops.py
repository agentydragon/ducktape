from pathlib import Path

import pytest_bazel
import yaml

from cluster.rotators import sops


def test_write_sops_yaml_encrypts_the_written_file_then_formats_it(monkeypatch, tmp_path: Path):
    path = tmp_path / "missing" / "parents" / "token.yaml"
    document = {"expires_unencrypted": "2030-01-01T00:00:00Z", "token": "plaintext-token"}
    calls: list[tuple[str, object]] = []

    def fake_run(args, **kwargs):
        assert kwargs == {"check": True}
        # `sops encrypt --in-place` needs the plaintext document on disk already.
        assert yaml.safe_load(path.read_text()) == document
        calls.append(("encrypt", list(args)))

    def fake_format(formatted: Path) -> None:
        calls.append(("format", formatted))

    monkeypatch.setattr(sops.subprocess, "run", fake_run)
    monkeypatch.setattr(sops, "prettier_format_yaml_in_place", fake_format)

    sops.write_sops_yaml(path=path, document=document)

    assert calls == [("encrypt", ["sops", "encrypt", "--indent", "2", "--in-place", str(path)]), ("format", path)]


def test_write_sops_yaml_preserves_key_order_and_keeps_long_values_on_one_line(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sops.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sops, "prettier_format_yaml_in_place", lambda _path: None)
    path = tmp_path / "token.yaml"
    long_value = " ".join(["word"] * 40)

    sops.write_sops_yaml(path=path, document={"zulu": 1, "alpha": long_value, "mike": [2]})

    written = path.read_text()
    assert list(yaml.safe_load(written)) == ["zulu", "alpha", "mike"]
    assert f"alpha: {long_value}\n" in written


if __name__ == "__main__":
    pytest_bazel.main()

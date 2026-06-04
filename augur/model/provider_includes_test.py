"""Tests for `resolve_provider_includes`: pure `{provider_config_path: <file>}` refs are inlined
(recursively, relative to the including file's dir), and any non-pure node is walked untouched —
so a dict that carries `provider_config_path` alongside other keys (e.g. a sample-sanity spec)
is left intact rather than mistaken for an include.
"""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from augur.model.provider_includes import resolve_provider_includes


def test_pure_ref_is_inlined(tmp_path: Path) -> None:
    (tmp_path / "macro.yaml").write_text("type: mirroring\nmodel: {type: state_space}\n")
    resolved = resolve_provider_includes({"provider_config_path": "macro.yaml"}, base_dir=tmp_path)
    assert resolved == {"type": "mirroring", "model": {"type": "state_space"}}


def test_ref_nested_in_larger_node_only_replaces_the_ref(tmp_path: Path) -> None:
    (tmp_path / "macro.yaml").write_text("type: mirroring\n")
    node = {"type": "composite", "macro": {"provider_config_path": "macro.yaml"}, "private_equity": {"type": "trained"}}
    resolved = resolve_provider_includes(node, base_dir=tmp_path)
    assert resolved == {"type": "composite", "macro": {"type": "mirroring"}, "private_equity": {"type": "trained"}}


def test_node_with_extra_keys_is_not_an_include(tmp_path: Path) -> None:
    # A sample-sanity spec carries `provider_config_path` plus bands; it must not be inlined.
    node = {"provider_config_path": "macro.yaml", "rollout_count": 64}
    assert resolve_provider_includes(node, base_dir=tmp_path) == node


def test_includes_resolve_recursively(tmp_path: Path) -> None:
    (tmp_path / "inner.yaml").write_text("type: state_space\n")
    (tmp_path / "outer.yaml").write_text("type: mirroring\nmodel: {provider_config_path: inner.yaml}\n")
    resolved = resolve_provider_includes({"provider_config_path": "outer.yaml"}, base_dir=tmp_path)
    assert resolved == {"type": "mirroring", "model": {"type": "state_space"}}


def test_lists_are_walked(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("name: a\n")
    node = {"items": [{"provider_config_path": "a.yaml"}, {"name": "b"}]}
    assert resolve_provider_includes(node, base_dir=tmp_path) == {"items": [{"name": "a"}, {"name": "b"}]}


if __name__ == "__main__":
    pytest_bazel.main()

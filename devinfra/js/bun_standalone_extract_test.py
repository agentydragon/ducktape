"""Synthetic coverage for the Bun standalone graph reader and CLI."""

import struct
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.js.bun_standalone_extract import (
    MODULE_ENTRY_SIZE,
    TRAILER,
    ExtractError,
    main,
    parse_modules,
    select_alias_module,
)


def synthetic_graph(entries: list[tuple[str, bytes]]) -> bytes:
    """Encode only the Bun graph fields this reader needs, with neutral modules."""
    payload = bytearray(MODULE_ENTRY_SIZE * len(entries))
    for index, (name, contents) in enumerate(entries):
        name_bytes = name.encode()
        name_offset = len(payload)
        payload.extend(name_bytes)
        contents_offset = len(payload)
        payload.extend(contents)
        struct.pack_into(
            "<4I", payload, index * MODULE_ENTRY_SIZE, name_offset, len(name_bytes), contents_offset, len(contents)
        )
    graph_end = len(payload) + 32 + len(TRAILER)
    payload.extend(struct.pack("<Q II I II I", graph_end, 0, MODULE_ENTRY_SIZE * len(entries), 0, 0, 0, 0))
    payload.extend(TRAILER)
    return bytes(payload)


@pytest.fixture
def alias_modules() -> list[dict]:
    return [
        {"name": "a.js", "contents": b"factory provider"},
        {"name": "b.js", "contents": b"factory interface"},
        {"name": "c.js", "contents": b"provider"},
    ]


def test_single_marker_requires_a_unique_module(alias_modules: list[dict]) -> None:
    name, selected = select_alias_module(alias_modules, "selected.js=interface")
    assert name == "selected.js"
    assert selected["name"] == "b.js"


def test_conjunctive_markers_disambiguate(alias_modules: list[dict]) -> None:
    name, selected = select_alias_module(alias_modules, "selected.js=factory::provider", "::")
    assert name == "selected.js"
    assert selected["name"] == "a.js"


def test_conjunctive_markers_reject_ambiguous_matches(alias_modules: list[dict]) -> None:
    with pytest.raises(ExtractError, match="matched 2 modules"):
        select_alias_module(alias_modules, "selected.js=provider", "::")


def test_conjunctive_markers_reject_no_match(alias_modules: list[dict]) -> None:
    with pytest.raises(ExtractError, match="matched 0 modules"):
        select_alias_module(alias_modules, "selected.js=factory::missing", "::")


def test_empty_marker_is_rejected(alias_modules: list[dict]) -> None:
    with pytest.raises(ExtractError, match="non-empty"):
        select_alias_module(alias_modules, "selected.js=factory::", "::")


def test_reads_synthetic_module_names_and_contents() -> None:
    contents = b"export const foo = 1;"
    graph = synthetic_graph([("/$bunfs/root/a.js", contents)])
    parsed = parse_modules(graph)
    assert parsed[0]["name"] == "/$bunfs/root/a.js"
    assert parsed[0]["contents"] == contents


def test_two_aliases_for_one_source_are_rejected(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(synthetic_graph([("/$bunfs/root/foo.js", b"one_marker two_marker")]))
    with pytest.raises(SystemExit, match="multiple aliases for one source module"):
        main(
            [
                str(binary),
                str(tmp_path / "extracted"),
                "--alias",
                "first.js=one_marker",
                "--alias",
                "second.js=two_marker",
            ]
        )


def test_virtual_source_path_and_stable_alias_are_both_written(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(synthetic_graph([("/$bunfs/root/foo.js", b"export function factory() {}")]))
    output = tmp_path / "extracted"
    assert main([str(binary), str(output), "--alias", "factory.js=factory"]) == 0
    assert (output / "$bunfs/root/foo.js").read_text() == "export function factory() {}"
    assert (output / "factory.js").read_text() == "export function factory() {}"


def test_debundle_list_excludes_non_utf8_and_non_js_files(tmp_path: Path) -> None:
    modules = [
        ("/$bunfs/root/a.js", b"export function factory() { return {}; }"),
        ("/$bunfs/root/b.js", b"import { factory } from '/$bunfs/root/a.js';"),
        ("/$bunfs/root/asset.js", b"\xff"),
        ("/$bunfs/root/entry", b"entry point"),
    ]
    binary = tmp_path / "binary"
    binary.write_bytes(synthetic_graph(modules))
    output = tmp_path / "extracted"
    assert (
        main(
            [
                str(binary),
                str(output),
                "--only-js",
                "--alias",
                "factory.js=export function factory",
                "--debundle-list",
                str(output / "js-files.txt"),
            ]
        )
        == 0
    )
    assert (output / "js-files.txt").read_text().splitlines() == ["$bunfs/root/b.js", "factory.js\t$bunfs/root/a.js"]


if __name__ == "__main__":
    pytest_bazel.main()

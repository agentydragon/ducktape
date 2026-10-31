"""Synthetic coverage for the Bun standalone graph reader and CLI."""

import json
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


def synthetic_graph(entries: list[tuple[str, bytes]], loaders: tuple[int, ...] = ()) -> bytes:
    """Encode only the Bun graph fields this reader needs, with neutral modules."""
    payload = bytearray(MODULE_ENTRY_SIZE * len(entries))
    for index, loader in enumerate(loaders):
        payload[index * MODULE_ENTRY_SIZE + 49] = loader
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


def test_exclusion_markers_disambiguate(alias_modules: list[dict]) -> None:
    name, selected = select_alias_module(alias_modules, "selected.js=provider", exclude_marker_sets=[("factory",)])
    assert name == "selected.js"
    assert selected["name"] == "c.js"


def test_exclusion_marker_set_requires_every_marker() -> None:
    modules = [
        {"name": "a.js", "contents": b"provider startServer"},
        {"name": "b.js", "contents": b"provider startServer export"},
    ]
    name, selected = select_alias_module(
        modules, "selected.js=provider", exclude_marker_sets=[("startServer", "export")]
    )
    assert name == "selected.js"
    assert selected["name"] == "a.js"


def test_exclusions_reject_zero_remaining_candidates(alias_modules: list[dict]) -> None:
    with pytest.raises(ExtractError, match="matched 2 modules before exclusions and 0 after exclusions"):
        select_alias_module(alias_modules, "selected.js=provider", exclude_marker_sets=[("factory",), ("provider",)])


def test_exclusions_reject_ambiguous_remaining_candidates(alias_modules: list[dict]) -> None:
    with pytest.raises(ExtractError, match="matched 2 modules before exclusions and 2 after exclusions"):
        select_alias_module(alias_modules, "selected.js=factory", exclude_marker_sets=[("not-present",)])


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


def test_alias_without_filters_a_structural_duplicate_in_binary_cli(tmp_path: Path) -> None:
    shared_config_provider = b"var config = globalThis.sharedConfig; export { config };"
    duplicate_entry = b"globalThis.sharedConfig; export { startService };"
    binary = tmp_path / "binary"
    binary.write_bytes(
        synthetic_graph(
            [
                ("/$bunfs/root/config-provider.js", shared_config_provider),
                ("/$bunfs/root/entry-copy.js", duplicate_entry),
            ]
        )
    )
    output = tmp_path / "extracted"
    assert (
        main(
            [
                str(binary),
                str(output),
                "--alias-all",
                "sharedConfig.js=globalThis.sharedConfig",
                "--alias-without",
                "sharedConfig.js=startService",
            ]
        )
        == 0
    )
    assert (output / "sharedConfig.js").read_bytes() == shared_config_provider


def test_alias_without_rejects_unknown_alias_name(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(synthetic_graph([("/$bunfs/root/a.js", b"provider")]))
    with pytest.raises(SystemExit, match=r"no --alias or --alias-all selector for 'missing\.js'"):
        main(
            [
                str(binary),
                str(tmp_path / "extracted"),
                "--alias-without",
                "missing.js=duplicate",
                "--alias",
                "provider.js=provider",
            ]
        )


def test_alias_without_rejects_empty_markers(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(synthetic_graph([("/$bunfs/root/a.js", b"provider")]))
    with pytest.raises(SystemExit, match="alias-without expects non-empty NAME and MARKER values"):
        main(
            [
                str(binary),
                str(tmp_path / "extracted"),
                "--alias",
                "provider.js=provider",
                "--alias-without",
                "provider.js=::duplicate",
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


def test_embedded_loaders_name_each_module_loader(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(
        synthetic_graph(
            [("/$bunfs/root/a.js", b"export {};"), ("/$bunfs/root/b.md", b"# b"), ("/$bunfs/root/c.node", b"")],
            loaders=(1, 13, 10),
        )
    )
    output = tmp_path / "extracted"
    manifest = output / "embedded-loaders.json"
    assert main([str(binary), str(output), "--only-js", "--embedded-loaders", str(manifest)]) == 0
    assert json.loads(manifest.read_text()) == {
        "$bunfs/root/a.js": "js",
        "$bunfs/root/b.md": "text",
        "$bunfs/root/c.node": "napi",
    }


def test_embedded_loaders_reject_an_unknown_loader(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(synthetic_graph([("/$bunfs/root/a.x", b"x")], loaders=(200,)))
    with pytest.raises(SystemExit, match="unknown Bun loader 200"):
        main([str(binary), str(tmp_path / "out"), "--embedded-loaders", str(tmp_path / "loaders.json")])


if __name__ == "__main__":
    pytest_bazel.main()

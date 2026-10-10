"""Extract embedded modules from a Bun `--compile` standalone executable.

This parses Bun's `StandaloneModuleGraph` serialization, retaining each
module's virtual source path and exact byte boundaries. Format per oven-sh/bun
`src/standalone_graph/StandaloneModuleGraph.rs`:

    [ ... graph payload ... ][ Offsets ][ TRAILER ]

`TRAILER` is `b"\\n---- Bun! ----\\n"`. `Offsets` is `#[repr(C)]`:

    byte_count            usize   (8)
    modules_ptr           StringPointer{off:u32, len:u32}
    entry_point_id        u32
    compile_exec_argv_ptr StringPointer
    flags                 u32

`modules_ptr` locates a table of `CompiledModuleGraphFile`, also `#[repr(C)]`
and 52 bytes each -- six `StringPointer`s (name, contents, sourcemap, bytecode,
module_info, bytecode_origin_path) followed by four `u8` enums.

Every `StringPointer.off` is relative to a base the graph does not record
directly: the mapped region's start. `byte_count` is *not* it -- deriving
`base = graph_end - byte_count` lands 48 bytes past the true base, so names
decode to garbage. Rather than hardcode that fudge, `_find_base` recovers the
base empirically by scoring candidates on how many module names decode to a
plausible `/$bunfs/root/` path, which stays correct if the struct grows.
"""

import argparse
import pathlib
import struct

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 8 + 8 + 4 + 8 + 4
MODULE_ENTRY_SIZE = 52
BUNFS_PREFIX = b"/$bunfs/root/"


class ExtractError(Exception):
    pass


def _read_offsets(data: bytes) -> tuple[int, int, int, int]:
    """Return (trailer_pos, byte_count, modules_off, modules_len)."""
    pos = data.rfind(TRAILER)
    if pos < 0:
        raise ExtractError("no Bun standalone trailer found -- not a `bun build --compile` binary?")
    if pos < OFFSETS_SIZE:
        raise ExtractError("truncated: trailer with no room for an Offsets struct")
    byte_count, modules_off, modules_len, _entry_point_id, _argv_off, _argv_len, _flags = struct.unpack_from(
        "<Q II I II I", data, pos - OFFSETS_SIZE
    )
    if modules_len % MODULE_ENTRY_SIZE:
        raise ExtractError(
            f"module table length {modules_len} is not a multiple of {MODULE_ENTRY_SIZE}; the graph layout has changed"
        )
    return pos, byte_count, modules_off, modules_len


def _find_base(data: bytes, graph_end: int, byte_count: int, modules_off: int, count: int) -> int:
    """Recover the base that every `StringPointer.off` is relative to.

    The graph does not store it. `graph_end - byte_count` is only approximate,
    so scan a window around it and score each candidate on how many module
    names decode to a `/$bunfs/root/...` path. The true base scores every
    entry and anything else scores almost none.
    """
    probe = min(count, 64)
    approx = graph_end - byte_count
    best: tuple[int, int] | None = None
    for delta in range(-(1 << 16), (1 << 16), 4):
        guess = approx + delta
        table = guess + modules_off
        if not 0 <= table <= len(data) - count * MODULE_ENTRY_SIZE:
            continue
        good = 0
        for k in range(probe):
            n_off, n_len = struct.unpack_from("<II", data, table + k * MODULE_ENTRY_SIZE)
            s = guess + n_off
            if 0 <= s <= len(data) - n_len and data[s : s + len(BUNFS_PREFIX)] == BUNFS_PREFIX:
                good += 1
            else:
                break
        if best is None or good > best[1]:
            best = (guess, good)
            if good == probe:
                return guess
    if best is None or best[1] == 0:
        raise ExtractError(
            "could not recover the module-graph base offset -- the Bun standalone layout may have changed"
        )
    return best[0]


def _string(data: bytes, base: int, off: int, length: int) -> bytes:
    start = base + off
    if length == 0:
        return b""
    if not 0 <= start <= len(data) - length:
        raise ExtractError(f"string pointer out of range: off={off} len={length}")
    return data[start : start + length]


def parse_modules(data: bytes) -> list[dict]:
    trailer_pos, byte_count, modules_off, modules_len = _read_offsets(data)
    graph_end = trailer_pos + len(TRAILER)
    count = modules_len // MODULE_ENTRY_SIZE
    base = _find_base(data, graph_end, byte_count, modules_off, count)

    modules = []
    for k in range(count):
        entry = base + modules_off + k * MODULE_ENTRY_SIZE
        fields = struct.unpack_from("<12I", data, entry)
        name = _string(data, base, fields[0], fields[1])
        contents = _string(data, base, fields[2], fields[3])
        sourcemap = _string(data, base, fields[4], fields[5])
        bytecode_len = fields[7]
        modules.append(
            {
                "index": k,
                "name": name.decode("utf-8", "replace"),
                "contents": contents,
                "sourcemap": sourcemap,
                "has_bytecode": bytecode_len > 0,
            }
        )
    return modules


def _safe_relpath(name: str) -> str:
    """Keep Bun's virtual root so absolute imports match extracted paths."""
    rel = name
    if rel.startswith("/$bunfs/root/"):
        rel = rel.lstrip("/")
    else:
        for prefix in ("B:\\~BUN\\root\\", "/$bunfs/", "~BUN/root/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix) :]
                break
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or "unnamed"


def _is_debundle_source(path: str, contents: bytes) -> bool:
    if not path.endswith(".js"):
        return False
    try:
        contents.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def select_alias_module(modules: list[dict], spec: str, marker_separator: str | None = None) -> tuple[str, dict]:
    """Resolve one stable alias from one marker or a conjunction of markers."""
    if "=" not in spec:
        raise ExtractError(f"alias expects NAME=MARKER, got {spec!r}")
    alias_name, marker_expr = spec.split("=", 1)
    markers = marker_expr.split(marker_separator) if marker_separator is not None else [marker_expr]
    if not alias_name or any(not marker for marker in markers):
        raise ExtractError(f"alias expects non-empty NAME and MARKER values, got {spec!r}")
    needles = [marker.encode() for marker in markers]
    hits = [m for m in modules if all(needle in m["contents"] for needle in needles)]
    if len(hits) != 1:
        marker_description = (
            repr(markers[0]) if len(markers) == 1 else "all markers " + ", ".join(repr(marker) for marker in markers)
        )
        raise ExtractError(
            f"alias {alias_name}: {marker_description} matched {len(hits)} modules, "
            "need exactly 1" + ("" if not hits else ": " + ", ".join(h["name"] for h in hits))
        )
    return alias_name, hits[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("binary", type=pathlib.Path)
    ap.add_argument("out_dir", type=pathlib.Path)
    ap.add_argument("--list", type=pathlib.Path, help="also write a newline-separated list of emitted paths here")
    ap.add_argument(
        "--debundle-list",
        type=pathlib.Path,
        help="write UTF-8 .js paths for Debundle, mapping stable aliases to source paths",
    )
    ap.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="NAME=MARKER",
        help=(
            "additionally write the unique module containing MARKER under the "
            "stable filename NAME. Marker selection avoids depending on "
            "content-hashed Bun chunk filenames. Repeatable. Errors if "
            "MARKER is not unique."
        ),
    )
    ap.add_argument(
        "--alias-all",
        action="append",
        default=[],
        metavar="NAME=MARKER::MARKER[::MARKER...]",
        help=(
            "like --alias, but select the unique module containing every "
            "double-colon-separated marker. Use when no single marker uniquely "
            "identifies the module."
        ),
    )
    ap.add_argument(
        "--only-js",
        action="store_true",
        help=(
            "skip modules with no source text (bytecode-only) and native "
            "addons. Some entry points have no .js suffix, so this does not "
            "filter by filename."
        ),
    )
    args = ap.parse_args(argv)

    data = args.binary.read_bytes()
    modules = parse_modules(data)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, bytes] = {}
    for mod in modules:
        if args.only_js and (not mod["contents"] or mod["name"].endswith(".node")):
            continue
        rel = _safe_relpath(mod["name"])
        dest = args.out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(mod["contents"])
        written[rel] = mod["contents"]

    alias_sources: dict[str, str] = {}
    source_aliases: dict[str, str] = {}
    alias_collisions: list[str] = []
    for option, spec, separator in [
        *(("--alias", spec, None) for spec in args.alias),
        *(("--alias-all", spec, "::") for spec in args.alias_all),
    ]:
        try:
            alias_name, hit = select_alias_module(modules, spec, separator)
        except ExtractError as error:
            raise SystemExit(f"{option}: {error}") from error
        source_path = _safe_relpath(hit["name"])
        if source_path in source_aliases:
            alias_collisions.append(f"{alias_name} and {source_aliases[source_path]} select {source_path}")
        if alias_name in written and alias_name != source_path:
            raise SystemExit(f"{option}: alias {alias_name} overwrites an extracted module")
        dest = args.out_dir / alias_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(hit["contents"])
        written[alias_name] = hit["contents"]
        alias_sources[alias_name] = source_path
        source_aliases.setdefault(source_path, alias_name)

    if alias_collisions:
        raise SystemExit("multiple aliases for one source module:\n" + "\n".join(alias_collisions))

    if args.list:
        args.list.parent.mkdir(parents=True, exist_ok=True)
        args.list.write_text("".join(f"{p}\n" for p in sorted(written)))

    if args.debundle_list:
        args.debundle_list.parent.mkdir(parents=True, exist_ok=True)
        args.debundle_list.write_text(
            "".join(
                f"{path}\t{alias_sources[path]}\n" if path in alias_sources else f"{path}\n"
                for path, contents in sorted(written.items())
                if path not in source_aliases and _is_debundle_source(path, contents)
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

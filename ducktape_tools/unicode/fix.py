# Ported from old fix_unicode.py
import argparse
from pathlib import Path
import sys
from typing import NamedTuple
import unicodedata


class UnicodeIssue(NamedTuple):
    line_num: int
    char_pos: int
    char: str
    codepoint: str
    name: str
    suggestion: str | None
    context: str


def get_char_info(char: str) -> tuple[str, str]:
    codepoint = f"U+{ord(char):04X}"
    try:
        name = unicodedata.name(char)
    except ValueError:
        name = "UNKNOWN"
    return codepoint, name


def get_common_suggestion(char: str) -> str | None:
    mapping = {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u2013": "--",
        "\u2014": "---",
        "\u00a0": " ",
        "\u2009": " ",
        "\u200b": "",
        "\u2026": "...",
    }
    return mapping.get(char)


def scan_file(filepath: Path) -> list[UnicodeIssue]:
    try:
        content = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"Error: {filepath} is not valid UTF-8")
        return []
    issues: list[UnicodeIssue] = []
    lines = content.splitlines(keepends=True)
    for ln, line in enumerate(lines, 1):
        for cp, ch in enumerate(line):
            if ord(ch) > 127:
                codepoint, name = get_char_info(ch)
                suggestion = get_common_suggestion(ch)
                preview = line.strip()[:60] + "..." if len(line.strip()) > 60 else line.strip()
                issues.append(UnicodeIssue(ln, cp + 1, ch, codepoint, name, suggestion, preview))
    return issues


def apply_conversions(filepath: Path, conversions: list[tuple[str, str]]) -> int:
    try:
        content = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"Error: {filepath} is not valid UTF-8")
        return 0
    cmap: dict[str, str] = {}
    for from_cp, to_cp in conversions:
        from_char = chr(int(from_cp.replace("U+", ""), 16))
        to_char = "" if to_cp == "DELETE" else chr(int(to_cp.replace("U+", ""), 16))
        cmap[from_char] = to_char
    changes = 0
    for from_char, to_char in cmap.items():
        count = content.count(from_char)
        if count:
            content = content.replace(from_char, to_char)
            changes += count
            from_name = get_char_info(from_char)[1]
            to_name = get_char_info(to_char)[1] if to_char else "DELETION"
            print(f"Replaced {count} instances of {from_char} ({from_name}) with {to_char!r} ({to_name})")
    if changes:
        filepath.write_text(content, encoding="utf-8")
        print(f"\nTotal: {changes} characters replaced in {filepath}")
    return changes


def parse_conversion(spec: str) -> tuple[str, str]:
    if "->" not in spec:
        raise ValueError(f"Invalid conversion format: {spec}")
    fr, to = (s.strip() for s in spec.split("->", 1))
    if not fr.startswith("U+"):
        raise ValueError(f"Invalid codepoint: {fr}")
    if to != "DELETE" and not to.startswith("U+"):
        raise ValueError(f"Invalid target: {to}")
    return fr, to


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect and fix problematic Unicode characters")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--convert", action="append", metavar="FROM->TO")
    args = parser.parse_args(argv)
    conversions: list[tuple[str, str]] = []
    if args.convert:
        for conv in args.convert:
            conversions.append(parse_conversion(conv))
    total_changes = 0
    for fp in args.files:
        path = Path(fp)
        if not path.exists() or not path.is_file():
            print(f"Error: {fp} not found or not a file")
            continue
        if conversions:
            total_changes += apply_conversions(path, conversions)
        else:
            issues = scan_file(path)
            if issues:
                print(f"\n{path}:")
                current = None
                for issue in issues:
                    if issue.line_num != current:
                        current = issue.line_num
                        print(f"\nLine {issue.line_num}: {issue.context}")
                    print(f"  Column {issue.char_pos}: {issue.codepoint} ({issue.name})")
                    if issue.suggestion:
                        sug_cp, _ = get_char_info(issue.suggestion)
                        print(
                            f"    -> suggest: --convert {issue.codepoint}->{sug_cp}  # {issue.char} � {issue.suggestion}"
                        )
                    elif any(cat in issue.name for cat in ["MATHEMATICAL", "ARROW", "SYMBOL", "LETTER"]):
                        print(f"    -> possibly intentional (category: {issue.name.split()[0]})")
                    else:
                        print(f"    -> no automatic suggestion, consider: --convert {issue.codepoint}->DELETE")
                print(f"\nTotal: {len(issues)} non-ASCII characters found")
    return 0


if __name__ == "__main__":
    sys.exit(cli())

import os
import posixpath
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest_bazel
from markdown_it import MarkdownIt
from markdown_it.token import Token

from skills.frontmatter_validation import validate_skill_frontmatter_text

_INCLUDE = re.compile(r"^@(\S+)$", re.MULTILINE)


def _archive() -> Path:
    archive_path = os.environ.get("SKILL_ARCHIVE")
    assert archive_path, "expected SKILL_ARCHIVE env var"
    return Path(archive_path)


def _walk(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        yield from _walk(token.children or [])


def _relative_targets(markdown: str) -> set[str]:
    """Link and image targets, plus Claude Code `@path` includes (a paragraph line
    of their own), that name a path relative to the file."""
    tokens = list(_walk(MarkdownIt().parse(markdown)))
    targets = {str(t.attrGet("href")) for t in tokens if t.type == "link_open"}
    targets |= {str(t.attrGet("src")) for t in tokens if t.type == "image"}
    targets |= {m.group(1) for t in tokens if t.type == "inline" for m in _INCLUDE.finditer(t.content)}
    return {
        path
        for target in targets
        if not re.match(r"^[a-z][a-z0-9+.-]*:", target) and not target.startswith(("#", "/"))
        if (path := target.split("#", 1)[0])
    }


def test_frontmatter_archives() -> None:
    archive = _archive()
    with zipfile.ZipFile(archive) as zf:
        skill_members = [name for name in zf.namelist() if name.endswith("/SKILL.md")]

        assert skill_members, f"{archive}: expected at least one SKILL.md in packaged archive"

        for name in skill_members:
            validate_skill_frontmatter_text(zf.read(name).decode(), source=f"{archive}:{name}")


def test_relative_links_resolve_inside_archive() -> None:
    """A skill is installed on its own, so every relative link or `@` include in its
    markdown must name a file the archive ships."""
    with zipfile.ZipFile(_archive()) as zf:
        members = set(zf.namelist())
        dangling = sorted(
            f"{name} -> {target}"
            for name in members
            if name.endswith(".md")
            for target in _relative_targets(zf.read(name).decode())
            if (resolved := posixpath.normpath(posixpath.join(posixpath.dirname(name), target))) not in members
            and not any(member.startswith(resolved.rstrip("/") + "/") for member in members)
        )
    assert not dangling, "links that do not resolve inside the skill archive:\n" + "\n".join(dangling)


if __name__ == "__main__":
    pytest_bazel.main()

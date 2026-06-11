from __future__ import annotations

import re
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

_ALLOWED_VARS = {"toolsBlob", "envGitBlobs", "modelLine", "mcpSection"}
_TOKEN_RE = re.compile(r"\{\{\s*([#/]?\w+)\s*}}")


def validate_template_text(text: str) -> None:
    """Validate template content for required placeholders and structure.

    Rules (must mirror JS system_rewrite_apply.js):
    - Must include all of: {{toolsBlob}}, {{envGitBlobs}}, {{modelLine}}, {{mcpSection}}
    - Each of the above appears at most once
    - No other {{...}} tokens are allowed (including sections like {{#...}} or {{/...}})
    """
    # Presence
    required = ("{{toolsBlob}}", "{{envGitBlobs}}", "{{modelLine}}", "{{mcpSection}}")
    missing = [m for m in required if m not in text]
    if missing:
        raise RuntimeError(
            "Invalid template: missing required placeholders: "
            + ", ".join(missing)
            + " — expected mustache markers like {{toolsBlob}}."
        )
    # Duplicates
    dups = [m for m in required if text.count(m) > 1]
    if dups:
        raise RuntimeError("Invalid template: duplicate placeholders (>1 occurrence): " + ", ".join(dups))
    # Unsupported tokens
    tokens = _TOKEN_RE.findall(text)
    bad = [t for t in tokens if t not in _ALLOWED_VARS]
    if bad:
        # Dedup and format like '{{foo}}'
        uniq = sorted(set(bad))
        raise RuntimeError(
            "Invalid template: unsupported tokens present: " + ", ".join(["{{" + b + "}}" for b in uniq])
        )


def validate_template_file(template_path: Path) -> None:
    """Fail fast if template file is unreadable or invalid."""
    if not isinstance(template_path, Path):
        raise ValueError("template_path must be a pathlib.Path")
    if not template_path.exists() or not template_path.is_file():
        raise FileNotFoundError(f"Template not found or not a file: {template_path}")
    text = template_path.read_text(encoding="utf-8")
    validate_template_text(text)


def iter_templates() -> Iterator[tuple[str, str]]:
    """Yield (relative_name, text) for all packaged templates/*.txt files.

    Traverses the installed package resources under sysrw.templates
    so it works from sdist/wheel installs and zipped packages.
    """
    root = resources.files(__name__)

    def _walk(dir_entry, prefix: str = "") -> Iterator[tuple[str, str]]:
        for child in dir_entry.iterdir():
            name = f"{prefix}{child.name}"
            if child.is_dir():
                yield from _walk(child, f"{name}/")
            elif name.endswith(".txt"):
                # Skip README.* files; they are documentation, not templates
                if child.name.lower().startswith("readme"):
                    continue
                text = child.read_text(encoding="utf-8")
                yield name, text

    return _walk(root, "")


def load_known_templates() -> dict[str, str]:
    """Return mapping of template content (full text) -> relative template name.

    Values look like "current_effective_template.txt" or "proposals/foo.txt".
    """
    mapping: dict[str, str] = {}
    for rel_name, text in iter_templates():
        mapping[text] = rel_name
    return mapping

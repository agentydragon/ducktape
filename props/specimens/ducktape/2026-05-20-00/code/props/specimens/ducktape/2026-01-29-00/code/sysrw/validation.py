from __future__ import annotations

from pathlib import Path


def validate_template_file(template_path: Path) -> None:
    """Fail fast if template file is unreadable or missing required placeholders.

    Required placeholders (mustache-style): {{toolsBlob}}, {{envGitBlobs}}, {{modelLine}}, {{mcpSection}}
    """
    assert isinstance(template_path, Path)
    if not template_path.is_file():
        raise FileNotFoundError(f"Template not a file: {template_path}")
    try:
        text = template_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Template not readable: {template_path}: {e}") from e

    required = ("{{toolsBlob}}", "{{envGitBlobs}}", "{{modelLine}}", "{{mcpSection}}")
    missing = [m for m in required if m not in text]
    if missing:
        raise RuntimeError(
            "Invalid template: missing required placeholders: "
            + ", ".join(missing)
            + " — expected mustache markers like {{toolsBlob}}."
        )

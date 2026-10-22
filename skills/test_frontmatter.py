from pathlib import Path

import pytest

from skills.frontmatter_validation import validate_skill_frontmatter, validate_skill_frontmatter_text

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"


def _frontmatter_text(*lines: str) -> str:
    return "---\n" + "\n".join(lines) + "\n---\n"


@pytest.mark.parametrize(
    "skill_path",
    sorted(path for path in SKILLS_DIR.glob("*/SKILL.md") if path.is_file()),
    ids=lambda path: path.parent.name,
)
def test_frontmatter_contract(skill_path: Path) -> None:
    try:
        validate_skill_frontmatter(skill_path)
    except ValueError as exc:
        raise AssertionError(f"{skill_path}: {exc}") from exc


@pytest.mark.parametrize("key", ["name", "description"])
def test_frontmatter_rejects_missing_required_string(key: str) -> None:
    frontmatter_lines = ["name: example", "description: Example skill."]
    text = _frontmatter_text(*(line for line in frontmatter_lines if not line.startswith(f"{key}:")))

    with pytest.raises(ValueError, match=f"frontmatter.{key} must be a string"):
        validate_skill_frontmatter_text(text, source="regression/SKILL.md")


@pytest.mark.parametrize("key", ["name", "description"])
def test_frontmatter_rejects_empty_required_string(key: str) -> None:
    frontmatter = {"name": "example", "description": "Example skill."}
    frontmatter[key] = '"   "'
    text = _frontmatter_text(*(f"{k}: {v}" for k, v in frontmatter.items()))

    with pytest.raises(ValueError, match=f"frontmatter.{key} must not be empty"):
        validate_skill_frontmatter_text(text, source="regression/SKILL.md")


@pytest.mark.parametrize("key", ["name", "description"])
def test_frontmatter_rejects_non_string_required_value(key: str) -> None:
    frontmatter = {"name": "example", "description": "Example skill."}
    frontmatter[key] = "false"
    text = _frontmatter_text(*(f"{k}: {v}" for k, v in frontmatter.items()))

    with pytest.raises(ValueError, match=f"frontmatter.{key} must be a string"):
        validate_skill_frontmatter_text(text, source="regression/SKILL.md")


def test_frontmatter_rejects_unquoted_mapping_separator() -> None:
    with pytest.raises(ValueError, match="invalid YAML frontmatter"):
        validate_skill_frontmatter_text(
            """---
name: invalid
description: This looks like prose. Trigger: user wants the skill.
---
""",
            source="regression/SKILL.md",
        )

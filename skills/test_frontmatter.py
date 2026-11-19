import pytest
import pytest_bazel

from skills.frontmatter_validation import validate_skill_frontmatter_text


def _frontmatter_text(*lines: str) -> str:
    return "---\n" + "\n".join(lines) + "\n---\n"


@pytest.mark.parametrize("key", ["name", "description"])
def test_frontmatter_rejects_missing_required_string(key: str) -> None:
    frontmatter_lines = ["name: example", "description: Example skill."]
    text = _frontmatter_text(*(line for line in frontmatter_lines if not line.startswith(f"{key}:")))

    with pytest.raises(ValueError, match=f"frontmatter.{key} must be a string"):
        validate_skill_frontmatter_text(text, source="regression/SKILL.md")


@pytest.mark.parametrize("key", ["name", "description"])
@pytest.mark.parametrize(("value", "reason"), [('"   "', "must not be empty"), ("false", "must be a string")])
def test_frontmatter_rejects_invalid_required_string(key: str, value: str, reason: str) -> None:
    frontmatter = {"name": "example", "description": "Example skill."}
    frontmatter[key] = value
    text = _frontmatter_text(*(f"{k}: {v}" for k, v in frontmatter.items()))

    with pytest.raises(ValueError, match=f"frontmatter.{key} {reason}"):
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


if __name__ == "__main__":
    pytest_bazel.main()

import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

MAX_DESCRIPTION_LEN = 1024


def parse_frontmatter_text(text: str) -> Mapping[str, object]:
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")

    try:
        _, frontmatter, _ = text.split("---\n", 2)
    except ValueError as exc:
        raise ValueError("malformed YAML frontmatter delimiters") from exc

    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("frontmatter must decode to a mapping")
    return data


def parse_frontmatter(skill_path: Path) -> Mapping[str, object]:
    return parse_frontmatter_text(skill_path.read_text())


def validate_skill_frontmatter_text(text: str, source: str = "SKILL.md") -> None:
    frontmatter = parse_frontmatter_text(text)
    description = frontmatter.get("description")
    if not isinstance(description, str):
        raise ValueError(f"{source}: frontmatter.description must be a string")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ValueError(
            f"{source}: frontmatter.description is {len(description)} chars, must be <= {MAX_DESCRIPTION_LEN}"
        )


def validate_skill_frontmatter(skill_path: Path) -> None:
    validate_skill_frontmatter_text(skill_path.read_text(), source=str(skill_path))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m skills.frontmatter_validation <input> <output>", file=sys.stderr)
        return 2

    src = Path(args[0])
    dst = Path(args[1])

    validate_skill_frontmatter(src)
    shutil.copyfile(src, dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

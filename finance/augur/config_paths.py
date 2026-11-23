"""File paths stored in Augur deployment and training configs."""

from pathlib import Path


def config_relative_path(path: str | Path, base_dir: Path) -> Path:
    """Resolve relative paths beside their config; preserve explicit absolute paths."""
    value = Path(path)
    return value if value.is_absolute() else (base_dir / value).resolve()

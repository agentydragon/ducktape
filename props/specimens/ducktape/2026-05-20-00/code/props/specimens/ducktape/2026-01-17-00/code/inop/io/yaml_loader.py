"""YAML loader for seeds and graders."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from inop.engine.models import SeedTask
from inop.io.logging_utils import DualOutputLogging

logger = DualOutputLogging.get_logger()


class GraderDataModel(BaseModel):
    """Pydantic model for grader data validation."""

    id: str  # This is the 'name' field from YAML
    description: str


class YamlLoader:
    """Handles loading YAML files."""

    def __init__(self, seeds_yaml_path: Path, graders_yaml_path: Path):
        self.seeds_yaml_path = Path(seeds_yaml_path)
        self.graders_yaml_path = Path(graders_yaml_path)
        self._seeds_models: list[SeedTask] | None = None
        self._graders_models: list[GraderDataModel] | None = None

    def _load_yaml_file(self, path: Path) -> Any:
        """Load YAML file content."""
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)

    @property
    def seeds_data(self) -> list[SeedTask]:
        """Load and validate seeds YAML as SeedTask models (cached)."""
        if self._seeds_models is None:
            data = self._load_yaml_file(self.seeds_yaml_path)
            if not isinstance(data, list):
                raise ValueError(f"Seeds YAML must contain a list of tasks, got {type(data)}")
            models: list[SeedTask] = []
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("Each seed task must be a mapping")
                models.append(SeedTask(**item))
            self._seeds_models = models
        return self._seeds_models

    @property
    def graders_data(self) -> list[GraderDataModel]:
        """Load graders YAML data as GraderDataModel (cached)."""
        if self._graders_models is None:
            data = self._load_yaml_file(self.graders_yaml_path)
            if not isinstance(data, dict) or "graders" not in data:
                raise ValueError(f"Graders YAML must be a dict with 'graders' key, got {type(data)}")
            models: list[GraderDataModel] = []
            for grader_data in data["graders"]:
                if not isinstance(grader_data, dict):
                    logger.warning("Skipping invalid grader data", data=grader_data)
                    continue

                # Map 'name' field to 'id' for consistency
                grader_data_copy = grader_data.copy()
                if "name" in grader_data_copy:
                    grader_data_copy["id"] = grader_data_copy.pop("name")

                models.append(GraderDataModel(**grader_data_copy))
            self._graders_models = models
        return self._graders_models


def load_yaml_files(seeds_yaml_path: Path | str, graders_yaml_path: Path | str) -> YamlLoader:
    """Create and return a configured YAML loader."""
    return YamlLoader(seeds_yaml_path=Path(seeds_yaml_path), graders_yaml_path=Path(graders_yaml_path))

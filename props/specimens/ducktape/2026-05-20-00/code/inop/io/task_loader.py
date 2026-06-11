"""Load and parse task type and runner configurations."""

from pathlib import Path

import yaml

from inop.engine.models import (
    RunnerConfig,
    RunnersYaml,
    TaskDefinition,
    TaskDefinitionsYaml,
    TaskTypeConfig,
    TaskTypeName,
    TaskTypesYaml,
)


def load_task_types(file_path: Path) -> dict[str, TaskTypeConfig]:
    """Load task type definitions from YAML file."""
    with file_path.open() as f:
        config = TaskTypesYaml.model_validate(yaml.safe_load(f))

    return {
        name: TaskTypeConfig(name=TaskTypeName(name), grading=cfg.grading) for name, cfg in config.task_types.items()
    }


def load_runner_configs(file_path: Path) -> dict[str, RunnerConfig]:
    """Load runner configurations from YAML file."""
    with file_path.open() as f:
        config = RunnersYaml.model_validate(yaml.safe_load(f))

    return config.runners


def load_task_definitions(file_path: Path, task_types: dict[str, TaskTypeConfig] | None = None) -> list[TaskDefinition]:
    """Load task definitions from seeds YAML file."""
    with file_path.open() as f:
        config = TaskDefinitionsYaml.model_validate(
            yaml.safe_load(f), context={"task_types": task_types} if task_types else None
        )

    return config.tasks

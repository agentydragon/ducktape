from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..config import SleepUntilUserMessagePolicy
from ..object_store import ObjectStoreClient
from ..tool_execution import ToolSpec
from .read_image import build_spec as build_read_image_spec
from .run_shell_command import build_spec as build_run_shell_command_spec
from .sleep_until_user_message import ConversationStatusProvider, build_spec as build_sleep_until_user_message_spec


def build_tool_specs(
    on_sleep: Callable[[], None],
    status_provider: ConversationStatusProvider,
    policy: SleepUntilUserMessagePolicy,
    workspace_path: Path,
    object_store: ObjectStoreClient | None,
) -> dict[str, ToolSpec]:
    specs = [build_run_shell_command_spec(), build_sleep_until_user_message_spec(on_sleep, status_provider, policy)]
    if object_store is not None:
        specs.append(build_read_image_spec(workspace_path, object_store))
    return {spec.name: spec for spec in specs}

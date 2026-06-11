from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points

import pluggy

PROJECT_NAME = "wt"
ENTRYPOINT_GROUP = "wt.plugins"

logger = logging.getLogger(__name__)


class _Spec:
    @pluggy.HookspecMarker(PROJECT_NAME)
    def wt_commands(self) -> dict[str, Callable] | None:
        """
        Return mapping of subcommand name -> callable
        Signatures supported:
        - async def run(args: list[str], client, config, io) -> int | None
        - def run(args: list[str], client, config, io) -> int | None
        """

    @pluggy.HookspecMarker(PROJECT_NAME)
    def wt_init(self, config) -> None:
        """Optional initialization hook; can modify config or set globals."""


class _Impl:
    @pluggy.HookimplMarker(PROJECT_NAME)
    def wt_commands(self) -> dict[str, Callable]:
        return {}

    @pluggy.HookimplMarker(PROJECT_NAME)
    def wt_init(self, config) -> None:
        pass


# Note: PluginIO removed as a thin wrapper; plugins should import shell_utils if needed.


def get_manager(config) -> pluggy.PluginManager:
    pm = pluggy.PluginManager(PROJECT_NAME)
    pm.add_hookspecs(_Spec)
    pm.register(_Impl())

    for ep in entry_points(group=ENTRYPOINT_GROUP):
        # Only catch expected plugin loading errors; let programming errors crash
        try:
            pm.register(ep.load())
        except (ImportError, AttributeError):
            # Plugin is misconfigured or missing; log full traceback and skip
            logger.exception("Failed to load plugin entry point %s (%s)", ep.name, ep.value)
            continue

    pm.hook.wt_init(config=config)
    return pm


def get_plugin_commands(pm: pluggy.PluginManager) -> dict[str, Callable]:
    commands: dict[str, Callable] = {}
    for mapping in pm.hook.wt_commands():
        commands.update(mapping or {})
    return commands


# resolve_command removed; callers should use get_plugin_commands(pm).get(name)

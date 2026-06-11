"""Base hook framework with JSON I/O handling."""

import json
import logging
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TypeVar

import yaml
from platformdirs import user_config_dir, user_state_dir

from claude_hooks.actions import (
    HookAction,
    NotificationAction,
    PostToolAction,
    PostToolContinue,
    PreCompactAction,
    PreToolAction,
    StopAction,
    SubagentStopAction,
    UserPromptSubmitAction,
)
from claude_hooks.inputs import (
    BaseHookInput,
    HookContext,
    NotificationInput,
    PostToolInput,
    PreCompactInput,
    PreToolInput,
    StopInput,
    SubagentStopInput,
    UserPromptSubmitInput,
)
from claude_hooks.logging_context import set_hook_context, setup_hook_logging

# Type variables for generic hook handling
InputT = TypeVar("InputT", bound=BaseHookInput)
OutputT = TypeVar("OutputT", bound=HookAction)


class HookBase[InputT: BaseHookInput, OutputT: HookAction](ABC):
    """Base class for all Claude Code hooks."""

    INPUT_MODEL: type[InputT]  # Subclasses must set this

    def __init__(self, hook_name: str):
        self.hook_name = hook_name
        self.logger = self._setup_logging()
        self.config = self._load_config()

    def _setup_logging(self) -> logging.Logger:
        """Setup logging to XDG state directory with context support."""
        state_dir = Path(user_state_dir("adgn-claude-hooks"))
        state_dir.mkdir(parents=True, exist_ok=True)
        log_file = state_dir / f"{self.hook_name}.log"

        logger = logging.getLogger(f"adgn-claude-hooks.{self.hook_name}")

        # Avoid duplicate handlers
        if not logger.handlers:
            handler = logging.FileHandler(log_file)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)

        # Setup contextual logging (this will add our filter and formatter)
        setup_hook_logging()

        return logger

    def _load_config(self) -> dict[str, Any]:
        """Load configuration from XDG config directory."""
        config_file = Path(user_config_dir("adgn-claude-hooks")) / "settings.yaml"
        if config_file.exists() and (config_data := yaml.safe_load(config_file.read_text())):
            result: dict[str, Any] = config_data.get(self.hook_name, {})
            return result
        return {}

    def run_hook(self) -> None:
        """Main pipeline: read → json → hook input → execute → hook output → json → exit"""
        try:
            # Read and log input
            raw_input = sys.stdin.read()
            self.logger.info(f"Raw input (first 200 chars): {raw_input[:200]}...")

            # Parse input
            input_data = json.loads(raw_input)

            # Log truncated parsed JSON
            def truncate_strings(obj, maxlen=100):
                if isinstance(obj, str):
                    return obj[:maxlen] + "..." if len(obj) > maxlen else obj
                if isinstance(obj, dict):
                    return {k: truncate_strings(v, maxlen) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [truncate_strings(item, maxlen) for item in obj]
                return obj

            truncated_data = truncate_strings(input_data, 100)
            self.logger.info(f"Parsed JSON: {json.dumps(truncated_data, indent=2)}")

            hook_input = self.INPUT_MODEL.model_validate(input_data)
            # TODO: should know what type hook_input is, not use defensive programming
            self.logger.info(f"Parsed input - event: {hook_input.hook_event_name}")

            # Create context from hook input (use hook_event_name from JSON)
            context = HookContext(
                hook_name=self.hook_name,
                hook_event=hook_input.hook_event_name,
                session_id=hook_input.session_id,
                cwd=hook_input.cwd,
            )

            # Set logging context for automatic injection into all log messages
            set_hook_context(invocation_id=context.invocation_id, name=self.hook_name, session_id=context.session_id)

            action = self.execute(hook_input, context)
            self.logger.info(f"Hook executed: {action}")

            # Convert to protocol JSON and exit
            protocol_dict = action.to_protocol()
            output_json = json.dumps(protocol_dict)
            self.logger.info(f"Output JSON: {output_json}")
            print(output_json)
            sys.exit(0)
        except Exception as e:
            self.logger.exception(f"Fatal error in run_hook: {e}")
            # Still need to output something to not break Claude
            fallback = PostToolContinue()
            print(json.dumps(fallback.to_protocol()))
            sys.exit(1)

    @abstractmethod
    def execute(self, hook_input: InputT, context: HookContext) -> OutputT:
        """Execute the hook logic. Must be implemented by subclasses."""
        ...


# Specialized base classes for each hook type
# ===========================================


class PreToolUseHook(HookBase[PreToolInput, PreToolAction]):
    INPUT_MODEL = PreToolInput


class PostToolUseHook(HookBase[PostToolInput, PostToolAction]):
    INPUT_MODEL = PostToolInput


class UserPromptSubmitHook(HookBase[UserPromptSubmitInput, UserPromptSubmitAction]):
    INPUT_MODEL = UserPromptSubmitInput


class StopHook(HookBase[StopInput, StopAction]):
    INPUT_MODEL = StopInput


class SubagentStopHook(HookBase[SubagentStopInput, SubagentStopAction]):
    INPUT_MODEL = SubagentStopInput


class NotificationHook(HookBase[NotificationInput, NotificationAction]):
    INPUT_MODEL = NotificationInput


class PreCompactHook(HookBase[PreCompactInput, PreCompactAction]):
    INPUT_MODEL = PreCompactInput

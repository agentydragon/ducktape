"""Hook handler implementation for Claude Code hooks."""

import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import platformdirs

from llm.claude_code_api import (
    BaseHookRequest,
    BaseResponse,
    BashToolCall,
    EditToolCall,
    HookEventName,
    MultiEditToolCall,
    NotificationRequest,
    PostToolUseRequest,
    PreToolUseRequest,
    ReadToolCall,
    StopRequest,
    SubagentStopRequest,
    WriteToolCall,
)
from llm.claude_linter_v2.access.context import PredicateContext
from llm.claude_linter_v2.access.rule_engine import RuleEngine
from llm.claude_linter_v2.check_python import check_python_file
from llm.claude_linter_v2.checkers_v2 import filter_violations
from llm.claude_linter_v2.config.clean_models import ModularConfig
from llm.claude_linter_v2.config.loader import ConfigLoader
from llm.claude_linter_v2.config.models import (
    AutofixCategory,
    NotificationHookConfig,
    PostToolHookConfig,
    RuleAction,
    StopHookConfig,
    Violation,
)
from llm.claude_linter_v2.diff.categorizer import ViolationCategory
from llm.claude_linter_v2.diff.intelligence import DiffIntelligence
from llm.claude_linter_v2.hooks.exceptions import HookBugError
from llm.claude_linter_v2.hooks.formatting import format_access_denial, format_llm_message
from llm.claude_linter_v2.hooks.validation import validate_hook_outcome
from llm.claude_linter_v2.linters.python_formatter import PythonFormatter
from llm.claude_linter_v2.llm_analyzer import LLMAnalyzer
from llm.claude_linter_v2.notifications import close_desktop_notification, send_desktop_notification
from llm.claude_linter_v2.pattern_matcher import PatternMatcher
from llm.claude_linter_v2.session.manager import SessionManager
from llm.claude_linter_v2.session.violations import ViolationTracker
from llm.claude_linter_v2.types import SessionID
from llm.claude_linter_v2.utils.gitignore import get_git_tracked_files
from llm.claude_outcomes import (
    HookOutcome,
    NotificationAcknowledge,
    PostToolNotifyLLM,
    PostToolSuccess,
    PreToolApprove,
    PreToolDeny,
    StopAllow,
    StopPrevent,
    SubagentStopAllow,
)

logger = logging.getLogger(__name__)

# Type alias for tool calls that have a file_path attribute
FilePathToolCall = EditToolCall | WriteToolCall | ReadToolCall | MultiEditToolCall


# Map hook type names to their request classes
HOOK_REQUEST_TYPES: dict[str, type[BaseHookRequest]] = {
    "PreToolUse": PreToolUseRequest,
    "PostToolUse": PostToolUseRequest,
    "Stop": StopRequest,
    "SubagentStop": SubagentStopRequest,
    "Notification": NotificationRequest,
}


class HookHandler:
    """Handles all hook types with type safety."""

    def __init__(self) -> None:
        self.session_manager = SessionManager()
        self.config_loader = ConfigLoader()
        self._warnings: dict[SessionID, str] = {}  # Store warnings per session
        self.violation_tracker = ViolationTracker(self.session_manager)
        self.diff_intelligence = DiffIntelligence(context_distance=3)

        # Initialize these once instead of lazy init
        config = self.config_loader.config
        self.rule_engine = RuleEngine(config, self.session_manager)
        self.pattern_matcher = PatternMatcher(config.pattern_rules)
        self.llm_analyzer = LLMAnalyzer(config.llm_analysis)

        self._setup_logging()

    def _setup_logging(self) -> None:
        """Set up session-based logging."""
        # Create logs directory
        log_dir = Path(platformdirs.user_cache_dir("claude-linter")) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir

        # Configure Python logging based on config
        config = self.config_loader.config

        # Set up root logger
        root_logger = logging.getLogger()

        # Clear any existing handlers to avoid duplicates
        root_logger.handlers.clear()

        # Console handler (always present for critical errors)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.ERROR)
        console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

        # File handler if configured
        if config.log_file:
            log_file = Path(config.log_file)
            log_file.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(log_file)

            # Set log level from config
            log_level = config.log_level
            try:
                level_value = logging._nameToLevel.get(log_level.upper(), logging.INFO)
                file_handler.setLevel(level_value)
            except (AttributeError, KeyError):
                file_handler.setLevel(logging.INFO)
                logger.warning(f"Invalid log level '{log_level}', using INFO")

            file_formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
            )
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)

            # Set root logger to the lowest level to let handlers filter
            root_logger.setLevel(logging.DEBUG)

            logger.info(f"Logging configured: level={log_level}, file={log_file}")

    def _log_hook_call(
        self, session_id: SessionID, hook_type: str, request: BaseHookRequest, outcome: Any, response: Any
    ) -> None:
        """Log detailed hook information to session log file."""
        log_file = self.log_dir / f"{session_id}.log"

        timestamp = datetime.now().isoformat()

        # Build log entry
        log_entry = {
            "timestamp": timestamp,
            "hook_type": hook_type,
            "session_id": str(session_id),
            "request": {
                "type": type(request).__name__,
                "data": request.model_dump(mode="json"),  # All requests are Pydantic models
            },
            "outcome": {"type": type(outcome).__name__, "data": str(outcome)},
            "response": response.model_dump(mode="json"),  # All responses are Pydantic models
            "decision_details": {},
        }

        # Add specific details based on hook type
        if isinstance(request, PreToolUseRequest | PostToolUseRequest):
            log_entry["decision_details"]["tool"] = request.tool_call.tool_name
            log_entry["decision_details"]["tool_input"] = request.tool_call.model_dump(mode="json")

        # Write to log file
        with log_file.open("a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def _log_decision(self, session_id: SessionID, decision_point: str, details: dict[str, Any]) -> None:
        """Log a specific decision point."""
        log_file = self.log_dir / f"{session_id}.log"

        log_entry = {"timestamp": datetime.now().isoformat(), "decision_point": decision_point, "details": details}

        with log_file.open("a") as f:
            f.write(f"DECISION: {json.dumps(log_entry)}\n")

    def handle(self, hook_type: str, request: BaseHookRequest) -> BaseResponse:
        """
        Main entry point - handles any hook type.

        This is the generic handler that dispatches to typed handlers.
        """
        # Get session ID (required)
        session_id = request.session_id
        self._track_session(request, session_id)

        # Log the incoming request
        logger.info(f"Hook call: {hook_type} for session {session_id}")

        # Dispatch to typed handler
        outcome = self._dispatch_hook(hook_type, request, session_id)

        # Validate outcome type
        validate_hook_outcome(HookEventName(hook_type), outcome)

        # Convert to response
        response = outcome.to_claude_response()

        # Log the complete interaction
        self._log_hook_call(session_id, hook_type, request, outcome, response)

        return response

    def _track_session(self, request: BaseHookRequest, session_id: SessionID) -> None:
        """Track session with appropriate working directory."""
        working_dir = Path.cwd()

        # Try to get working dir from tool input if available
        if isinstance(request, PreToolUseRequest | PostToolUseRequest):
            tool_call = request.tool_call
            if isinstance(tool_call, FilePathToolCall):
                working_dir = tool_call.file_path.parent

        self.session_manager.track_session(session_id, working_dir)

    def _dispatch_hook(self, hook_type: str, request: BaseHookRequest, session_id: SessionID) -> HookOutcome:
        """Dispatch to appropriate typed handler."""
        if hook_type == "PreToolUse" and isinstance(request, PreToolUseRequest):
            return self._handle_pre_hook(request, session_id)
        if hook_type == "PostToolUse" and isinstance(request, PostToolUseRequest):
            return self._handle_post_hook(request, session_id)
        if hook_type == "Stop" and isinstance(request, StopRequest):
            return self._handle_stop_hook(request, session_id)
        if hook_type == "SubagentStop" and isinstance(request, SubagentStopRequest):
            return self._handle_subagent_stop(request, session_id)
        if hook_type == "Notification" and isinstance(request, NotificationRequest):
            return self._handle_notification(request, session_id)
        raise HookBugError(f"Invalid hook type: {hook_type}")

    def _handle_pre_hook(self, request: PreToolUseRequest, session_id: SessionID) -> HookOutcome:
        """Handle PreToolUse with early bailout pattern."""
        config = self.config_loader.config
        tool_call = request.tool_call
        tool_name = tool_call.tool_name
        file_path = tool_call.file_path if isinstance(tool_call, FilePathToolCall) else None
        content = tool_call.content if isinstance(tool_call, WriteToolCall) else None

        logger.info(f"Pre-hook for {tool_name} in session {session_id}")

        # Clear any existing notification for this session
        self._clear_notification(session_id)

        # Log detailed request info
        self._log_decision(
            session_id,
            "pre_hook_start",
            {"tool": tool_name, "file_path": str(file_path) if file_path else None, "has_content": bool(content)},
        )

        # Access control check - early bailout
        action, message = self._check_access_control(request, session_id)
        self._log_decision(
            session_id, "access_control", {"action": action.value if action else None, "message": message}
        )

        if action == RuleAction.DENY:
            return PreToolDeny(
                llm_message=format_access_denial(
                    predicate=f"{tool_name}('{file_path}')", session_id=session_id, message=message
                )
            )

        # Store warnings for post-hook if needed
        if action == RuleAction.WARN and message:
            self._warnings[session_id] = message

        # Python violations check - early bailout
        is_python = self._is_python_file(request)
        self._log_decision(
            session_id, "file_type_check", {"is_python": is_python, "file_path": str(file_path) if file_path else None}
        )

        if not is_python:
            return PreToolApprove()

        violations = self._check_python_violations(request, config)
        self._log_decision(
            session_id,
            "python_violations",
            {
                "count": len(violations),
                "violations": [v.model_dump() for v in violations[:3]],  # All violations are Pydantic models
            },
        )

        # Check for LLM analysis if enabled and applicable
        llm_violations = []
        if (
            config.llm_analysis.enabled
            and isinstance(tool_call, WriteToolCall | EditToolCall | MultiEditToolCall)
            and content
        ):
            llm_ok, llm_message, llm_violations_found = self.llm_analyzer.analyze_code(
                tool_call=tool_call, content=content
            )

            if not llm_ok and llm_message:
                # LLM found critical issues - add them to violations
                llm_violations.extend(llm_violations_found)

            self._log_decision(
                session_id,
                "llm_analysis",
                {"ok": llm_ok, "message": llm_message, "violations_count": len(llm_violations_found)},
            )

        # Combine all violations
        all_violations = violations + llm_violations

        # Filter to only blocking violations
        blocking_violations = filter_violations(all_violations, config, "pre")

        if not blocking_violations:
            return PreToolApprove()

        formatted_violations = "\n".join(
            f"Line {v.line}: {v.message}" for v in blocking_violations[: config.max_errors_to_show]
        )

        file_path_str = str(file_path) if file_path else ""
        formatted = format_llm_message(
            "Python code contains hard-blocked patterns:",
            formatted_violations,
            "\n".join(
                [
                    "Fix these patterns:",
                    "- Bare except: Use specific exception types",
                    "- hasattr/getattr: Use proper type checking",
                    "",
                    f"Check violations: cl2 check {file_path_str}",
                    f"Override: cl2 session allow '{tool_name}(\"{file_path_str}\")' --session {session_id}",
                ]
            ),
        )

        # Track all violations (not just blocking ones) for the Stop hook
        if all_violations and file_path:
            self.violation_tracker.add_violations(
                session_id=session_id,
                violations=all_violations,
                file_path=file_path,
                severity="mixed",  # Contains both blocking and non-blocking
            )

        return PreToolDeny(llm_message=formatted)

    def _handle_post_hook(self, request: PostToolUseRequest, session_id: SessionID) -> HookOutcome:
        """Handle PostToolUse."""
        config = self.config_loader.config
        messages = []
        tool_call = request.tool_call
        tool_name = tool_call.tool_name
        file_path = tool_call.file_path if isinstance(tool_call, FilePathToolCall) else None

        logger.info(f"Post-hook for {tool_name} in session {session_id}")

        # Clear any existing notification for this session
        self._clear_notification(session_id)

        # Log detailed info
        self._log_decision(
            session_id,
            "post_hook_start",
            {
                "tool": tool_name,
                "file_path": str(file_path) if file_path else None,
                "has_tool_response": request.tool_response is not None or request.tool_result is not None,
            },
        )

        # Apply autofix if configured
        autofix_msg = self._try_autofix(request, config)
        self._log_decision(
            session_id, "autofix_attempt", {"attempted": autofix_msg is not None, "message": autofix_msg}
        )
        if autofix_msg:
            messages.append(autofix_msg)

        # Add any warnings from pre-hook
        if session_id in self._warnings:
            warning = self._warnings.pop(session_id)
            messages.append(f"Warning: {warning}")

        post_hook_config = config.hooks.get("post")
        if isinstance(post_hook_config, PostToolHookConfig) and post_hook_config.inject_permissions:
            perms = self._build_permissions_info(session_id)
            if perms:
                messages.append(perms)

        # Check remaining violations
        if self._is_python_file(request) and file_path:
            self._update_violation_tracking(request, session_id)

        # Log final decision
        self._log_decision(
            session_id,
            "post_hook_outcome",
            {
                "has_messages": bool(messages),
                "messages": messages,
                "has_autofix": bool(autofix_msg),
                "has_important_info": (self._has_important_info(messages) if messages else False),
            },
        )

        # Return appropriate outcome
        if not messages:
            return PostToolSuccess()

        # Determine if this needs LLM attention
        if autofix_msg:
            return PostToolNotifyLLM(llm_message=f"Autofix: {' | '.join(messages)}")
        if self._has_important_info(messages):
            return PostToolNotifyLLM(llm_message=f"Violations: {' | '.join(messages)}")
        return PostToolSuccess()

    def _handle_stop_hook(self, request: StopRequest, session_id: SessionID) -> HookOutcome:
        """
        Handle Stop hook (Claude ending its turn).

        Note: This fires when Claude wants to end its response to the user,
        NOT when a session ends. Sessions persist across multiple turns.
        """
        config = self.config_loader.config

        logger.info(f"Stop hook for session {session_id}")

        stop_hook_config = config.hooks.get("stop")
        if isinstance(stop_hook_config, StopHookConfig) and not stop_hook_config.quality_gate:
            return StopAllow()

        # TODO: Read transcript to find only files touched since last Stop
        # For now, scan all Python files in working directory

        # Get the working directory
        # TODO: Track directory per session
        working_dir = Path.cwd()

        # IMPORTANT: Run a fresh scan - DO NOT use stale violation tracking
        all_violations = []
        files_with_errors: dict[str, list] = {}

        # Find all Python files in the working directory that are tracked by git
        # This respects .gitignore and won't scan node_modules, venv, etc.
        python_files = get_git_tracked_files(working_dir, "*.py")
        logger.info(f"Stop hook: Found {len(python_files)} git-tracked Python files in {working_dir}")

        for py_file in python_files:
            if not py_file.exists():
                continue

            try:
                file_content = py_file.read_text()
            except (OSError, UnicodeDecodeError) as e:
                logger.debug(f"Could not read {py_file}: {e}")
                continue

            # Use the pure function to check for violations
            violations = check_python_file(
                file_path=py_file,
                content=file_content,
                config=config,
                critical_only=False,  # Stop hook checks all violations
            )

            logger.info(f"Stop hook: Checked {py_file}, found {len(violations)} violations")
            if violations:
                for v in violations:
                    logger.info(f"  - Violation: {v.rule} at line {v.line}: {v.message}")

            if violations:
                # Only track violations that block stop hooks for quality gate
                blocking_violations = filter_violations(violations, config, "stop")
                logger.info(f"Stop hook: {len(blocking_violations)} are blocking violations")
                if blocking_violations:
                    files_with_errors[str(py_file)] = blocking_violations
                    all_violations.extend(blocking_violations)

        logger.info(f"Stop hook: Total error violations: {len(all_violations)}")
        # If no errors found, allow stop
        if not all_violations:
            return StopAllow()

        # Build detailed error message
        error_parts = [f"Code has {len(all_violations)} errors that must be fixed:"]

        # Get configured limits
        max_files = stop_hook_config.max_files_to_show if isinstance(stop_hook_config, StopHookConfig) else 5
        max_per_file = stop_hook_config.max_violations_per_file if isinstance(stop_hook_config, StopHookConfig) else 3

        # Show up to max_files files with their violations
        for file_path, file_violations in list(files_with_errors.items())[:max_files]:
            error_parts.append(f"\n{file_path}:")
            # Show up to max_per_file violations per file
            for v in file_violations[:max_per_file]:
                # v.rule is an optional field in Violation model
                if v.rule:
                    error_parts.append(f"  Line {v.line}: {v.message} [{v.rule}]")
                else:
                    error_parts.append(f"  Line {v.line}: {v.message}")
            if len(file_violations) > max_per_file:
                error_parts.append(f"  ... and {len(file_violations) - max_per_file} more")

        if len(files_with_errors) > max_files:
            error_parts.append(f"\n... and {len(files_with_errors) - max_files} more files")

        # Add single command to check all files with violations
        error_parts.append("\n\nCommand to check all violations:")
        all_files = list(files_with_errors.keys())
        error_parts.append(f"  cl2 check {' '.join(all_files)}")

        return StopPrevent(llm_message="".join(error_parts))

    def _handle_subagent_stop(self, request: SubagentStopRequest, session_id: SessionID) -> HookOutcome:
        """Handle SubagentStop."""
        logger.info(f"SubagentStop hook for session {session_id}")
        # For now, always allow subagent to stop
        return SubagentStopAllow()

    def _handle_notification(self, request: NotificationRequest, session_id: SessionID) -> HookOutcome:
        """Handle Notification."""
        logger.info(f"Notification hook for session {session_id}")

        # Get notification hook config
        config = self.config_loader.config
        notification_config = config.hooks.get("notification")

        # Import proper type for type checking

        # Check if we should send to D-Bus using proper type checking
        if (
            isinstance(notification_config, NotificationHookConfig)
            and notification_config.send_to_dbus
            and (request.message or request.title)
        ):
            self._send_dbus_notification(
                title=request.title or "Claude Code",
                message=request.message or "",
                session_id=session_id,
                urgency=notification_config.urgency,
            )

        return NotificationAcknowledge()

    def _send_dbus_notification(self, title: str, message: str, session_id: SessionID, urgency: str = "normal") -> None:
        """Send a notification via D-Bus, replacing any existing notification for this session."""
        # Import here to avoid circular import

        try:
            # Get existing notification ID for this session (if any) from session data
            replaces_id = self.session_manager.get_notification_id(session_id) or 0

            # Send notification, replacing the previous one if it exists
            notification_id = send_desktop_notification(title, message, urgency=urgency, replaces_id=replaces_id)

            # Store the notification ID in session data
            self.session_manager.set_notification_id(session_id, notification_id)

            logger.info(
                f"Sent D-Bus notification for session {session_id}: {title} "
                f"(ID: {notification_id}, replaced: {replaces_id})"
            )
        except (OSError, ImportError, AttributeError) as e:
            logger.error(f"Failed to send D-Bus notification: {e}", exc_info=True)

    def _clear_notification(self, session_id: SessionID) -> None:
        """Clear any existing notification for this session."""
        notification_id = self.session_manager.get_notification_id(session_id)
        if notification_id:
            # Import here to avoid circular import

            try:
                # Try to close D-Bus notification if function available
                with contextlib.suppress(Exception):
                    close_desktop_notification(notification_id)

                self.session_manager.clear_notification_id(session_id)
                logger.debug(f"Cleared notification {notification_id} for session {session_id}")
            except (OSError, ImportError, AttributeError) as e:
                logger.debug(f"Failed to clear notification for session {session_id}: {e}")

    # Helper methods
    def _check_access_control(self, request: PreToolUseRequest, session_id: SessionID) -> tuple[RuleAction, str | None]:
        """Check access control rules."""
        tool_call = request.tool_call
        args: dict[str, Any] = {
            "file_path": tool_call.file_path if isinstance(tool_call, FilePathToolCall) else None,
            "content": tool_call.content if isinstance(tool_call, WriteToolCall) else None,
            "old_string": tool_call.old_string if isinstance(tool_call, EditToolCall) else None,
            "command": tool_call.command if isinstance(tool_call, BashToolCall) else None,
        }
        context = PredicateContext(tool=tool_call.tool_name, args=args, session_id=session_id, timestamp=datetime.now())

        return self.rule_engine.evaluate_access(context, session_id)

    def _is_python_file(self, request: PreToolUseRequest | PostToolUseRequest) -> bool:
        """Check if request is for a Python file with content."""
        tool_call = request.tool_call
        if not isinstance(tool_call, WriteToolCall):
            return False
        return tool_call.file_path.suffix == ".py"

    def _check_python_violations(self, request: PreToolUseRequest, config: ModularConfig) -> list[Violation]:
        """Check for Python AST and ruff violations."""
        tool_call = request.tool_call
        if not isinstance(tool_call, WriteToolCall):
            return []

        return check_python_file(
            file_path=tool_call.file_path,
            content=tool_call.content,
            config=config,
            critical_only=True,  # Pre-hook only checks critical violations
        )

    def _try_autofix(self, request: PostToolUseRequest, config: ModularConfig) -> str | None:
        """Try to apply autofix and return message if successful."""

        hook_config = config.hooks.get("post")

        # Type check instead of hasattr - only PostToolHookConfig has auto_fix
        if not isinstance(hook_config, PostToolHookConfig):
            self._log_decision(request.session_id, "autofix_skip", {"reason": "not_post_tool_hook_config"})
            return None

        tool_call = request.tool_call
        # Autofix only applies to WriteToolCall (has file_path and content)
        if not isinstance(tool_call, WriteToolCall):
            return None

        is_python = tool_call.file_path.suffix == ".py"

        # Log autofix decision details
        self._log_decision(
            request.session_id,
            "autofix_check",
            {
                "auto_fix_enabled": hook_config.auto_fix,
                "file_path": str(tool_call.file_path),
                "is_python": is_python,
                "has_content": True,
                "tool_name": tool_call.tool_name,
            },
        )

        if not hook_config.auto_fix or not is_python:
            return None

        categories = hook_config.autofix_categories or [AutofixCategory.ALL]

        # Format the code
        formatter = PythonFormatter(config.python_tools)
        formatted_code, changes = formatter.format_code(tool_call.content, tool_call.file_path, categories)

        if not changes or formatted_code == tool_call.content:
            return None

        try:
            tool_call.file_path.write_text(formatted_code)
            logger.info(f"Applied autofix to {tool_call.file_path}: {changes}")
            return f"Autofix applied: {', '.join(changes)}"
        except (OSError, PermissionError, UnicodeDecodeError) as e:
            logger.error(f"Failed to apply autofix: {e}")
            return f"Autofix failed: {e}"

    def _update_violation_tracking(self, request: PostToolUseRequest, session_id: SessionID) -> None:
        """Update violation tracking after tool execution."""
        tool_call = request.tool_call
        if not isinstance(tool_call, FilePathToolCall):
            return
        file_path = tool_call.file_path
        if not file_path.exists():
            return

        config = self.config_loader.config
        file_content = file_path.read_text()

        # Use the pure function to check all violations
        all_violations = check_python_file(
            file_path=file_path,
            content=file_content,
            config=config,
            critical_only=False,  # Post-hook tracks all violations
        )

        if all_violations:
            # Only use diff intelligence for Edit/MultiEdit tools with tool_response
            tool_response = request.tool_response if request.tool_response is not None else request.tool_result
            if isinstance(tool_call, EditToolCall | MultiEditToolCall) and tool_response:
                # Use diff intelligence to categorize violations
                categorized_groups = self.diff_intelligence.analyze(
                    tool_call=tool_call, tool_response=tool_response, violations=all_violations
                )

                # Only track in-diff and near-diff violations as important
                important_violations = (
                    categorized_groups[ViolationCategory.IN_DIFF] + categorized_groups[ViolationCategory.NEAR_DIFF]
                )

                if important_violations:
                    # Convert back to plain violations for tracker
                    violations_to_track = [cv.violation for cv in important_violations]
                    self.violation_tracker.add_violations(
                        session_id=session_id,
                        violations=violations_to_track,
                        file_path=file_path,
                        severity="mixed",  # Let the Stop hook decide what blocks
                    )
                else:
                    # Only out-of-diff violations remain - mark file as effectively fixed
                    self.violation_tracker.mark_file_fixed(session_id, file_path)
            # For other tools (Write, etc), or when tool_response is missing, track all violations
            else:
                self.violation_tracker.add_violations(
                    session_id=session_id, violations=all_violations, file_path=file_path, severity="mixed"
                )
        else:
            # File is completely clean - mark as fixed
            self.violation_tracker.mark_file_fixed(session_id, file_path)

    def _has_important_info(self, messages: list[str]) -> bool:
        """Check if messages contain important info that Claude should see."""
        # Simple heuristic - if we applied autofix or have warnings, it's important
        return any("autofix" in msg.lower() or "warning:" in msg.lower() for msg in messages)

    def _build_permissions_info(self, session_id: SessionID) -> str | None:
        """Build a string describing current permissions."""
        rules = self.session_manager.get_session_rules(session_id)
        if not rules:
            return None

        lines = ["You have blanket approval for:"]
        for rule in rules:
            if rule.action == "allow":
                predicate = rule.predicate
                # Simplify common predicates for readability
                if predicate.startswith("Edit(") and predicate.endswith(")"):
                    pattern = predicate[5:-1].strip("\"'")
                    lines.append(f"- Editing files matching {pattern}")
                elif predicate == "safe_git_commands()":
                    lines.append("- Safe git commands (status, diff, add, commit, etc)")
                else:
                    lines.append(f"- {predicate}")

        return "\n".join(lines) if len(lines) > 1 else None

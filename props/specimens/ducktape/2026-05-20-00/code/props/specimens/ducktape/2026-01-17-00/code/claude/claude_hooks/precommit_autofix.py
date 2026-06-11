"""Pre-commit autofix hook implementation."""

import ast
import subprocess
import textwrap
import traceback
from dataclasses import dataclass
from pathlib import Path

import pygit2
from platformdirs import user_state_dir

from claude_hooks.actions import PostToolAction, PostToolContinue, PostToolFeedbackToClaude
from claude_hooks.base import PostToolUseHook
from claude_hooks.config import AutofixerConfig
from claude_hooks.inputs import HookContext, PostToolInput
from claude_hooks.logging_context import get_current_invocation_id
from claude_hooks.tool_models import EditInput, MultiEditInput, WriteInput

PRECOMMIT_CONFIG_FILE = ".pre-commit-config.yaml"


@dataclass
class ChangesMade:
    """Pre-commit ran successfully and made changes to the file."""


@dataclass
class NoChanges:
    """Pre-commit ran successfully but made no changes to the file."""


@dataclass
class Crashed:
    """Pre-commit failed unexpectedly with detailed error information."""

    stdout: str
    stderr: str
    exit_code: int


PreCommitResult = ChangesMade | NoChanges | Crashed


def extract_file_path(tool_input) -> Path | None:
    """Extract file path from tool input if available."""
    if isinstance(tool_input, EditInput | MultiEditInput | WriteInput):
        return Path(tool_input.file_path)
    return None


def truncate_output(output: str, max_lines: int = 20) -> str:
    """Truncate output if too long, showing first and last lines.

    Args:
        output: Output string to truncate
        max_lines: Maximum lines to show per section (first/last)

    Returns:
        Truncated output string
    """
    if not output.strip():
        return "(no output)"

    lines = output.strip().split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines * 2:
        return "\n".join(lines)

    first_part = lines[:max_lines]
    last_part = lines[-max_lines:]
    omitted_count = total_lines - (max_lines * 2)

    return "\n".join([*first_part, f"... {omitted_count} lines omitted ...", *last_part])


def format_crashed_output(stdout: str, stderr: str, exit_code: int) -> str:
    """Format output from crashed pre-commit run for user display.

    Args:
        stdout: Standard output from pre-commit
        stderr: Standard error from pre-commit
        exit_code: Exit code from pre-commit

    Returns:
        Formatted output string with truncated stdout/stderr
    """
    parts = []

    if stdout.strip():
        parts.append("STDOUT:")
        parts.append(truncate_output(stdout))
        parts.append("")  # Empty line separator

    if stderr.strip():
        parts.append("STDERR:")
        parts.append(truncate_output(stderr))
        parts.append("")  # Empty line separator

    parts.append(f"Exit code: {exit_code}")

    return "\n".join(parts)


def check_python_syntax(file_path: Path) -> tuple[bool, str | None]:
    """Check if a Python file has valid syntax.

    Returns:
        (is_valid, error_message): Tuple with validity and error message if invalid
    """
    if file_path.suffix != ".py":
        return True, None  # Not a Python file, no syntax check needed

    try:
        source_code = Path(file_path).read_text(encoding="utf-8")

        # Parse the source code to check for syntax errors
        ast.parse(source_code, filename=str(file_path))
        return True, None
    except SyntaxError as e:
        error_msg = f"SyntaxError in {file_path.name}:{e.lineno}:{e.offset}: {e.msg}"
        return False, error_msg
    except (OSError, UnicodeDecodeError) as e:
        error_msg = f"Error reading {file_path.name}: {e}"
        return False, error_msg


class PreCommitAutoFixerHook(PostToolUseHook):
    """Hook that automatically runs pre-commit autofix on Claude-modified files."""

    def __init__(self):
        super().__init__("precommit_autofix")
        self.autofixer_config = AutofixerConfig.model_validate(self.config)

    def execute(self, hook_input: PostToolInput, context: HookContext) -> PostToolAction:
        self.logger.info(
            f"Config: enabled={self.autofixer_config.enabled}, dry_run={self.autofixer_config.dry_run}, tools={self.autofixer_config.tools}"
        )

        if not self.autofixer_config.enabled:
            self.logger.info("Hook disabled by configuration")
            return PostToolContinue()

        if hook_input.tool_name not in self.autofixer_config.tools:
            self.logger.info(f"Tool {hook_input.tool_name} not in tools list")
            return PostToolContinue()

        file_path = extract_file_path(hook_input.tool_input)
        if not file_path:
            self.logger.info("No file path found in tool input")
            return PostToolContinue()

        if not file_path.exists():
            self.logger.info(f"File does not exist: {file_path}")
            return PostToolContinue()

        self.logger.info(f"Processing file: {file_path}")

        # Check Python syntax before running pre-commit
        is_valid, syntax_error = check_python_syntax(file_path)
        if not is_valid:
            self.logger.warning(f"Skipping pre-commit due to syntax error: {syntax_error}")
            return PostToolFeedbackToClaude(feedback_to_claude=f"⚠️ Fix {syntax_error}.")

        try:
            result = self._run_precommit_autofix(file_path, context)
            if isinstance(result, ChangesMade):
                message = "🧹 pre-commit autofixes applied"
                self.logger.info(message)
                return PostToolFeedbackToClaude(feedback_to_claude=message)
            if isinstance(result, Crashed):
                # Pre-commit failed unexpectedly, show user feedback with formatted output
                formatted_output = format_crashed_output(result.stdout, result.stderr, result.exit_code)
                return PostToolFeedbackToClaude(
                    feedback_to_claude=f"⚠️ Pre-commit failed on {file_path.name}\nCommand: pre-commit run --files {file_path}\n\n{formatted_output}"
                )
        except Exception as e:
            # Catch all exceptions to provide user-friendly error messages instead of crashing Claude Code.
            # This includes file system errors (OSError, FileNotFoundError), subprocess failures
            # (SubprocessError), and any unexpected errors from dependencies like pre-commit itself.
            # The broad catch ensures users get actionable feedback with logs and tracebacks
            # rather than cryptic tool failures.
            self.logger.exception(f"Pre-commit autofix failed: {e}")
            # Show feedback for unexpected exceptions with debugging guidance
            tb_str = traceback.format_exc()
            invocation_id = get_current_invocation_id() or "unknown"
            log_path = Path(user_state_dir("adgn-claude-hooks")) / f"{self.hook_name}.log"

            debug_message = textwrap.dedent(f"""
                Unhandled {type(e).__name__} from PreCommitAutoFixerHook: {e!s}
                Logs: {log_path}
                Look for invocation ID: {invocation_id}
                Traceback:
                {tb_str}
            """).strip()

            return PostToolFeedbackToClaude(feedback_to_claude=debug_message)
        return PostToolContinue()

    def _run_precommit_autofix(self, file_path: Path, context: HookContext) -> PreCommitResult:
        """Run pre-commit on specific file.

        Returns:
            ChangesMade: File was modified by pre-commit
            NoChanges: Pre-commit ran successfully but made no changes
            Crashed: Pre-commit failed unexpectedly with detailed error info
        """

        if self.autofixer_config.dry_run:
            self.logger.info(f"DRY RUN: Would run pre-commit on {file_path}")
            return NoChanges()

        try:
            original_mtime = file_path.stat().st_mtime
            self.logger.info(f"Original mtime: {original_mtime}")

            config_root = self._get_precommit_root(file_path)

            config_file = config_root / PRECOMMIT_CONFIG_FILE
            cmd = ["pre-commit", "run", "--config", str(config_file), "--files", str(file_path)]
            self.logger.info(f"Running command: {cmd}")

            result = subprocess.run(
                cmd,
                cwd=config_root,
                capture_output=True,
                text=True,
                timeout=self.autofixer_config.timeout_seconds,
                check=False,  # Don't raise on non-zero exit
            )

            # Log subprocess result for debugging
            self.logger.info(f"Pre-commit exit code: {result.returncode}")
            if result.stdout.strip():
                self.logger.info(f"Pre-commit stdout: {result.stdout.strip()}")
            if result.stderr.strip():
                self.logger.warning(f"Pre-commit stderr: {result.stderr.strip()}")

            # Handle different exit codes
            # See: https://pre-commit.com/#exit-codes
            if result.returncode in (0, 1):
                # Exit code 0: Success (standard Unix convention)
                # Exit code 1: "A detected / expected error" - hooks found issues and possibly fixed them
                new_mtime = file_path.stat().st_mtime
                self.logger.info(f"New mtime: {new_mtime}, changed: {new_mtime > original_mtime}")
                return ChangesMade() if new_mtime > original_mtime else NoChanges()
            # Exit code 3: "An unexpected error", 130: "interrupted by ^C", etc.
            self.logger.warning(f"Pre-commit unexpected exit code {result.returncode}")
            return Crashed(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)
        except Exception as e:
            self.logger.exception(f"Error in _run_precommit_autofix: {e}")
            # Re-raise exceptions that aren't pre-commit failures
            raise

    def _get_precommit_root(self, file_path: Path) -> Path:
        """Get pre-commit configuration root directory using pygit2."""
        search_dir = file_path.parent if file_path.is_file() else file_path

        # Use pygit2 to find repository root, starting from file's directory
        self.logger.info(f"Looking for git repo starting from: {search_dir}")
        git_path = pygit2.discover_repository(str(search_dir))
        if git_path is None:
            self.logger.info(f"Not in git repo: {search_dir}")
            raise RuntimeError(f"Not in a git repository: {search_dir}")

        repo = pygit2.Repository(git_path)
        repo_root = Path(repo.workdir).resolve()
        self.logger.info(f"Found git repo root: {repo_root}")
        if (repo_root / PRECOMMIT_CONFIG_FILE).exists():
            self.logger.info(f"Found pre-commit config at: {repo_root / PRECOMMIT_CONFIG_FILE}")
            return repo_root

        raise RuntimeError(f"No pre-commit config in git repo root: {repo_root}")


def main():
    PreCommitAutoFixerHook().run_hook()


if __name__ == "__main__":
    main()

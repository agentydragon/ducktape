"""Interface library for running pre-commit hooks programmatically.

Wraps pre-commit's Python internals to provide structured per-hook results
without subprocess calls or stdout parsing. All pre-commit imports are
confined to this module.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import os
from collections.abc import Generator, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pre_commit.all_languages import languages
from pre_commit.clientlib import load_config
from pre_commit.commands.run import Classifier
from pre_commit.constants import CONFIG_FILE
from pre_commit.repository import all_hooks, install_hook_envs
from pre_commit.store import Store

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _chdir(path: Path) -> Generator[None]:
    """Temporarily change working directory, restoring on exit."""
    saved = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(saved)


@dataclass(frozen=True)
class HookPassed:
    """Hook ran clean — exit 0, no file modifications."""


@dataclass(frozen=True)
class HookAutoApplied:
    """Hook modified the file; changes kept on disk. Re-run verified satisfaction."""

    exit_code: int
    output: bytes
    rerun_exit_code: int


@dataclass(frozen=True)
class HookWouldEdit:
    """Report-only hook that would modify the file (changes reverted)."""

    exit_code: int
    output: bytes


@dataclass(frozen=True)
class HookFailedNotApplied:
    """Report-only hook that exited non-zero without modifying the file."""

    exit_code: int
    output: bytes


HookOutcome = HookPassed | HookAutoApplied | HookWouldEdit | HookFailedNotApplied


@dataclass
class RunResult:
    hooks: dict[str, HookOutcome] = field(default_factory=dict)
    report_only_diff: list[str] = field(default_factory=list)

    @property
    def auto_applied(self) -> dict[str, HookAutoApplied]:
        return {k: h for k, h in self.hooks.items() if isinstance(h, HookAutoApplied)}

    @property
    def would_edit(self) -> dict[str, HookWouldEdit]:
        return {k: h for k, h in self.hooks.items() if isinstance(h, HookWouldEdit)}

    @property
    def failed_not_applied(self) -> dict[str, HookFailedNotApplied]:
        return {k: h for k, h in self.hooks.items() if isinstance(h, HookFailedNotApplied)}

    @property
    def has_issues(self) -> bool:
        """True when any hook has something to report (auto-applied, would-edit, or failed)."""
        return bool(self.auto_applied or self.would_edit or self.failed_not_applied)


def _run_hooks(
    file_path: Path, project_dir: Path, auto_apply_hooks: Iterable[str] = ()
) -> tuple[dict[str, HookOutcome], list[str]]:
    """Run all applicable pre-commit hooks on a single file.

    Two-phase execution:
    1. Auto-apply hooks run first (in original order), changes kept on disk.
    2. Report-only hooks run second (in original order) on the auto-applied
       result. Their cumulative diff is captured, then changes are reverted.

    Returns (hook_results, report_only_diff_lines).
    """
    auto_apply = set(auto_apply_hooks)
    config_path = project_dir / CONFIG_FILE

    store = Store()
    config = load_config(str(config_path))
    hooks = [h for h in all_hooks(config, store) if not h.stages or "pre-commit" in h.stages]
    if not hooks:
        return [], []

    install_hook_envs(hooks, store)

    rel_path = str(file_path.relative_to(project_dir))
    classifier = Classifier([rel_path])

    # Partition hooks into auto-apply and report-only, preserving relative order.
    auto_hooks = []
    report_hooks = []
    for hook in hooks:
        filenames = tuple(classifier.filenames_for_hook(hook))
        if not filenames and not hook.always_run:
            continue
        if hook.id in auto_apply:
            auto_hooks.append((hook, filenames))
        else:
            report_hooks.append((hook, filenames))

    def run_hook(hook, filenames) -> tuple[int, bytes]:
        """Run a single pre-commit hook, returning (exit_code, output)."""
        language = languages[hook.language]
        with language.in_env(hook.prefix, hook.language_version):
            return language.run_hook(
                hook.prefix,
                hook.entry,
                hook.args,
                filenames if hook.pass_filenames else (),
                is_local=hook.src == "local",
                require_serial=hook.require_serial,
                color=False,
            )

    def classify_report_only(retcode: int, out: bytes, *, modified: bool) -> HookOutcome:
        if modified:
            return HookWouldEdit(exit_code=retcode, output=out)
        if retcode == 0:
            return HookPassed()
        return HookFailedNotApplied(exit_code=retcode, output=out)

    results: dict[str, HookOutcome] = {}

    # Phase 1: auto-apply hooks — keep their changes, re-run to verify satisfaction.
    for hook, filenames in auto_hooks:
        content_before = file_path.read_bytes()
        retcode, out = run_hook(hook, filenames)
        current = file_path.read_bytes()
        modified = current != content_before

        assert hook.id not in results, f"Duplicate hook ID: {hook.id}"
        if modified:
            rerun_retcode, _ = run_hook(hook, filenames)
            rerun_content = file_path.read_bytes()
            if rerun_content != current:
                file_path.write_bytes(current)
            results[hook.id] = HookAutoApplied(exit_code=retcode, output=out, rerun_exit_code=rerun_retcode)
        else:
            results[hook.id] = classify_report_only(retcode, out, modified=False)

    # Phase 2: report-only hooks — capture diff, then revert.
    baseline = file_path.read_bytes()
    for hook, filenames in report_hooks:
        assert hook.id not in results, f"Duplicate hook ID: {hook.id}"
        content_before = file_path.read_bytes()
        retcode, out = run_hook(hook, filenames)
        current = file_path.read_bytes()
        results[hook.id] = classify_report_only(retcode, out, modified=current != content_before)

    # Compute diff of what report-only hooks would change, then revert.
    after_all = file_path.read_bytes()
    diff_lines: list[str] = []
    if after_all != baseline:
        # Skip diff for binary files (non-UTF-8).
        try:
            baseline_text = baseline.decode()
            after_text = after_all.decode()
            diff_lines = list(
                difflib.unified_diff(baseline_text.splitlines(keepends=True), after_text.splitlines(keepends=True))
            )
        except UnicodeDecodeError:
            pass
        file_path.write_bytes(baseline)

    return results, diff_lines


def run_on_file(file_path: Path, project_dir: Path, auto_apply_hooks: Iterable[str] = ()) -> RunResult:
    """Run pre-commit hooks on a single file using the Python API.

    Auto-apply hooks keep their modifications on disk. All other hooks'
    modifications are reverted. On crash, restores original content.
    """
    original_content = file_path.read_bytes()

    # pre-commit's internals assume cwd is the project root:
    # Classifier uses relative paths and hooks inherit process cwd
    # via subprocess.Popen (no cwd= parameter).
    try:
        with _chdir(project_dir):
            hook_results, diff_lines = _run_hooks(file_path, project_dir, auto_apply_hooks)
    except Exception:
        file_path.write_bytes(original_content)
        raise

    return RunResult(hooks=hook_results, report_only_diff=diff_lines)

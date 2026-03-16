"""Interface library for running pre-commit hooks programmatically.

Wraps pre-commit's Python internals to provide structured per-hook results
without subprocess calls or stdout parsing. All pre-commit imports are
confined to this module.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

from pre_commit.all_languages import languages
from pre_commit.clientlib import load_config
from pre_commit.commands.run import Classifier
from pre_commit.constants import CONFIG_FILE
from pre_commit.repository import all_hooks, install_hook_envs
from pre_commit.store import Store

from util.fs import restore_file

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 500


@contextlib.contextmanager
def _chdir(path: Path) -> Generator[None]:
    """Temporarily change working directory, restoring on exit."""
    saved = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(saved)


@dataclass
class HookResult:
    hook_id: str
    hook_name: str
    passed: bool
    output: str
    files_modified: bool


@dataclass
class RunResult:
    hooks: list[HookResult] = field(default_factory=list)
    modified_content: bytes = b""
    original_content: bytes = b""

    @property
    def failed_hooks(self) -> list[HookResult]:
        return [h for h in self.hooks if not h.passed]

    @property
    def all_passed(self) -> bool:
        return all(h.passed for h in self.hooks)


def _run_hooks(file_path: Path, project_dir: Path) -> list[HookResult]:
    """Run all applicable pre-commit hooks on a single file.

    Caller must ensure cwd is project_dir.
    """
    config_path = project_dir / CONFIG_FILE
    original_content = file_path.read_bytes()

    store = Store()
    config = load_config(str(config_path))
    hooks = [h for h in all_hooks(config, store) if not h.stages or "pre-commit" in h.stages]
    if not hooks:
        return []

    install_hook_envs(hooks, store)

    rel_path = str(file_path.relative_to(project_dir))
    classifier = Classifier([rel_path])
    results: list[HookResult] = []

    for hook in hooks:
        filenames = tuple(classifier.filenames_for_hook(hook))
        if not filenames and not hook.always_run:
            continue

        language = languages[hook.language]
        with language.in_env(hook.prefix, hook.language_version):
            retcode, out = language.run_hook(
                hook.prefix,
                hook.entry,
                hook.args,
                filenames if hook.pass_filenames else (),
                is_local=hook.src == "local",
                require_serial=hook.require_serial,
                color=False,
            )

        modified = file_path.read_bytes() != original_content
        results.append(
            HookResult(
                hook_id=hook.id,
                hook_name=hook.name,
                passed=retcode == 0 and not modified,
                output=out.decode(errors="replace").strip()[:_MAX_OUTPUT_CHARS],
                files_modified=modified,
            )
        )

    return results


def run_on_file(file_path: Path, project_dir: Path) -> RunResult:
    """Run pre-commit hooks on a single file using the Python API.

    Always restores the original file content after running.
    """
    original_content = file_path.read_bytes()

    # pre-commit's internals assume cwd is the project root:
    # Classifier uses relative paths and hooks inherit process cwd
    # via subprocess.Popen (no cwd= parameter).
    with _chdir(project_dir), restore_file(file_path):
        hook_results = _run_hooks(file_path, project_dir)
        modified_content = file_path.read_bytes()

    return RunResult(hooks=hook_results, modified_content=modified_content, original_content=original_content)

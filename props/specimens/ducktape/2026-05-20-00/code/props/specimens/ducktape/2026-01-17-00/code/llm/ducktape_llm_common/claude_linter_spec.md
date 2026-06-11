# Claude Linter Hooks Specification

This document outlines the understanding of the requirements, constraints, and implementation details for the `claude-linter` tool, specifically its integration with Claude Code Hooks.

## 1. User Requirements for `claude-linter`

The primary goal of `claude-linter` is to provide real-time linting feedback and automatic fixes during Claude Code's file manipulation operations (`Write`, `Edit`, `MultiEdit`).

- **Pre-Write Hook**
  - **Purpose:** To act as a gatekeeper, preventing a write operation if critical, non-autofixable violations are detected in the proposed content.
  - **Behavior:**
    - Should analyze the _proposed content_ of the file _before_ it is written.
    - Should _only_ report violations that _cannot_ be automatically fixed by the linter.
    - If non-autofixable violations are found, it **MUST** signal Claude Code to `block` the write operation, providing a clear `reason` detailing the violations.
    - If no non-autofixable violations are found, it **MUST** signal Claude Code to `approve` the write operation.
  - **Target Tools:** Primarily `Write`. (Future consideration for non-blocking warnings on `Edit`/`MultiEdit`.)

- **Post-Write Hook**
  - **Purpose:** To apply automatic fixes to the file _after_ it has been written and to inform Claude Code if any changes were made.
  - **Behavior:**
    - Should analyze the _actual content_ of the file _after_ it has been written to disk.
    - Should apply all configured automatic fixes (autofixable violations).
    - If auto-fixes were applied and the file content changed, it **MUST** signal Claude Code to `block` the operation (as a feedback mechanism), providing a `reason` that simply states "FYI: Auto-fixes were applied to your code, you will need to re-Read before further edits." (No diffs are required in the `reason`).
    - If no auto-fixes were applied (or no changes were made to the file), it **MUST** signal Claude Code to `approve` the operation.
  - **Target Tools:** `Write`, `Edit`, `MultiEdit`.

## 2. Claude Code Hooks API Constraints and Requirements

The `claude-linter` tool must strictly adhere to the Claude Code Hooks API for communication:

- **JSON Communication:** All input from Claude Code to the hook, and all output from the hook to Claude Code, **MUST** be valid JSON objects transmitted via `stdin` and `stdout` respectively.
- **Exit Code 0:** The `claude-linter` process **MUST ALWAYS** exit with a status code of `0`. Any non-zero exit code will be interpreted by Claude Code as an internal error of the hook, not a controlled decision.
- **Hook Output API (`decision`, `reason`, `continue`):**
  - **`decision` (string, required):**
    - `"approve"`: Indicates that the hook allows the operation to proceed (or that no further action is needed).
    - `"block"`: Indicates that the hook wants to prevent the operation (for `PreToolUse`) or provide feedback/re-prompt the model (for `PostToolUse`).
  - **`reason` (string, optional):** A human-readable message explaining the `decision`. This message is displayed to the user and/or used to re-prompt the model.
  - **`continue` (boolean, required):** **MUST ALWAYS BE `true`**. This signals to Claude Code that the model should continue its execution flow after processing the hook's response.

## 3. Complications and Constraints around `pre-commit`

`pre-commit` is chosen as the underlying linting engine due to its extensive collection of hooks and robust configuration system. However, its primary design as a Git hook introduces several complexities for our use case:

- **Git Dependency:** `pre-commit` is fundamentally designed to operate within a Git repository. Many of its internal APIs and CLI commands implicitly assume the presence of a `.git` directory and staged/unstaged files.
- **Programmatic vs. CLI Execution:**
  - **CLI (`subprocess.run`):** Shelling out to `pre-commit` CLI (`pre-commit run --files ...`) is simpler to implement initially but introduces challenges:
    - It returns its own exit codes (non-zero for failures), which must be carefully wrapped to ensure our `claude-linter` always exits `0`.
    - It still performs Git checks, which can fail if the `cwd` is not a Git repo.
    - It's less granular and harder to control specific behaviors (e.g., only running non-fixing hooks).
  - **Programmatic (Internal APIs):** Directly calling `pre-commit`'s Python APIs (`load_config`, `all_hooks`, `languages[hook.language].run_hook`) offers fine-grained control and avoids CLI exit code issues. However, it requires:
    - **Targeted Git Mocking:** Extensive mocking of `pre_commit.git` functions to prevent `pre-commit` from attempting Git operations on the user's working directory when not in a Git repo.
    - **Remote Repository Management:** `pre-commit`'s handling of remote hook repositories (e.g., `https://github.com/psf/black`) involves `git clone`/`fetch`/`checkout`. This _must_ be managed by our tool, ideally in a central cache, using `subprocess.check_call(['git', ...])`. This is the _only_ acceptable use of `subprocess.check_call` for Git operations.

- **Local vs. Remote Hooks:**
  - **`repo: local` hooks:** These are scripts defined directly within the `.pre-commit-config.yaml` or referenced locally. They are easier to run programmatically without Git dependencies.
  - **Remote hooks:** These require `pre-commit` to clone and manage a separate Git repository for the hook's source code. This is where the Git dependency becomes unavoidable for `pre-commit`'s internal logic.

- **Configuration Resolution:** `pre-commit` typically searches for `.pre-commit-config.yaml` by traversing up the directory tree. Our implementation needs to replicate this behavior when in a Git repository. When not in a Git repository, it should use the configuration provided to `claude-linter`.

## 4. Behavior Matrix (Block/Approve, Autofix/No-Autofix, Stage/Tool)

The `claude-linter` will operate in two primary modes based on the environment:

### Mode 1: Running in a Git Repository

- **Detection:** `pre_commit.git.is_in_git_repo(cwd)` returns `True` AND a `.pre-commit-config.yaml` is found by traversing up from `cwd`.
- **Behavior:** `claude-linter` will leverage the existing `pre-commit` setup and configuration within that repository.
- **Git Mocking:** No Git mocking is performed on the user's working directory.

### Mode 2: Running Outside a Git Repository (or no config found)

- **Detection:** `pre_commit.git.is_in_git_repo(cwd)` returns `False` OR no `.pre-commit-config.yaml` is found.
- **Behavior:** `claude-linter` will use the configuration provided to it (likely from a user's global `~/.claude.json` or `~/.claude/settings.json`). It will set up a hermetic environment for `pre-commit`'s internal operations.
- **Git Mocking:** Extensive mocking of `pre_commit.git` functions will occur to prevent any Git operations on the user's working directory.

### Hook-Specific Behaviors (Applies to both modes, driven by `PreCommitRunner`)

| Hook Type | Target Tools (`tool_name` from Claude) | Linter Type | `pre-commit` Hook `stages` | `PreCommitRunner` Action               | Claude Code Output (`decision`, `reason`)                           |
| :-------- | :------------------------------------- | :---------- | :------------------------- | :------------------------------------- | :------------------------------------------------------------------ |
| `pre`     | `Write`                                | Non-Fixing  | `commit`, `push`, etc.     | Run hook on proposed content.          | `block` if violations found (with `reason`), `approve` otherwise.   |
| `pre`     | `Edit`, `MultiEdit`                    | Non-Fixing  | `commit`, `push`, etc.     | (Future: Non-blocking warning)         | `approve` (for now, no blocking)                                    |
| `post`    | `Write`, `Edit`, `MultiEdit`           | Fixing      | `manual`                   | Run hook on written file. Apply fixes. | `block` if file changed (with "FYI" `reason`), `approve` otherwise. |

**Important Notes:**

- **`pre-commit` Hook `stages`:** The `stages` key in `.pre-commit-config.yaml` is crucial for distinguishing between fixing and non-fixing hooks. Hooks with `stages: [manual]` are typically fixers, while others (e.g., `commit`, `push`) are usually non-fixing. Our `get_merged_config` function will use this distinction.
- **`PreCommitRunner` Return Value:** The `PreCommitRunner.run` method will always return a tuple `(total_retcode, stdout_output, stderr_output)`. `total_retcode` will be `0` if all relevant hooks passed/fixed, and `1` if any non-fixable issues were found or if a fixing hook failed to apply its fix. The `cli.py` will then interpret this to generate the correct JSON output for Claude Code.

This detailed specification should serve as a clear guide for the implementation and testing of the `claude-linter` hooks.

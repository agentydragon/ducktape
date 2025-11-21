local I = import '../../specimens/lib.libsonnet';

// iss-011: Duplicated PID file reading logic in wt client

I.issueOneOccurrence(
  rationale=|||
    The logic for reading and parsing the daemon PID file is duplicated in two places in the
    wt (worktree) client code. This should be extracted into a shared helper function.

    **Duplication 1: wt_client.py lines 108-113 (is_daemon_running method):**
    ```python
    pid_str = await asyncio.to_thread(self.config.daemon_pid_path.read_text)
    pid_str = pid_str.strip()
    if not pid_str:
        return False

    pid = int(pid_str)
    ```

    **Duplication 2: handlers.py lines 250-258 (kill_daemon handler):**
    ```python
    pid_str = await asyncio.to_thread(pid_file.read_text)
    pid_str = pid_str.strip()

    if not pid_str:
        click.echo("Empty PID file - cleaning up stale files")
        _cleanup_daemon_files(pid_file, socket_file)
        return

    pid = int(pid_str)
    ```

    **Why this is problematic:**
    - Same pattern repeated: async read, strip, check if empty, parse int
    - If PID file format or error handling needs to change, must update multiple places
    - Risk of inconsistent behavior if one location is updated but not the other
    - Violates DRY (Don't Repeat Yourself) principle

    **Recommended fix:**

    Create a shared async helper function in wt_client.py or a shared module:

    ```python
    async def read_daemon_pid(pid_path: Path) -> int | None:
        """Read daemon PID from file.

        Returns:
            PID as int if file exists and contains valid PID, None otherwise.
        """
        if not pid_path.exists():
            return None

        try:
            pid_str = await asyncio.to_thread(pid_path.read_text)
            pid_str = pid_str.strip()

            if not pid_str:
                return None

            return int(pid_str)
        except (OSError, ValueError):
            return None
    ```

    Then both call sites become:

    **wt_client.py (is_daemon_running):**
    ```python
    pid = await read_daemon_pid(self.config.daemon_pid_path)
    if pid is None:
        return False
    return bool(psutil.pid_exists(pid) and self.config.daemon_socket_path.exists())
    ```

    **handlers.py (kill_daemon):**
    ```python
    pid = await read_daemon_pid(pid_file)
    if pid is None:
        click.echo("Empty or invalid PID file - cleaning up stale files")
        _cleanup_daemon_files(pid_file, socket_file)
        return
    # Continue with kill logic...
    ```

    **Benefits:**
    - Single source of truth for PID file reading logic
    - Easier to maintain and modify
    - Consistent error handling
    - More testable (can unit test the helper independently)
    - Clearer intent at call sites

    **Note:**
    Line 536 in wt_client.py also reads the PID file but synchronously for debug output.
    This is fine as-is since it's just displaying raw contents and has different requirements
    (sync vs async, no parsing needed).
  |||,
  properties=['dry-principle', 'duplication', 'maintainability'],
  filesToRanges={
    'wt/src/wt/client/wt_client.py': [
      [108, 113],  // Duplicated PID reading in is_daemon_running
    ],
    'wt/src/wt/client/handlers.py': [
      [250, 258],  // Duplicated PID reading in kill_daemon
    ],
  },
)

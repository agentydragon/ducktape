# CI `bazel-test` Failure Diagnosis

Analyzed 5 most recent failed runs on `devel` (2026-01-31). The most recent run
has 18 real test failures plus ~46 tests that never executed (RBE infra flakiness).

## Fix 1 (HIGH): Missing `pre-commit` on RBE — 7 targets

**Targets:**

- `//claude/claude_hooks:test_autofixer`
- `//claude/claude_hooks:test_integration`
- `//claude/claude_hooks:test_precommit_integration`
- `//llm/claude_linter:test_claude_post_hook`
- `//llm/claude_linter:test_hooks`
- `//llm/claude_linter_v2:test_integration`
- `//llm/claude_linter_v2:test_stop_hook_gitignore`

**Error:** `FileNotFoundError: [Errno 2] No such file or directory: 'pre-commit'`

**Root cause:** Tests spawn `pre-commit` as a subprocess. The RBE worker image
doesn't have it.

**Fix:** Tag these tests `no-remote` to force local execution.

## Fix 2 (HIGH): Missing `python` binary in mcp_starter — 1 target (5 cases)

**Target:** `//mcp_starter:test_server`

**Error:** `FileNotFoundError: [Errno 2] No such file or directory: 'python'`

**Root cause:** `mcp_starter/conftest.py:15` hardcodes `command="python"` but
the RBE image only has `python3`.

**Fix:** Use `sys.executable` instead of `"python"`.

## Fix 3 (HIGH): Missing `anyio` in subprocess on RBE — 2 targets

**Targets:**

- `//agent_server/mcp:test_notifications_envelope`
- `//mcp_infra/compositor:test_admin`

**Error:** `ModuleNotFoundError: No module named 'anyio'` in subprocess,
causing `McpError: Connection closed`.

**Root cause:** `mcp_infra/testing/stdio_notifier.py` imports `anyio`. The
BUILD deps are correct, but when the `stdio_notifier` `py_binary` is launched
as a subprocess, its runfiles may not ship correctly to RBE.

**Fix:** Investigate runfiles propagation for `py_binary` on RBE. May need
`data` dep on tests or `exec_properties` adjustments.

## Fix 4 (MEDIUM): pytest-asyncio fixture incompatibility — 4 targets (15 cases)

**Targets:**

- `//homeassistant/iaqi/custom_components/indoor_aqi:test_config_flow`
- `//homeassistant/iaqi/custom_components/indoor_aqi:test_init`
- `//homeassistant/iaqi/custom_components/indoor_aqi:test_sensor`
- `//homeassistant/iaqi/custom_components/indoor_aqi:test_sensor_simple`

**Error:** `'test_import_flow' requested an async fixture 'hass', with no
plugin or hook that supports it`

**Root cause:** The `hass` fixture from `pytest_homeassistant_custom_component`
is async. The conftest.py has no `pytest_configure` hook to set
`asyncio_mode = "auto"`, unlike other packages in this repo.

**Fix:** Add `pytest_configure` hook to set asyncio auto mode in
`homeassistant/iaqi/custom_components/indoor_aqi/conftest.py`.

## Fix 5 (MEDIUM): Ruff not available on RBE — 1 target (2 cases)

**Target:** `//llm/claude_linter_v2:test_cl2_check`

**Error:** `assert 'x=1\ny=2\n' == 'x = 1\ny = 2\n'` — ruff auto-fix didn't
run.

**Fix:** Add `ruff` as a `data` dep on the test target, or tag `no-remote`.

## Fix 6 (LOW): Test assertion mismatch — 1 target

**Target:** `//llm/claude_linter_v2:test_stop_hook_fresh_scan`

**Error:** Test expects `"Do not use bare \`except\`"`in reason but gets`"Use of hasattr"` instead.

**Root cause:** The test file has both `except:` and `hasattr()` violations.
The detector returns the `hasattr` violation first.

**Fix:** Fix detector ordering or relax the assertion to accept either
violation.

## Fix 7 (LOW): Jinja2 template syntax — 1 target

**Target:** `//llm/html/llm_html:test_html_rendering`

**Error:** `expected token 'end of statement block', got '='`

**Fix:** Escape `{{ ... }}` Jinja2-like syntax in markdown content.

## Fix 8 (LOW): Missing `dbus-daemon` — 1 target

**Target:** `//experimental/dbus_fast_example:test_example`

**Fix:** Tag `no-remote`.

## Infrastructure: Firecracker VM crashes — ~46 tests per run

**Error:** `Unavailable: failed to sync workspace: Firecracker VM crashed`

BuildBuddy infrastructure issue. Not actionable from the repo side.

## Summary

| #   | Fix                                                     | Targets fixed | Effort   |
| --- | ------------------------------------------------------- | ------------- | -------- |
| 1   | Tag 7 pre-commit tests `no-remote`                      | 7             | Trivial  |
| 2   | `"python"` -> `sys.executable` in mcp_starter           | 1 (5 cases)   | One-line |
| 3   | Investigate anyio subprocess runfiles on RBE            | 2             | Medium   |
| 4   | Add `pytest_configure` asyncio auto-mode to HA conftest | 4 (15 cases)  | 3-line   |
| 5   | Add ruff data dep or tag `no-remote`                    | 1 (2 cases)   | Small    |
| 6   | Fix assertion in `test_stop_hook_fresh_scan`            | 1             | Small    |
| 7   | Fix Jinja2 template escaping                            | 1             | Small    |
| 8   | Tag dbus test `no-remote`                               | 1             | Trivial  |

Fixes 1-4 resolve 13 of 18 failing targets with minimal effort.

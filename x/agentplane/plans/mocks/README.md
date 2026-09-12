# Agentplane UI mocks

Static HTML mocks (no framework, no build step — open directly in a browser) captured during
design discussion. Not wired to real state; each mock's own footer names the plan doc it was made
for.

- **`app_shell.html`** — the session-first sidebar shell: shipped, referenced from
  [`../task_dag.md`](../task_dag.md)'s UI-shell track and
  [`../session_first_navigation.md`](../session_first_navigation.md).
- **`nav_overflow.html`** — an earlier proposal (fold the top nav's last three buttons behind a
  "More" menu) for the now-deleted `mobile_density.md`. Superseded by `app_shell.html`'s sidebar,
  which removes the top nav row entirely rather than trimming it — kept for the drilled-in-header
  menu pattern (Actions/Connections/MCP servers/Notifications reachable from a sandbox or session
  page's own header) that `app_shell.html` doesn't cover.
- **`composer_bar.html`** — an earlier proposal for consolidating session/harness status, the raw
  frames switch, and shut-down-harness into the composer bar. Its core content (status dot, model
  picker, kebab menu, interrupt) already shipped in the transcript-restyle PR; kept for the
  status-dot state-machine table (`status`/`harness` → one of four dots), which is more detailed
  than what actually shipped.

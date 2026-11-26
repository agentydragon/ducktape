# Agentplane UI mocks

Static HTML mocks (no framework, no build step — open directly in a browser) captured during
design discussion. Not wired to real state; each mock's own footer names the plan doc it was made
for. Design tokens and boilerplate shared by two or more mocks live in `shared.css`, linked from
each file's `<head>`; each mock's own `<style>` keeps only what's unique to it, or a single
differing property (e.g. `.device { width: ...; }`) where a shared rule's shape matches but its
size doesn't.

- **`app_shell.html`** — the session-first sidebar shell: shipped, referenced from
  [`../task_dag.md`](../task_dag.md)'s UI-shell track and
  [`../session_first_navigation.md`](../session_first_navigation.md).
- **`composer_bar.html`** — an earlier proposal for consolidating session/harness status, the raw
  frames switch, and shut-down-harness into the composer bar. Its core content (status dot, model
  picker, kebab menu, interrupt) already shipped in the transcript-restyle PR; kept for the
  status-dot state-machine table (`status`/`harness` → one of four dots), which is more detailed
  than what actually shipped.
- **`new_thread_landing.html`** — the unscoped/pre-scoped "New thread" composer
  (`UISHELL_NEWTHREAD_LANDING`/`UISHELL_NEWTHREAD_SANDBOX` in [`../task_dag.md`](../task_dag.md)) as
  one page with three states (picking a target, provisioning, bound to the live thread) instead of a
  separate wizard — the composer and its position never change across states.

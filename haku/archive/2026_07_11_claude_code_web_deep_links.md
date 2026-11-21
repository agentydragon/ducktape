# Claude Code web deep links as a second coding-delegation path

**Built and shipped.** The `<code-session>` widget lives in haku-state (`ui/frontend/src/affordances.tsx`),
and the operational "when to use this" guidance is haku-state's `memory/delegation.md`. Kept because two
things below are durable and easy to re-derive wrongly: the `claude.ai/code` query parameters (and the
branch one that does not exist), and why a link from inside the haku-ui iframe has to go through the
`openLink` bridge rather than a plain `target="_blank"`.

The ask (operator, 2026-07-11) was a second way to delegate coding tasks. A plain claude.ai conversation
has no git remote access, so any code change still has to come back through a human; a Claude Code web
session pre-wired to `ducktape` gets **push access to the repo directly**.

## The mechanism (confirmed real, not speculative)

`code.claude.com/docs/en/web-quickstart.md` → "Pre-fill sessions" documents URL query
parameters on `https://claude.ai/code` that open a **new** session pre-loaded with a
prompt and an environment:

- `prompt` (alias `q`) — the input box text, URL-encoded. An earlier reading of the docs gave a
  ~5,000 char cap; the current page states none. For a prompt too long to embed, use `prompt_url`
  instead: a URL the page fetches the prompt text from (must allow cross-origin requests; ignored
  when `prompt` is also set).
- `repositories` (alias `repo`) — comma-separated `owner/repo` GitHub slugs. This is
  GitHub-specific: it only reaches repos the Claude Code web GitHub integration can
  see. **`ducktape` (`agentydragon/ducktape`) qualifies; `haku-state` does not** — it's
  self-hosted on Forgejo (`git.allegedly.works`), not GitHub, so this delegation path
  is `ducktape`-only. (A `haku-state` coding task still has to go through Haku's own
  Claude Code web home, which clones it directly — see `haku/runtime/claude_web_env/`.)
- `environment` — **name or ID** of a saved cloud environment (confirmed in the docs
  verbatim: "Name or ID of the environment to preselect") — no opaque-ID lookup
  needed, the environment's display name works directly. The operator runs several
  environments and picks per task, so there is no default to hardcode.

Example: `https://claude.ai/code?prompt=Fix%20the%20login%20bug&repositories=agentydragon/ducktape&environment=<name>`.

**Known gap: no branch parameter.** Whoever opens the link still picks the branch by
hand in the Claude Code web UI after it loads. If a task needs a specific base branch,
say so in the prompt text itself rather than relying on the link to pre-select it.

**Pre-fill is not auto-submit.** The docs' own wording is "prefill the prompt... in the
input box" — clicking the link lands the operator on `claude.ai/code` with the prompt
sitting in the input box, unsent. He still reads it and presses Enter (or edits/cancels)
before anything actually runs. This is a real, structural review step — different in
kind from `<tool-call>`'s executed-on-approval model, where clicking through in
haku-console is the last human touch before execution. Don't describe `<code-session>`
as fully zero-touch the way an executed tool call is; "nothing to approve" is true only
about the link itself, not about whether the resulting session runs unreviewed.

## Policy: no approval gate, but a real gate DOES exist — go through it, don't route around it

A plain markdown link does nothing in haku-ui: haku-ui runs inside haku-console's **sandboxed
cross-origin iframe** (`sandbox="allow-scripts allow-same-origin allow-forms"`, deliberately
**no `allow-popups`**), so a native `<a target="_blank">` click is silently blocked by the
browser. The console's actual mechanism is the **`openLink` bridge**: the iframe
`postMessage`s `{type: "openLink", url}` to the trusted shell, which vets the URL
(`vetOpenLink` in `haku/console/frontend/bridge.ts`) — HTTPS/mailto only as a hard scheme
gate, then a host **whitelist** (`OPEN_LINK_WHITELIST`, PR-gated, shell-owned) decides
open-directly vs. show-a-confirm-dialog. `claude.ai` is already on that whitelist (used by
the `<handoff>` widget for `claude.ai/new`), so a `claude.ai/code` link matches the same
host and opens directly — no console/ducktape change, no new approval plumbing.

The lesson: "gated" here means "goes through the console's one real link-opening
mechanism," not "needs a new approval step". Route a new link-opening widget through the
existing gate; don't add or remove one.

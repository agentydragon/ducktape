# Plan: Claude Code web deep links as a second coding-delegation path

Status: design landed, one operator confirmation outstanding (see _Open question_).

## Why

Operator ask (2026-07-11): a second way to delegate coding tasks, alongside describing
a task directly to Claude.ai chat. The gap that motivates it: a plain claude.ai
conversation has no git remote access, so any actual code change still has to come
back through a human. A Claude Code web session pre-wired to `ducktape` gets **push
access to the repo directly** — closing that gap for anything that's really a coding
task rather than a research/writing one.

## The mechanism (confirmed real, not speculative)

`code.claude.com/docs/en/web-quickstart.md` → "Pre-fill sessions" documents URL query
parameters on `https://claude.ai/code` that open a **new** session pre-loaded with a
prompt and an environment:

- `prompt` (alias `q`) — the input box text, URL-encoded, ~5,000 char cap. Use
  `prompt_url` instead (a URL Claude fetches for the prompt text) if a task
  description needs to run longer.
- `repositories` (alias `repo`) — comma-separated `owner/repo` GitHub slugs. This is
  GitHub-specific: it only reaches repos the Claude Code web GitHub integration can
  see. **`ducktape` (`agentydragon/ducktape`) qualifies; `haku-state` does not** — it's
  self-hosted on Forgejo (`git.allegedly.works`), not GitHub, so this delegation path
  is `ducktape`-only. (A `haku-state` coding task still has to go through Haku's own
  Claude Code web home, which clones it directly — see `haku/runtime/claude_web_env/`.)
- `environment` — name/ID of a saved cloud environment (network policy, setup script,
  env vars). This is the parameter that actually grants the git remote access —
  picking the wrong one either fails outright or hands the session credentials scoped
  for a different purpose.

Example: `https://claude.ai/code?prompt=Fix%20the%20login%20bug&repositories=agentydragon/ducktape&environment=<name>`.

**Known gap: no branch parameter.** Whoever opens the link still picks the branch by
hand in the Claude Code web UI after it loads. If a task needs a specific base branch,
say so in the prompt text itself rather than relying on the link to pre-select it.

## Open question: which `environment` to target

Do **not** reuse the dedicated "Haku" web-home environment
(`haku/runtime/claude_web_env/README.md`) for this — it's provisioned with Haku's own
`SOPS_AGE_KEY` and a domain allowlist scoped to Haku's operational needs (cluster API,
Gmail, `*.allegedly.works`), not a general-purpose coding sandbox. Reusing it for
arbitrary delegated coding tasks would hand those sessions Haku's own secrets for no
reason.

This plan needs the operator to confirm the exact name/ID of the environment he wants
these links to target (a general `ducktape` dev environment, separate from Haku's).
Once confirmed, record it in haku-state's `memory/delegation.md` so every future link
uses the same value instead of guessing.

## Policy: no approval gate

These links are **inert until a human opens them in a browser** — clicking one just
navigates to `claude.ai/code`, which runs Anthropic's own session-creation flow
(its own confirmation UI) before anything happens. No credential is exposed, no
mutation occurs, and no ducktape/haku-console code executes as a side effect of
generating or clicking the link. Per the operator's own framing ("I'll want to
autoapprove those links") and the existing `<handoff>`-vs-`<tool-call>` calibration
rule (`procedures/tool_calls.md` in haku-state): render these as a **plain link**, not
a `<tool-call>` — there is nothing to approve. Confirmed in haku-state's renderer
(`ui/frontend/src/mdx.tsx`): an external markdown link already renders as a clickable
`<Anchor target="_blank">` with zero extra frontend work needed.

## What this does NOT need

- No new haku-state UI widget — a plain `[Open in Claude Code web](https://claude.ai/code?...)`
  markdown link in an item body already renders correctly (verified against the
  existing `mdx.tsx` link-rendering path).
- No ducktape-side script/service to mint these links — the URL is simple enough to
  construct inline (base URL + `urllib.parse.urlencode`-style encoding of `prompt`)
  each time one is authored; a dedicated helper would be premature abstraction for a
  three-field query string.
- No new approval/auth plumbing — see _Policy_ above.

## Where the authoring guidance lives

haku-state's `memory/delegation.md` carries the operational "when to use this, how to
build the URL" guidance for Haku itself; this file is the durable design record for
why the mechanism works the way it does and what it deliberately doesn't build.

# Claude Code web deep links as a second coding-delegation path

**Built and shipped; moved out of `plans/` on 2026-08-16, unchanged.** The `<code-session>` widget
lives in haku-state (`ui/frontend/src/affordances.tsx`), and the operational "when to use this"
guidance is haku-state's `memory/delegation.md`. Kept because two things below are durable and
easy to re-derive wrongly: the `claude.ai/code` query parameters (and the branch one that does not
exist), and why a link from inside the haku-ui iframe has to go through the `openLink` bridge
rather than a plain `target="_blank"`.

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
- `environment` — **name or ID** of a saved cloud environment (confirmed in the docs
  verbatim: "Name or ID of the environment to preselect") — no opaque-ID lookup
  needed, the environment's display name works directly.

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

## Which `environment` to target

The operator runs **multiple environments for different cases** and picks per task
which one a link should target — there's no single default to hardcode. (One data
point: this very session's own environment turned out to already be "Haku" —
`DUCKTAPE_CLAUDE_HOOKS_PROFILE` points at Haku's profile, `K8S_NAMESPACE=haku-sandbox`
— confirming an environment name alone is enough to identify it, with no separate ID
lookup required. Whether "Haku" itself is ever the right target for one of these links
is the operator's call each time, not something to assume.)

## Policy: no approval gate, but a real gate DOES exist — go through it, don't route around it

**Correction (operator, 2026-07-11):** the first version of this plan assumed a plain
markdown link would just work in haku-ui with zero engineering. Wrong — the operator
caught it: haku-ui runs inside haku-console's **sandboxed cross-origin iframe**
(`sandbox="allow-scripts allow-same-origin allow-forms"`, deliberately **no
`allow-popups`**), so a native `<a target="_blank">` click from inside that iframe is
silently blocked by the browser — it can't open anything. The console's actual
mechanism is the **`openLink` bridge**: the iframe `postMessage`s `{type: "openLink",
url}` to the trusted shell, which vets the URL (`vetOpenLink` in
`haku/console/frontend/bridge.ts`) — HTTPS/mailto only as a hard scheme gate, then a
host **whitelist** (`OPEN_LINK_WHITELIST`, PR-gated, shell-owned) decides open-directly
vs. show-a-confirm-dialog. `claude.ai` is already on that whitelist (used by the
existing `<handoff>` widget for `claude.ai/new`), so a `claude.ai/code` link matches
the same host and opens directly — **no console/ducktape change needed**, no new
approval plumbing, and this genuinely has "nothing to approve" once routed through the
bridge that already exists. The lesson: "gated" here means "goes through the
console's one real link-opening mechanism," not "needs a new approval step" — those
are different things, and the fix was routing through the existing gate correctly, not
adding or removing one.

## What got built

- **`ui/frontend/src/affordances.tsx`** (haku-state): `<CodeSession>` component, a
  direct sibling of the existing `<Handoff>` (which already does this exact pattern for
  `claude.ai/new` — same `openLink` call, same whitelist hit). Builds
  `https://claude.ai/code?prompt=...&repositories=...&environment=...` and calls the
  existing gated `openLink`.
- **`ui/frontend/src/mdx.tsx`**: registers `<code-session prompt="…" repository="…"
environment="…" label="…"></code-session>` as an authorable widget tag (mirroring
  `<handoff>`), plus the two new attributes in DOMPurify's `ADD_ATTR` allowlist.
- **`procedures/garden.md`** (haku-state): documents the new tag alongside `<handoff>`.
- Tests: `affordances.test.tsx` (URL construction, defaults) and
  `mdx_render.test.tsx` (DOMPurify sanitizer regression, matching the existing
  `<handoff>` coverage).
- **No ducktape-side change was needed** — the `openLink` whitelist already covers
  `claude.ai` for any path, so no PR to `haku/console/frontend/bridge.ts` was required.

## Where the authoring guidance lives

haku-state's `memory/delegation.md` carries the operational "when to use this" guidance
for Haku itself; this file is the durable design record for why the mechanism works the
way it does.

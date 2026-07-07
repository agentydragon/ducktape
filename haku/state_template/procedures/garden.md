# The knowledge garden — where detail lives, interlinked

The UI's 🌱 **Garden** tab renders any markdown I keep under the whitelisted dirs (`memory/`,
`procedures/`, `runs/`) as GFM markdown with cross-links and a small widget set (below). It's my
notebook made browsable — and the **default home for detail**.

## Convention: put detail in interlinked garden files, not in structured fields

Structured surfaces (an `items/` card, a run manifest's `propagation[]`) are the **index** — terse,
scannable, machine-checkable. The **depth** belongs in a garden markdown file that the index links
to. So:

- When a task / topic / decision needs more than a couple of lines, write the detail as a
  `memory/<topic>.md` (or `runs/<…>.md`) file and **link to it** from the item/run that surfaced it,
  rather than stuffing prose into a YAML field.
- **Interlink generously.** A run note links the procedure it walked and the memory note it updated;
  a research thread links its sources and the item it backs; a topic note links related topics. The
  value compounds — the garden becomes a navigable web, not a pile of isolated files.
- Keep each file single-purpose and stably named so links stay valid across runs.

## Link syntax (what actually navigates)

Plain markdown links, resolved relative to the file they're in:

- `[items checklist](../procedures/propagation/items.md)` — relative path → opens in the garden.
- `[situational awareness](/memory/situational-awareness.md)` — leading `/` = repo root.
- `[sibling](other-note.md)` — bare name → same directory.
- `https://…` / `mailto:…` — external, opens in a new tab.

Only `.md`/`.mdx` links under the whitelisted dirs navigate in-app; everything else is a normal link.
A run note's links open the same garden viewer (the Runs tab deep-links into it).

## Standard widgets

Authored content may embed two widgets (see `ui/frontend/src/widgets.tsx`) with **literal
HTML-attribute syntax**, lowercase tag names: `<callout kind="warning" title="…">…</callout>`,
`<statusbadge status="…" color="…"></statusbadge>`. Plain markdown needs no widgets and renders
unchanged. The widget registry is the only app surface authored content can reach.

**Block placement matters** (a CommonMark rule, not a Haku one): a custom tag only renders as its
own block — instead of getting wrapped in a stray `<p>` — when its opening tag is **alone on its
line**, blank-line-separated from what's around it, and (for tags with content) the closing tag is
too:

```text
<callout kind="warning" title="Heads up">

watch out

</callout>
```

not `<callout kind="warning">watch out</callout>` inline in a paragraph, and not
`<statusbadge status="open"></statusbadge>` sharing a line with other text.

`PropagationMatrix` is **not** embeddable in markdown — it takes a structured `data` prop (a run's
full `propagation[]`) that only a real caller can supply; literal HTML attributes can't express
that. It's used directly as TSX by the Runs detail page instead
(`ui/frontend/src/runs.tsx` → `ui/frontend/src/widgets.tsx`).

**Deviation from MDX:** the renderer is not MDX. MDX compiles content to a function via
`new Function`/`eval`, which the gateway CSP (`script-src 'self'`, no `unsafe-eval`) blocks. The
renderer (`ui/frontend/src/mdx.tsx`) is instead `marked` (GFM, no eval) → `DOMPurify` (sanitize,
extended to keep the widget tags) → a plain recursive DOM-to-React walk — no JS ever gets
evaluated, so no CSP directive can block it. This is why widgets take only literal string
attributes: there's no expression evaluator left to compute anything richer (no `data={…}`
JSX-prop syntax). See `ui/frontend/src/mdx_render.test.tsx` for the regression coverage.

## Affordance widgets (action buttons Haku embeds free-form)

Beyond the view widgets above, there's a growing library of **action affordances**
(`ui/frontend/src/affordances.tsx`) — reviewed buttons you drop inline into any item/note body to
give the operator a one-tap way to act or report back, instead of a rigid typed action model. Each
wraps an already-gated capability, so embedding one never widens the trust boundary. Reach for
these freely to make notes actionable and to make it _cheap_ for the operator to steer — a tapped
button beats asking them to type. All use the same literal-attribute, alone-on-its-line block rules
as the widgets above.

- `<handoff prompt="…" label="<short imperative>"></handoff>` — opens a fresh claude.ai conversation
  seeded with the prompt. `label` is a **very short imperative summary** of what it'll do ("Cancel
  the subscription", "Draft the reply"); the button shows the Claude logomark. **Short prompts** go
  in the `prompt` attribute; a **long/multi-line prompt** goes _inside_ the tag as a fenced code
  block (a literal attribute can't hold newlines) — the widget reads its text content:

  ````text
  <handoff label="Renew the passport">

  ```text
  Help the operator renew their passport: confirm the current one's expiry, check the renewal
  eligibility, pre-fill the form fields we know, and surface what's still needed…
  (the full multi-line executor prompt)
  ```

  </handoff>
  ````

- `<launch prompt="…" label="…"></launch>` — asks the console to start a Haku run (the shell shows
  its own confirm first). Use when the follow-up is Haku's work, not the operator's.
- `<feedback text="<canned note>" label="<button text>" item="<id>"></feedback>` — one tap appends
  a fixed note to intake. Good for canned reactions (`👎 not useful`).
- `<choices prompt="<question>" item="<id>">` with `<choice value="…">` children — the workhorse
  for **capturing an outcome**: a single-select slot (at most one answer) plus an always-present
  "Other…" free-text escape. Composes like `<select>`/`<option>`; each `<choice>`'s `value` is what
  gets recorded, optional child text overrides the visible label (`<choice value="yes">Yes, all
set</choice>`). Records the answer as an intake note prefixed with the question, so the next run
  reads what happened and plans the follow-up. This is how to turn "you had appointment X to do Y"
  into one-tap reporting. **Nesting placement:** the whole block is one raw-HTML block, so keep the
  `<choice>` lines tight — no blank lines between them (a blank line would end the block):

  ```text
  <choices prompt="How did the appointment go?" item="…">
  <choice value="Missed it"></choice>
  <choice value="Went, as expected"></choice>
  <choice value="Needs a follow-up"></choice>
  </choices>
  ```

  Prefer it over asking the operator to type free-form; the "Other…" box is there for when the
  fixed options don't fit.

- `<signal-toggle scope="<id>" field="<slot>">` with `<choice value="…">` children — the **stateful**
  sibling of `<choices>`. Same authoring, but instead of a one-time intake note it writes the
  responses log (`responses/<scope>/<field>.yaml` — the file is the current answer, git history is
  the log), **prefills** the current answer (shown pressed), and re-picking the active answer clears
  the slot (radio-with-retract). Use it for a changeable, machine-read slot — an item's status, for
  example — rather than a report-once outcome. `scope` is an item id / context key, `field` the slot
  name:

  ```text
  <signal-toggle scope="renew-passport" field="status">
  <choice value="done">Done</choice>
  <choice value="snoozed">Snooze</choice>
  <choice value="rejected">Dismiss</choice>
  </signal-toggle>
  ```

- `<tool-call request="<state_request_id>" label="…"></tool-call>` — asks the haku-ui frontend to
  read `tool_requests/<state_request_id>.yaml`, send that exact body through the haku-ui backend,
  and submit it to haku-console for operator-approved execution. Use this when Haku can author a
  precise, schema-valid privileged call but should not run it autonomously — for example, "Restart
  stuck rollout" once a kubectl MCP catalog entry exists, or "Add arrived order items to inventory"
  once a Grocy MCP catalog entry exists. The request YAML carries the catalog id, rationale, and
  arguments; haku-console owns approval, execution, audit, and results. Use this for asynchronous
  review in context; the broader proposal/direct-RPC playbook lives in
  [`tool_calls.md`](tool_calls.md):

  ```text
  <tool-call request="restart-stuck-rollout" label="Restart rollout"></tool-call>
  ```

  Haku can query or sweep haku-console's tool-call audit log during its normal run when it wants to
  act on completed calls.

Most affordances record via feedback/intake or responses, so they're read by the next run, not a
live schema field. If code (a sort, an automation) must compute over an answer, that fact still
needs a real schema field. `<tool-call>` is the exception: the request is in git, but the result and
audit live in haku-console.

## Where this is headed

The registry is the seed of a general idea, not a fixed set: collapse bespoke one-off pages
(their own schema + endpoint + hand-built component) into garden documents that embed shared
widgets, so a page's structure scales continuously from plain prose to a fully interactive tool
instead of forking into "garden note" vs. "real page." The **`improvements` surface is the first
one migrated**: its data is the markdown files under `memory/improvements/` (per-item frontmatter +
prose), rendered by the `<improvement-board>` widget, which reads that directory through the generic
tree+blobs content proxy — no bespoke endpoint or component.

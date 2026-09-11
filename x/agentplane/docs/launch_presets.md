# Sandbox and Thread presets

Status: **implemented by PR [#5648](https://github.com/agentydragon/ducktape/pull/5648)**.
The focused tests and build landed; the configured manual staging acceptance remains useful deployment
evidence. This is an integration-app feature, not a new Agentplane runtime authority.

## Outcome

An operator can launch a useful agent without reconstructing its sandbox and thread settings by
hand, while retaining the existing free-form controls. Selecting a preset fills fields; it does not
lock them or grant capabilities beyond the caller's authority.

The first concrete preset is `public-coder`: a Codex runner Sandbox composing the `basic` and
`github-public` egress policies, a runner-owned workspace initialization script, and a Codex Thread
default.

## Split and ownership

A `SandboxPreset` owns what is established by or needed to initialize a Sandbox:

- the `SandboxTemplate` selection;
- egress policy selections;
- persistent workspace/runtime settings; and
- bootstrap source sent to the runner.

A `ThreadPreset` owns what is selected when a native Thread is opened:

- provider and model;
- appended standing instructions; and
- native session options exposed by the integration app.

A SandboxPreset may name one default ThreadPreset. This is one explicit association, not arbitrary
preset inheritance. Preset definitions, Sandbox-to-preset bindings, and override intent belong to
the integration app's product layer. Agentplane runtime APIs receive the resolved concrete
configuration and remain unaware of preset names.

## Live binding

The Sandbox annotation stores the SandboxPreset name, an optional ThreadPreset override, and only
the operator's explicit sandbox-level Thread edits. Later Threads resolve against the currently
configured presets, so a changed preset changes future defaults without overwriting a field the
operator customized. With no ThreadPreset override, a Sandbox follows the default ThreadPreset its
SandboxPreset names. A Thread keeps the effective configuration it was opened with.

## Resolution

The precedence is:

```text
explicit launch field > live preset field > platform default
```

At the Sandbox level:

```text
SandboxPreset + Sandbox overrides
```

At the Thread level:

```text
selected ThreadPreset + Sandbox Thread overrides + Thread overrides
```

A list supplied explicitly replaces the preset list. An explicit empty value clears a preset field
where the field is nullable. Omitted fields remain eligible for preset defaults. The same launch
fields remain available when no preset is selected.

## Update rules

Preset configuration is live: a bound Sandbox is resolved against the currently configured presets
at every session open.

- New Thread defaults update automatically.
- Existing Threads do not change.
- Mutable egress/runtime settings update only through existing supported runtime operations.
- A binding naming a SandboxPreset or ThreadPreset that is no longer configured makes
  `POST /sandboxes/{name}/sessions` answer 422 (`UnknownPresetError`); the Sandbox is neither
  deleted nor mutated. `POST /sandboxes` answers 422 for an unknown preset at creation.
- A changed bootstrap script is refused on a live Sandbox (below).

## Bootstrap

Bootstrap belongs to the SandboxPreset as inline script content. Before every session open on a
bound Sandbox, the app sends that content to the runner (`bridge.initialize`); the app never
executes shell. The runner pins the SHA-256 of the first script it is given under its state
directory, runs it with `/bin/sh -eu`, and keeps a replayable log of output and result: the same
script re-sent is a no-op once completed and a retry after a failure, while a different script is
refused (`FAILED_PRECONDITION`), so a changed preset bootstrap never reruns on a live Sandbox. A
refused or non-zero-exit bootstrap fails the session open with 409. Arbitrary user-provided shell,
automatic reruns after source changes, and a script registry are out of scope.

## UX

Keep the existing `Create Sandbox` and `Launch Thread` flows, adding an optional preset selector.
Selecting a SandboxPreset fills both the Sandbox fields and its inherited ThreadPreset fields; every
normal field remains editable.

Add a `Create Sandbox and Launch Thread` action for the common case. Creating only a Sandbox stores
the live preset binding and the edited defaults. Launching a Thread later starts from the Sandbox's
current effective Thread defaults, while per-Thread edits remain local to that Thread.

An existing Sandbox page shows the bound preset and whether its Thread default is inherited or
overridden, for example `Public coder · inherited`. A failed bootstrap fails the Thread launch; the
operator retries it or creates a fresh Sandbox.

## Implemented first slice

**Observed P0 behavior**

1. Define app-owned `SandboxPreset` and `ThreadPreset` configuration for `public-coder`.
2. Resolve a preset plus explicit overrides without changing the existing no-preset launch path.
3. Remember the Sandbox binding and use it for later Thread defaults.
4. Send the configured bootstrap source to the runner before opening the first Thread.
5. Add the preset selector and inherited-default presentation to the existing UI.
6. Add the one-action Sandbox-plus-Thread launch path.

**Live acceptance target**

After the landed images are deployed, launch `public-coder` through the integration app, override at least the model and standing
instructions, and verify that:

- the Sandbox receives the selected effective egress policy and runner configuration;
- the runner executes the configured bootstrap and creates the persistent workspace marker;
- the first Thread uses the edited Thread defaults;
- a later Thread starts from the Sandbox's live defaults while a per-Thread model override stays
  local;
- an updated preset changes future defaults but not an existing Thread;
- an unavailable egress policy or bootstrap configuration is rejected before launch; and
- a bootstrap failure is visible and does not produce a usable Thread.

## Deferred

- General-purpose capability/access profiles.
- Arbitrary preset inheritance graphs.
- Preset editing UI and rollout approval workflows.
- Automatic execution of changed bootstrap code.
- OpenClaw/Matrix migration.
- Per-preset credential authority or a new policy DSL.

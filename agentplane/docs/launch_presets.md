# Sandbox and Thread presets

Status: **implemented by PR [#5648](https://github.com/agentydragon/ducktape/pull/5648)**.
This is an integration-app feature, not a new Agentplane runtime authority.

## Outcome

An operator can launch a useful agent without reconstructing its sandbox and thread settings by
hand, while retaining the existing free-form controls. Selecting a preset fills fields; it does not
lock them or grant capabilities beyond the caller's authority.

The first concrete preset is `public-coder`: a Codex runner Sandbox composing the `basic` and
`github-agentydragon-agent` egress `PolicySet`s, a runner-owned workspace initialization script, and a Codex Thread
default.

## Split and ownership

A `SandboxPreset` owns what is established by or needed to initialize a Sandbox:

- the `SandboxTemplate` selection;
- egress policy selections;
- action policy set selections, editable at launch like the egress selections and written as one
  `PolicyBinding` per launched Sandbox;
- persistent workspace/runtime settings; and
- bootstrap source the Sandbox's Runtime runs once.

A `ThreadPreset` owns what is selected when a native Thread is opened:

- harness and model;
- appended standing instructions; and
- a one-time setup script run from the resolved Thread working directory; and
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
for each Session creation.

- New Thread defaults update automatically.
- Existing Threads do not change.
- Mutable egress/runtime settings update only through existing supported runtime operations.
- A Sandbox naming a SandboxPreset that is no longer configured makes
  `POST /sandboxes/{name}/sessions` answer 409 (`FailedPrecondition`); the Sandbox is neither
  deleted nor mutated. The browser selects creation presets from the app's configured catalog;
  EnvironmentService stores the submitted preset label, which is resolved at Session creation.
- A changed bootstrap script is refused on a live Sandbox (below).

## Bootstrap

Bootstrap belongs to the SandboxPreset as inline script content. The app resolves it into the
Sandbox creation request (`CreateEnvironmentRequest.bootstrap`). EnvironmentService stores it on the
Sandbox, and the Sandbox Controller hands it to Environment Runtime in the launch file; Runtime takes
bootstrap from no other source. The app never executes shell.

Runtime runs the script once per Sandbox with `/bin/sh -eu`, before any Session setup or harness
start, and refuses a changed script
([Runtime initialization](../env/runtime/docs/control.md#initialization)). A failed or interrupted
run is terminal: the Sandbox never becomes Ready, and the operator creates a fresh one. Automatic
reruns after source changes and a script registry are out of scope.

Thread setup is separate from Sandbox bootstrap. The app includes the selected Thread setup source
in logical `CreateSession`; the Dispatcher returns the Session resource and its current materialization
status while the placement owner handles setup. Setup output and its result become typed events in the
Thread's durable history. The owner creates the resolved `SessionSpec.cwd` and invokes `/bin/sh -eu`
there. A successful exit starts the harness in that same directory. Owner acceptance does not mean
setup has finished. A failed or interrupted setup leaves a readable Thread and workspace, but cannot
be retried in that Thread; the operator starts a new Thread. Haku and Finance presets use this
mechanism to create independent repository checkouts per Thread. The platform mechanism has no
Git-specific fields.

## UX

Keep the existing `Create Sandbox` and `Launch Thread` flows, adding an optional preset selector.
Selecting a SandboxPreset fills both the Sandbox fields and its inherited ThreadPreset fields; every
normal field remains editable.

Add a `Create Sandbox and Launch Thread` action for the common case. Creating only a Sandbox stores
the live preset binding and the edited defaults. Launching a Thread later starts from the Sandbox's
current effective Thread defaults, while per-Thread edits remain local to that Thread.

An existing Sandbox page shows the bound preset and whether its Thread default is inherited or
overridden, for example `Public coder · inherited`. A failed bootstrap fails the Thread launch; the
operator creates a fresh Sandbox.

## Deferred

- General-purpose capability/access profiles.
- Arbitrary preset inheritance graphs.
- Preset editing UI and rollout approval workflows.
- Automatic execution of changed bootstrap code.
- OpenClaw/Matrix migration.
- Per-preset credential authority or a new policy DSL.

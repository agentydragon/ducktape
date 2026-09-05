# Sandbox and Thread presets

Status: **implemented in PR #5648, pending merge/deployment evidence**. This is an
integration-app feature, not a new Agentplane runtime authority.

## Outcome

An operator can launch a useful agent without reconstructing its sandbox and thread settings by
hand, while retaining the existing free-form controls. Selecting a preset fills fields; it does not
lock them or grant capabilities beyond the caller's authority.

The first concrete preset is `public-coder`: a Codex runner Sandbox with the public-coder egress
policy, a runner-owned workspace initialization script, and a Codex Thread default.

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
preset inheritance. Preset definitions, revisions, Sandbox-to-preset bindings, and override intent
belong to the integration app's product layer. Agentplane runtime APIs receive the resolved concrete
configuration and remain unaware of preset names.

## Live binding and history

The integration app stores a stable Sandbox-to-SandboxPreset binding. The binding follows later
accepted revisions for future Threads and settings that can be reconciled in place. It stores
explicit Sandbox overrides separately from the preset so a changed preset cannot overwrite a field
the operator customized.

A Sandbox may explicitly choose a different ThreadPreset. With no such override, it follows the
current default ThreadPreset named by its SandboxPreset.

Each Thread records the SandboxPreset and ThreadPreset revisions used at launch, plus its explicit
Thread overrides. This is historical provenance, not a request to rewrite an existing Thread when a
preset changes. A resumed Thread keeps the effective configuration with which it was opened.

Accepted revisions are immutable and shared across references. If definitions initially come from a
ConfigMap, the ConfigMap remains the authoring source and the integration app records accepted
revisions in its durable store; a Kubernetes `resourceVersion` is not the application's history.

## Resolution

The precedence is:

```text
explicit launch field > live preset field > platform default
```

At the Sandbox level:

```text
SandboxPreset revision + Sandbox overrides
```

At the Thread level:

```text
selected ThreadPreset revision + Sandbox Thread overrides + Thread overrides
```

A list supplied explicitly replaces the preset list. An explicit empty value clears a preset field
where the field is nullable. Omitted fields remain eligible for preset defaults. The same launch
fields remain available when no preset is selected.

## Update rules

Preset changes reconcile through the integration app, which is the desired-state authority. The
runtime reports what was applied.

- New Thread defaults update automatically.
- Existing Threads do not change.
- Mutable egress/runtime settings update only through existing supported runtime operations.
- Pod template or mount-topology changes report that a new Sandbox is required.
- A changed bootstrap script becomes a pending update; it is never rerun automatically on a live
  Sandbox. Applying it is an explicit runner initialization operation.
- A removed or invalid preset leaves the last valid Sandbox state usable and surfaces the broken
  binding rather than deleting or silently mutating the Sandbox.

## Bootstrap

Bootstrap belongs to the SandboxPreset and is executed by the runner after the Sandbox Pod is ready
and before the first Thread is opened. The integration app resolves either inline configured source
or a configured file/ConfigMap path to content, then sends the content to the runner; the app never
executes shell.

The runner validates and executes the requested initialization, returns bounded structured status,
and writes an idempotence marker on persistent state. The first slice may use configured script
content. Arbitrary user-provided shell, automatic reruns after source changes, and a general script
registry remain out of scope.

## UX

Keep the existing `Create Sandbox` and `Launch Thread` flows, adding an optional preset selector.
Selecting a SandboxPreset fills both the Sandbox fields and its inherited ThreadPreset fields; every
normal field remains editable.

Add a `Create Sandbox and Launch Thread` action for the common case. Creating only a Sandbox stores
the live preset binding and the edited defaults. Launching a Thread later starts from the Sandbox's
current effective Thread defaults, while per-Thread edits remain local to that Thread.

An existing Sandbox page shows the binding and reconciliation state, for example:

```text
Sandbox preset: Public coder · live
Thread default: Public coder / Codex · inherited
Applied revision: 8
```

A Thread shows the revisions and overrides that produced it. If a different preset requires a new
Sandbox template or bootstrap/runtime shape, the UI must say so and offer creating a new Sandbox;
it must not silently apply only half of the preset.

Initialization is visible in the Sandbox lifecycle (`Initializing workspace`) and a failed required
bootstrap prevents the Thread launch action until the operator retries or creates a fresh Sandbox.

## First implementation slice

**P0 behavior**

1. Define app-owned `SandboxPreset` and `ThreadPreset` configuration for `public-coder`.
2. Resolve a preset plus explicit overrides without changing the existing no-preset launch path.
3. Remember the Sandbox binding and use it for later Thread defaults.
4. Send the configured bootstrap source to the runner before opening the first Thread.
5. Add the preset selector and inherited-default presentation to the existing UI.
6. Add the one-action Sandbox-plus-Thread launch path.

**Needed support**

- Durable app records for preset revisions, Sandbox bindings, and Thread launch provenance.
- A runner initialization operation with idempotence and bounded result reporting.
- Reconciliation for changed preset revisions where the runtime supports it.
- A dedicated `public-coder` egress policy and a real acceptance fixture.

**Acceptance test**

Launch `public-coder` through the integration app, override at least the model and standing
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

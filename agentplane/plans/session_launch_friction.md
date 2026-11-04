# Reused-Sandbox Thread launch

Status: proposal for operator review, 2026-11-02. This proposes one frontend-only first slice; it
does not authorize implementation or change the OpenSession contract.

## Proposed first slice

Add a **New thread** action to each row on the existing `/sandboxes` list. It opens
`/sandboxes/{name}?tab=threads&new_thread=1`, with the existing `NewThreadForm` already expanded.
Remove `new_thread=1` when the operator closes the form. Keep the Sandbox name action as the normal
details-page link. The shortcut carries only the Sandbox name in the route; the page reads the
current Sandbox snapshot and uses its current UID when the operator submits.

The first slice changes list navigation and form-open state, plus the form's submit guard while its
defaults are loading. It reuses the current editable fields and API. It does not add a global
Sandbox-free composer, Session/attachment API, remembered last-used values, or Sandbox provisioning
controls. No backend gap blocks this slice: `POST /sandboxes/{name}/sessions` already opens a
Session against the selected existing Sandbox.

The chosen Sandbox is also the access choice. This Session form has no policy override: the new
Session inherits the Sandbox's existing ServiceAccount, credentials and policy bindings.

| Starting point | Current path                                                                                                                                  | Proposed path                                                                                                                                |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Threads home   | click **New thread (via Sandboxes)** → `/sandboxes` → open the Sandbox by name → **New thread** → review or edit defaults → **Create thread** | click **New thread (via Sandboxes)** → `/sandboxes` → **New thread** on the chosen Sandbox row → review or edit defaults → **Create thread** |

The row action removes the extra details-page click. The route opens the same Sandbox page, selects
its default Threads tab, and expands the current form. The page's existing readiness check remains
authoritative: it allows a ready Sandbox or one that can wake on send, and shows the existing reason
when creation is unavailable. Keep that gate in the page instead of duplicating lifecycle rules in
the list.

## Defaults and overrides

| Field                                  | Source and behavior in the proposed flow                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Harness, model, reasoning effort       | For a preset-bound Sandbox, use the app configuration's `SandboxPreset → ThreadPreset` defaults, surfaced by `/upstream/app-config/v1/presets`; the model catalog from `/upstream/app-config/v1/models` supplies the offered values. Without a preset, retain the form's current fallback: first supported harness (Claude when offered), first offered model, and `low` effort when offered or the model's first offered effort. Hide the effort selector when the model offers none. |
| Working directory                      | Use that same ThreadPreset's `cwd` unchanged. With no preset, retain `/state/workspaces/{session_id}`. Keep the template visible and editable; the app replaces `{session_id}` with the newly minted Thread/Session ID at OpenSession. A template containing the token gives each Thread its own path. A fixed path remains shared by design; this UI must not silently rewrite it.                                                                                                    |
| Standing instructions and setup script | Use the ThreadPreset values; keep both in the existing collapsed disclosures. Preserve the current `SessionSpec.instructions` meaning and one-time setup-script behavior; neither is a user message or starts a model turn.                                                                                                                                                                                                                                                            |
| Operator edits                         | Keep Harness, Model, Reasoning effort and Working directory visible and editable. Treat edits as choices for this new Thread only. Do not save them as Sandbox defaults or reuse them for later launches.                                                                                                                                                                                                                                                                              |

The repository's current staging defaults in [app_settings.py](../../cluster/cdk8s/agentplane/app_settings.py)
and [staging_config.py](../../cluster/cdk8s/agentplane/staging_config.py) for `public-coder`, `haku`,
and `finance-agent` include `{session_id}` in `cwd`, with their configured subdirectory suffixes
preserved. That is configured behavior, not an app-enforced guarantee for every preset.

The browser currently fetches preset defaults and the model catalog separately. Before enabling
**Create thread**, wait for both requests when a Sandbox has a preset; report a failed or missing
preset instead of allowing the form's initial values to be sent as overrides. For a Sandbox with no
preset, the existing form fallbacks may be used once the model catalog is ready. This makes the
values shown before submission match the values the operator intends to accept.

Defaults are configuration, not history: the shortcut should not copy the last Thread's explicit
overrides. At OpenSession, the app independently resolves the Sandbox's configured preset,
validates the current Sandbox UID, overlays the submitted SessionSpec fields, expands `{session_id}`
in `cwd`, creates Thread metadata and submits one opening to the Session Dispatcher. The caller
keeps the same minted ID to check an uncertain opening result; it must not retry with a new ID.

## What the action means

The user chooses a **Sandbox** (the existing execution environment), then creates a new **Session**
in it and sees that interaction as a **Thread**. A Session is the logical agent interaction,
whether running or retained; the Thread is its conversation projection. The current app uses the same public ID for
both. The Session's Event history is evidence of the interaction, not its identity. This action
opens a Session in the selected Sandbox; it does not create a new Sandbox. The Thread page opens
when History projects it. No user input is submitted by the Create action, and standing
instructions alone do not start a turn. The operator sends the first message separately.

This is reuse, not a new isolation boundary: Sessions in one Sandbox share its filesystem, volume,
credentials and ServiceAccount authority. A distinct `cwd` is a distinct path, not a security
boundary. If a Sandbox is suspended, wake-on-send may still incur its existing startup delay. This
shortcut avoids fresh-Sandbox provisioning for a ready reusable Sandbox; it does not improve VM
startup latency (`VM_STARTUP_LATENCY` remains independent).

## Review decisions

1. **Entry-point recommendation:** approve a per-row **New thread** action. It preserves explicit
   Sandbox selection, removes one navigation click, and uses the current OpenSession API.
2. **Defaults recommendation:** use the currently configured ThreadPreset for that Sandbox, not
   last-used Thread values. Confirm that launch-specific edits should remain ephemeral.
3. **Working-directory recommendation:** preserve the configured path exactly and rely on
   `{session_id}` where separate per-Thread paths are intended. Confirm whether the form should warn
   when a configured preset path lacks that token; do not append it automatically.

After review, the implementation acceptance is one end-to-end browser path from the Sandbox list
through the prefilled form to the existing OpenSession endpoint, including visible overrides,
loading/error handling for defaults, unchanged ID reconciliation and normal visual review. It must
also show that **Create thread** submits no user message. Fresh-Sandbox provisioning, preset
ownership changes, and VM timing measurements stay outside this slice.

## Source paths inspected

- [Sandbox list](../app/frontend/sandboxes/list.tsx), [Sandbox page](../app/frontend/sandboxes/sandbox_page.tsx), and [New Thread form](../app/frontend/threads/new_thread_form.tsx).
- [OpenSession facade and default resolution](../app/go/sessionapi.go) and [browser Session client](../app/frontend/sandboxes/client.ts).
- [Session and Thread terminology](../docs/system_overview.md#sandbox-lifecycle-and-runner-control) and [Sandbox isolation boundary](../docs/thread_layering.md#sandbox-isolation-boundary).

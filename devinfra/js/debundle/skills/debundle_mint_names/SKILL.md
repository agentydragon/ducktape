---
name: debundle_mint_names
description: >
  Assign descriptive names to unrenamed symbols in any debundle spec. Reads the
  emitted JS, understands each symbol's implementation and call sites, and
  updates the YAML spec `name:` field. Does NOT move modules or edit taxonomy.
  Trigger: user wants to name/rename minified symbols in a debundle RE spec.
---

# Debundle Mint Names

Use this role to name bindings that already belong to a module but still use an
input-bundle name. Read the [naming task guide](references/docs/naming.md) for
candidate selection, evidence, naming, edits, and validation. If tooling blocks
the task, follow [bug reproductions](references/docs/bug_reproductions.md) first.

The only permitted spec edit is the `name:` field of an existing member. Do not
move bindings, change selectors, create modules, add residual bindings, or edit
module paths or taxonomy. Read the implementation and a meaningful call site;
leave the binding unchanged when its purpose remains unclear. Obtain the active
modules tree and matching source/emitted output from the project adapter. Use
the adapter's naming checks; do not run extraction/cycle gates. Report changed names, unresolved candidates,
and any structural observations separately. For coordinated work involving
other roles, use the [shared Debundle workflow](references/skills/shared/workflow.md).

# Naming Existing Debundle Bindings

Use this guide when a debundle spec already assigns a binding to a module but
still exposes the input bundle's name. The goal is to give that binding a
concise name supported by its behavior and call sites.

This is a naming-only task. Edit the `name:` of an existing member and leave its
selector, module path, membership, taxonomy, and other spec fields alone. Do not
add bindings for residual code; record a useful lead for a later extraction or
planning task. If the source is too opaque to support a name, keep the current
name and report the uncertainty.

## Inputs and candidate selection

Obtain the exact target's active modules directory, matching source snapshot,
and emitted JavaScript tree from the project adapter. Use artifacts produced
from the same input and spec revision; stale output can make a plausible name
refer to a different declaration.

When available, start with the pipeline's `reports/rename_queue.json`. Its
entries identify top-level output bindings still using an input-bundle name and
rank them by reference count, then module fanout. Use the queue to choose a
high-value seed, then confirm that the binding has an existing member entry in
the active YAML. The queue can include declarations that have not been claimed
by a module; those are outside this task. If the report is unavailable, inspect
the active module files and find entries whose `name` still equals
`selector.binding.name`. Do not treat short spelling alone as proof that a
binding needs a name.

## Read in neighborhoods

Work through a local cluster of related symbols so terminology stays coherent.

1. Choose a seed from the user's request, a small module, or the highest-ranked
   queue entry. Read the whole emitted module, not only the target declaration.
2. Name all confidently understood bindings in that module before leaving it.
3. Follow imports, exports, and callers into adjacent modules. Read each target's
   definition and enough call sites to see arguments, return-value use, and
   side effects. Continue while the neighborhood is understandable; start a
   separate seed when it becomes unrelated or opaque.
4. Use the YAML and emitted code together. The assigned module name and nearby
   already-named symbols help orient the review, but neither is proof of what a
   binding does.

For each candidate, read the implementation and at least one meaningful
call-site. For a function, determine its inputs, result, and effects. For a
class, inspect its constructor, methods, and base class. For a value, inspect
its initializer and uses. For a component, inspect its props, hooks, and
rendered structure. If emitted code is unavailable, use the exact upstream
source as a fallback and report that limitation.

## Choose and apply a name

Name the behavior or domain concept, not the minifier token or implementation
type. Prefer a short, specific name consistent with established siblings:

| Kind                      | Common shape                       | Examples                         |
| ------------------------- | ---------------------------------- | -------------------------------- |
| Predicate                 | `isX`, `hasX`, `canX`              | `isNodeEmpty`, `hasChildren`     |
| Accessor                  | `getX`, `findX`, `resolveX`        | `getNodeById`, `findParent`      |
| Mutation                  | `setX`, `updateX`, `applyX`        | `setSessionId`, `updateFilters`  |
| Factory                   | `createX`, `buildX`, `makeX`       | `createPalette`, `buildQuery`    |
| Formatting                | `formatX`, `renderX`, `serializeX` | `formatError`, `serializeTree`   |
| Class, type, interface    | `PascalCase` noun                  | `NodeAccessor`, `PanelState`     |
| Constant or configuration | `camelCase` noun                   | `defaultTimeout`, `nodeIdLength` |
| Component                 | `PascalCase`                       | `CommandButton`, `FilterPill`    |

Avoid guesses, generic names such as `processData` or `helper`, names that only
encode a type, and long names that restate the full call signature. A minified
name is preferable to a confident-sounding but unsupported name.

Use the current rename operation, for example:

```sh
debundle bindings rename --modules <modules-dir> <current-name> <new-name>
```

It checks readable-name collisions within the chunk. If editing YAML directly,
change only the existing member's `name:` field. A rename may affect other
`source_match` templates: identifiers can stop resolving to the old export name
or begin resolving to the new one. After each coherent batch, run the
source-only validation on the same modules and source inputs. Compare selector
outcomes and resolved reference identities with the pre-rename result so
a readable-name change does not silently change a selector claim:

```sh
debundle spec validate --modules <modules-dir> --source-file <chunk.js> --format json
```

Use the adapter's documented naming checks and keep their results separate from
source-only validation. Do not run extraction/cycle gates or repair module
boundaries as part of naming; report problems requiring broader spec changes.

## Leave structure for its owner

If code appears misplaced, two modules seem to share a concept, or a module
path/name should change, record the evidence in the project's designated notes
or report it to the user as a follow-up. You may write a structure-notes file
for those suggestions, but tell the user where you put it. Do not implement
those changes here.
For coordinated work across roles, see the [shared Debundle workflow](../skills/shared/workflow.md).
If the tool cannot express or validate the requested selector, follow the
[bug reproduction guide](bug_reproductions.md) before proposing a tooling fix.

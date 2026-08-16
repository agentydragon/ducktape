# Spec YAML Language Cleanup

Historical design note for the migration that introduced canonical
`source_matches[]` binding claims and module-level `annotations`. This file
preserves the reasoning behind the migration; use the current docs for authoring
syntax.

## Goal

Make debundle module YAML describe three different concerns in three different
places:

1. **Selectors** say how to find source entities.
2. **Binding claims** say which matched bindings a logical module owns and what
   readable names they get.
3. **Binding annotations** say what the owned binding means to analysis or to a
   human reviewer: purity, local effects, callback-storage behavior, comments,
   notes, and similar author-owned metadata.

The intended end state was Ducktape and gaffer-private both using the new
format, with no permanent compatibility shims for the old YAML shapes.

## Current Problems

### Semantic hints are attached to selector syntax

`members[]` can carry `purity`, `effect`, `pure_members`, and
`no_sync_callback_members`. `binding_groups[]` cannot carry those facts without
adding more parallel per-binding maps. That makes selector rewrites change
where metadata has to live.

The Tana spec makes this visible today:

- `effect: typescript_decorate_helper` forces decorator helpers to stay as
  singleton members when they would otherwise fit a binding group.
- `no_sync_callback_members` sometimes forces a name-pinned singleton because
  the hint must be visible to analysis.

### Binding groups use parallel metadata maps

`binding_groups[]` currently has `exports`, `comments`, and `notes`, each keyed
by selector-local binding name. Adding `effects` or callback hints would repeat
that shape and make every new per-binding field a group-specific map.

The more durable rule is: per-binding metadata should be keyed by final readable
binding name, not by the selector-local spelling that happened to capture it.

### `source_match.target_binding` mixes selector body and claim projection

`SourceMatch` is shared by member selectors, binding groups, and anonymous
statements, but `target_binding` is meaningful only for some claim sites. Binding
groups reconstruct it from `exports`; anonymous statements reject it.

This is a schema smell: the selector body should be reusable, and claim sites
should project the desired target binding outside the selector body.

### Singleton source matches and binding groups are the same concept

`members[].selector.source_match` and `binding_groups[]` both mean "match this
source shape, then claim one or more local bindings from it". The singleton form
claims one `target_binding`; the group form claims several keys from the old
`exports` map. Keeping two schema branches makes codemods and metadata migration
more complex than the concept requires.

### `identifiers` is legacy surface area

The public selector mode is effectively alpha-all. `identifiers: alpha_all` is
schema noise, while exact mode is rejected. Generated YAML should stop emitting
the field, and the field should be removed after migration.

### `chunk_renames.members` reuses `Member` but is not a member list

`chunk_renames.members` uses `Vec<Member>`, but not all member selector forms are
accepted there. That makes the shape look more general than it is. It should get
a dedicated schema with only the fields that actually work for in-place chunk
renames.

### YAML reserialization is intentional

Ducktape codemods are allowed to reserialize YAML. Persistent author data must
live in typed YAML fields such as `comment`, `note`, `comments`, or future
`annotations` entries. Supporting raw YAML `#` comments as preserved data is out
of scope for this plan.

## Target Shape

### Source-shape claims

Add a source-shape claim list that replaces both singleton
`members[].selector.source_match` and `binding_groups[]` in the final schema:

```yaml
source_matches:
  - match: |
      var objectDefineProperty = Object.defineProperty,
        objectGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor,
        tsDecorate = (decorators, target, key, kind) => {
          STMT_LIST;
        };
    bindings:
      - objectDefineProperty
      - objectGetOwnPropertyDescriptor
      - tsDecorate

  - match: |
      const workspaceInviteStateSingleton = new WorkspaceInviteState(),
        workspaceInviteState = workspaceInviteStateSingleton,
        DECLARATORS_AFTER = null;
    bindings:
      - workspaceInviteStateSingleton
      - workspaceInviteState
```

A singleton source selector is the same shape with one `bindings` entry. The
selector body has no `target_binding`; claim projection lives in `bindings`.
A string entry is shorthand for `{ local: foo, name: foo }`; when an object entry
omits `name`, it defaults to `local`. Old `adopt_names` becomes ordinary
binding-list entries rather than another special field.

Keep non-source selector forms (`binding`, `cross_ref`, `reads_member`,
`member_of_module`, `passed_to_call`, `makes_decorate_call`, `intrinsic_alias`)
under `members[]` for this migration. They are single-target selectors and do
not need the multi-binding source-shape surface.

### Per-module binding annotations

Add a module-level map keyed by final readable binding name:

```yaml
annotations:
  tsDecorate:
    effect: typescript_decorate_helper
  workspaceInviteState:
    no_sync_callback_members:
      - setPendingJoinWorkspace
```

`annotations.<binding>` should own member/binding metadata:

- `purity`
- `effect`
- `pure_members`
- `no_sync_callback_members`
- `comment`
- `note`

Unknown `annotations` keys are spec errors unless the logical module claims that
readable binding name through `members[]` or `source_matches[]` after
migration-time expansion of old shapes.

During the migration window, inline member fields can be normalized into the
same internal binding-annotation structure. If inline fields and `annotations`
specify the same fact or annotation differently, Ducktape should fail the spec
rather than guess precedence.

### Binding-keyed annotations beat group-local maps

For metadata that describes the claimed binding rather than the selector, prefer
the module-level `annotations` map over more maps under `binding_groups[]`.

That means a selector rewrite from singleton member to binding group does not
move the binding's annotations. It also means `binding_groups[].comments` and
`binding_groups[].notes` become migration targets rather than a pattern to
extend.

### Claim projection outside `source_match`

The `source_matches[].bindings` list accepts object entries so a future extension
can add fields without inventing parallel maps:

```yaml
source_matches:
  - match: |
      const a = EXPR_A, b = EXPR_B;
    bindings:
      - a
      - local: b
        name: readableB
```

This structured form is mostly about selector/claim clarity. Binding annotations
still live under `annotations`, keyed by `a` and `readableB`.

### Cross-module grouped claims are not part of this migration

A useful-looking extension is one selector body whose targets land in different
logical modules, instead of letting the YAML file path determine every target's
destination. That could look like a global claim table or like target entries
with an optional `module:` field.

Do not bundle that into the current migration. A structured parse of the Tana RE
spec found 6,451 selector entries, 6,449 distinct `match` bodies, and zero exact
`match` bodies reused across module files. The only exact repeated selector body
is the known `app/localeSettings.yaml` decorator-helper trio inside one module.
Binding-keyed annotations and single-module `source_matches` address that case.

Keep `source_matches[].bindings[]` compatible with a future `module:` field, but
only implement cross-module grouped claims when a real spec has repeated
selector bodies or module-boundary work that would materially benefit.

## Historical Migration Plan

### Phase 1: Ducktape accepts the new shape

- Add a `LogicalModule::source_matches` list that lowers to member requests.
- Add a `LogicalModule::annotations` map and a binding-annotation schema.
- Merge member inline metadata and module `annotations` into one internal view
  during lowering.
- Preserve then-current YAML long enough for Ducktape tests and the Gaffer spec
  to migrate.
- Add validation:
  - each `source_matches[].bindings[].local` must be a binding declared by the
    selector body;
  - `annotations` keys must name claimed bindings from the same logical module;
  - duplicate conflicting binding annotations are errors;
  - metadata that affects analysis must be available before the analysis phase
    that consumes it.
- Teach selector/codemod output to write new-style `annotations`.
- Add a migration codemod or codemod mode that:
  - converts `members[].selector.source_match` to singleton `source_matches`
    entries;
  - converts `binding_groups[]` to multi-binding `source_matches` entries;
  - expands `binding_groups[].adopt_names` to explicit `source_matches`
    bindings;
  - converts existing member metadata to `annotations`;
  - converts `binding_groups[].comments` / `notes` to `annotations`;
  - removes emitted `identifiers: alpha_all`;
  - reports any remaining old-shape fields after conversion.
- Stop emitting `identifiers: alpha_all` in generated selector YAML.
- Add fixtures covering:
  - non-source `members[]` binding with `annotations.effect`;
  - non-source `members[]` binding with `annotations.comment` /
    `annotations.note`;
  - `source_matches` entry with one binding and annotations;
  - `source_matches` entry with several bindings;
  - multi-binding source match with `annotations.effect`;
  - multi-binding source match with `annotations.comment` / `annotations.note`;
  - multi-binding source match with `annotations.no_sync_callback_members`;
  - stale `annotations` key rejection;
  - inline/new conflict rejection.

### Phase 2: Convert gaffer-private completely

- Convert all inline member `purity`, `effect`, `pure_members`, and
  `no_sync_callback_members` to module-level `annotations`.
- Convert all `members[].selector.source_match` entries and all
  `binding_groups[]` entries to `source_matches[]`.
- Convert member-level `comment` / `note` and `binding_groups[].comments` /
  `binding_groups[].notes` to binding-keyed annotations.
- Remove generated `identifiers: alpha_all` from selectors.
- Keep YAML reserialization acceptable; do not preserve raw YAML comments.
- Run the Tana RE validation gates on the migrated spec.

### Phase 3: Pause for repository handoff

At this point Ducktape accepted both forms and gaffer-private used only the new
forms. The pause existed so the Ducktape and Gaffer changes could be
squash-merged in the right order. Gaffer was the only debundle consumer, so a
separate spec capability/version check was not needed for this migration.

### Phase 4: Delete old Ducktape acceptance

After the migrated Gaffer state was merged and consumed the new Ducktape
behavior:

- Remove inline member metadata fields from the accepted module YAML schema:
  `purity`, `effect`, `pure_members`, `no_sync_callback_members`, `comment`,
  and `note`. Module-level and anonymous-statement comments/notes stay in place
  because they are not binding-keyed.
- Remove `members[].selector.source_match`.
- Remove `binding_groups[]`.
- Remove `BindingGroupAdoptNames` / `adopt_names`.
- Remove `source_match.identifiers` and the identifier-mode discriminator.
- Remove any code that emits `identifiers: alpha_all`.
- Replace `chunk_renames.members: Vec<Member>` with a dedicated chunk-rename
  schema and delete the partial-`Member` compatibility path.
- Update docs and examples so the old shapes are gone, not merely deprecated.
- Delete tests whose only purpose was proving old-shape compatibility.

## Non-goals

- No long-term dual read/write support.
- No raw YAML comment preservation.
- No spec feature-version gate for this migration; the Gaffer consumer will move
  in lockstep.
- No independent syntax for `effect` or callback hints under source-shape
  claims. They belong in shared binding annotations.

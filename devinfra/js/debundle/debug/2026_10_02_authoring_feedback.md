# Debundle authoring feedback: unresolved requests

Collected 2026-10-02 from Claude Code Web debundling work. Evaluated against
Ducktape `cb5ef4b52a75b39ec7e6f3611fefe4128135a863`, with only the report and
synthetic reproduction harness added. All examples below were invented from
scratch; they contain no upstream application source or session data.

These are **feature requests / usability limitations**, not confirmed new
miscompilation bugs. Rejected input is distinguished from incorrect output.
The list contains only outstanding work.

## Reproduction

From the repository's Nix devshell, following its Bazel execution instructions:

```sh
bbr test \
  //devinfra/js/debundle/e2e:authoring_feedback_repro_test \
  //devinfra/js/debundle/e2e:duplicate_top_level_declaration_test \
  --test_output=all --test_arg=--nocapture
```

The [synthetic reproducers](../e2e/authoring_feedback_repro_test.rs) drive the
real CLI using the existing fixture harness. They are tagged `manual` because
they characterize current limitations: **passing means the limitation was
reproduced**, not that the requested feature exists. Convert their assertions
to the desired contract when implementing a request. The existing repeated
declaration test is an additional independent reproduction for R3.

The remaining characterization case covers R4; the existing
repeated-declaration suite covers R3 and scope separation. The original
validation run passed on the revision above.
[BuildBuddy run and test output](https://app.buildbuddy.io/invocation/c6245dea-5f4a-40f2-a051-15ea9873c85f).

## R3 — Support valid repeated top-level `var` declarations

**Type:** JavaScript coverage feature request. **Priority:** medium.

**Impact observed:** a duplicate-top-level-binding rejection blocked a
full-corpus run. The exact original declaration combination has not been
established here; this report deliberately makes the narrower, independently
reproducible claim below.

**Minimal valid JavaScript module:**

```js
var counter = 1;
var counter = 2;
console.log(counter);
export { counter };
```

Select `counter` into a logical module with `selector.binding.name: counter`.
Current analysis rejects the chunk with:

```text
duplicate top-level declaration of binding `counter`
```

The existing
[`duplicate_top_level_var_declaration_is_rejected`](../e2e/duplicate_top_level_declaration_test.rs)
uses the same minimal language construct with synthetic string values.

**Requested behavior:** represent one binding's multiple declaration sites
without losing either initializer or changing their order. Preserve the
binding's import/export identity when splitting a chunk containing this form.

**Acceptance:** input and emitted entry both print `2`; an additional case
with a read between initializers preserves both observable values. Invalid
lexical redeclarations must remain rejected.

**Correctness boundary:** the current rejection is deliberate fail-closed
behavior. Simply allowing a later declaration to overwrite the earlier owner
mapping is not a solution. Duplicate function declarations are not evidence
for this request, because their legality depends on parse context.

## R4 — Allow explicit, scope-aware names for parameters and locals

**Type:** readability / spec-authoring feature request. **Priority:** medium.

**Impact observed:** naming extracted functions still leaves substantial
minified dataflow inside them. An engineer reading a recovered fold or
dispatcher must repeatedly infer what parameters and intermediates represent.

**Minimal source:**

```js
function a(b) {
  return b + 2;
}
```

The spec claims the function as `addTwo` using:

```js
function selected(amount) {
  return amount + 2;
}
```

**Current behavior:** the emitted function is `addTwo(b)` and still reads `b`.
The readable `amount` in the selector is an alpha-matching placeholder, not
an authored local rename. The reproducer also executes the emitted module to
confirm its behavior is correct.

**Requested behavior:** an explicit opt-in spec annotation for selected
parameters/locals, anchored to lexical binding identity inside a resolved
function. It should survive upstream minifier renaming. Do not automatically
adopt every selector placeholder: many are intentionally generic or describe
matching context rather than the variable's meaning.

**Acceptance:** an authored `amount` name appears in the output and all uses;
shadowed bindings and same-spelling parameters in other functions remain
independent; collisions are diagnosed or safely resolved; Node behavior is
unchanged. Function/compiler control-flow simplification is a separate feature.

**Existing capability:** debundle already performs some heuristic local
naturalization and safely renames top-level claimed bindings. See
[the current spec fields](../spec/spec.rs) and
[scope-local naturalization coverage](../e2e/naturalize_cross_scope_params_test.rs).
The gap is author-controlled semantic names for locals not inferable by those
heuristics, not a claim that local naturalization is entirely absent.

## Evidence still needed before filing additional bugs

- The unfinished classifier selectors need a control that demonstrates valid
  supported syntax matching the intended declarations but being rejected.
  There is no verified matcher bug from that extraction in this report.
- Native solver abort/OOM reports need a captured failure and a reduced model
  before attributing them to a debundle defect. No minimal crash reproducer
  was recovered, so no crash bug is asserted here.

# Checking two splits in one module graph

[The Lean primitive](../lean/EsmSplit/CoreJS/JointStatic.lean) checks a closed
pair of static modules. Supply a plan, the original annotated ESTree and the
output ESTrees for each. Both plans use the same deployment root. The checker
resolves relative requests from the real declaring and importing filenames,
requires disjoint output paths, and checks that every original unit belongs
to exactly one output of its own plan. Neither chunk can silently replace an
output of the other.

The app's units keep their source ordinals. Provider identities start after
the app's complete unit array, including non-executable import/export rows.
The same identity follows a unit into the split graph and numbers its opaque
callable sites. This keeps allocation regions and captured cells comparable
without relying on either graph's traversal order.

The checker links both programs from the app, including the generated glue.
It checks syntax and resolved references and validates one schedule proof for
the whole graph. Its result is equality of normal final CoreJS states in
every world, plus the same public names bound to the same live cells at both
original paths. Checking the provider namespace uses the app's linker
witness; it does not reroot evaluation at the provider.

[The executable fixture](../translation/linked/joint_static_test.mjs) uses
two source-hashed resolved placements and the production planner. For the
positive case it emits and disk-checks both splits, assembles their files,
reparses them, runs the joint checker, and exercises the live exports in Node.
Two separately certified splits that claim the same private URL instead
share one cell in the overlay. A second negative uses distinct URLs: in a
cycle the provider-root run hides an early read that changes when the app is
the root. The checker refuses the app-root move.

## Boundary for main and editor

This primitive takes ASTs under the usual parser and scope-annotation premises.
It fully lowers two modules in the empty host profile and refuses location
uses (including deferred dynamic imports and metadata). The caller supplies a
closed, one-to-one mapping from safe paths to module resources on a single
origin. The primitive does not publish files or attest to their bytes.

The [historical Excalidraw deployment replay](../integration/excalidraw/joint.md) illustrates these additional obligations:

1. Freeze the app entry URL, both source hashes and named placements, host
   contract, import map, and the resource inventory of the whole deployment.
   Stage **both** plans in one output tree; reject collisions with other
   outputs and existing resources. Reparse that exact tree and check each
   callable's preserved source spelling as in the single-output disk check.
2. Give every original unit across both chunks and all shared providers a
   single identity. Include each shared dependency once. Compute static
   requests, lazy closure edges, URLs and synthetic metadata cells per actual
   referrer on each side. Compare the requested public interfaces and retain
   the original receiver/record identities and live-cell aliases. A provider
   that is itself split cannot be treated as unchanged by the loader check.
3. Run the linker and structural schedule checker from the app over that
   combined closure, with host premises recorded once for the deployment.
   Recheck the assembled disk artifacts before reporting a certificate. Two
   individual verdicts and two successful browser runs do not discharge
   these obligations.

The fixture is runnable with
`bbr test //devinfra/js/debundle/x/split_proof/translation/linked:joint_static_test`.

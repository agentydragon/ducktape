# Import guarantees and publication boundaries

This is the contract for evaluating an import-preserving split. It separates
three obligations: the CoreJS graph comparison, the public-client
boundary, and realization of the checked graphs in a deployment. The
[publication receipt audit](publication_receipt_boundary.md) records what the
existing routes actually check. The agreed obligations below do not by
themselves establish a general contextual theorem or a CLI admission rule.

## CoreJS graph boundary

The input at this boundary is a finite, already resolved module graph with a
designated entry and ordered imports. The source and output may each contain
multiple module records. A rule claiming this boundary must derive both graphs
from the actual source, the authored ownership and import priority, and the
independently reread output. It must retain the original public export names
and their live cells, including aliases. For every modeled world and equal fuel,
compare the complete run result, including normal, throwing and other modeled
outcomes and their state where retained. An unsupported or unknown operation does not become
proved JavaScript behavior merely because both sides are unknown.

The CoreJS model, parser and scope annotations, lowering, and host semantics are
trusted or separately tested as described in [the target theorem and trusted
parts](../PLAN.md#target). The current constructor rules establish scoped
instances of the graph result for their accepted input shapes; they do not
accept every finite graph or every JavaScript operation. The
[route audit](publication_receipt_boundary.md) identifies the supported source,
provider and readback boundaries. For example, the current shared static
provider route admits a particular closed relative provider, not arbitrary
external dependency graphs. Some Lean results or receipts have a narrower
normal-completion guarantee; do not read them as an all-outcomes proof. The
abstract graph result does not by itself prove all future calls through exported
values or a multi-entry client theorem. Those claims need their own modeled
contexts and proofs; specific callable rules can prove more within their scope.

## Public-client contract

Declare the original public entry points, including each supported public
subpath. A conforming transformation must preserve what clients can observe
through those names: export names and aliases, calls, mutation, identity,
side effects before throws, and evaluation order wherever it affects those
observations. Preserve the meaning and identity of existing external
dependencies, and choose fresh names for new modules. This contract includes
observable properties on published values; it does not silently discard
function names, descriptors, source text, or other observations merely because
an implementation or model does not currently cover them.

Generated paths are private to this contract. A client that detects, imports,
or enumerates implementation files is outside this client boundary unless
the deployment actually encapsulates those files so that the observation is
unchanged. Privacy here describes the permitted observation boundary; placing
a file on disk does not make it inaccessible. The condition is about what a
client can observe or reach, including through calls and capabilities, not
whether its own AST lacks a particular token or import expression. A checker
may instead certify a specific narrower client, provided it states and proves
that narrower context. Neither the public boundary nor a copied caller
establishes such a theorem by itself.

Public initialization order belongs to the contract when a client can observe
it. The [default snapshot control](../integration/library_splits/lodash_is_length_test.mjs)
uses a public default function captured before a reassignment: the original and
certified output return `true` for `3`. Moving that same snapshot after the
reassignment makes the public default return `false`, and disk readback
refuses the changed output. This is an observable initialization-order change,
even though the client uses the public entry.

An independent [Excalidraw projection control](../integration/excalidraw/visible_elements_region.mjs)
instruments an early read of `st`: it prints `undefined` before the arrow
assignment and `function` when moved past it. The early placement is refused.
This is an instrumented stdout observation of the real projection, not an
untouched public client. Its [replay](https://app.buildbuddy.io/invocation/39c7a34d-d0f1-4f22-86f8-2fc0c7a3f785)
shows why visible initialization effects must be included in the boundary.

The [owned-package caller control](../integration/library_splits/lodash_sorted_uniq_test.mjs)
uses identical caller bytes that test for the fresh `sorted-uniq.mjs` helper:
the test returns `false` in the original tree and `true` in the emitted tree.
The route refuses that caller. This discriminator must be excluded explicitly
by a public-entry-only premise or made unobservable by a genuinely encapsulated
deployment; declaring the helper private does not itself hide the file.

## Deployment realization

A separate deployment obligation connects the graph theorem to a target
runtime. Every admitted public specifier and internal static edge must resolve
to the checked graph with the intended module instances, caching and effects.
Existing external dependencies must resolve with their preserved meaning and
identity. Format selection, package scope, resolution and instance identity
are runtime facts, not consequences of comparing two CoreJS graphs.

The CLI must distinguish checked facts from assumptions and facts guaranteed
by an enclosing bundler or deployer. It must identify the graph and client
context to which its claim applies. Copying a manifest and a caller's bytes
alone does not establish arbitrary Node caller behavior or deployment
realization. A graph receipt is not, by itself, a claim that Node will load it.
The owned-package route checks selected package and provider bytes and a narrow
default-import snapshot caller; those checks do not cover an arbitrary caller
or every runtime resolution fact.

The [measured format control](publication_receipt_boundary.md#reproduced-format-gap)
illustrates the separation. The default-snapshot route produces identical graph
receipts for byte-identical `isLength.js` split files under ESM and CommonJS
parent packages. Node imports the ESM tree and rejects the CommonJS tree; the
parent package scope was outside the checked artifact set. Even a client using
only the public entry therefore cannot infer Node loadability from that graph
receipt.

Before broadening input-side handling, specify which client boundary the CLI
exposes, what evidence supplies its deployment and client premises, and which
source and output graphs are in scope. This contract adds no global assumptions
or dynamic-loader design and does not widen any current constructor.

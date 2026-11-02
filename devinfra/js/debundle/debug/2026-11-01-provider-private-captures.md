# Private provider captures

The shared provider query can model a function-private `var` read without
claiming that the captured value is primitive, immutable, unaliased or private.
The canonical declaration visitor records a simple var's enclosing callable;
the query excludes module/import cells, lexical and destructured declarations,
parameter redeclarations, non-simple/default parameter scopes and dynamic scope.
The var binding exists and is initialized to undefined when its body scope is
entered. Source writes to its value do not turn the cell read into a getter.

Arguments and captures have distinct scoped symbolic inputs. Returned graph
traversal records captured capabilities, including through self-links. A
direct capture return has no borrowed-result contract yet and is refused.
Retained captures may alias actual arguments, so the returned opaque carrier
keeps all argument identities and a foreign capability. Later use preserves
possible importer reads, writes and heap mutation.

## Evidence and next consumer

This is source review and shared-query implementation, not a canonical program
acceptance result. No new tests or local test runs accompany the change.
Existing test constructors receive only the new empty retention field.

The declaration-only export collector remains unchanged. Property-origin
export inference is the next consumer of this query for nested callable keys:
it must prove the export's initialized callable value through its source
wrappers and descriptors. No real captured factory is claimed admitted here.
That query must preserve the provider initialization and captured-cell boundary;
this work grants no owner-completion or general purity certificate.

## Encountered tooling friction

**F8 — Keep foreign capability effects explicit:** following the old foreign
record marker reached a read-only importer promotion despite its global reach.
The marker now promotes possible rebinds and unsettles available prefix roots
as well. Capture retention also keeps possible argument aliases instead of
discarding arguments by parameter-use count. These are source-audit changes,
not a reproduced runtime failure. A future narrower carrier must retain the
actual captured roots and their read/write/heap effects; a set of capture names
alone cannot prove that opaque use is harmless.

**Addressed scope ambiguity:** the existing execution index's `current` is an
effect owner inherited by unindexed accessors and static blocks. It is not
always the declaring var environment. Capture evidence now uses a separate
lexical scope, cleared across those boundaries and restored for indexed nested
callables, so the parameter/TDZ guard checks the right callable.

**F9 — Clarify A11 versus native census guards:** design's A11 paragraph says
intrinsic pollution is not detected, while migrated consumers query a closed
source-write census and expose mutation witnesses. That wording obscures the
distinction between host/entry assumptions and additional source-proof guards.
The design now documents this distinction and requires a failed slot query to
retain its opaque fallback. The consumer-by-consumer inventory of unmigrated
spelling-based admissions remains follow-up work; the A11 heading must not
silently override a failed native-slot query.

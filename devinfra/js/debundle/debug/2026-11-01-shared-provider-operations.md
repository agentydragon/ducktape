# Shared provider operation execution

The ordered local-operation semantics now has application and strict-provider
consumers. Local instantiation, binding writes, sequences, field-reference
ordering, abrupt paths and return/throw semantics live in one executor. Literal
values and field stores use the shared allocation transfers and heap. The
application adapter supplies access/provenance and conservative publication;
the strict adapter refuses external bindings and unproved operations.

The provider projection can follow a returned record's self-links and nested
records to derive retained input arguments. This does not establish any real
program's provider identity or captured values. Source review and required
hooks are separate from hosted tests and canonical acceptance. No new tests or
local test runs accompany this slice.

## Remaining source-summary boundary

- **F6 — Explain provider identity and capture refusal:** the candidate
  collector currently accepts exported function/class declarations and direct
  function/arrow initializers. An exported alias initialized from an object's
  property never reaches this query. Add bounded candidate/refusal reporting
  with the initializer span and missing identity/capture obligation. Derive
  stable property identity only with the descriptor, initialization and
  mutation/escape proof at the actual export read; a matching property name or
  a pure body is insufficient. Captured primitives also need source-backed
  binding and initialization proof before entering the strict frame.

The earlier allocation-generation limitation remains: application execution
cannot identify a newly created record by its allocation site alone when that
site already denotes older objects. The strict query starts with a new private
heap and symbolic inputs; this supplies no generation proof to application
execution or returned opaque carriers.

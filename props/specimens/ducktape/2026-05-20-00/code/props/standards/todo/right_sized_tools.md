# Property: Right-Sized Tools

**Status:** Planned
**Kind:** behavior

## Predicate

Prefer the simplest construct that meets the requirement; avoid heavier mechanisms when a direct, simpler reference works (e.g., pass callables/constants instead of string→getattr→dict lookups; use library calls over subprocess when available; choose sync vs async and blocking vs non-blocking appropriately; pass Path directly instead of str conversions).

## Acceptance Criteria (Draft)

- No string→symbol indirection when a direct symbol can be passed
- No classes-as-namespaces for constants; in Python 'class' implies instances; prefer modules/enums/simple names (use a class only when you intend to instantiate)
- Prefer direct library APIs over shelling out (unless required by constraints)
- Choose appropriately between sync/async, blocking/non-blocking based on actual needs
- Pass Path objects directly instead of converting to strings and back

## Examples Needed

- [ ] Positive: Direct callable passing
- [ ] Negative: String→getattr lookup when direct reference works
- [ ] Negative: Class used as namespace for constants
- [ ] Positive: Direct library API usage
- [ ] Negative: Subprocess call when library API available

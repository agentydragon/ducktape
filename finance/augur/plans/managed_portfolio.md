# Paired TLH portfolio study

The [roadmap](roadmap.md) owns dispatch. **MA3** is a runnable paired
harvesting/no-harvest comparison using the existing Python `TlhPortfolio` and
common action session. Reuse [the TLH component](../sim/tlh.py), not a second
managed-account abstraction or financial executor.

## Remaining acceptance

- Use identical supplied paths and external investor flows for harvesting and
  no-harvest controls, with explicit contribution/withdrawal decisions.
- Reconcile cash, realized ST/LT gains and remaining value/basis against
  independent expected financial facts. Keep component value counted once.
- Report typed compact outcomes and verify selected replay on the same original
  path IDs, preserving stopped-month facts and unobserved future months.
- Run the documented CLI with generated financial inputs in Bazel CI. Label
  these accounting controls separately from calibration evidence.

This study does not wait for the app's housing/PE branches, unrelated configured
reader retirement or performance work. BIND/TAX gate expanded product/statutory
claims, not clearly labeled synthetic controls.

Empirical calibration, wash sales, fees and constituent modeling remain
[future research](future_work.md#reduced-form-tlh-portfolios), not prerequisites
for the paired accounting control.

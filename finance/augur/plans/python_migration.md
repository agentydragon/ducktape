# Python cutover validation

All Augur financial execution is now Python-owned. The native source, extension,
binding stubs and redundant private transport have been removed. Current ownership
is documented in [the simulator design](../sim/DESIGN.md); this remaining checklist
is validation work, not a second implementation roadmap.

The [native test mapping](native_test_mapping.md) records independently checked
Python counterparts for all 95 native declarations. Existing Python financial
acceptance suites are retained under `sim/testing/`, with public artifact helpers
in `sim/artifacts.py`. No native fallback or alternate evaluator remains.

- [ ] Complete the full Augur product/consumer and financial test matrix after removal.
- [ ] Verify generated dependencies, native-free build graph and final hosted CI.
- [ ] Retire this final validation checklist after the cutover is accepted.

Preserve exact money/basis, rejection atomicity, successful prefixes, stopped-path
validity and financial phase ordering. The language cutover does not complete the
separate APP/P12 configured-policy/housing/PE work or add new financial features.
There is no speedup or large-N performance gate. Future measured optimization must
preserve one canonical domain model rather than reintroducing duplicate evaluators.

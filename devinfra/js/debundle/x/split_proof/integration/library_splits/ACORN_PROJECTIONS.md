# Acorn declaration projections

[The public Acorn bundle](prepare.mjs) has 447 units and SHA-256
`e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee`.
The projection test copies complete AST-selected declarations verbatim and
adds an observation export. These are three distinct requests on excerpts;
they do not count as full Acorn placements or certify any other original units.

| Request                    | Original units            | Placement                                                                                       | Baseline `d10843cd` | Initialized `var` [#20602](https://github.com/agentydragon/ducktape/pull/20602) at `518c09de` |
| -------------------------- | ------------------------- | ----------------------------------------------------------------------------------------------- | ------------------- | --------------------------------------------------------------------------------------------- |
| Literal `var` declarations | #2–3, two Unicode strings | Keep the original halves owner (`m0.mjs`) and relative order; observation in `m1.mjs`           | Refused             | Certified, `all-outcomes`                                                                     |
| Dense array declarations   | #0–1, two numeric tables  | Same halves owners and order                                                                    | Refused             | Refused                                                                                       |
| Dense array declarations   | Same #0–1                 | Advance #1 to `moved.mjs`; retain #0 in the entry, as in the full-bundle advance-second request | Refused             | Refused                                                                                       |

The baseline [run](https://app.buildbuddy.io/invocation/2095bb3c-ed06-4783-9960-9f879af800a9)
certified 0/3. The same projection test applied to the
[initialized-var head](https://app.buildbuddy.io/invocation/bd51062f-e021-4116-aa18-62cacd7ab32b)
certified 1/3. The result used the compiled constructor. Both files passed disk
readback, and Node agreed on the one-name observation namespace and the
931-code-unit string. Each refusal returned `no_constructed_rule` and left its
output directory absent. The test writes excerpts, placements and verdicts as
artifacts, including hashes for both the original bundle and each projection.

The Node array observation checks distinct object identity and that writing the
first table leaves the second table unchanged. A separate negative control adds
an eager read of the first Unicode string. The two orders both complete, but
the advanced read captures `undefined` rather than the initialized string.
The ordinary CLI refuses that request without publishing files on either
revision. This control is outside the three-projection denominator.

The full Acorn and semver ladder remained 0/5 on the
[initialized-var head](https://app.buildbuddy.io/invocation/f435bc3b-7d15-4375-b15c-1b3fca56f8d9).
A useful next constructor check is ordinary, order-preserving dense-array
allocation over these two verbatim Acorn declarations; proving the crossing is
a separate step. The real full Acorn chunk still encounters an ambient
`RegExp` during source classification. This test does not add a host contract.

The diagnostic is independently reviewable against `devel`. Only the
comparison run depends on #20602
(`518c09de0b944de4f96190eb63b16c866d00ff1e`); the test works on either revision.
Use the explicit assertion to reproduce the corresponding observation:

```sh
p=//devinfra/js/debundle/x/split_proof/integration/library_splits
bbr test "$p:acorn_projection" --test_env=EXPECT_LITERAL_VARS=refused --test_output=all
# Use EXPECT_LITERAL_VARS=certified on the initialized-var comparison branch.
```

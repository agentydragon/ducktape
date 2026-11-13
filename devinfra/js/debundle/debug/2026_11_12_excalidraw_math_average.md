# Excalidraw math: `average` extraction

At engine `2c5040e7e6`, the public Excalidraw 0.18.1 corpus's 32 original
`packages/math/{point,vector,angle,utils}` bindings all resolve uniquely in joint
source-only validation, including selectors disambiguated through their callers.
The full assignment rejects only when `average` is included in the `utils` owner.
With that one binding left in the editor entry, the other 31 pass together.

To reproduce, add `average` to the existing `bindings` list in
`excalidraw/spec/modules/editor/packages/math/utils.yaml` and build
`//devinfra/js/debundle/excalidraw:debundle`. Its template already matches the
upstream function; no source or selector change is needed.

The ordinary gate reports an 82-unit atomic group crossing the residual entry
and `packages/math/utils`. One cut is from `unit:485` (`rr`, the upstream
`average` arrow initializer) to `unit:19` (`Ml`): `sequenced`,
`binding_timeline`, `writer_after_reader`, with the reader attributed to
`opaque_callback_invocation`. The residual endpoint calls `ra` during bundled
vendor initialization. The diagnostics include further unknown calls and
Array/typed-array constructors; these are opacity evidence, not proof of an
actual call to `average` during that initialization.

Before changing the analysis or asserting a contract, trace the reciprocal
edge and callback publication path and reduce the same rejection to a neutral
CLI fixture. Determine whether the current host assumptions allow that
reentry, or whether the callback pool attributes a read of this unpublished
binding without a realizable call path. Re-extract `average` into its original
module only after the normal full gate and emitted runtime checks pass.

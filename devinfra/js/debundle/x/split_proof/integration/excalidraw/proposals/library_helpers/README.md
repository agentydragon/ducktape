# Additional editor selector fragments

These declarations belong to the pinned Excalidraw v0.18.1 sources:

- [`average`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/math/utils.ts) shares a declaration list with the other math utilities. `getSvgPathFromStroke` captures its private cell.
- [`RS_LTR_CHARS` and `RS_RTL_CHARS`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/utils.ts) are the string inputs to the RTL pattern. Selecting the strings leaves `RE_RTL_CHECK` and `isRTL` in the editor entry.
- [`SHIFT_LOCKING_ANGLE`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/constants.ts) reads `Math.PI / 12`. The proof assumes that throughout initialization the ambient binding returns the same Math object and its own PI data property returns the expected Number without observable effects. An installation that replaces Math can violate this premise.

The Rust resolver appends these claims to their canonical owners before the
joint selector solve. Lean derives the module links using the resulting owners
and authored import priority. The ordinary Rust emitter uses its own accepted
spec view; nothing in this test depends on its acceptance or emitted schedule.

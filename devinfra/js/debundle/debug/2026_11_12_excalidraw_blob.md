# Excalidraw Blob/File extraction boundary

The public Excalidraw 0.18.1 corpus resolves 25 runtime bindings from upstream
`packages/excalidraw/data/blob.ts` with structural source selectors. Assigning
all 25 to their original home rejects; leaving `getFileHandleType`,
`isImageFileHandle`, `isSupportedImageFileType` and `createFile` in the editor
entry permits the other 21 to pass the complete gate. `getMimeType` has no
matching declaration in this tree-shaken input and is not claimed.

To reproduce, extend `excalidraw/spec/modules/editor/packages/excalidraw/data/blob.yaml`
with structural selectors for those four upstream declarations and build
`//devinfra/js/debundle/excalidraw:debundle`. The source-only selectors resolve
uniquely; the rejection is in module assignment, not matching.

The atomic-group report contains a 410-unit group spanning the residual entry
and those four selected bindings. One witness reaches `getFileHandleType`
(`unit:1752`, `Bm`) from `unit:675` through `unit:1206` and `unit:1689`
(`validateIndicesThrottled`). Its return path uses a `binding_timeline`,
`writer_after_reader` edge to `unit:1517`, attributed to a `possible_getter`
with lazy owner `unit:1754` (`isImageFileHandle`). The path continues through
`unit:1132`, whose return to `unit:675` cites `callback_pool_limit` (64,
pool size 316). These are analysis witnesses, not proof that initialization
actually invokes the file helpers.

Before changing analysis or asserting an initialization contract, trace that
getter/callback publication path and reduce the rejection to a neutral CLI
fixture. Preserve the original module home; copying the entire residual group
there would obscure ownership. Reclaim the four functions only when the full
gate and emitted browser behavior pass together.

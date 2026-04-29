# Human-likeness gap inventory (Excalidraw semi-recovered reference)

## Reasonably fixable in debundler automation

1. Stage init wrappers still dominate each recovered module and could be collapsed into top-level statements where ordering allows.
2. Generated init symbol names (`__dt_generated_init__...`) remain noisy and can be hidden or canonicalized.
3. Some alias/bridge assignments can be inlined now that const-promotion exists.

## Not realistically fixable by deterministic debundler transforms alone

1. Minified variable names require semantic inference to rename robustly.
2. Recovering exact original source-level module boundaries/API naming needs intent inference beyond structural analysis.


## Recent improvements

- Const-promotion of single-assignment literal/object/array initializers reduces some init-wrapper noise, but TDZ-sensitive class/function cases still require wrapper retention for safety.

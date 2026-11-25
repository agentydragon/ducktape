@README.md

Structure the debundled Excalidraw code to mirror the pinned upstream source:
recover its module paths, boundaries, and names as closely as the bundled code
and the correctness gate allow. Ground assignments in the upstream source;
leave code in the residual when a corresponding boundary cannot yet pass.

Start ownership research by reading the [pinned Excalidraw source tree](https://github.com/excalidraw/excalidraw/tree/v0.18.1/packages).
Find the declaration and its uses there. Record its upstream path and name
alongside a structural selector that resolves uniquely in the production
bundle. For example, compare the four direction vectors and the angle
branches in `packages/excalidraw/element/heading.ts` with the bundled
declarations. A development source map is optional discovery data; neither
authoring the spec nor certifying a placement requires it. If the production
match is ambiguous, keep the unit in the entry until there is a better selector.

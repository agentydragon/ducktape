// Ambient types for the @tabler/icons-react per-icon subpath imports used by icons.tsx. The
// `.mjs` subpaths resolve to real but untyped files, so each needs an *exact* module
// declaration. Kept as a global script (no top-level import/export — the React imports live
// INSIDE each block) so the declarations register globally. The subpath form is mandatory:
// the `@tabler/icons-react` barrel OOMs esbuild on RBE (~8.7 GB; debug/esbuild_tabler_memory.md).
// Add a block here for each icon imported.
declare module "@tabler/icons-react/dist/esm/icons/IconMenu2.mjs" {
  import type { FC, SVGProps } from "react";
  const Icon: FC<SVGProps<SVGSVGElement> & { size?: number | string; stroke?: number | string }>;
  export default Icon;
}
declare module "@tabler/icons-react/dist/esm/icons/IconHistory.mjs" {
  import type { FC, SVGProps } from "react";
  const Icon: FC<SVGProps<SVGSVGElement> & { size?: number | string; stroke?: number | string }>;
  export default Icon;
}
declare module "@tabler/icons-react/dist/esm/icons/IconArrowLeft.mjs" {
  import type { FC, SVGProps } from "react";
  const Icon: FC<SVGProps<SVGSVGElement> & { size?: number | string; stroke?: number | string }>;
  export default Icon;
}

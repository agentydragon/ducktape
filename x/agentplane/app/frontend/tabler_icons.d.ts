// Ambient types for the @tabler/icons-react per-icon subpath imports sandboxes.tsx uses. The `.mjs`
// subpaths resolve to real but untyped files, so they need a module declaration; the wildcard covers
// every icon under the subpath directory, so adding an icon needs no edit here. The subpath form is
// mandatory — the `@tabler/icons-react` barrel OOMs esbuild on RBE (~8.7 GB) — and the pattern
// deliberately does not match it.
//
// Must stay a global script (no top-level import/export — the React import lives INSIDE the block)
// so the declaration registers globally; a top-level import would make this a module and the
// `declare module` an augmentation of a module that has no types to augment.
//
// Gotcha: a wildcard declaration types any matching specifier, so a misspelled icon name is not a
// tsc error — esbuild reports it as an unresolved import at bundle time instead.
declare module "@tabler/icons-react/dist/esm/icons/*.mjs" {
  import type { FC, SVGProps } from "react";

  const Icon: FC<SVGProps<SVGSVGElement> & { size?: number | string; stroke?: number | string }>;
  export default Icon;
}

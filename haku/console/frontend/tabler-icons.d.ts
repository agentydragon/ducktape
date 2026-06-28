// Ambient types for deep per-icon imports from @tabler/icons-react.
//
// The package ships only barrel types (dist/tabler-icons-react.d.ts) — there are no
// .d.mts files for the per-icon modules under dist/esm/icons/. But the barrel makes
// esbuild OOM tree-shaking it (~8.7 GB peak with the full node_modules tree, and no
// clean per-action RAM lever exists — see debug/esbuild_tabler_memory.md), so icons
// are imported by subpath (default export). This gives those imports accurate types:
// a forward-ref SVG component accepting the Tabler size/stroke/title props (matching
// the barrel's IconProps).
declare module "@tabler/icons-react/dist/esm/icons/IconMessage2.mjs" {
  import type { ForwardRefExoticComponent, RefAttributes, SVGProps } from "react";

  const IconMessage2: ForwardRefExoticComponent<
    SVGProps<SVGSVGElement> & {
      size?: string | number;
      stroke?: string | number;
      title?: string;
    } & RefAttributes<SVGSVGElement>
  >;
  export default IconMessage2;
}

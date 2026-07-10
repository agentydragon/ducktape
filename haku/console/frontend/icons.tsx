// Inline SVG icons — deliberately NOT `@tabler/icons-react`: its barrel OOMs esbuild on
// RBE (~8.7 GB peak) and even a per-icon subpath import needs an ambient declaration.
// See debug/esbuild_tabler_memory.md → "Inline SVG". Each glyph strokes `currentColor`
// so it inherits the button/text color and both color schemes.
import type { SVGProps } from "react";

function Glyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    />
  );
}

/** Hamburger — the shell's console-panel toggle. */
export function MenuIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Glyph {...props}>
      <line x1="4" y1="6" x2="20" y2="6" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <line x1="4" y1="18" x2="20" y2="18" />
    </Glyph>
  );
}

/** Clock-with-rewind — links to the past-tool-calls history view. */
export function HistoryIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Glyph {...props}>
      <path d="M3 3v5h5" />
      <path d="M3.05 13A9 9 0 1 0 6 5.3L3 8" />
      <path d="M12 7v5l3 2" />
    </Glyph>
  );
}

/** Left arrow — the history view's back-to-embed control. */
export function ArrowLeftIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Glyph {...props}>
      <line x1="19" y1="12" x2="5" y2="12" />
      <polyline points="12 19 5 12 12 5" />
    </Glyph>
  );
}

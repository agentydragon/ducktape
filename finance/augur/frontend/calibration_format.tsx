// Shared calibration formatting + the platform badge, used by the scored/surfaced market
// tables and the categorical chart panel. Kept in one place so those panels (which live in
// separate files) don't each re-derive probability/KL formatting or duplicate the brand icons.

import React from "react";

import { fmtPct } from "./lib/format.ts";

export function fmtProb(value) {
  return value == null || !Number.isFinite(Number(value)) ? "n/a" : fmtPct(value);
}

// `D_KL(market ‖ model)`: 0 = model matches the market, larger = louder disagreement. Pass
// `withUnit` for standalone figures (e.g. a categorical family's overall KL) that have no column
// header to carry the unit; the scored-markets table omits it because its "KL (bits)" header does.
export function fmtKl(value, { withUnit = false } = {}) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const formatted = Number(value).toFixed(3);
  return withUnit ? `${formatted} bits` : formatted;
}

// Bigger model-vs-market divergences get a louder tint on the KL cell itself so the eye lands on
// the disagreements first. The full row stays untinted so platform-logo and link contrast doesn't
// have to fight a rose/amber background.
export function klTextClass(klBits) {
  if (klBits == null || !Number.isFinite(Number(klBits))) return "augur-muted";
  if (klBits >= 0.15) return "font-semibold text-rose-700 dark:text-rose-300";
  if (klBits >= 0.05) return "font-semibold text-amber-700 dark:text-amber-300";
  return "augur-tabular";
}

// Each platform's actual brand mark, trimmed to the icon glyph only:
//   - manifold: the official "crane" logo from manifold.markets/logo.svg (indigo #4337C9 on
//     light) and logo-white.svg (white on dark); same path either way, only stroke color flips.
//   - polymarket: the trapezoidal icon extracted from the Wikimedia logo SVG
//     (dropping the "polymarket" wordmark that sits beside it).
//   - kalshi: the lowercase "k" subpath extracted from the Wikimedia wordmark
//     (Kalshi only ships a wordmark, so this is the most icon-like fragment).
// Each viewBox is square so the three icons rendered at h-5 w-5 occupy the same physical
// footprint; `stroke|fill="currentColor"` (where used) lets the wrapper drive the color so
// dark-mode swaps stay declarative.
const PLATFORM_ICON = {
  manifold: (
    <svg
      viewBox="0 0 24 24"
      className="h-5 w-5 shrink-0 text-[#4337C9] dark:text-white"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M5.24854 17.0952L18.7175 6.80301L14.3444 20M5.24854 17.0952L9.79649 18.5476M5.24854 17.0952L4.27398 6.52755M14.3444 20L9.79649 18.5476M14.3444 20L22 12.638L16.3935 13.8147M9.79649 18.5476L12.3953 15.0668M4.27398 6.52755L10.0714 13.389M4.27398 6.52755L2 9.0818L4.47389 8.85643M12.9451 11.1603L10.971 5L8.65369 11.6611" />
    </svg>
  ),
  polymarket: (
    // Pad the 137x168 icon out to a 168x168 square so it centers in the same w-4 box as the
    // other two; shift x by -15.5 (= (168-137)/2) so the trapezoid sits in the middle.
    <svg
      viewBox="-15.5 0 168 168"
      className="h-5 w-5 shrink-0 text-slate-800 dark:text-slate-200"
      fill="currentColor"
      aria-hidden
    >
      <path d="M136.267 152.495C136.267 159.76 136.267 163.392 133.891 165.192C131.516 166.993 128.019 166.012 121.024 164.049L8.63192 132.51C4.41793 131.328 2.31093 130.737 1.09248 129.129C-0.125977 127.522 -0.125977 125.333 -0.125977 120.957V47.0434C-0.125977 42.6667 -0.125977 40.4783 1.09248 38.8709C2.31093 37.2634 4.41792 36.6722 8.63191 35.4897L121.024 3.95096C128.019 1.98834 131.516 1.00703 133.891 2.80771C136.267 4.60839 136.267 8.24049 136.267 15.5047V152.495ZM27.9043 122.228L120.966 148.345V96.1133L27.9043 122.228ZM15.1738 110.111L108.217 84L15.1738 57.8887V110.111ZM27.9033 45.7725L120.966 71.8877V19.6553L27.9033 45.7725Z" />
    </svg>
  ),
  kalshi: (
    // Pad the 180x226 "k" out to a 226x226 square so it centers in the same w-4 box; shift x
    // by -23 (= (226-180)/2). Brand green #00DD94 reads on both light and dark backgrounds.
    <svg viewBox="-23 0 226 226" className="h-5 w-5 shrink-0" fill="#00DD94" aria-hidden>
      <path d="M105.23 105.628L179.66 222.61H115.118L54.3009 121.934V222.61H0V3.38607H54.3009V99.102L119.464 3.38607H177.489L105.23 105.628Z" />
    </svg>
  ),
};

export function PlatformBadge({ platform }) {
  const icon = PLATFORM_ICON[platform] ?? PLATFORM_ICON.manifold;
  // Centered inside the dedicated platform column; the parent <td> handles spacing.
  return (
    <span className="inline-flex items-center justify-center" title={platform}>
      {icon}
    </span>
  );
}

/** Browser adapters for the shared runtime-neutral pace implementation. */

import {
  bindingTint as sharedBindingTint,
  computePace as sharedComputePace,
  elapsedFraction as sharedElapsedFraction,
  isExhausted as sharedIsExhausted,
  tintFor as sharedTintFor,
} from "../shared/presentation.js";
import type { Pace, Tint, WindowMath } from "../shared/presentation.js";

export type { Pace, Tint, WindowMath };

export function isExhausted(window: WindowMath): boolean {
  return sharedIsExhausted(window);
}

export function elapsedFraction(window: WindowMath): number {
  return sharedElapsedFraction(window);
}

export function computePace(window: WindowMath): Pace {
  return sharedComputePace(window);
}

export function tintFor(pace: Pace | null, usedPercent: number, { isShort }: { isShort: boolean }): Tint {
  return sharedTintFor(pace, usedPercent, isShort);
}

export function bindingTint(tints: Tint[]): Tint {
  return sharedBindingTint(tints);
}

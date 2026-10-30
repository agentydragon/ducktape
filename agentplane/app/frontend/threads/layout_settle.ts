/** A layout counts as at rest once this many frames pass without it changing. */
const QUIET_FRAMES = 5;

/**
 * Whether a layout has come to rest. `changed()` says it moved; it is unsettled until it has
 * stayed put for `QUIET_FRAMES` frames, and while `busy()` says something that will move it is still
 * under way: a correction, or content a row is waiting for.
 */
export class LayoutSettle {
  readonly #busy: () => boolean;
  readonly #onChange: (settled: boolean) => void;
  #settled = false;
  #frame: number | null = null;

  constructor(busy: () => boolean, onChange: (settled: boolean) => void) {
    this.#busy = busy;
    this.#onChange = onChange;
    this.#wait(0);
  }

  changed(): void {
    if (this.#settled) {
      this.#settled = false;
      this.#onChange(false);
    }
    if (this.#frame !== null) cancelAnimationFrame(this.#frame);
    this.#wait(0);
  }

  dispose(): void {
    if (this.#frame !== null) cancelAnimationFrame(this.#frame);
    this.#frame = null;
  }

  #wait(quiet: number): void {
    this.#frame = requestAnimationFrame(() => {
      if (this.#busy()) return this.#wait(0);
      if (quiet + 1 < QUIET_FRAMES) return this.#wait(quiet + 1);
      this.#frame = null;
      this.#settled = true;
      this.#onChange(true);
    });
  }
}

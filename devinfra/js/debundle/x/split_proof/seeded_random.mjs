/** Mulberry32 draws in [0, 1). Keep the sequence stable for saved fuzz seeds. */
export function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = Math.imul(state ^ (state >>> 15), state | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 2 ** 32;
  };
}

/** Linear congruential draws as unsigned 32-bit integers. */
export function lcg32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state;
  };
}

export function seededRandom(seed) {
  const float = mulberry32(seed);
  const int = (n) => Math.floor(float() * n);
  return {
    float,
    int,
    chance: (p) => float() < p,
    pick: (xs) => xs[int(xs.length)],
    shuffle: (xs) => {
      const out = [...xs];
      for (let i = out.length - 1; i > 0; i--) {
        const j = int(i + 1);
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    },
  };
}

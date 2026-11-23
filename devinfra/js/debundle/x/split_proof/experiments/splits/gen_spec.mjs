// Split specs for a bundle of n top-level statements, selecting statements by position (`#i`).
//
//   node gen_spec.mjs <n> halves
//   node gen_spec.mjs <n> cut <c> [entry-first]   two modules: statements [0, c) and [c, n); the entry is the second unless told otherwise
//   node gen_spec.mjs <n> blocks <k>          k contiguous blocks of about n/k statements
//   node gen_spec.mjs <n> random <k> <seed>   every statement to a uniformly random one of k modules
//
// The entry is the module that holds the last statement, where esbuild puts the bundle's `export { … }`.
// A module name is `m<j>.mjs`. Modules nobody was assigned are left out.

import { mulberry32 } from "../../seeded_random.mjs";

export function assignment(n, strategy, args = []) {
  const a = new Array(n);
  if (strategy === "halves") return assignment(n, "cut", [Math.floor(n / 2)]);
  if (strategy === "cut") {
    const c = Number(args[0]);
    for (let i = 0; i < n; i++) a[i] = i < c ? 0 : 1;
  } else if (strategy === "blocks") {
    const per = Math.ceil(n / Number(args[0]));
    for (let i = 0; i < n; i++) a[i] = Math.floor(i / per);
  } else if (strategy === "random") {
    const k = Number(args[0]);
    const r = mulberry32(Number(args[1]));
    for (let i = 0; i < n; i++) a[i] = Math.floor(r() * k);
  } else {
    throw new Error(`unknown strategy ${strategy}`);
  }
  return a;
}

export function spec(n, strategy, args = []) {
  const a = assignment(n, strategy, args);
  const modules = {};
  a.forEach((m, i) => (modules[`m${m}.mjs`] ??= []).push(`#${i}`));
  const entryFirst = strategy === "cut" && args[1] === "entry-first";
  return { entry: entryFirst ? "m0.mjs" : `m${a[n - 1]}.mjs`, modules };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [n, strategy, ...args] = process.argv.slice(2);
  process.stdout.write(JSON.stringify(spec(Number(n), strategy, args)));
}

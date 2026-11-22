// Seeded, bounded coverage for rule families that the general tree generator rarely composes. Each generated program is
// still checked by effects/run.mjs: lower classifies every unit, translate checks the reordered tree, and Node supplies
// the observable behavior oracle.
const noWorldHooks = { noWorldHooks: true };

export const requiredFamilies = {
  iterator_consumption: 4,
  prototype_depth: 4,
  flat_depth: 4,
  recursive_hooks: 4,
  uncaught_throw: 3,
};

function random(seed) {
  let state = seed >>> 0;
  const float = () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = Math.imul(state ^ (state >>> 15), state | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return {
    chance: (p) => float() < p,
    int: (n) => Math.floor(float() * n),
    shuffle: (values) => {
      const out = [...values];
      for (let i = out.length - 1; i > 0; i--) {
        const j = Math.floor(float() * (i + 1));
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    },
  };
}

const pure = (code) => ({ code, expect: "pure" });
const world = (code) => ({ code, expect: "world" });
const writes = (code, name) => ({ code, expect: `writes ${name}` });

function swapOrder(length, left, right) {
  const order = [...Array(length).keys()];
  [order[left], order[right]] = [order[right], order[left]];
  return order;
}

function generatedCase(family, name, lines, effectIndex, observedIndex, positiveAnchor = true) {
  const reorders = [
    {
      order: swapOrder(lines.length, effectIndex, observedIndex),
      behavior: "differs",
      stdoutDiffers: true,
    },
  ];
  if (positiveAnchor) {
    reorders.push({
      order: [1, 0, ...[...Array(lines.length - 2).keys()].map((i) => i + 2)],
      behavior: "same",
      accepted: true,
    });
  }
  return {
    name,
    family,
    premises: noWorldHooks,
    source: `${lines.map((line) => line.code).join("\n")}\n`,
    expect: lines.map((line) => line.expect),
    reorders,
  };
}

function makeIteratorCases(rng) {
  const consumers = rng.shuffle(["toArray", "forOf", "some", "reduce"]);
  return consumers.map((consumer, index) => {
    const hasAlias = rng.chance(0.5);
    const inputName = hasAlias ? "iteratorAlias" : "iterator";
    const delta = rng.int(3) + 1;
    const lines = [
      pure("const independentA = 13;"),
      pure("const independentB = 29;"),
      pure("const hits = { n: 0 };"),
      pure("const source = new Set([1, 2, 3]).values();"),
      pure("const iterator = source;"),
    ];
    if (hasAlias) lines.push(pure("const iteratorAlias = iterator;"));
    lines.push(world(`const mapped = ${inputName}.map((value) => { hits.n += value; return value + ${delta}; });`));
    if (consumer === "forOf") lines.push(pure("let outcome = 0;"));
    const consumeIndex = lines.length;
    if (consumer === "toArray") lines.push(world('const outcome = mapped.toArray().join(",");'));
    else if (consumer === "forOf") lines.push(world("for (const value of mapped) outcome += value;"));
    else if (consumer === "some") lines.push(world("const outcome = mapped.some((value) => value > 3);"));
    else lines.push(world("const outcome = mapped.reduce((sum, value) => sum + value, 0);"));
    const observedIndex = lines.length;
    lines.push(pure("const observed = hits.n;"), world("console.log(observed, outcome);"));
    return generatedCase(
      "iterator_consumption",
      `seeded iterator helper ${consumer} consumer ${index}`,
      lines,
      consumeIndex,
      observedIndex
    );
  });
}

function makePrototypeCases(rng) {
  return rng.shuffle([9, 10, 11, 12]).map((depth) => {
    const lines = [
      pure("const independentA = 17;"),
      pure("const independentB = 31;"),
      pure("const hits = { n: 0 };"),
      pure(`const base = { get value() { hits.n += 1; return ${depth}; } };`),
    ];
    for (let i = 1; i <= depth; i++) {
      const parent = i === 1 ? "base" : `p${i - 1}`;
      // Object.create records which later aliases can reach this prototype. The final ten-object walk stays precise;
      // a longer chain reaches the analyzer's conservative give-up path.
      const inheritedWrites = [...Array(depth - i).keys()].map((j) => `p${i + j + 1}`);
      if (i === 1) inheritedWrites.push("hits");
      inheritedWrites.sort();
      const expected = i >= 10 ? "world" : inheritedWrites.length ? `writes ${inheritedWrites.join(" ")}` : "pure";
      lines.push({ code: `const p${i} = Object.create(${parent});`, expect: expected });
    }
    const readIndex = lines.length;
    // The getter is present at depth 9; beyond the ten-object walk the analyzer must conservatively give up.
    lines.push(
      depth === 9 ? writes(`const found = p${depth}.value;`, "hits") : world(`const found = p${depth}.value;`)
    );
    const observedIndex = lines.length;
    lines.push(pure("const observed = hits.n;"), world("console.log(observed, found);"));
    return generatedCase("prototype_depth", `seeded inherited getter depth ${depth}`, lines, readIndex, observedIndex);
  });
}

function makeFlatCases(rng) {
  return rng.shuffle([6, 7, 8, 9]).map((depth) => {
    const useAlias = rng.chance(0.5);
    const nestedName = useAlias ? "element" : "leaf";
    let nested = nestedName;
    for (let i = 0; i < depth; i++) nested = `[${nested}]`;
    const lines = [pure("const independentA = 19;"), pure("const independentB = 37;"), pure("const leaf = { n: 0 };")];
    if (useAlias) lines.push(pure("const element = leaf;"));
    lines.push(pure(`const nested = ${nested};`), pure("const flattened = nested.flat(Infinity);"));
    const writeIndex = lines.length;
    // Indexed access on the deep flattened result is summarized conservatively as unknown code.
    lines.push(world("flattened[0].n = 1;"));
    const observedIndex = lines.length;
    lines.push(pure("const observed = leaf.n;"), world("console.log(observed);"));
    return generatedCase("flat_depth", `seeded flat(Infinity) nesting ${depth}`, lines, writeIndex, observedIndex);
  });
}

function makeRecursiveHookCases(rng) {
  const depths = rng.shuffle([7, 8, 9, 10]);
  const shapes = rng.shuffle(["primitive", "primitive", "array", "array"]);
  return depths.map((depth, index) => {
    const shape = shapes[index];
    const alias = rng.chance(0.5);
    const lines = [
      pure("const independentA = 23;"),
      pure("const independentB = 41;"),
      pure("const hits = { n: 0 };"),
      pure(shape === "array" ? "const value = [];" : "const value = {};"),
    ];
    if (alias) lines.push(pure("const alias = value;"));
    const recursiveValue = alias ? "alias" : "value";
    if (shape === "array") {
      lines.push(
        writes("value.push(value);", alias ? "alias value" : "value"),
        writes(
          `value.toString = function () { hits.n += 1; if (hits.n < ${depth}) return String(${recursiveValue}); return "done"; };`,
          alias ? "alias value" : "value"
        )
      );
    } else {
      lines.push(
        world(
          `value[Symbol.toPrimitive] = function () { hits.n += 1; if (hits.n < ${depth}) return +${recursiveValue}; return 1; };`
        )
      );
    }
    const convertIndex = lines.length;
    lines.push(shape === "array" ? world("const converted = String(value);") : world("const converted = +value;"));
    const observedIndex = lines.length;
    lines.push(pure("const observed = hits.n;"), world("console.log(observed, converted);"));
    return generatedCase(
      "recursive_hooks",
      `seeded recursive ${shape} hook depth ${depth}`,
      lines,
      convertIndex,
      observedIndex
    );
  });
}

function makeThrowCases() {
  const direct = [
    pure("const independentA = 7;"),
    pure("const independentB = 11;"),
    world('console.log("before direct throw");'),
    world('throw new Error("generated direct stop");'),
    world('console.log("after direct throw");'),
  ];
  const helper = [
    pure("const independentA = 7;"),
    pure("const independentB = 11;"),
    pure('function stop() { throw new Error("generated helper stop"); }'),
    world('console.log("before helper throw");'),
    world("stop();"),
    world('console.log("after helper throw");'),
  ];
  const callback = [
    pure("const independentA = 7;"),
    pure("const independentB = 11;"),
    pure("const iterator = new Set([1]).values();"),
    world('console.log("before callback throw");'),
    world('iterator.forEach(() => { throw new Error("generated callback stop"); });'),
    world('console.log("after callback throw");'),
  ];
  return [
    generatedCase("uncaught_throw", "seeded direct uncaught throw", direct, 3, 2, false),
    generatedCase("uncaught_throw", "seeded helper uncaught throw", helper, 4, 3, false),
    generatedCase("uncaught_throw", "seeded iterator callback uncaught throw", callback, 4, 3, false),
  ];
}

export function generateCases(seed) {
  const rng = random(seed);
  return [
    ...makeIteratorCases(rng),
    ...makePrototypeCases(rng),
    ...makeFlatCases(rng),
    ...makeRecursiveHookCases(rng),
    ...makeThrowCases(),
  ].map((c) => ({ ...c, name: `seed ${seed}: ${c.name}` }));
}

import { rng } from "./gen/core.mjs";

const operations = [
  { method: "get", property: "accessor" },
  { method: "set", property: "accessor" },
  { method: "set", property: "data" },
];
const familyOf = (method, property, receiver, inherited) =>
  [method, property, receiver ? "explicit" : "default", inherited ? "inherited" : "own"].join("/");

const shapes = operations.flatMap(({ method, property }) =>
  [false, true].flatMap((receiver) => [false, true].map((inherited) => ({ method, property, receiver, inherited })))
);
export const requiredFamilies = Object.fromEntries(
  shapes.map(({ method, property, receiver, inherited }) => [familyOf(method, property, receiver, inherited), 1])
);

function makeCase(random, seed, { method, property, receiver, inherited }) {
  const pick = (values) => values[random.int(values.length)];
  const key = pick(["slot", "mark", "item"]);
  const state = pick(["tally", "amount", "total"]);
  const initial = random.int(5);
  const delta = random.int(7) + 1;
  const lines = ["const first = 37;", "const second = 13;"];
  if (receiver) lines.push(`const anchor = { ${state}: ${initial}, ${key}: ${initial} };`);

  // Build the lookup object independently of the receiver. Use the same descriptor productions for both own
  // properties and prototype chains, and put the observable state on the object which receives `this`.
  const descriptor =
    property === "data"
      ? `${key}: ${initial}`
      : method === "get"
        ? `get ${key}() { this.${state} += ${delta}; return this.${state}; }`
        : `set ${key}(value) { this.${state} += value; }`;
  const depth = inherited ? 1 + random.int(3) : 0;
  const base = depth ? "layer0" : "lookup";
  lines.push(`const ${base} = { ${state}: ${initial}, ${descriptor} };`);
  for (let i = 1; i <= depth; i++) {
    const parent = `layer${i - 1}`;
    const name = i === depth ? "lookup" : `layer${i}`;
    const initializer = pick([`{ __proto__: ${parent}, ${state}: ${initial} }`, `Object.create(${parent})`]);
    // The final receiver needs its own initial tally, independent of which prototype recipe was chosen.
    lines.push(
      `const ${name} = ${i === depth && !receiver ? `{ __proto__: ${parent}, ${state}: ${initial} }` : initializer};`
    );
  }

  const recv = receiver ? pick(["anchor", "(() => anchor)()", "({ alias: anchor }).alias"]) : undefined;
  const args = ["lookup", JSON.stringify(key), ...(method === "set" ? [String(delta + initial)] : [])];
  if (recv) args.push(recv);
  const callStyle = pick(["direct", "alias", "call", "wrapper"]);
  let expression = `Reflect.${method}(${args.join(", ")})`;
  if (callStyle === "alias") {
    lines.push(`const invoke = Reflect.${method};`);
    expression = `invoke(${args.join(", ")})`;
  } else if (callStyle === "call") {
    expression = `Reflect.${method}.call(null, ${args.join(", ")})`;
  } else if (callStyle === "wrapper") {
    lines.push(
      `const invoke = (fn, ${args.map((_, i) => `arg${i}`).join(", ")}) => fn(${args.map((_, i) => `arg${i}`).join(", ")});`
    );
    expression = `invoke(Reflect.${method}, ${args.join(", ")})`;
  }
  const observation = property === "data" ? key : state;
  const subject = receiver ? "anchor" : "lookup";
  const effectIndex = lines.length;
  lines.push(
    `const result = ${expression};`,
    pick([`const seen = ${subject}.${observation};`, `const { ${observation}: seen } = ${subject};`]),
    "console.log(seen, result, first + second);"
  );
  const safe = lines.map((_, index) => index);
  [safe[0], safe[1]] = [safe[1], safe[0]];
  const unsafe = lines.map((_, index) => index);
  [unsafe[effectIndex], unsafe[effectIndex + 1]] = [unsafe[effectIndex + 1], unsafe[effectIndex]];
  const family = familyOf(method, property, receiver, inherited);
  return {
    name: `seed ${seed}: ${family}, depth ${depth}, ${callStyle}`,
    family,
    premises: {},
    source: `${lines.join("\n")}\n`,
    reorders: [
      { order: safe, behavior: "same", accepted: true },
      { order: unsafe, behavior: "differs", stdoutDiffers: true },
    ],
  };
}

export function generateCases(seed) {
  const random = rng(seed);
  return shapes.map((shape) => makeCase(random, seed, shape));
}

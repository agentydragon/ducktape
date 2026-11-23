// Smoke tests: call a bundle's exported API (or the entry module of a split of it) and print what it computed.
//
//   node smoke.mjs <lodash|acorn|semver|zod> <path to the module>
//
// Prints one line of JSON: {"ok":true,"value":…} or {"ok":false,"error":"<ErrorName>: <first line>"}.
// A split is behaving like the bundle when both print the same line.
import { pathToFileURL } from "node:url";

const tests = {
  lodash: ({ chunk, get, set, merge, cloneDeep, groupBy, sortBy, uniqBy, debounce, throttle }) => {
    const source = { a: [1, { b: 2 }], when: new Date(0), nested: { deep: { deeper: [3] } } };
    const copy = cloneDeep(source);
    return {
      chunk: chunk([1, 2, 3, 4, 5], 2),
      get: get({ a: { b: [10, 20, { c: 3 }] } }, "a.b[2].c"),
      getDefault: get({}, "x.y.z", "fallback"),
      set: set({}, "x.y[1].z", 7),
      merge: merge({ a: 1, b: { c: 2 } }, { b: { d: 3 }, e: [1, 2] }),
      cloneDeep: { copy, sameObject: copy === source, sameInner: copy.a === source.a, time: copy.when.getTime() },
      groupBy: groupBy([6.1, 4.2, 6.3], Math.floor),
      sortBy: sortBy(
        [
          { n: "b", a: 2 },
          { n: "a", a: 2 },
          { n: "c", a: 1 },
        ],
        ["a", "n"]
      ),
      uniqBy: uniqBy([2.1, 1.2, 2.3], Math.floor),
      timers: [typeof debounce(() => {}, 10), typeof throttle(() => {}, 10)],
    };
  },

  acorn: ({ parse, parseExpressionAt, tokenizer, version }) => {
    const parsed = (code, options = {}) => parse(code, { ecmaVersion: 2022, ...options });
    const failure = (code) => {
      try {
        parsed(code);
        return null;
      } catch (error) {
        return `${error.name}: ${error.message}`;
      }
    };
    return {
      version,
      program: parsed(
        "const [a, ...b] = f(`x${y}`, /re/g) ?? class A { #p = 1; static s = 2 }; label: for (;;) break label;"
      ),
      module: parsed("import x, { y as z } from 'm'; export default async function* g() { await x; yield* z; }", {
        sourceType: "module",
      }),
      expression: parseExpressionAt("1 + 2 * (3 - x) ** 2", 0, { ecmaVersion: 2020 }),
      tokens: [...tokenizer("let x = 1.5e3; // c\nx >>>= 2n", { ecmaVersion: 2020 })].map((t) => [
        t.type.label,
        t.value,
        t.start,
      ]),
      errors: [failure("let let"), failure("a => {"), failure("1 +"), failure("class { }")],
    };
  },

  semver: ({ semver }) => ({
    valid: [semver.valid("1.2.3"), semver.valid("1.2"), semver.valid("v2.0.0-beta.1+build")],
    compare: [semver.gt("1.2.3", "1.2.2"), semver.lt("1.0.0-alpha", "1.0.0"), semver.compare("2.0.0", "10.0.0")],
    satisfies: [
      semver.satisfies("1.2.3", "^1.0.0"),
      semver.satisfies("2.0.0", "~1.2.0"),
      semver.satisfies("1.5.0", ">=1.2 <2 || 3.x"),
    ],
    inc: [semver.inc("1.2.3", "minor"), semver.inc("1.2.3-beta.1", "prerelease", "beta"), semver.inc("1.2.3", "major")],
    coerce: [String(semver.coerce("v2.3 and more")), semver.coerce("nope")],
    maxSatisfying: semver.maxSatisfying(["1.0.0", "1.5.2", "2.1.0", "1.9.9"], "^1.0.0"),
    diff: [semver.diff("1.2.3", "1.3.0"), semver.diff("1.2.3", "1.2.3-rc.1")],
    range: new semver.Range("1.2.x || >=3.0.0 <3.2.0").set.map((comparators) => comparators.map(String)),
    parsed: JSON.parse(JSON.stringify(semver.parse("3.4.5-alpha.7+exp.sha"))),
  }),

  zod: ({ user, z }) => {
    const outcome = (schema, input) => {
      const result = schema.safeParse(input);
      return result.success
        ? { data: result.data }
        : { issues: result.error.issues.map((i) => [i.code, i.path, i.message]) };
    };
    return {
      user: [
        outcome(user, { name: "ada", age: 36, tags: ["x", "y"] }),
        outcome(user, { name: "", age: 1.5, tags: [1] }),
        outcome(user, null),
      ],
      email: [outcome(z.string().email(), "a@b.co"), outcome(z.string().email(), "nope")],
      union: [outcome(z.union([z.string(), z.number()]), 5), outcome(z.union([z.string(), z.number()]), true)],
      discriminated: [
        outcome(
          z.discriminatedUnion("t", [z.object({ t: z.literal("a"), x: z.number() }), z.object({ t: z.literal("b") })]),
          { t: "a", x: 1 }
        ),
        outcome(
          z.discriminatedUnion("t", [z.object({ t: z.literal("a"), x: z.number() }), z.object({ t: z.literal("b") })]),
          { t: "c" }
        ),
      ],
      transform: outcome(
        z
          .string()
          .transform((s) => s.length)
          .pipe(z.number().min(3)),
        "ab"
      ),
      record: outcome(z.record(z.string(), z.number().int()), { a: 1, b: 2.5 }),
      defaults: outcome(z.object({ a: z.number().default(7), b: z.string().optional() }), {}),
    };
  },
};

export async function smoke(name, modulePath) {
  try {
    const api = await import(pathToFileURL(modulePath).href);
    return {
      ok: true,
      value: JSON.parse(
        JSON.stringify(tests[name](api), (_, v) =>
          typeof v === "undefined" ? "<undefined>" : typeof v === "bigint" ? `${v}n` : v
        )
      ),
    };
  } catch (error) {
    return { ok: false, error: `${error?.name ?? "Error"}: ${String(error?.message ?? error).split("\n")[0]}` };
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [name, modulePath] = process.argv.slice(2);
  process.stdout.write(`${JSON.stringify(await smoke(name, modulePath))}\n`);
}

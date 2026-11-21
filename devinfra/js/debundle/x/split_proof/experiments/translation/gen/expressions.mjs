// Family `expressions`: expressions of type `num`, `str` and `bool` that cannot throw, built from literals, bindings, every
// operator, and the standard functions that have a row in `Intrinsics.lean` (and a few that have none, which the analysis
// must treat as calls into the world). Every compound expression is parenthesized, so a production never needs to know the
// precedence of the one inside it. Anything that can be `undefined` (a missing map key, a hole) is turned into a number
// before it is used, or flows only into operators that do not throw.
import { kindOf, local } from "./core.mjs";

const NUMBERS = [
  "0", "1", "2", "3", "4", "5", "7", "9", "10", "42", "255", "0.5", "1.5", "2.25", "(-1)", "(-3)", "1e3", "1e-2",
  "0x1f", "0b101", "0o17", "1_000", "123456789", "2147483647", "4294967296",
]; // prettier-ignore
const SPECIAL_NUMBERS = ["NaN", "Infinity", "(-Infinity)", "(-0)"];
const STRINGS = [
  '"a"', '"bc"', '""', '"x y"', "'q'", '"\\n"', '"\\u00e9"', '"\\u{1F600}"', '"A-b_C"', '"  pad  "', '"0123"', '"abc,def"',
]; // prettier-ignore
const SMALL = ["0", "1", "2", "3"];

export function install(g) {
  const N = (s, d) => g.num(s, d);
  const S = (s, d) => g.str(s, d);
  const B = (s, d) => g.bool(s, d);
  const p = (list, w, make, extra = {}) => list.push({ w, make, ...extra });
  const pick = (xs) => g.pick(xs);
  const fresh = (prefix) => g.fresh(prefix);
  const op = (...ops) => pick(ops);

  // ---- numbers
  const num = g.prods.num;
  p(num, 4, () => pick(NUMBERS), { leaf: true });
  p(num, 1, () => pick(SPECIAL_NUMBERS), { leaf: true });
  p(num, 6, (g, s) => g.ref(pick(g.refs(s, "num"))), { leaf: true, ok: (g, s) => g.refs(s, "num").length > 0 });
  p(num, 5, (g, s, d) => `(${N(s, d)} ${op("+", "-", "*", "%", "/")} ${N(s, d)})`);
  p(num, 1, (g, s, d) => `(${N(s, d)} ** ${op("2", "3", "0.5", "(-1)")})`);
  p(num, 2, (g, s, d) => `(${N(s, d)} ${op("|", "&", "^")} ${N(s, d)})`);
  p(num, 1, (g, s, d) => `(${N(s, d)} ${op("<<", ">>", ">>>")} (${N(s, d)} & 7))`);
  p(num, 2, (g, s, d) => `(${op("-", "+", "~")}${N(s, d)})`);
  p(num, 1, (g, s, d) => `(+${B(s, d)})`);
  p(num, 2, (g, s, d) => `(${B(s, d)} ? ${N(s, d)} : ${N(s, d)})`);
  p(num, 1, (g, s, d) => `(${N(s, d)}, ${N(s, d)})`);
  p(num, 2, (g, s, d) => `(${N(s, d)} ${op("||", "&&", "??")} ${N(s, d)})`);
  p(num, 1, (g, s, d) => `((void ${N(s, d)}) ?? ${N(s, d)})`);
  // a field of an object, read with a dot, with brackets, and through an optional chain
  p(
    num,
    4,
    (g, s) => {
      const [b, f] = pick(g.fields(s, "num"));
      const read = g.access(b, f);
      return pick([
        read,
        read,
        /^[A-Za-z_$][\w$]*$/.test(f) ? `${g.ref(b)}?.${f}` : read,
        `${g.ref(b)}?.[${JSON.stringify(f)}]`,
        `${g.ref(b)}[${JSON.stringify(f)}]`,
      ]);
    },
    { ok: (g, s) => g.fields(s, "num").length > 0 }
  );
  // an element of an array: a valid index, and `at`
  p(
    num,
    3,
    (g, s, d) => {
      const a = g.ref(pick(g.arrays(s, "num")));
      return pick([`${a}[(Math.abs(${N(s, d)} | 0) % ${a}.length)]`, `(${a}.at(${N(s, d)} | 0) ?? 0)`, `${a}[0]`]);
    },
    { ok: (g, s) => g.arrays(s, "num").length > 0 }
  );
  p(num, 3, (g, s) => `${g.ref(pick(g.refs(s, (b) => kindOf(b.type) === "arr")))}.length`, {
    ok: (g, s) => g.refs(s, (b) => kindOf(b.type) === "arr").length > 0,
  });
  p(num, 2, (g, s) => `${g.ref(pick(g.refs(s, (b) => ["map", "set"].includes(kindOf(b.type)))))}.size`, {
    ok: (g, s) => g.refs(s, (b) => ["map", "set"].includes(kindOf(b.type))).length > 0,
  });
  p(num, 2, (g, s, d) => `${S(s, d)}.length`);
  p(
    num,
    3,
    (g, s, d) =>
      `Math.${op("abs", "floor", "ceil", "round", "trunc", "sign", "sqrt", "cbrt", "clz32", "fround")}(${N(s, d)})`
  );
  p(num, 3, (g, s, d) => `Math.${op("max", "min", "pow", "imul", "hypot", "atan2")}(${N(s, d)}, ${N(s, d)})`);
  p(num, 1, (g, s, d) => `Math.${op("exp", "log", "sin", "cos")}(${N(s, d)} % 3)`);
  p(num, 2, (g, s, d) =>
    pick([
      `Number(${S(s, d)})`,
      `parseInt(${S(s, d)}, 10)`,
      `parseFloat(${S(s, d)})`,
      `Number.parseInt(${S(s, d)})`,
      `Number(${B(s, d)})`,
    ])
  );
  p(num, 2, (g, s, d) =>
    pick([
      `${S(s, d)}.indexOf(${S(s, d)})`,
      `${S(s, d)}.charCodeAt(0)`,
      `${S(s, d)}.localeCompare(${S(s, d)})`,
      `(${S(s, d)}.codePointAt(0) ?? 0)`,
    ])
  );
  // a map's value, or 0 for a key it does not have
  p(
    num,
    3,
    (g, s, d) => {
      const [kt, m] = pick([...g.maps(s, "num").map((b) => ["num", b]), ...g.maps(s, "str").map((b) => ["str", b])]);
      return `(${g.ref(m)}.get(${kt === "num" ? N(s, d) : S(s, d)}) ?? 0)`;
    },
    { ok: (g, s) => g.maps(s, "num").length + g.maps(s, "str").length > 0 }
  );
  // callbacks of array methods; the callback's parameters are locals of the callback
  p(
    num,
    2,
    (g, s, d) => {
      const a = g.ref(pick(g.arrays(s, "num")));
      const [x, y] = [fresh("l"), fresh("l")];
      const numeric = (name) => ({ name, scope: "local", type: "num", mutable: false });
      const one = g.callback(s, [numeric(x)], { ret: "bool" });
      const two = g.callback(s, [numeric(x), numeric(y)], { ret: "num" });
      return pick([
        `${a}.reduce((${local(x)}, ${local(y)}) => ${N(two, d)}, ${N(s, d)})`,
        `${a}.findIndex((${local(x)}) => ${B(one, d)})`,
        `${a}.filter((${local(x)}) => ${B(one, d)}).length`,
        `${a}.map((${local(x)}) => ${N(one, d)}).length`,
        `${a}.indexOf(${N(s, d)})`,
        `${a}.lastIndexOf(${N(s, d)})`,
        `Math.max(...${a}, 0)`,
      ]);
    },
    { ok: (g, s) => g.arrays(s, "num").length > 0 }
  );
  p(num, 1, (g, s, d) => `new Date(${N(s, d)} % 1000).getTime()`);
  p(num, 1, (g, s, d) => `(JSON.parse(JSON.stringify(${N(s, d)}) ?? "0") ?? 0)`);
  p(num, 1, (g, s, d) => `(+${N(s, d)}).toString(${op("2", "8", "16")}).length`);

  // ---- strings
  const str = g.prods.str;
  p(str, 5, () => pick(STRINGS), { leaf: true });
  p(str, 6, (g, s) => g.ref(pick(g.refs(s, "str"))), { leaf: true, ok: (g, s) => g.refs(s, "str").length > 0 });
  p(str, 4, (g, s, d) => `(${S(s, d)} + ${pick([S(s, d), N(s, d)])})`);
  p(str, 1, (g, s, d) => `(${N(s, d)} + ${S(s, d)})`);
  p(str, 3, (g, s, d) => "`" + `${pick(["a", "x", "", "<"])}\${${N(s, d)}}${pick(["", "-", ">"])}\${${S(s, d)}}` + "`");
  p(str, 1, () => pick(["`plain`", "`two\\nlines`", "`a${1 + 1}b`"]), { leaf: true });
  p(str, 3, (g, s, d) =>
    pick([
      `String(${N(s, d)})`,
      `String(${B(s, d)})`,
      `(+${N(s, d)}).toString()`,
      `(+${N(s, d)}).toFixed(${pick(SMALL)})`,
      `(+${N(s, d)}).toPrecision(3)`,
    ])
  );
  p(str, 4, (g, s, d) => {
    const x = S(s, d);
    return pick([
      `${x}.toUpperCase()`, `${x}.toLowerCase()`, `${x}.trim()`, `${x}.trimStart()`, `${x}.slice(${pick(SMALL)}, ${pick(["2", "4"])})`,
      `${x}.substring(${pick(SMALL)})`, `${x}.padStart(${pick(["3", "5"])}, "0")`, `${x}.padEnd(${pick(["3", "5"])}, "-")`,
      `${x}.repeat(${pick(SMALL)})`, `${x}.replace("a", "b")`, `${x}.replaceAll("b", "cc")`, `${x}.split("").reverse().join("")`,
      `${x}.concat(${S(s, d)})`, `${x}.normalize()`, `(${x}.at(0) ?? "")`, `${x}.charAt(0)`, `${x}.toLocaleUpperCase()`, `${x}.substr(1, 2)`,
    ]); // prettier-ignore
  });
  p(str, 2, (g, s, d) => `(${B(s, d)} ? ${S(s, d)} : ${S(s, d)})`);
  p(str, 2, (g, s, d) => `(${S(s, d)} ${op("||", "??")} ${S(s, d)})`);
  p(str, 1, (g, s) => `(typeof ${g.ref(pick(g.refs(s, () => true)))})`, {
    ok: (g, s) => g.refs(s, () => true).length > 0,
  });
  p(
    str,
    3,
    (g, s) => {
      const a = g.arrays(s, "num").concat(g.arrays(s, "str"));
      return `${g.ref(pick(a))}.join(${pick(['","', '"-"', '""', '" "'])})`;
    },
    { ok: (g, s) => g.arrays(s, "num").length + g.arrays(s, "str").length > 0 }
  );
  p(
    str,
    3,
    (g, s) => {
      const [b, f] = pick(g.fields(s, "str"));
      return g.access(b, f);
    },
    { ok: (g, s) => g.fields(s, "str").length > 0 }
  );
  p(str, 1, (g, s, d) =>
    pick([
      `(JSON.stringify(${N(s, d)}) ?? "")`,
      `JSON.stringify({ k: ${N(s, d)}, s: ${S(s, d)} })`,
      `JSON.stringify([${N(s, d)}, ${B(s, d)}])`,
    ])
  );
  p(str, 1, (g, s) => `Object.keys(${g.ref(pick(g.refs(s, (b) => kindOf(b.type) === "obj")))}).join(",")`, {
    ok: (g, s) => g.refs(s, (b) => kindOf(b.type) === "obj").length > 0,
  });
  p(str, 1, (g, s, d) => `[${S(s, d)}, ${S(s, d)}].join(${pick(['"+"', '","'])})`);
  p(str, 1, (g, s, d) => `String.fromCharCode(${N(s, d)} & 127)`);

  // ---- booleans
  const bool = g.prods.bool;
  p(bool, 3, () => pick(["true", "false"]), { leaf: true });
  p(bool, 5, (g, s) => g.ref(pick(g.refs(s, "bool"))), { leaf: true, ok: (g, s) => g.refs(s, "bool").length > 0 });
  p(bool, 5, (g, s, d) => `(${N(s, d)} ${op("<", "<=", ">", ">=", "==", "!=", "===", "!==")} ${N(s, d)})`);
  p(bool, 3, (g, s, d) => `(${S(s, d)} ${op("===", "!==", "<", "==", "!=")} ${S(s, d)})`);
  p(bool, 2, (g, s, d) => `(!${B(s, d)})`);
  p(bool, 3, (g, s, d) => `(${B(s, d)} ${op("&&", "||", "??")} ${B(s, d)})`);
  p(bool, 1, (g, s, d) => `(${B(s, d)} ? ${B(s, d)} : ${B(s, d)})`);
  p(bool, 2, (g, s, d) =>
    pick([
      `Number.isNaN(${N(s, d)})`,
      `Number.isInteger(${N(s, d)})`,
      `isNaN(${N(s, d)})`,
      `isFinite(${N(s, d)})`,
      `Number.isFinite(${N(s, d)})`,
      `Number.isSafeInteger(${N(s, d)})`,
      `Object.is(${N(s, d)}, ${N(s, d)})`,
    ])
  );
  p(
    bool,
    2,
    (g, s) => {
      const o = pick(g.refs(s, (b) => kindOf(b.type) === "obj"));
      const f = pick([...Object.keys(o.type.fields), "missing"]);
      return pick([
        `(${JSON.stringify(f)} in ${g.ref(o)})`,
        o.type.bare
          ? `Object.hasOwn(${g.ref(o)}, ${JSON.stringify(f)})`
          : `${g.ref(o)}.hasOwnProperty(${JSON.stringify(f)})`,
        `Object.hasOwn(${g.ref(o)}, ${JSON.stringify(f)})`,
        ...(o.type.bare ? [] : [`(${g.ref(o)} instanceof Object)`]),
      ]);
    },
    { ok: (g, s) => g.refs(s, (b) => kindOf(b.type) === "obj").length > 0 }
  );
  p(
    bool,
    2,
    (g, s, d) => {
      const a = pick(g.arrays(s, "num"));
      const x = fresh("l");
      const inner = g.callback(s, [{ name: x, scope: "local", type: "num", mutable: false }], { ret: "bool" });
      return pick([
        `${g.ref(a)}.includes(${N(s, d)})`,
        `Array.isArray(${g.ref(a)})`,
        `${g.ref(a)}.some((${local(x)}) => ${local(x)} > ${N(inner, d)})`,
        `${g.ref(a)}.every((${local(x)}) => ${local(x)} === ${local(x)})`,
      ]);
    },
    { ok: (g, s) => g.arrays(s, "num").length > 0 }
  );
  p(
    bool,
    2,
    (g, s, d) => {
      const sets = g.sets(s, "num");
      const maps = g.maps(s, "num");
      const b = pick([...sets, ...maps]);
      return `${g.ref(b)}.has(${N(s, d)})`;
    },
    { ok: (g, s) => g.sets(s, "num").length + g.maps(s, "num").length > 0 }
  );
  p(bool, 3, (g, s, d) => {
    const x = S(s, d);
    return pick([
      `${x}.startsWith(${S(s, d)})`,
      `${x}.endsWith(${S(s, d)})`,
      `${x}.includes(${S(s, d)})`,
      `/a/.test(${x})`,
      `/^[a-c]+$/i.test(${x})`,
      `/(?<first>\\w)\\k<first>/.test(${x})`,
      `/\\p{L}/u.test(${x})`,
      `/[\\p{L}--[a-z]]/v.test(${x})`,
    ]);
  });
  p(bool, 1, (g, s, d) => `(typeof ${N(s, d)} === "number")`);
}

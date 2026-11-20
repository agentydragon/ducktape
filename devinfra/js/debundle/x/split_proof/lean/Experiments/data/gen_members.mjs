// Lists the own properties of the standard objects, as `Path.member` strings, from the running Node.
//
//   node gen_members.mjs > intrinsic_members.json
//
// `Intrinsics.lean` uses the list to tell a member that exists but is not summarized (a call of it is a call into the
// world) from a property that does not exist (reading it yields nothing). A member of a newer ECMAScript than the
// Node that generated the list is missing from it: regenerate when Node moves on.
const roots = [
  "Object",
  "Function",
  "Array",
  "String",
  "Number",
  "Boolean",
  "Symbol",
  "BigInt",
  "Map",
  "Set",
  "WeakMap",
  "WeakSet",
  "WeakRef",
  "RegExp",
  "Error",
  "TypeError",
  "RangeError",
  "SyntaxError",
  "ReferenceError",
  "EvalError",
  "URIError",
  "AggregateError",
  "Promise",
  "Date",
  "JSON",
  "Math",
  "Reflect",
  "Proxy",
  "ArrayBuffer",
  "DataView",
  "Atomics",
  "Uint8Array",
  "Int8Array",
  "Uint16Array",
  "Int16Array",
  "Uint32Array",
  "Int32Array",
  "Float32Array",
  "Float64Array",
];
const out = new Set();
const own = (o) => Object.getOwnPropertyNames(o);
const add = (path, o) => {
  for (const k of own(o)) out.add(`${path}.${k}`);
};
for (const r of roots) {
  const v = globalThis[r];
  if (v === undefined) continue;
  add(r, v);
  if (v.prototype) add(`${r}.prototype`, v.prototype);
}
for (const k of own(Intl)) {
  out.add(`Intl.${k}`);
  if (Intl[k]?.prototype) add(`Intl.${k}.prototype`, Intl[k].prototype);
}
// methods of a string, number, boolean, symbol or bigint
for (const p of [String.prototype, Number.prototype, Boolean.prototype, Symbol.prototype, BigInt.prototype])
  for (const k of own(p)) out.add(`prim.${k}`);
console.log(JSON.stringify([...out].sort(), null, 0));

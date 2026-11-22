// Preload of the footprint oracle (`node --import oracle_harness.mjs program.mjs`): the part that runs next to the program
// under test. The instrumented program calls `globalThis.__oracle(unit, snapshot)` after each unit (`snapshot()` returns,
// per module-scope binding, `[value]`, or nothing for one still in its temporal dead zone) and once before the first.
// This file keeps for every binding a digest of what it reaches, and writes the units after which a digest differs to
// `$ORACLE_OUT` when the process exits. `footprint_oracle.mjs` compares them with the footprints and says what is not seen.
//
// The digest of a binding covers the graph its value reaches: each object (prototype, extensibility, every own property
// with its attributes, a getter and a setter by identity and never called), the contents of `Map`, `Set`, typed arrays,
// array buffers, dates, regular expressions and boxed primitives, and the objects those refer to, cycles included.
// Objects are numbered in the order they are first met, so two runs of a deterministic program agree, and a binding that
// is given a new object that looks the same changes. What the global object reaches before the program runs (the
// standard objects, `process`, `console`, …) are opaque leaves; the ECMAScript standard objects among them are compared
// once at the start and once at the end, and a difference is reported as a patch of an intrinsic (premise P1).
import { writeFileSync } from "node:fs";
import { types } from "node:util";

// What the harness uses, captured before the program runs.
const { ownKeys, getPrototypeOf, getOwnPropertyDescriptor } = Reflect;
const { isExtensible } = Object;
const { isArray } = Array;
const { stringify } = JSON;
const mapForEach = Map.prototype.forEach;
const setForEach = Set.prototype.forEach;
const getter = (proto, key) => getOwnPropertyDescriptor(proto, key).get;
const dateGetTime = Date.prototype.getTime;
const regexpSource = getter(RegExp.prototype, "source");
const regexpFlags = getter(RegExp.prototype, "flags");
const symbolDescription = getter(Symbol.prototype, "description");
const typedProto = getPrototypeOf(Uint8Array.prototype);
const typedLength = getter(typedProto, "length");
const typedName = getter(typedProto, Symbol.toStringTag);
const typedBuffer = getter(typedProto, "buffer");
const typedOffset = getter(typedProto, "byteOffset");
const bufferLength = getter(ArrayBuffer.prototype, "byteLength");
const viewBuffer = getter(DataView.prototype, "buffer");
const viewOffset = getter(DataView.prototype, "byteOffset");
const viewLength = getter(DataView.prototype, "byteLength");
const unbox = [
  [types.isNumberObject, Number.prototype.valueOf],
  [types.isStringObject, String.prototype.valueOf],
  [types.isBooleanObject, Boolean.prototype.valueOf],
  [types.isBigIntObject, BigInt.prototype.valueOf],
  [types.isSymbolObject, Symbol.prototype.valueOf],
];

// ---- hashing: two 32-bit FNV-style states, printed as one hex string
function hash(text) {
  let a = 0x811c9dc5;
  let b = 0x01000193;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    a = Math.imul(a ^ c, 0x01000193) >>> 0;
    b = Math.imul(b ^ c, 0x85ebca6b) >>> 0;
    b = (b ^ (b >>> 13)) >>> 0;
  }
  return a.toString(16).padStart(8, "0") + b.toString(16).padStart(8, "0");
}

// ---- the digest of a value
const objectIds = new WeakMap();
const symbolIds = new Map();
let nextId = 1;
const idOf = (o) => {
  let id = objectIds.get(o);
  if (id === undefined) objectIds.set(o, (id = nextId++));
  return id;
};
const symbolName = (s) => {
  let id = symbolIds.get(s);
  if (id === undefined) symbolIds.set(s, (id = symbolIds.size + 1));
  return `${id}:${symbolDescription.call(s) ?? ""}`;
};
const isObjectLike = (v) => v !== null && (typeof v === "object" || typeof v === "function");

function primitive(v) {
  switch (typeof v) {
    case "undefined":
      return "u";
    case "boolean":
      return v ? "t" : "f";
    case "number":
      return Object.is(v, -0) ? "n-0" : `n${v}`;
    case "bigint":
      return `i${v}`;
    case "symbol":
      return `y${symbolName(v)}`;
    case "string":
      return v.length > 200 ? `s${v.length}:${hash(v)}` : `s${stringify(v)}`;
    default:
      return "null";
  }
}

const isIndexKey = (k) => typeof k === "string" && /^(0|[1-9]\d*)$/.test(k);
const sum = (view) => {
  let bytes = "";
  for (let i = 0; i < view.length; i++) bytes += `${view[i]},`;
  return hash(bytes);
};

/** What an object is besides its properties: the engine's internal slots a program can change. */
function internals(o, ref) {
  if (isArray(o)) return "array";
  if (typeof o === "function") return "function";
  const parts = [];
  if (types.isMap(o)) {
    parts.push("map");
    mapForEach.call(o, (v, k) => parts.push(`${ref(k)}=${ref(v)}`));
  } else if (types.isSet(o)) {
    parts.push("set");
    setForEach.call(o, (v) => parts.push(ref(v)));
  } else if (types.isDate(o)) parts.push(`date${dateGetTime.call(o)}`);
  else if (types.isRegExp(o)) parts.push(`regexp/${regexpSource.call(o)}/${regexpFlags.call(o)}`);
  else if (types.isTypedArray(o))
    parts.push(
      `typed${typedName.call(o)}:${typedLength.call(o)}:${ref(typedBuffer.call(o))}@${typedOffset.call(o)}:${sum(o)}`
    );
  else if (types.isArrayBuffer(o) || types.isSharedArrayBuffer(o))
    parts.push(`buffer${bufferLength.call(o)}:${sum(new Uint8Array(o))}`);
  else if (types.isDataView(o))
    parts.push(`view${ref(viewBuffer.call(o))}@${viewOffset.call(o)}:${viewLength.call(o)}`);
  else if (types.isBoxedPrimitive(o)) {
    const [, valueOf] = unbox.find(([is]) => is(o));
    parts.push(`boxed${primitive(valueOf.call(o))}`);
  } else if (types.isNativeError(o)) parts.push("error");
  else if (types.isPromise(o)) parts.push("promise");
  else if (types.isWeakMap(o) || types.isWeakSet(o)) parts.push("weak");
  else if (types.isGeneratorObject(o) || types.isMapIterator(o) || types.isSetIterator(o)) parts.push("iterator");
  else if (types.isArgumentsObject(o)) parts.push("arguments");
  else if (types.isModuleNamespaceObject(o)) parts.push("namespace");
  else parts.push("object");
  return parts.join(";");
}

/** The record of one object: a string, and the objects it refers to. A proxy is opaque. */
function describe(o) {
  const refs = [];
  const ref = (v) => {
    if (!isObjectLike(v)) return primitive(v);
    refs.push(v);
    return `#${idOf(v)}`;
  };
  if (types.isProxy(o)) return { text: "proxy", refs };
  const typed = types.isTypedArray(o);
  const parts = [internals(o, ref), `p${ref(getPrototypeOf(o))}`, isExtensible(o) ? "x" : "-"];
  for (const key of ownKeys(o)) {
    if (typed && isIndexKey(key)) continue;
    const d = getOwnPropertyDescriptor(o, key);
    const name = typeof key === "symbol" ? `@${symbolName(key)}` : key;
    const flags = `${d.enumerable ? "e" : "-"}${d.configurable ? "c" : "-"}`;
    parts.push(
      "value" in d
        ? `${name}:${flags}${d.writable ? "w" : "-"}${ref(d.value)}`
        : `${name}:${flags}a${ref(d.get)}/${ref(d.set)}`
    );
  }
  return { text: parts.join("|"), refs };
}

// ---- the objects the global object reaches before the program runs, and the standard ones among them
function reachable(roots) {
  const found = new Map(); // object -> label
  const queue = roots.map(([label, o]) => [label, o]);
  for (const [label, o] of queue) if (!found.has(o)) found.set(o, label);
  for (let i = 0; i < queue.length; i++) {
    const [label, o] = queue[i];
    if (types.isProxy(o)) continue;
    const next = [];
    for (const key of ownKeys(o)) {
      const d = getOwnPropertyDescriptor(o, key);
      const name = typeof key === "symbol" ? `[${symbolName(key)}]` : key;
      for (const v of "value" in d ? [d.value] : [d.get, d.set]) next.push([`${label}.${name}`, v]);
    }
    next.push([`${label}.__proto__`, getPrototypeOf(o)]);
    for (const [l, v] of next)
      if (isObjectLike(v) && !found.has(v)) {
        found.set(v, l);
        queue.push([l, v]);
      }
  }
  return found;
}
// prettier-ignore
const STANDARD = [
  "Object", "Function", "Array", "String", "Number", "Boolean", "Symbol", "BigInt", "Map", "Set", "WeakMap", "WeakSet",
  "WeakRef", "FinalizationRegistry", "RegExp", "Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError",
  "EvalError", "URIError", "AggregateError", "Promise", "Date", "JSON", "Math", "Reflect", "Proxy", "ArrayBuffer",
  "SharedArrayBuffer", "DataView", "Atomics", "Iterator", "Intl", "Int8Array", "Uint8Array", "Uint8ClampedArray",
  "Int16Array", "Uint16Array", "Int32Array", "Uint32Array", "Float32Array", "Float64Array", "BigInt64Array",
  "BigUint64Array", "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent",
  "encodeURI", "decodeURI", "escape", "unescape",
];
const standard = reachable(STANDARD.filter((n) => n in globalThis).map((n) => [n, globalThis[n]]));
const opaque = new Set([...reachable([["globalThis", globalThis]]).keys()].map(idOf));
const standardDigests = () => new Map([...standard].map(([o, label]) => [label, hash(describe(o).text)]));
const standardBefore = standardDigests();

// ---- snapshots
const ABSENT = "tdz";

/** `{ name: digest }` for every binding, `ABSENT` for one that is not initialized. */
function snapshot(names, values, globals) {
  const records = new Map(); // id -> { digest, refs: [id] }
  const walk = (start) => {
    const stack = [start];
    while (stack.length > 0) {
      const o = stack.pop();
      const id = idOf(o);
      if (records.has(id)) continue;
      if (opaque.has(id)) {
        records.set(id, { digest: "opaque", refs: [] });
        continue;
      }
      const { text, refs } = describe(o);
      records.set(id, { digest: hash(text), refs: refs.map(idOf) });
      for (const r of refs) stack.push(r);
    }
  };
  const repr = (v, ids) => {
    if (!isObjectLike(v)) return primitive(v);
    walk(v);
    ids.push(idOf(v));
    return `#${idOf(v)}`;
  };
  const roots = names.map((name, i) => {
    const ids = [];
    return { name, ids, text: values[i] === undefined ? ABSENT : repr(values[i][0], ids) };
  });
  {
    const ids = [];
    roots.push({ name: "<global>", ids, text: globals.map(([k, v]) => `${k}=${repr(v, ids)}`).join(",") });
  }
  const digests = {};
  for (const { name, ids, text } of roots) {
    if (text === ABSENT) {
      digests[name] = ABSENT;
      continue;
    }
    const seen = new Set();
    const stack = [...ids];
    while (stack.length > 0) {
      const id = stack.pop();
      if (seen.has(id)) continue;
      seen.add(id);
      for (const r of records.get(id).refs) stack.push(r);
    }
    const reached = [...seen].sort((a, b) => a - b).map((id) => `${id}:${records.get(id).digest}`);
    digests[name] = hash(`${text}|${reached.join(",")}`);
  }
  return digests;
}

// ---- what the program printed, and the globals it added
let printed = 0;
for (const stream of [process.stdout, process.stderr]) {
  const write = stream.write;
  stream.write = function (...args) {
    printed++;
    return Reflect.apply(write, this, args);
  };
}
const initialGlobals = new Set(ownKeys(globalThis));
const addedGlobals = () =>
  ownKeys(globalThis)
    .filter((k) => !initialGlobals.has(k))
    .map((k) => {
      const d = getOwnPropertyDescriptor(globalThis, k);
      return [typeof k === "symbol" ? symbolName(k) : k, "value" in d ? d.value : d.get];
    });

// ---- the units
const units = [];
let previous = null;
let last = null;
let printedBefore = 0;

globalThis.__oracle = (unit, snapshotFn) => {
  const names = snapshotFn.names;
  const digests = snapshot(names, snapshotFn(), addedGlobals());
  const entry = { unit, printed: printed - printedBefore, changed: [], initialized: [] };
  printedBefore = printed;
  if (previous !== null)
    for (const name of Object.keys(digests)) {
      if (previous[name] === digests[name]) continue;
      (previous[name] === ABSENT ? entry.initialized : entry.changed).push(name);
    }
  previous = digests;
  last = snapshotFn;
  if (unit >= 0) units.push(entry);
};

let tail = null;
process.on("beforeExit", () => {
  // what asynchronous continuations changed after the last unit
  if (last === null || tail !== null) return;
  const digests = snapshot(last.names, last(), addedGlobals());
  tail = Object.keys(digests).filter((name) => previous[name] !== ABSENT && digests[name] !== previous[name]);
});

process.on("exit", (code) => {
  const after = standardDigests();
  const patched = [...standardBefore].filter(([label, digest]) => after.get(label) !== digest).map(([label]) => label);
  writeFileSync(process.env.ORACLE_OUT, stringify({ code, units, tail: tail ?? [], patched }));
});

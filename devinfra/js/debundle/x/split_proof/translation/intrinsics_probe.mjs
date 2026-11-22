// Preload (`node --import intrinsics_probe.mjs program.mjs`) that counts which standard functions a program calls. It
// replaces every function the keys name by a Proxy that counts the call and forwards it, and writes
// `{ counts: { key: calls }, unavailable: [key] }` to `$PROBE_OUT` when the process exits. `$PROBE_KEYS` is a JSON list
// of keys in the spelling of `Intrinsics.lean`: `Object.keys`, `Array.prototype.map`, `prim.trim` (a method of a
// string, number, boolean, symbol or bigint), `env:console.log` (the environment), `Map.prototype.next` (the `next` of
// the iterator of a map) and the iterator helpers of a map or a set (`Set.prototype.map`).
//
// Only calls made from the program's own file count (`$PROBE_PROGRAM`, its file URL): Node's loader and its console call
// standard functions too. The caller is read from the stack, so a callback of the program that calls `Object.keys`
// counts and a call that a standard function makes on its own does not.
//
// A measurement, not a fixture: a call the engine makes on its own (the iterator behind a spread, a conversion) is not
// counted; a function that is not configurable, and an accessor, is not wrapped (listed as `unavailable`); a program
// that compares a standard function with another value sees the proxy.
import { readFileSync, writeFileSync } from "node:fs";

// What the probe uses after it starts replacing standard functions, captured before.
const { apply, construct, getPrototypeOf, getOwnPropertyDescriptor, defineProperty } = Reflect;
const { stringify } = JSON;
const { hasOwn } = Object;
const { captureStackTrace } = Error;
const objectToString = Object.prototype.toString;
const ProxyConstructor = Proxy;
const WeakMapConstructor = WeakMap;
const wmGet = WeakMap.prototype.get;
const wmSet = WeakMap.prototype.set;

const keys = JSON.parse(readFileSync(process.env.PROBE_KEYS, "utf8"));
const programUrl = process.env.PROBE_PROGRAM;
const counts = Object.create(null);
const unavailable = [];
const isObject = (v) => v !== null && (typeof v === "object" || typeof v === "function");

function calledByProgram(skip) {
  const holder = {};
  const previous = Error.prepareStackTrace;
  Error.prepareStackTrace = (_, frames) => frames;
  captureStackTrace(holder, skip);
  const frames = holder.stack;
  Error.prepareStackTrace = previous;
  // the frames above `skip` (the trap) and the trap are left out: the first one is whoever called through the proxy
  return frames.length > 0 && frames[0].getFileName() === programUrl;
}

// A static function of a constructor that was replaced by a proxy sees the proxy as `this`: `Promise.resolve(p)` would
// build a new promise instead of returning `p`. It gets the real constructor.
const originals = new WeakMapConstructor();
const realThis = (v) => (isObject(v) ? (apply(wmGet, originals, [v]) ?? v) : v);

/** `labels`: keys to count, or functions from the receiver to the key to count (`null`: none). */
function wrap(fn, labels) {
  const hit = (thisArg, trap) => {
    if (!calledByProgram(trap)) return;
    for (const label of labels) {
      const key = typeof label === "function" ? label(thisArg) : label;
      if (key !== null) counts[key] = (counts[key] ?? 0) + 1;
    }
  };
  const proxy = new ProxyConstructor(fn, {
    apply: function applyTrap(target, thisArg, args) {
      hit(thisArg, applyTrap);
      return apply(target, realThis(thisArg), args);
    },
    construct: function constructTrap(target, args, newTarget) {
      hit(undefined, constructTrap);
      return construct(target, args, newTarget === proxy ? target : newTarget);
    },
  });
  apply(wmSet, originals, [proxy, fn]);
  return proxy;
}

const walk = (segments) => {
  let value = globalThis;
  for (const segment of segments) {
    if (value === null || value === undefined) return undefined;
    value = value[segment];
  }
  return value;
};

// First everything is looked up, then anything is replaced: the lookups use standard functions themselves. A function
// that several keys name (`parseInt` and `Number.parseInt`, `prim.trim` and `String.prototype.trim`) gets one proxy that
// counts each of them.
const PRIMITIVE_PROTOTYPES = [
  String.prototype,
  Number.prototype,
  Boolean.prototype,
  Symbol.prototype,
  BigInt.prototype,
];
const COLLECTION_HELPERS = ["map", "filter", "flatMap", "take", "drop", "reduce", "toArray", "some", "every", "find"];
const jobs = new Map(); // holder -> prop -> { holder, prop, descriptor, labels }
const want = (key, holder, prop, label = key) => {
  let owner = holder;
  while (owner !== null && owner !== undefined && !hasOwn(owner, prop)) owner = getPrototypeOf(owner);
  const descriptor = owner === null || owner === undefined ? undefined : getOwnPropertyDescriptor(owner, prop);
  if (!descriptor || typeof descriptor.value !== "function" || (owner === holder && !descriptor.configurable)) {
    unavailable.push(key);
    return;
  }
  if (!jobs.has(holder)) jobs.set(holder, Object.create(null));
  const byProp = jobs.get(holder);
  byProp[prop] ??= { holder, prop, descriptor, labels: [] };
  byProp[prop].labels.push(label);
};
for (const key of keys) {
  const segments = key.split(".");
  if (segments.at(-1) === "constructor") continue; // reached by the engine (species lookups), not called by name
  if (key.startsWith("prim.")) {
    // any method of a string, number, boolean, symbol or bigint with this name
    const found = PRIMITIVE_PROTOTYPES.filter((proto) => hasOwn(proto, segments[1]));
    if (found.length === 0) unavailable.push(key);
    for (const proto of found) want(key, proto, segments[1]);
  } else if (key === "Map.prototype.next" || key === "Set.prototype.next") {
    want(key, getPrototypeOf(key.startsWith("Map") ? new Map().entries() : new Set().values()), "next");
  } else if (
    /^(Map|Set)\.prototype\./.test(key) &&
    COLLECTION_HELPERS.includes(segments[2]) &&
    !hasOwn(globalThis[segments[0]].prototype, segments[2])
  ) {
    // the helpers of the iterator of a map or a set are on `Iterator.prototype`: a call counts for the collection of the iterator
    want(key, globalThis.Iterator?.prototype, segments[2], (receiver) => {
      const tag = isObject(receiver) ? apply(objectToString, receiver, []) : "";
      const collection = /^\[object (Map|Set) Iterator\]$/.exec(tag)?.[1];
      return collection === segments[0] ? key : null;
    });
  } else {
    const path = key.startsWith("env:") ? key.slice(4).split(".") : segments;
    const holder = walk(path.slice(0, -1));
    if (holder === undefined || holder === null) unavailable.push(key);
    else want(key, holder, path.at(-1));
  }
}
for (const byProp of jobs.values())
  for (const job of Object.values(byProp))
    defineProperty(job.holder, job.prop, { ...job.descriptor, value: wrap(job.descriptor.value, job.labels) });

process.on("exit", () => {
  writeFileSync(process.env.PROBE_OUT, stringify({ counts, unavailable }));
});

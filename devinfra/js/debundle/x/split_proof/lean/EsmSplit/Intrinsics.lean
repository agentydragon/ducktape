import Lean.Data.Json
import Std.Data.HashMap
import Std.Data.HashSet
import EsmSplit.IntrinsicMembers

/-!
# What the standard library does, as data

The effect analysis (`Effects.lean`) runs a bundle's top-level code abstractly. A call to a function it cannot see
into is a call into *the world*: it may do anything. This file lists the standard functions that are not: what they
change, what they store where, what they return.

**Premise P1: nothing the analysis cannot see patches the intrinsics.** `Object.keys` is the function this table
describes, and `Object.prototype` has the members it has. A patch the bundle makes itself is seen: a store, a
definition, a deletion, a change of prototype or an accessor on an intrinsic reached by name or along a prototype
chain (`Object.prototype.x = …`, `Object.defineProperty(Array.prototype, …)`, `Reflect.set(…)`,
`Array.prototype.__proto__.x = …`; not through a value that may also be another object), and a store into a global
that names an intrinsic (`Array = …`, `globalThis.Promise = …`). In code that is not run (a generator body, a call
deeper than the analysis follows, a callback of a function without a row, a `Proxy` trap, a unit it gave up on) the
text is read for the same shapes. Any unit may read what a patch changes, so then every unit that is not a function
declaration is treated as touching the world (`Effects.footprints`); for a static member (`Math.max = …`) it is the
units that read it. Not seen: a patch through an alias or a computed name in code that is not run, a standard
function forwarded through a spread or called through a computed name, `Object.defineProperty(globalThis, …)`, a
patch by another script loaded before or during the bundle, one through a value the world returned, and state the
engine keeps (`RegExp.$1`). `effects/debug/p1_known_gaps` has a program for each.

**Premise P3: the world defines no hooks.** The world (the environment and every module outside the bundle) adds no
accessor and no conversion hook (`valueOf`, `toString`, `Symbol.toPrimitive`) to an object the program made. So a
property of an object the world holds is read and assigned like any other. `Premises.noWorldHooks` switches this
premise; off, reading, assigning or converting an object the world holds is a call into the world. Code of the
program that the world runs can break it (finding F7), so it is off for real inputs. Accessors and hooks
the *program* defined are found and run like any call. The premise is about objects the analysis follows. A value the
world *made* is not one: it may be a `Proxy` with a handler of the program, or an object of the program that came
back, so every operation on it is unknown code (`Effects.worldValue`).

Every call of a row also converts the receiver and the arguments to primitives, as the standard functions do for
numbers, strings and keys (`Math.floor(x)`, `String(x)`, `o.hasOwnProperty(k)`): a `valueOf` or `toString` the program
defined is run (`Effects.convert`).

Positions in a summary: `0` is the receiver (`this`), `i + 1` is argument `i`.

A standard function may also run the program's code through a protocol: it calls a callback (`calls`), iterates its
argument (`iterates`), reads the properties of an argument, so that a getter runs (`readsContents`, `copiesRest`), or calls a
method by name (`callsMethods`: `test` calls `exec`, `replace` calls the `Symbol.replace` of its pattern). A row says
which, and nothing else runs. A protocol the row does not list is a limit of the rules:
`effects/debug/general_known_gaps` has a program for each one known.

Not listed means a world call. A row is added when a real bundle needs it, with the reason it is safe.
-/

namespace Effects

/-- What a call returns. -/
inductive Ret where
  /-- a primitive -/
  | prim
  /-- the receiver -/
  | thisV
  /-- argument `i` -/
  | arg (i : Nat)
  /-- whatever the objects at the position contain -/
  | contents (pos : Nat)
  /-- a new array holding what the objects at the position contain -/
  | freshOf (pos : Nat)
  /-- a new array holding what the callbacks return -/
  | freshOfCalls
  /-- a new array of primitives -/
  | freshPrims
  /-- a new plain object whose prototype is the object at the position -/
  | freshProto (pos : Nat)
  /-- the prototypes of the objects at the position -/
  | protoOf (pos : Nat)
  /-- a new plain object holding the keys and values inside the entries at the position -/
  | freshOfEntries (pos : Nat)
  /-- a property descriptor of the object at the position: it holds what that object holds -/
  | descriptorOf (pos : Nat)
  /-- a new instance whose prototype is the intrinsic `path`, holding every argument -/
  | inst (path : String)
  /-- a new array holding the arguments -/
  | freshArgs
  /-- a new array holding what the receiver and the arguments hold, and the arguments that are not arrays (`concat`) -/
  | concatLike
  /-- a new array holding what the objects at the position hold, at any depth (`flat`) -/
  | flatLike (pos : Nat)
  /-- a new array of arrays holding what the objects at the position hold, and keys (`Object.entries`) -/
  | entriesOf (pos : Nat)
  /-- a new object `{ value, done }` whose `value` holds what iterating the objects at the position yields (what `next`
      returns) -/
  | iterResultOf (pos : Nat)
  /-- a new array holding what the callbacks return and what those hold (`flatMap`) -/
  | freshOfCallsFlat
  deriving Inhabited

structure Summary where
  ret    : Ret := .prim
  /-- positions whose objects it changes -/
  muts   : List Nat := []
  /-- every argument is stored into the receiver -/
  storeArgs : Bool := false
  /-- `(from, to)`: what is at `from` is stored into the objects at `to` -/
  store  : List (Nat × Nat) := []
  /-- positions of functions it calls before returning, with unknown arguments -/
  calls  : List Nat := []
  /-- positions of functions it calls before returning, with functions the world made as arguments (a promise's
      executor gets `resolve` and `reject`) -/
  callsWithWorld : List Nat := []
  /-- the descriptor at position 3 defines a property of the object at position 1 -/
  define : Bool := false
  /-- the objects at position 2 become prototypes of the objects at position 1 -/
  setProto : Bool := false
  /-- positions it iterates (`Symbol.iterator`): an object the program made, other than an array or a collection, may
      run program code to be iterated -/
  iterates : List Nat := []
  /-- positions it iterates if they have an iterator, and reads as array-likes otherwise -/
  optionalIterates : List Nat := []
  /-- positions whose contents it converts to strings (`join`) -/
  convertsContents : List Nat := []
  /-- the call reads or changes the environment (the console, the clock, a listener table): an effect that orders it
      against the other such effects. It does not run code of the program, and touches no object but the ones below. -/
  world  : Bool := false
  /-- positions the environment keeps: the objects there escape -/
  retains : List Nat := []
  /-- positions whose properties it reads, so that a getter the program defined runs (`Object.prototype.toString`
      reads `Symbol.toStringTag`) -/
  readsContents : List Nat := []
  /-- `(to, f)`: the function at `f` becomes an accessor of the objects at `to` (`__defineGetter__`) -/
  accessors : List (Nat × Nat) := []
  /-- the callback result feeds the next call (`reduce`): it runs twice -/
  accumulates : Bool := false
  /-- what the callbacks return may be returned (`reduce`, a `mapFn`) -/
  returnsCalls : Bool := false
  /-- positions whose values may be returned (the initial value of `reduce`) -/
  alsoReturns : List Nat := []
  /-- the object at this position maps names to descriptors that define properties of the first argument -/
  defineMap : Option Nat := none
  /-- `(first, to)`: the properties of the objects at every position from `first` on are copied into the objects at `to` -/
  copiesRest : Option (Nat × Nat) := none
  /-- `(p, m, as)`: the method named `m` of the objects at `p`, if the program defined one, is called with the values at
      the positions `as`, as the standard function does through its protocol (`RegExp.prototype.test` calls `exec`) -/
  callsMethods : List (Nat × String × List Nat) := []
  deriving Inhabited

open Ret in
/-- Rows by path. `X.prototype.m` is a method of every `X` instance; `prim.m` a method of a string, number, boolean,
symbol or bigint. `Function.prototype.call`, `apply` and `bind` have rows so that reading them yields the function; the
analysis runs them itself (`callBuiltin`). -/
def table : Std.HashMap String Summary := Std.HashMap.ofList [
  -- Object
  ("Object.keys", { ret := freshPrims }),
  ("Object.getOwnPropertyNames", { ret := freshPrims }),
  ("Object.getOwnPropertySymbols", { ret := freshPrims }),
  ("Object.values", { ret := freshOf 1 }),
  ("Object.entries", { ret := entriesOf 1 }),
  ("Object.fromEntries", { ret := freshOfEntries 1, iterates := [1] }),
  ("Object.create", { ret := freshProto 1 }),
  ("Object.getPrototypeOf", { ret := protoOf 1 }),
  ("Object.getOwnPropertyDescriptor", { ret := descriptorOf 1 }),
  ("Object.defineProperty", { ret := arg 0, muts := [1], define := true }),
  ("Object.defineProperties", { ret := arg 0, muts := [1], defineMap := some 2 }),
  ("Object.is", {}),
  ("Object.isFrozen", {}), ("Object.isSealed", {}), ("Object.isExtensible", {}),
  ("Object.freeze", { ret := arg 0, muts := [1] }),
  ("Object.seal", { ret := arg 0, muts := [1] }),
  ("Object.preventExtensions", { ret := arg 0, muts := [1] }),
  ("Object.assign", { ret := arg 0, muts := [1], copiesRest := some (2, 1) }),
  ("Object.setPrototypeOf", { ret := arg 0, muts := [1], setProto := true }),
  ("Object.prototype.hasOwnProperty", {}),
  ("Object.prototype.isPrototypeOf", {}),
  ("Object.prototype.propertyIsEnumerable", {}),
  ("Object.prototype.toString", { readsContents := [0] }), ("Object.prototype.toLocaleString", { callsMethods := [(0, "toString", [])] }),
  ("Object.prototype.__defineGetter__", { muts := [0], accessors := [(0, 2)] }),
  ("Object.prototype.__defineSetter__", { muts := [0], accessors := [(0, 2)] }),
  ("Object.prototype.__lookupGetter__", { ret := contents 0 }), ("Object.prototype.__lookupSetter__", { ret := contents 0 }),
  -- Reflect: the same as the `Object` functions with the same names, with the target first
  -- `Reflect.get` and `Reflect.set` have dedicated receiver handling in `Effects.callBuiltin`.
  ("Reflect.get", {}), ("Reflect.has", {}), ("Reflect.ownKeys", { ret := freshPrims }),
  ("Reflect.getPrototypeOf", { ret := protoOf 1 }), ("Reflect.getOwnPropertyDescriptor", { ret := descriptorOf 1 }),
  ("Reflect.isExtensible", {}),
  ("Reflect.set", { muts := [1], store := [(3, 1)] }),
  ("Reflect.defineProperty", { muts := [1], define := true }),
  ("Reflect.setPrototypeOf", { muts := [1], setProto := true }),
  ("Reflect.deleteProperty", { muts := [1] }), ("Reflect.preventExtensions", { muts := [1] }),
  ("Function.prototype.call", {}), ("Function.prototype.apply", {}), ("Function.prototype.bind", {}),
  ("Function.prototype.toString", {}), ("Array.prototype.toString", { convertsContents := [0] }),
  ("RegExp.prototype.toString", {}),
  ("Object.prototype.valueOf", { ret := thisV }),
  -- Symbol, JSON, Math, conversions
  ("Symbol", {}), ("Symbol.for", {}), ("Symbol.keyFor", {}),
  ("Math.abs", {}), ("Math.ceil", {}), ("Math.floor", {}), ("Math.round", {}), ("Math.trunc", {}), ("Math.sign", {}),
  ("Math.max", {}), ("Math.min", {}), ("Math.pow", {}), ("Math.sqrt", {}), ("Math.log", {}), ("Math.exp", {}),
  ("Math.random", { world := true }),
  ("Math.PI", {}), ("Math.E", {}),
  ("Number", {}), ("String", {}), ("Boolean", {}), ("BigInt", {}),
  ("Number.isInteger", {}), ("Number.isFinite", {}), ("Number.isNaN", {}), ("Number.parseInt", {}),
  ("Number.parseFloat", {}), ("Number.isSafeInteger", {}),
  ("Number.MAX_SAFE_INTEGER", {}), ("Number.MIN_SAFE_INTEGER", {}), ("Number.EPSILON", {}),
  ("String.fromCharCode", {}), ("String.fromCodePoint", {}), ("String.raw", {}),
  ("parseInt", {}), ("parseFloat", {}), ("isNaN", {}), ("isFinite", {}),
  ("encodeURIComponent", {}), ("decodeURIComponent", {}), ("encodeURI", {}), ("decodeURI", {}),
  -- Array
  ("Array", { ret := freshArgs }),
  ("Array.isArray", {}),
  ("Array.of", { ret := freshArgs }),
  ("Array.from", { ret := freshOf 1, calls := [2], optionalIterates := [1], returnsCalls := true }),
  ("Array.prototype.push", { muts := [0], storeArgs := true }),
  ("Array.prototype.unshift", { muts := [0], storeArgs := true }),
  ("Array.prototype.pop", { ret := contents 0, muts := [0] }),
  ("Array.prototype.shift", { ret := contents 0, muts := [0] }),
  ("Array.prototype.splice", { ret := freshOf 0, muts := [0], storeArgs := true }),
  ("Array.prototype.reverse", { ret := thisV, muts := [0] }),
  ("Array.prototype.sort", { ret := thisV, muts := [0], calls := [1], convertsContents := [0] }),
  ("Array.prototype.fill", { ret := thisV, muts := [0], store := [(1, 0)] }),
  ("Array.prototype.slice", { ret := freshOf 0 }),
  ("Array.prototype.concat", { ret := concatLike }),
  ("Array.prototype.flat", { ret := flatLike 0 }),
  ("Array.prototype.filter", { ret := freshOf 0, calls := [1] }),
  ("Array.prototype.map", { ret := freshOfCalls, calls := [1] }),
  ("Array.prototype.flatMap", { ret := freshOfCallsFlat, calls := [1] }),
  ("Array.prototype.forEach", { calls := [1] }),
  ("Array.prototype.some", { calls := [1] }), ("Array.prototype.every", { calls := [1] }),
  ("Array.prototype.find", { ret := contents 0, calls := [1] }),
  ("Array.prototype.findLast", { ret := contents 0, calls := [1] }),
  ("Array.prototype.findIndex", { calls := [1] }),
  ("Array.prototype.reduce", { ret := contents 0, calls := [1], accumulates := true, returnsCalls := true, alsoReturns := [2] }),
  -- a search reads the length and the elements of its receiver, which need not be an array
  ("Array.prototype.includes", { readsContents := [0] }), ("Array.prototype.indexOf", { readsContents := [0] }),
  ("Array.prototype.lastIndexOf", { readsContents := [0] }),
  ("Array.prototype.join", { convertsContents := [0] }), ("Array.prototype.at", { ret := contents 0 }),
  -- Map, Set, WeakMap, WeakSet
  ("Map", { ret := inst "Map.prototype", iterates := [1] }), ("Set", { ret := inst "Set.prototype", iterates := [1] }),
  ("WeakMap", { ret := inst "Map.prototype", iterates := [1] }), ("WeakSet", { ret := inst "Set.prototype", iterates := [1] }),
  ("Map.prototype.get", { ret := contents 0 }),
  ("Map.prototype.set", { ret := thisV, muts := [0], storeArgs := true }),
  ("Map.prototype.has", {}), ("Map.prototype.delete", { muts := [0] }), ("Map.prototype.clear", { muts := [0] }),
  ("Map.prototype.forEach", { calls := [1] }),
  -- the iterator of a collection is the collection: a live view of it, and `next` moves its cursor. The helpers of an
  -- iterator (`map`, `some`, `toArray`, …) have no rows: `Effects.getVal` finds them as members without a summary
  ("Map.prototype.keys", { ret := thisV }), ("Map.prototype.values", { ret := thisV }),
  ("Map.prototype.entries", { ret := thisV }), ("Map.prototype.next", { ret := iterResultOf 0, muts := [0] }),
  ("Set.prototype.add", { ret := thisV, muts := [0], storeArgs := true }),
  ("Set.prototype.has", {}), ("Set.prototype.delete", { muts := [0] }), ("Set.prototype.clear", { muts := [0] }),
  ("Set.prototype.forEach", { calls := [1] }),
  ("Set.prototype.keys", { ret := thisV }), ("Set.prototype.values", { ret := thisV }),
  ("Set.prototype.entries", { ret := thisV }), ("Set.prototype.next", { ret := iterResultOf 0, muts := [0] }),
  -- errors, regular expressions
  -- the options of an error constructor are read for `cause`
  ("Error", { ret := inst "Error.prototype", readsContents := [2] }),
  ("TypeError", { ret := inst "Error.prototype", readsContents := [2] }),
  ("RangeError", { ret := inst "Error.prototype", readsContents := [2] }),
  ("SyntaxError", { ret := inst "Error.prototype", readsContents := [2] }),
  ("ReferenceError", { ret := inst "Error.prototype", readsContents := [2] }),
  ("Error.prototype.toString", { readsContents := [0] }),
  ("RegExp", { ret := inst "RegExp.prototype" }),
  ("RegExp.prototype.test", { muts := [0], callsMethods := [(0, "exec", [1])] }),
  ("RegExp.prototype.exec", { ret := freshPrims, muts := [0] }),
  -- web platform constructors that only allocate
  ("TextEncoder", { ret := inst "TextEncoder.prototype" }), ("TextDecoder", { ret := inst "TextDecoder.prototype" }),
  ("URLSearchParams", { ret := inst "URLSearchParams.prototype" }),
  ("DOMException", { ret := inst "Error.prototype" }),
  -- the options of a formatter are read: a getter the program defined runs
  ("Intl.DateTimeFormat", { ret := inst "Intl.DateTimeFormat.prototype", readsContents := [2] }),
  ("Intl.NumberFormat", { ret := inst "Intl.NumberFormat.prototype", readsContents := [2] }),
  ("Intl.Collator", { ret := inst "Intl.Collator.prototype", readsContents := [2] }),
  -- Promise: the executor runs now; the callbacks of `then` run after the program, and so does the `then` of a thenable
  -- that is resolved, in the order the promises were made: resolving one is an effect
  ("Promise", { ret := inst "Promise.prototype", callsWithWorld := [1] }),
  ("Promise.resolve", { ret := inst "Promise.prototype", world := true, retains := [1] }),
  ("Promise.reject", { ret := inst "Promise.prototype" }),
  -- the environment of a browser or of Node: effects on the world, no code of the program runs
  ("env:console.log", { world := true }), ("env:console.warn", { world := true }),
  ("env:console.error", { world := true }), ("env:console.info", { world := true }),
  ("env:console.debug", { world := true }), ("env:console.trace", { world := true }),
  ("env:console.group", { world := true }), ("env:console.groupEnd", { world := true }),
  ("env:console.table", { world := true }), ("env:console.time", { world := true }),
  ("env:console.timeEnd", { world := true }),
  ("env:window.addEventListener", { world := true, retains := [2] }),
  ("env:window.removeEventListener", { world := true }),
  ("env:document.addEventListener", { world := true, retains := [2] }),
  ("env:document.removeEventListener", { world := true }),
  ("env:window.setTimeout", { world := true, retains := [1] }), ("env:setTimeout", { world := true, retains := [1] }),
  ("env:window.setInterval", { world := true, retains := [1] }), ("env:setInterval", { world := true, retains := [1] }),
  ("env:clearTimeout", { world := true }), ("env:clearInterval", { world := true }),
  ("env:window.requestAnimationFrame", { world := true, retains := [1] }),
  ("env:requestAnimationFrame", { world := true, retains := [1] }),
  ("env:queueMicrotask", { world := true, retains := [1] }),
  ("env:performance.now", { world := true }), ("env:Date.now", { world := true }),
  ("env:localStorage.getItem", { world := true }), ("env:localStorage.setItem", { world := true }),
  ("env:localStorage.removeItem", { world := true }),
  ("env:sessionStorage.getItem", { world := true }), ("env:sessionStorage.setItem", { world := true }),
  ("env:sessionStorage.removeItem", { world := true }),
  -- primitives: strings, numbers, booleans, symbols, bigints
  ("prim.toString", {}), ("prim.valueOf", {}), ("prim.toLowerCase", {}), ("prim.toUpperCase", {}),
  ("prim.toLocaleLowerCase", {}), ("prim.toLocaleUpperCase", {}), ("prim.toLocaleString", {}),
  ("prim.trim", {}), ("prim.trimStart", {}), ("prim.trimEnd", {}),
  ("prim.slice", {}), ("prim.substring", {}), ("prim.substr", {}), ("prim.charAt", {}), ("prim.charCodeAt", {}),
  ("prim.codePointAt", {}), ("prim.at", {}), ("prim.indexOf", {}), ("prim.lastIndexOf", {}), ("prim.includes", {}),
  ("prim.startsWith", {}), ("prim.endsWith", {}), ("prim.padStart", {}), ("prim.padEnd", {}), ("prim.repeat", {}),
  ("prim.normalize", {}), ("prim.localeCompare", {}), ("prim.concat", {}), ("prim.toFixed", {}),
  ("prim.toPrecision", {}), ("prim.description", {}),
  -- a pattern that is an object with a `Symbol.search`, `split`, `match` or `replace` method gets the string. (`matchAll`
  -- returns an iterator, which has no model: no row)
  ("prim.search", { muts := [1], callsMethods := [(1, "@@search", [0])] }),
  ("prim.split", { ret := freshPrims, muts := [1], callsMethods := [(1, "@@split", [0, 2])] }),
  ("prim.match", { ret := freshPrims, muts := [1], callsMethods := [(1, "@@match", [0])] }),
  ("prim.replace", { calls := [2], muts := [1], callsMethods := [(1, "@@replace", [0, 2])] }),
  ("prim.replaceAll", { calls := [2], muts := [1], callsMethods := [(1, "@@replace", [0, 2])] })
]

/-- Every own property of the standard objects, as `Path.member` (`prim.member` for what a string, number, boolean,
symbol or bigint has), generated from Node by `effects/gen_members.mjs`. -/
def members : Std.HashSet String := Std.HashSet.ofList memberPaths

/-- Globals that name an intrinsic object or function. Everything else undeclared is the environment. -/
def intrinsicGlobals : Std.HashSet String := Std.HashSet.ofList [
  "Object", "Array", "Symbol", "Map", "Set", "WeakMap", "WeakSet", "Promise", "Error", "TypeError", "RangeError",
  "SyntaxError", "ReferenceError", "EvalError", "URIError", "AggregateError", "RegExp", "JSON", "Math", "Number", "String",
  "Boolean", "BigInt", "Intl", "Function", "Reflect", "Atomics", "WeakRef", "FinalizationRegistry", "Iterator",
  "ArrayBuffer", "SharedArrayBuffer", "DataView", "Int8Array", "Uint8Array", "Uint8ClampedArray", "Int16Array",
  "Uint16Array", "Int32Array", "Uint32Array", "Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array",
  "escape", "unescape",
  "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
  "TextEncoder", "TextDecoder", "URLSearchParams", "DOMException"]

/-- The namespace objects: they are not functions, and inherit from `Object.prototype`. -/
def namespaces : List String := ["Math", "JSON", "Reflect", "Atomics", "Intl"]

/-- The error constructors: each inherits the statics of `Error`, and each prototype inherits `Error.prototype`. -/
def errorTypes : List String :=
  ["TypeError", "RangeError", "SyntaxError", "ReferenceError", "EvalError", "URIError", "AggregateError"]

/-- The prototype of an intrinsic object, by path (`none` for `Object.prototype`, which has none): `Array.prototype` and
the namespaces inherit from `Object.prototype`, `TypeError` from `Error`, and every other function, a constructor or a
method, from `Function.prototype`. -/
def builtinProto (path : String) : Option String :=
  if path == "Object.prototype" then none
  else if path == "Function.prototype" then some "Object.prototype"
  else if path.endsWith ".prototype" then
    if errorTypes.contains (path.toRawSubstring.dropRight 10).toString then some "Error.prototype" else some "Object.prototype"
  else if namespaces.contains path then some "Object.prototype"
  else if errorTypes.contains path then some "Error"
  else some "Function.prototype"

/-- The paths of the prototypes of a string, a number, a boolean, a symbol and a bigint: a primitive may be any of them. -/
def primitivePrototypes : List String :=
  ["String.prototype", "Number.prototype", "Boolean.prototype", "Symbol.prototype", "BigInt.prototype"]

/-- One intrinsic object has more than one path: an error constructor inherits the statics of `Error`, and `parseFloat`
is `Number.parseFloat`. A patch and a read of a member meet under the same path. -/
def canonPath (path : String) : String :=
  if errorTypes.contains path then "Error"
  else if path == "parseFloat" then "Number.parseFloat"
  else if path == "parseInt" then "Number.parseInt"
  else path

/-- Globals that name an object or a function of the environment. Reading one is not an effect; reading what it holds is,
and a call of one of its listed members is the effect its row says. -/
def envGlobals : Std.HashSet String := Std.HashSet.ofList [
  "console", "window", "document", "navigator", "localStorage", "sessionStorage", "performance", "location", "history",
  "self", "globalThis", "Date", "setTimeout", "setInterval", "clearTimeout", "clearInterval", "requestAnimationFrame",
  "cancelAnimationFrame", "queueMicrotask"]

/-- Values of globals that are primitives. -/
def primGlobals : Std.HashSet String := Std.HashSet.ofList ["undefined", "NaN", "Infinity"]

end Effects

import EsmSplit.CoreJS.HostLayer

namespace CoreJS.ECMAScriptHost

private def globalPremise (name : String) :=
  s!"the ambient {name} binding is the unmodified ECMAScript intrinsic throughout initialization"

private def intrinsicFunction (name : String) (worldCall := false) : HostGlobalEntry :=
  ⟨name, some "function", some ("intrinsic." ++ name), false, false, false, worldCall,
    some (globalPremise name)⟩

private def intrinsicObject (name : String) : HostGlobalEntry :=
  ⟨name, some "object", some ("intrinsic." ++ name), false, false, false, false,
    some (globalPremise name)⟩

def globalFunctions : List String :=
  ["AggregateError", "Array", "ArrayBuffer", "BigInt", "BigInt64Array", "BigUint64Array", "SharedArrayBuffer",
   "Boolean", "DataView", "Date", "Error", "EvalError", "FinalizationRegistry", "Float16Array",
   "Float32Array", "Float64Array", "Function", "Int16Array", "Int32Array", "Int8Array", "Iterator",
   "Map", "Number", "Object", "Promise", "Proxy", "RangeError", "ReferenceError", "RegExp", "Set",
   "String", "SuppressedError", "Symbol", "SyntaxError", "TypeError", "URIError", "Uint16Array",
   "Uint32Array", "Uint8Array", "Uint8ClampedArray", "WeakMap", "WeakRef", "WeakSet", "decodeURI",
   "decodeURIComponent", "encodeURI", "encodeURIComponent", "eval", "escape", "isFinite", "isNaN",
   "parseFloat", "parseInt", "unescape"]

def globalObjects : List String := ["Atomics", "Intl", "JSON", "Math", "Reflect", "WebAssembly"]

def globalEntries : List HostGlobalEntry :=
  (globalFunctions.map fun name => intrinsicFunction name
    (name == "Boolean" || name == "Number" || name == "isFinite" || name == "isNaN" ||
      name == "parseFloat" || name == "parseInt" || name == "eval")) ++
  (globalObjects.map intrinsicObject)

-- These constructors can be retained opaquely by profiles that opt into the
-- deferred-constructor capability. The names live here because they are
-- ECMAScript globals, not browser globals.
def opaqueConstructorNames : List String := ["Date", "Error", "RegExp", "Set"]

private def memberPremise (owner key : String) (worldCall : Bool) : String :=
  if worldCall then
    s!"the listed {owner} host calls are provided by their unmodified ECMAScript intrinsic methods throughout initialization"
  else
    s!"the ambient ECMAScript intrinsic {owner} provides its unmodified {key} property throughout initialization"

private def member (owner key : String) (worldCall := false) (primitive : Option Prim := none) : HostMemberEntry :=
  ⟨owner, key, some ("intrinsic." ++ owner ++ "." ++ key), worldCall, primitive, none,
    some (memberPremise owner key worldCall)⟩

private def mathConstant (key : String) (bits : UInt64) : HostMemberEntry :=
  ⟨"Math", key, none, false, none, some (.number bits),
    some s!"the own Math.{key} data property returns a Number with binary64 bits {bits.toNat} throughout initialization"⟩

private def membersFor (owner : String) (keys : List String) (world : List String := []) : List HostMemberEntry :=
  keys.map fun key => member owner key (world.contains key)

def mathConstants : List String := ["PI"]

def mathConstantEntries : List HostMemberEntry := [mathConstant "PI" 0x400921fb54442d18]

private def mathWorldOnly : List String := ["acosh", "asinh", "atanh", "tanh"]

private def mathMembers : List HostMemberEntry :=
  ["abs", "acos", "acosh", "asin", "asinh", "atan", "atanh", "atan2", "cbrt", "ceil", "clz32",
   "cos", "cosh", "exp", "expm1", "floor", "fround", "hypot", "imul", "log", "log10", "log1p",
   "log2", "max", "min", "pow", "random", "round", "sign", "sin", "sinh", "sqrt", "tan", "tanh",
   "trunc"].map fun key =>
      if mathWorldOnly.contains key then
        ⟨"Math", key, none, true, none, none, some (memberPremise "Math" key true)⟩
      else member "Math" key true

def mathFunctions : List String := mathMembers.map (·.key)

def numberStaticFunctions : List String :=
  ["isFinite", "isInteger", "isNaN", "isSafeInteger", "parseFloat", "parseInt"]

def stringStaticFunctions : List String := ["fromCharCode", "fromCodePoint"]

def stringPrototypeNames : List String :=
  ["length", "constructor", "anchor", "at", "big", "blink", "bold", "charAt", "charCodeAt", "codePointAt",
   "concat", "endsWith", "fontcolor", "fontsize", "fixed", "includes", "indexOf", "isWellFormed", "italics",
   "lastIndexOf", "link", "localeCompare", "match", "matchAll", "normalize", "padEnd", "padStart", "repeat",
   "replace", "replaceAll", "search", "slice", "small", "split", "strike", "sub", "substr", "substring", "sup",
   "startsWith", "toString", "toWellFormed", "trim", "trimStart", "trimLeft", "trimEnd", "trimRight",
   "toLocaleLowerCase", "toLocaleUpperCase", "toLowerCase", "toUpperCase", "valueOf"]

def numberPrototypeNames : List String :=
  ["constructor", "toExponential", "toFixed", "toPrecision", "toString", "valueOf", "toLocaleString"]

def booleanPrototypeNames : List String := ["constructor", "toString", "valueOf"]

def objectPrototypeNames : List String :=
  ["constructor", "__defineGetter__", "__defineSetter__", "hasOwnProperty", "__lookupGetter__", "__lookupSetter__",
   "isPrototypeOf", "propertyIsEnumerable", "toString", "valueOf", "toLocaleString"]

def functionPrototypeNames : List String := ["apply", "bind", "call", "constructor", "toString"]

def arrayPrototypeNames : List String :=
  ["constructor", "at", "concat", "copyWithin", "fill", "find", "findIndex", "findLast", "findLastIndex",
   "lastIndexOf", "pop", "push", "reverse", "shift", "unshift", "slice", "sort", "splice", "includes",
   "indexOf", "join", "keys", "entries", "values", "forEach", "filter", "flat", "flatMap", "map", "every",
   "some", "reduce", "reduceRight", "toReversed", "toSorted", "toSpliced", "with", "toLocaleString", "toString"]

def worldStringPrototypeMethods : List String :=
  ["at", "charAt", "charCodeAt", "codePointAt", "concat", "endsWith", "includes", "indexOf", "isWellFormed",
   "lastIndexOf", "localeCompare", "normalize", "padEnd", "padStart", "repeat", "replace", "replaceAll", "search",
   "slice", "startsWith", "substr", "substring", "toLowerCase", "toString", "toUpperCase", "toWellFormed", "trim",
   "trimEnd", "trimLeft", "trimRight", "trimStart", "valueOf"]

def worldNumberPrototypeMethods : List String := ["toExponential", "toFixed", "toPrecision", "toString", "valueOf"]

def worldBooleanPrototypeMethods : List String := ["toString", "valueOf"]

def hostFunctions : List String :=
  globalEntries.filterMap fun entry => if entry.worldCall then some entry.name else none

def qualified (table : List (String × List String)) : List String :=
  table.flatMap fun (owner, members) => members.map fun key => s!"{owner}.{key}"

def staticMembers : List HostMemberEntry :=
  membersFor "Array" ["from", "isArray"] ++
  membersFor "ArrayBuffer" ["isView"] ++
  membersFor "BigInt" ["asUintN"] ++
  membersFor "Date" ["UTC", "now", "parse"] ++
  membersFor "Intl" ["DateTimeFormat", "ListFormat", "NumberFormat", "RelativeTimeFormat", "Segmenter"] ++
  membersFor "JSON" ["parse", "stringify"] ++
  mathConstantEntries ++
  mathMembers ++
  membersFor "Number" numberStaticFunctions numberStaticFunctions ++
  (["assign", "create", "defineProperties", "defineProperty", "entries", "freeze", "fromEntries",
    "getOwnPropertyDescriptor", "getOwnPropertyDescriptors", "getOwnPropertyNames", "getOwnPropertySymbols",
    "getPrototypeOf", "hasOwn", "is", "isExtensible", "isFrozen", "keys", "preventExtensions", "seal",
    "setPrototypeOf", "values"].map fun key =>
      let primitive := match key with
        | "create" => some .create
        | "defineProperty" => some .defineProperty
        | "getPrototypeOf" => some .getPrototypeOf
        | "setPrototypeOf" => some .setPrototypeOf
        | _ => none
      member "Object" key false primitive) ++
  membersFor "Promise" ["all", "allSettled", "race", "reject", "resolve"] ++
  membersFor "Proxy" ["revocable"] ++
  membersFor "Reflect"
    ["apply", "construct", "defineProperty", "deleteProperty", "get", "getOwnPropertyDescriptor",
     "getPrototypeOf", "has", "ownKeys", "set"] ++
  membersFor "String" stringStaticFunctions stringStaticFunctions

private def worldMember (owner key : String) : HostMemberEntry :=
  ⟨owner, key, none, true, none, none, some (memberPremise owner key true)⟩

def prototypeMembers : List HostMemberEntry :=
  worldStringPrototypeMethods.map (worldMember "String.prototype") ++
  worldNumberPrototypeMethods.map (worldMember "Number.prototype") ++
  worldBooleanPrototypeMethods.map (worldMember "Boolean.prototype")

def symbolMembers : List HostMemberEntry :=
  ["asyncIterator", "hasInstance", "iterator", "toStringTag"].map fun key =>
    ⟨"Symbol", key, some ("Symbol." ++ key), false, none, none,
      some s!"Symbol.{key} is the realm's well-known symbol throughout initialization"⟩

def allMembers : List HostMemberEntry := staticMembers ++ prototypeMembers ++ symbolMembers

private def worldMembersFor (owner : String) : List String :=
  allMembers.filterMap fun entry => if entry.owner == owner && entry.worldCall then some entry.key else none

def worldMethodNames : List (String × List String) :=
  ["String.prototype", "Number.prototype", "Boolean.prototype"].map fun owner =>
    (owner, worldMembersFor owner)

def hostNamespaces : List (String × List String) :=
  ["Math", "Number", "String"].map fun owner => (owner, worldMembersFor owner)

def layer : HostLayer :=
  { name := "ecmascript", globals := globalEntries, members := allMembers,
    capabilities := [
      ⟨.ambientSymbols, some "Symbol is the unmodified ECMAScript intrinsic throughout initialization"⟩] }

end CoreJS.ECMAScriptHost

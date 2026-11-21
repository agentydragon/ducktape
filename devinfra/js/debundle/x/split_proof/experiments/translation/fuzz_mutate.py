"""Mutation test of the analysis' tests: weaken the checker in a scratch copy, rebuild `translate` and `lower`, run the
cases of `experiments/effects` (`run.mjs`), then `fuzz.mjs`.

    [SHARD=i/n] [START=k] python3 fuzz_mutate.py [bundles-per-mutant] [seed] [only-substring]

A mutant that neither the cases nor the fuzzer flag as a WRONG ACCEPT either is harmless (the rule it removes is
redundant or conservative) or shows a gap in the catalog. Each line says which.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BUNDLE, PLAN, LOWER, TRANSLATE = (
    "EsmSplit/Bundle.lean",
    "EsmSplit/Plan.lean",
    "Experiments/Lower.lean",
    "Experiments/Translate.lean",
)

# name: (file, old text, new text, why the fuzzer should see it)
EFFECTS = "Experiments/Effects.lean"
INTRINSICS = "Experiments/Intrinsics.lean"
MUTANTS = {
    # the model: the conflict rule and module order
    "drop the effect-effect conflict": (BUNDLE, "(a.effect && b.effect)", "false", "effects swap"),
    "drop read-after-declare": (
        BUNDLE,
        "|| (!b.hoisted && b.binds.any",
        "|| (false && b.binds.any",
        "a read runs before its declaration",
    ),
    "drop declare-after-read": (
        BUNDLE,
        "|| (!a.hoisted && a.binds.any",
        "|| (false && a.binds.any",
        "`var` read before its declaration",
    ),
    "imports run in reverse order": (
        PLAN,
        "srcs.foldl (fun st s => P.visit fuel s st) (m :: entered, done)",
        "srcs.reverse.foldl (fun st s => P.visit fuel s st) (m :: entered, done)",
        "module order",
    ),
    "conflicts ignore writes": (
        BUNDLE,
        "def Stmt.binds (s : Stmt) : List Name := s.defs ++ s.writes",
        "def Stmt.binds (s : Stmt) : List Name := s.defs",
        "a read of a variable moves across its write",
    ),
    "reads ignore deps": (
        BUNDLE,
        "closeFrom idx B.fuel (s.eager ++ s.deps) ∅",
        "closeFrom idx B.fuel s.eager ∅",
        "a function stored in an object reads a name its caller does not mention",
    ),
    "no AssignsHome": (
        TRANSLATE,
        "fun _ => decide (AssignsHome B P)",
        "fun _ => true",
        "an assignment through an import",
    ),
    "no Hygienic": (TRANSLATE, "fun _ => decide (Hygienic B P)", "fun _ => true", "redundant with the link check"),
    # the analysis: one rule at a time
    "stores record no write": (
        EFFECTS,
        "    | _ =>\n      recWrite o\n      -- an assignment runs a setter",
        "    | _ =>\n      -- an assignment runs a setter",
        "a property write through any name",
    ),
    "stores do not flow": (
        EFFECTS,
        "      | some k => joinProp o k v\n      | none => joinAny o v\n      if ob.escaped then escapeAV v",
        "      | some k => pure ()\n      | none => pure ()\n      if ob.escaped then escapeAV v",
        "an object stored in another object is written through the container",
    ),
    "a call returns nothing": (
        EFFECTS,
        "    return AV.ofObj p\n  return r\n",
        "    return AV.ofObj p\n  return #[]\n",
        "an alias returned by a function",
    ),
    "parameters are not bound": (
        EFFECTS,
        "    else bindPattern frP p (args[i]?.getD past)",
        "    else pure ()",
        "a function writes its argument",
    ),
    "builtins mutate nothing": (
        EFFECTS,
        '  for p in s.muts do\n    noteChange (pos p) "mutation of"\n    for c in pos p do',
        '  for p in ([] : List Nat) do\n    noteChange (pos p) "mutation of"\n    for c in pos p do',
        "`Object.assign` / `defineProperty` / `push` write their target",
    ),
    "call and apply do not call": (
        EFFECTS,
        "      return ← callAll thisV (args[0]?.getD #[]) (args.drop 1) false deeper site",
        "      return #[]",
        "`f.call(o)` writes `o`",
    ),
    "bound functions are not called": (
        EFFECTS,
        "    callAll target bthis (bargs ++ args) isNew { fr with depth := fr.depth + 1 } site\n",
        "    pure #[]\n",
        "`f.bind(o)()` writes `o`",
    ),
    "captured cells are not reachable": (
        EFFECTS,
        "      kids := kids ++ AV.objs this\n      for fr in scope do kids := kids ++ fr.values",
        "      kids := kids ++ AV.objs this",
        "a closure's state is written by a call and read by another",
    ),
    "a world call keeps its arguments": (
        EFFECTS,
        "  for v in vals do escapeAV v\n  return AV.top",
        "  return AV.top",
        "an object handed to the world is changed by it",
    ),
    "no taint": (
        EFFECTS,
        "    if let some c := c? then modObj c ({ · with taint := true })",
        "    if let some c := c? then pure ()",
        "a handler the world calls assigns a variable",
    ),
    "accessors are not calls": (
        EFFECTS,
        '      if ob.acc.contains k || ob.acc.contains "*" then\n        r := r.join (← runAccessors (seen',
        "      if false then\n        r := r.join (← runAccessors (seen",
        "a getter with an effect",
    ),
    "operands are not converted": (
        EFFECTS,
        '        for k in ["valueOf", "toString"] do',
        "        for k in ([] : List String) do",
        "a valueOf or toString with an effect",
    ),
    "class fields do not run": (
        EFFECTS,
        '    if typ m == "PropertyDefinition" && !flag m "static" then',
        "    if false then",
        "an instance field initializer with an effect",
    ),
    "constructors do not run": (
        EFFECTS,
        '    let ctor := members.find? fun m => typ m == "MethodDefinition" && str (fld m "kind") == "constructor"',
        "    let ctor : Option Json := none",
        "a constructor writes an object",
    ),
    "iteration yields nothing": (
        EFFECTS,
        "        else r := r.join (← contentsOf (AV.ofObj (c - 1)))\n",
        "        else pure ()\n",
        "an object reached by spreading an array",
    ),
    "object spread copies nothing": (
        EFFECTS,
        "      joinAny o (← contentsOf src)\n    else",
        "      pure ()\n    else",
        "an object reached through a spread copy",
    ),
    "module reads are not recorded": (
        EFFECTS,
        "  if let some n := ob.name then modify fun s => { s with reads := s.reads.insert n }\n",
        "",
        "a closure made in the unit reads a name the unit does not mention",
    ),
    "defineProperty stores nothing": (
        EFFECTS,
        "  setAV target none v\n  noteProps target v\n",
        "  pure ()\n",
        "a value defined on an object, read through another name",
    ),
    "Map from entries holds the entries": (
        EFFECTS,
        '    if proto == "Map.prototype" then joinAny o (← entryContents (pos 1))',
        '    if proto == "Map.prototype" then joinAny o (AV.joinAll args)',
        "an object put in a Map by its constructor",
    ),
    "reads of tainted variables are plain": (
        EFFECTS,
        "  return if ob.taint then ob.any.join AV.top else ob.any",
        "  return ob.any",
        "a variable a handler may assign",
    ),
    "escaped objects hold what the world put": (
        EFFECTS,
        "    if ob.escaped then\n      r := r.join AV.top\n      worldMayHaveAccessors",
        "    if ob.escaped then\n      worldMayHaveAccessors",
        "an object the world holds, read",
    ),
    "unknown keys see no properties": (
        EFFECTS,
        "      for v in ob.props.values do r := r.join v\n      unless ob.acc.isEmpty do\n        r := r.join (← runAccessors",
        "      unless ob.acc.isEmpty do\n        r := r.join (← runAccessors",
        "`o[k]` with a computed key",
    ),
    "function declarations are not bound": (
        EFFECTS,
        "        if let some c ← lookup fr n then joinAny c (AV.ofObj f)",
        "        pure ()",
        "a hoisted function called before its declaration",
    ),
    "catch handlers do not run": (
        EFFECTS,
        '      exec fr\' (fld h "body")',
        "      pure ()",
        "a write in a catch block",
    ),
    "memoized calls replay no writes": (
        EFFECTS,
        "      modify fun s => { s with writes := ws.foldl (·.insert ·) s.writes, reads",
        "      modify fun s => { s with writes := s.writes, reads",
        "the same call made twice in one unit",
    ),
    "loops run once": (
        EFFECTS,
        "    if n ≥ 6 then\n      modify fun s => { s with broke := true }\n      break",
        "    break",
        "a loop that stores through a variable assigned later in its body",
    ),
    # fail-safe paths and code that runs implicitly (found by reading the interpreter; each has cases in experiments/effects)
    "an unseen callee does not escape": (
        EFFECTS,
        "    escapeCode code scope\n    escapeAV cthis\n    return ← worldCall (thisV :: args)",
        "    return ← worldCall (thisV :: args)",
        "a call deeper than the analysis follows, or a generator advanced in a later unit",
    ),
    "an unseen constructor does not escape": (
        EFFECTS,
        "      escapeObj cls\n      return ← worldCall (AV.ofObj inst :: args)",
        "      return ← worldCall (AV.ofObj inst :: args)",
        "constructors nested deeper than the analysis follows",
    ),
    "a call through many functions does not escape them": (
        EFFECTS,
        "    escapeAV callee\n    return ← worldCall (thisV :: args)",
        "    return ← worldCall (thisV :: args)",
        "a call through more functions than the analysis follows",
    ),
    "await is not an effect": (
        EFFECTS,
        '  setTrace "await"\n',
        "",
        "two async calls swapped: their continuations run in the other order",
    ),
    "unknown code does not write the imports": (
        EFFECTS,
        "e.writes ++ r.escaped ++ importNames",
        "e.writes ++ r.escaped",
        "a read of state a module outside the tree keeps",
    ),
    "a getter the world made is not unknown code": (
        EFFECTS,
        '    escapeAV fns\n    escapeObj recv\n    setWorld "getter or setter the world made"',
        "    pure ()",
        "a getter that calls back a function the program left in a global",
    ),
    "a property of an object the world holds is a plain access": (
        EFFECTS,
        "  unless (← get).prem.noWorldHooks do\n    escapeObj recv\n    setWorld",
        "  unless true do\n    escapeObj recv\n    setWorld",
        "an accessor the world added to an object it holds",
    ),
    "inherited setters are plain stores": (
        EFFECTS,
        '      let (hit, fns) ← accessorsOn o key "set"',
        "      let (hit, fns) := (false, (#[] : AV))",
        "a setter that writes an object",
    ),
    "a __proto__ in a literal is a property": (
        EFFECTS,
        'then joinProp o "__proto__" v else joinProto o v',
        'then joinProp o "__proto__" v else joinProp o "__proto__" v',
        "a method found through __proto__",
    ),
    "setPrototypeOf stores contents": (
        EFFECTS,
        "      else if c ≥ 2 then joinProto (c - 1) (pos 2)\n",
        "      else if c ≥ 2 then joinAny (c - 1) (pos 2)\n",
        "a method found through a prototype set later",
    ),
    "getPrototypeOf returns nothing": (
        EFFECTS,
        "          let ob ← getObj (c - 1)\n          r := r.join ob.proto\n          match ob.kind with",
        "          let ob ← getObj (c - 1)\n          match ob.kind with",
        "a class prototype patched through an instance",
    ),
    "standard functions do not convert": (
        EFFECTS,
        "    convert args fr (mixHash site 89)\n",
        "",
        "Math.floor of an object with valueOf",
    ),
    "iterating a program object is a plain read": (
        EFFECTS,
        "        if optional && !(← mayHaveComputedKey (c - 1)) then",
        "        if true then",
        "Array.from, new Set or Object.fromEntries of an iterable the program defined",
    ),
    "++ does not read its operand": (
        EFFECTS,
        "      convert [← getAV o (memberKey t)] fr (shallow j)\n      setAV o (memberKey t) AV.prim\n    else\n      convert [← eval fr t] fr (shallow j)\n      assignTo fr t AV.prim",
        "      setAV o (memberKey t) AV.prim\n    else\n      assignTo fr t AV.prim",
        "x++ where x has a valueOf that writes",
    ),
    "stores into escaped objects do not escape": (
        EFFECTS,
        "  if ob.escaped then escapeAV v\n",
        "",
        "an object assigned to a variable or a property the world holds",
    ),
    "exported names are not held by the world": (
        EFFECTS,
        "    for n in exported do\n      if let some c := (← get).mods[n]? then escapeObj c",
        "    for n in ([] : Array String) do\n      if let some c := (← get).mods[n]? then escapeObj c",
        "an exported function that a module outside calls back while this one runs",
    ),
    "a change to an intrinsic is not noticed": (
        EFFECTS,
        "    else { s with patches := s.patches.push { unit := s.unit, path, key, what } }",
        "    else s",
        "an accessor the bundle defines on Object.prototype and a unit that reads a plain object",
    ),
    "a read of an intrinsic member is not recorded": (
        EFFECTS,
        '    unless path.startsWith "env:" || path.startsWith "vendor" do recBuiltinRead path key\n',
        "",
        "a unit that reads a static member that another unit adds",
    ),
    "the members of an intrinsic that a call lists are not read": (
        EFFECTS,
        "  recBuiltinsRead all\n",
        "",
        "Object.keys of an intrinsic that another unit added a member to",
    ),
    "X.prototype is a value of the world": (
        EFFECTS,
        '      else if k == "prototype" && members.contains child then return AV.ofObj (← builtinObj child)\n',
        "",
        "a store into Object.prototype is then a store into a value the world made",
    ),
    "a standard function has no call, apply or bind": (
        EFFECTS,
        '      else if (k == "call" || k == "apply" || k == "bind") && !path.endsWith ".prototype"',
        '      else if false && !path.endsWith ".prototype"',
        "Object.prototype.hasOwnProperty.call is then a call into the world (precision)",
    ),
    "a bound function is called without a level": (
        EFFECTS,
        "  | .bound target bthis bargs =>\n    tick\n    if (← isBroke) then return AV.top\n"
        "    if fr.depth ≥ maxDepth then\n      -- the target may run later, at any time\n      escapeAV target\n"
        "      escapeAV bthis\n      for a in bargs do escapeAV a\n      return ← worldCall (thisV :: args)\n"
        "    callAll target bthis (bargs ++ args) isNew { fr with depth := fr.depth + 1 } site\n",
        "  | .bound target bthis bargs => callAll target bthis (bargs ++ args) isNew fr site\n",
        "a bound function that holds itself recurses until the stack overflows",
    ),
    "Promise.resolve is not an effect": (
        "Experiments/Intrinsics.lean",
        'world := true, retains := [1] }),\n  ("Promise.reject"',
        '}),\n  ("Promise.reject"',
        "two thenables resolved in the other order",
    ),
    "a promise executor gets no world functions": (
        "Experiments/Intrinsics.lean",
        "callsWithWorld := [1] }",
        "calls := [1] }",
        "an executor that resolves a thenable",
    ),
    "JSON.parse calls no reviver": (
        "Experiments/Intrinsics.lean",
        '("JSON.parse", { ret := fresh, calls := [2], returnsCalls := true })',
        '("JSON.parse", { ret := fresh })',
        "a reviver that writes an object",
    ),
    "join does not convert its elements": (
        "Experiments/Intrinsics.lean",
        '("Array.prototype.join", { convertsContents := [0] })',
        '("Array.prototype.join", {})',
        "an element with a toString that writes an object",
    ),
    "a bare assignment to an intrinsic global is not a patch": (
        EFFECTS,
        '      if intrinsicGlobals.contains n then patchIntrinsic "global" (some n) s!"assignment to {n}"\n',
        "",
        "Array = class { … }",
    ),
    "the prototype of an intrinsic is the world's": (
        EFFECTS,
        '      else if k == "__proto__" && !path.startsWith "env:" then\n        match builtinProto path with\n        | some pp => return AV.ofObj (← builtinObj pp)\n        | none => return #[]\n',
        "",
        "Array.prototype.__proto__.tag = …",
    ),
    "a primitive has no wrapper prototypes": (
        EFFECTS,
        '  if key == some "__proto__" && a == AV.prim then return ← primitivePrototypeValue\n',
        "",
        '"x".__proto__.tag = …',
    ),
    "code that is not run is not scanned for patches": (
        EFFECTS,
        '  for (p, k) in (← codePatches code) do patchIntrinsic p k "a patch in code that is not run here"\n',
        "",
        "a patch in a generator body or a callback of a standard function without a row",
    ),
    "a give-up is not scanned for patches": (
        EFFECTS,
        '  for (p, k) in (← codePatches code) do patchIntrinsic p k "a patch in code the analysis gave up on"\n',
        "",
        "a patch after a loop that needs many rounds",
    ),
    "a read of a patched member runs no unknown code": (
        EFFECTS,
        "    let unk := e.unk || patched\n",
        "    let unk := e.unk\n",
        "a replaced Math.max that writes module state",
    ),
    "an error constructor has its own statics": (
        INTRINSICS,
        '  if errorTypes.contains path then "Error"\n  else if path == "parseFloat"',
        '  if false then "Error"\n  else if path == "parseFloat"',
        "Error.tag = … read as TypeError.tag",
    ),
    "Object.assign copies no property values": (
        EFFECTS,
        "  for (src, dst) in s.copies do\n    let v ← contentsOf (pos src)\n    setAV (pos dst) none v\n    noteProps (pos dst) v\n",
        "",
        "Object.assign runs a getter of its source",
    ),
    "__defineGetter__ defines no accessor": (
        EFFECTS,
        "  for (dst, f) in s.accessors do\n    setAV (pos dst) none (pos f)\n",
        "  for (dst, f) in s.accessors do\n    pure ()\n",
        "__defineGetter__ on an intrinsic prototype",
    ),
    "toString does not read the string tag": (
        EFFECTS,
        "  for p in s.readsContents do let _ ← contentsOf (pos p)\n",
        "",
        "Object.prototype.toString runs a Symbol.toStringTag getter",
    ),
    "Error.stackTraceLimit is not read by every unit": (
        EFFECTS,
        '"__proto__", "prototype",\n   "stackTraceLimit", "prepareStackTrace"]',
        '"__proto__", "prototype"]',
        "Error.stackTraceLimit = 0 before new Error",
    ),
    # the general rules
    "a spread is one argument position": (
        EFFECTS,
        "  return if spread then out ++ List.replicate spreadArity rest else out\n",
        "  return out\n",
        "an argument after a spread, or a wrapper that forwards its rest parameters",
    ),
    "apply fills one position": (
        EFFECTS,
        "(List.replicate spreadArity a) false deeper site",
        "[a] false deeper site",
        "`f.apply(null, [1, 2, 3, 4, o])` writes its fifth parameter",
    ),
    "no positions past a spread": (
        EFFECTS,
        "  let past : AV := if args.length ≥ spreadArity then args.getLast?.getD #[] else #[]",
        "  let past : AV := #[]",
        "a function with more parameters than a spread fills",
    ),
    "new yields the instance only": (
        EFFECTS,
        "      return (AV.ofObj inst).join r\n",
        "      return AV.ofObj inst\n",
        "a class constructor that returns another object",
    ),
    "the depth cutoffs find nothing": (
        EFFECTS,
        (
            "  if seen.length > 10 then return AV.top\n",
            "  if seen.length > 10 then return (true, AV.top)\n",
            "  if seen.length > 10 then return true\n",
        ),
        (
            "  if seen.length > 10 then return #[]\n",
            "  if seen.length > 10 then return (false, #[])\n",
            "  if seen.length > 10 then return false\n",
        ),
        "a method or a getter found 11 prototypes up (each cutoff alone is covered by the others)",
    ),
    "callbacks get only their own arguments": (
        EFFECTS,
        "    let given : AV := AV.join (AV.join (← contentsOf others) others) AV.prim\n",
        "    let given : AV := AV.prim\n",
        "the element of `reduce` is its second parameter",
    ),
    "a chain of callbacks runs once": (
        EFFECTS,
        "for round in [0:(if s.accumulates then 2 else 1)] do",
        "for round in [0:1] do",
        "a reduce callback reads what the previous call returned",
    ),
    "a callback's result is not returned": (
        EFFECTS,
        "#[]) (if s.returnsCalls then calledRet else #[])\n",
        "#[]) #[]\n",
        "a reviver or a reduce callback returns an object",
    ),
    "reduce does not return its initial value": (
        EFFECTS,
        "(s.alsoReturns.foldl (fun (acc : AV) p => AV.join acc (pos p)) #[])",
        "#[]",
        "`[].reduce(f, o)` returns `o`",
    ),
    "Array.from keeps no mapFn result": (
        EFFECTS,
        "    | .freshOf p => mkArr ((← contentsOf (pos p)).join (if s.returnsCalls then calledRet else #[]))\n",
        "    | .freshOf p => mkArr (← contentsOf (pos p))\n",
        "`Array.from(xs, f)` holds what `f` returns",
    ),
    "defineProperties reads no descriptors": (
        EFFECTS,
        "  if let some m := s.defineMap then defineFrom (pos 1) (← contentsOf (pos m))\n",
        "  if let some m := s.defineMap then pure ()\n",
        "`Object.defineProperties` defines an accessor",
    ),
    "assign copies from one source": (
        EFFECTS,
        "    for i in [first:args.length + 1] do\n      let v ← contentsOf (pos i)",
        "    for i in [first:first + 1] do\n      let v ← contentsOf (pos i)",
        "`Object.assign` with several sources",
    ),
    "an assignment does not convert its key": (
        EFFECTS,
        '      convert [← eval fr (fld t "property")] fr (shallow t)\n    setAV o (memberKey t) v',
        '      let _ ← eval fr (fld t "property")\n    setAV o (memberKey t) v',
        "`o[key] = v` converts the key",
    ),
    "an object literal does not convert its key": (
        EFFECTS,
        '(do convert [← eval fr (fld p "key")] fr (shallow p); pure (propKey p))',
        '(do let _ ← eval fr (fld p "key"); pure (propKey p))',
        "`{ [key]: v }` converts the key",
    ),
    "delete does not convert its key": (
        EFFECTS,
        '(do convert [← eval fr (fld arg "property")] fr (shallow arg); pure (memberKey arg))',
        '(do let _ ← eval fr (fld arg "property"); pure (memberKey arg))',
        "`delete o[key]` converts the key",
    ),
    "a class member does not convert its key": (
        EFFECTS,
        "(do convert [← eval fr' (fld m \"key\")] fr' (shallow m); pure (propKey m))",
        '(do let _ ← eval fr\' (fld m "key"); pure (propKey m))',
        "`class { [key]() {} }` converts the key",
    ),
    "a pattern does not convert its key": (
        EFFECTS,
        '(do convert [← eval fr (fld q "key")] fr (shallow q); pure (propKey q))',
        '(do let _ ← eval fr (fld q "key"); pure (propKey q))',
        "`const { [key]: x } = o` converts the key",
    ),
    "sort converts nothing": (
        INTRINSICS,
        '("Array.prototype.sort", { ret := thisV, muts := [0], calls := [1], convertsContents := [0] })',
        '("Array.prototype.sort", { ret := thisV, muts := [0], calls := [1] })',
        "`sort()` converts the elements to strings",
    ),
    "a computed getter key is lost": (
        EFFECTS,
        '        modObj o fun x => { x with acc := (key.getD "*") :: x.acc }',
        '        modObj o fun x => { x with acc := (key.getD "") :: x.acc }',
        "a getter under a computed key is read by name",
    ),
    "a computed static getter key is lost": (
        EFFECTS,
        '        modObj target fun x => { x with acc := (key.getD "*") :: x.acc }',
        '        modObj target fun x => { x with acc := (key.getD "") :: x.acc }',
        "a static getter of a class under a computed key",
    ),
    "a class prototype has no constructor": (
        EFFECTS,
        '  joinProp P "constructor" (AV.ofObj C)\n',
        "  pure ()\n",
        "`inst.constructor.x` reads a static of the class",
    ),
    "a function prototype has no constructor": (
        EFFECTS,
        '            joinProp p "constructor" (AV.ofObj o)\n',
        "            pure ()\n",
        "`new F().constructor.x` reads a property of the function",
    ),
    "an async function resolves no thenable": (
        EFFECTS,
        "    callThenables r fr (mixHash site 71)\n",
        "",
        "an async function returns a thenable",
    ),
    "arguments is not a binding": (
        EFFECTS,
        '  let bindsArguments := !isArrow && !(paramNames ++ bodyNames).contains "arguments" && (← codeInfo code).1.contains "arguments"',
        "  let bindsArguments := false",
        "`arguments` in a function and in an arrow function inside it",
    ),
    "a Map iterator is a copy": (
        INTRINSICS,
        '("Map.prototype.values", { ret := thisV })',
        '("Map.prototype.values", { ret := freshOf 0 })',
        "an iterator made before a `set` shows it",
    ),
    "next moves no cursor": (
        INTRINSICS,
        '("Map.prototype.next", { ret := iterResultOf 0, muts := [0] })',
        '("Map.prototype.next", { ret := iterResultOf 0 })',
        "two `next()` calls keep their order",
    ),
    "next returns no value": (
        EFFECTS,
        '      joinProp o "value" (← contentsOf (pos p))\n      joinProp o "done" AV.prim',
        '      joinProp o "done" AV.prim',
        "`it.next().value` is an element",
    ),
    "iterator helpers touch nothing": (
        INTRINSICS,
        "{ world := true, retains := [1, 2] })",
        "{ muts := [] })",
        "an iterator helper runs its callback when consumed",
    ),
    "a map copy holds no keys and values": (
        EFFECTS,
        "    if c ≥ 2 && (← getObj (c - 1)).kind matches .coll then parts := parts.push (← contentsOf #[c])",
        "    pure ()",
        "`new Map(map)` and `Object.fromEntries(map)` hold the keys and values",
    ),
    "standard functions call no methods": (
        EFFECTS,
        "  for (p, m, as) in s.callsMethods do runProtocol (pos p) m (as.map pos) fr (mixHash site (hash m))\n",
        "",
        "`test` calls an own `exec`, `replace` the `Symbol.replace` of its pattern",
    ),
    "instanceof calls no hasInstance": (
        EFFECTS,
        '    if op == "instanceof" then runProtocol b "@@hasInstance" [a] fr (shallow j)\n',
        "",
        "a class with a static `Symbol.hasInstance`",
    ),
    "join is not a conversion hook": (
        EFFECTS,
        '            for k in ["valueOf", "toString", "join"] do',
        '            for k in ["valueOf", "toString"] do',
        "`String(array)` calls an own `join`",
    ),
    "a collection converts as nothing": (
        EFFECTS,
        "        | .builtin _ => pure ()\n        | .array | .coll =>",
        "        | .builtin _ | .coll => pure ()\n        | .array =>",
        "a Map with an own `toString` in a template literal",
    ),
    "a function under an unknown key of an array is not a hook": (
        EFFECTS,
        "    if (memberKey t).isNone then noteProps o v\n",
        "    pure ()\n",
        "an array with an own `Symbol.iterator`",
    ),
    "a hooked array is iterated natively": (
        EFFECTS,
        "        if ob.hook then r := r.join (← worldCall [AV.ofObj (c - 1)])",
        "        if false then r := r.join (← worldCall [AV.ofObj (c - 1)])",
        "spreading an array with an own `Symbol.iterator`",
    ),
    "Error reads no options": (
        INTRINSICS,
        '("Error", { ret := inst "Error.prototype", readsContents := [2] })',
        '("Error", { ret := inst "Error.prototype" })',
        "`new Error(m, options)` reads a `cause` getter",
    ),
    "Error.prototype.toString reads nothing": (
        INTRINSICS,
        '("Error.prototype.toString", { readsContents := [0] })',
        '("Error.prototype.toString", {})',
        "`Error.prototype.toString.call(x)` reads a `name` getter",
    ),
    "Intl reads no options": (
        INTRINSICS,
        '("Intl.NumberFormat", { ret := inst "Intl.NumberFormat.prototype", readsContents := [2] })',
        '("Intl.NumberFormat", { ret := inst "Intl.NumberFormat.prototype" })',
        "`new Intl.NumberFormat(l, options)` reads option getters",
    ),
    "test calls no exec": (
        INTRINSICS,
        '("RegExp.prototype.test", { muts := [0], callsMethods := [(0, "exec", [1])] })',
        '("RegExp.prototype.test", { muts := [0] })',
        "`re.test(s)` calls an own `exec`",
    ),
    "replace calls no Symbol.replace": (
        INTRINSICS,
        '("prim.replace", { calls := [2], muts := [1], callsMethods := [(1, "@@replace", [0, 2])] })',
        '("prim.replace", { calls := [2], muts := [1] })',
        "`s.replace(pattern, r)` calls the `Symbol.replace` of the pattern",
    ),
    "toLocaleString calls no toString": (
        INTRINSICS,
        '("Object.prototype.toLocaleString", { callsMethods := [(0, "toString", [])] })',
        '("Object.prototype.toLocaleString", {})',
        "`x.toLocaleString()` calls an own `toString`",
    ),
    # the core language
    "the memo replays a run that grew the heap": (
        EFFECTS,
        "  unless s2.broke || s2.changes != s.changes do\n",
        "  unless s2.broke do\n",
        "two identical calls move a value one hop each; a loop of calls",
    ),
    "a replayed call drops what it threw": (
        EFFECTS,
        "unk := s.unk || u, thrown := s.thrown.join th }",
        "unk := s.unk || u }",
        "`catch (e)` after a call that was replayed",
    ),
    "a named read of an array skips what has no name": (
        EFFECTS,
        "isElements && isStandardMember k | none => false",
        "isElements && !(k.all Char.isDigit) | none => false",
        '`arr.foo` after `arr["fo" + "o"] = o`',
    ),
    "a prototype of the program is no hook of an array": (
        EFFECTS,
        "        | .builtin _ => pure ()\n        | _ => modObj o ({ · with hook := true })",
        "        | _ => pure ()",
        "`Object.setPrototypeOf(arr, { toString() {} })`",
    ),
    "properties copied onto an array are no hooks": (
        EFFECTS,
        "      if c ≥ 2 && (← getObj (c - 1)).kind matches .array | .coll then modObj (c - 1) ({ · with hook := true })",
        "      if c ≥ 2 && (← getObj (c - 1)).kind matches .array | .coll then pure ()",
        "`Object.assign(arr, { [Symbol.iterator]() {} })`, then a spread",
    ),
    "a hooked array converts as a plain one": (
        EFFECTS,
        "let f ← if ob.hook then hookFns ((← getVal c (some k)).join ob.any) else pure (ob.props.getD k #[])",
        "let f ← pure (ob.props.getD k #[])",
        "a `toString` found on the replaced prototype of an array",
    ),
    "the fields of a derived class run before super": (
        EFFECTS,
        ("    unless derived do initFields cls inst fr0\n", "  initFields cls inst fr\n  return r"),
        ("    initFields cls inst fr0\n", "  return r"),
        "a field initializer reads what the parent constructor set",
    ),
    "a parent constructor's returned object is not this": (
        EFFECTS,
        '    escapeAV others\n    setWorld "a parent constructor returned another object"',
        "    pure ()",
        "a derived class whose parent constructor returns an object",
    ),
    "super into a standard constructor builds nothing": (
        EFFECTS,
        "do setAV fr.this none (← contentsOf (← callBuiltin path fr.this args fr 41))",
        "do let _ ← callBuiltin path fr.this args fr 41",
        "`class M extends Map {}` holds the entries given to `super`",
    ),
    "a static block runs in text order": (
        EFFECTS,
        (
            '    | "StaticBlock" => pure ()\n    | t => giveUp t m',
            "      for s in stmts do exec fr'' s\n    | _ => pure ()",
        ),
        (
            '    | "StaticBlock" =>\n      let stmts := items (fld m "body")\n      let fr\'\' ← newFrame fr\' (lexicalNames stmts ++ stmts.toList.flatMap varNames)\n'
            "      hoistFns fr'' stmts\n      for s in stmts do exec fr'' s\n    | t => giveUp t m",
            "      pure ()\n    | _ => pure ()",
        ),
        "a static block calls a method defined after it",
    ),
    "a static field runs in text order": (
        EFFECTS,
        (
            '    | "PropertyDefinition" =>\n      if flag m "static" && !isNull (fld m "value") then',
            '        convert [← eval fr\' (fld m "key")] fr\' (shallow m)\n    | "StaticBlock" => pure ()',
        ),
        (
            '    | "PropertyDefinition" =>\n      if false then',
            '        convert [← eval fr\' (fld m "key")] fr\' (shallow m)\n      if flag m "static" && !isNull (fld m "value") then\n'
            '        let v ← eval fr\' (fld m "value")\n        recWrite C\n        match propKey m with\n        | some k => joinProp C k v\n        | none => joinAny C v\n    | "StaticBlock" => pure ()',
        ),
        "a static field calls a method defined after it",
    ),
    "a parameter default sees the body's declarations": (
        EFFECTS,
        (
            '(if bindsArguments then ["arguments"] else []) ++ selfName ++ paramNames)',
            "  let fr1 ← if bodyNames.isEmpty then pure frP else newFrame frP bodyNames",
        ),
        (
            '(if bindsArguments then ["arguments"] else []) ++ selfName ++ paramNames ++ bodyNames)',
            "  let fr1 ← pure frP",
        ),
        "`function f(a = o) { var o; }` uses the outer `o`",
    ),
    "a switch binds no functions": (EFFECTS, "    hoistFns fr' bodies\n", "", "a function declared in a `switch` case"),
    "a closure site keeps the last closure": (
        EFFECTS,
        "let (nw, grew) := joinKind (← getObj o).kind k",
        "let (nw, grew) := (k, false)",
        "two `f.bind(x)` or two arrows made by one call site",
    ),
    "a standard function is no hook": (
        EFFECTS,
        "      | .func .. | .klass .. | .bound .. | .builtin _ => r := r.join (AV.ofObj (d - 1))",
        "      | .func .. | .klass .. | .bound .. => r := r.join (AV.ofObj (d - 1))",
        "`valueOf: Array.prototype.shift`",
    ),
    "a standard function is no accessor": (
        EFFECTS,
        "      | .func .. | .klass .. | .bound .. | .builtin _ => programs := programs.join (AV.ofObj (d - 1))",
        "      | .func .. | .klass .. | .bound .. => programs := programs.join (AV.ofObj (d - 1))",
        "`get: Array.prototype.pop`",
    ),
    "delete through an optional chain is a read": (
        EFFECTS,
        'let arg := if op == "delete" then unwrap (fld j "argument") else fld j "argument"',
        'let arg := fld j "argument"',
        "`delete a?.b`",
    ),
    "a callee in an optional chain has no this": (
        EFFECTS,
        "  let m := unwrap calleeJ\n  if typ m ==",
        "  let m := calleeJ\n  if typ m ==",
        "`(a?.b)``` calls `b` with `a` as `this`",
    ),
    "the computed key of a callee is not converted": (
        EFFECTS,
        '      convert [← eval fr (fld m "property")] fr (shallow m)\n    return (← getAV o (memberKey m), o)',
        '      let _ ← eval fr (fld m "property")\n    return (← getAV o (memberKey m), o)',
        "`o[key]()` runs the `toString` of `key`",
    ),
    "patches read off code skip optional chains": (
        EFFECTS,
        (
            "  let t := unwrap t\n  match typ t with",
            '  | "ParenthesizedExpression" | "ChainExpression" => memberChain (fld j "expression")\n',
            '        let c := unwrap (fld j "callee")',
        ),
        ("  match typ t with", "", '        let c := fld j "callee"'),
        "`delete Math?.max` in a generator body",
    ),
    "Array(n) is a primitive": (
        INTRINSICS,
        '("Array", { ret := freshArgs }),',
        '("Array", {}),',
        "`Array(2).fill(o)` holds `o`, and its `forEach` runs the callback",
    ),
    "JSON.parse returns an empty object": (
        INTRINSICS,
        '("Math.abs", {}),',
        '("JSON.parse", { ret := inst "Object.prototype" }), ("Math.abs", {}),',
        "the result may be an array, and holds what the reviver returned",
    ),
    "new String is a primitive": (
        EFFECTS,
        "    if isNew && (match table[path]? with",
        "    if false && (match table[path]? with",
        "`new String(s)` is an object that holds what is stored in it",
    ),
    "a collection has no iterator helpers": (
        EFFECTS,
        '&& members.contains ("Iterator.prototype." ++ k)) then',
        "&& false) then",
        "`set.values().some(f)` runs `f`, and `map.values().map(f).toArray()` does when it is consumed",
    ),
    "matchAll returns an array": (
        INTRINSICS,
        '("prim.split", {',
        '("prim.matchAll", { ret := freshPrims }), ("prim.split", {',
        "a helper of the iterator runs its callback when the result is consumed",
    ),
    "a collection yields no pairs": (
        EFFECTS,
        "    if c ≥ 2 && (← getObj (c - 1)).kind matches .coll then\n      let pair",
        "    if false then\n      let pair",
        "`for (const pair of map) pair[1].n = 1`",
    ),
    "a long prototype chain reads as the world's value": (
        EFFECTS,
        '  if seen.length > 10 then return ← runAccessors (seen.getLast?.getD o) AV.top [] "get"',
        "  if seen.length > 10 then return AV.top",
        "a getter or a `toString` 12 prototypes up",
    ),
    "flat looks two levels deep": (
        EFFECTS,
        "        if more == every then break\n        every := more\n      mkArr every",
        "        every := more\n        break\n      mkArr every",
        "`flat(Infinity)` of arrays nested seven deep",
    ),
    "nested hooks are unbounded": (
        EFFECTS,
        "  if (← get).hookDepth ≥ maxDepth then",
        "  if false then",
        "a standard function used as a getter reads the getter it is",
    ),
    "conversion hooks are not counted": (
        EFFECTS,
        "              unless f.isEmpty do let _ ← callHooks (c - 1) f [] fr (mixHash site (hash k))",
        "              unless f.isEmpty do let _ ← callAll f (AV.ofObj (c - 1)) [] false fr (mixHash site (hash k))",
        "`Array.prototype.toString` on an array that holds itself",
    ),
    "protocol methods are not counted": (
        EFFECTS,
        "      unless fns.isEmpty do let _ ← callHooks (c - 1) fns args fr site",
        "      unless fns.isEmpty do let _ ← callAll fns (AV.ofObj (c - 1)) args false fr site",
        "an `exec` that is `RegExp.prototype.test`",
    ),
}


def main():
    count = sys.argv[1] if len(sys.argv) > 1 else "150"
    seed = sys.argv[2] if len(sys.argv) > 2 else "2"
    only = sys.argv[3] if len(sys.argv) > 3 else ""
    with tempfile.TemporaryDirectory() as tmp:
        lean = pathlib.Path(tmp) / "lean"
        shutil.copytree(ROOT / "lean", lean, symlinks=True, ignore=shutil.ignore_patterns("bin"))
        # `SHARD=i/n` takes every n-th mutant starting at i, so that n copies can run at once; `START=k` skips the first k
        index, shards = (int(x) for x in os.environ.get("SHARD", "0/1").split("/"))
        start = int(os.environ.get("START", "0"))
        for n, (name, (file, old, new, why)) in enumerate(MUTANTS.items()):
            if only not in name or n % shards != index or n < start:
                continue
            path = lean / file
            original = path.read_text()
            mutated = original
            # a mutant may change several sites at once: `old` and `new` are then tuples of the same length
            for site_old, site_new in zip(
                old if isinstance(old, tuple) else (old,), new if isinstance(new, tuple) else (new,), strict=True
            ):
                assert site_old in mutated, f"mutation site not found: {name}"
                mutated = mutated.replace(site_old, site_new)
            path.write_text(mutated)
            # proofs about a mutated definition may no longer hold; only the executable matters here
            sem = lean / "EsmSplit/Semantics.lean"
            sem_original = sem.read_text()
            a, b = sem_original.index("theorem Bundle.step_comm"), sem_original.index("theorem Bundle.exec_cons")
            sem.write_text(
                sem_original[:a]
                + sem_original[a : sem_original.index(":= by", a)]
                + ":= by sorry\n\n"
                + sem_original[b:]
            )
            others = {f: (lean / f).read_text() for f in ["EsmSplit/Instance.lean", "EsmSplit/Controls.lean"]}
            for f, text in others.items():
                (lean / f).write_text(text.replace("by decide +kernel", "by sorry"))
            try:
                build = subprocess.run(
                    ["lake", "build", "translate", "lower"], check=False, cwd=lean, capture_output=True, text=True
                )
                if build.returncode:
                    first = next((ln for ln in build.stdout.splitlines() if "error" in ln), "")
                    print(f"{'CAUGHT by a proof':18} {name:36} build fails: {first[:80]}", flush=True)
                    continue
                env = dict(
                    os.environ,
                    TRANSLATE=str(lean / ".lake/build/bin/translate"),
                    LOWER=str(lean / ".lake/build/bin/lower"),
                )
                cases = subprocess.run(
                    ["node", str(HERE / "../effects/run.mjs")], check=False, env=env, capture_output=True, text=True
                )
                if cases.returncode:
                    first = next((ln for ln in cases.stdout.splitlines() if "FAIL" in ln), cases.stderr[-120:])
                    print(f"{'CAUGHT by cases':18} {name:36} {first.strip()[:90]}  [{why}]", flush=True)
                    continue
                run = subprocess.run(
                    ["node", str(HERE / "fuzz.mjs"), "--count", count, "--seed", seed],
                    check=False,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                line = next(
                    (ln for ln in run.stdout.splitlines() if ln.startswith("WRONG ACCEPTS")),
                    run.stdout[-200:] + run.stderr[-200:],
                )
                print(f"{'CAUGHT' if run.returncode else 'SURVIVED':18} {name:36} {line}  [{why}]", flush=True)
            finally:
                path.write_text(original)
                sem.write_text(sem_original)
                for f, text in others.items():
                    (lean / f).write_text(text)


main()

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
        "    else bindPattern fr1 p (args[i]?.getD past)",
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
        '      if typ m == "PropertyDefinition" && !flag m "static" then',
        "      if false then",
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
        "  setAV target none (← contentsOf d)\n",
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
        "  for (src, dst) in s.copies do setAV (pos dst) none (← contentsOf (pos src))\n",
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
            assert old in original, f"mutation site not found: {name}"
            path.write_text(original.replace(old, new))
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

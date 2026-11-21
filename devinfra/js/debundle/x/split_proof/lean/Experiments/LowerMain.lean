import Experiments.Effects

open Lean Lower Effects

/-- `lower <bundle.json> [--premises <file>]`: one JSON footprint per top-level unit, one per line, from the abstract
interpreter of `Effects`. The bundle is either acorn's ESTree (`{"statements": [{"ast": …}]}`,
`experiments/lowering/dump.mjs`) or the annotated units of the translation front end
(`{"units": […], "imports": […], "exports": […]}`, `experiments/translation/frontend.mjs`). The premises the
verdict rests on go to stderr. -/
def main (args : List String) : IO UInt32 := do
  let (premPath?, args) := takeFlag "--premises" args
  let [path] := args | IO.eprintln "usage: lower <bundle.json> [--premises <file>]"; return 2
  let prem ← loadPremises premPath?
  let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
  let units := if isNull (fld j "units") then dumpUnits j else items (fld j "units")
  -- the front end lists the exports; an ESTree dump has them in its `export` statements
  let exported :=
    if isNull (fld j "units") then (units.toList.flatMap exportedBindings).toArray
    else (items (fld j "exports")).map fun e => str (fld e "local")
  let (fps, r) := footprints units (importsOf j) prem exported
  for f in fps do IO.println f.toJson.compress
  for line in premiseLines prem r do IO.eprintln line
  if (← IO.getEnv "EFFECTS_WHY").isSome then
    -- why units touch the world, by the first reason: unknown code first, then the environment; a few unit numbers each
    let mut counts : Std.HashMap String (Nat × List Nat) := {}
    for (u, i) in r.units.toList.zipIdx do
      if u.world then
        let k := (if u.unk then "unknown code: " else "environment: ") ++ (if u.unk then u.whyUnk else u.why)
        let (n, ex) := counts.getD k (0, [])
        counts := counts.insert k (n + 1, if ex.length < 4 then i :: ex else ex)
    for (k, n, ex) in (counts.toList.map (fun (k, n, ex) => (k, n, ex))).toArray.qsort (fun a b => a.2.1 > b.2.1) |>.toList.take 60 do
      IO.eprintln s!"{n}\t{k}\t{ex}"
  if (← IO.getEnv "EFFECTS_DEBUG").isSome then
    IO.eprintln s!"escaped: {r.escapedObjs.size}; biggest values: {r.biggest}; getAV (calls, max callee set, total parts, max parts): {r.dbg}"
    let top := (r.steps.toList.zipIdx.toArray.qsort (fun a b => a.1 > b.1)).toList.take 8
    IO.eprintln s!"most steps (steps, unit): {top}; total {r.steps.foldl (· + ·) 0}"
  return 0

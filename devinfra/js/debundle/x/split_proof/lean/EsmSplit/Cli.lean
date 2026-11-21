import Lean.Data.Json
import EsmSplit.Split
import EsmSplit.Render

/-! Helpers shared by the executables: the per-check report, writing modules, reading a spec. -/

open EsmSplit Lean

namespace EsmSplit

/-- Evaluate a check and print how long it took. `lazyPure` makes sure the work happens between the clocks. -/
def timedCheck (label : String) (p : Unit → Bool) : IO Bool := do
  let t0 ← IO.monoMsNow
  let r ← IO.lazyPure p
  let dt := (← IO.monoMsNow) - t0
  IO.println s!"  {if r then "pass" else "FAIL"}  {label}  ({dt} ms)"
  (← IO.getStdout).flush
  return r

def report (B : Bundle) (S : Spec) (P : Plan) : IO Bool := do
  let t0 ← IO.monoMsNow
  let order ← IO.lazyPure fun _ => P.evalOrder
  let exec ← IO.lazyPure fun _ => P.execOrder
  IO.println s!"  module order  {" → ".intercalate order}"
  IO.println s!"  statement order  {if exec.length > 40 then s!"[{exec.length} statements]" else toString exec}  ({(← IO.monoMsNow) - t0} ms)"
  let rs := [← timedCheck "UniqueDefs" fun _ => decide (UniqueDefs B),
             ← timedCheck "ReadsClosed" fun _ => decide (ReadsClosed B),
             ← timedCheck "Faithful" fun _ => decide (Faithful B S P),
             ← timedCheck "Linkable" fun _ => decide (Linkable B P),
             ← timedCheck "ExportsUnique" fun _ => decide (ExportsUnique B P),
             ← timedCheck "Hygienic" fun _ => decide (Hygienic B P),
             ← timedCheck "AssignsHome" fun _ => decide (AssignsHome B P),
             ← timedCheck "Permutes" fun _ => decide (Permutes B P),
             ← timedCheck "OrderOk" fun _ => decide (OrderOk B P)]
  return rs.all id

def writeModules (B : Bundle) (P : Plan) (dir : System.FilePath) (render : Module → String := (·.render B P)) : IO Unit := do
  IO.FS.createDirAll dir
  for m in P.mods do
    IO.FS.writeFile (dir / m.name) (render m)
  IO.println s!"  wrote {P.mods.length} modules to {dir}"

/-- Read a spec file: `{"entry": "main.mjs", "modules": {"a.mjs": ["name", "#7"], …}, "order": ["a.mjs", …]}`.
Module order follows the sorted keys of the parsed object; `order` is optional and keeps its order. -/
def loadSpec (path : System.FilePath) : IO Spec := do
  let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
  let entry ← IO.ofExcept (j.getObjVal? "entry" >>= Json.getStr?)
  let mods ← IO.ofExcept (j.getObjVal? "modules" >>= Json.getObj?)
  let modules ← mods.toList.mapM fun (name, v) => do
    let sels ← (← IO.ofExcept v.getArr?).toList.mapM fun x => do
      let s ← IO.ofExcept x.getStr?
      if s.startsWith "#" then
        let some k := (s.drop 1).toNat? | throw (IO.userError s!"bad selector {s}")
        pure (Selector.stmt k)
      else pure (Selector.name s)
    pure (name, sels)
  let order ← match j.getObjVal? "order" with
    | .ok v => do
      let items ← IO.ofExcept v.getArr?
      items.toList.mapM fun x => IO.ofExcept x.getStr?
    | .error _ => pure []
  pure { entry, modules, order }

end EsmSplit

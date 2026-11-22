import Lean.Data.Json
import EsmSplit.Split
import EsmSplit.ModulePath

/-! Helpers shared by the executables: the per-check report and spec reader. -/

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

structure PlanChecks where
  layout : Bool
  order : Bool

def PlanChecks.ok (checks : PlanChecks) : Bool := checks.layout && checks.order

def report (B : Bundle) (S : Spec) (P : Plan) (verbose := true) : IO PlanChecks := do
  if verbose then
    let t0 ← IO.monoMsNow
    let order ← IO.lazyPure fun _ => P.evalOrder
    let exec ← IO.lazyPure fun _ => P.execOrder
    IO.println s!"  module order  {" → ".intercalate order}"
    IO.println s!"  statement order  {if exec.length > 40 then s!"[{exec.length} statements]" else toString exec}  ({(← IO.monoMsNow) - t0} ms)"
  let check (name : String) (p : Unit → Bool) : IO Bool :=
    if verbose then timedCheck name p else IO.lazyPure p
  let rs := [← check "UniqueDefs" fun _ => decide (UniqueDefs B),
             ← check "ReadsClosed" fun _ => decide (ReadsClosed B),
             ← check "Faithful" fun _ => decide (Faithful B S P),
             ← check "Linkable" fun _ => decide (Linkable B P),
             ← check "ExportsUnique" fun _ => decide (ExportsUnique B P),
             ← check "Hygienic" fun _ => decide (Hygienic B P),
             ← check "AssignsHome" fun _ => decide (AssignsHome B P),
             ← check "Permutes" fun _ => decide (Permutes B P)]
  let orderOk ← check "OrderOk" fun _ => decide (OrderOk B P)
  return ⟨rs.all id, orderOk⟩

/-- Read a spec file: `{"entry": "main.mjs", "modules": {"a.mjs": ["name", "#7"], …}, "order": ["a.mjs", …]}`.
Module order follows the sorted keys of the parsed object; `order` is optional and keeps its order;
`"sequential": true` makes the modules run in exactly that order (`Spec.sequential`). -/
def loadSpec (path : System.FilePath) : IO Spec := do
  let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
  let entry ← IO.ofExcept (j.getObjVal? "entry" >>= Json.getStr?)
  let mods ← IO.ofExcept (j.getObjVal? "modules" >>= Json.getObj?)
  IO.ofExcept (validateModulePaths (entry :: mods.toList.map (·.1)))
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
  let sequential := (j.getObjVal? "sequential" >>= Json.getBool?).toOption.getD false
  pure { entry, modules, order, sequential }

/-- Why two statements conflict, as the bits of `Bundle.conflictT`: 1 both touch the world, 2 `j` binds a name `i` reads,
4 `i` binds a name `j` reads, 8 both bind a name. A pair conflicts if and only if some bit is set. -/
def conflictReasons (arr : Array Stmt) (tab : Array EsmSplit.NameSet) (i j : Nat) : Nat :=
  let a := arr.getD i default
  let b := arr.getD j default
  (if a.effect && b.effect then 1 else 0)
    + (if !b.hoisted && b.binds.any (tab.getD i ∅).contains then 2 else 0)
    + (if !a.hoisted && a.binds.any (tab.getD j ∅).contains then 4 else 0)
    + (if !a.hoisted && !b.hoisted && a.binds.any b.binds.contains then 8 else 0)

/-- Pairs of statements that conflict and that the plan runs in the opposite order from the bundle (what `OrderOk`
rejects): how many, five of them, and (if `all`) every one as `(runs first in the output, runs first in the bundle,
reasons)`. -/
def conflictInversions (B : Bundle) (P : Plan) (all : Bool := false) :
    Nat × Array (Nat × Nat) × Array (Nat × Nat × Nat) := Id.run do
  let arr := B.toArray
  let tab := B.readsTab
  let ord := P.execOrder.toArray
  let mut count := 0
  let mut sample : Array (Nat × Nat) := #[]
  let mut every : Array (Nat × Nat × Nat) := #[]
  for x in [0:ord.size] do
    for y in [x + 1:ord.size] do
      let a := ord[x]!
      let b := ord[y]!
      if b < a && Bundle.conflictT arr tab b a then
        count := count + 1
        if sample.size < 5 then sample := sample.push (a, b)
        if all then every := every.push (a, b, conflictReasons arr tab b a)
  return (count, sample, every)

end EsmSplit

import Lean.Data.Json
import EsmSplit.Split
import EsmSplit.ModulePath

/-! Helpers shared by the executables: the per-check report and spec reader. -/

open EsmSplit Lean

namespace EsmSplit

/-- Report elapsed milliseconds and return the start time for the next phase. -/
def reportElapsed (what : String) (t0 : Nat) : IO Nat := do
  let t ← IO.monoMsNow
  IO.println s!"  [time] {what}: {t - t0} ms"
  (← IO.getStdout).flush
  return t

/-- Remove the first `--name value` pair from command-line arguments. -/
def takeFlag (name : String) : List String → Option String × List String
  | a :: v :: rest =>
    if a == name then (some v, rest) else let (r, l) := takeFlag name (v :: rest); (r, a :: l)
  | l => (none, l)

/-- Evaluate a check and print how long it took. `lazyPure` makes sure the work happens between the clocks. -/
def timedCheck (label : String) (p : Unit → Bool) : IO Bool := do
  let t0 ← IO.monoMsNow
  let r ← IO.lazyPure p
  let dt := (← IO.monoMsNow) - t0
  IO.println s!"  {if r then "pass" else "FAIL"}  {label}  ({dt} ms)"
  (← IO.getStdout).flush
  return r

/-- Check the plan's layout. An authored spec also requires checking that the plan follows its selectors.
The translation checker constructs its plan from validated statement pairs and supplies no spec. -/
def checkLayout (B : Bundle) (P : Plan) (spec : Option Spec) (verbose : Bool) : IO Bool := do
  let check (name : String) (p : Unit → Bool) : IO Bool :=
    if verbose then timedCheck name p else IO.lazyPure p
  let unique ← check "UniqueDefs" fun _ => decide (UniqueDefs B)
  let closed ← check "ReadsClosed" fun _ => decide (ReadsClosed B)
  let faithful ← match spec with
    | some S => check "Faithful" fun _ => decide (Faithful B S P)
    | none => pure true
  let rs := [← check "Linkable" fun _ => decide (Linkable B P),
             ← check "ExportsUnique" fun _ => decide (ExportsUnique B P),
             ← check "Hygienic" fun _ => decide (Hygienic B P),
             ← check "AssignsHome" fun _ => decide (AssignsHome B P),
             ← check "Permutes" fun _ => decide (Permutes B P)]
  return unique && closed && faithful && rs.all id

def reportPlanChecks (B : Bundle) (S : Spec) (P : Plan) : IO Bool := do
  let t0 ← IO.monoMsNow
  let order ← IO.lazyPure fun _ => P.evalOrder
  let exec ← IO.lazyPure fun _ => P.execOrder
  IO.println s!"  module order  {" → ".intercalate order}"
  IO.println s!"  statement order  {if exec.length > 40 then s!"[{exec.length} statements]" else toString exec}  ({(← IO.monoMsNow) - t0} ms)"
  let layout ← checkLayout B P (some S) true
  let orderOk ← timedCheck "OrderOk" fun _ => decide (OrderOk B P)
  return layout && orderOk

/-- Read a spec file: `{"entry": "main.mjs", "modules": {"a.mjs": ["name", "#7"], …}, "order": ["a.mjs", …]}`.
Module order follows the sorted keys of the parsed object; `order` is optional and keeps its order;
`"sequential": true` makes the modules run in exactly that order (`Spec.sequential`). -/
def parseSpec (j : Json) : Except String Spec := do
  let entry ← j.getObjVal? "entry" >>= Json.getStr?
  let mods ← j.getObjVal? "modules" >>= Json.getObj?
  validateModulePaths (entry :: mods.toList.map (·.1))
  let modules ← mods.toList.mapM fun (name, v) => do
    let sels ← (← v.getArr?).toList.mapM fun x => do
      let s ← x.getStr?
      if s.startsWith "#" then
        let some k := (s.drop 1).toNat? | throw s!"bad selector {s}"
        pure (Selector.stmt k)
      else pure (Selector.name s)
    pure (name, sels)
  let order ← match j.getObjVal? "order" with
    | .ok v => do
      let items ← v.getArr?
      items.toList.mapM fun x => x.getStr?
    | .error _ => pure []
  let sequential := (j.getObjVal? "sequential" >>= Json.getBool?).toOption.getD false
  let privateEntryBody ← match j.getObjVal? "privateEntryBody" with
    | .ok v => v.getBool?
    | .error _ => pure false
  pure { entry, modules, order, sequential, privateEntryBody }

/- The certification entry point accepts an authored spec, not the richer
instance-generator control format. Check duplicates while parsing: `Json.parse`
otherwise overwrites earlier object members, including escaped-equivalent keys. -/
namespace AuthoredSpecJson

open Std.Internal.Parsec Std.Internal.Parsec.String

private partial def fields (value : String → Parser Json) (seen : Std.HashSet String)
    (acc : List (String × Json)) : Parser Json := do
  Lean.Json.Parser.lookahead (· == '"') "object key"
  skip
  let key ← Lean.Json.Parser.str
  if seen.contains key then fail s!"duplicate authored spec key: {key}"
  ws
  Lean.Json.Parser.lookahead (· == ':') ":"
  skip
  ws
  let item ← value key
  let acc := (key, item) :: acc
  let c ← any
  if c == '}' then
    ws
    return Json.mkObj acc.reverse
  else if c == ',' then
    ws
    fields value (seen.insert key) acc
  else
    fail "unexpected character in authored spec object"

private def object (value : String → Parser Json) : Parser Json := do
  skipString "{"
  ws
  if (← peek!) == '}' then
    skip
    ws
    return Json.mkObj []
  fields value {} []

private def rootValue (key : String) : Parser Json := do
  if key == "modules" && (← peek!) == '{' then
    object fun _ => Lean.Json.Parser.anyCore
  else
    Lean.Json.Parser.anyCore

def parse (text : String) : Except String Json :=
  Parser.run (do
    ws
    let result ← object rootValue
    eof
    pure result) text

end AuthoredSpecJson

/-- Decode exactly the externally authored certification format. Internal
instance-control JSON has additional metadata and continues to use `parseSpec`. -/
def parseAuthoredSpec (text : String) : Except String Spec := do
  let j ← AuthoredSpecJson.parse text
  let fields ← j.getObj?
  for (key, _) in fields.toList do
    unless ["entry", "modules", "order", "sequential", "privateEntryBody"].contains key do
      throw s!"unexpected authored spec field: {key}"
  let sequential ← match j.getObjVal? "sequential" with
    | .ok value => value.getBool?
    | .error _ => pure false
  let spec ← parseSpec j
  return { spec with sequential }

def loadAuthoredSpec (path : System.FilePath) : IO Spec := do
  IO.ofExcept (parseAuthoredSpec (← IO.FS.readFile path))

/-- Read a selector-only ownership export. Its import priority orders requests;
the untrusted planner and the linked checker decide the actual evaluation order. -/
def parseResolvedSpec (j : Json) (chunk entry : String) (unitCount : Nat) : Except String Spec := do
  unless (← j.getObjValAs? String "chunk_id") == chunk do throw "resolved placement chunk ID differs"
  -- A caller with a required evaluation order must supply an ordinary sequential spec.
  unless (← j.getObj?).toList.all (fun e => ["chunk_id", "source_sha256", "import_order", "units"].contains e.1) do
    throw "unexpected resolved placement field"
  let owners ← j.getObjValAs? (List String) "import_order"
  validateModulePaths owners
  let order := owners.map (· ++ ".mjs")
  let names := entry :: order
  validateModulePaths names
  unless names.Nodup do throw "duplicate module in resolved placement"
  let mut groups : Std.HashMap String (List Selector) := {}
  let units ← j.getObjValAs? (Array Json) "units"
  unless units.size == unitCount do throw "resolved placement unit count differs from source"
  let allowed := Std.HashSet.ofList owners
  for (unit, i) in units.zipIdx do
    unless (← unit.getObjValAs? Nat "ordinal") == i do throw "expected consecutive source-unit ordinals"
    let value ← unit.getObjVal? "owner"
    let dest ← if value == .null then pure entry else do
      let owner ← value.getStr?
      unless allowed.contains owner do throw s!"resolved owner {owner} absent from import priority"
      pure (owner ++ ".mjs")
    groups := groups.insert dest (.stmt i :: groups.getD dest [])
  unless order.all groups.contains do throw "resolved import priority includes an owner with no units"
  return { entry, modules := names.map fun name => (name, (groups.getD name []).reverse), order }

def loadSpec (path : System.FilePath) : IO Spec := do
  IO.ofExcept (Json.parse (← IO.FS.readFile path) >>= parseSpec)

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

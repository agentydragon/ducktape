import EsmSplit.Instance

/-!
# Controls: things that must be rejected

Spec-level controls are generated into `Instance.lean` (`control_*_rejected`).
Here: plans a *buggy splitter* could emit for the good spec, and a check that the rejected
orders change behavior in the semantic model, so rejection is not just conservatism.
-/

open EsmSplit

namespace Example

def dropImport (m src : ModName) (P : Plan) : Plan :=
  { P with mods := P.mods.map fun md =>
      if md.name == m then { md with imports := md.imports.filter (·.1 != src) } else md }

/-- main.mjs still reads `registry` but forgot to import it. -/
def plan_missing_import : Plan := dropImport "main.mjs" "registry.mjs" plan
theorem missing_import_rejected : ¬ Hygienic bundle plan_missing_import := by decide +kernel

/-- Nobody imports plugins.mjs, so its `register` calls never run. -/
def plan_dropped_module : Plan := dropImport "main.mjs" "plugins.mjs" plan
theorem dropped_module_rejected : ¬ Permutes bundle plan_dropped_module := by decide +kernel

def planControls : List (String × Plan) :=
  [("missing_import", plan_missing_import), ("dropped_module", plan_dropped_module)]

/-! ## Export names

The entry's own statement publishes `X` as `m`, and `show.mjs` needs the entry's local `m`. -/

def exportBundle : Bundle := [
  { src := "const X = 1;", defs := ["X"] },
  { src := "const m = 2;", defs := ["m"] },
  { src := "function show() { return m; }", defs := ["show"], lazy := ["m"], hoisted := true },
  { src := "export { X as m };", eager := ["X"], exports := [("m", some "X")] }]

def exportSpec : Spec where
  entry := "main.mjs"
  modules := [("main.mjs", [.name "X", .name "m", .stmt 3]), ("show.mjs", [.name "show"])]

/-- The splitter exports the local `m` under another name. -/
theorem export_alias_valid : Valid exportBundle exportSpec (mkPlan exportBundle exportSpec) := by decide +kernel

theorem export_alias_named : ((mkPlan exportBundle exportSpec).find "main.mjs").map (·.exports) = some [("m$1", "m")] := by
  decide +kernel

/-- A splitter that exports it as `m` publishes `m` twice. -/
theorem export_collision_rejected : ¬ ExportsUnique exportBundle
    { entry := "main.mjs"
      mods := [{ name := "main.mjs", stmts := [0, 1, 3], imports := [("show.mjs", [])], exports := [("m", "m")] },
               { name := "show.mjs", stmts := [2], imports := [("main.mjs", ["m"])], exports := [] }] } := by
  decide +kernel

/-! ## The semantic model sees the difference

A value records where it came from (statement `j` applied to the values it read). The world is a
transcript of effect results. An effect also sees how much has happened so far, the way
`registry.join` sees earlier registrations. -/

def meaning : Meaning (List Nat) (List (List Nat)) where
  pure j vs := j :: vs.flatten
  run j vs w := let v := j :: w.length :: vs.flatten; (v, w ++ [v])

def start : St (List Nat) (List (List Nat)) := ⟨fun _ => [], []⟩

/-- Everything observable after running statements in this order: every name's value, and the world. -/
def observe (ord : List Nat) : List (List Nat) × List (List Nat) :=
  let s := bundle.exec meaning start ord
  ((bundle.flatMap (·.defs)).map s.env, s.world)

/-- The accepted split ends in the bundle's state (an instance of `plan_correct`, computed). -/
theorem good_observes_same : observe plan.execOrder = observe (List.range bundle.length) := by
  decide +kernel

theorem cycle_variant_observes_same :
    observe (mkPlan bundle variant_cycle).execOrder = observe (List.range bundle.length) := by
  decide +kernel

theorem effect_order_observes_differently :
    observe (mkPlan bundle control_effect_order).execOrder ≠ observe (List.range bundle.length) := by
  decide +kernel

theorem cycle_tdz_observes_differently :
    observe (mkPlan bundle control_cycle_tdz).execOrder ≠ observe (List.range bundle.length) := by
  decide +kernel

end Example

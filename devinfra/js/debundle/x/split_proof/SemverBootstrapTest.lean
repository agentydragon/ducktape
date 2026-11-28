import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.Scope
import EsmSplit.ESTreeEmit
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input
import EsmSplit.SplitFiles

open Lean CoreJS EsmSplit SplitFiles

namespace SemverBootstrap

private inductive Row where
  | create | defineProperty
  deriving DecidableEq

private def Row.target : Row → CoreJS.Name
  | .create => "__create"
  | .defineProperty => "__defProp"

private def Row.key : Row → String
  | .create => "create"
  | .defineProperty => "defineProperty"

-- The global reference is resolved at execution time; the destination is an
-- already allocated mutable module-var cell, as in execStmt's var case.
private def save {w : World} (fuel : Nat) (env : Env) (global : GlobalEnv) (row : Row) : M w Unit := do
  let ref ← resolveGlobal env global "Object"
  let base ← readGlobal (callFn fuel) "Object" ref
  let value ← getProp (callFn fuel) base (.str row.key)
  writeVar env row.target value

private theorem save_same {w : World} (fuel : Nat) (global : GlobalEnv) (row : Row) {a b : Env}
    (cell : a.vars.lookup "Object" = b.vars.lookup "Object")
    (target : a.vars.lookup row.target = b.vars.lookup row.target) :
    (save fuel a global row : M w Unit) = save fuel b global row := by
  unfold save
  refine M.bind_congr (by simp only [resolveGlobal, cell]) fun ref => ?_
  refine M.bind_congr rfl fun base => ?_
  exact M.bind_congr rfl (writeVar_agree target)

private def decode (node : Json) : Except String Row := do
  let field (j : Json) (k : String) := j.getObjValAs? String k
  unless (← field node "type") == "VariableDeclaration" && (← field node "kind") == "var" do
    throw "expected a var declaration"
  let [declarator] ← node.getObjValAs? (List Json) "declarations" | throw "expected a single declarator"
  let dst ← declarator.getObjVal? "id"
  let init ← declarator.getObjVal? "init"
  let obj ← init.getObjVal? "object"
  let prop ← init.getObjVal? "property"
  unless (← field dst "type") == "Identifier" && (← field init "type") == "MemberExpression" &&
      !(← init.getObjValAs? Bool "computed") && !(← init.getObjValAs? Bool "optional") &&
      (← field obj "type") == "Identifier" && (← field obj "name") == "Object" && (← field obj "k") == "G" &&
      (← field prop "type") == "Identifier" do
    throw "expected an ordinary Get on the ambient Object reference"
  match ← field dst "name", ← field prop "name" with
  | "__create", "create" => pure .create
  | "__defProp", "defineProperty" => pure .defineProperty
  | _, _ => throw "unexpected bootstrap member"

private def runRows {w : World} (fuel : Nat) (env : Env) (global : GlobalEnv) :
    List Row → M w Unit
  | [] => pure ()
  | r :: rs => save fuel env global r *> runRows fuel env global rs

private theorem runRows_same {w : World} (fuel : Nat) (global : GlobalEnv) (rows : List Row) {a b : Env}
    (cell : a.vars.lookup "Object" = b.vars.lookup "Object")
    (targets : ∀ row ∈ rows, a.vars.lookup row.target = b.vars.lookup row.target) :
    (runRows fuel a global rows : M w Unit) = runRows fuel b global rows := by
  induction rows with
  | nil => rfl
  | cons row tail ih =>
    simp only [runRows, save_same fuel global row cell (targets row (by simp))]
    rw [ih fun r h => targets r (List.mem_cons_of_mem _ h)]

/-- The independently decoded schedule is ordered, not a set. Callback
fuel, the ordinary global environment, and the input state are shared. -/
private theorem sameCheckedSchedule {w : World} {original staged : List Json} {rows : List Row}
    (source : original.mapM decode = .ok rows) (output : staged.mapM decode = .ok rows)
    (fuel : Nat) (global : GlobalEnv) {a b : Env}
    (cell : a.vars.lookup "Object" = b.vars.lookup "Object")
    (targets : ∀ row ∈ rows, a.vars.lookup row.target = b.vars.lookup row.target) :
    original.mapM decode = staged.mapM decode ∧
      (runRows fuel a global rows : M w Unit) = runRows fuel b global rows :=
  ⟨source.trans output.symm, runRows_same fuel global rows cell targets⟩

private def logStmt (mark : String) : CoreJS.Stmt :=
  .expr (.assign (.var "trace") (.binary .add (.var "trace") (.str mark)))

private def getter (env : Env) (receiver mark : String) (finish : CoreJS.Stmt) : M World.probe Val :=
  makeFunc env <| .mk "getter" .method false []
    [.ite (.binary .strictEq .this (.var receiver)) (.block []) (.throw (.str "wrong receiver")),
     logStmt mark, finish]

private def scenario (rows : List Row) (bindingThrows propertyThrows mutableBinding : Bool) :
    M World.probe (String × Bool × Bool) := do
  let trace ← newCell ⟨some (.str ""), true⟩
  let create ← newCell ⟨some .undef, true⟩
  let defProp ← newCell ⟨some .undef, true⟩
  let original ← alloc { proto := none, props := [] }
  let replacement ← alloc { proto := none, props := [("create", .data (.str "replaced") true true true)] }
  let globalObj ← alloc { proto := none, props := [] }
  let saved ← alloc { proto := none, props := [] }
  let sentinel ← alloc { proto := none, props := [] }
  let receiver ← newCell ⟨some (.obj original), false⟩
  let root ← newCell ⟨some (.obj globalObj), false⟩
  let value ← newCell ⟨some (.obj saved), false⟩
  let error ← newCell ⟨some (.obj sentinel), false⟩
  let objectCell ← newCell ⟨some (.obj original), true⟩
  let env : Env := ⟨[("trace", trace), ("base", receiver), ("root", root), ("saved", value), ("sentinel", error),
    ("__create", create), ("__defProp", defProp)], .undef, none⟩
  let global : GlobalEnv := ⟨if mutableBinding then [("Object", objectCell)] else [], globalObj, .obj globalObj⟩
  let p ← getter env "base" "C" (if propertyThrows then .throw (.var "sentinel") else .ret (.var "saved"))
  let d ← getter env "base" "D" (.ret (.str "define"))
  modifyObj original fun o => { o with props := [("create", .accessor p .undef true true),
    ("defineProperty", .accessor d .undef true true)] }
  let g ← getter env "root" "B" (if bindingThrows then .throw (.var "sentinel") else .ret (.var "base"))
  modifyObj globalObj fun o => { o with props := [("Object", .accessor g .undef true true)] }
  let result ← attempt (runRows 48 env global rows)
  let .str log ← readVar env "trace" | unknown "expected a trace"
  let stored ← readVar env "__create"
  if bindingThrows || propertyThrows then
    unless stored == .undef do unknown "initializer committed after a throw"
    return (log, match result with | .error e => e == .obj sentinel | _ => false, false)
  match result with
  | .error _ => unknown "unexpected getter throw"
  | .ok () =>
    if mutableBinding then
      let ref ← resolveGlobal env global "Object"
      writeGlobal (callFn 48) "Object" ref (.obj replacement)
      let latest ← readGlobal (callFn 48) "Object" ref
      let fresh ← getProp (callFn 48) latest (.str "create")
      return (log, stored == .obj saved, fresh == .str "replaced" && (← readVar env "__create") == stored)
    return (log, stored == .obj saved, false)

private def checkScenario (rows : List Row) (bindThrow propThrow mutableBinding : Bool)
    (expected : String × Bool × Bool) : IO Unit := do
  match scenario rows bindThrow propThrow mutableBinding { initialState World.probe 1 with region := 1 } with
  | .ok observed _ =>
    unless observed == expected do throw (IO.userError s!"unexpected model result: {repr observed}")
    IO.println s!"model: {repr observed}"
  | .unknown why _ => throw (IO.userError why)
  | _ => throw (IO.userError "scenario did not complete")

def main (args : List String) : IO Unit := do
  let checkOnly := args.contains "--check-scratch"
  let [input, dest] := args.filter (· != "--check-scratch") |
    throw (IO.userError "expected pinned input and output")
  IO.FS.withTempDir fun work => do
    let original ← loadProgram input work true (some "9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44")
    let units ← IO.ofExcept (ESTreeUnits.checkedNodes original (← IO.FS.readFile input))
    let some first := units[0]? | throw (IO.userError "missing actual bootstrap")
    let some second := units[1]? | throw (IO.userError "missing actual second row")
    let .error "unsupported global Object" := FromESTree.linkedUnit [] first |
      throw (IO.userError "ordinary empty-profile lowering no longer refuses")
    let spec ← IO.ofExcept (parseAuthoredSpec "{\"entry\":\"entry.mjs\",\"modules\":{\"helper.mjs\":[\"#0\"],\"entry.mjs\":[\"#1\"]},\"order\":[\"helper.mjs\"],\"sequential\":true}")
    -- Node supplies the exact source spans as a prefix. Reparse the prefix
    -- independently, checking its first two rows again before printing.
    let prefixPath := (System.FilePath.mk dest).parent.getD "." / "prefix.mjs"
    let text ← IO.FS.readFile prefixPath
    let prefixAst ← loadProgram prefixPath work true
    let rows ← IO.ofExcept (ESTreeUnits.checkedNodes prefixAst text)
    let [a, b] := rows.toList | throw (IO.userError "expected only the two bootstrap rows")
    let before := [first, second]
    let .ok [.create, .defineProperty] := before.mapM decode | throw (IO.userError "wrong original sequence")
    let .ok [.create, .defineProperty] := [a, b].mapM decode | throw (IO.userError "wrong prefix sequence")
    let bundle := LowerSplit.sourceBundle rows
    let plan := mkPlan bundle spec
    unless decide (Valid bundle spec plan) && plan.execOrder == [0, 1] do
      throw (IO.userError "unexpected authored plan")
    let programs ← IO.ofExcept (ESTreeEmit.programs rows bundle plan text spec.entry)
    let part : OutputPart := ⟨programs, text, prefixPath, work⟩
    let result ← certifyOutput [part] (Json.mkObj []) dest work checkOnly false fun files => pure do
      let checked ← (do
        let left ← ESTreeUnits.nodes (← files.getObjVal? "helper.mjs")
        let right ← ESTreeUnits.nodes (← files.getObjVal? "entry.mjs")
        let [unit] := left.toList | throw "unexpected helper"
        let [edge, next] := right.toList | throw "unexpected entry"
        unless (← edge.getObjValAs? String "type") == "ImportDeclaration" &&
            (← FromESTree.stringLiteral (← edge.getObjVal? "source")) == "./helper.mjs" &&
            (← edge.getObjValAs? (List Json) "specifiers").isEmpty do
          throw "helper must execute first"
        let after := [unit, next]
        match hs : before.mapM decode, ho : after.mapM decode with
        | .ok [.create, .defineProperty], .ok [.create, .defineProperty] =>
          let _equivalence := fun w : World => @sameCheckedSchedule w before after _ hs ho
          pure ()
        | _, _ => throw "reread changed the bootstrap schedule") |>.mapError fun reason => ({reason} : CertificationRefusal)
      return checked
    match result with
    | .ok () => pure ()
    | .error failure => throw (IO.userError failure.reason)
  checkScenario [.create, .defineProperty] false false false ("BCBD", true, false)
  checkScenario [.create, .create, .defineProperty] false false false ("BCBCBD", true, false)
  checkScenario [.defineProperty, .create] false false false ("BDBC", true, false)
  checkScenario [.create, .defineProperty] true false false ("B", true, false)
  checkScenario [.create, .defineProperty] false true false ("BC", true, false)
  checkScenario [.create, .defineProperty] false false true ("CD", true, true)

end SemverBootstrap

def main := SemverBootstrap.main

import EsmSplit.CoreJS.CallTurn
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.StateMatch
import EsmSplit.ESTreeEmit
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input
import EsmSplit.SplitFiles

open Lean (Json)
open CoreJS CoreJS.LinkedBehavior EsmSplit SplitFiles

namespace SemverCaller

private inductive Phase where
  | idle
  | helper (exports module : Nat)
  | reply
  deriving DecidableEq

private structure Script where
  phase : Phase := .idle
  events : List String := []
  deriving DecidableEq

private def code : Nat := 900

private def world (refuse : Bool) : World where
  σ := Script
  init := {}
  step := fun _ _ _ s => (.unknown "no primitive host calls in this fixture", s)
  talk := fun told s =>
    match s.phase, told with
    | .idle, .run id 1 _ (.prim .undef) [.handle ex, .handle m] false =>
      if id == code then (.readVar 0, ⟨.helper ex m, s.events ++ ["resolve effect"]⟩)
      else (.unknown "wrong opaque site", s)
    | .helper ex m, .value (.handle f) =>
      (.call (.handle f) (.prim .undef) [.handle ex, .handle m], ⟨.reply, s.events ++ ["call effect"]⟩)
    | .reply, .threw e => (.throw e, ⟨.idle, s.events ++ ["throw"]⟩)
    | .reply, .value r =>
      (if refuse then .unknown "opaque refusal after effect" else .done r, ⟨.idle, s.events ++ ["reply"]⟩)
    | _, _ => (.unknown "unexpected opaque conversation", s)

private instance (r : Bool) : DecidableEq (world r).σ := inferInstanceAs (DecidableEq Script)

private def publicCells (entry : String) (linked : List (CoreJS.Module × Env)) : Except String (Addr × Addr) := do
  let cells ← EntryExports.resolve entry linked
  let some first := cells.lookup "open" | throw "missing public caller"
  let some other := cells.lookup "alias" | throw "missing public alias"
  unless cells.length == 2 && first != other do throw "expected exactly two distinct public cells"
  return (first, other)

private theorem initializedSame {before after : Program} (links : CheckedProbePair before after)
    (ready : links.before.ready = links.after.ready)
    (units : SameUnits (unitsOf links.before.linked) (unitsOf links.after.linked)) (w : World) (fuel : Nat) :
    runProgram w fuel before = runProgram w fuel after := by
  rw [run_after_link fuel (links.before.forWorld w), run_after_link fuel (links.after.forWorld w)]
  change runUnits fuel (unitsOf links.before.linked) (linkState w links.before.ready) =
    runUnits fuel (unitsOf links.after.linked) (linkState w links.after.ready)
  rw [ready, runUnits_same fuel units]

private def checkedPair (source files : Json) (plan : Plan) :
    Except String (Σ dep : LinkedInput.DecodedDeployment,
      (Addr × Addr) × PLift (∀ w fuel, runProgram w fuel dep.before = runProgram w fuel dep.after)) := do
  let dep ← LinkedInput.decodeSingleton source (Json.mkObj []) files plan
  let _ ← checkPlaces dep.assignment dep.before dep.after
  unless dep.before.fileUnitPlacements.contains (2, plan.entry) &&
      dep.after.fileUnitPlacements.contains (2, "wrapper.mjs") do
    throw "the original factory did not change owners"
  let links ← checkProbePair dep.before dep.after
  let first ← publicCells dep.before.entry links.before.linked
  let second ← publicCells dep.after.entry links.after.linked
  unless first == second && exportsMatchFrom links.before links.after do throw "changed public cells"
  let some ⟨ready⟩ := StateMatch.probe links.before.ready links.after.ready | throw "different ready states"
  let left := unitsOf links.before.linked
  let right := unitsOf links.after.linked
  let some ⟨units⟩ := sameUnits? left right | throw "changed executable statement or capture"
  let some factory := right.find? (·.id == 2) | throw "missing factory"
  let .decl .var "__commonJS" (.func fn) := factory.stmt | throw "factory did not lower"
  unless fn.free.contains "__getOwnPropNames" do throw "factory did not retain the live helper"
  let some original := factory.env.vars.lookup "__getOwnPropNames" | throw "missing helper"
  let some decoy := factory.env.vars.lookup "decoy" | throw "missing decoy cell"
  unless original != decoy do throw "the decoy must be a separate cell"
  let changed : LinkedUnit :=
    (factory.id, factory.stmt, { factory.env with vars := ("__getOwnPropNames", decoy) :: factory.env.vars })
  unless (sameUnits? left (right.map fun u => if u.id == factory.id then changed else u)).isNone do
    throw "same statement with the wrong helper cell passed"
  let .ok () initialized := runProgram World.probe 96 dep.after | throw "the projection did not initialize"
  let a := (initialized.cellAt original).bind Binding.value
  let b := (initialized.cellAt decoy).bind Binding.value
  unless a.isSome && a == b do throw "the two cells must hold the same value"
  pure ⟨dep, first, ⟨initializedSame links ready units⟩⟩

private def emptyEnv : Env := ⟨[], .undef, none⟩

private def log (mark : String) : CoreJS.Stmt := .expr (.prim .log [.str mark])

private def orThrow (test : Expr) (message : String) : CoreJS.Stmt :=
  .ite test (.block []) (.throw (.str message))

private def effect (failOnce : Bool) : M (world r) (Val × Addr × Addr × Addr) := do
  let loader ← newCell ⟨some .undef, true⟩
  let prior ← newCell ⟨some .undef, true⟩
  let fail ← newCell ⟨some (.bool failOnce), true⟩
  let sentinel ← alloc { proto := some objectProto, props := [] }
  let reason ← newCell ⟨some (.obj sentinel), false⟩
  let env : Env := { emptyEnv with vars := [("loader", loader), ("prior", prior), ("fail", fail), ("reason", reason)] }
  let helper ← makeFunc env (.mk "effect" .normal false ["exports", "module"] [
    orThrow (.binary .strictEq .this .undef) "wrong callback receiver",
    orThrow (.binary .strictEq (.get (.var "module") (.str "exports")) (.var "exports")) "changed module alias",
    -- The reentrant call must use the installed cache and skip the callback.
    orThrow (.binary .strictEq (.call (.var "loader") []) (.var "exports")) "cache installed too late",
    .expr (.assign (.member (.var "exports") (.str "written")) (.bool true)),
    .expr (.assign (.var "prior") (.var "exports")),
    log "effect",
    .ite (.var "fail") (.block [
      .expr (.assign (.var "fail") (.bool false)),
      .throw (.var "reason")]) (.block []),
    .ret .undef])
  let captured ← newCell ⟨some helper, false⟩
  let cb ← makeOpaque code 1 [some captured] "callback" 2 false
  return (cb, loader, prior, sentinel)

private structure PublicCall where
  loader : Val
  cache : Addr
  helper : Addr
  prior : Addr
  sentinel : Addr

private def prepare (cells : Addr × Addr) (failOnce : Bool) : TurnM (world r) PublicCall := do
  let (cb, slot, prior, sentinel) ← TurnM.lift (effect failOnce)
  let env : Env := { emptyEnv with vars := [("open", cells.1), ("alias", cells.2)] }
  let openFn ← TurnM.lift (readVar env "open")
  let alias ← TurnM.lift (readVar env "alias")
  unless openFn == alias do TurnM.lift (unknown "public aliases differ")
  let loader ← callFnTurn 96 alias .undef [cb]
  let .obj addr := loader | TurnM.lift (unknown "factory did not produce a function")
  let some (.closure fn captured _) := (← TurnM.lift (getObj addr)).code |
    TurnM.lift (unknown "expected the pinned loader closure")
  unless fn.name == "__require" && fn.opaqueBody.isNone do
    TurnM.lift (unknown "exact loader is not executable")
  let some cache := captured.vars.lookup "mod" | TurnM.lift (unknown "missing captured cache")
  let some helper := captured.vars.lookup "__getOwnPropNames" |
    TurnM.lift (unknown "missing live own-names helper")
  TurnM.lift (setCell slot loader)
  return ⟨loader, cache, helper, prior, sentinel⟩

private def invoke (fuel : Nat) (call : PublicCall) : TurnM (world r) Val :=
  callFnTurn fuel call.loader .undef []

private def firstCall (program : Program) (cells : Addr × Addr) (initFuel callFuel : Nat)
    (failOnce : Bool) : TurnM (world r) Val := do
  TurnM.lift (fun _ => runProgram (world r) initFuel program)
  let call ← prepare cells failOnce
  invoke callFuel call

/-- The caller uses one checked pair of public addresses. Congruence applies to
the whole continuation, including its allocation, opaque callback and unwind. -/
private theorem firstCall_same {before after : Program} (cells : Addr × Addr)
    (initFuel callFuel : Nat) (r failOnce : Bool)
    (initialized : runProgram (world r) initFuel before = runProgram (world r) initFuel after) :
    (firstCall before cells initFuel callFuel failOnce : TurnM (world r) Val) =
      firstCall after cells initFuel callFuel failOnce := by
  unfold firstCall
  rw [initialized]

private def checkEqual (a b : TurnResult (world r) Val) : IO Unit := do
  let same (a b : Turn (world r)) := (StateMatch.state a.state b.state).isSome &&
    a.jobs.map Job.promise == b.jobs.map Job.promise
  unless (match a, b with
    | .ok av sa, .ok bv sb => av == bv && same sa sb
    | .throw av sa, .throw bv sb => av == bv && same sa sb
    | .unknown av sa, .unknown bv sb => av == bv && same sa sb
    | .fuel, .fuel => true
    | _, _ => false) do throw (IO.userError "caller results differ")

private def checkHelperRebind (call other : PublicCall) (a b : Turn (world false)) : IO Unit := do
  let replace (addr : Addr) : M (world false) Unit := do
    let f ← makeFunc emptyEnv (.mk "replacement" .normal false []
      [log "replacement lookup", .throw (.str "lookup threw")])
    setCell addr f
  let checkCold (call : PublicCall) : TurnM (world false) Val := do
    TurnM.lift (replace call.helper)
    invoke 96 call
  checkEqual (checkCold call a) (checkCold other b)
  let .throw (.str "lookup threw") failed := checkCold call a |
    throw (IO.userError "cold loader did not use the new helper")
  unless failed.state.out == #["replacement lookup"] && failed.state.world.state.events.isEmpty &&
      (failed.state.cellAt call.cache).bind Binding.value == some (.num 0) do
    throw (IO.userError "failure before the opaque call reached the cache or callback")
  let checkHot (call : PublicCall) : TurnM (world false) (Val × Val) := do
    let first ← invoke 96 call
    TurnM.lift (replace call.helper)
    let second ← invoke 96 call
    return (first, second)
  let .ok (x, y) a' := checkHot call a | throw (IO.userError "cached call reread the helper")
  let .ok (x', y') b' := checkHot other b | throw (IO.userError "relocated cached call reread the helper")
  checkEqual (.ok y a') (.ok y' b')
  unless x == y && x == x' && a'.state.out == #["effect"] &&
      a'.state.world.state.events == ["resolve effect", "call effect", "reply"] do
    throw (IO.userError "cached call changed the exports or repeated an effect")
  IO.println "live helper: cold failure before callback; hot cache skips lookup"

private def checkTrial (dep : LinkedInput.DecodedDeployment) (cells : Addr × Addr)
    (r failOnce : Bool) : IO Unit := do
  let a ← match runProgram (world r) 96 dep.before with
    | .ok _ state => pure state
    | _ => throw (IO.userError "original initialization failed")
  let b ← match runProgram (world r) 96 dep.after with
    | .ok _ state => pure state
    | _ => throw (IO.userError "relocated initialization failed")
  let some ⟨_⟩ := StateMatch.state a b | throw (IO.userError "different initialized state")
  let (call, sa) ← match prepare cells failOnce ⟨a, []⟩ with
    | .ok c s => pure (c, s)
    | .unknown why _ => throw (IO.userError why)
    | _ => throw (IO.userError "could not invoke original public factory")
  let (other, sb) ← match prepare cells failOnce ⟨b, []⟩ with
    | .ok c s => pure (c, s)
    | .unknown why _ => throw (IO.userError why)
    | _ => throw (IO.userError "could not invoke relocated public factory")
  unless call.loader == other.loader && call.cache == other.cache do
    throw (IO.userError "different loader or cache identities")
  let mut hitFuel := false
  for fuel in List.range 97 do
    let a := invoke fuel call sa
    let b := invoke fuel other sb
    checkEqual a b
    if let .fuel := a then hitFuel := true
  unless hitFuel do throw (IO.userError "fuel control was not exercised")
  let captured (state : State (world r)) (addr : Addr) := (state.cellAt addr).bind Binding.value
  let written (state : State (world r)) (v : Val) := match v with
    | .obj a =>
      match (state.objAt a).bind (·.props.lookup (.str "written")) with
      | some (.data (.bool true) ..) => true
      | _ => false
    | _ => false
  match invoke 96 call sa, invoke 96 other sb with
  | .ok value a, .ok _ b =>
    unless a.state.world.state.events == ["resolve effect", "call effect", "reply"] &&
        !r && !failOnce && a.state.out == #["effect"] &&
        captured a.state call.prior == some value && written a.state value do
      throw (IO.userError "wrong return, write or alias")
    checkEqual (invoke 96 call a) (invoke 96 other b)
    let .ok cached rest := invoke 96 call a | throw (IO.userError "cached call did not return")
    unless cached == value && rest.state.out == a.state.out do
      throw (IO.userError "cached call replayed or changed the export")
    IO.println "normal: effect once, cached export identity"
  | .throw reason a, .throw _ b =>
    let some failedExports := captured a.state call.prior | throw (IO.userError "missing failed export object")
    unless a.state.world.state.events == ["resolve effect", "call effect", "throw"] &&
        failOnce && !r && reason == .obj call.sentinel && written a.state failedExports &&
        captured a.state call.cache == some (.num 0) && a.state.out == #["effect"] do
      throw (IO.userError "throw did not preserve its reason, write and cache reset")
    checkEqual (invoke 96 call a) (invoke 96 other b)
    let .ok retried after := invoke 96 call a | throw (IO.userError "retry did not succeed")
    unless retried != failedExports && written after.state retried && after.state.out == #["effect", "effect"] do
      throw (IO.userError "retry did not allocate fresh exports and rerun the callback")
    IO.println "throw: original sentinel, cache reset, fresh retry"
  | .unknown "opaque refusal after effect" a, .unknown _ _ =>
    let some failedExports := captured a.state call.prior | throw (IO.userError "missing failedExports export")
    unless a.state.world.state.events == ["resolve effect", "call effect", "reply"] &&
        r && !failOnce && written a.state failedExports && a.state.out == #["effect"] &&
        captured a.state call.cache != some (.num 0) do
      throw (IO.userError "refusal lost effects or executed catch")
    IO.println "refusal: effect retained; no catch"
  | _, _ => throw (IO.userError "unexpected caller result")

private def checkHelperTrial (dep : LinkedInput.DecodedDeployment) (cells : Addr × Addr) : IO Unit := do
  let setup (p : Program) : TurnResult (world false) PublicCall :=
    (do
      TurnM.lift (fun _ => runProgram (world false) 96 p)
      prepare cells false) ⟨initialState (world false) 0, []⟩
  let .ok call a := setup dep.before | throw (IO.userError "original loader not initialized")
  let .ok other b := setup dep.after | throw (IO.userError "moved loader not initialized")
  checkHelperRebind call other a b

def main (args : List String) : IO Unit := do
  let [full, projection, dest] := args | throw (IO.userError "expected full source, projection and scratch output")
  IO.FS.withTempDir fun work => do
    let fullAst ← loadProgram full work true (some "9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44")
    let fullRows ← IO.ofExcept (ESTreeUnits.nodes fullAst)
    let .error "unsupported global Object" := FromESTree.linkedUnit [] fullRows[0]! |
      throw (IO.userError "unexpected full-source boundary")
    let text ← IO.FS.readFile projection
    let ast ← loadProgram projection work true
    let rows ← IO.ofExcept (ESTreeUnits.checkedNodes ast text)
    unless rows.size == 5 do throw (IO.userError "unexpected projection inputs")
    let spec ← IO.ofExcept (parseAuthoredSpec "{\"entry\":\"entry.mjs\",\"modules\":{\"wrapper.mjs\":[\"#0\",\"#1\",\"#2\"],\"entry.mjs\":[\"#3\",\"#4\"]},\"order\":[\"wrapper.mjs\"],\"sequential\":true}")
    let bundle := LowerSplit.sourceBundle rows
    let plan := mkPlan bundle spec
    unless decide (Valid bundle spec plan) && plan.execOrder == [0, 1, 2, 3, 4] do
      throw (IO.userError "projection order changed")
    let programs ← IO.ofExcept (ESTreeEmit.programs rows bundle plan text spec.entry)
    let result ← certifyOutput [⟨programs, text, projection, work⟩] (Json.mkObj []) dest work false false fun files =>
      pure ((checkedPair ast files plan).mapError fun reason => ({reason} : CertificationRefusal))
    match result with
    | .error why => throw (IO.userError why.reason)
    | .ok ⟨dep, cells, ⟨initialized⟩⟩ =>
      let _caller := fun initFuel callFuel r failOnce =>
        firstCall_same cells initFuel callFuel r failOnce (initialized (world r) initFuel)
      checkTrial dep cells false false
      checkTrial dep cells false true
      checkTrial dep cells true false
      checkHelperTrial dep cells

end SemverCaller

def main := SemverCaller.main

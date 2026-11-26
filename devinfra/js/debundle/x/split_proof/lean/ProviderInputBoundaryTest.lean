import EsmSplit.CoreJS.LinkedPivotCertificate
import EsmSplit.CoreJS.SharedProviderBoundary
import EsmSplit.CoreJS.LinkedInput

open Lean (Json toJson)
open CoreJS CoreJS.LinkedBehavior

namespace ProviderBoundaryFixture

private def exactUnit : SyntaxMatch.MatchEq LinkedUnit
  | (i, stmt, env), (j, stmt', env') => do
    let ⟨id⟩ ← SyntaxMatch.scalarEq i j
    let ⟨body⟩ ← SyntaxMatch.stmtEq stmt stmt'
    let ⟨scope⟩ ← SyntaxMatch.scalarEq env env'
    return ⟨id ▸ body ▸ scope ▸ rfl⟩

private theorem filterOther (name : String) : ∀ (xs : List (Module × Env)),
    xs.all (fun x => x.1.name != name) = true → xs.filter (fun x => x.1.name == name) = []
  | [], _ => rfl
  | x :: xs, ok => by
    simp only [List.all_cons, Bool.and_eq_true] at ok
    have different : (x.1.name == name) = false := by simpa using ok.1
    simp [different, filterOther name xs ok.2]

private theorem providerPrefix (name : String) (m : Module) (env : Env) (rest : List (Module × Env))
    (named : m.name = name) (unique : rest.all (fun x => x.1.name != name) = true) :
    ProviderPrefix name ((m, env) :: rest) (unitsOf [(m, env)]) := by
  refine ⟨m, env, rfl, named, ?_, rfl⟩
  simp [named, filterOther name rest unique]

private def provider (linked : List (Module × Env)) : Except String
    (Σ m : Module, Σ env : Env, Σ rest : List (Module × Env),
      PLift (linked = (m, env) :: rest ∧ ProviderPrefix "provider.mjs" linked (unitsOf [(m, env)]))) := do
  match h : linked with
  | [] => throw "linked program has no provider"
  | (m, env) :: rest =>
    if named : m.name = "provider.mjs" then
      if unique : rest.all (fun x => x.1.name != "provider.mjs") = true then
        return ⟨m, env, rest, ⟨h, h.symm ▸ providerPrefix "provider.mjs" m env rest named unique⟩⟩
      else throw "provider module is repeated"
    else throw "provider module is not first"

private def providerCells (linked : List (Module × Env)) : Except String
    (Σ cells : EntryExports.Bindings, PLift (EntryExports.resolve "provider.mjs" linked = .ok cells)) := do
  match h : EntryExports.resolve "provider.mjs" linked with
  | .ok cells => return ⟨cells, ⟨h⟩⟩
  | .error why => throw why

/-- The fixture's proof is about the decoded CoreJS graphs, for every world and fuel. -/
def Proved (source candidate : Program) : Prop :=
  (∀ w fuel, runProgram w fuel source = runProgram w fuel candidate) ∧ SameExports source candidate

/-- An external implementation must correspond to the *same* modeled
initializer and callable on all states, bounds and arguments. This premise
includes effects and abrupt outcomes; export names alone do not establish it. -/
structure ProviderCorrespondence (w : World) (init : Nat → M w Unit)
    (call : Nat → Val → List Val → M w Val)
    (externalInit : Nat → M w Unit) (externalCall : Nat → Val → List Val → M w Val) : Prop where
  initialization : ∀ fuel state, externalInit fuel state = init fuel state
  invocation : ∀ fuel receiver args state, externalCall fuel receiver args state = call fuel receiver args state

/-- Correspondence is an explicit boundary obligation; it does not assume
equality of complete importer runs. -/
theorem commonProviderBehavior {w : World} {init : Nat → M w Unit}
    {call : Nat → Val → List Val → M w Val} {initA initB : Nat → M w Unit}
    {callA callB : Nat → Val → List Val → M w Val}
    (a : ProviderCorrespondence w init call initA callA)
    (b : ProviderCorrespondence w init call initB callB) :
    (∀ fuel state, initA fuel state = initB fuel state) ∧
    (∀ fuel receiver args state, callA fuel receiver args state = callB fuel receiver args state) := by
  exact ⟨fun fuel state => (a.initialization fuel state).trans (b.initialization fuel state).symm,
    fun fuel receiver args state => (a.invocation fuel receiver args state).trans
      (b.invocation fuel receiver args state).symm⟩

/-- The modeled operations in the correspondence are the actual linked
prefix and callable. A separate cell/capture check must identify `setter`. -/
theorem sourceFreeLinkedBoundary {source candidate : Program} {w : World}
    (boundary : SharedProvider source candidate w) (fuel : Nat)
    (continuations : ∀ state, runUnits fuel boundary.pre boundary.before.ready = .ok () state →
      runUnits fuel boundary.sourceTail state = runUnits fuel boundary.candidateTail state)
    (setter : Val) {initA initB : Nat → M w Unit} {callA callB : Nat → Val → List Val → M w Val}
    (a : ProviderCorrespondence w (fun n => runUnits n boundary.pre)
      (fun n receiver args => callFn n setter receiver args) initA callA)
    (b : ProviderCorrespondence w (fun n => runUnits n boundary.pre)
      (fun n receiver args => callFn n setter receiver args) initB callB) :
    runProgram w fuel source = runProgram w fuel candidate ∧
    (∀ n state, initA n state = initB n state) ∧
    (∀ n receiver args state, callA n receiver args state = callB n receiver args state) :=
  ⟨boundary.run_eq fuel continuations, commonProviderBehavior a b⟩

/-- The fixture's importer calls `set(true)` once. This dispatcher substitutes
the supplied operation only when the reached callee is the checked setter. -/
private def fixtureCall : Stmt := .expr (.call (.var "set") [.bool true])

private def dispatchSetter {w : World} (setter callee : Val)
    (call : Nat → Val → List Val → M w Val) (fuel : Nat) (receiver : Val) (args : List Val) : M w Val :=
  if callee = setter then call fuel receiver args else callFn fuel callee receiver args

private theorem dispatchSetter_eq {w : World} (setter callee : Val)
    (call : Nat → Val → List Val → M w Val)
    (corresponds : ∀ fuel receiver args state,
      call fuel receiver args state = callFn fuel setter receiver args state)
    (fuel : Nat) (receiver : Val) (args : List Val) :
    dispatchSetter setter callee call fuel receiver args = callFn fuel callee receiver args := by
  by_cases h : callee = setter
  · subst callee
    simp only [dispatchSetter, ↓reduceIte]
    funext state
    exact corresponds fuel receiver args state
  · simp [dispatchSetter, h]

private def injectedStmt {w : World} (fuel : Nat) (env : Env) (setter : Val)
    (call : Nat → Val → List Val → M w Val) : M w Flow :=
  match fuel with
  | 0 | 1 => outOfFuel
  | n + 2 => do
    let callee ← evalExpr n env (.var "set")
    let args ← evalArgs n env [.bool true]
    let _ ← dispatchSetter setter callee call n .undef args
    pure .normal

private theorem injectedStmt_eq {w : World} (fuel : Nat) (env : Env) (setter : Val)
    (call : Nat → Val → List Val → M w Val)
    (corresponds : ∀ fuel receiver args state,
      call fuel receiver args state = callFn fuel setter receiver args state) :
    injectedStmt fuel env setter call = execStmt fuel env fixtureCall := by
  cases fuel with
  | zero => rfl
  | succ fuel =>
    cases fuel with
    | zero => rfl
    | succ n => simp only [injectedStmt, fixtureCall, execStmt, evalExpr,
        dispatchSetter_eq setter _ call corresponds, M.bind_assoc]

/-- The same interpreter schedule, replacing only the fixture's reached call
unit. All other units retain their actual CoreJS semantics. -/
def injectedUnits {w : World} (fuel : Nat) (setter : Val)
    (call : Nat → Val → List Val → M w Val) : List LinkedUnit → M w Unit
  | [] => pure ()
  | (id, stmt, env) :: rest => do
    let _ ← if (SyntaxMatch.stmtEq stmt fixtureCall).isSome then
      inRegion id (injectedStmt fuel env setter call)
    else runUnit fuel env id stmt
    injectedUnits fuel setter call rest

private theorem injectedUnits_eq {w : World} (fuel : Nat) (setter : Val)
    (call : Nat → Val → List Val → M w Val)
    (corresponds : ∀ fuel receiver args state,
      call fuel receiver args state = callFn fuel setter receiver args state) :
    ∀ units, injectedUnits fuel setter call units = runUnits fuel units := by
  intro units
  induction units with
  | nil => rfl
  | cons unit rest ih =>
    obtain ⟨id, stmt, env⟩ := unit
    cases h : SyntaxMatch.stmtEq stmt fixtureCall with
    | none => simp [injectedUnits, runUnits, h, ih]
    | some equal =>
      obtain ⟨equal⟩ := equal
      subst stmt
      simp [injectedUnits, runUnits, h, runUnit, injectedStmt_eq fuel env setter call corresponds, ih]

/-- Substituting each corresponding initializer and setter into the real
linked importer schedules preserves the entire result, including throws. -/
theorem sourceFreeContext {source candidate : Program} {w : World}
    (boundary : SharedProvider source candidate w) (fuel : Nat)
    (continuations : ∀ state, runUnits fuel boundary.pre boundary.before.ready = .ok () state →
      runUnits fuel boundary.sourceTail state = runUnits fuel boundary.candidateTail state)
    (setter : Val) {initA initB : Nat → M w Unit} {callA callB : Nat → Val → List Val → M w Val}
    (a : ProviderCorrespondence w (fun n => runUnits n boundary.pre)
      (fun n receiver args => callFn n setter receiver args) initA callA)
    (b : ProviderCorrespondence w (fun n => runUnits n boundary.pre)
      (fun n receiver args => callFn n setter receiver args) initB callB) :
    (initA fuel >>= fun _ => injectedUnits fuel setter callA boundary.sourceTail) boundary.before.ready =
    (initB fuel >>= fun _ => injectedUnits fuel setter callB boundary.candidateTail) boundary.after.ready ∧
    (initA fuel >>= fun _ => injectedUnits fuel setter callA boundary.sourceTail) boundary.before.ready =
      runProgram w fuel source ∧
    (initB fuel >>= fun _ => injectedUnits fuel setter callB boundary.candidateTail) boundary.after.ready =
      runProgram w fuel candidate := by
  have initEqA : initA fuel = runUnits fuel boundary.pre := by funext state; exact a.initialization fuel state
  have initEqB : initB fuel = runUnits fuel boundary.pre := by funext state; exact b.initialization fuel state
  rw [injectedUnits_eq fuel setter callA a.invocation, injectedUnits_eq fuel setter callB b.invocation,
    initEqA, initEqB]
  have sourceEq : (runUnits fuel boundary.pre >>= fun _ => runUnits fuel boundary.sourceTail) boundary.before.ready =
      runProgram w fuel source := by
    rw [run_after_link fuel boundary.before, boundary.sourceSchedule, runUnits_append]
  have candidateEq : (runUnits fuel boundary.pre >>= fun _ => runUnits fuel boundary.candidateTail) boundary.after.ready =
      runProgram w fuel candidate := by
    rw [run_after_link fuel boundary.after, boundary.candidateSchedule, runUnits_append]
  exact ⟨sourceEq.trans ((boundary.run_eq fuel continuations).trans candidateEq.symm), sourceEq, candidateEq⟩

/-- In addition to run equality, retain the actual checked boundary and its
quantified external-provider obligation for every world. -/
def ExternalProved (source candidate : Program) (setter : Val) : Prop :=
  ∀ w, ∃ boundary : SharedProvider source candidate w,
    ∀ (fuel : Nat) (initA initB : Nat → M w Unit) (callA callB : Nat → Val → List Val → M w Val),
      ProviderCorrespondence w (fun n => runUnits n boundary.pre)
        (fun n receiver args => callFn n setter receiver args) initA callA →
      ProviderCorrespondence w (fun n => runUnits n boundary.pre)
        (fun n receiver args => callFn n setter receiver args) initB callB →
      (initA fuel >>= fun _ => injectedUnits fuel setter callA boundary.sourceTail) boundary.before.ready =
        (initB fuel >>= fun _ => injectedUnits fuel setter callB boundary.candidateTail) boundary.after.ready ∧
      (initA fuel >>= fun _ => injectedUnits fuel setter callA boundary.sourceTail) boundary.before.ready =
        runProgram w fuel source ∧
      (initB fuel >>= fun _ => injectedUnits fuel setter callB boundary.candidateTail) boundary.after.ready =
        runProgram w fuel candidate

/-- Construct #20043's boundary from the actual links; every missing witness refuses. -/
def certify {source candidate : Program} (before : CheckedLink source World.probe)
    (after : CheckedLink candidate World.probe) (setter : Val := .undef) :
    Except String (PLift (Proved source candidate ∧ ExternalProved source candidate setter)) := do
  let ⟨first, firstEnv, left, ⟨leftLink, leftProvider⟩⟩ ← provider before.linked
  let ⟨second, secondEnv, right, ⟨rightLink, rightProvider⟩⟩ ← provider after.linked
  let ⟨leftCells, ⟨leftExports⟩⟩ ← providerCells before.linked
  let ⟨rightCells, ⟨rightExports⟩⟩ ← providerCells after.linked
  let ⟨sameCells⟩ ← if h : leftCells = rightCells then pure (PLift.up h) else throw "provider export cells differ"
  unless (unitsOf [(first, firstEnv)]).all (fun u => match u.stmt with
      | .opaque .. => true
      | _ => false) do throw "provider interface was not retained as opaque code"
  let some ⟨samePre⟩ := SyntaxMatch.listEq exactUnit (unitsOf [(first, firstEnv)])
    (unitsOf [(second, secondEnv)]) | throw "provider units or scope differ"
  let some ⟨sameReady⟩ := StateMatch.probe before.ready after.ready | throw "linked initial states differ"
  let some ⟨sameTail⟩ := sameUnits? (unitsOf left) (unitsOf right) | throw "importer continuations differ"
  if entry : exportsMatchFrom before after = true then
    let boundary (w : World) : SharedProvider source candidate w := {
      before := before.forWorld w
      after := after.forWorld w
      name := "provider.mjs"
      cells := leftCells
      pre := unitsOf [(first, firstEnv)]
      sourceTail := unitsOf left
      candidateTail := unitsOf right
      ready := congrArg (linkState w) sameReady
      sourceSchedule := by change unitsOf before.linked = _; rw [leftLink]; simp [unitsOf]
      candidateSchedule := by change unitsOf after.linked = _; rw [rightLink, samePre]; simp [unitsOf]
      sourceProvider := leftProvider
      candidateProvider := by
        change ProviderPrefix "provider.mjs" after.linked (unitsOf [(first, firstEnv)])
        rw [samePre]
        exact rightProvider
      sourceCells := leftExports
      candidateCells := by
        change EntryExports.resolve "provider.mjs" after.linked = .ok leftCells
        rw [sameCells]
        exact rightExports
      entryCells := entry
    }
    have continuation (w : World) (fuel : Nat) : ∀ state,
        runUnits fuel (boundary w).pre (boundary w).before.ready = .ok () state →
        runUnits fuel (boundary w).sourceTail state = runUnits fuel (boundary w).candidateTail state := by
      intro state _
      have same : (runUnits fuel (unitsOf left) : M w Unit) = runUnits fuel (unitsOf right) := runUnits_same fuel sameTail
      exact congrFun same state
    return ⟨⟨⟨fun w fuel => (boundary w).run_eq fuel (continuation w fuel),
      SharedProvider.sameExports boundary⟩, fun w => ⟨boundary w, by
        intro fuel initA initB callA callB a b
        exact sourceFreeContext (boundary w) fuel (continuation w fuel) setter a b⟩⟩⟩
  else throw "entry live cells differ"


/-- The interface gives each retained declaration its own world turn; the
setter's captured second cell is the live value binding. -/
private def writingWorld : World where
  σ := Nat
  init := 0
  step := fun _ _ _ state => (.ret .undef, state)
  talk := fun _ state =>
    match state with
    | 0 => (.writeVar 0 (.prim (.bool false)), 1)
    | 1 => (.done (.prim .undef), 2)
    | 2 => (.writeVar 0 (.prim (.bool false)), 3)
    | 3 => (.done (.prim .undef), 4)
    | 4 => (.writeVar 1 (.prim (.bool true)), 5)
    | 5 => (.done (.prim .undef), 6)
    | _ => (.unknown "unexpected provider turn", state)

private def exercise (source candidate : Program) (live snapshot : Addr) : Except String Nat := do
  let .ok () before := runProgram writingWorld 64 source | throw "original did not complete in the scripted world"
  let .ok () after := runProgram writingWorld 64 candidate | throw "candidate did not complete in the scripted world"
  letI : DecidableEq writingWorld.σ := by change DecidableEq Nat; infer_instance
  unless (StateMatch.state before after).isSome do throw "scripted world results differ"
  let turns : Nat := before.world.state
  unless turns == 6 do throw s!"unexpected provider turn count {turns}"
  let some actual := before.cellAt live | throw "live cell disappeared"
  let some copied := before.cellAt snapshot | throw "entry snapshot cell disappeared"
  unless actual.value == some (.bool true) && copied.value == some (.bool true) do
    throw "provider write was not observed through the live cell"
  return turns

private def plan : EsmSplit.Plan where
  entry := "entry.mjs"
  mods := [⟨"entry.mjs", [0, 2, 3], [("part.mjs", [])], []⟩,
    ⟨"part.mjs", [1], [], []⟩]

private def cell (linked : List (Module × Env)) (moduleName name : String) : Except String Addr := do
  let some (_, env) := linked.find? (fun pair => pair.1.name == moduleName) | throw s!"missing module {moduleName}"
  let some addr := env.vars.lookup name | throw s!"missing binding {moduleName}:{name}"
  return addr

/-- This is an explicit model of a source-free interface, not a decoded provider.
Its initializer and the body of `set` are interpreted by the world. -/
private def interfaceProvider (id code : Nat) : Module where
  name := "provider.mjs"
  imports := []
  body := [(id, .opaque code [("value", .var), ("alternate", .var)]
    [("set", 1, .ordinary true)] [] .world)]
  exports := [("value", "value"), ("alternate", "alternate"), ("set", "set")]

private def sourceFreeWorld : World where
  σ := Nat
  init := 0
  step := fun _ _ _ state => (.ret .undef, state)
  talk := fun told state =>
    match state, told with
    | 0, .run _ 0 _ (.prim .undef) [] false => (.writeVar 0 (.prim (.bool false)), 1)
    | 1, .value _ => (.done (.prim .undef), 2)
    | 2, .run _ 1 (.handle _) (.prim .undef) [.prim (.bool true)] false => (.writeVar 0 (.prim (.bool true)), 3)
    | 3, .value _ => (.done (.prim .undef), 4)
    | _, _ => (.unknown "unexpected provider turn", state)

private def sourceFreeThrowWorld : World where
  σ := Nat
  init := 0
  step := fun _ _ _ state => (.ret .undef, state)
  talk := fun _ state => (.throw (.prim (.str "provider stopped")), state + 1)

private def injectedExecution {p : Program} (checked : CheckedLink p World.probe) (w : World)
    (setter : Val) : Except String (Res w Unit) := do
  let ⟨m, env, rest, _⟩ ← provider checked.linked
  let ready := (checked.forWorld w).ready
  let init : Nat → M w Unit := fun fuel state => runUnits fuel (unitsOf [(m, env)]) state
  let call : Nat → Val → List Val → M w Val := fun fuel receiver args state =>
    callFn fuel setter receiver args state
  return (init 64 >>= fun _ => injectedUnits 64 setter call (unitsOf rest)) ready

private def sourceFreeExercise {source candidate : Program}
    (sourceLink : CheckedLink source World.probe) (candidateLink : CheckedLink candidate World.probe)
    (setter : Val) (live snapshot : Addr) : Except String Unit := do
  let .ok () before := runProgram sourceFreeWorld 64 source | throw "original source-free world did not complete"
  let .ok () after := runProgram sourceFreeWorld 64 candidate | throw "candidate source-free world did not complete"
  letI : DecidableEq sourceFreeWorld.σ := by change DecidableEq Nat; infer_instance
  unless (StateMatch.state before after).isSome do throw "source-free world results differ"
  let injectedSource ← injectedExecution sourceLink sourceFreeWorld setter
  let injectedCandidate ← injectedExecution candidateLink sourceFreeWorld setter
  let .ok () injectedBefore := injectedSource | throw "injected original did not complete"
  let .ok () injectedAfter := injectedCandidate | throw "injected candidate did not complete"
  letI : DecidableEq sourceFreeWorld.σ := by change DecidableEq Nat; infer_instance
  unless (StateMatch.state before injectedBefore).isSome && (StateMatch.state after injectedAfter).isSome do
    throw "injected provider execution differs"
  let turns : Nat := before.world.state
  unless turns == 4 do throw s!"unexpected source-free provider turns {turns}"
  unless (before.cellAt live).bind (·.value) == some (.bool true) &&
      (before.cellAt snapshot).bind (·.value) == some (.bool true) do
    throw "source-free provider write was not observed"
  let .throw (.str a) thrown := runProgram sourceFreeThrowWorld 64 source | throw "original provider throw was not propagated"
  let .throw (.str b) thrown' := runProgram sourceFreeThrowWorld 64 candidate | throw "candidate provider throw was not propagated"
  let injectedSourceThrow ← injectedExecution sourceLink sourceFreeThrowWorld setter
  let injectedCandidateThrow ← injectedExecution candidateLink sourceFreeThrowWorld setter
  let .throw (.str c) injectedThrow := injectedSourceThrow | throw "injected provider throw was not propagated"
  let .throw (.str d) injectedThrow' := injectedCandidateThrow | throw "injected candidate throw was not propagated"
  letI : DecidableEq sourceFreeThrowWorld.σ := by change DecidableEq Nat; infer_instance
  let count : Nat := thrown.world.state
  unless a == "provider stopped" && b == a && c == a && d == a && count == 1 &&
      (thrown.cellAt snapshot).bind (·.value) == some .undef &&
      (StateMatch.state thrown thrown').isSome && (StateMatch.state thrown injectedThrow).isSome &&
      (StateMatch.state thrown' injectedThrow').isSome do
    throw "source-free provider throw states differ"

private def inspectSourceFree (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let files ← input.getObjVal? "files"
  let iface ← input.getObjVal? "interface"
  let exports ← iface.getObjValAs? (List String) "exports"
  unless exports == ["value", "alternate", "set"] do throw "provider interface exports differ"
  let code ← iface.getObjValAs? Nat "code"
  let fragment ← (LinkedInput.decodeStructuralImporterFragment source files plan ["provider.mjs"] [1, 2]).mapError
    StructuralRefusal.reason
  let provider := interfaceProvider fragment.nextIdentity code
  let beforeProgram : Program := ⟨plan.entry, fragment.source :: [provider]⟩
  let afterProgram : Program := ⟨plan.entry, fragment.emitted ++ [provider]⟩
  let _ ← checkPlaces (fragment.assignment ++ provider.unitPlacements) beforeProgram afterProgram
  let before ← checkLink beforeProgram
  let after ← checkLink afterProgram
  let callCount (linked : List (Module × Env)) := ((unitsOf linked).filter fun unit =>
    unit.id == 1 && (SyntaxMatch.stmtEq unit.stmt fixtureCall).isSome).length
  unless callCount before.linked == 1 && callCount after.linked == 1 do
    throw "fixture setter call is missing or repeated"
  let a ← cell before.linked "entry.mjs" "live"
  let b ← cell after.linked "entry.mjs" "live"
  let setter ← cell before.linked "entry.mjs" "set"
  let setterAfter ← cell after.linked "part.mjs" "set"
  let providerCells ← EntryExports.resolve "provider.mjs" before.linked
  let providerCellsAfter ← EntryExports.resolve "provider.mjs" after.linked
  unless providerCells.lookup "value" == some a && b == a &&
      providerCells.lookup "set" == some setter && setterAfter == setter && providerCellsAfter == providerCells do
    throw "source-free provider cell mismatch"
  let some alternate := providerCells.lookup "alternate" | throw "source-free alternate cell missing"
  let some (.obj fn) := (before.ready.cellAt setter).bind (·.value) | throw "provider setter is not an object"
  let some (.opaque actualCode part captures _) := (before.ready.objAt fn).bind (·.code) | throw "provider setter is not opaque"
  unless actualCode == code && part == 1 && captures == [some a, some alternate, some setter] do
    throw "provider setter captures differ"
  let _ ← certify before after (.obj fn)
  let entry ← EntryExports.resolve "entry.mjs" before.linked
  let emitted ← EntryExports.resolve "entry.mjs" after.linked
  unless entry == emitted && entry.lookup "live" == some a do throw "source-free entry cells differ"
  let some snapshot := entry.lookup "snapshot" | throw "source-free snapshot cell missing"
  sourceFreeExercise before after (.obj fn) a snapshot
  return Json.mkObj [("live", toJson (a.region, a.index)), ("providerTurns", toJson (4 : Nat))]

def inspect (input : Json) : Except String Json := do
  if (input.getObjVal? "interface").isOk then return ← inspectSourceFree input
  let source ← input.getObjVal? "source"
  let providers ← input.getObjVal? "providers"
  let files ← input.getObjVal? "files"
  let decoded ← (LinkedInput.decodeStructuralSingleton source providers files plan [1, 2]).mapError
    StructuralRefusal.reason
  let _ ← checkPlaces decoded.assignment decoded.before decoded.after
  let before ← checkLink decoded.before
  let after ← checkLink decoded.after
  let _ ← certify before after
  let a ← cell before.linked "entry.mjs" "live"
  let b ← cell after.linked "entry.mjs" "live"
  let setter ← cell before.linked "entry.mjs" "set"
  let setterAfter ← cell after.linked "part.mjs" "set"
  let provider ← (EntryExports.resolve "provider.mjs" before.linked).mapError id
  let providerAfter ← (EntryExports.resolve "provider.mjs" after.linked).mapError id
  let some exported := provider.lookup "value" | throw "provider has no value export"
  unless a == exported && b == exported do throw "importer live binding does not match the provider export"
  unless setter == setterAfter && provider.lookup "set" == some setter do throw "setter alias does not match the provider export"
  unless provider == providerAfter do throw "provider exports differ"
  let entry ← (EntryExports.resolve "entry.mjs" before.linked).mapError id
  let emitted ← (EntryExports.resolve "entry.mjs" after.linked).mapError id
  unless entry == emitted do throw "entry live cells differ"
  let some snapshot := entry.lookup "snapshot" | throw "entry has no snapshot export"
  let turns ← exercise decoded.before decoded.after a snapshot
  return Json.mkObj [
    ("live", toJson (a.region, a.index)),
    ("setter", toJson (setter.region, setter.index)),
    ("entry", toJson (entry.map fun (name, addr) => (name, (addr.region, addr.index)))),
    ("worldTurns", toJson turns)]

end ProviderBoundaryFixture

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  let result := Json.parse line >>= ProviderBoundaryFixture.inspect
  match result with
  | .ok value => IO.println (Json.mkObj [("status", toJson "ok"), ("result", value)]).compress
  | .error reason => IO.println (Json.mkObj [("status", toJson "refused"), ("reason", toJson reason)]).compress
  return 0

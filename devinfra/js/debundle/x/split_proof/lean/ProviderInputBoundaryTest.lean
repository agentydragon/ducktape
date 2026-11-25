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

/-- Construct #20043's boundary from the actual links; every missing witness refuses. -/
def certify {source candidate : Program} (before : CheckedLink source World.probe)
    (after : CheckedLink candidate World.probe) : Except String (PLift (Proved source candidate)) := do
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
    return ⟨⟨fun w fuel => (boundary w).run_eq fuel (fun state _ => by
      have same : (runUnits fuel (unitsOf left) : M w Unit) = runUnits fuel (unitsOf right) := runUnits_same fuel sameTail
      exact congrFun same state), SharedProvider.sameExports boundary⟩⟩
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

def inspect (input : Json) : Except String Json := do
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

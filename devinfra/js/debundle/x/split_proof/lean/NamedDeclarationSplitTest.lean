import EsmSplit.CoreJS.NamedDeclarationSplit
import EsmSplit.CoreJS.StateMatch

open CoreJS CoreJS.NamedDeclarationSplit

private def entry := "entry.mjs"
private def worker := "worker.mjs"
private def name (i : Nat) : String := s!"binding{i}"

/-- An unbounded family: every declaration after the first reads its predecessor. -/
private def source (n : Nat) (kind : DeclKind := .const) : Program :=
  let body := (List.range n).map fun i =>
    (i, .decl kind (name i) (if i == 0 then .num 7 else .var (name (i - 1))))
  ⟨entry, [⟨entry, [], body, if n == 0 then [] else [("last", name (n - 1))]⟩]⟩

private def request (n cut : Nat) : EsmSplit.Spec := {
  entry
  modules := [(entry, ((List.range n).drop cut).map .stmt),
    (worker, ((List.range n).take cut).map .stmt)]
  order := [worker]
  sequential := true
}

private def requireRefusal (label : String) (source : Program) (spec : EsmSplit.Spec) : IO Unit := do
  match construct source spec with
  | .error _ => pure ()
  | .ok _ => throw (IO.userError s!"{label}: inadmissible request was accepted")

private def checkFamily (n : Nat) (kind : DeclKind := .const) : IO Unit := do
  let cut := n / 2
  let original := source n kind
  let spec := request n cut
  let result ← IO.ofExcept ((construct original spec).mapError fun why => s!"{n} declarations: {why}")
  let output := result.split.program
  let some moved := output.find worker | throw (IO.userError "missing provider")
  let some remaining := output.find entry | throw (IO.userError "missing entry")
  unless moved.body.map Prod.fst == (List.range n).take cut &&
      remaining.body.map Prod.fst == (List.range n).drop cut do
    throw (IO.userError s!"{n} declarations: wrong owners or original ordinals")
  unless output.evalOrder == [worker, entry] do throw (IO.userError "authored module order changed")
  let some exports := LinkedBehavior.exports World.probe original | throw (IO.userError "source link failed")
  let some outputExports := LinkedBehavior.exports World.probe output | throw (IO.userError "output link failed")
  unless decide (exports = outputExports) do throw (IO.userError "live exports changed")
  let some last := exports.lookup "last" | throw (IO.userError "missing last export")
  unless last == ⟨n, 0⟩ do throw (IO.userError "last export did not resolve to its original live cell")
  let .ok () before := runProgram World.probe 16 original | throw (IO.userError "source did not complete")
  let .ok () after := runProgram World.probe 16 output | throw (IO.userError "constructed program did not complete")
  let .ok (.num value) _ := readCell last after | throw (IO.userError "last binding is not a number")
  unless value == 7 do throw (IO.userError "earlier read changed value")
  unless (StateMatch.probe before after).isSome do throw (IO.userError "constructed final state changed")
  match runProgram World.probe 0 original, runProgram World.probe 0 output with
  | .fuel, .fuel => pure ()
  | _, _ => throw (IO.userError "fuel exhaustion changed")

private def run : IO Unit := do
  let negationSource : Program := ⟨entry, [⟨entry, [], [
    (0, .decl .const "seed" (.num 7)),
    (1, .decl .const "ready" (.unary .not (.var "seed"))),
    (2, .decl .const "result" (.var "ready"))], [("result", "result")]⟩]⟩
  let negationRequest : EsmSplit.Spec := {
    entry
    modules := [(entry, [.stmt 2]), (worker, [.stmt 0, .stmt 1])]
    order := [worker]
    sequential := true
  }
  let negationBuilt ← IO.ofExcept ((construct negationSource negationRequest).mapError
    ("logical negation: " ++ ·))
  unless negationBuilt.split.program.evalOrder == [worker, entry] do
    throw (IO.userError "logical negation: authored order changed")
  let .ok () _ := runProgram World.probe 32 negationBuilt.split.program |
    throw (IO.userError "logical negation: constructed graph did not complete")
  let arithmeticSource : Program := ⟨entry, [⟨entry, [], [
    (0, .decl .let_ "seed" (.num 7)),
    (1, .decl .let_ "ready" (.binary .add (.var "seed") (.num 2))),
    (2, .decl .let_ "result" (.var "ready"))], [("result", "result")]⟩]⟩
  let arithmeticRequest : EsmSplit.Spec := {
    entry
    modules := [(entry, [.stmt 1, .stmt 2]), (worker, [.stmt 0])]
    order := [worker]
    sequential := true
  }
  let arithmeticBuilt ← IO.ofExcept ((construct arithmeticSource arithmeticRequest).mapError
    ("let arithmetic: " ++ ·))
  unless arithmeticBuilt.split.program.evalOrder == [worker, entry] do
    throw (IO.userError "let arithmetic: authored order changed")
  let .ok () arithmeticBefore := runProgram World.probe 32 arithmeticSource |
    throw (IO.userError "let arithmetic: source did not complete")
  let .ok () arithmeticAfter := runProgram World.probe 32 arithmeticBuilt.split.program |
    throw (IO.userError "let arithmetic: constructed graph did not complete")
  unless (StateMatch.probe arithmeticBefore arithmeticAfter).isSome do
    throw (IO.userError "let arithmetic: constructed final state changed")
  for n in [1, 2, 5, 19, 64] do checkFamily n
  for n in [1, 2, 5, 19, 64] do checkFamily n .let_
  let emptySource := source 0
  let emptySpec : EsmSplit.Spec := { entry, modules := [(entry, [])], order := [], sequential := true }
  let _ ← IO.ofExcept (construct emptySource emptySpec)
  let namedSpec : EsmSplit.Spec := { (request 2 1) with modules :=
    [(entry, [.name (name 1)]), (worker, [.name (name 0)])] }
  let _ ← IO.ofExcept (construct (source 2) namedSpec)
  let badNames : Program := ⟨entry, [⟨entry, [],
    [(0, .decl .const "same" (.num 1)), (1, .decl .const "same" (.num 2))], []⟩]⟩
  let simpleSpec : EsmSplit.Spec := { entry, modules := [(entry, [.stmt 0, .stmt 1])], sequential := true }
  requireRefusal "duplicate declaration" badNames simpleSpec
  let forward : Program := ⟨entry, [⟨entry, [],
    [(0, .decl .const "a" (.var "b")), (1, .decl .const "b" (.num 2))], []⟩]⟩
  requireRefusal "forward read" forward simpleSpec
  let nestedForward : Program := ⟨entry, [⟨entry, [],
    [(0, .decl .const "a" (.binary .add (.num 1) (.unary .neg (.var "b")))),
     (1, .decl .const "b" (.num 2))], []⟩]⟩
  requireRefusal "nested forward read" nestedForward simpleSpec
  let selfRead : Program := ⟨entry, [⟨entry, [], [(0, .decl .const "a" (.var "a"))], []⟩]⟩
  let selfSpec : EsmSplit.Spec := { entry, modules := [(entry, [.stmt 0])], sequential := true }
  requireRefusal "self read" selfRead selfSpec
  let letForward : Program := ⟨entry, [⟨entry, [],
    [(0, .decl .let_ "a" (.var "b")), (1, .decl .let_ "b" (.num 2))], []⟩]⟩
  requireRefusal "lexical forward read" letForward simpleSpec
  let uninitializedLet : Program := ⟨entry, [⟨entry, [], [(0, .decl .let_ "a" .undef)], []⟩]⟩
  requireRefusal "uninitialized lexical declaration" uninitializedLet selfSpec
  let missing : EsmSplit.Spec := { (request 5 2) with modules :=
    [(entry, [.stmt 2, .stmt 3, .stmt 4, .stmt 99]), (worker, [.stmt 0, .stmt 1])] }
  requireRefusal "missing authored selector" (source 5) missing
  let repeated : EsmSplit.Spec := { (request 5 2) with modules :=
    [(entry, [.stmt 2, .stmt 3, .stmt 4]), (worker, [.stmt 0, .name (name 0), .stmt 1])] }
  requireRefusal "same original unit selected twice by different selectors" (source 5) repeated
  let wrongOrder : EsmSplit.Spec := { (request 5 2) with order := [] }
  requireRefusal "authored module order" (source 5) wrongOrder
  let base := source 2
  let movedExport : Program := { base with modules := base.modules.map fun m =>
    { m with exports := [("moved", name 0), ("last", name 1)] } }
  requireRefusal "moved public export without entry re-export" movedExport (request 2 1)
  let nonSequential : EsmSplit.Spec := { (request 5 2) with sequential := false }
  requireRefusal "non-sequential placement" (source 5) nonSequential
  let sparse : Program := ⟨entry, [⟨entry, [],
    [(0, .decl .const "a" (.num 1)), (2, .decl .const "b" (.var "a"))], []⟩]⟩
  requireRefusal "sparse original ordinals" sparse simpleSpec

def main : IO Unit := run

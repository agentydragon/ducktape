import Lean.Elab.Tactic.Cbv
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.TurnUnits
import EsmSplit.CoreJS.LinkedPrimitiveAwaitProof

/-! Positive support for the original staged fixture, independent of the
production evaluator's rules. The checked comparison below ties this proof
to the actual lowered source; it cannot change what the evaluator executes. -/

namespace CoreJS.TwoJobAwait

private def tailBody (label : String) (ending : Stmt) : List Stmt :=
  [.expr (.assignOp (.var "trace") .add (.binary .add (.str label) (.var "current"))), ending]

private def awaitDecl (name : Name) (ordinal : Nat) (tail : List Stmt) : DeferredAwait :=
  ⟨.opaque name .asyncFn false 0 ordinal 1 ["trace", "current"] (some "async invocation requires Promise semantics"),
    ⟨.mk name .asyncFn false [] (.expr (.num 0) :: tail), [], .num 0, tail⟩⟩

private def fixture : TurnProgram :=
  let left := awaitDecl "left" 2 (tailBody "A" (.throw (.str "left")))
  let right := awaitDecl "right" 3 (tailBody "B" (.ret (.var "current")))
  ⟨⟨"entry.mjs", [⟨"entry.mjs", [],
    [(0, .decl .let_ "trace" (.str "")), (1, .decl .let_ "current" (.num 0)),
      (2, .funDecl left.deferred), (3, .funDecl right.deferred),
      (4, .funDecl (.mk "setValue" .normal false ["next"]
        [.expr (.assign (.var "current") (.var "next"))])),
      (5, .decl .const "first" (.call (.var "left") [])),
      (6, .decl .const "second" (.call (.var "right") [])),
      (7, .expr (.call (.var "setValue") [.num 7])),
      (8, .decl .const "inTurn" (.var "trace"))],
    [("first", "first"), ("second", "second"), ("trace", "trace"), ("inTurn", "inTurn")]⟩]⟩,
    [(2, left), (3, right)]⟩

open LinkedBehavior PrimitiveAwait

private def base : Env :=
  ⟨[("trace", ⟨1, 0⟩), ("current", ⟨2, 0⟩), ("left", ⟨3, 0⟩), ("right", ⟨4, 0⟩),
    ("setValue", ⟨5, 0⟩), ("first", ⟨6, 0⟩), ("second", ⟨7, 0⟩), ("inTurn", ⟨9, 0⟩)], .undef, none⟩

private def leftDecl := awaitDecl "left" 2 (tailBody "A" (.throw (.str "left")))
private def rightDecl := awaitDecl "right" 3 (tailBody "B" (.ret (.var "current")))
private def setValue : Func := .mk "setValue" .normal false ["next"]
  [.expr (.assign (.var "current") (.var "next"))]
private def leftClose : Env := ⟨[("trace", ⟨1, 0⟩), ("current", ⟨2, 0⟩)], .undef, none⟩

private def scope : List (Module × Env) := (fixture).program.modules.map (·, base)
private def units := unitsOf scope

private def bound (v : Option Val) (writable : Bool := true) : Array Binding := #[⟨v, writable⟩]

private def fnObj (f : Func) (env : Env) (extra : List (PropertyKey × Property) := []) : Obj :=
  { proto := some f.intrinsicProto, code := some (.closure f env []),
    props := [(PropertyKey.str "length", Property.data (.num (Float.ofNat f.length)) false false true),
      (PropertyKey.str "name", Property.data (.str f.name) false false true)] ++ extra }

private def ready (w : World) : State w :=
  { heap := #[builtins, #[], #[], #[fnObj leftDecl.deferred leftClose], #[fnObj rightDecl.deferred leftClose],
      #[fnObj setValue ⟨[("current", ⟨2, 0⟩)], .undef, none⟩
          [("prototype", .data (.obj ⟨5, 1⟩) true false false)],
        { proto := some objectProto, props := [("constructor", .data (.obj ⟨5, 0⟩) true false true)] }],
      #[], #[], #[], #[]],
    cells := #[#[], bound none, bound none, bound (some (.obj ⟨3, 0⟩)), bound (some (.obj ⟨4, 0⟩)),
      bound (some (.obj ⟨5, 0⟩)), bound none false, bound none false, #[], bound none false],
    region := 0, out := #[], world := ⟨w.init, #[]⟩ }

attribute [local cbv_opaque] builtins Num.toString String.append

private theorem linkedProbe :
    link (fixture).program (initialState World.probe (fixture).program.regions) =
      .ok scope (ready World.probe) := by
  first | rfl | cbv

private theorem linkedEvery (w : World) :
    link (fixture).program (initialState w (fixture).program.regions) = .ok scope (ready w) := by
  exact link_from_probe ⟨scope, ready World.probe, linkedProbe⟩ w

open TurnUnits

private def cuts : List (Name × Cut) :=
  [("left", ⟨⟨3, 0⟩, leftClose, leftDecl⟩), ("right", ⟨⟨4, 0⟩, leftClose, rightDecl⟩)]

private theorem boundCuts (w : World) :
    (bindCuts units (fixture).cuts : M w _) (ready w) = .ok cuts (ready w) := by
  first | rfl | cbv

private def cell (s : State w) (region : Nat) (v : Val) (writable : Bool := true) : State w :=
  { s with cells := s.cells.set! region (bound (some v) writable) }

private def obj (s : State w) (region : Nat) (o : Obj) : State w :=
  { s with heap := s.heap.set! region #[o] }

private def initialized (w : World) : State w :=
  cell (cell (ready w) 1 (.str "")) 2 (.num 0)

private def leftJob : Job := (Job.mk ⟨6, 0⟩ 6 leftClose leftDecl.parts.after).capture
private def rightJob : Job := (Job.mk ⟨7, 0⟩ 7 leftClose rightDecl.parts.after).capture

private def afterLeft (w : World) : State w :=
  cell (obj (initialized w) 6 (AsyncPrefix.pendingObject promiseProto)) 6 (.obj ⟨6, 0⟩) false

private def afterRight (w : World) : State w :=
  cell (obj (afterLeft w) 7 (AsyncPrefix.pendingObject promiseProto)) 7 (.obj ⟨7, 0⟩) false

private def afterSet (w : World) : State w :=
  cell (cell (afterRight w) 8 (.num 7)) 2 (.num 7)

private def immediateState (w : World) : State w :=
  cell (afterSet w) 9 (.str "") false

private def immediateTurn (w : World) : Turn w := ⟨immediateState w, [leftJob, rightJob]⟩

private theorem locals (w : World) :
    runUnitsWithCuts cuts 7 (units.take 5) ⟨ready w, []⟩ = .ok () ⟨initialized w, []⟩ := by
  first | rfl | cbv

private theorem enqueueLeft (w : World) :
    runUnitWithCuts cuts 7 (units[5]'(by decide)) ⟨initialized w, []⟩ = .ok () ⟨afterLeft w, [leftJob]⟩ := by
  first | rfl | cbv

private theorem enqueueRight (w : World) :
    runUnitWithCuts cuts 7 (units[6]'(by decide)) ⟨afterLeft w, [leftJob]⟩ =
      .ok () ⟨afterRight w, [leftJob, rightJob]⟩ := by
  first | rfl | cbv

private theorem setCurrent (w : World) :
    runUnitWithCuts cuts 7 (units[7]'(by decide)) ⟨afterRight w, [leftJob, rightJob]⟩ =
      .ok () ⟨afterSet w, [leftJob, rightJob]⟩ := by
  first | rfl | cbv

private theorem sampleInTurn (w : World) :
    runUnitWithCuts cuts 7 (units[8]'(by decide)) ⟨afterSet w, [leftJob, rightJob]⟩ = .ok () (immediateTurn w) := by
  first | rfl | cbv

private theorem runAppend {w : World} (front suffix : List LinkedUnit) (start middle : Turn w)
    (h : runUnitsWithCuts cuts 7 front start = .ok () middle) :
    runUnitsWithCuts cuts 7 (front ++ suffix) start = runUnitsWithCuts cuts 7 suffix middle := by
  induction front generalizing start with
  | nil =>
    cases h
    rfl
  | cons u us ih =>
    simp only [List.cons_append, runUnitsWithCuts] at h ⊢
    cases step : runUnitWithCuts cuts 7 u start with
    | ok val next =>
      simp only [step] at h ⊢
      exact ih next h
    | throw | unknown | fuel => simp only [step, reduceCtorEq] at h

private theorem runCons {w : World} {u : LinkedUnit} (us : List LinkedUnit)
    {start middle : Turn w} (h : runUnitWithCuts cuts 7 u start = .ok () middle) :
    runUnitsWithCuts cuts 7 (u :: us) start = runUnitsWithCuts cuts 7 us middle := by
  simp only [runUnitsWithCuts, h]

private theorem completeSchedule (w : World) :
    runUnitsWithCuts cuts 7 units ⟨ready w, []⟩ = .ok () (immediateTurn w) := by
  let u5 := units[5]'(by decide)
  let u6 := units[6]'(by decide)
  let u7 := units[7]'(by decide)
  let u8 := units[8]'(by decide)
  calc
    _ = runUnitsWithCuts cuts 7 (units.take 5 ++ [u5, u6, u7, u8]) ⟨ready w, []⟩ := rfl
    _ = runUnitsWithCuts cuts 7 [u5, u6, u7, u8] ⟨initialized w, []⟩ :=
      runAppend _ _ _ _ (locals w)
    _ = runUnitsWithCuts cuts 7 [u6, u7, u8] ⟨afterLeft w, [leftJob]⟩ :=
      runCons _ (enqueueLeft w)
    _ = runUnitsWithCuts cuts 7 [u7, u8] ⟨afterRight w, [leftJob, rightJob]⟩ :=
      runCons _ (enqueueRight w)
    _ = runUnitsWithCuts cuts 7 [u8] ⟨afterSet w, [leftJob, rightJob]⟩ :=
      runCons _ (setCurrent w)
    _ = runUnitsWithCuts cuts 7 [] (immediateTurn w) := runCons _ (sampleInTurn w)
    _ = _ := rfl

private theorem complete (w : World) :
    runProgramTurn w 7 (fixture) = .ok () (immediateTurn w) := by
  have scheduled : runLinked 7 (fixture).cuts (unitsOf scope) ⟨ready w, []⟩ =
      runUnitsWithCuts cuts 7 units ⟨ready w, []⟩ :=
    runLinked_boundCuts 7 (2, leftDecl) [(3, rightDecl)] units ⟨ready w, []⟩ cuts (ready w) (boundCuts w)
  exact (show runProgramTurn w 7 (fixture) =
      runLinked 7 (fixture).cuts (unitsOf scope) ⟨ready w, []⟩ by
    simp only [runProgramTurn, withLinkedTurn, TurnResult.andThen, linkedEvery]).trans
      (scheduled.trans (completeSchedule w))

private def firstWrite : String := "" ++ ("A" ++ Num.toString 7)
private def lastWrite : String := firstWrite ++ ("B" ++ Num.toString 7)

private def rejection : Obj :=
  { AsyncPrefix.pendingObject promiseProto with promise := some (.rejected (.str "left")) }

private def fulfillment : Obj :=
  { AsyncPrefix.pendingObject promiseProto with promise := some (.fulfilled (.num 7)) }

private def afterReject (w : World) : State w :=
  obj (cell (immediateState w) 1 (.str firstWrite)) 6 rejection

private def afterFulfill (w : World) : State w :=
  obj (cell (afterReject w) 1 (.str lastWrite)) 7 fulfillment

private theorem resumeLeft (w : World) :
    runJob leftJob 7 (immediateState w) [rightJob] = .ok () ⟨afterReject w, [rightJob]⟩ := by
  cbv

private theorem resumeRight (w : World) :
    runJob rightJob 7 (afterReject w) [] = .ok () ⟨afterFulfill w, []⟩ := by
  cbv

private theorem drained (w : World) :
    drain 7 2 (immediateTurn w) = .ok .empty ⟨afterFulfill w, []⟩ := by
  simp only [immediateTurn, drain, resumeLeft, resumeRight]

theorem exactFixtureSupported : LinkedPrimitiveAwait.Supported (fixture) := by
  intro w
  refine ⟨7, 7, 2, immediateTurn w, ⟨afterFulfill w, []⟩, ?_, drained w⟩
  have entry : (entryEnv (fixture).program.entry scope).isSome = true := rfl
  exact congrArg TurnResult.completedTurn
    ((LinkedPrimitiveAwait.runImmediate_eq_programTurn (linkedEvery w) entry 7).trans (complete w))

private def bodyEq : SyntaxMatch.MatchEq AwaitBody := fun a b => do
  let ⟨scope⟩ ← SyntaxMatch.funcEq a.scope b.scope
  let ⟨before⟩ ← SyntaxMatch.listEq SyntaxMatch.stmtEq a.before b.before
  let ⟨operand⟩ ← SyntaxMatch.exprEq a.operand b.operand
  let ⟨after⟩ ← SyntaxMatch.listEq SyntaxMatch.stmtEq a.after b.after
  return ⟨by cases a; cases b; cases scope; cases before; cases operand; cases after; rfl⟩

private def cutEq : SyntaxMatch.MatchEq (Nat × DeferredAwait) := fun (i, a) (j, b) => do
  let ⟨id⟩ ← SyntaxMatch.scalarEq i j
  let ⟨decl⟩ ← SyntaxMatch.funcEq a.deferred b.deferred
  let ⟨body⟩ ← bodyEq a.parts b.parts
  return ⟨by cases a; cases b; cases id; cases decl; cases body; rfl⟩

def checkSupport (input : TurnProgram) : Except String (PLift (LinkedPrimitiveAwait.Supported input)) := do
  let some ⟨program⟩ := SyntaxMatch.programEq input.program (fixture).program |
    throw "the lowered source differs from the supported two-job turn"
  let some ⟨cuts⟩ := SyntaxMatch.listEq cutEq input.cuts (fixture).cuts |
    throw "the lowered await bodies differ from the supported two-job turn"
  have eq : input = fixture :=
    (congrArg (fun p => TurnProgram.mk p input.cuts) program).trans
      (congrArg (TurnProgram.mk (fixture).program) cuts)
  return ⟨Eq.mpr (congrArg LinkedPrimitiveAwait.Supported eq) exactFixtureSupported⟩

end CoreJS.TwoJobAwait

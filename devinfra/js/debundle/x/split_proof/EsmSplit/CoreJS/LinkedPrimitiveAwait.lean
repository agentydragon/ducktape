import EsmSplit.CoreJS.CallTurn
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.ProgramTurn
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.ESTreeUnits
import EsmSplit.Plan

/-!
An explicit model entry for linked modules with discarded primitive awaits
in ordinary async declarations. The bridge recognizes calls syntactically in
module expression statements and const initializers, then checks the callee
against the actual linked function object. Aliases, nested call sites and any
call outside this driver still see the ordinary deferred evaluator.

The result at the end of the module includes its pending queue. A consumer
can inspect that state or explicitly drain; this gives the driver no facts
about host checkpoints or placement safety.
-/

open Lean (Json)

namespace CoreJS.LinkedPrimitiveAwait

open PrimitiveAwait

variable {W : World}

/-- A source cut carries the exact deferred function lowered at that ordinal.
The callable site and capture list are part of that function's syntax. -/
structure DeferredAwait where
  deferred : Func
  parts : FromESTree.PrimitiveAwaitParts

structure Input where
  private mk ::
  program : Program
  cuts : List (Nat × DeferredAwait)

private def registerCuts (source : LinkedInput.DecodedSource) : List (Nat × DeferredAwait) :=
  source.nodes.toList.zipIdx.filterMap fun (node, id) => do
    let parts ← (FromESTree.primitiveAwaitParts node).toOption
    let some (.funDecl f) := source.source.body.lookup id | none
    let some (unit, _, free, some _) := f.opaqueBody | none
    guard (unit == id && f.kind == .asyncFn && f.name == parts.scope.name &&
      parts.scope.closes.all free.contains)
    return (id, ⟨f, parts⟩)

private def decodeSource (source : Json) (entry : String) : Except String LinkedInput.DecodedSource := do
  let decoded ← LinkedInput.decodeSingletonSourceNodes (← ESTreeUnits.nodes source) entry
  unless decoded.source.imports.isEmpty && !decoded.hasTemplates do
    throw "await model requires an import-free original without templates"
  return decoded

def decode (source : Json) (entry : String) : Except String Input := do
  let decoded ← decodeSource source entry
  return ⟨decoded.program, registerCuts decoded⟩

/-- Reread a placed graph through the existing syntax, ordinal and live-cell
checks. Source cuts transfer only by their checked original declaration IDs. -/
structure Placed where
  before : Input
  after : Input
  sameCuts : before.cuts = after.cuts

def decodePlacedChecked (source files : Json) (plan : EsmSplit.Plan) : Except String Placed := do
  let original ← decodeSource source plan.entry
  let (before, after) ← LinkedInput.decodePair source (Json.mkObj []) files plan
  let cuts := registerCuts original
  for (id, cut) in cuts do
    let some owner := after.modules.find? (fun m => (m.body.lookup id).isSome) |
      throw s!"missing placed async declaration {id}"
    let some (.funDecl actual) := owner.body.lookup id | throw s!"missing placed async declaration {id}"
    unless (SyntaxMatch.funcEq actual cut.deferred).isSome do
      throw s!"placed deferred closure differs at original ordinal {id}"
    let imports := owner.imports.flatMap fun i => i.names.map Prod.snd
    -- Cut code came from the original module. It cannot be reused if a
    -- captured name became an import: an assignment would then need a
    -- frozen target, even though both lookups still reach the same cell.
    if let some name := cut.parts.scope.free.find? imports.contains then
      throw s!"placed async declaration {id} captures imported binding {name}"
  return ⟨⟨before, cuts⟩, ⟨after, cuts⟩, rfl⟩

def decodePlaced (source files : Json) (plan : EsmSplit.Plan) : Except String (Input × Input) :=
  (decodePlacedChecked source files plan).map fun p => (p.before, p.after)

/-- The executable payload is attached to one concrete linked closure. An
identical name at a different address cannot acquire the payload. -/
private structure Cut where
  callee : Addr
  closed : Env
  lowered : DeferredAwait

private def bindClosure (id : Nat) (lowered : DeferredAwait) (addr : Addr) : M W (Name × Cut) := do
  unless addr.region == id + 1 do unknown "model: async closure allocated outside its original region"
  let some (.closure actual closed _) := (← getObj addr).code | unknown "model: expected an async closure"
  unless (SyntaxMatch.funcEq actual lowered.deferred).isSome do
    unknown "model: the linked closure differs from the lowered await"
  return (lowered.parts.scope.name, ⟨addr, closed, lowered⟩)

private def bindCut (id : Nat) (lowered : DeferredAwait) (units : List LinkedUnit) :
    M W (Name × Cut) := do
  let name := lowered.parts.scope.name
  let some unit := units.find? (·.id == id) | unknown "model: missing async declaration ordinal"
  let .funDecl f := unit.stmt | unknown "model: expected an async declaration at the checked ordinal"
  unless f.kind == .asyncFn && f.name == name do unknown "model: async declaration changed"
  unless (SyntaxMatch.funcEq f lowered.deferred).isSome do unknown "model: async code changed"
  let .obj addr ← readVar unit.env name | unknown "model: expected the linked async declaration"
  bindClosure id lowered addr

private def bindCuts (units : List LinkedUnit) (parts : List (Nat × DeferredAwait)) :
    M W (List (Name × Cut)) :=
  parts.mapM (fun (id, part) => bindCut id part units)

/-- The staged operand supports a member read through the shared turn
rule. The callback sees the getter's complete returned turn; only ordinary
callees are admitted here, so a deferred nested suspension still refuses. -/
private def operandTurn (env : Env) : Nat → Expr → TurnM W Val
  | n + 1, .get receiver key =>
    readProperty (getPropWith TurnM.lift (callFnTurn n))
      (TurnM.lift (evalExpr n env receiver)) (TurnM.lift (evalExpr n env key))
  | fuel, expr => TurnM.lift (evalExpr fuel env expr)

private def prefixOf (cut : Cut) (args : List Val) (fuel : Nat) : TurnM W Prefix := do
  let env ← TurnM.lift (enterFunction cut.lowered.parts.scope cut.closed .undef args)
  match ← TurnM.lift (execStmts fuel env cut.lowered.parts.before) with
  | .normal =>
    -- Each preceding item uses one layer of execStmts. At the cut
    -- the list and the expression statement each consume one more.
    let operand ← operandTurn env (fuel - cut.lowered.parts.before.length - 2) cut.lowered.parts.operand
    pure (.suspended operand env cut.lowered.parts.after)
  | .ret value => pure (.completed value)
  | _ => TurnM.lift (unknown "model: invalid control flow before await")

/-- At the staged boundary the verified ordinal and address choose a cut.
Ordinary call dispatch still reads the actual closure; only that deferred
async body may run the authenticated suspension rule. -/
private def invokeCut (fuel : Nat) (cut : Cut) (args : List Val) : TurnM W Val :=
  dispatchCallWithDeferred TurnM.lift
    (fun f closed receiver argv => TurnM.lift (runLocalCall (execStmts (fuel - 1)) f closed receiver argv))
    (fun f closed _ argv reason => fun turn =>
      if (SyntaxMatch.funcEq f cut.lowered.deferred).isSome && decide (closed = cut.closed) then
        match launchTurn promiseProto (prefixOf cut argv) fuel turn with
        | .ok promise next => .ok promise next
        | .unknown why stopped => .unknown why stopped
        | .fuel => .fuel
      else .unknown s!"deferred function {f.name}: {reason}" turn)
    (fun code cells told => TurnM.lift (converse (fuel - 1) code cells told)) (.obj cut.callee) .undef args

abbrev Immediate := TurnResult

/-- Erase the driver-only result: keep the exact shared result, including
abrupt reasons and their heap, cell, world and output state. A pending queue
is a separate observation and cannot in general be erased for a split proof. -/
def Immediate.shared (outcome : Immediate W α) : Res W Unit :=
  (outcome.map fun _ => ()).erase

def Immediate.pending : Immediate W α → Option (List Job)
  | .ok _ turn | .throw _ turn | .unknown _ turn => some turn.jobs
  | .fuel => none

private def ordinary (fuel : Nat) (unit : LinkedUnit) (turn : Turn W) : Immediate W Unit :=
  TurnResult.andThen (runUnit fuel unit.env unit.id unit.stmt) turn (fun _ next => .ok () next)

private def directCall? : Stmt → Option (Option Name × Name × List Expr)
  | .decl .const name (.call (.var callee) args) => some (some name, callee, args)
  | .expr (.call (.var callee) args) => some (none, callee, args)
  | _ => none

private def finishDirect (env : Env) (target : Option Name) (promise : Val) : M W Unit := do
  if let some dest := target then initVar env dest promise
  modifyState fun s => { s with region := 0 }

private def runUnitWithCuts (cuts : List (Name × Cut)) (fuel : Nat) (unit : LinkedUnit)
    (turn : Turn W) : Immediate W Unit :=
  match fuel, directCall? unit.stmt with
  | n + 3, some (target, name, args) =>
    match cuts.lookup name with
    | none => ordinary fuel unit turn
    | some cut =>
      let entered := { turn with state := { turn.state with region := unit.id + 1 } }
      TurnResult.andThen (readVar unit.env name) entered fun callee prepared =>
        if callee != .obj cut.callee then ordinary fuel unit turn else
        (do
          let promise ← directExprCall (fun _ _ argv => invokeCut (n + 1) cut argv)
            (pure callee) (TurnM.lift (evalArgs (n + 1) unit.env args))
          TurnM.lift (finishDirect unit.env target promise)) prepared
  | _, _ => ordinary fuel unit turn

private def runUnitsWithCuts (cuts : List (Name × Cut)) (fuel : Nat) :
    List LinkedUnit → TurnM W Unit
  | [] => pure ()
  | unit :: rest => fun turn =>
    (runUnitWithCuts cuts fuel unit >>= fun () => runUnitsWithCuts cuts fuel rest) turn

private theorem find_same {us vs : List LinkedUnit} (units : SameUnits us vs) (id : Nat) :
    match us.find? (·.id == id), vs.find? (·.id == id) with
    | none, none => True
    | some u, some v => SameUnit u v
    | _, _ => False := by
  induction units with
  | nil => trivial
  | @cons u v us vs same _ ih =>
    simp only [List.find?_cons, same.1]
    by_cases selected : (v.id == id) = true
    · simp only [selected]; exact same
    · simp only [selected]; exact ih

private theorem bindCut_same {us vs : List LinkedUnit} (units : SameUnits us vs)
    (id : Nat) (part : DeferredAwait) :
    (bindCut id part us : M W (Name × Cut)) = bindCut id part vs := by
  have h := find_same units id
  unfold bindCut
  cases u : us.find? (·.id == id) <;> cases v : vs.find? (·.id == id)
  all_goals simp only [u, v] at h
  all_goals try contradiction
  · rfl
  · rename_i left right
    simp only []
    obtain ⟨_, eq, agree⟩ := h
    rw [← eq]
    cases stmt : left.stmt <;> try rfl
    case funDecl f =>
      have names := agree.vars f.name (by simp [stmt, Stmt.free])
      by_cases ok : (f.kind == .asyncFn && f.name == part.parts.scope.name) = true
      · have nameEq : f.name = part.parts.scope.name := by
          have both := ok
          simp at both
          exact both.2
        rw [nameEq] at names
        rw [readVar_agree names]
      · funext s
        simp [ok, M.bind_apply, unknown]

private theorem bindCuts_same {us vs : List LinkedUnit} (units : SameUnits us vs)
    (parts : List (Nat × DeferredAwait)) :
    (bindCuts us parts : M W (List (Name × Cut))) = bindCuts vs parts := by
  have eq : (fun (p : Nat × DeferredAwait) => (bindCut p.1 p.2 us : M W (Name × Cut))) =
      (fun p => bindCut p.1 p.2 vs) := by
    funext p
    exact bindCut_same units p.1 p.2
  unfold bindCuts
  exact congrArg (fun f => parts.mapM f) eq

private theorem directCall_uses {stmt : Stmt} {target : Option Name} {name : Name} {args : List Expr}
    (call : directCall? stmt = some (target, name, args)) :
    name ∈ stmt.free ∧ (∀ x ∈ freeExprs args, x ∈ stmt.free) ∧
      (∀ dest ∈ target, dest ∈ stmt.free) := by
  unfold directCall? at call
  split at call <;> try contradiction
  all_goals cases call <;> simp [Stmt.free, Expr.free] <;> grind

private theorem runUnitWithCuts_same (cuts : List (Name × Cut)) (fuel : Nat)
    {u v : LinkedUnit} (unit : SameUnit u v) (turn : Turn W) :
    runUnitWithCuts cuts fuel u turn = runUnitWithCuts cuts fuel v turn := by
  obtain ⟨id, stmt, env⟩ := u
  obtain ⟨id', stmt', env'⟩ := v
  obtain ⟨hid, hstmt, bindings⟩ := unit
  dsimp only [LinkedUnit.id, LinkedUnit.stmt, LinkedUnit.env] at hid hstmt bindings
  subst id' stmt'
  have base : ordinary fuel (id, stmt, env) turn = ordinary fuel (id, stmt, env') turn := by
    unfold ordinary
    rw [runUnit_agree bindings]
  match fuel with
  | 0 | 1 | 2 => exact base
  | n + 3 =>
    dsimp only [runUnitWithCuts, LinkedUnit.stmt, LinkedUnit.id, LinkedUnit.env]
    cases call : directCall? stmt with
    | none => exact base
    | some callData =>
      obtain ⟨target, name, args⟩ := callData
      obtain ⟨usesCallee, usesArgs, usesDest⟩ := directCall_uses call
      cases cut : cuts.lookup name with
      | none => simp only [cut]; exact base
      | some cut =>
        have read : (readVar env name : M W Val) = readVar env' name :=
          readVar_agree (bindings.vars _ usesCallee)
        have argv : (evalArgs (n + 1) env args : M W (List Val)) = evalArgs (n + 1) env' args :=
          (same (W := W) (n + 1)).args _ _ _ (bindings.mono usesArgs)
        have write (promise : Val) :
            (finishDirect env target promise : M W Unit) = finishDirect env' target promise := by
          cases target with
          | none => rfl
          | some dest =>
            change (initVar env dest promise >>= fun _ => modifyState fun s => { s with region := 0 }) =
              (initVar env' dest promise >>= fun _ => modifyState fun s => { s with region := 0 })
            rw [initVar_agree (bindings.vars _ (usesDest dest (by simp))) promise]
        simp only [cut, read, argv, base, write]

private theorem runUnitsWithCuts_same (cuts : List (Name × Cut)) (fuel : Nat)
    {us vs : List LinkedUnit} (units : SameUnits us vs) (turn : Turn W) :
    runUnitsWithCuts cuts fuel us turn = runUnitsWithCuts cuts fuel vs turn := by
  induction units generalizing turn with
  | nil => rfl
  | @cons u v us vs head _ ih =>
    have step : (runUnitWithCuts cuts fuel u : TurnM W Unit) = runUnitWithCuts cuts fuel v :=
      funext (runUnitWithCuts_same cuts fuel head)
    have rest : (runUnitsWithCuts cuts fuel us : TurnM W Unit) = runUnitsWithCuts cuts fuel vs :=
      funext ih
    simp only [runUnitsWithCuts, step, rest]

def entryEnv (entry : String) (linked : List (Module × Env)) : Option Env :=
  match linked with
  | [(_, env)] => some env
  | _ => (linked.find? fun (m, _) => m.name == entry).map Prod.snd

private def runLinked (fuel : Nat) (parts : List (Nat × DeferredAwait))
    (units : List LinkedUnit) (turn : Turn W) : Immediate W Unit :=
  match parts with
  | [] => TurnResult.andThen (runUnits fuel units) turn fun () done => .ok () done
  | parts => TurnResult.andThen (bindCuts units parts) turn fun cuts ready => runUnitsWithCuts cuts fuel units ready

private theorem runLinked_same (fuel : Nat) (parts : List (Nat × DeferredAwait))
    {us vs : List LinkedUnit} (units : SameUnits us vs) (turn : Turn W) :
    runLinked fuel parts us turn = runLinked fuel parts vs turn := by
  cases parts with
  | nil =>
    unfold runLinked
    rw [runUnits_same (W := W) fuel units]
  | cons head tail =>
    simp only [runLinked]
    rw [bindCuts_same (W := W) units]
    unfold TurnResult.andThen
    split <;> first | rfl | exact runUnitsWithCuts_same _ _ units _

private theorem observed_map {α β : Type} (f : α → β) (outcome : Immediate W α) :
    (outcome.map f).observed = outcome.observed := by cases outcome <;> rfl

/-- The module phase never runs a continuation. Its callers can separately use
`PrimitiveAwait.drain` on the returned turn. -/
def runImmediate (W : World) (fuel : Nat) (input : Input) : Immediate W Env :=
  withLinkedTurn W input.program fun linked entered =>
    match entryEnv input.program.entry linked with
    | some env => (runLinked fuel input.cuts (unitsOf linked) entered).map fun () => env
    | none => .unknown "expected the linked entry module" entered

/-- Holding the shared link state, cut table and ordered free-name bindings
fixed makes all module outcomes and the full pending queue equal. -/
theorem runImmediate_same {W : World} {a b : Input} {left right : List (Module × Env)}
    {ready : State W}
    (aLink : link a.program (initialState W a.program.regions) = .ok left ready)
    (bLink : link b.program (initialState W b.program.regions) = .ok right ready)
    (aEntry : (entryEnv a.program.entry left).isSome = true)
    (bEntry : (entryEnv b.program.entry right).isSome = true)
    (cuts : a.cuts = b.cuts) (units : SameUnits (unitsOf left) (unitsOf right)) (fuel : Nat) :
    (runImmediate W fuel a).observed = (runImmediate W fuel b).observed := by
  simp only [runImmediate, withLinkedTurn, TurnResult.andThen, aLink, bLink]
  cases ha : entryEnv a.program.entry left with
  | none => simp [ha] at aEntry
  | some ea =>
    cases hb : entryEnv b.program.entry right with
    | none => simp [hb] at bEntry
    | some eb => rw [observed_map, observed_map, cuts, runLinked_same fuel b.cuts units]

/-- On a successfully linked graph with an entry and no authenticated cuts,
the staged rule returns exactly the ordinary result at the same fuel and
never introduces jobs. -/
theorem noCuts_turn {input : Input} {linked : List (Module × Env)} {ready : State W} {env : Env}
    (cuts : input.cuts = [])
    (linkedOk : link input.program (initialState W input.program.regions) = .ok linked ready)
    (entry : entryEnv input.program.entry linked = some env) (fuel : Nat) :
    (runImmediate W fuel input).observed = TurnResult.withJobs [] (runProgram W fuel input.program) := by
  simp only [runImmediate, withLinkedTurn, runProgram, M.bind_apply, TurnResult.andThen, runLinked,
    linkedOk, entry, cuts, observed_map]
  cases runUnits fuel (unitsOf linked) ready <;> rfl

/-- Once a module has linked, the no-cut path agrees with the shared
evaluator at every bound. Equality retains object identity and all stateful
outcomes; the queue is empty whenever the shared result has state. -/
theorem noCuts_agree {W : World} {input : Input} {mod : Module} {env : Env} {ready : State W}
    (cuts : input.cuts = [])
    (linked : link input.program (initialState W input.program.regions) = .ok [(mod, env)] ready)
    (fuel : Nat) :
    let immediate := runImmediate W fuel input
    let shared := runProgram W fuel input.program
    immediate.shared = shared ∧
      immediate.pending = (match shared with | .fuel => none | _ => some []) := by
  simp only [runImmediate, withLinkedTurn, runProgram, M.bind_apply, TurnResult.andThen, entryEnv, runLinked, linked, cuts]
  cases runUnits fuel (unitsOf [(mod, env)]) ready <;> exact ⟨rfl, rfl⟩

end CoreJS.LinkedPrimitiveAwait

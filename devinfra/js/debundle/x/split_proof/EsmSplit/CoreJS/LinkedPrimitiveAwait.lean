import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Split
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

structure Input where
  private mk ::
  program : Program
  cuts : List (Nat × FromESTree.PrimitiveAwaitParts)

private def registerCuts (source : LinkedInput.DecodedSource) :
    List (Nat × FromESTree.PrimitiveAwaitParts) :=
  source.nodes.toList.zipIdx.filterMap fun (node, id) => do
    let parts ← (FromESTree.primitiveAwaitParts node).toOption
    let some (.funDecl f) := source.source.body.lookup id | none
    let some (_, _, _, some _) := f.opaqueBody | none
    if f.kind == .asyncFn && f.name == parts.scope.name then some (id, parts) else none

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
  for (id, part) in cuts do
    let some owner := after.modules.find? (fun m => (m.body.lookup id).isSome) |
      throw s!"missing placed async declaration {id}"
    let imports := owner.imports.flatMap fun i => i.names.map Prod.snd
    -- Cut code came from the original module. It cannot be reused if a
    -- captured name became an import: an assignment would then need a
    -- frozen target, even though both lookups still reach the same cell.
    if let some name := part.scope.free.find? imports.contains then
      throw s!"placed async declaration {id} captures imported binding {name}"
  return ⟨⟨before, cuts⟩, ⟨after, cuts⟩, rfl⟩

def decodePlaced (source files : Json) (plan : EsmSplit.Plan) : Except String (Input × Input) :=
  (decodePlacedChecked source files plan).map fun p => (p.before, p.after)

private structure Cut where
  callee : Addr
  closed : Env
  parts : FromESTree.PrimitiveAwaitParts

private def bindCut (id : Nat) (part : FromESTree.PrimitiveAwaitParts) (units : List LinkedUnit) :
    M W (Name × Cut) := do
  let name := part.scope.name
  let some unit := units.find? (·.id == id) | unknown "model: missing async declaration ordinal"
  let .funDecl f := unit.stmt | unknown "model: expected an async declaration at the checked ordinal"
  unless f.kind == .asyncFn && f.name == name do unknown "model: async declaration changed"
  let .obj addr ← readVar unit.env name | unknown "model: expected the linked async declaration"
  let some (.closure _ closed _) := (← getObj addr).code | unknown "model: expected an async closure"
  return (name, ⟨addr, closed, part⟩)

private def bindCuts (units : List LinkedUnit)
    (parts : List (Nat × FromESTree.PrimitiveAwaitParts)) : M W (List (Name × Cut)) :=
  parts.mapM (fun (id, part) => bindCut id part units)

private def prefixOf (cut : Cut) (args : List Val) (fuel : Nat) : M W Prefix := do
  let env ← enterFunction cut.parts.scope cut.closed .undef args
  match ← execStmts fuel env cut.parts.before with
  | .normal =>
    -- Each preceding item uses one layer of execStmts. At the cut
    -- the list and the expression statement each consume one more.
    let operand ← evalExpr (fuel - cut.parts.before.length - 2) env cut.parts.operand
    pure (.suspended operand env cut.parts.after)
  | .ret value => pure (.completed value)
  | _ => unknown "model: invalid control flow before await"

inductive Immediate (W : World) (α : Type) where
  | ok (value : α) (turn : Turn W)
  | throw (reason : Val) (turn : Turn W)
  | unknown (reason : String) (turn : Turn W)
  | fuel

/-- Erase the driver-only result: keep the exact shared result, including
abrupt reasons and their heap, cell, world and output state. A pending queue
is a separate observation and cannot in general be erased for a split proof. -/
def Immediate.shared : Immediate W α → Res W Unit
  | .ok _ turn => .ok () turn.state
  | .throw reason turn => .throw reason turn.state
  | .unknown reason turn => .unknown reason turn.state
  | .fuel => .fuel

def Immediate.pending : Immediate W α → Option (List Job)
  | .ok _ turn | .throw _ turn | .unknown _ turn => some turn.jobs
  | .fuel => none

private def thenSync {α β : Type} (step : M W α) (turn : Turn W)
    (next : α → Turn W → Immediate W β) : Immediate W β :=
  match step turn.state with
  | .ok value s => next value { turn with state := s }
  | .throw reason s => .throw reason { turn with state := s }
  | .unknown why s => .unknown why { turn with state := s }
  | .fuel => .fuel

private def ordinary (fuel : Nat) (unit : LinkedUnit) (turn : Turn W) : Immediate W Unit :=
  thenSync (runUnit fuel unit.env unit.id unit.stmt) turn (fun _ next => .ok () next)

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
      thenSync (readVar unit.env name) entered fun callee prepared =>
        if callee != .obj cut.callee then ordinary fuel unit turn else
        thenSync (evalArgs (n + 1) unit.env args) prepared fun argv evaluated =>
          match launch promiseProto (prefixOf cut argv) (n + 1) evaluated with
          | .ok promise started =>
            thenSync (finishDirect unit.env target promise) started fun _ done => .ok () done
          | .unknown why stopped => .unknown why stopped
          | .fuel => .fuel
  | _, _ => ordinary fuel unit turn

private def runUnitsWithCuts (cuts : List (Name × Cut)) (fuel : Nat) :
    List LinkedUnit → Turn W → Immediate W Unit
  | [], turn => .ok () turn
  | unit :: rest, turn =>
    match runUnitWithCuts cuts fuel unit turn with
    | .ok () next => runUnitsWithCuts cuts fuel rest next
    | .throw reason stopped => .throw reason stopped
    | .unknown why stopped => .unknown why stopped
    | .fuel => .fuel

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
    (id : Nat) (part : FromESTree.PrimitiveAwaitParts) :
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
      by_cases ok : (f.kind == .asyncFn && f.name == part.scope.name) = true
      · have nameEq : f.name = part.scope.name := by
          have both := ok
          simp at both
          exact both.2
        rw [nameEq] at names
        rw [readVar_agree names]
      · funext s
        simp [ok, M.bind_apply, unknown]

private theorem bindCuts_same {us vs : List LinkedUnit} (units : SameUnits us vs)
    (parts : List (Nat × FromESTree.PrimitiveAwaitParts)) :
    (bindCuts us parts : M W (List (Name × Cut))) = bindCuts vs parts := by
  have eq : (fun (p : Nat × FromESTree.PrimitiveAwaitParts) => (bindCut p.1 p.2 us : M W (Name × Cut))) =
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
  | cons head tail ih =>
    simp only [runUnitsWithCuts, runUnitWithCuts_same cuts fuel head turn]
    split <;> first | exact ih _ | rfl

def entryEnv (entry : String) (linked : List (Module × Env)) : Option Env :=
  match linked with
  | [(_, env)] => some env
  | _ => (linked.find? fun (m, _) => m.name == entry).map Prod.snd

private def runLinked (fuel : Nat) (parts : List (Nat × FromESTree.PrimitiveAwaitParts))
    (units : List LinkedUnit) (turn : Turn W) : Immediate W Unit :=
  match parts with
  | [] => thenSync (runUnits fuel units) turn fun () done => .ok () done
  | parts => thenSync (bindCuts units parts) turn fun cuts ready => runUnitsWithCuts cuts fuel units ready

private theorem runLinked_same (fuel : Nat) (parts : List (Nat × FromESTree.PrimitiveAwaitParts))
    {us vs : List LinkedUnit} (units : SameUnits us vs) (turn : Turn W) :
    runLinked fuel parts us turn = runLinked fuel parts vs turn := by
  cases parts with
  | nil =>
    unfold runLinked
    rw [runUnits_same (W := W) fuel units]
  | cons head tail =>
    simp only [runLinked]
    rw [bindCuts_same (W := W) units]
    unfold thenSync
    split <;> first | rfl | exact runUnitsWithCuts_same _ _ units _

def Immediate.map {α β : Type} (f : α → β) : Immediate W α → Immediate W β
  | .ok value turn => .ok (f value) turn
  | .throw why turn => .throw why turn
  | .unknown why turn => .unknown why turn
  | .fuel => .fuel

/-- Entry environments differ between placements. Project them away after
checking their public addresses, keeping the entire state and pending queue. -/
def Immediate.observed (outcome : Immediate W α) : Immediate W Unit :=
  outcome.map fun _ => ()

private theorem observed_map {α β : Type} (f : α → β) (outcome : Immediate W α) :
    (outcome.map f).observed = outcome.observed := by cases outcome <;> rfl

/-- The module phase never runs a continuation. Its callers can separately use
`PrimitiveAwait.drain` on the returned turn. -/
def runImmediate (W : World) (fuel : Nat) (input : Input) : Immediate W Env :=
  thenSync (link input.program) ⟨initialState W input.program.regions, []⟩ fun linked entered =>
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
  simp only [runImmediate, thenSync, aLink, bLink]
  cases ha : entryEnv a.program.entry left with
  | none => simp [ha] at aEntry
  | some ea =>
    cases hb : entryEnv b.program.entry right with
    | none => simp [hb] at bEntry
    | some eb => rw [observed_map, observed_map, cuts, runLinked_same fuel b.cuts units]

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
  simp only [runImmediate, runProgram, M.bind_apply, thenSync, entryEnv, runLinked, linked, cuts]
  cases runUnits fuel (unitsOf [(mod, env)]) ready <;> exact ⟨rfl, rfl⟩

end CoreJS.LinkedPrimitiveAwait

import EsmSplit.CoreJS.CallTurn
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Split

namespace CoreJS

/-- Executable suspension body produced by source lowering. The whole scope
is instantiated once, then the body runs on either side of the cut. -/
structure AwaitBody where
  scope : Func
  before : List Stmt
  operand : Expr
  after : List Stmt

/-- The lowered code is keyed by the exact deferred function and original
unit, never the spelling of a call site alone. -/
structure DeferredAwait where
  deferred : Func
  parts : AwaitBody

/-- The program consumed by the fixed turn evaluator. Ordinary compiled
programs embed with no suspension bodies. -/
structure TurnProgram where
  program : Program
  cuts : List (Nat × DeferredAwait)

instance : Coe Program TurnProgram := ⟨fun p => ⟨p, []⟩⟩

namespace TurnUnits

open PrimitiveAwait
variable {W : World}

/-- The deferred constructor has no recursive fields. Compare every field
without invoking the general syntax comparison. -/
private def sameDeferred : Func → Func → Bool
  | .opaque n k self len unit part free reason, .opaque n' k' self' len' unit' part' free' reason' =>
    k == k' && n == n' && self == self' && len == len' && unit == unit' && part == part' && free == free' && reason == reason'
  | _, _ => false

/-- The executable payload is attached to one concrete linked closure. An
identical name at a different address cannot acquire the payload. -/
structure Cut where
  callee : Addr
  closed : Env
  lowered : DeferredAwait

private def bindClosure (id : Nat) (lowered : DeferredAwait) (addr : Addr) : M W (Name × Cut) := do
  unless addr.region == id + 1 do unknown "model: async closure allocated outside its original region"
  let some (.closure actual closed _) := (← getObj addr).code | unknown "model: expected an async closure"
  unless sameDeferred actual lowered.deferred do
    unknown "model: the linked closure differs from the lowered await"
  return (lowered.parts.scope.name, ⟨addr, closed, lowered⟩)

private def bindCut (id : Nat) (lowered : DeferredAwait) (units : List LinkedUnit) :
    M W (Name × Cut) := do
  let name := lowered.parts.scope.name
  let some unit := units.find? (·.id == id) | unknown "model: missing async declaration ordinal"
  let .funDecl f := unit.stmt | unknown "model: expected an async declaration at the checked ordinal"
  unless f.kind == .asyncFn && f.name == name do unknown "model: async declaration changed"
  unless sameDeferred f lowered.deferred do unknown "model: async code changed"
  let .obj addr ← readVar unit.env name | unknown "model: expected the linked async declaration"
  bindClosure id lowered addr

def bindCuts (units : List LinkedUnit) (parts : List (Nat × DeferredAwait)) :
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
      if sameDeferred f cut.lowered.deferred && decide (closed = cut.closed) then
        match launchTurn promiseProto (prefixOf cut argv) fuel turn with
        | .ok promise next => .ok promise next
        | .unknown why stopped => .unknown why stopped
        | .fuel => .fuel
      else .unknown s!"deferred function {f.name}: {reason}" turn)
    (fun code cells told => TurnM.lift (converse (fuel - 1) code cells told)) (.obj cut.callee) .undef args

private def ordinary (fuel : Nat) (unit : LinkedUnit) (turn : Turn W) : TurnResult W Unit :=
  TurnResult.andThen (runUnit fuel unit.env unit.id unit.stmt) turn (fun _ next => .ok () next)

private def directCall? : Stmt → Option (Option Name × Name × List Expr)
  | .decl .const name (.call (.var callee) args) => some (some name, callee, args)
  | .expr (.call (.var callee) args) => some (none, callee, args)
  | _ => none

private def finishDirect (env : Env) (target : Option Name) (promise : Val) : M W Unit := do
  if let some dest := target then initVar env dest promise
  modifyState fun s => { s with region := 0 }

def runUnitWithCuts (cuts : List (Name × Cut)) (fuel : Nat) (unit : LinkedUnit)
    (turn : Turn W) : TurnResult W Unit :=
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

def runUnitsWithCuts (cuts : List (Name × Cut)) (fuel : Nat) :
    List LinkedUnit → TurnM W Unit
  | [] => pure ()
  | unit :: rest => fun turn =>
    match runUnitWithCuts cuts fuel unit turn with
    | .ok _ next => runUnitsWithCuts cuts fuel rest next
    | .throw why stopped => .throw why stopped
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

def runLinked (fuel : Nat) (parts : List (Nat × DeferredAwait))
    (units : List LinkedUnit) (turn : Turn W) : TurnResult W Unit :=
  match parts with
  | [] => TurnResult.andThen (runUnits fuel units) turn fun () done => .ok () done
  | parts => TurnResult.andThen (bindCuts units parts) turn fun cuts ready => runUnitsWithCuts cuts fuel units ready

theorem runLinked_boundCuts (fuel : Nat) (head : Nat × DeferredAwait) (tail : List (Nat × DeferredAwait))
    (units : List LinkedUnit) (turn : Turn W) (cuts : List (Name × Cut)) (next : State W)
    (bound : bindCuts units (head :: tail) turn.state = .ok cuts next) :
    runLinked fuel (head :: tail) units turn = runUnitsWithCuts cuts fuel units { turn with state := next } := by
  simp only [runLinked, TurnResult.andThen, bound]

theorem runLinked_same (fuel : Nat) (parts : List (Nat × DeferredAwait))
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

end TurnUnits
end CoreJS

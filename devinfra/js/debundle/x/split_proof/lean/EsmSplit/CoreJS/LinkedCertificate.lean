import Lean.Elab.Deriving.ToExpr
import Lean.Elab.Tactic.Cbv
import Lean.Util.CollectAxioms
import EsmSplit.CoreJS.BatchState
import EsmSplit.CoreJS.ClassParents
import EsmSplit.CoreJS.DenseArray
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.LiteralRecord

namespace CoreJS.LinkedBehavior

attribute [cbv_eval] DenseArray.reduce
-- Lean cannot derive DecidableEq for nested mutual inductives. Compare the constructors
-- produced by LinkedInput; every successful branch returns equality, including deferred code.
private abbrev MatchEq (α : Type) := (a b : α) → Option (PLift (a = b))

private def scalarEq [DecidableEq α] : MatchEq α := fun a b =>
  if h : a = b then some ⟨h⟩ else none

private def listEq (item : MatchEq α) : MatchEq (List α)
  | [], [] => some ⟨rfl⟩
  | x :: xs, y :: ys => do
    let ⟨head⟩ ← item x y
    let ⟨tail⟩ ← listEq item xs ys
    return ⟨head ▸ tail ▸ rfl⟩
  | _, _ => none

mutual
private partial def exprEq : MatchEq Expr
  | .undef, .undef | .null, .null | .this, .this => some ⟨rfl⟩
  | .str a, .str b => (scalarEq a b).map fun h => ⟨congrArg Expr.str h.down⟩
  | .num a, .num b => (scalarEq a b).map fun h => ⟨congrArg Expr.num h.down⟩
  | .bool a, .bool b => (scalarEq a b).map fun h => ⟨congrArg Expr.bool h.down⟩
  | .var a, .var b => (scalarEq a b).map fun h => ⟨congrArg Expr.var h.down⟩
  | .obj a, .obj b => (listEq propEq a b).map fun h => ⟨congrArg Expr.obj h.down⟩
  | .arr a, .arr b => (listEq exprEq a b).map fun h => ⟨congrArg Expr.arr h.down⟩
  | .arrWithSpread a, .arrWithSpread b =>
    (listEq arrayElementEq a b).map fun h => ⟨congrArg Expr.arrWithSpread h.down⟩
  | .prim a [], .prim b [] =>
    (scalarEq a b).map fun h => ⟨congrArg (fun op => Expr.prim op []) h.down⟩
  | .unary op a, .unary op' b => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨v⟩ ← exprEq a b
    return ⟨o ▸ v ▸ rfl⟩
  | .binary op a b, .binary op' a' b' => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨left⟩ ← exprEq a a'
    let ⟨right⟩ ← exprEq b b'
    return ⟨o ▸ left ▸ right ▸ rfl⟩
  | .logical op a b, .logical op' a' b' => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨left⟩ ← exprEq a a'
    let ⟨right⟩ ← exprEq b b'
    return ⟨o ▸ left ▸ right ▸ rfl⟩
  | .cond c t e, .cond c' t' e' => do
    let ⟨test⟩ ← exprEq c c'
    let ⟨yes⟩ ← exprEq t t'
    let ⟨no⟩ ← exprEq e e'
    return ⟨test ▸ yes ▸ no ▸ rfl⟩
  | .seq a b, .seq a' b' => do
    let ⟨left⟩ ← exprEq a a'
    let ⟨right⟩ ← exprEq b b'
    return ⟨left ▸ right ▸ rfl⟩
  | .hasProp k o, .hasProp k' o' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨base⟩ ← exprEq o o'
    return ⟨key ▸ base ▸ rfl⟩
  | .get o k, .get o' k' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    return ⟨base ▸ key ▸ rfl⟩
  | .delete o k, .delete o' k' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    return ⟨base ▸ key ▸ rfl⟩
  | .assign t rhs, .assign t' rhs' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨value⟩ ← exprEq rhs rhs'
    return ⟨target ▸ value ▸ rfl⟩
  | .assignOp t op rhs, .assignOp t' op' rhs' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨operator⟩ ← scalarEq op op'
    let ⟨value⟩ ← exprEq rhs rhs'
    return ⟨target ▸ operator ▸ value ▸ rfl⟩
  | .assignLogical t op rhs, .assignLogical t' op' rhs' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨operator⟩ ← scalarEq op op'
    let ⟨value⟩ ← exprEq rhs rhs'
    return ⟨target ▸ operator ▸ value ▸ rfl⟩
  | .update t inc pre, .update t' inc' pre' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨increment⟩ ← scalarEq inc inc'
    let ⟨isPrefix⟩ ← scalarEq pre pre'
    return ⟨target ▸ increment ▸ isPrefix ▸ rfl⟩
  | .callMethod o k args, .callMethod o' k' args' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨base ▸ key ▸ arguments ▸ rfl⟩
  | .callOptionalReceiver o k args, .callOptionalReceiver o' k' args' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨base ▸ key ▸ arguments ▸ rfl⟩
  | .callOptionalCallee o k args, .callOptionalCallee o' k' args' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨base ▸ key ▸ arguments ▸ rfl⟩
  | .cls n self ctor ms, .cls n' self' ctor' ms' => do
    let ⟨k⟩ ← scalarEq n n'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨c⟩ ← funcEq ctor ctor'
    let ⟨m⟩ ← listEq classMemberEq ms ms'
    return ⟨k ▸ s ▸ c ▸ m ▸ rfl⟩
  | .derived n self parent ms, .derived n' self' parent' ms' => do
    let ⟨k⟩ ← scalarEq n n'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨p⟩ ← exprEq parent parent'
    let ⟨m⟩ ← listEq classMemberEq ms ms'
    return ⟨k ▸ s ▸ p ▸ m ▸ rfl⟩
  | .new f args, .new f' args' => do
    let ⟨callee⟩ ← exprEq f f'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨callee ▸ arguments ▸ rfl⟩
  | .call f args, .call f' args' => do
    let ⟨callee⟩ ← exprEq f f'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨callee ▸ arguments ▸ rfl⟩
  | .func a, .func b => (funcEq a b).map fun h => ⟨congrArg Expr.func h.down⟩
  | _, _ => none

private partial def targetEq : MatchEq Target
  | .var x, .var x' => (scalarEq x x').map fun h => ⟨congrArg Target.var h.down⟩
  | .frozen x, .frozen x' => (scalarEq x x').map fun h => ⟨congrArg Target.frozen h.down⟩
  | .member o k, .member o' k' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    return ⟨base ▸ key ▸ rfl⟩
  | _, _ => none

private partial def arrayElementEq : MatchEq (ArrayElementKind × Expr)
  | (kind, e), (kind', e') => do
    let ⟨tag⟩ ← scalarEq kind kind'
    let ⟨value⟩ ← exprEq e e'
    return ⟨tag ▸ value ▸ rfl⟩

private partial def funcEq : MatchEq Func
  | .mk name kind self params body, .mk name' kind' self' params' body' => do
    let ⟨n⟩ ← scalarEq name name'
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨p⟩ ← scalarEq params params'
    let ⟨b⟩ ← listEq stmtEq body body'
    return ⟨n ▸ k ▸ s ▸ p ▸ b ▸ rfl⟩
  | .withDefaults name kind self params arity body, .withDefaults name' kind' self' params' arity' body' => do
    let ⟨n⟩ ← scalarEq name name'
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨p⟩ ← scalarEq params params'
    let ⟨a⟩ ← scalarEq arity arity'
    let ⟨b⟩ ← listEq stmtEq body body'
    return ⟨n ▸ k ▸ s ▸ p ▸ a ▸ b ▸ rfl⟩
  | _, _ => none

private partial def classMemberEq : MatchEq ClassMember
  | .field s k v, .field s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← exprEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | .method s k v, .method s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← funcEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | .getter s k v, .getter s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← funcEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | .setter s k v, .setter s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← funcEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | _, _ => none

private partial def propEq : MatchEq PropDef
  | .init k v, .init k' v' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← exprEq v v'
    return ⟨key ▸ value ▸ rfl⟩
  | .getter k f, .getter k' f' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨fn⟩ ← funcEq f f'
    return ⟨key ▸ fn ▸ rfl⟩
  | .setter k f, .setter k' f' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨fn⟩ ← funcEq f f'
    return ⟨key ▸ fn ▸ rfl⟩
  | .proto v, .proto v' => (exprEq v v').map fun h => ⟨congrArg PropDef.proto h.down⟩
  | _, _ => none

private partial def stmtEq : MatchEq Stmt
  | .funDecl a, .funDecl b => (funcEq a b).map fun h => ⟨congrArg Stmt.funDecl h.down⟩
  | .varDecl a, .varDecl b => (scalarEq a b).map fun h => ⟨congrArg Stmt.varDecl h.down⟩
  | .expr a, .expr b => (exprEq a b).map fun h => ⟨congrArg Stmt.expr h.down⟩
  | .ret a, .ret b => (exprEq a b).map fun h => ⟨congrArg Stmt.ret h.down⟩
  | .throw a, .throw b => (exprEq a b).map fun h => ⟨congrArg Stmt.throw h.down⟩
  | .ite c t e, .ite c' t' e' => do
    let ⟨test⟩ ← exprEq c c'
    let ⟨yes⟩ ← stmtEq t t'
    let ⟨no⟩ ← stmtEq e e'
    return ⟨by rw [test, yes, no]⟩
  | .block a, .block b => (listEq stmtEq a b).map fun h => ⟨congrArg Stmt.block h.down⟩
  | .brk, .brk | .cont, .cont => some ⟨rfl⟩
  | .loop init test update body, .loop init' test' update' body' => do
    let ⟨i⟩ ← stmtEq init init'
    let ⟨t⟩ ← exprEq test test'
    let ⟨u⟩ ← exprEq update update'
    let ⟨b⟩ ← stmtEq body body'
    return ⟨i ▸ t ▸ u ▸ b ▸ rfl⟩
  | .tryCatch body param handler, .tryCatch body' param' handler' => do
    let ⟨b⟩ ← stmtEq body body'
    let ⟨p⟩ ← scalarEq param param'
    let ⟨h⟩ ← stmtEq handler handler'
    return ⟨b ▸ p ▸ h ▸ rfl⟩
  | .tryFinally body finalizer, .tryFinally body' finalizer' => do
    let ⟨b⟩ ← stmtEq body body'
    let ⟨f⟩ ← stmtEq finalizer finalizer'
    return ⟨b ▸ f ▸ rfl⟩
  | .decl kind name init, .decl kind' name' init' => do
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨n⟩ ← scalarEq name name'
    let ⟨v⟩ ← exprEq init init'
    return ⟨k ▸ n ▸ v ▸ rfl⟩
  | _, _ => none
end

private def unitEq : MatchEq (Nat × Stmt)
  | (i, s), (j, t) => do
    let ⟨id⟩ ← scalarEq i j
    let ⟨stmt⟩ ← stmtEq s t
    return ⟨id ▸ stmt ▸ rfl⟩

private def moduleEq : MatchEq Module
  | ⟨n, i, b, e⟩, ⟨n', i', b', e'⟩ => do
    let ⟨name⟩ ← scalarEq n n'
    let ⟨imports⟩ ← scalarEq i i'
    let ⟨body⟩ ← listEq unitEq b b'
    let ⟨exports⟩ ← scalarEq e e'
    return ⟨name ▸ imports ▸ body ▸ exports ▸ rfl⟩

private def programEq : MatchEq Program
  | ⟨e, m⟩, ⟨e', m'⟩ => do
    let ⟨entry⟩ ← scalarEq e e'
    let ⟨modules⟩ ← listEq moduleEq m m'
    return ⟨entry ▸ modules ▸ rfl⟩

/-- For a selected profile, quantify over worlds satisfying its observations. -/
def EquivalentFor (profile : HostProfile) (source candidate : Program) : Prop :=
  match profile with
  | .empty => Equivalent source candidate
  | .browserPage => ∀ w, profile.Satisfied w →
      ∃ t : State w, Ends w source (.ok () t) ∧ Ends w candidate (.ok () t)

theorem of_browser_evaluation {source candidate : Program} (fuel : Nat)
    (checks : ∀ w, normal (runProgram (HostProfile.browserPage.specialize w) fuel source) = true ∧
      runProgram (HostProfile.browserPage.specialize w) fuel source =
        runProgram (HostProfile.browserPage.specialize w) fuel candidate) :
    EquivalentFor .browserPage source candidate := by
  intro w hw
  have e := HostProfile.specialize_eq .browserPage w hw
  obtain ⟨hc, he⟩ := checks w
  rw [e] at hc he
  cases hs : runProgram w fuel source with
  | ok _ t => exact ⟨t, ⟨fuel, hs, by simp⟩, ⟨fuel, he.symm.trans hs, by simp⟩⟩
  | _ => simp [hs, normal] at hc

/-- The two programs are exactly the lowering of these three AST inputs under this placement.
Their public bindings and their complete normally terminating executions agree in every world. -/
def ASTEquivalent (profile premises : HostProfile) (source providers files : Lean.Json) (plan : EsmSplit.Plan) : Prop :=
  ∃ original candidate, LinkedInput.decodePair source providers files plan profile = .ok (original, candidate) ∧
    EquivalentFor premises original candidate ∧ SameExports original candidate

/-- Decode the actual ASTs and transport the execution and export proofs to those programs. -/
def certifyASTs (profile premises : HostProfile) (source providers files : Lean.Json) (plan : EsmSplit.Plan) (original candidate : Program)
    (semantics : EquivalentFor premises original candidate ∧ SameExports original candidate) :
    Except String (PLift (ASTEquivalent profile premises source providers files plan)) := do
  match decoded : LinkedInput.decodePair source providers files plan profile with
  | .error why => throw why
  | .ok (a, b) =>
    let some ⟨before⟩ := programEq a original | throw "source AST does not match the certified program"
    let some ⟨after⟩ := programEq b candidate | throw "output ASTs do not match the certified program"
    return ⟨⟨a, b, decoded, before ▸ after ▸ semantics⟩⟩

/-- Successful execution of the production checker establishes a claim about the input
and output ASTs for every host world. Its callers can discard the proof at runtime. -/
theorem checkASTs_sound {profile premises source providers files plan original candidate}
    (semantics : EquivalentFor premises original candidate ∧ SameExports original candidate)
    (accepted : (certifyASTs profile premises source providers files plan original candidate semantics).isOk = true) :
    ASTEquivalent profile premises source providers files plan := by
  cases h : certifyASTs profile premises source providers files plan original candidate semantics with
  | error why => rw [h] at accepted; contradiction
  | ok proof => exact proof.down

-- Reify every constructor and field, including the deferred code in heap closures.
-- IEEE bit patterns distinguish both zeroes and preserve NaN payloads.
local instance : Lean.ToExpr Float where
  toExpr n := Lean.mkApp (Lean.mkConst ``Float.ofBits) (Lean.toExpr n.toBits)
  toTypeExpr := Lean.mkConst ``Float

deriving instance Lean.ToExpr for ArrayElementKind
deriving instance Lean.ToExpr for DeclKind
deriving instance Lean.ToExpr for UnOp
deriving instance Lean.ToExpr for BinOp
deriving instance Lean.ToExpr for LogOp
deriving instance Lean.ToExpr for Prim
deriving instance Lean.ToExpr for FuncKind
deriving instance Lean.ToExpr for OpaqueRun
deriving instance Lean.ToExpr for Expr, Target, PropDef, ClassMember, Func, Stmt
deriving instance Lean.ToExpr for Import
deriving instance Lean.ToExpr for Module
deriving instance Lean.ToExpr for Program

deriving instance Lean.ToExpr for CoreJS.Addr
deriving instance Lean.ToExpr for CoreJS.ErrorKind
deriving instance Lean.ToExpr for CoreJS.Val
deriving instance Lean.ToExpr for CoreJS.Property
deriving instance Lean.ToExpr for CoreJS.Env
deriving instance Lean.ToExpr for CoreJS.Code
deriving instance Lean.ToExpr for CoreJS.Obj
deriving instance Lean.ToExpr for CoreJS.Binding

/-- Render only the constructor applications, closed constants and literals produced by
`ToExpr`. Program strings are data, including line breaks, quotes and Lean comment delimiters. -/
private partial def termWith (replace : Lean.Expr → Option String) (e : Lean.Expr) : Except String String := do
  if let some quoted := replace e then return quoted
  match e with
  | .app .. => do
    let f ← termWith replace e.getAppFn
    let args ← e.getAppArgs.toList.mapM (termWith replace)
    return "(" ++ String.intercalate " " (f :: args) ++ ")"
  | .const n _ => return "@" ++ n.toString
  | .lit (.strVal s) => return s.quote
  | .lit (.natVal n) => return toString n
  | _ => throw "unsupported expression in quoted CoreJS data"

private def term := termWith (fun _ => none)

/-! `State World.probe` has a closed shape. Keep a canonical projection of every
field in the run's final state; the probe's host state is `Unit`. Closures retain
their full code and environment through `Obj.code`. Floats use the `ToExpr`
instance above, which quotes their IEEE bit patterns. -/
private structure ProbeStateObservation where
  heap : List (List CoreJS.Obj)
  cells : List (List CoreJS.Binding)
  region : Nat
  out : List String
  worldState : Unit
  handed : List CoreJS.Addr
  deriving Lean.ToExpr

private def observeProbeState (profile : HostProfile) (state : CoreJS.State (profile.specialize CoreJS.World.probe)) : ProbeStateObservation :=
  { heap := state.heap.toList.map Array.toList
    cells := state.cells.toList.map Array.toList
    region := state.region
    out := state.out.toList
    worldState := state.world.state
    handed := state.world.handed.toList }

private def probeStateLiteral (profile : HostProfile) (state : CoreJS.State (profile.specialize CoreJS.World.probe)) : String :=
  match term (Lean.toExpr (observeProbeState profile state)) with
  | .ok literal => literal
  | .error _ => ""

/-- Compare the complete state of two completed executions in the concrete probe.
This is a refusal check only; matching probes still go through the all-world proof. -/
def probeStatesEqual (profile : HostProfile) (left right : CoreJS.State (profile.specialize CoreJS.World.probe)) : Bool :=
  probeStateLiteral profile left == probeStateLiteral profile right

/-- A closed Lean constructor term for the complete decoded program. -/
def programLiteral (p : Program) : Except String String := term (Lean.toExpr p)

private abbrev StringFields := List (String × String)

private def wideRecordMinFields : Nat := 96

private structure RecordQuotes where
  fields : Array StringFields := #[]
  replacements : Std.HashMap Lean.Expr String := {}

/-- Intern ordered field lists across the two programs. Replace complete top-level
units, whose source identities distinguish them from statements in function bodies. -/
private def recordQuotes (source candidate : Program) : Except String RecordQuotes := do
  let mut quotes : RecordQuotes := {}
  for program in [source, candidate] do
    for md in program.modules do
      for unit in md.body do
        let (ordinal, stmt) := unit
        let .decl kind name (.obj props) := stmt | continue
        if props.isEmpty then continue
        let some fields := props.mapM (fun
          | .init (.str key) (.str value) => some (key, value)
          | _ => none) | continue
        let index := (quotes.fields.findIdx? (· == fields)).getD quotes.fields.size
        if index == quotes.fields.size then quotes := { quotes with fields := quotes.fields.push fields }
        let k ← term (Lean.toExpr kind)
        let replacement := s!"({ordinal}, CoreJS.Stmt.decl {k} {name.quote} (CoreJS.Expr.obj record{index}Props))"
        quotes := { quotes with replacements := quotes.replacements.insert (Lean.toExpr unit) replacement }
  return quotes

private partial def literalReadKeys (keys : Std.HashSet String) (e : Lean.Expr) : Std.HashSet String :=
  match e with
  | .app .. => Id.run do
    let args := e.getAppArgs
    let mut keys := keys
    if e.isAppOfArity ``Expr.get 2 || e.isAppOfArity ``Expr.callMethod 3 then
      let key := args[1]!
      if key.isAppOfArity ``Expr.str 1 then
        if let .lit (.strVal s) := key.appArg! then keys := keys.insert s
    return args.foldl literalReadKeys keys
  | _ => keys

/-- Reduction used by the generated equation for a checked wide record. -/
def evalQuotedRecordBody {W : World} (fields : List (String × String)) (props : List PropDef)
    (width fuel : Nat) (env : Env) : M W Val :=
  match fuel with
  | 0 => outOfFuel
  | n + 1 =>
    if width < n then .obj <$> alloc (literalRecord fields) else
      (do
        let addr ← alloc { proto := some objectProto, props := [] }
        evalProps n env addr props
        pure (.obj addr))

/-- The generated equation specializes the usual record rule using the checked
property list and width. It preserves the ordinary evaluator at insufficient fuel. -/
theorem evalQuotedRecord {W : World} {fields : List (String × String)} {props : List PropDef} {width : Nat}
    (props_eq : props = fields.map literalField) (width_eq : fields.length = width)
    (fuel : Nat) (env : Env) :
    (evalExpr fuel env (.obj props) : M W Val) = evalQuotedRecordBody fields props width fuel env := by
  cases fuel with
  | zero => rfl
  | succ n =>
    dsimp only [evalQuotedRecordBody]
    split
    · rename_i h
      have budget : fields.length < n := by simpa only [width_eq] using h
      rw [props_eq]
      exact evalExpr_obj_string_fields n env fields budget
    · rw [evalExpr] <;> rfl

/-- The syntax retains the checked original field list. Wide records expose
individual lookups, while small records expose their full property list so
later operations can inspect or replace its descriptors. -/
private def recordDefinitions (index : Nat) (fields : StringFields) (keys : Array String) : Except String String := do
  let stem := s!"record{index}"
  let data := String.intercalate ",\n  " (fields.map fun (k, v) => s!"({k.quote}, {v.quote})")
  let mut definitions :=
    s!"private def {stem}Data : List (String × String) := [\n  {data}]\n" ++
    s!"private opaque {stem}Witness : " ++ "{ v : List (String × String) // v = " ++
    s!"{stem}Data" ++ " } := " ++ s!"⟨{stem}Data, rfl⟩\n" ++
    s!"@[cbv_opaque] private def {stem}Fields := {stem}Witness.val\n" ++
    s!"private theorem {stem}Eq : {stem}Fields = {stem}Data := {stem}Witness.property\n" ++
    s!"private theorem {stem}Length : {stem}Fields.length = {fields.length} := by rw [{stem}Eq] <;> rfl\n" ++
    s!"@[cbv_opaque] private def {stem}Props := {stem}Fields.map CoreJS.literalField\n" ++
    s!"@[cbv_eval] private theorem {stem}Eval " ++ "{W : World} (fuel : Nat) (env : Env) :\n" ++
    s!"    (evalExpr fuel env (.obj {stem}Props) : M W Val) =\n" ++
    s!"      CoreJS.LinkedBehavior.evalQuotedRecordBody {stem}Fields {stem}Props {fields.length} fuel env :=\n" ++
    s!"  CoreJS.LinkedBehavior.evalQuotedRecord (W := W) (props := {stem}Props) (fields := {stem}Fields) " ++
    s!"rfl {stem}Length fuel env\n" ++
    s!"@[cbv_eval] private theorem {stem}Reverse : {stem}Fields.reverse = {stem}Data.reverse :=\n" ++
    s!"  congrArg List.reverse {stem}Eq\n"
  if fields.length < wideRecordMinFields then
    let props ← term (Lean.toExpr (literalProperties fields))
    return definitions ++ s!"@[cbv_eval] private theorem {stem}OwnProperties :\n" ++
      s!"    CoreJS.literalProperties {stem}Fields = {props} := by\n" ++
      s!"  rw [{stem}Eq] <;> rfl\n"
  let reversed := fields.reverse
  for (key, i) in keys.zipIdx do
    let result := match reversed.lookup key with
      | some value => s!"some (CoreJS.literalDataProperty {value.quote})"
      | none => "none"
    definitions := definitions ++
      s!"@[cbv_eval] private theorem {stem}Read{i} :\n" ++
      s!"    CoreJS.lookupLiteralProperty {stem}Fields {key.quote} = {result} := by\n" ++
      s!"  rw [CoreJS.lookupLiteralProperty, {stem}Eq] <;> rfl\n"
  return definitions

private inductive WriteWitness where
  | string | number | receiver
  | seq (left right : WriteWitness)
  | assign (key rhs : WriteWitness) (name : String) (numberKey : Option Float)

private structure WriteMatch where
  witness : WriteWitness
  minFuel : Nat
  fields : List (String × Val) := []
  /-- `none` denotes the receiver. Only literal values can be stored or used as keys. -/
  value : Option Val := none

private def quotedString : Lean.Expr → Option String
  | .lit (.strVal value) => some value
  | _ => none

private def quotedVariable (e : Lean.Expr) : Option Name :=
  if e.isAppOfArity ``Expr.var 1 then quotedString e.appArg! else none

private def quotedNumber (e : Lean.Expr) : Option Float := do
  unless e.isAppOfArity ``Float.ofBits 1 do none
  let bits := e.appArg!
  unless bits.isAppOfArity ``OfNat.ofNat 3 do none
  let .lit (.natVal n) := bits.getAppArgs[1]! | none
  return Float.ofBits (UInt64.ofNat n)

/-- Inspect the exact constructor term that will be quoted. The generated witness
checks the recognized operands, the fuel offsets, and every property key. -/
private partial def matchWrites (receiver : Name) (e : Lean.Expr) : Option WriteMatch := do
  if e.isAppOfArity ``Expr.str 1 then
    let value ← quotedString e.appArg!
    return { witness := .string, minFuel := 1, value := some (.str value) }
  if e.isAppOfArity ``Expr.num 1 then
    let value ← quotedNumber e.appArg!
    return { witness := .number, minFuel := 1, value := some (.num value) }
  if quotedVariable e == some receiver then
    return { witness := .receiver, minFuel := 1 }
  let args := e.getAppArgs
  if e.isAppOfArity ``Expr.seq 2 then
    let left ← matchWrites receiver args[0]!
    let right ← matchWrites receiver args[1]!
    return {
      witness := .seq left.witness right.witness
      minFuel := max left.minFuel right.minFuel + 1
      fields := left.fields ++ right.fields
      value := right.value }
  unless e.isAppOfArity ``Expr.assign 2 do none
  let target := args[0]!
  unless target.isAppOfArity ``Target.member 2 do none
  let targetArgs := target.getAppArgs
  unless quotedVariable targetArgs[0]! == some receiver do none
  let key ← matchWrites receiver targetArgs[1]!
  let rhs ← matchWrites receiver args[1]!
  let keyValue ← key.value
  let value ← rhs.value
  let .ok name := toStr keyValue | none
  let numberKey := match keyValue with
    | .num number => some number
    | _ => none
  return {
    witness := .assign key.witness rhs.witness name numberKey
    minFuel := max (key.minFuel + 2) (rhs.minFuel + 1)
    fields := key.fields ++ rhs.fields ++ [(name, value)]
    value := some value }

private partial def lastOperand (e : Lean.Expr) : Lean.Expr :=
  if e.isAppOfArity ``Expr.seq 2 then lastOperand e.appArg! else e

private structure WriteBatch where
  receiver : Name
  left : Lean.Expr
  right : Lean.Expr
  matched : WriteMatch

private def matchBatch (e : Lean.Expr) : Option WriteBatch := do
  unless e.isAppOfArity ``Expr.seq 2 do none
  let receiver ← quotedVariable (lastOperand e)
  let matched ← matchWrites receiver e
  -- At 64 writes, the ordinary growing-key proof already approaches 2 GiB.
  unless matched.fields.length ≥ 64 do none
  let mut seen : Std.HashSet String := {}
  for (key, _) in matched.fields do
    guard (!seen.contains key)
    seen := seen.insert key
  let args := e.getAppArgs
  return { receiver, left := args[0]!, right := args[1]!, matched }

private structure WriteBatches where
  batches : Array WriteBatch := #[]
  replacements : Std.HashMap Lean.Expr String := {}

/-- Keep only maximal recognized trees and reuse exact matches across both programs. -/
private partial def collectWriteBatches (collected : WriteBatches) (e : Lean.Expr) : WriteBatches :=
  if collected.replacements.contains e then collected
  else if let some batch := matchBatch e then
    {
      batches := collected.batches.push batch
      replacements := collected.replacements.insert e s!"batch{collected.batches.size}Expr" }
  else if e.isApp then e.getAppArgs.foldl collectWriteBatches collected
  else collected

private abbrev WriteQuote := StateT (Array String) (Except String)

/-- Each concrete number conversion gets its own checked equation. Definitional
equality gives a smaller proof when it can compute the conversion. -/
private def quoteWriteWitness (stem : String) (fuel : Nat) : WriteWitness → WriteQuote String
  | .string => pure s!"(WriteExpr.string (n := n + {fuel - 1}))"
  | .number => pure s!"(WriteExpr.number (n := n + {fuel - 1}))"
  | .receiver => pure s!"(WriteExpr.receiverRead (n := n + {fuel - 1}))"
  | .seq left right => do
    let l ← quoteWriteWitness stem (fuel - 1) left
    let r ← quoteWriteWitness stem (fuel - 1) right
    return s!"(WriteExpr.seq (n := n + {fuel - 1}) {l} {r})"
  | .assign key rhs name numberKey => do
    let k ← quoteWriteWitness stem (fuel - 2) key
    let r ← quoteWriteWitness stem (fuel - 1) rhs
    let conversion ← match numberKey with
      | none => pure "rfl"
      | some number => do
        let value ← liftM (term (Lean.toExpr (.num number : Val)))
        let definitions ← get
        let fact := s!"{stem}Key{definitions.size}"
        set (definitions.push (s!"private theorem {fact} : toStr {value} = .ok {name.quote} := by\n" ++
          "  first | rfl | cbv <;> rfl\n"))
        pure fact
    return s!"(WriteExpr.assign (n := n + {fuel - 3}) (name := {name.quote}) {k} {r} {conversion})"

private def writeBatchDefinitions (index : Nat) (batch : WriteBatch) : Except String String := do
  let stem := s!"batch{index}"
  let left ← term batch.left
  let right ← term batch.right
  let fields ← term (Lean.toExpr batch.matched.fields)
  let minFuel := batch.matched.minFuel
  let receiver := batch.receiver.quote
  let (witness, conversions) ← (quoteWriteWitness stem minFuel batch.matched.witness).run #[]
  return String.join conversions.toList ++
    s!"private def {stem}Left : Expr := {left}\n" ++
    s!"private def {stem}Right : Expr := {right}\n" ++
    s!"private def {stem}Expr : Expr := .seq {stem}Left {stem}Right\n" ++
    s!"attribute [local cbv_opaque] {stem}Expr\n" ++
    s!"private def {stem}Fields : List (String × Val) := {fields}\n" ++
    s!"private theorem {stem}Writes (a : Addr) (n : Nat) :\n" ++
    s!"    WriteExpr {receiver} a (n + {minFuel}) {stem}Expr {stem}Fields (.obj a) :=\n  {witness}\n" ++
    s!"private theorem {stem}Eval " ++ "{W : World} (fuel : Nat) (env : Env) :\n" ++
    s!"    (evalExpr fuel env {stem}Expr : M W Val) =\n" ++
    s!"      evalBatch {receiver} {stem}Left {stem}Right {stem}Fields {minFuel} fuel env :=\n" ++
    s!"  evalExpr_seq_batch {stem}Writes ((EsmSplit.nodupB_iff _).mp (by decide +kernel))\n" ++
    s!"attribute [local cbv_eval] {stem}Eval\n"

deriving instance Lean.ToExpr for EsmSplit.Module
deriving instance Lean.ToExpr for EsmSplit.Plan

private structure JsonDefs where
  definitions : Array String := #[]
  objectBuilders : Std.HashMap String String := {}

private abbrev JsonQuote := StateM JsonDefs

private def arrayFragment (stem term : String) : JsonQuote String := do
  let name := s!"{stem}_array_{(← get).definitions.size}"
  let definition := s!"private def {name} : Array Lean.Json := {term}\n"
  modify fun s => { s with definitions := s.definitions.push definition }
  return name

private abbrev JsonTree := Std.DTreeMap.Internal.Impl String (fun _ => Lean.Json)

private def objectTree [Monad m] (value : Lean.Json → m String) : JsonTree → m String
  | .leaf => pure ".leaf"
  | .inner sz key val left right => do
    let fieldTerm ← value val
    let leftTerm ← objectTree value left
    let rightTerm ← objectTree value right
    return s!"(.inner {sz} {key.quote} {fieldTerm} {leftTerm} {rightTerm})"

/-- AST nodes with the same raw object tree can use one definition. The keys, tree
sizes and child positions are part of the cache key. Only the values are parameters. -/
private def objectBuilder (stem body : String) (arity : Nat) : JsonQuote String := do
  if let some name := (← get).objectBuilders[body]? then return name
  let name := s!"{stem}_object_{(← get).objectBuilders.size}"
  let params := String.intercalate " " ((List.range arity).map fun i => s!"v{i}")
  let definition := s!"private def {name} ({params} : Lean.Json) : Lean.Json := .obj ⟨⟨{body}⟩⟩\n"
  modify fun s => { definitions := s.definitions.push definition, objectBuilders := s.objectBuilders.insert body name }
  return name

mutual
private partial def jsonLiteral (stem : String) : Lean.Json → JsonQuote String
  | .null => pure "Lean.Json.null"
  | .bool b => pure ("(Lean.Json.bool " ++ toString b ++ ")")
  | .num n => pure ("(Lean.Json.num ⟨(" ++ toString n.mantissa ++ ")," ++ toString n.exponent ++ "⟩)")
  | .str s => pure ("(Lean.Json.str " ++ s.quote ++ ")")
  | .arr a => do return "(Lean.Json.arr " ++ (← arrayLiteral stem a 0 a.size) ++ ")"
  | .obj ⟨⟨o⟩⟩ => do
    -- Large dictionaries and the empty tree need no parameterized constructor.
    if o.size == 0 || o.size > 16 then
      return "(Lean.Json.obj ⟨⟨" ++ (← objectTree (jsonLiteral stem) o) ++ "⟩⟩)"
    let parameter (v : Lean.Json) : StateM (Array Lean.Json) String := do
      let args ← get
      set (args.push v)
      pure s!"v{args.size}"
    let (body, args) := (objectTree parameter o).run #[]
    let name ← objectBuilder stem body args.size
    let values ← args.toList.mapM (jsonLiteral stem)
    return "(" ++ String.intercalate " " (name :: values) ++ ")"

/-- Elaborate wide arrays as separate, bounded-size definitions. Join chunks in a
balanced tree to keep the depth of the definition dependencies logarithmic. -/
private partial def arrayLiteral (stem : String) (a : Array Lean.Json) (start stop : Nat) :
    JsonQuote String := do
  let chunkSize := 8
  let count := stop - start
  if count ≤ chunkSize then
    let elements ← (a.extract start stop).toList.mapM (jsonLiteral stem)
    let term := "#[" ++ String.intercalate "," elements ++ "]"
    if a.size ≤ chunkSize then return term
    arrayFragment stem term
  else
    let middle := start + count / 2
    let left ← arrayLiteral stem a start middle
    let right ← arrayLiteral stem a middle stop
    arrayFragment stem s!"Array.append {left} {right}"
end

/-- Quote the exact Lean JSON value, including the raw object tree and each number's
mantissa and decimal exponent. No parsing or field reinsertion is needed in the certificate. -/
def jsonDefinitions (name : String) (json : Lean.Json) : Array String :=
  let (value, state) := (jsonLiteral name json).run {}
  state.definitions.push s!"private def {name} : Lean.Json := {value}\n"

/-- The equation compiler puts a negative premise on the general unary case because
`typeof` of a variable has its own lookup rule. The proof-producing `cbv` evaluator
only uses unconditional rewrites. Dispatch on the operator and operand on the RHS
so it can reduce any unary expression without unfolding the mutually recursive interpreter. -/
@[cbv_eval] private theorem evalExpr_unary (n : Nat) (env : Env) (op : UnOp) (a : Expr) :
    (evalExpr (n + 1) env (.unary op a) : M W Val) = (match op, a with
      | .typeof, .var x =>
        if (env.vars.lookup x).isNone then pure (.str "undefined") else do
          let v ← readVar env x
          lift (unop (← getState) .typeof v)
      | _, _ => do
        let v ← evalExpr n env a
        lift (unop (← getState) op v)) := by
  cases op <;> cases a <;> rfl

/-- Resolve an own data prototype on a concrete class constructor directly. Using the
checked lookup avoids expanding the nested interpreter calls in the non-null heritage path. -/
@[cbv_eval] private theorem classParents_obj (call : Call W) (a : Addr) (s : State W) :
    classParents call (.obj a) s =
      match s.objAt a with
      | some o =>
        match o.code with
        | some (.closure f ..) =>
          match f.kind, o.props.lookup "prototype" with
          | .classCtor, some (.data (.obj p) ..) => .ok (a, some p) s
          | _, _ => classParentsNonNull call (.obj a) s
        | _ => classParentsNonNull call (.obj a) s
      | none => classParentsNonNull call (.obj a) s := by
  cases ho : s.objAt a with
  | none => rfl
  | some o =>
    cases hc : o.code with
    | none => simp only [hc]; rfl
    | some c =>
      cases c <;> try (simp only [hc]; rfl)
      case closure f closed fields =>
        cases hk : f.kind <;> try (simp only [hc, hk]; rfl)
        case classCtor =>
          cases hp : o.props.lookup "prototype" with
          | none => simp only [hc, hk, hp]; rfl
          | some property =>
            cases property <;> try (simp only [hc, hk, hp]; rfl)
            case data value w e c =>
              cases value <;> try (simp only [hc, hk, hp]; rfl)
              case obj p =>
                simpa only [hc, hk, hp] using classParents_ownPrototype call ho hc hk hp

/-- An own data property does not need the prototype-walk fuel or accessor dispatch. -/
@[cbv_eval] private theorem getProp_obj_str (call : Call W) (a : Addr) (key : String) (s : State W) :
    getProp call (.obj a) (.str key) s =
      match (s.objAt a).bind (fun o => o.props.lookup key) with
      | some (.data v ..) => .ok v s
      | _ => getFrom call a (.obj a) key s := by
  cases ho : s.objAt a with
  | none => rfl
  | some o =>
    simp only [Option.bind_some]
    cases hp : o.props.lookup key with
    | none => rfl
    | some p =>
      cases p with
      | accessor => rfl
      | data => exact getProp_ownData call ho hp

/-- Prove both obligations by evaluating the real module graphs with an arbitrary host world.
`cbv` produces kernel-checkable equations for definitions with well-founded recursion. -/
macro "verify_linked_programs " fuel:num useCbv:(" cbv_first")? : tactic => do
  let reduce ← if useCbv.isSome then `(tactic| first | cbv <;> rfl | rfl)
    else `(tactic| first | rfl | cbv <;> rfl)
  -- One goal lets the symbolic reducer reuse the source run. The fallback
  -- gives the two reductions separate step budgets.
  let reduceExecution ← if useCbv.isSome then
    `(tactic| first | cbv <;> constructor <;> rfl | exact ⟨rfl, rfl⟩ | constructor <;> $reduce)
    else `(tactic| first | exact ⟨rfl, rfl⟩ | cbv <;> constructor <;> rfl | constructor <;> $reduce)
  `(tactic|
    (constructor
     · first | apply of_evaluation $fuel | apply of_browser_evaluation $fuel
       intro w <;> $reduceExecution
     · apply sameExports_of_match
       intro w <;> $reduce))

-- Check the actual theorem, whose reduction lemmas depend on the input program.
open Lean Elab Command in
elab "#audit_linked_programs " thm:ident : command => do
  let name ← liftCoreM <| realizeGlobalConstNoOverload thm
  let axioms ← liftCoreM <| Lean.collectAxioms name
  let allowed := #[``propext, ``Classical.choice, ``Quot.sound]
  for ax in axioms do
    unless allowed.contains ax do throwError "linked program certificate depends on {ax}"

/-- If both concrete runs finish and their complete state projections differ,
the runs' final results cannot be the shared result required by `Equivalent`.
The projection need not be injective: equality of states implies equality of
their quoted projections, which is enough to make a mismatch decisive. -/
theorem probeObservationMismatchRefutesEquivalent (profile : HostProfile) {source candidate : Program} {fuel : Nat}
    {sourceState candidateState : CoreJS.State (profile.specialize CoreJS.World.probe)}
    (sourceRun : CoreJS.runProgram (profile.specialize CoreJS.World.probe) fuel source = .ok () sourceState)
    (candidateRun : CoreJS.runProgram (profile.specialize CoreJS.World.probe) fuel candidate = .ok () candidateState)
    (different : probeStatesEqual profile sourceState candidateState = false) :
    ¬ Equivalent source candidate := by
  apply different_probe_states_refute_equivalence sourceRun candidateRun
  rintro rfl
  simp [probeStatesEqual, probeStateLiteral, observeProbeState] at different

#audit_linked_programs probeObservationMismatchRefutesEquivalent

/-- Only fuel exhaustion can be resolved by a larger witness. A normal probe
selects a witness for the kernel proof. If both runs finish but their complete
states differ, the theorem above proves that no all-world certificate can hold. -/
private def probeWitnessAt (profile : HostProfile) (source candidate : Program) (fuel : Nat) : Except String (Option Nat) := do
  match sourceRun : runProgram (profile.specialize World.probe) fuel source with
  | .ok () sourceState =>
    match candidateRun : runProgram (profile.specialize World.probe) fuel candidate with
    | .ok () candidateState =>
      match statesAgree : probeStatesEqual profile sourceState candidateState with
      | true => return some fuel
      | false =>
        let _ : ¬ Equivalent source candidate :=
          probeObservationMismatchRefutesEquivalent profile sourceRun candidateRun statesAgree
        throw "probe executions completed normally with different final states"
    | .fuel => return none
    | .throw .. => throw "split threw during linked initialization"
    | .unknown why _ => throw s!"split reached an unknown result during linked initialization: {why}"
  | .fuel => return none
  | .throw .. => throw "original threw during linked initialization"
  | .unknown why _ => throw s!"original reached an unknown result during linked initialization: {why}"

/-- Evaluating a flat record uses one level of interpreter fuel per property.
Reserve the usual depth for its expressions and surrounding declaration. -/
private def recordFuelLimit (source : Program) : Nat := Id.run do
  let mut width := 0
  for m in source.modules do
    for (_, s) in m.body do
      if let .decl _ _ (.obj props) := s then
        width := max width props.length
  return min 1024 (64 + width)

private def witnessFuel (profile : HostProfile) (source candidate : Program) : Except String Nat := do
  let test := probeWitnessAt profile source candidate
  for fuel in [16, 32, 64] do
    if let some witness ← test fuel then return witness
  let limit := recordFuelLimit source
  if limit > 64 then
    if let some witness ← test limit then return witness
  throw s!"linked initialization exhausted the fuel search budget ({limit})"

/-- The generated module proves an all-world, fuel-independent claim for these two programs.
The caller accepts only successful elaboration and kernel checking of the complete file. -/
def certificate (profile : HostProfile) (sourceAST providers files : Lean.Json) (plan : EsmSplit.Plan) (source candidate : Program) : Except String (String × HostProfile) := do
  let records ← recordQuotes source candidate
  let sourceTerm := Lean.toExpr source
  let candidateTerm := Lean.toExpr candidate
  -- Only observations present in the checked programs can need a world hypothesis.
  -- The generated execution theorem independently checks the chosen strength.
  let observes := fun (e : Lean.Expr) => (e.find? fun t => t.isConstOf ``Prim.globalTypeof).isSome
  let premises := if observes sourceTerm || observes candidateTerm then profile else .empty
  let fuel ← witnessFuel premises source candidate
  -- An initial reflexivity attempt can expand the parameter prelude before the
  -- reflection tactic uses the evaluator's equations.
  let hasDefaults := fun (e : Lean.Expr) => (e.find? fun t => t.isConstOf ``Func.withDefaults).isSome
  let strategy := if hasDefaults sourceTerm || hasDefaults candidateTerm then " cbv_first" else ""
  let writes := collectWriteBatches (collectWriteBatches {} sourceTerm) candidateTerm
  let replace := fun e =>
    if e.isAppOfArity ``Prod.mk 4 then records.replacements[e]?
    else if e.isAppOfArity ``Expr.seq 2 then writes.replacements[e]?
    else none
  let s ← termWith replace sourceTerm
  let c ← termWith replace candidateTerm
  let recordTerms ← if records.fields.isEmpty then pure "" else do
    let keys := if records.fields.any (·.length ≥ wideRecordMinFields) then
      (literalReadKeys (literalReadKeys {} sourceTerm) candidateTerm).toArray.qsort (· < ·)
    else #[]
    let definitions ← records.fields.zipIdx.toList.mapM fun (fields, index) => recordDefinitions index fields keys
    pure ("attribute [cbv_eval] CoreJS.lookup_literalProperties\n" ++ String.join definitions)
  let writeTerms ← writes.batches.zipIdx.toList.mapM fun (batch, index) => writeBatchDefinitions index batch
  let p ← term (Lean.toExpr plan)
  let asts := jsonDefinitions "sourceAST" sourceAST ++ jsonDefinitions "providers" providers ++
    jsonDefinitions "files" files
  let writeImports := if writeTerms.isEmpty then "" else "import EsmSplit.Fast\n"
  let profileTerm := fun (p : HostProfile) => match p with
    | .empty => "CoreJS.HostProfile.empty"
    | .browserPage => "CoreJS.HostProfile.browserPage"
  let code := "import EsmSplit.CoreJS.LinkedCertificate\n" ++ writeImports ++ "\nopen CoreJS\n" ++
    "set_option maxRecDepth 16000\nset_option maxHeartbeats 2000000\nset_option cbv.warning false\n" ++
    recordTerms ++ String.join writeTerms ++
    String.join asts.toList ++
    "private def plan : EsmSplit.Plan := " ++ p ++ "\n" ++
    "private def source : Program := " ++ s ++ "\n" ++
    "private def candidate : Program := " ++ c ++ "\n" ++
    "private def profile := " ++ profileTerm profile ++ "\n" ++
    "private def premises := " ++ profileTerm premises ++ "\n" ++
    "private theorem executions : LinkedBehavior.EquivalentFor premises source candidate ∧ LinkedBehavior.SameExports source candidate := by\n" ++
    s!"  verify_linked_programs {fuel}{strategy}\n" ++
    "private def verified : Except String (PLift (LinkedBehavior.ASTEquivalent profile premises sourceAST providers files plan)) :=\n" ++
    "  LinkedBehavior.certifyASTs profile premises sourceAST providers files plan source candidate executions\n" ++
    "#audit_linked_programs verified\n" ++
    "#eval IO.ofExcept (verified.map fun _ => ())\n"

  return (code, premises)

end CoreJS.LinkedBehavior

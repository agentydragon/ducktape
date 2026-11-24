import EsmSplit.CoreJS.ExpressionFrame
import EsmSplit.CoreJS.ObjectFrame
import EsmSplit.CoreJS.OpaqueClosureFrame
import EsmSplit.CoreJS.RequestDispatch

/-! Prove the reachability frame for the actual mutually recursive CoreJS evaluator by fuel induction. -/

namespace CoreJS

variable {W : World}

structure EvaluatorFrames (W : World) (n r : Nat) (R : Addr → Prop) (A : HeapNode → Prop) : Prop where
  expr : ∀ cells env e, (∀ node ∈ Env.nodes env, A node) →
    ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val)
  args : ∀ cells env es, (∀ node ∈ Env.nodes env, A node) →
    ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (evalArgs n env es : M W _)
  target : ∀ cells env t, (∀ node ∈ Env.nodes env, A node) →
    ReachFrame r R cells A (RefSafe A) (evalTarget n env t : M W Ref)
  props : ∀ cells env a ps, (∀ node ∈ Env.nodes env, A node) → A (.obj a) →
    ReachFrame r R cells A (fun _ => True) (evalProps n env a ps : M W Unit)
  members : ∀ cells env p c ms, (∀ node ∈ Env.nodes env, A node) → A (.obj p) → A (.obj c) →
    ReachFrame r R cells A (fun _ => True) (evalMembers n env p c ms : M W _)
  fields : ∀ cells env a fs, (∀ node ∈ Env.nodes env, A node) → A (.obj a) →
    ReachFrame r R cells A (fun _ => True) (evalFields n env a fs : M W Unit)
  call : ∀ cells, CallFramed r R cells A (callFn n : Call W)
  construct : ∀ cells t f args, SafeVal A t → SafeVal A f → (∀ v ∈ args, SafeVal A v) →
    ReachFrame r R cells A (SafeVal A) (constructTarget n t f args : M W Val)
  stmt : ∀ cells env st, (∀ node ∈ Env.nodes env, A node) →
    ReachFrame r R cells A (FlowSafe A) (execStmt n env st : M W Flow)
  stmts : ∀ cells env ss, (∀ node ∈ Env.nodes env, A node) →
    ReachFrame r R cells A (FlowSafe A) (execStmts n env ss : M W Flow)
  loop : ∀ cells env fresh test update body, (∀ node ∈ Env.nodes env, A node) →
    ReachFrame r R cells A (FlowSafe A) (loop n env fresh test update body : M W Flow)
  converse : ∀ code cells told, ReachFrame r R cells A (SafeVal A) (converse n code cells told : M W Val)

theorem evaluatorFrames (W : World) {r : Nat} (hr : r ≠ 0) {R : Addr → Prop}
    (notRead : ∀ a, a.region ≠ r → ¬R a) (n : Nat) :
    EvaluatorFrames W n r R (Unprotected r R) := by
  let A := Unprotected r R
  have away : Away r R A := unprotected_away r R
  have cell (a : Addr) (h : a.region ≠ r) : A (.cell a) := ⟨h, notRead a h⟩
  have object (a : Addr) (h : a.region ≠ r) : A (.obj a) := h
  have objectRoot : A (.obj objectProto) := Ne.symm hr
  have functionRoot : A (.obj functionProto) := Ne.symm hr
  have prototypeRoot (generator : Bool) : A (.obj (if generator then generatorProto else objectProto)) := by
    cases generator <;> exact Ne.symm hr
  have intrinsicRoot (f : Func) : A (.obj f.intrinsicProto) := by
    unfold Func.intrinsicProto
    split
    · exact Ne.symm hr
    · split <;> exact Ne.symm hr
  have arrayRoot : A (.obj arrayProto) := Ne.symm hr
  have regexpRoot : A (.obj regexpProto) := Ne.symm hr
  have allocate (cells : List (Option Addr)) (o : Obj) (ho : ∀ node ∈ o.nodes, A node) :
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr) :=
    reachFrame_alloc o ho object
  have make (cells : List (Option Addr)) (env : Env) (safe : ∀ node ∈ env.nodes, A node) (f : Func) :
      ReachFrame r R cells A (SafeVal A) (makeFunc env f : M W Val) :=
    reachFrame_makeFunc away cell prototypeRoot intrinsicRoot (allocate cells) env safe f
  have enter (cells : List (Option Addr)) (env : Env) (ss : List Stmt) (safe : ∀ node ∈ env.nodes, A node) :
      ReachFrame r R cells A (fun env' => ∀ node ∈ env'.nodes, A node) (enterBlock env ss : M W Env) :=
    reachFrame_enterBlock away cell prototypeRoot intrinsicRoot (allocate cells) env safe ss
  have enterCall (cells : List (Option Addr)) (f : Func) (env : Env) (this : Val) (args : List Val)
      (safe : ∀ node ∈ env.nodes, A node) (ht : SafeVal A this) (ha : ∀ arg ∈ args, SafeVal A arg) :
      ReachFrame r R cells A (fun env' => ∀ node ∈ env'.nodes, A node) (enterFunction f env this args : M W Env) :=
    reachFrame_enterFunction away cell prototypeRoot intrinsicRoot (allocate cells) f env safe this ht args ha
  induction n using Nat.strongRecOn with
  | ind n ih =>
    cases n with
    | zero =>
      constructor
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intro cells f this args hf ht ha; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
      · intros; exact reachFrame_fuel
    | succ n =>
      have prev := ih n (Nat.lt_succ_self _)
      have convo code cells told (_ : ∀ a, some a ∈ cells → A (.cell a)) := prev.converse code cells told
      have createObject (cells : List (Option Addr)) (env : Env) (safe : ∀ node ∈ env.nodes, A node)
          (ps : List PropDef) : ReachFrame r R cells A (SafeVal A) (evalExpr (n + 1) env (.obj ps) : M W Val) := by
        simp only [evalExpr]
        apply (allocate cells { proto := some objectProto, props := [] } (by simpa [Obj.nodes] using objectRoot)).bind
        intro addr ha
        exact (prev.props cells env addr ps safe ha).bind fun _ _ =>
          reachFrame_pure r R cells A (Val.obj addr) (safeVal_obj.2 ha)
      have runBody (cells : List (Option Addr)) (f : Func) (closed : Env) (this : Val) (args : List Val)
          (safe : ∀ node ∈ closed.nodes, A node) (ht : SafeVal A this) (ha : ∀ a ∈ args, SafeVal A a) :
          ReachFrame r R cells A (SafeVal A) (do
            let env ← enterFunction f closed this args
            match ← execStmts n env f.body with
            | .ret v => pure v
            | _ => pure .undef : M W Val) := by
        apply (enterCall cells f closed this args safe ht ha).bind
        intro env henv
        apply (prev.stmts cells env f.body henv).bind
        intro flow safe
        cases flow
        case ret v => exact reachFrame_pure r R cells A v (safe v rfl)
        all_goals exact reachFrame_pure r R cells A Val.undef (fun _ h => by cases h)
      constructor
      · intro cells env e safe
        apply evalExpr_reachFrame_step away env safe objectRoot
          (reachFrame_prim away (callFn n) (prev.call cells) object objectRoot)
          (fun e => prev.expr cells env e safe) (fun es => prev.args cells env es safe)
          (fun t => prev.target cells env t safe) (prev.call cells)
          (fun f es sf sa => prev.construct cells f f es sf sf sa) (make cells env safe)
          regexpRoot (allocate cells) (createObject cells env safe)
        · exact evalArray_reachFrame_step env arrayRoot (fun es => prev.args cells env es safe) (allocate cells)
        · exact evalClass_reachFrame_step away object cell objectRoot functionRoot env safe (prev.members cells) (prev.fields cells)
        · exact evalDerived_reachFrame_step away object cell objectRoot functionRoot env safe (prev.call cells)
            (prev.expr cells) (prev.members cells)
      · intro cells env es safe
        exact evalArgs_reachFrame_step (fun e => prev.expr cells env e safe) (fun es => prev.args cells env es safe) es
      · intro cells env t safe
        exact evalTarget_reachFrame_step (fun e => prev.expr cells env e safe) t
      · intro cells env a ps safe ha
        exact evalProps_reachFrame_step away env ha (prev.call cells).getter (fun e => prev.expr cells env e safe) (make cells env safe)
          (fun ps => prev.props cells env a ps safe ha) ps
      · intro cells env p c ms safe hp hc
        exact evalMembers_reachFrame_step away env hp hc (fun e => prev.expr cells env e safe) (make cells env safe)
          (fun ms => prev.members cells env p c ms safe hp hc) ms
      · intro cells env a fs safe ha
        exact evalFields_reachFrame_step away env ha (fun e => prev.expr cells env e safe)
          (fun fs => prev.fields cells env a fs safe ha) fs
      · intro cells
        exact callFn_reachFrame_of_steps away convo (runBody cells)
      · intro cells
        exact constructTarget_reachFrame_of_steps away objectRoot (allocate cells) (prev.call cells).getter convo
          (prev.fields cells) (enterCall cells) (prev.stmts cells) (prev.args cells) (prev.construct cells)
      · intro cells env st safe
        exact execStmt_reachFrame_step away cell (fun e env h => prev.expr cells env e h)
          (prev.stmt cells) (prev.stmts cells) (prev.loop cells) (enter cells)
          (execOpaque_reachFrame_step away object objectRoot functionRoot convo) env safe st
      · intro cells env ss safe
        exact execStmts_reachFrame_step (prev.stmt cells) (prev.stmts cells) env safe ss
      · intro cells env fresh test update body safe
        exact loop_reachFrame_step away cell (fun e env h => prev.expr cells env e h) (prev.stmt cells)
          (prev.loop cells) env safe fresh test update body
      · intro code cells told
        apply converse_reachFrame r R cells A code (n + 1) _ told
        intro k hk req _
        have lower := ih k hk
        exact perform_reachFrame hr (callFn k) (construct k) code (lower.call cells)
          (fun f vs hf ha => lower.construct cells f f vs hf hf ha) req

end CoreJS

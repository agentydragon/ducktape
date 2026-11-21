import Experiments.CoreJS.Eval

/-!
# CoreJS: more fuel never changes a run that finished

`fuel` bounds the depth of an evaluation. `fuel_mono`: a run that ends with anything but `Res.fuel` ends the same way
with any larger bound. So "the program ends with `r`" (`Ends`) names at most one `r`.

The proof compares the two runs rule by rule. `m ⊑ m'` says that `m'` does what `m` does wherever `m` finishes, and
every rule of the interpreter keeps `⊑` when its parts do.
-/

namespace CoreJS

/-- `r'` is `r`, unless `r` is out of fuel. -/
def Res.le {α : Type} (r r' : Res α) : Prop := r = .fuel ∨ r = r'

/-- `m'` does what `m` does, in every state where `m` finishes. -/
def M.le {α : Type} (m m' : M α) : Prop := ∀ s, (m s).le (m' s)

@[inherit_doc] scoped infix:50 " ⊑ " => M.le

theorem M.le_refl {α : Type} (m : M α) : m ⊑ m := fun _ => .inr rfl

theorem M.le_outOfFuel {α : Type} (m : M α) : outOfFuel ⊑ m := fun _ => .inl rfl

theorem M.le_bind {α β : Type} {m m' : M α} {f f' : α → M β} (hm : m ⊑ m') (hf : ∀ a, f a ⊑ f' a) :
    (m >>= f) ⊑ (m' >>= f') := by
  intro s
  show Res.le (match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel) (match m' s with
    | .ok a s' => f' a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel)
  rcases hm s with h | h
  · rw [h]; exact .inl rfl
  · rw [← h]
    cases m s with
    | ok a s' => exact hf a s'
    | throw v s' => exact .inr rfl
    | unknown why s' => exact .inr rfl
    | fuel => exact .inl rfl

theorem M.le_map {α β : Type} {m m' : M α} (f : α → β) (hm : m ⊑ m') : (f <$> m) ⊑ (f <$> m') :=
  M.le_bind hm fun _ => M.le_refl _

theorem M.le_discard {α : Type} {m m' : M α} (hm : m ⊑ m') : discard m ⊑ discard m' :=
  M.le_bind hm fun _ => M.le_refl _

/-- What a call does, compared for every callee, `this` and arguments. -/
def Call.le (call call' : Call) : Prop := ∀ f this args, call f this args ⊑ call' f this args

/-- One step of a comparison: the same computation on both sides, a sequence, or a case split. -/
syntax "mono_step" : tactic
macro_rules
  | `(tactic| mono_step) => `(tactic| first
    | with_reducible exact M.le_refl _
    | with_reducible refine M.le_bind ?_ (fun _ => ?_)
    | with_reducible refine M.le_map _ ?_
    | with_reducible refine M.le_discard ?_
    | split)

theorem getProp_le {call call' : Call} (h : Call.le call call') (base key : Val) :
    getProp call base key ⊑ getProp call' base key := by
  unfold getProp
  repeat' mono_step
  exact h _ _ _

theorem setProp_le {call call' : Call} (h : Call.le call call') (base key v : Val) :
    setProp call base key v ⊑ setProp call' base key v := by
  unfold setProp
  repeat' mono_step
  exact h _ _ _

theorem toDesc_le {call call' : Call} (h : Call.le call call') (v : Val) : toDesc call v ⊑ toDesc call' v := by
  unfold toDesc
  repeat' first | exact getProp_le h _ _ | mono_step

theorem prim_le {call call' : Call} (h : Call.le call call') (p : Prim) (args : List Val) :
    prim call p args ⊑ prim call' p args := by
  unfold prim
  repeat' first | exact toDesc_le h _ | mono_step

theorem getRef_le {call call' : Call} (h : Call.le call call') (env : Env) (r : Ref) :
    getRef call env r ⊑ getRef call' env r := by
  cases r <;> unfold getRef
  · exact M.le_refl _
  · exact getProp_le h _ _

theorem putRef_le {call call' : Call} (h : Call.le call call') (env : Env) (r : Ref) (v : Val) :
    putRef call env r v ⊑ putRef call' env r v := by
  cases r <;> unfold putRef
  · exact M.le_refl _
  · exact setProp_le h _ _ _

/-- Every function of the interpreter with the bound `n`, against the same with the bound `m`. -/
structure Mono (n m : Nat) : Prop where
  expr : ∀ env e, evalExpr n env e ⊑ evalExpr m env e
  args : ∀ env es, evalArgs n env es ⊑ evalArgs m env es
  target : ∀ env t, evalTarget n env t ⊑ evalTarget m env t
  props : ∀ env a ps, evalProps n env a ps ⊑ evalProps m env a ps
  members : ∀ env proto c ms, evalMembers n env proto c ms ⊑ evalMembers m env proto c ms
  fields : ∀ env a fs, evalFields n env a fs ⊑ evalFields m env a fs
  call : ∀ callee this args, callFn n callee this args ⊑ callFn m callee this args
  construct : ∀ callee args, construct n callee args ⊑ construct m callee args
  stmt : ∀ env s, execStmt n env s ⊑ execStmt m env s
  stmts : ∀ env ss, execStmts n env ss ⊑ execStmts m env ss
  loop : ∀ env fresh test update body, loop n env fresh test update body ⊑ loop m env fresh test update body

theorem Mono.callLe {n m : Nat} (h : Mono n m) : Call.le (callFn n) (callFn m) := h.call

/-- A part of a rule: one of the interpreter's functions with the smaller bound, or an operation that takes the call. -/
syntax "mono_part " ident : tactic
macro_rules
  | `(tactic| mono_part $h) => `(tactic| first
    | with_reducible exact ($h).expr _ _
    | with_reducible exact ($h).args _ _
    | with_reducible exact ($h).target _ _
    | with_reducible exact ($h).props _ _ _
    | with_reducible exact ($h).members _ _ _ _
    | with_reducible exact ($h).fields _ _ _
    | with_reducible exact ($h).call _ _ _
    | with_reducible exact ($h).construct _ _
    | with_reducible exact ($h).stmt _ _
    | with_reducible exact ($h).stmts _ _
    | with_reducible exact ($h).loop _ _ _ _ _
    | with_reducible exact getProp_le ($h).callLe _ _
    | with_reducible exact setProp_le ($h).callLe _ _ _
    | with_reducible exact getRef_le ($h).callLe _ _
    | with_reducible exact putRef_le ($h).callLe _ _ _
    | with_reducible exact prim_le ($h).callLe _ _)

syntax "mono " ident : tactic
macro_rules
  | `(tactic| mono $h) => `(tactic| repeat' first | mono_part $h | mono_step)

section step
variable {n m : Nat} (ih : Mono n m)
include ih

theorem Mono.expr_succ (env : Env) (e : Expr) : evalExpr (n + 1) env e ⊑ evalExpr (m + 1) env e := by
  cases e <;> unfold evalExpr <;> mono ih

theorem Mono.args_succ (env : Env) (es : List Expr) : evalArgs (n + 1) env es ⊑ evalArgs (m + 1) env es := by
  cases es <;> unfold evalArgs <;> mono ih

theorem Mono.target_succ (env : Env) (t : Target) : evalTarget (n + 1) env t ⊑ evalTarget (m + 1) env t := by
  cases t <;> unfold evalTarget <;> mono ih

theorem Mono.props_succ (env : Env) (a : Addr) (ps : List PropDef) :
    evalProps (n + 1) env a ps ⊑ evalProps (m + 1) env a ps := by
  cases ps <;> unfold evalProps <;> mono ih

theorem Mono.members_succ (env : Env) (proto c : Addr) (ms : List ClassMember) :
    evalMembers (n + 1) env proto c ms ⊑ evalMembers (m + 1) env proto c ms := by
  cases ms <;> unfold evalMembers <;> mono ih

theorem Mono.fields_succ (env : Env) (a : Addr) (fs : List (String × Expr)) :
    evalFields (n + 1) env a fs ⊑ evalFields (m + 1) env a fs := by
  cases fs <;> unfold evalFields <;> mono ih

theorem Mono.call_succ (callee this : Val) (args : List Val) :
    callFn (n + 1) callee this args ⊑ callFn (m + 1) callee this args := by
  unfold callFn
  mono ih

theorem Mono.construct_succ (callee : Val) (args : List Val) :
    CoreJS.construct (n + 1) callee args ⊑ CoreJS.construct (m + 1) callee args := by
  unfold CoreJS.construct
  mono ih

theorem Mono.stmt_succ (env : Env) (s : Stmt) : execStmt (n + 1) env s ⊑ execStmt (m + 1) env s := by
  cases s with
  | tryCatch body param handler =>
    unfold execStmt
    intro s
    dsimp only
    rcases ih.stmt env body s with h | h
    · rw [h]; exact .inl rfl
    · rw [← h]
      cases execStmt n env body s with
      | throw v s' =>
        dsimp only
        refine (show _ ⊑ _ from ?_) s'
        mono ih
      | _ => exact .inr rfl
  | tryFinally body finalizer =>
    unfold execStmt
    intro s
    dsimp only
    rcases ih.stmt env body s with h | h
    · rw [h]; exact .inl rfl
    · rw [← h]
      cases execStmt n env body s with
      | ok flow s' =>
        dsimp only
        refine (show _ ⊑ _ from ?_) s'
        mono ih
      | throw v s' =>
        dsimp only
        refine (show _ ⊑ _ from ?_) s'
        mono ih
      | _ => exact .inr rfl
  | decl kind x e => cases kind <;> unfold execStmt <;> mono ih
  | _ => unfold execStmt; mono ih

theorem Mono.stmts_succ (env : Env) (ss : List Stmt) : execStmts (n + 1) env ss ⊑ execStmts (m + 1) env ss := by
  cases ss <;> unfold execStmts <;> mono ih

theorem Mono.loop_succ (env : Env) (fresh : List Name) (test update : Expr) (body : Stmt) :
    CoreJS.loop (n + 1) env fresh test update body ⊑ CoreJS.loop (m + 1) env fresh test update body := by
  unfold CoreJS.loop
  mono ih

end step

theorem mono : ∀ n m, n ≤ m → Mono n m := by
  intro n
  induction n with
  | zero =>
    intro m _
    exact ⟨fun _ _ => M.le_outOfFuel _, fun _ _ => M.le_outOfFuel _, fun _ _ => M.le_outOfFuel _,
      fun _ _ _ => M.le_outOfFuel _, fun _ _ _ _ => M.le_outOfFuel _, fun _ _ _ => M.le_outOfFuel _,
      fun _ _ _ => M.le_outOfFuel _, fun _ _ => M.le_outOfFuel _, fun _ _ => M.le_outOfFuel _,
      fun _ _ => M.le_outOfFuel _, fun _ _ _ _ _ => M.le_outOfFuel _⟩
  | succ n ih =>
    intro m hm
    cases m with
    | zero => omega
    | succ m =>
      have ih := ih m (by omega)
      exact ⟨ih.expr_succ, ih.args_succ, ih.target_succ, ih.props_succ, ih.members_succ, ih.fields_succ, ih.call_succ,
        ih.construct_succ, ih.stmt_succ, ih.stmts_succ, ih.loop_succ⟩

theorem runUnit_le {n m : Nat} (h : n ≤ m) (env : Env) (ss : List Stmt) (j : Nat) :
    runUnit n env ss j ⊑ runUnit m env ss j := by
  have ih := mono n m h
  unfold runUnit
  mono ih

theorem runUnits_le {n m : Nat} (h : n ≤ m) (env : Env) (ss : List Stmt) (order : List Nat) :
    runUnits n env ss order ⊑ runUnits m env ss order := by
  induction order with
  | nil => exact M.le_refl _
  | cons j rest ih => exact M.le_bind (runUnit_le h env ss j) fun _ => ih

/-- **More fuel never changes a run that finished.** -/
theorem fuel_mono {n m : Nat} {ss : List Stmt} {r : Res Unit} (h : runProgram n ss = r) (hr : r ≠ .fuel)
    (hnm : n ≤ m) : runProgram m ss = r := by
  have hle : (runProgram n ss).le (runProgram m ss) :=
    M.le_bind (M.le_refl _) (fun env => runUnits_le hnm env ss _) (initialState ss.length)
  rcases hle with hf | he
  · exact absurd (h ▸ hf) hr
  · exact he ▸ h

/-- The program ends with `r`: some bound on the depth is enough. -/
def Ends (ss : List Stmt) (r : Res Unit) : Prop := ∃ n, runProgram n ss = r ∧ r ≠ .fuel

/-- A program ends in at most one way. -/
theorem Ends.unique {ss : List Stmt} {r r' : Res Unit} (h : Ends ss r) (h' : Ends ss r') : r = r' := by
  obtain ⟨n, hn, hr⟩ := h
  obtain ⟨n', hn', hr'⟩ := h'
  rw [← fuel_mono hn hr (Nat.le_max_left n n'), ← fuel_mono hn' hr' (Nat.le_max_right n n')]

#print axioms fuel_mono
#print axioms Ends.unique

end CoreJS

import EsmSplit.CoreJS.AsyncPrefix
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.Iterator

/-!
# CoreJS: more fuel never changes a run that finished

`fuel` bounds the depth of an evaluation. `fuel_mono`: a run that ends with anything but `Res.fuel` ends the same way
with any larger bound. So "the program ends with `r`" (`Ends`) names at most one `r`.

The proof compares the two runs rule by rule. `m ⊑ m'` says that `m'` does what `m` does wherever `m` finishes, and
every rule of the interpreter keeps `⊑` when its parts do.
-/

namespace CoreJS

variable {W : World}

/-- `r'` is `r`, unless `r` is out of fuel. -/
def Res.le {α : Type} (r r' : Res W α) : Prop := r = .fuel ∨ r = r'

/-- `m'` does what `m` does, in every state where `m` finishes. -/
def M.le {α : Type} (m m' : M W α) : Prop := ∀ s, (m s).le (m' s)

@[inherit_doc] scoped infix:50 " ⊑ " => M.le

theorem M.le_refl {α : Type} (m : M W α) : m ⊑ m := fun _ => .inr rfl

theorem M.le_outOfFuel {α : Type} (m : M W α) : outOfFuel ⊑ m := fun _ => .inl rfl

theorem M.le_bind {α β : Type} {m m' : M W α} {f f' : α → M W β} (hm : m ⊑ m') (hf : ∀ a, f a ⊑ f' a) :
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

theorem M.le_map {α β : Type} {m m' : M W α} (f : α → β) (hm : m ⊑ m') : (f <$> m) ⊑ (f <$> m') :=
  M.le_bind hm fun _ => M.le_refl _

theorem M.le_discard {α : Type} {m m' : M W α} (hm : m ⊑ m') : discard m ⊑ discard m' :=
  M.le_bind hm fun _ => M.le_refl _

/-- What a call does, compared for every callee, `this` and arguments. -/
def Call.le (call call' : Call W) : Prop := ∀ f this args, call f this args ⊑ call' f this args

/-- One step of a comparison: the same computation on both sides, a sequence, or a case split. -/
syntax "mono_step" : tactic
macro_rules
  | `(tactic| mono_step) => `(tactic| first
    | with_reducible exact M.le_refl _
    | with_reducible refine M.le_bind ?_ (fun _ => ?_)
    | with_reducible refine M.le_map _ ?_
    | with_reducible refine M.le_discard ?_
    | split)

theorem getFrom_le {call call' : Call W} (h : Call.le call call') (a : Addr) (base : Val) (k : PropertyKey) :
    getFrom call a base k ⊑ getFrom call' a base k := by
  unfold getFrom
  repeat' mono_step
  exact h _ _ _

theorem copySpreadKeys_le {call call' : Call W} (h : Call.le call call') (target source : Addr) (keys : List PropertyKey) :
    copySpreadKeys call target source keys ⊑ copySpreadKeys call' target source keys := by
  induction keys with
  | nil => exact M.le_refl _
  | cons key rest ih =>
    unfold copySpreadKeys
    repeat' first | exact ih | exact getFrom_le h _ _ _ | mono_step

theorem copySpread_le {call call' : Call W} (h : Call.le call call') (target : Addr) (excluded : List Name) (v : Val) :
    copySpread call target excluded v ⊑ copySpread call' target excluded v := by
  cases v <;> unfold copySpread <;> repeat' first | exact copySpreadKeys_le h _ _ _ | mono_step

theorem protoProp_le {call call' : Call W} (h : Call.le call call') (owner : String) (names : List String) (base : Val)
    (k : PropertyKey) : protoProp call owner names base k ⊑ protoProp call' owner names base k := by
  cases k <;> simp only [protoProp] <;> try split
  all_goals first | exact M.le_refl _ | exact getFrom_le h _ _ _

theorem getProp_le {call call' : Call W} (h : Call.le call call') (base key : Val) :
    getProp call base key ⊑ getProp call' base key := by
  unfold getProp
  repeat' first | exact getFrom_le h _ _ _ | exact protoProp_le h _ _ _ _ | mono_step

theorem getSuperProp_le {call call' : Call W} (h : Call.le call call') (home : Option Addr) (receiver key : Val) :
    getSuperProp call home receiver key ⊑ getSuperProp call' home receiver key := by
  cases home with
  | none => exact M.le_refl _
  | some a =>
    unfold getSuperProp
    refine M.le_bind (M.le_refl _) fun key => M.le_bind (M.le_refl _) fun obj => ?_
    cases obj.proto <;> simp only []
    · exact M.le_refl _
    · exact getFrom_le h _ _ key

theorem setProp_le {call call' : Call W} (h : Call.le call call') (base key v : Val) :
    setProp call base key v ⊑ setProp call' base key v := by
  unfold setProp
  repeat' mono_step
  exact h _ _ _

theorem toDesc_le {call call' : Call W} (h : Call.le call call') (v : Val) : toDesc call v ⊑ toDesc call' v := by
  unfold toDesc
  repeat' first | exact getProp_le h _ _ | mono_step

theorem regexpFlags_le {call call' : Call W} (h : Call.le call call') (v : Val) (keys : List (String × String)) :
    regexpFlags call v keys ⊑ regexpFlags call' v keys := by
  induction keys with
  | nil => exact M.le_refl _
  | cons pair rest ih =>
    rcases pair with ⟨k, flag⟩
    unfold regexpFlags
    repeat' first | exact ih | exact getProp_le h _ _ | mono_step

theorem ordinaryInstance_le {call call' : Call W} (h : Call.le call call') (value ctor : Val) :
    ordinaryInstance call value ctor ⊑ ordinaryInstance call' value ctor := by
  cases value <;> unfold ordinaryInstance <;> repeat' first | exact getProp_le h _ _ | mono_step

theorem instanceOf_le {call call' : Call W} (h : Call.le call call') (value ctor : Val) :
    instanceOf call value ctor ⊑ instanceOf call' value ctor := by
  unfold instanceOf
  repeat' first | exact ordinaryInstance_le h _ _ | exact getProp_le h _ _ | exact h _ _ _ | mono_step

theorem prim_le {call call' : Call W} (h : Call.le call call') (p : Prim) (args : List Val) :
    prim call p args ⊑ prim call' p args := by
  unfold prim
  repeat' first | exact instanceOf_le h _ _ | exact h _ _ _ | exact toDesc_le h _ | exact regexpFlags_le h _ _ | unfold readRegExp | mono_step

theorem getRef_le {call call' : Call W} (h : Call.le call call') (env : Env) (r : Ref) :
    getRef call env r ⊑ getRef call' env r := by
  cases r <;> first | exact M.le_refl _ | exact getProp_le h _ _

theorem putRef_le {call call' : Call W} (h : Call.le call call') (env : Env) (r : Ref) (v : Val) :
    putRef call env r v ⊑ putRef call' env r v := by
  cases r <;> first | exact M.le_refl _ | exact setProp_le h _ _ _

theorem attempt_le {α : Type} {m m' : M W α} (h : m ⊑ m') : attempt m ⊑ attempt m' := by
  intro s
  unfold attempt
  rcases h s with h | h
  · rw [h]; exact .inl rfl
  · rw [← h]; exact .inr rfl

theorem perform_le {call call' : Call W} {cons cons' : Val → List Val → M W Val} (h : Call.le call call')
    (hc : ∀ f args, cons f args ⊑ cons' f args) (code : Nat) (cells : List (Option Addr)) (req : Request) :
    perform call cons code cells req ⊑ perform call' cons' code cells req := by
  cases req <;> unfold perform <;> dsimp only <;> repeat' first
    | exact getProp_le h _ _
    | exact setProp_le h _ _ _
    | exact prim_le h _ _
    | exact (h _ _ _)
    | exact (hc _ _)
    | mono_step
    | exact M.le_refl _

theorem classParents_le {call call' : Call W} (h : Call.le call call') (base : Val) :
    classParents call base ⊑ classParents call' base := by
  cases base <;> simp only [classParents, classParentsNonNull] <;> repeat' first
    | exact getProp_le h _ _
    | mono_step

private theorem evalStaticInitializers_le {eval eval' : Expr → M W Val} (h : ∀ e, eval e ⊑ eval' e)
    (target : Addr) (plan : List (Option PropertyKey × Expr)) :
    evalStaticInitializers eval target plan ⊑ evalStaticInitializers eval' target plan := by
  induction plan with
  | nil => exact M.le_refl _
  | cons step rest ih =>
    rcases step with ⟨key, init⟩
    exact M.le_bind (h init) fun _ => M.le_bind (M.le_refl _) fun _ => ih

theorem getIterator_le {call call' : Call W} (h : Call.le call call') (v : Val) :
    getIterator call v ⊑ getIterator call' v := by
  unfold getIterator
  repeat' first | exact getProp_le h _ _ | exact h _ _ _ | mono_step

theorem iteratorStep_le {call call' : Call W} (h : Call.le call call') (cursor : IteratorCursor) (readValue : Bool) :
    iteratorStep call cursor readValue ⊑ iteratorStep call' cursor readValue := by
  cases cursor <;> simp only [iteratorStep] <;>
    repeat' first | exact getProp_le h _ _ | exact h _ _ _ | mono_step

theorem callIteratorReturn_le {call call' : Call W} (h : Call.le call call') (cursor : IteratorCursor) :
    callIteratorReturn call cursor ⊑ callIteratorReturn call' cursor := by
  cases cursor <;> simp only [callIteratorReturn] <;>
    repeat' first | exact getProp_le h _ _ | exact h _ _ _ | mono_step

theorem iteratorClose_le {α : Type} {call call' : Call W} (h : Call.le call call') (cursor : IteratorCursor)
    (completion : Except Val α) :
    iteratorClose call cursor completion ⊑ iteratorClose call' cursor completion := by
  unfold iteratorClose
  exact M.le_bind (attempt_le (callIteratorReturn_le h cursor)) (fun _ => M.le_refl _)

theorem collectIterator_le {call call' : Call W} (h : Call.le call call') {n m : Nat} (hFuel : n ≤ m)
    (cursor : IteratorCursor) : collectIterator call n cursor ⊑ collectIterator call' m cursor := by
  induction n generalizing m cursor with
  | zero => exact M.le_outOfFuel _
  | succ n ih =>
    cases m with
    | zero => omega
    | succ m =>
      simp only [collectIterator]
      refine M.le_bind (iteratorStep_le h cursor true) fun step => ?_
      cases step with
      | none => exact M.le_refl _
      | some next => exact M.le_map _ (ih (by omega) next.2)

theorem iteratorValues_le {call call' : Call W} (h : Call.le call call') {n m : Nat} (hFuel : n ≤ m)
    (source : Val) : iteratorValues n call source ⊑ iteratorValues m call' source :=
  M.le_bind (getIterator_le h source) (collectIterator_le h hFuel)

private theorem optionElim_le {α β : Type} (a : Option α) (fallback : M W β) {some some' : α → M W β}
    (h : ∀ x, some x ⊑ some' x) : a.elim fallback some ⊑ a.elim fallback some' := by
  cases a with
  | none => exact M.le_refl _
  | some x => exact h x

theorem bindArrayElements_le {call call' : Call W} {bindField bindField' : BindingPattern → Val → M W Unit}
    (hCall : Call.le call call') (hBind : ∀ p v, bindField p v ⊑ bindField' p v)
    (elements : List (Option BindingPattern)) (cursor : Option IteratorCursor) :
    bindArrayElements call bindField elements cursor ⊑ bindArrayElements call' bindField' elements cursor := by
  induction elements generalizing cursor with
  | nil => exact M.le_refl _
  | cons p rest ih =>
    simp only [bindArrayElements]
    repeat' first
    | exact optionElim_le _ _ (fun cursor => iteratorStep_le hCall cursor _)
    | exact attempt_le (optionElim_le _ _ (fun p => hBind p _))
    | exact ih _
    | exact iteratorClose_le hCall _ _
    | mono_step

theorem bindArrayPattern_le {call call' : Call W} {bindField bindField' : BindingPattern → Val → M W Unit}
    (hCall : Call.le call call') (hBind : ∀ p v, bindField p v ⊑ bindField' p v)
    {n m : Nat} (hFuel : n ≤ m) (elements : List (Option BindingPattern)) (rest : Option BindingPattern) (v : Val) :
    bindArrayPattern call n bindField elements rest v ⊑ bindArrayPattern call' m bindField' elements rest v := by
  unfold bindArrayPattern
  repeat' first
  | exact getIterator_le hCall _
  | exact bindArrayElements_le hCall hBind _ _
  | exact iteratorClose_le hCall _ _
  | exact optionElim_le _ _ (collectIterator_le hCall hFuel)
  | exact hBind _ _
  | mono_step

theorem loopIterator_le {call call' : Call W} {body body' : Val → M W Flow}
    (hCall : Call.le call call') (hBody : ∀ v, body v ⊑ body' v)
    {n m : Nat} (hFuel : n ≤ m) (cursor : IteratorCursor) :
    loopIterator call body n cursor ⊑ loopIterator call' body' m cursor := by
  induction n generalizing m cursor with
  | zero => exact M.le_outOfFuel _
  | succ n ih =>
    cases m with
    | zero => omega
    | succ m =>
      simp only [loopIterator]
      repeat' first
      | exact iteratorStep_le hCall _ _
      | exact attempt_le (hBody _)
      | exact ih (Nat.le_of_succ_le_succ hFuel) _
      | exact iteratorClose_le hCall _ _
      | mono_step

/-- Every function of the interpreter with the bound `n`, against the same with the bound `m`. -/
structure Mono (W : World) (n m : Nat) : Prop where
  expr : ∀ env e, (evalExpr n env e : M W _) ⊑ evalExpr m env e
  args : ∀ env es, (evalArgs n env es : M W _) ⊑ evalArgs m env es
  target : ∀ env t, (evalTarget n env t : M W _) ⊑ evalTarget m env t
  props : ∀ env a ps, (evalProps n env a ps : M W _) ⊑ evalProps m env a ps
  members : ∀ env proto c ms, (evalMembers n env proto c ms : M W _) ⊑ evalMembers m env proto c ms
  fields : ∀ env a fs, (evalFields n env a fs : M W _) ⊑ evalFields m env a fs
  call : ∀ callee this args, (callFn n callee this args : M W _) ⊑ callFn m callee this args
  construct : ∀ target callee args, (constructTarget n target callee args : M W _) ⊑ constructTarget m target callee args
  binding : ∀ env k p v, (bindPattern n env k p v : M W _) ⊑ bindPattern m env k p v
  stmt : ∀ env s, (execStmt n env s : M W _) ⊑ execStmt m env s
  stmts : ∀ env ss, (execStmts n env ss : M W _) ⊑ execStmts m env ss
  loop : ∀ env fresh test update body postTest,
    (loop n env fresh test update body postTest : M W _) ⊑ loop m env fresh test update body postTest
  converse : ∀ code cells told, (converse n code cells told : M W _) ⊑ converse m code cells told

theorem Mono.callLe {n m : Nat} (h : Mono W n m) : Call.le (callFn n : Call W) (callFn m) := h.call

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
    | with_reducible exact ($h).construct _ _ _
    | with_reducible exact ($h).binding _ _ _ _
    | with_reducible exact ($h).stmt _ _
    | with_reducible exact ($h).stmts _ _
    | with_reducible exact ($h).loop _ _ _ _ _ _
    | with_reducible exact ($h).converse _ _ _
    | with_reducible exact attempt_le (perform_le ($h).callLe (fun f args => ($h).construct f f args) _ _ _)
    | with_reducible exact copySpread_le ($h).callLe _ _ _
    | with_reducible exact classParents_le ($h).callLe _
    | with_reducible exact evalStaticInitializers_le (fun e => ($h).expr _ e) _ _
    | with_reducible exact getProp_le ($h).callLe _ _
    | with_reducible exact getSuperProp_le ($h).callLe _ _ _
    | with_reducible exact setProp_le ($h).callLe _ _ _
    | with_reducible exact getRef_le ($h).callLe _ _
    | with_reducible exact putRef_le ($h).callLe _ _ _
    | with_reducible exact prim_le ($h).callLe _ _)

syntax "mono " ident : tactic
macro_rules
  | `(tactic| mono $h) => `(tactic| repeat' first | mono_part $h | mono_step)

section step
theorem AsyncPrefix.invokeBody_le (prototype : Addr) {body body' : M W Val}
    (h : body ⊑ body') : invokeBody prototype body ⊑ invokeBody prototype body' := by
  unfold invokeBody
  exact M.le_bind (M.le_refl _) fun _ =>
    M.le_bind (attempt_le h) fun _ => M.le_refl _

variable {n m : Nat} (ih : Mono W n m)
include ih

theorem Mono.expr_succ (env : Env) (e : Expr) : (evalExpr (n + 1) env e : M W _) ⊑ evalExpr (m + 1) env e := by
  cases e <;> unfold evalExpr <;> try unfold finishDerived <;> mono ih

theorem Mono.args_succ (hFuel : n ≤ m) (env : Env) (es : List Expr) : (evalArgs (n + 1) env es : M W _) ⊑ evalArgs (m + 1) env es := by
  cases es with
  | nil => exact M.le_refl _
  | cons e es =>
    unfold evalArgs
    generalize e.argument = arg
    obtain ⟨spread, operand⟩ := arg
    cases spread <;> simp only [Bool.false_eq_true, ↓reduceIte] <;> mono ih
    exact iteratorValues_le ih.callLe hFuel _

theorem Mono.target_succ (env : Env) (t : Target) : (evalTarget (n + 1) env t : M W _) ⊑ evalTarget (m + 1) env t := by
  cases t <;> unfold evalTarget <;> mono ih

theorem Mono.props_succ (env : Env) (a : Addr) (ps : List PropDef) :
    (evalProps (n + 1) env a ps : M W _) ⊑ evalProps (m + 1) env a ps := by
  cases ps <;> unfold evalProps <;> mono ih

theorem Mono.members_succ (env : Env) (proto c : Addr) (ms : List ClassMember) :
    (evalMembers (n + 1) env proto c ms : M W _) ⊑ evalMembers (m + 1) env proto c ms := by
  cases ms <;> unfold evalMembers <;> mono ih

theorem Mono.fields_succ (env : Env) (a : Addr) (fs : List (PropertyKey × Expr)) :
    (evalFields (n + 1) env a fs : M W _) ⊑ evalFields (m + 1) env a fs := by
  cases fs <;> unfold evalFields <;> mono ih

theorem Mono.call_succ (callee this : Val) (args : List Val) :
    (callFn (n + 1) callee this args : M W _) ⊑ callFn (m + 1) callee this args := by
  unfold callFn
  mono ih
  apply AsyncPrefix.invokeBody_le
  mono ih

theorem Mono.construct_succ (target callee : Val) (args : List Val) :
    (constructTarget (n + 1) target callee args : M W _) ⊑ constructTarget (m + 1) target callee args := by
  have superArgsMono (argExprs : SuperCall) (env : Env) :
      (evalSuperArguments (evalArgs n env) env args argExprs : M W _) ⊑
        evalSuperArguments (evalArgs m env) env args argExprs := by
    cases argExprs with
    | forward => exact M.le_refl _
    | explicit es => exact ih.args env es
    | conditional choice yes no =>
      unfold evalSuperArguments
      refine M.le_bind (M.le_refl _) fun value => ?_
      cases value <;> try exact M.le_refl _
      case bool selected =>
        cases selected
        · exact ih.args env no
        · exact ih.args env yes
  unfold constructTarget constructParent initDerivedFields
  mono ih
  exact superArgsMono _ _

theorem Mono.binding_succ (hFuel : n ≤ m) (env : Env) (kind : DeclKind) (p : BindingPattern) (v : Val) :
    (bindPattern (n + 1) env kind p v : M W _) ⊑ bindPattern (m + 1) env kind p v := by
  cases p with
  | name => simp only [bindPattern]; exact M.le_refl _
  | defaultValue => simp only [bindPattern]; mono ih
  | array elements rest =>
    exact bindArrayPattern_le ih.callLe (ih.binding env kind) hFuel elements rest v
  | object props =>
    simp only [bindPattern]
    have fields : ∀ props : List (Expr × BindingPattern),
        (props.forM (fun (key, field) => do
          let key ← evalExpr n env key
          let v' ← getProp (callFn n) v key
          bindPattern n env kind field v') : M W Unit) ⊑
        props.forM (fun (key, field) => do
          let key ← evalExpr m env key
          let v' ← getProp (callFn m) v key
          bindPattern m env kind field v') := by
      intro props
      induction props with
      | nil => exact M.le_refl _
      | cons item rest step =>
        obtain ⟨key, field⟩ := item
        unfold List.forM
        repeat' first | exact step | mono_part ih | mono_step
    split <;> first | exact M.le_refl _ | exact fields props

theorem Mono.stmt_succ (hFuel : n ≤ m) (env : Env) (s : Stmt) : (execStmt (n + 1) env s : M W _) ⊑ execStmt (m + 1) env s := by
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
  | forOf kind pattern source body =>
    simp only [execStmt]
    refine M.le_bind (M.le_refl _) fun sourceEnv => ?_
    refine M.le_bind (ih.expr sourceEnv source) fun value => ?_
    refine M.le_bind (getIterator_le ih.callLe value) fun cursor => ?_
    refine loopIterator_le ih.callLe (fun v => ?_) hFuel cursor
    mono ih
  | «opaque» code decls funs free run =>
    cases run with
    | world => unfold execStmt; mono ih
    | closures fns => unfold execStmt; exact M.le_refl _
  | _ => unfold execStmt; mono ih

theorem Mono.stmts_succ (env : Env) (ss : List Stmt) : (execStmts (n + 1) env ss : M W _) ⊑ execStmts (m + 1) env ss := by
  cases ss <;> unfold execStmts <;> mono ih

theorem Mono.loop_succ (env : Env) (fresh : List Name) (test update : Expr) (body : Stmt) (postTest : Bool) :
    (CoreJS.loop (n + 1) env fresh test update body postTest : M W _) ⊑
      CoreJS.loop (m + 1) env fresh test update body postTest := by
  cases postTest <;> simp only [CoreJS.loop, loopCondition, Bool.not_true, Bool.not_false, Bool.false_eq_true,
    ↓reduceIte] <;> mono ih

theorem Mono.converse_succ (code : Nat) (cells : List (Option Addr)) (told : Told) :
    (CoreJS.converse (n + 1) code cells told : M W _) ⊑ CoreJS.converse (m + 1) code cells told := by
  unfold CoreJS.converse
  mono ih

end step

theorem mono : ∀ n m, n ≤ m → Mono W n m := by
  intro n
  induction n with
  | zero =>
    intro m _
    exact ⟨fun _ _ => M.le_outOfFuel _, fun _ _ => M.le_outOfFuel _, fun _ _ => M.le_outOfFuel _,
      fun _ _ _ => M.le_outOfFuel _, fun _ _ _ _ => M.le_outOfFuel _, fun _ _ _ => M.le_outOfFuel _,
      fun _ _ _ => M.le_outOfFuel _, fun _ _ _ => M.le_outOfFuel _, fun _ _ _ _ => M.le_outOfFuel _,
      fun _ _ => M.le_outOfFuel _,
      fun _ _ => M.le_outOfFuel _, fun _ _ _ _ _ _ => M.le_outOfFuel _, fun _ _ _ => M.le_outOfFuel _⟩
  | succ n ih =>
    intro m hm
    cases m with
    | zero => omega
    | succ m =>
      have ih := ih m (by omega)
      exact ⟨ih.expr_succ, ih.args_succ (by omega), ih.target_succ, ih.props_succ, ih.members_succ, ih.fields_succ, ih.call_succ,
        ih.construct_succ, ih.binding_succ (by omega), ih.stmt_succ (by omega), ih.stmts_succ, ih.loop_succ, ih.converse_succ⟩

theorem runUnits_le {n m : Nat} (h : n ≤ m) (units : List LinkedUnit) :
    (runUnits n units : M W _) ⊑ runUnits m units := by
  induction units with
  | nil => exact M.le_refl _
  | cons unit rest ih =>
    obtain ⟨id, s, env⟩ := unit
    have hs := (mono (W := W) n m h).stmt env s
    unfold runUnits runUnit inRegion
    repeat' first | exact hs | exact ih | mono_step

/-- **More fuel never changes a run that finished.** -/
theorem fuel_mono {n m : Nat} {P : Program} {r : Res W Unit} (h : runProgram W n P = r) (hr : r ≠ .fuel)
    (hnm : n ≤ m) : runProgram W m P = r := by
  have hle : (runProgram W n P).le (runProgram W m P) :=
    M.le_bind (M.le_refl _) (fun linked => runUnits_le hnm (unitsOf linked)) (initialState W P.regions)
  rcases hle with hf | he
  · exact absurd (h ▸ hf) hr
  · exact he ▸ h

/-- The program ends with `r`: some bound on the depth is enough. -/
def Ends (W : World) (P : Program) (r : Res W Unit) : Prop := ∃ n, runProgram W n P = r ∧ r ≠ .fuel

/-- A program ends in at most one way. -/
theorem Ends.unique {P : Program} {r r' : Res W Unit} (h : Ends W P r) (h' : Ends W P r') : r = r' := by
  obtain ⟨n, hn, hr⟩ := h
  obtain ⟨n', hn', hr'⟩ := h'
  rw [← fuel_mono hn hr (Nat.le_max_left n n'), ← fuel_mono hn' hr' (Nat.le_max_right n n')]


end CoreJS

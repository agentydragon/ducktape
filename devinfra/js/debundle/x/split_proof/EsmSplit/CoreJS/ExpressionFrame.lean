import EsmSplit.CoreJS.InvocationFrame

/-! Frame the expression evaluator's cases, given frames for its smaller evaluations and calls. -/

namespace CoreJS

variable {W : World}

private theorem binop_nodes (op : BinOp) (a b v : Val) (h : binop op a b = .ok v) :
    v.nodes = [] := by
  cases op <;> simp only [binop, Functor.map, Except.map, bind, Except.bind, pure, Except.pure] at h
  all_goals
    repeat' (split at h)
    all_goals cases h <;> rfl

/-- The primitive binary operators cannot return an object. -/
theorem reachFrame_binop {r R cells A} (op : BinOp) (left right : Val) :
    ReachFrame r R cells A (SafeVal A) (lift (binop op left right) : M W Val) :=
  reachFrame_lift _ fun value result node edge => by
    rw [binop_nodes op left right value result] at edge; cases edge

private theorem unop_nodes (s : State W) (op : UnOp) (a v : Val) (h : unop s op a = .ok v) :
    v.nodes = [] := by
  cases op <;> simp only [unop, Functor.map, Except.map, pure, Except.pure] at h
  all_goals
    repeat' (split at h)
    all_goals cases h <;> rfl

theorem reachFrame_unop {r R cells A} (away : Away r R A) (op : UnOp) (v : Val)
    (safe : SafeVal A v) :
    ReachFrame r R cells A (SafeVal A)
      (do
        lift (unop (← getState) op v) : M W Val) := by
  constructor
  · intro s H C _ _
    have same : unop (s.put r H C) op v = unop s op v := by
      cases op <;> try rfl
      cases v <;> try rfl
      case obj a =>
        have outside : a.region ≠ r := away.region _ (safeVal_obj.1 safe)
        simp [unop, typeOf, isCallable, State.objAt_put, outside]
        rfl
    simp only [M.bind_apply, getState]
    rw [same]
    cases unop s op v with
    | ok value => rfl
    | error why => cases why <;> rfl
  · intro s _ _
    simp only [M.bind_apply, getState]
    cases unop s op v with
    | ok value => exact Preserves.refl r R s
    | error why => cases why <;> exact Preserves.refl r R s
  · intro s hs _
    simp only [M.bind_apply, getState]
    cases h : unop s op v with
    | ok value =>
      exact
        ⟨(fun node edge => by
            rw [unop_nodes s op v value h] at edge; cases edge),
          hs⟩
    | error why =>
      cases why with
      | error kind message => exact ⟨(fun _ hn => by cases hn), hs⟩
      | unknown why => exact hs

def RefSafe (A : HeapNode → Prop) : Ref → Prop
  | .name _ | .frozen _ => True
  | .prop o k => SafeVal A o ∧ SafeVal A k

theorem reachFrame_getRef {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (root : A (.obj objectProto)) (env : Env)
    (safe : ∀ node ∈ env.nodes, A node) (ref : Ref) (hr : RefSafe A ref) :
    ReachFrame r R cells A (SafeVal A) (getRef call env ref) := by
  cases ref with
  | name x
  | frozen x => exact reachFrame_readVar away env x safe
  | prop o k => exact reachFrame_getProp away call framed.getter root o k hr.1

theorem reachFrame_putRef {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (env : Env) (safe : ∀ node ∈ env.nodes, A node)
    (ref : Ref) (hr : RefSafe A ref) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (putRef call env ref v) := by
  cases ref with
  | name x => exact reachFrame_writeVar away env x v safe hv
  | frozen _ => exact reachFrame_typeError "assignment to constant variable"
  | prop o k => exact reachFrame_setProp away call framed.setter o k v hr.1 hv

theorem evalArgs_reachFrame_step {r R cells A n} {env : Env}
    (eval : ∀ e, ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (tail :
      ∀ es, ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (evalArgs n env es : M W _))
    (iterate :
      ∀ v,
        SafeVal A v →
          ReachFrame r R cells A (fun vs => ∀ x ∈ vs, SafeVal A x)
            (iteratorValues n (callFn n) v : M W _))
    (es : List Expr) :
    ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (evalArgs (n + 1) env es : M W _) := by
  cases es with
  | nil => exact reachFrame_pure r R cells A [] (by simp)
  | cons e es =>
    unfold evalArgs
    generalize e.argument = arg
    obtain ⟨spread, operand⟩ := arg
    apply (eval operand).bind
    intro v hv
    have expanded :
      ReachFrame r R cells A (fun vs => ∀ x ∈ vs, SafeVal A x)
        (if spread then iteratorValues n (callFn n) v else pure [v] : M W (List Val)) := by
      cases spread
      · exact reachFrame_pure r R cells A [v] (by simpa using hv)
      · exact iterate v hv
    cases spread <;> apply expanded.bind <;> intro vs hs <;>
      exact
        (tail es).bind fun rest hr =>
          reachFrame_pure r R cells A (vs ++ rest) fun x h =>
            (List.mem_append.mp h).elim (hs x) (hr x)

theorem evalTarget_reachFrame_step {r R cells A n} {env : Env}
    (eval : ∀ e, ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (target : Target) :
    ReachFrame r R cells A (RefSafe A) (evalTarget (n + 1) env target : M W Ref) := by
  cases target with
  | var x => exact reachFrame_pure r R cells A (Ref.name x) trivial
  | frozen x => exact reachFrame_pure r R cells A (Ref.frozen x) trivial
  | member o k =>
    exact
      (eval o).bind fun base hb =>
        (eval k).bind fun key hk => reachFrame_pure r R cells A (Ref.prop base key) ⟨hb, hk⟩

private theorem reachFrame_shortCircuits {r R cells A} (op : LogOp) (v : Val) :
    ReachFrame r R cells A (fun _ => True) (lift (shortCircuits op v) : M W Bool) :=
  reachFrame_lift _ (fun _ _ => trivial)

private theorem reachFrame_toNumber {r R cells A} (v : Val) :
    ReachFrame r R cells A (fun _ => True) (lift (toNumber v) : M W Float) :=
  reachFrame_lift _ (fun _ _ => trivial)

private theorem reachFrame_getSuperProp {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (env : Env) (safe : ∀ node ∈ env.nodes, A node)
    (key : Val) : ReachFrame r R cells A (SafeVal A) (getSuperProp call env.home env.this key) := by
  unfold getSuperProp
  split
  · rename_i home found
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    apply (reachFrame_getObj away (Env.nodes_home safe found)).bind
    intro obj safeObj
    cases hparent : obj.proto with
    | some parent =>
      have hp : A (.obj parent) := safeObj _ (by simp [Obj.nodes, hparent])
      exact reachFrame_getFrom away call framed.getter hp env.this (Env.nodes_this safe) k
    | none => exact reachFrame_typeError "cannot read properties of null super base"
  · exact reachFrame_unknown r R cells A _ _

theorem evalExpr_reachFrame_step {r R cells A n} (away : Away r R A) (env : Env)
    (safe : ∀ node ∈ env.nodes, A node) (root : A (.obj objectProto))
    (primitive :
      ∀ p vs,
        (∀ v ∈ vs, SafeVal A v) →
          ReachFrame r R cells A (SafeVal A) (prim (callFn n) p vs : M W Val))
    (eval : ∀ e, ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (args :
      ∀ es, ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (evalArgs n env es : M W _))
    (target : ∀ t, ReachFrame r R cells A (RefSafe A) (evalTarget n env t : M W Ref))
    (call : CallFramed r R cells A (callFn n : Call W))
    (construct :
      ∀ f vs,
        SafeVal A f →
          (∀ v ∈ vs, SafeVal A v) → ReachFrame r R cells A (SafeVal A) (construct n f vs : M W Val))
    (make : ∀ f, ReachFrame r R cells A (SafeVal A) (makeFunc env f : M W Val))
    (regexpRoot : A (.obj regexpProto))
    (allocate :
      ∀ o : Obj,
        (∀ node ∈ o.nodes, A node) →
          ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (makeObject :
      ∀ ps, ReachFrame r R cells A (SafeVal A) (evalExpr (n + 1) env (.obj ps) : M W Val))
    (makeArray :
      ∀ es, ReachFrame r R cells A (SafeVal A) (evalExpr (n + 1) env (.arr es) : M W Val))
    (makeClass :
      ∀ name self f members,
        ReachFrame r R cells A (SafeVal A)
          (evalExpr (n + 1) env (.cls name self f members) : M W Val))
    (extendClass :
      ∀ name self base members ctor,
        ReachFrame r R cells A (SafeVal A)
          (evalExpr (n + 1) env (.derived name self base members ctor) : M W Val))
    (e : Expr) : ReachFrame r R cells A (SafeVal A) (evalExpr (n + 1) env e : M W Val) := by
  have get := reachFrame_getProp away (callFn n) call.getter root
  have getRef' := reachFrame_getRef away (callFn n) call root env safe
  have putRef' := reachFrame_putRef away (callFn n) call env safe
  have undef : ReachFrame r R cells A (SafeVal A) (pure Val.undef : M W Val) :=
    reachFrame_pure r R cells A Val.undef (fun _ hn => by cases hn)
  have unary op input := (eval input).bind fun v hv => reachFrame_unop away op v hv
  cases e with
  | undef => exact undef
  | null
  | bool _
  | num _
  | str _
  | host _ => exact reachFrame_pure r R cells A _ (fun _ hn => by cases hn)
  | var x => exact reachFrame_readVar away env x safe
  | this => exact reachFrame_pure r R cells A env.this (Env.nodes_this safe)
  | func f => exact make f
  | regexp
    data =>
    have edges : ∀ node ∈ (regexpObject data).nodes, A node := by
      simpa [regexpObject, Obj.nodes, Property.nodes, Val.nodes] using regexpRoot
    exact
      (allocate _ edges).bind fun a ha => reachFrame_pure r R cells A (Val.obj a) (safeVal_obj.2 ha)
  | obj ps => exact makeObject ps
  | arr es => exact makeArray es
  | cls name self f members => exact makeClass name self f members
  | derived name self base members ctor => exact extendClass name self base members ctor
  | spreadArg _ => exact reachFrame_unknown r R cells A _ _
  | get o k => exact (eval o).bind fun base hb => (eval k).bind fun key _ => get base key hb
  | getSuper k =>
    exact (eval k).bind fun key _ => reachFrame_getSuperProp away (callFn n) call env safe key
  | getOptional o k =>
    simp only [evalExpr]
    apply (eval o).bind
    intro base hb
    apply (reachFrame_shortCircuits .nullish base).bind
    intro skip _
    cases skip
    · exact undef
    · exact (eval k).bind fun key _ => get base key hb
  | hasProp k o =>
    exact
      (eval k).bind fun key _ => (eval o).bind fun base hb => reachFrame_hasProp away key base hb
  | delete o k =>
    exact
      (eval o).bind fun base hb => (eval k).bind fun key _ => reachFrame_deleteProp away base key hb
  | assign t rhs =>
    exact
      (target t).bind fun ref hr =>
        (eval rhs).bind fun v hv =>
          (putRef' ref hr v hv).bind fun _ _ => reachFrame_pure r R cells A v hv
  | assignOp t op rhs =>
    exact
      (target t).bind fun ref hr =>
        (getRef' ref hr).bind fun old _ =>
          (eval rhs).bind fun right _ =>
            (reachFrame_binop op old right).bind fun v hv =>
              (putRef' ref hr v hv).bind fun _ _ => reachFrame_pure r R cells A v hv
  | assignLogical t op rhs =>
    simp only [evalExpr]
    apply (target t).bind
    intro ref hr
    apply (getRef' ref hr).bind
    intro old ho
    apply (reachFrame_shortCircuits op old).bind
    intro skip _
    cases skip
    · exact
        (eval rhs).bind fun v hv =>
          (putRef' ref hr v hv).bind fun _ _ => reachFrame_pure r R cells A v hv
    · exact reachFrame_pure r R cells A old ho
  | update t inc pre =>
    exact
      (target t).bind fun ref hr =>
        (getRef' ref hr).bind fun v _ =>
          (reachFrame_toNumber v).bind fun old _ =>
            let new := if inc then old + 1.0 else old - 1.0
            (putRef' ref hr (.num new) (fun _ hn => by cases hn)).bind fun _ _ =>
              reachFrame_pure r R cells A (Val.num (if pre then new else old))
                (fun _ hn => by cases hn)
  | call f es =>
    exact
      (eval f).bind fun v hv =>
        (args es).bind fun vs hs => call v .undef vs hv (fun _ hn => by cases hn) hs
  | callMethod o k es =>
    exact
      (eval o).bind fun base hb =>
        (eval k).bind fun key _ =>
          (get base key hb).bind fun f hf => (args es).bind fun vs hvs => call f base vs hf hb hvs
  | callSuper k es optional =>
    simp only [evalExpr]
    apply (eval k).bind
    intro key _
    apply (reachFrame_getSuperProp away (callFn n) call env safe key).bind
    intro f hf
    have invoke := (args es).bind fun vs hvs => call f env.this vs hf (Env.nodes_this safe) hvs
    cases optional
    · exact invoke
    · apply (reachFrame_shortCircuits .nullish f).bind
      intro present _
      cases present
      · exact undef
      · exact invoke
  | callOptionalReceiver o k es =>
    simp only [evalExpr]
    apply (eval o).bind
    intro base hb
    apply (reachFrame_shortCircuits .nullish base).bind
    intro skip _
    cases skip
    · exact undef
    · exact
        (eval k).bind fun key _ =>
          (get base key hb).bind fun f hf => (args es).bind fun vs hvs => call f base vs hf hb hvs
  | callOptionalCallee o k es property =>
    simp only [evalExpr]
    apply (eval o).bind
    intro base hb
    apply (eval k).bind
    intro key _
    apply (get base key hb).bind
    intro f hf
    apply (reachFrame_shortCircuits .nullish f).bind
    intro skip _
    cases skip
    · exact undef
    · apply (args es).bind
      intro vs hvs
      apply (call f base vs hf hb hvs).bind
      intro value hv
      cases property with
      | none => exact reachFrame_pure r R cells A value hv
      | some p => exact (eval p).bind fun key _ => get value key hv
  | new f es => exact (eval f).bind fun c hc => (args es).bind fun vs hs => construct c vs hc hs
  | prim p es => exact (args es).bind fun vs hs => primitive p vs hs
  | unary op input =>
    cases op
    case typeof =>
      cases input
      case var x =>
        simp only [evalExpr]
        split
        · exact reachFrame_pure r R cells A _ (fun _ hn => by cases hn)
        · exact
            (reachFrame_readVar away env x safe).bind fun v hv => reachFrame_unop away .typeof v hv
      all_goals exact unary .typeof _
    all_goals exact unary _ _
  | binary op a b => exact (eval a).bind fun x _ => (eval b).bind fun y _ => reachFrame_binop op x y
  | logical op a b =>
    simp only [evalExpr]
    apply (eval a).bind
    intro v hv
    apply (reachFrame_shortCircuits op v).bind
    intro skip _
    cases skip
    · exact eval b
    · exact reachFrame_pure r R cells A v hv
  | cond c t e =>
    simp only [evalExpr]
    apply (eval c).bind
    intro v _
    apply (reachFrame_lift (toBoolean v) (P := fun _ => True) fun _ _ => trivial).bind
    intro test _
    cases test
    · exact eval e
    · exact eval t
  | seq a b => exact (eval a).bind fun _ _ => eval b

end CoreJS

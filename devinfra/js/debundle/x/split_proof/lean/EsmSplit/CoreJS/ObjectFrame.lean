import EsmSplit.CoreJS.InvocationFrame
import EsmSplit.CoreJS.Request.Allocation

/-! Reachability frames for object, array and class literals. -/

namespace CoreJS

variable {W : World}

private theorem descriptorField_safe {A : HeapNode → Prop} {v : Val} (safe : SafeVal A v) :
    ∀ a, a ∈ some v → SafeVal A a := fun _ h => by cases h; exact safe

private theorem descriptorNone_safe {A : HeapNode → Prop} :
    ∀ a, a ∈ (none : Option Val) → SafeVal A a := fun _ h => by cases h

private theorem reachFrame_createData {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a))
    (k : PropertyKey) (v : Val) (safe : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (createData a k v : M W Unit) :=
  reachFrame_defineOwn away ha k _
    ⟨descriptorField_safe safe, descriptorNone_safe, descriptorNone_safe⟩

private theorem reachFrame_copySpreadKeys {r R cells A} (away : Away r R A) (call : Call W)
    (getter : ∀ f base, SafeVal A f → SafeVal A base → ReachFrame r R cells A (SafeVal A) (call f base []))
    {target source : Addr} (ht : A (.obj target)) (hs : A (.obj source)) (keys : List PropertyKey) :
    ReachFrame r R cells A (fun _ => True) (copySpreadKeys call target source keys) := by
  induction keys with
  | nil => exact reachFrame_pure r R cells A () trivial
  | cons key rest ih =>
    unfold copySpreadKeys
    apply (reachFrame_getObj away hs).bind
    intro current _
    split
    · apply (reachFrame_getFrom away call getter hs (.obj source) (safeVal_obj.2 hs) key).bind
      intro value safe
      exact (reachFrame_createData away ht key value safe).bind fun _ _ => ih
    · exact ih

/-- A spread snapshots keys, then checks each own descriptor in the state left by earlier getters. -/
theorem reachFrame_copySpread {r R cells A} (away : Away r R A) (call : Call W)
    (getter : ∀ f base, SafeVal A f → SafeVal A base → ReachFrame r R cells A (SafeVal A) (call f base []))
    {target : Addr} (ht : A (.obj target)) (source : Val) (hs : SafeVal A source) :
    ReachFrame r R cells A (fun _ => True) (copySpread call target source) := by
  cases source <;> simp only [copySpread]
  case obj a =>
    exact (reachFrame_ownKeys away (.obj a) hs).bind fun keys _ =>
      reachFrame_copySpreadKeys away call getter ht (safeVal_obj.1 hs) keys
  case str s =>
    split
    · apply ReachFrame.bind (P := fun _ => True) (Q := fun _ => True) _ (fun _ _ => reachFrame_pure r R cells A () trivial)
      generalize s.toList.zipIdx = chars
      induction chars with
      | nil => exact reachFrame_pure r R cells A () trivial
      | cons c rest ih =>
        simp only [List.forIn_cons]
        apply ReachFrame.bind (P := fun step => step = .yield ())
        · exact (reachFrame_createData away ht (toString c.2) (.str (String.singleton c.1))
            (by simp [SafeVal, Val.nodes])).bind fun _ _ => reachFrame_pure r R cells A _ rfl
        · intro step h
          subst step
          exact ih
    · exact reachFrame_unknown r R cells A _ _
  case host => exact reachFrame_unknown r R cells A _ _
  case error => exact reachFrame_unknown r R cells A _ _
  all_goals exact reachFrame_pure r R cells A () trivial

private theorem reachFrame_directProto {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a))
    (p : Option Addr) (hp : ∀ b ∈ p, A (.obj b)) :
    ReachFrame r R cells A (fun _ => True) (modifyObj a fun o => { o with proto := p } : M W Unit) := by
  apply reachFrame_modifyObj away a ha
  intro o node h
  simp only [Obj.nodes, List.mem_append] at h
  rcases h with (proto | props) | code
  · obtain ⟨b, hb, rfl⟩ := List.mem_map.mp proto
    exact Or.inr (hp b (Option.mem_toList.mp hb))
  · exact Or.inl (List.mem_append_left _ (List.mem_append_right _ props))
  · exact Or.inl (List.mem_append_right _ code)

theorem evalFields_reachFrame_step {r R cells A n} (away : Away r R A) (env : Env) {a : Addr}
    (ha : A (.obj a))
    (eval : ∀ e, ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (rest : ∀ fs, ReachFrame r R cells A (fun _ => True) (evalFields n env a fs : M W Unit))
    (fs : List (PropertyKey × Expr)) :
    ReachFrame r R cells A (fun _ => True) (evalFields (n + 1) env a fs : M W Unit) := by
  cases fs with
  | nil => exact reachFrame_pure r R cells A () trivial
  | cons f fs =>
    obtain ⟨key, initial⟩ := f
    exact (eval initial).bind fun v hv => (reachFrame_createData away ha key v hv).bind fun _ _ => rest fs

theorem evalProps_reachFrame_step {r R cells A n} (away : Away r R A) (env : Env) {a : Addr}
    (ha : A (.obj a))
    (getter : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (callFn n f base [] : M W Val))
    (eval : ∀ e, ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (make : ∀ home : Addr, A (.obj home) → ∀ f,
      ReachFrame r R cells A (SafeVal A) (makeFunc { env with home := some home } f : M W Val))
    (rest : ∀ ps, ReachFrame r R cells A (fun _ => True) (evalProps n env a ps : M W Unit))
    (ps : List PropDef) :
    ReachFrame r R cells A (fun _ => True) (evalProps (n + 1) env a ps : M W Unit) := by
  cases ps with
  | nil => exact reachFrame_pure r R cells A () trivial
  | cons head tail =>
    simp only [evalProps]
    cases head with
    | init k v =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      exact (eval v).bind fun value hv =>
        (reachFrame_createData away ha key value hv).bind fun _ _ => rest tail
    | method k f =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      exact (make a ha f).bind fun value hv =>
        (reachFrame_createData away ha key value hv).bind fun _ _ => rest tail
    | getter k f =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      apply (make a ha f).bind
      intro getter safe
      exact (reachFrame_defineOwn away ha key _ ⟨descriptorNone_safe, descriptorField_safe safe, descriptorNone_safe⟩).bind
        fun _ _ => rest tail
    | setter k f =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      apply (make a ha f).bind
      intro setter safe
      exact (reachFrame_defineOwn away ha key _ ⟨descriptorNone_safe, descriptorNone_safe, descriptorField_safe safe⟩).bind
        fun _ _ => rest tail
    | spread e =>
      exact (eval e).bind fun source hs =>
        (reachFrame_copySpread away (callFn n) getter ha source hs).bind fun _ _ => rest tail
    | proto e =>
      apply (eval e).bind
      intro v hv
      cases v
      case obj parent =>
        exact (reachFrame_directProto away ha (some parent) (fun p hp => by cases hp; exact safeVal_obj.1 hv)).bind
          fun _ _ => rest tail
      case null =>
        exact (reachFrame_directProto away ha none (fun _ h => by cases h)).bind fun _ _ => rest tail
      case host name => exact reachFrame_unknown r R cells A _ _
      all_goals exact rest tail

theorem evalMembers_reachFrame_step {r R cells A n} (away : Away r R A) (env : Env) {proto c : Addr}
    (hp : A (.obj proto)) (hc : A (.obj c))
    (eval : ∀ e, ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (make : ∀ home : Addr, A (.obj home) → ∀ f,
      ReachFrame r R cells A (SafeVal A) (makeFunc { env with home := some home } f : M W Val))
    (rest : ∀ ms, ReachFrame r R cells A (fun _ => True) (evalMembers n env proto c ms : M W _))
    (ms : List ClassMember) :
    ReachFrame r R cells A (fun _ => True) (evalMembers (n + 1) env proto c ms : M W _) := by
  cases ms with
  | nil => exact reachFrame_pure r R cells A [] trivial
  | cons head tail =>
    simp only [evalMembers]
    have hs (static : Bool) : A (.obj (if static then c else proto)) := by
      cases static
      · exact hp
      · exact hc
    cases head with
    | method static k f =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      apply (make _ (hs static) f).bind
      intro v hv
      apply (reachFrame_defineOwn away (hs static) key _
        ⟨descriptorField_safe hv, descriptorNone_safe, descriptorNone_safe⟩).bind
      intro _ _
      exact (rest tail).bind fun xs _ => reachFrame_pure r R cells A xs trivial
    | getter static k f =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      apply (make _ (hs static) f).bind
      intro v hv
      apply (reachFrame_defineOwn away (hs static) key _
        ⟨descriptorNone_safe, descriptorField_safe hv, descriptorNone_safe⟩).bind
      intro _ _
      exact (rest tail).bind fun xs _ => reachFrame_pure r R cells A xs trivial
    | setter static k f =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      apply (make _ (hs static) f).bind
      intro v hv
      apply (reachFrame_defineOwn away (hs static) key _
        ⟨descriptorNone_safe, descriptorNone_safe, descriptorField_safe hv⟩).bind
      intro _ _
      exact (rest tail).bind fun xs _ => reachFrame_pure r R cells A xs trivial
    | field static k _ =>
      apply (eval k).bind
      intro kv _
      apply (reachFrame_toKey r R cells A kv).bind
      intro key _
      exact (rest tail).bind fun xs _ => reachFrame_pure r R cells A ((static, key) :: xs) trivial

private theorem literalArray_nodes {A : HeapNode → Prop} (root : A (.obj arrayProto))
    (vs : List Val) (h : ∀ v ∈ vs, SafeVal A v) : ∀ node ∈ (arrayObject vs).nodes, A node := by
  intro node hn
  simp only [arrayObject, Obj.nodes, List.mem_append, List.mem_flatMap, List.mem_map,
    Option.toList_some, List.mem_singleton, Option.toList_none, List.flatMap_nil, List.mem_nil_iff, or_false] at hn
  rcases hn with hroot | ⟨p, hmem, hnode⟩
  · obtain ⟨_, rfl, rfl⟩ := hroot
    exact root
  · rcases hmem with ⟨item, hitem, rfl⟩ | hlength
    · exact h item.1 (List.fst_mem_of_mem_zipIdx hitem) node hnode
    · subst p
      cases hnode

theorem evalArray_reachFrame_step {r R cells A n} (env : Env) (root : A (.obj arrayProto))
    (args : ∀ es, ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (evalArgs n env es : M W _))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (es : List Expr) :
    ReachFrame r R cells A (SafeVal A) (evalExpr (n + 1) env (.arr es) : M W Val) :=
  (args es).bind fun values hv =>
    (allocate _ (literalArray_nodes root values hv)).bind fun a ha =>
      reachFrame_pure r R cells A (Val.obj a) (safeVal_obj.2 ha)

private theorem reachFrame_constructorLink {r R cells A} (away : Away r R A) (proto c : Addr)
    (hp : A (.obj proto)) (hc : A (.obj c)) :
    ReachFrame r R cells A (fun _ => True)
      (modifyObj proto fun o => { o with props := [("constructor", .data (.obj c) true false true)] } : M W Unit) := by
  apply reachFrame_modifyObj away proto hp
  intro o node hn
  simp [Obj.nodes, Property.nodes, Val.nodes] at hn ⊢
  rcases hn with h | rfl | h
  · exact Or.inl (Or.inl h)
  · exact Or.inr hc
  · exact Or.inl (Or.inr (Or.inr h))

private theorem reachFrame_constructorCode {r R cells A} (away : Away r R A) (c : Addr)
    (hc : A (.obj c)) (code : Code) (safe : ∀ node ∈ code.nodes, A node) :
    ReachFrame r R cells A (fun _ => True)
      (modifyObj c fun o => { o with code := some code } : M W Unit) := by
  apply reachFrame_modifyObj away c hc
  intro o node hn
  simp [Obj.nodes] at hn ⊢
  rcases hn with h | h | h
  · exact Or.inl (Or.inl h)
  · exact Or.inl (Or.inr (Or.inl h))
  · exact Or.inr (safe _ h)

theorem evalClass_reachFrame_step {r R cells A n} (away : Away r R A)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (bindings : ∀ a : Addr, a.region ≠ r → A (.cell a))
    (objectRoot : A (.obj objectProto)) (functionRoot : A (.obj functionProto))
    (env : Env) (safe : ∀ node ∈ env.nodes, A node)
    (evalMemberList : ∀ e proto c ms, (∀ node ∈ Env.nodes e, A node) → A (.obj proto) → A (.obj c) →
      ReachFrame r R cells A (fun _ => True) (evalMembers n e proto c ms : M W _))
    (evalFieldList : ∀ e c fields, (∀ node ∈ Env.nodes e, A node) → A (.obj c) →
      ReachFrame r R cells A (fun _ => True) (evalFields n e c fields : M W Unit))
    (name : Name) (self : Bool) (ctor : Func) (members : List ClassMember) :
    ReachFrame r R cells A (SafeVal A) (evalExpr (n + 1) env (.cls name self ctor members) : M W Val) := by
  simp only [evalExpr]
  apply (reachFrame_selfScope away bindings env self name safe).bind
  intro localEnv hlocal
  apply (reachFrame_alloc { proto := some objectProto, props := [] } ?_ objects).bind
  · intro proto hp
    let closed := { localEnv with home := some proto }.capture (ctor.free ++ freeMembers members)
    have safeClosed : ∀ node ∈ closed.nodes, A node :=
      Env.nodes_capture (Env.nodes_setHome hlocal (some proto) (by simpa)) _
    have ctorObj := reachFrame_alloc (W := W) (cells := cells) (R := R)
      { proto := some functionProto, code := some (.closure ctor closed []),
        props := [("length", .data (.num (Float.ofNat ctor.length)) false false true),
          ("name", .data (.str name) false false true), ("prototype", .data (.obj proto) false false false)] }
      (by
        intro node h
        simp [Obj.nodes, Property.nodes, Val.nodes, Code.nodes] at h
        rcases h with rfl | rfl | capture
        · exact functionRoot
        · exact hp
        · exact safeClosed node capture) objects
    apply ctorObj.bind
    intro c hc
    apply (reachFrame_constructorLink away proto c hp hc).bind
    intro _ _
    apply (evalMemberList localEnv proto c members hlocal hp hc).bind
    intro keys _
    let fields := keys.zip (fieldInits members)
    let perInstance := fields.filterMap fun ((static, k), init) => if static then none else some (k, init)
    apply (reachFrame_constructorCode away c hc (.closure ctor closed perInstance) safeClosed).bind
    intro _ _
    apply (reachFrame_initSelf away localEnv self name (.obj c) hlocal (safeVal_obj.2 hc)).bind
    intro _ _
    apply (evalFieldList { localEnv with this := .obj c, home := some c } c _
      (Env.nodes_setHome (Env.nodes_setThis hlocal (safeVal_obj.2 hc)) (some c) (by simpa)) hc).bind
    intro _ _
    exact reachFrame_pure r R cells A (Val.obj c) (safeVal_obj.2 hc)
  · simpa [Obj.nodes] using objectRoot

private theorem reachFrame_classParentsNonNull {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (root : A (.obj objectProto))
    (base : Val) (safe : SafeVal A base) :
    ReachFrame r R cells A (fun p => A (.obj p.1) ∧ ∀ a ∈ p.2, A (.obj a)) (classParentsNonNull call base) := by
  unfold classParentsNonNull
  apply (reachFrame_isConstructor away base safe).bind
  intro succeeds _
  cases succeeds
  · exact reachFrame_typeError _
  · apply (reachFrame_getProp away call framed.getter root base (.str "prototype") safe).bind
    intro proto hproto
    let coerceParent : Val → M W (Option Addr)
      | .null => pure none
      | .obj a => pure (some a)
      | .host _ => unknown "host superclass prototype"
      | _ => throwErr .type "superclass prototype is not an object or null"
    have parent : ReachFrame r R cells A (fun p => ∀ a ∈ p, A (.obj a)) (coerceParent proto) := by
      cases proto
      case null => exact reachFrame_pure r R cells A none (fun _ hn => by cases hn)
      case obj a => exact reachFrame_pure r R cells A (some a) (fun _ hm => by cases hm; exact safeVal_obj.1 hproto)
      case host name => exact reachFrame_unknown r R cells A _ _
      all_goals exact reachFrame_typeError _
    let finish (pp : Option Addr) : M W (Addr × Option Addr) :=
      match base with
      | .obj a => pure (a, pp)
      | _ => unknown "host superclass"
    have finishFrame (pp : Option Addr) (hp : ∀ a ∈ pp, A (.obj a)) :
        ReachFrame r R cells A (fun p => A (.obj p.1) ∧ ∀ a ∈ p.2, A (.obj a)) (finish pp) := by
      cases base
      case obj a => exact reachFrame_pure r R cells A (a, pp) ⟨safeVal_obj.1 safe, hp⟩
      all_goals exact reachFrame_unknown r R cells A _ _
    cases proto <;> cases base <;> exact parent.bind finishFrame

private theorem reachFrame_classParents {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (objectRoot : A (.obj objectProto)) (functionRoot : A (.obj functionProto))
    (base : Val) (safe : SafeVal A base) :
    ReachFrame r R cells A (fun p => A (.obj p.1) ∧ ∀ a ∈ p.2, A (.obj a)) (classParents call base) := by
  have nonNull := reachFrame_classParentsNonNull away call framed objectRoot base safe
  cases base
  case null =>
    exact reachFrame_pure r R cells A (functionProto, (none : Option Addr))
      ⟨functionRoot, fun _ h => by cases h⟩
  all_goals exact nonNull

private theorem derivedCtorCode_safe {A : HeapNode → Prop} {env : Env}
    (safe : ∀ node ∈ env.nodes, A node) (ctor : Option (List Stmt × Option (List Expr) × Func))
    (fields : List (PropertyKey × Expr)) :
    ∀ node ∈ (derivedCtorCode env ctor fields).1.nodes, A node := by
  cases ctor with
  | none =>
    cases fields with
    | nil => simp [derivedCtorCode, Code.nodes]
    | cons f fs => exact Env.nodes_capture safe _
  | some parts =>
    rcases parts with ⟨before, args, body⟩
    exact Env.nodes_capture safe _

private theorem reachFrame_finishDerived {r R cells A} (away : Away r R A)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (env : Env) (safe : ∀ node ∈ env.nodes, A node)
    (name : Name) (self : Bool) (members : List ClassMember) (parents : Addr × Option Addr)
    (ctor : Option (List Stmt × Option (List Expr) × Func))
    (guard : A (.obj parents.1) ∧ ∀ a ∈ parents.2, A (.obj a))
    (runMembers : Env → Addr → Addr → List ClassMember → M W (List (Bool × PropertyKey)))
    (framed : ∀ proto c, A (.obj proto) → A (.obj c) →
      ReachFrame r R cells A (fun _ => True) (runMembers env proto c members)) :
    ReachFrame r R cells A (SafeVal A) (finishDerived runMembers env name self members parents ctor) := by
  unfold finishDerived
  apply (reachFrame_alloc _ ?_ objects).bind
  · intro proto hp
    have ctorSafe := Env.nodes_setHome safe (some proto) (by simpa)
    apply (reachFrame_alloc _ ?_ objects).bind
    · intro c hc
      apply (reachFrame_constructorLink away proto c hp hc).bind
      intro _ _
      apply (framed proto c hp hc).bind
      intro keys _
      dsimp only
      have done := (reachFrame_initSelf (W := W) away env self name (.obj c) safe (safeVal_obj.2 hc)).bind fun _ _ =>
        reachFrame_pure r R cells A (Val.obj c) (safeVal_obj.2 hc)
      split
      · exact done
      · exact (reachFrame_constructorCode away c hc _ (derivedCtorCode_safe ctorSafe ctor _)).bind fun _ _ => done
    · intro node h
      simp [Obj.nodes, Property.nodes, Val.nodes] at h
      rcases h with rfl | rfl | capture
      · exact guard.1
      · exact hp
      · exact derivedCtorCode_safe ctorSafe ctor [] node (by simpa [derivedCtorCode] using capture)
  · intro node h
    simp [Obj.nodes] at h
    obtain ⟨p, hp, rfl⟩ := h
    exact guard.2 p hp

theorem evalDerived_reachFrame_step {r R cells A n} (away : Away r R A)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (bindings : ∀ a : Addr, a.region ≠ r → A (.cell a))
    (objectRoot : A (.obj objectProto)) (functionRoot : A (.obj functionProto))
    (env : Env) (safe : ∀ node ∈ env.nodes, A node)
    (call : CallFramed r R cells A (callFn n : Call W))
    (eval : ∀ e exp, (∀ node ∈ Env.nodes e, A node) →
      ReachFrame r R cells A (SafeVal A) (evalExpr n e exp : M W Val))
    (evalMemberList : ∀ e proto c ms, (∀ node ∈ Env.nodes e, A node) → A (.obj proto) → A (.obj c) →
      ReachFrame r R cells A (fun _ => True) (evalMembers n e proto c ms : M W _))
    (name : Name) (self : Bool) (base : Expr) (members : List ClassMember) (ctor : Option (List Stmt × Option (List Expr) × Func)) :
    ReachFrame r R cells A (SafeVal A) (evalExpr (n + 1) env (.derived name self base members ctor) : M W Val) := by
  simp only [evalExpr]
  split
  · exact reachFrame_unknown r R cells A _ _
  · apply (reachFrame_selfScope away bindings env self name safe).bind
    intro localEnv hlocal
    apply (eval localEnv base hlocal).bind
    intro proto hp
    apply (reachFrame_classParents away (callFn n) call objectRoot functionRoot proto hp).bind
    intro parents guard
    exact reachFrame_finishDerived away objects localEnv hlocal name self members parents ctor guard (evalMembers n)
      fun a b ha hb => evalMemberList localEnv a b members hlocal ha hb

end CoreJS

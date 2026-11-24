import EsmSplit.CoreJS.BindingFrame
import EsmSplit.CoreJS.HostFrame
import EsmSplit.CoreJS.Request.Property

/-! # Frames for call and constructor dispatch

A nested invocation changes its supplied capture cells. The same closed heap
envelope remains valid when those captures are already accessible. Arguments
and results preserve their reachability through handle publication and lookup.

Fuel-indexed calls reduce to the actual smaller evaluator steps. The call and
constructor lemmas state those steps as separate premises for their caller.
-/

namespace CoreJS

variable {W : World} {α β : Type}

private theorem Access.withCells {cells other A} {s : State W} (h : Access cells A s)
    (roots : ∀ a, some a ∈ other → A (.cell a)) : Access other A s :=
  ⟨h.handed, roots, h.closed⟩

/-- The graph envelope does not change when a nested call substitutes its lexical capabilities. -/
theorem ReachFrame.withCells {r R inside outside A P} {m : M W α} (hf : ReachFrame r R inside A P m)
    (roots : ∀ a, some a ∈ inside → A (.cell a)) : ReachFrame r R outside A P m := by
  constructor
  · intro s H C hs hr
    exact hf.replace s H C (hs.withCells roots) hr
  · intro s hs hr
    exact hf.preserves s (hs.withCells roots) hr
  · intro s hs hr
    have result := hf.accessible s (hs.withCells roots) hr
    cases h : m s <;> rw [h] at result
    · exact ⟨result.1, result.2.withCells hs.supplied⟩
    · exact ⟨result.1, result.2.withCells hs.supplied⟩
    · exact result.withCells hs.supplied
    · exact trivial

theorem ReachFrame.mapM {r R cells A} {P : β → Prop} {f : α → M W β}
    (xs : List α) (frame : ∀ x ∈ xs, ReachFrame r R cells A P (f x)) :
    ReachFrame r R cells A (fun ys => ∀ y ∈ ys, P y) (xs.mapM f) := by
  suffices loop : ∀ acc : List β, (∀ y ∈ acc, P y) →
      ReachFrame r R cells A (fun ys => ∀ y ∈ ys, P y) (List.mapM.loop f xs acc) from
    loop [] (by simp)
  induction xs with
  | nil =>
    intro acc hacc
    simp only [List.mapM.loop]
    exact reachFrame_pure r R cells A acc.reverse (fun b hb => hacc b (List.mem_reverse.mp hb))
  | cons x xs ih =>
    intro acc hacc
    simp only [List.mapM.loop]
    exact (frame x List.mem_cons_self).bind fun y hy =>
      ih (fun a ha => frame a (List.mem_cons_of_mem _ ha)) (y :: acc) fun b hb => by
        rcases List.mem_cons.mp hb with rfl | hb
        · exact hy
        · exact hacc b hb

def CallFramed (r : Nat) (R : Addr → Prop) (cells : List (Option Addr)) (A : HeapNode → Prop)
    (call : Call W) : Prop :=
  ∀ f this args, SafeVal A f → SafeVal A this → (∀ arg ∈ args, SafeVal A arg) →
    ReachFrame r R cells A (SafeVal A) (call f this args)

/-- Invoke a reachable callable with a reachable receiver and arguments. -/
theorem perform_call {r R cells A} (call : Call W)
    (construct : Val → List Val → M W Val) (code) (hf : CallFramed r R cells A call)
    (f this : WVal) (args : List WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.call f this args)) :=
  (reachFrame_fromW r R cells A f).bind fun callee hc =>
    (reachFrame_fromW r R cells A this).bind fun receiver hr =>
      (ReachFrame.mapM args fun w _ => reachFrame_fromW r R cells A w).bind fun vs hv =>
        (hf callee receiver vs hc hr hv).bind fun v safe =>
          (reachFrame_toW r R cells A v safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

/-- Invoke a reachable constructor with reachable arguments. -/
theorem perform_construct {r R cells A} (call : Call W)
    (construct : Val → List Val → M W Val) (code)
    (hf : ∀ f args, SafeVal A f → (∀ arg ∈ args, SafeVal A arg) →
      ReachFrame r R cells A (SafeVal A) (construct f args))
    (f : WVal) (args : List WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.construct f args)) :=
  (reachFrame_fromW r R cells A f).bind fun callee hc =>
    (ReachFrame.mapM args fun w _ => reachFrame_fromW r R cells A w).bind fun vs hv =>
      (hf callee vs hc hv).bind fun v safe =>
        (reachFrame_toW r R cells A v safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

private theorem env_cells_safe {A : HeapNode → Prop} {env} (h : ∀ node ∈ Env.nodes env, A node)
    {names : List Name} {a : Addr} (hmem : some a ∈ names.map (env.vars.lookup ·)) : A (.cell a) := by
  obtain ⟨name, _, hl⟩ := List.mem_map.mp hmem
  exact Env.nodes_lookup h hl

private theorem opaque_cells_safe {A : HeapNode → Prop} {code part cells ctor} (h : ∀ node ∈ Code.nodes (.opaque code part cells ctor), A node)
    {a : Addr} (ha : some a ∈ cells) : A (.cell a) :=
  h _ (List.mem_filterMap.mpr ⟨some a, ha, rfl⟩)

private theorem reachFrame_invokesOpaque {r R cells A n}
    (conversation : ∀ code inner told, (∀ a, some a ∈ inner → A (.cell a)) →
      ReachFrame r R inner A (SafeVal A) (converse n code inner told : M W Val))
    (callee this : Val) (args : List Val) (hc : SafeVal A callee) (ht : SafeVal A this)
    (ha : ∀ a ∈ args, SafeVal A a) (code part : Nat) (inner : List (Option Addr))
    (roots : ∀ a, some a ∈ inner → A (.cell a)) (isNew : Bool) :
    ReachFrame r R cells A (SafeVal A) (do
      converse n code inner (.run code part (← toW callee) (← toW this) (← args.mapM toW) isNew) : M W Val) := by
  apply (reachFrame_toW r R cells A callee hc).bind
  intro f _
  apply (reachFrame_toW r R cells A this ht).bind
  intro receiver _
  apply (ReachFrame.mapM args fun a h => reachFrame_toW r R cells A a (ha a h)).bind
  intro argv _
  exact (conversation code inner (.run code part f receiver argv isNew) roots).withCells roots

private theorem objectCode_safe {A : HeapNode → Prop} {o : Obj}
    (safe : ∀ n ∈ o.nodes, A n) {code : Code} (hc : o.code = some code) :
    ∀ n ∈ code.nodes, A n :=
  fun node hn => safe node (List.mem_append_right _ (List.mem_flatMap.mpr ⟨code, by simp [hc], hn⟩))

theorem callFn_reachFrame_of_steps {r R cells A n} (away : Away r R A)
    (conversation : ∀ code inner told, (∀ a, some a ∈ inner → A (.cell a)) →
      ReachFrame r R inner A (SafeVal A) (converse n code inner told : M W Val))
    (run : ∀ f closed this args, (∀ node ∈ Env.nodes closed, A node) → SafeVal A this →
      (∀ a ∈ args, SafeVal A a) →
      ReachFrame r R cells A (SafeVal A) (do
        let env ← enterFunction f closed this args
        match ← execStmts n env f.body with
        | .ret v => pure v
        | _ => pure .undef : M W Val)) :
    CallFramed r R cells A (callFn (n + 1) : Call W) := by
  intro callee this args hc ht ha
  cases callee
  case host name => exact reachFrame_hostCall r R cells A name this args
  case obj a =>
    simp only [callFn]
    apply (reachFrame_getObj away (safeVal_obj.1 hc)).bind
    intro obj safe
    cases kind : obj.code with
    | none => exact reachFrame_typeError "not a function"
    | some code =>
      have caps := objectCode_safe safe kind
      cases code with
      | derived | derivedFields | derivedBody => exact reachFrame_typeError "class constructor cannot be invoked without 'new'"
      | host name => exact reachFrame_hostCall r R cells A name this args
      | «opaque» code part inner _ =>
        exact reachFrame_invokesOpaque conversation _ _ _ hc ht ha code part inner
          (fun a h => opaque_cells_safe caps h) false
      | closure f closed fields =>
        simp only []
        split
        · exact reachFrame_typeError "class constructor cannot be invoked without 'new'"
        · cases h : f.opaqueBody with
          | none => exact run f closed this args caps ht ha
          | some body =>
            rcases body with ⟨code, part, free, refusal⟩
            cases refusal with
            | some reason => exact reachFrame_unknown r R cells A _ _
            | none =>
              have safeThis : SafeVal A (if f.kind == .arrow then closed.this else this) := by
                split
                · exact Env.nodes_this caps
                · exact ht
              exact reachFrame_invokesOpaque conversation _ _ _ hc safeThis ha code part _
                (fun _ mem => env_cells_safe caps mem) false
  all_goals exact reachFrame_typeError "not a function"


theorem reachFrame_isConstructor {r R cells A} (away : Away r R A) (v : Val)
    (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (isConstructor v : M W Bool) := by
  have yes := reachFrame_pure (W := W) r R cells A (P := fun _ => True) true trivial
  have no := reachFrame_pure (W := W) r R cells A (P := fun _ => True) false trivial
  cases v <;> simp only [isConstructor] <;> try exact no
  case host name => exact reachFrame_unknown r R cells A _ _
  case obj a =>
    apply (reachFrame_getObj away (safeVal_obj.1 hv)).bind
    intro o _
    cases h : o.code with
    | none => exact no
    | some code =>
      cases code with
      | derived | derivedFields | derivedBody => exact yes
      | closure f env fields => exact reachFrame_pure r R cells A _ trivial
      | host name =>
        simp only []
        split
        · exact no
        · exact reachFrame_unknown r R cells A _ _
      | «opaque» code part cells ctor => exact reachFrame_pure r R _ A ctor trivial

private theorem objectProto_safe {A : HeapNode → Prop} {o : Obj}
    (safe : ∀ n ∈ o.nodes, A n) {parent : Addr} (hp : o.proto = some parent) :
    SafeVal A (.obj parent) :=
  safeVal_obj.2 (safe (.obj parent) (by simp [Obj.nodes, hp]))

private theorem reachFrame_constructorReceiver {r R cells A}
    (root : A (.obj objectProto))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (proto : Val) (safe : SafeVal A proto) :
    ReachFrame r R cells A (fun a => A (.obj a)) (constructorReceiver proto : M W Addr) := by
  cases proto
  case error kind _ => cases kind <;> simp only [constructorReceiver] <;>
    first
    | exact reachFrame_unknown r R cells A _ _
    | apply allocate; simpa [Obj.nodes] using root
  all_goals simp only [constructorReceiver] <;>
    first
    | exact reachFrame_unknown r R cells A _ _
    | apply allocate; simpa [Obj.nodes] using root
    | apply allocate; simpa [Obj.nodes] using safeVal_obj.1 safe

private theorem reachFrame_constructNativeError {r R cells A}
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (target : Val) (args : List Val) :
    ReachFrame r R cells A (SafeVal A) (constructNativeError target args : M W Val) := by
  unfold constructNativeError
  split
  · exact reachFrame_unknown r R cells A _ _
  · apply (reachFrame_lift _ (P := fun _ => True) (fun _ _ => trivial)).bind
    intro message _
    apply (reachFrame_readHostRef (W := W) (r := r) (R := R) (cells := cells) (A := A) .nativeErrorConstructor).bind
    intro ctor _
    split
    · apply (allocate _ (by simp [Obj.nodes])).bind
      intro identity _
      exact reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])
    · exact reachFrame_unknown r R cells A _ _

private theorem reachFrame_initDerivedFields {r R cells A n}
    (run : ∀ env a fs, (∀ node ∈ Env.nodes env, A node) → A (.obj a) →
      ReachFrame r R cells A (fun _ => True) (evalFields n env a fs : M W Unit))
    (closed : Env) (safe : ∀ node ∈ closed.nodes, A node) (receiver : Val) (hr : SafeVal A receiver)
    (fields : List (PropertyKey × Expr)) :
    ReachFrame r R cells A (fun _ => True) (initDerivedFields (evalFields n) closed receiver fields : M W Unit) := by
  unfold initDerivedFields
  split
  · exact reachFrame_pure r R cells A () trivial
  · cases receiver <;> try exact reachFrame_unknown r R cells A _ _
    exact run _ _ _ (Env.nodes_setThis safe hr) (safeVal_obj.1 hr)

/-- Constructor dispatch preserves reachability through allocation, argument evaluation and smaller calls. -/
theorem constructTarget_reachFrame_of_steps {r R cells A n} (away : Away r R A)
    (iteratorRoot : A (.obj arrayIteratorProto)) (root : A (.obj objectProto))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (getter : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (callFn n f base [] : M W Val))
    (conversation : ∀ code inner told, (∀ a, some a ∈ inner → A (.cell a)) →
      ReachFrame r R inner A (SafeVal A) (converse n code inner told : M W Val))
    (fields : ∀ env a fs, (∀ n ∈ Env.nodes env, A n) → A (.obj a) →
      ReachFrame r R cells A (fun _ => True) (evalFields n env a fs : M W Unit))
    (enter : ∀ f closed this args, (∀ node ∈ Env.nodes closed, A node) → SafeVal A this →
      (∀ a ∈ args, SafeVal A a) →
      ReachFrame r R cells A (fun env => ∀ node ∈ env.nodes, A node) (enterFunction f closed this args : M W Env))
    (stmts : ∀ env ss, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (fun flow => ∀ v, flow = .ret v → SafeVal A v) (execStmts n env ss : M W Flow))
    (evalArgsFrame : ∀ env es, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (evalArgs n env es : M W (List Val)))
    (smaller : ∀ target callee args, SafeVal A target → SafeVal A callee → (∀ a ∈ args, SafeVal A a) →
      ReachFrame r R cells A (SafeVal A) (constructTarget n target callee args : M W Val))
    (target callee : Val) (args : List Val) (safeTarget : SafeVal A target)
    (safeCallee : SafeVal A callee) (safeArgs : ∀ a ∈ args, SafeVal A a) :
    ReachFrame r R cells A (SafeVal A) (constructTarget (n + 1) target callee args : M W Val) := by
  cases callee
  case host name =>
    by_cases native : name = "window.Error"
    · subst name
      exact reachFrame_constructNativeError allocate target args
    · simpa [constructTarget, native] using
        (reachFrame_unknown (W := W) r R cells A (SafeVal A) s!"host value {name} is constructed")
  case obj a =>
    simp only [constructTarget]
    apply (reachFrame_getObj away (safeVal_obj.1 safeCallee)).bind
    intro o safe
    cases hc : o.code with
    | none => exact reachFrame_typeError "not a constructor"
    | some code =>
      have caps := objectCode_safe safe hc
      have parentRun (original : Obj) (safeObj : ∀ node ∈ original.nodes, A node)
          (vs : List Val) (hvs : ∀ v ∈ vs, SafeVal A v) :
          ReachFrame r R cells A (SafeVal A) (constructParent (constructTarget n target) original.proto vs : M W Val) := by
        unfold constructParent
        cases hp : original.proto with
        | none => exact reachFrame_typeError "superclass is not a constructor"
        | some p =>
          have parent := objectProto_safe safeObj hp
          apply (reachFrame_isConstructor away (.obj p) parent).bind
          intro test _
          cases test
          · exact reachFrame_typeError "superclass is not a constructor"
          · exact smaller target (.obj p) vs safeTarget parent hvs
      have active := reachFrame_getObj (W := W) (cells := cells) away (safeVal_obj.1 safeCallee)
      cases code with
      | host name => exact reachFrame_unknown r R cells A _ _
      | derived => exact active.bind fun original safeObj => parentRun original safeObj args safeArgs
      | derivedFields closed fs =>
        apply active.bind
        intro original safeObj
        apply (parentRun original safeObj args safeArgs).bind
        intro receiver hr
        exact (reachFrame_initDerivedFields fields closed caps receiver hr fs).bind fun _ _ =>
          reachFrame_pure r R cells A receiver hr
      | derivedBody before superArgs body closed fs =>
        let captured := capturesDerivedReceiver before superArgs
        apply (enter (derivedScope before body captured) closed .undef args caps (fun _ h => by cases h) safeArgs).bind
        intro env he
        apply (stmts env before he).bind
        intro initialFlow _
        cases initialFlow <;> try exact reachFrame_unknown r R cells A _ _
        case normal =>
          apply active.bind
          intro original safeObj
          have argvFrame : ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v)
              (superArgs.elim (forwardFreshArguments args) (evalArgs n env) : M W (List Val)) := by
            cases superArgs with
            | some es => exact evalArgsFrame env es he
            | none =>
              apply (reachFrame_getObj away iteratorRoot).bind
              intro proto _
              split <;> first | exact reachFrame_pure r R cells A args safeArgs | exact reachFrame_unknown r R cells A _ _
          apply argvFrame.bind
          intro argv hargv
          apply active.bind
          intro current _
          split
          · exact reachFrame_unknown r R cells A _ _
          · apply (parentRun original safeObj argv hargv).bind
            intro receiver hr
            have initializeReceiver : ReachFrame r R cells A (fun _ => True)
                (initCapturedReceiver env receiver captured : M W Unit) := by
              unfold initCapturedReceiver
              split
              · exact reachFrame_initVar away env derivedReceiverName receiver he hr
              · exact reachFrame_pure r R cells A () trivial
            apply initializeReceiver.bind
            intro _ _
            apply (reachFrame_initDerivedFields fields closed caps receiver hr fs).bind
            intro _ _
            apply (stmts { env with this := receiver } body.body (Env.nodes_setThis he hr)).bind
            intro flow hflow
            cases flow
            case ret v =>
              have hv := hflow v rfl
              cases v <;> first
              | exact reachFrame_pure r R cells A _ hv
              | exact reachFrame_pure r R cells A _ hr
              | exact reachFrame_typeError _
              | exact reachFrame_unknown r R cells A _ _
            all_goals first
            | exact reachFrame_pure r R cells A _ hr
            | exact reachFrame_unknown r R cells A _ _
      | «opaque» code part inner ctor =>
        cases ctor
        · exact reachFrame_typeError "not a constructor"
        · simp only []
          apply (reachFrame_getProp away (callFn n) getter root target (.str "prototype") safeTarget).bind
          intro proto hp
          apply (reachFrame_constructorReceiver root allocate proto hp).bind
          intro receiver hra
          have hr := safeVal_obj.2 hra
          apply (reachFrame_toW r R cells A (.obj a) safeCallee).bind
          intro f _
          apply (reachFrame_toW r R cells A (.obj receiver) hr).bind
          intro this _
          apply (ReachFrame.mapM args fun a h => reachFrame_toW r R cells A a (safeArgs a h)).bind
          intro argv _
          have roots (b : Addr) (hb : some b ∈ inner) : A (.cell b) := opaque_cells_safe caps hb
          apply ((conversation code inner (.run code part f this argv true) roots).withCells roots).bind
          intro result hs
          cases result <;> first
          | exact reachFrame_pure r R cells A _ hs
          | exact reachFrame_pure r R cells A _ hr
      | closure f closed fs =>
        simp only []
        split
        · exact reachFrame_typeError "not a constructor"
        · apply (reachFrame_getProp away (callFn n) getter root target (.str "prototype") safeTarget).bind
          intro proto hp
          apply (reachFrame_constructorReceiver root allocate proto hp).bind
          intro receiver hra
          have hr := safeVal_obj.2 hra
          have fieldEnv := Env.nodes_setThis caps hr
          apply (fields { closed with this := .obj receiver } receiver fs fieldEnv hra).bind
          intro _ _
          let finishValue : Val → M W Val
            | .obj r => pure (.obj r)
            | .error kind msg => pure (.error kind msg)
            | .host name => unknown s!"host value {name} is inspected"
            | _ => pure (.obj receiver)
          have finish (result : Val) (hs : SafeVal A result) :
              ReachFrame r R cells A (SafeVal A) (finishValue result) := by
            cases result
            case host name => exact reachFrame_unknown r R cells A _ _
            all_goals first
            | exact reachFrame_pure r R cells A _ hs
            | exact reachFrame_pure r R cells A _ hr
          cases h : f.opaqueBody with
          | none =>
            simp only []
            apply (enter f closed (.obj receiver) args caps hr safeArgs).bind
            intro env he
            apply (stmts env f.body he).bind
            intro flow hflow
            cases flow
            case ret v => exact finish v (hflow v rfl)
            all_goals exact finish Val.undef (fun _ hn => by cases hn)
          | some data =>
            rcases data with ⟨code, part, free, refusal⟩
            cases refusal with
            | some reason => exact reachFrame_unknown r R cells A _ _
            | none =>
              simp only []
              apply (reachFrame_toW r R cells A (.obj a) safeCallee).bind
              intro f _
              apply (reachFrame_toW r R cells A (.obj receiver) hr).bind
              intro this _
              apply (ReachFrame.mapM args fun a h => reachFrame_toW r R cells A a (safeArgs a h)).bind
              intro argv _
              let inner := free.map (closed.vars.lookup ·)
              have roots (b : Addr) (hb : some b ∈ inner) : A (.cell b) := env_cells_safe caps hb
              exact ((conversation code inner (.run code part f this argv true) roots).withCells roots).bind finish
  all_goals exact reachFrame_typeError "not a constructor"

theorem CallFramed.getter {r R cells A} {call : Call W} (hf : CallFramed r R cells A call)
    (f base : Val) (safe : SafeVal A f) (recv : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (call f base []) :=
  hf f base [] safe recv (by simp)

theorem CallFramed.setter {r R cells A} {call : Call W} (hf : CallFramed r R cells A call)
    (f base value : Val) (sf : SafeVal A f) (sb : SafeVal A base) (sv : SafeVal A value) :
    ReachFrame r R cells A (SafeVal A) (call f base [value]) :=
  hf f base [value] sf sb fun _ h => List.mem_singleton.mp h ▸ sv

end CoreJS

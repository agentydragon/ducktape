import Experiments.CoreJS.RequestFrame

namespace CoreJS
namespace ReachabilityExamples

private def external : Addr := ⟨0, 0⟩
private def protectedAddr : Addr := ⟨1, 0⟩

private def objectState (o : Obj) : State readOnceWorld where
  heap := #[#[o], #[]]
  cells := #[#[], #[]]
  region := 0
  out := #[]
  world := { state := false, handed := #[external] }

private theorem fromHanded (o : Obj) (node : HeapNode) (h : node ∈ o.nodes) :
    Reachable (objectState o) [] node :=
  .edge (.handed (a := external) (by simp [objectState])) (.obj rfl h)

private def prototypeObject : Obj := { proto := some protectedAddr, props := [] }
private def accessorObject : Obj :=
  { proto := none, props := [("get", .accessor (.obj protectedAddr) .undef true true)] }
private def capturedObject : Obj :=
  { proto := none, props := [],
    code := some (.closure (.mk "callback" .arrow false [] []) { vars := [("x", protectedAddr)], this := .undef } []) }
private def capturedThisObject : Obj :=
  { proto := none, props := [],
    code := some (.closure (.mk "callback" .arrow false [] []) { vars := [], this := .obj protectedAddr } []) }
private def opaqueObject : Obj := { proto := none, props := [], code := some (.opaque 0 1 [some protectedAddr]) }

/-- Prototype edges count even if no property has yet been read. -/
theorem prototype_reachable : Reachable (objectState prototypeObject) [] (.obj protectedAddr) :=
  fromHanded _ _ (by simp [prototypeObject, Obj.nodes])

/-- Accessor functions are reachable pointers, not data values that may be ignored. -/
theorem accessor_reachable : Reachable (objectState accessorObject) [] (.obj protectedAddr) :=
  fromHanded _ _ (by simp [accessorObject, Obj.nodes, Property.nodes, Val.nodes])

theorem closure_cell_reachable : Reachable (objectState capturedObject) [] (.cell protectedAddr) :=
  fromHanded _ _ (by simp [capturedObject, Obj.nodes, Code.nodes, Env.nodes, Val.nodes])

theorem closure_this_reachable : Reachable (objectState capturedThisObject) [] (.obj protectedAddr) :=
  fromHanded _ _ (by simp [capturedThisObject, Obj.nodes, Code.nodes, Env.nodes, Val.nodes])

theorem opaque_cell_reachable : Reachable (objectState opaqueObject) [] (.cell protectedAddr) :=
  fromHanded _ _ (by simp [opaqueObject, Obj.nodes, Code.nodes])

/-- Hidden closure capture prevents separation even though the request's supplied-cell list is empty. -/
theorem hidden_capture_not_away : ¬ Away 1 (fun _ => False) (Reachable (objectState capturedObject) []) := by
  intro h
  exact h.region _ closure_cell_reachable rfl

private def bindingState (value : Option Val) (mutable : Bool) : State readOnceWorld where
  heap := #[#[], #[]]
  cells := #[#[{ value, mutable }], #[]]
  region := 0
  out := #[]
  world := { state := false, handed := #[] }

private def onlyBinding (node : HeapNode) : Prop := node = .cell external

private theorem bindingAccess : Access [some external] onlyBinding (bindingState (some (.bool false)) true) := by
  constructor
  · intro a ha; simp [bindingState] at ha
  · intro a ha
    simp only [List.mem_singleton, Option.some.injEq] at ha
    subst a; rfl
  · intro node target hn he
    change node = .cell external at hn
    subst node
    cases he with
    | cell hb hm =>
      have hb' : ({ value := some (.bool false), mutable := true } : Binding) = _ := Option.some.inj hb
      subst hb'
      simp [Binding.nodes, Val.nodes] at hm

private theorem bindingAway : Away 1 (fun a => a.region = 1) onlyBinding := by
  constructor
  · intro node h; change node = .cell external at h; subst node; decide
  · intro a h; change HeapNode.cell a = .cell external at h; cases h; decide

/-- Object and binding nodes with the same numeric address are distinct; supplying a binding grants no object. -/
theorem same_address_not_object : ¬ Reachable (bindingState (some (.bool false)) true) [some external] (.obj external) := by
  intro h
  have ha := bindingAccess.reachable h
  cases ha

/-- Region separation alone is insufficient: a supplied binding may be one the initializer reads. -/
theorem initializer_read_not_away :
    ¬ Away 1 (fun a => a = external) (Reachable (bindingState none true) [some external]) := by
  intro h
  exact h.reads external (.supplied (by simp)) rfl

/-- This concrete separated state is covered by the actual request theorem, including mutation and returned handles. -/
theorem separated_request (req : Request) (hreq : req.bindingOrKeys = true) :
    (perform (callFn 0) (construct 0) 0 [some external] req (bindingState (some (.bool false)) true)).Accessible
      [some external] onlyBinding (fun _ => True) :=
  (perform_bindingOrKeys bindingAway (callFn 0) (construct 0) 0 req hreq).accessible _ bindingAccess (by decide)

theorem mutable_write :
    perform (callFn 0) (construct 0) 0 [some external] (.writeVar 0 (.prim (.bool true)))
      (bindingState (some (.bool false)) true) =
      .ok (.value (.prim .undef)) (bindingState (some (.bool true)) true) := rfl

theorem immutable_write :
    perform (callFn 0) (construct 0) 0 [some external] (.writeVar 0 (.prim (.bool true)))
      (bindingState (some (.bool false)) false) =
      .throw (.error .type "assignment to constant variable") (bindingState (some (.bool false)) false) := rfl

theorem initialize_tdz :
    perform (callFn 0) (construct 0) 0 [some external] (.initVar 0 (.prim (.bool true)))
      (bindingState none false) = .ok (.value (.prim .undef)) (bindingState (some (.bool true)) false) := rfl

theorem tdz_read_caught :
    attempt (perform (callFn 0) (construct 0) 0 [some external] (.readVar 0)) (bindingState none false) =
      .ok (.error (.error .reference "cannot access a binding before initialization")) (bindingState none false) := rfl

theorem forged_handle :
    perform (callFn 0) (construct 0) 0 [some external] (.initVar 0 (.handle 0)) (bindingState none true) =
      .unknown "the world names an object it was not handed" (bindingState none true) := rfl

theorem missing_binding :
    perform (callFn 0) (construct 0) 0 [some external] (.readVar 1) (bindingState none true) =
      .unknown "the world names a binding the code was not handed" (bindingState none true) := rfl

private def keysObject : Obj :=
  { proto := none, props := [("z", .data .undef true true true),
      ("accessor", .accessor (.host "must not run") .undef true true), ("a", .data .null true true true)] }

/-- Own-key enumeration returns keys in order without executing a getter. -/
theorem keys_without_callback :
    perform (callFn 0) (construct 0) 0 [] (.keys (.handle 0)) (objectState keysObject) =
      .ok (.keys ["z", "accessor", "a"]) (objectState keysObject) := rfl

private def aliasState (initialized : Bool) : State readOnceWorld where
  heap := #[#[{ proto := none, props := [] }], #[]]
  cells := #[#[{ value := some (.obj external), mutable := false },
    { value := if initialized then some (.obj external) else none, mutable := false }], #[]]
  region := 0
  out := #[]
  world := { state := false, handed := if initialized then #[external] else #[] }

/-- A reachable object is still not a valid world handle until the interpreter has actually handed it out. -/
theorem forged_existing_object :
    perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.initVar 1 (.handle 0)) (aliasState false) =
      .unknown "the world names an object it was not handed" (aliasState false) := rfl

private def outside (node : HeapNode) : Prop := node.addr.region = 0

private theorem aliasAccess : Access [some external, some ⟨0, 1⟩] outside (aliasState false) := by
  constructor
  · intro a ha; simp [aliasState] at ha
  · intro a ha
    simp only [List.mem_cons, List.not_mem_nil, or_false, Option.some.injEq] at ha
    rcases ha with ha | ha <;> subst a <;> rfl
  · intro node target hn he
    cases he with
    | @obj a o target ho hm =>
      rcases a with ⟨region, index⟩
      change region = 0 at hn
      subst region
      cases index with
      | zero =>
        have heq : ({ proto := none, props := [] } : Obj) = o := Option.some.inj ho
        subst o
        simp [Obj.nodes] at hm
      | succ index => simp [State.objAt, aliasState] at ho
    | @cell a b target hb hm =>
      rcases a with ⟨region, index⟩
      change region = 0 at hn
      subst region
      cases index with
      | zero =>
        have heq : ({ value := some (.obj external), mutable := false } : Binding) = b := Option.some.inj hb
        subst b
        simp only [Binding.nodes, Val.nodes, Option.toList_some, List.flatMap_cons, List.flatMap_nil,
          List.append_nil, List.mem_singleton] at hm
        subst target
        rfl
      | succ index =>
        cases index with
        | zero =>
          have heq : ({ value := none, mutable := false } : Binding) = b := Option.some.inj hb
          subst b
          simp [Binding.nodes] at hm
        | succ index => simp [State.cellAt, aliasState] at hb

private theorem outsideAway : Away 1 (fun a => a.region = 1) outside := by
  constructor
  · intro node hn; change node.addr.region = 0 at hn; rw [hn]; decide
  · intro a hn; change a.region = 0 at hn; rw [hn]; decide

/-- The alias-publication example starts in a separated closed envelope and retains it after all three requests. -/
theorem publish_alias_accessible :
    ((do
      let _ ← perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.readVar 0)
      let _ ← perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.initVar 1 (.handle 0))
      perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.readVar 1)) (aliasState false)).Accessible
        [some external, some ⟨0, 1⟩] outside (fun _ => True) :=
  (publish_alias_frame outsideAway (callFn 0) (construct 0) 0 0 1 0).accessible _ aliasAccess (by decide)

/-- The composed actual requests publish one handle, store its alias, and reuse that handle on the final read. -/
theorem publish_alias :
    (do
      let _ ← perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.readVar 0)
      let _ ← perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.initVar 1 (.handle 0))
      perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.readVar 1)) (aliasState false) =
        .ok (.value (.handle 0)) (aliasState true) := by
  let handed : State readOnceWorld := { aliasState false with world := { state := false, handed := #[external] } }
  have hread : perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.readVar 0) (aliasState false) =
      .ok (.value (.handle 0)) handed := by
    change (toW (.obj external) >>= fun w => pure (Told.value w)) (aliasState false) = _
    rw [M.bind_apply]
    simp only [toW, aliasState, Bool.false_eq_true, ↓reduceIte, Array.findIdx?_empty]
    rfl
  have hinit : perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.initVar 1 (.handle 0)) handed =
      .ok (.value (.prim .undef)) (aliasState true) := rfl
  have halias : perform (callFn 0) (construct 0) 0 [some external, some ⟨0, 1⟩] (.readVar 1) (aliasState true) =
      .ok (.value (.handle 0)) (aliasState true) := by
    change (toW (.obj external) >>= fun w => pure (Told.value w)) (aliasState true) = _
    rw [M.bind_apply]
    simp only [toW, aliasState, ↓reduceIte, Array.findIdx?_singleton, beq_self_eq_true]
    rfl
  rw [bind_of_ok hread, bind_of_ok hinit]
  exact halias

end ReachabilityExamples
end CoreJS

import EsmSplit.CoreJS.HostFrame

/-!
# Reachability of property requests

The lookup budget counts every object in the heap. Replacing a protected
region can change that count, even when the receiver cannot reach the region.
A successful search has a first hit along a path of distinct objects, so
the object count of either heap is enough for the same path.
Property writes preserve the envelope when both the target and inserted
values are accessible. Property deletion only removes existing edges.
-/

namespace CoreJS

variable {W : World}

private inductive PropertyPath (s : State W) (key : String) (p : Property) : Addr → List Addr → Prop
  | own {a o} : s.objAt a = some o → o.props.lookup key = some p → PropertyPath s key p a [a]
  | inherited {a o parent rest} : s.objAt a = some o → o.props.lookup key = none →
      o.proto = some parent → PropertyPath s key p parent rest → PropertyPath s key p a (a :: rest)

private theorem findProp_path {s : State W} {key p} {budget : Nat} {a : Addr}
    (h : findProp s budget a key = some p) :
    ∃ path, PropertyPath s key p a path ∧ path.length ≤ budget := by
  induction budget generalizing a with
  | zero => simp [findProp] at h
  | succ n ih =>
    cases ho : s.objAt a with
    | none => simp [findProp, ho] at h
    | some o =>
      cases hk : o.props.lookup key with
      | some q =>
        simp only [findProp, ho, hk, Option.some.injEq] at h
        subst q
        exact ⟨[a], .own ho hk, by simp⟩
      | none =>
        cases hp : o.proto with
        | none => simp [findProp, ho, hk, hp] at h
        | some parent =>
          have ht : findProp s n parent key = some p := by simpa [findProp, ho, hk, hp] using h
          obtain ⟨rest, ht, hn⟩ := ih ht
          exact ⟨a :: rest, .inherited ho hk hp ht, by simpa using Nat.succ_le_succ hn⟩

private theorem PropertyPath.found {s : State W} {key p a path} (hp : PropertyPath s key p a path)
    {budget : Nat} (hbudget : path.length ≤ budget) : findProp s budget a key = some p := by
  induction hp generalizing budget with
  | own ho hk =>
    cases budget with
    | zero => simp at hbudget
    | succ n => simp [findProp, ho, hk]
  | inherited ho hk hproto _ ih =>
    cases budget with
    | zero => simp at hbudget
    | succ n =>
      simp only [findProp, ho, hk, hproto, Option.bind_some]
      exact ih (by simpa using hbudget)

private theorem PropertyPath.beforeHit {s : State W} {key p a path} (hp : PropertyPath s key p a path)
    {budget : Nat} (hbudget : budget < path.length) : findProp s budget a key = none := by
  induction hp generalizing budget with
  | own ho hk =>
    cases budget with
    | zero => rfl
    | succ n => simp at hbudget
  | inherited ho hk hproto _ ih =>
    cases budget with
    | zero => rfl
    | succ n =>
      simp only [findProp, ho, hk, hproto, Option.bind_some]
      exact ih (by simpa using hbudget)

private theorem PropertyPath.suffix {s : State W} {key p a path} (hp : PropertyPath s key p a path)
    {b : Addr} (hb : b ∈ path) :
    ∃ rest, PropertyPath s key p b rest ∧ rest.length ≤ path.length := by
  induction hp with
  | @own addr o ho hk =>
    simp only [List.mem_singleton] at hb
    subst b
    exact ⟨[addr], .own ho hk, Nat.le_refl _⟩
  | @inherited addr o parent rest ho hk hproto ht ih =>
    rcases List.mem_cons.mp hb with rfl | hb
    · exact ⟨b :: rest, .inherited ho hk hproto ht, Nat.le_refl _⟩
    · obtain ⟨suffix, hp, hlen⟩ := ih hb
      exact ⟨suffix, hp, Nat.le_trans hlen (Nat.le_succ _)⟩

private theorem PropertyPath.nodup {s : State W} {key p a path}
    (hp : PropertyPath s key p a path) : path.Nodup := by
  induction hp with
  | own => simp
  | @inherited addr o parent rest ho hk hproto ht ih =>
    refine List.nodup_cons.mpr ⟨fun h => ?_, ih⟩
    obtain ⟨suffix, hf, hlen⟩ := ht.suffix h
    have never := (PropertyPath.inherited ho hk hproto ht).beforeHit
      (budget := suffix.length) (by simpa using Nat.lt_succ_of_le hlen)
    rw [hf.found (Nat.le_refl _)] at never
    contradiction

private theorem PropertyPath.existsObject {s : State W} {key p a path}
    (hp : PropertyPath s key p a path) : ∀ b ∈ path, ∃ o, s.objAt b = some o := by
  induction hp with
  | own ho _ => simpa using (show ∃ o, s.objAt _ = some o from ⟨_, ho⟩)
  | inherited ho _ _ _ ih =>
    intro b hb
    rcases List.mem_cons.mp hb with rfl | hb
    · exact ⟨_, ho⟩
    · exact ih b hb

private def objectAddresses : List (Array Obj) → Nat → List Addr
  | [], _ => []
  | region :: rest, i => (List.range region.size).map (Addr.mk i) ++ objectAddresses rest (i + 1)

private theorem objectAddresses_length (regions : List (Array Obj)) (base acc : Nat) :
    (objectAddresses regions base).length + acc = regions.foldl (fun n objects => n + objects.size) acc := by
  induction regions generalizing base acc with
  | nil => simp [objectAddresses]
  | cons region rest ih =>
    simp only [objectAddresses, List.length_append, List.length_map, List.length_range, List.foldl_cons]
    rw [← ih (base + 1) (acc + region.size)]
    omega

private theorem objectAddresses_mem {regions : List (Array Obj)} (base region index : Nat) {o : Obj}
    (h : regions[region]?.bind (·[index]?) = some o) :
    ⟨base + region, index⟩ ∈ objectAddresses regions base := by
  induction regions generalizing base region with
  | nil => simp at h
  | cons objects rest ih =>
    cases region with
    | zero =>
      simp only [List.getElem?_cons_zero, Option.bind_some] at h
      have hi := (Array.getElem?_eq_some_iff.mp h).1
      apply List.mem_append_left
      simp [List.mem_map, hi]
    | succ r =>
      simp only [List.getElem?_cons_succ] at h
      exact List.mem_append_right _
        (by simpa [Nat.add_assoc, Nat.add_comm 1 r] using ih (base + 1) r h)

private theorem PropertyPath.withinCount {s : State W} {key p a path}
    (hp : PropertyPath s key p a path) : path.length ≤ s.objectCount := by
  have bound := hp.nodup.length_le_of_subset (l₂ := objectAddresses s.heap.toList 0) (by
    intro b hb
    obtain ⟨_, h⟩ := hp.existsObject b hb
    unfold State.objAt at h
    simpa using objectAddresses_mem (regions := s.heap.toList) 0 b.region b.index (by simpa using h))
  rw [show (objectAddresses s.heap.toList 0).length = s.objectCount by
    simpa [State.objectCount, Array.foldl_toList] using objectAddresses_length s.heap.toList 0 0] at bound
  exact bound

private theorem Access.prototype {cells A} {s : State W} (hs : Access cells A s)
    {a o parent} (ha : A (.obj a)) (ho : s.objAt a = some o) (hp : o.proto = some parent) :
    A (.obj parent) :=
  hs.closed _ _ ha (.obj ho (by simp [Obj.nodes, hp]))

private theorem PropertyPath.transfer {cells A} {s t : State W} (hs : Access cells A s)
    (same : ∀ a, A (.obj a) → s.objAt a = t.objAt a)
    {key p a path} (hp : PropertyPath s key p a path) (ha : A (.obj a)) :
    PropertyPath t key p a path := by
  induction hp with
  | own ho hk => exact .own ((same _ ha).symm.trans ho) hk
  | inherited ho hk hproto _ ih =>
    exact .inherited ((same _ ha).symm.trans ho) hk hproto (ih (hs.prototype ha ho hproto))

private theorem PropertyPath.safe {cells A} {s : State W} (hs : Access cells A s)
    {key p a path} (hp : PropertyPath s key p a path) (ha : A (.obj a)) :
    ∀ node ∈ p.nodes, A node := by
  induction hp with
  | own ho hk =>
    intro node hn
    apply hs.closed _ _ ha
    apply Edge.obj ho
    apply List.mem_append_left
    apply List.mem_append_right
    exact List.mem_flatMap.mpr ⟨(key, p), lookup_mem hk, hn⟩
  | inherited ho _ hproto _ ih => exact ih (hs.prototype ha ho hproto)

private theorem findProp_same {cells A} {s t : State W} (hs : Access cells A s) (ht : Access cells A t)
    (same : ∀ a, A (.obj a) → s.objAt a = t.objAt a) {a key} (ha : A (.obj a)) :
    findProp s s.objectCount a key = findProp t t.objectCount a key := by
  cases h : findProp s s.objectCount a key with
  | some p =>
    obtain ⟨_, path, _⟩ := findProp_path h
    have target := path.transfer hs same ha
    exact (target.found target.withinCount).symm
  | none =>
    cases h' : findProp t t.objectCount a key with
    | none => rfl
    | some p =>
      obtain ⟨_, path, _⟩ := findProp_path h'
      have target := path.transfer ht (fun addr accessible => (same addr accessible).symm) ha
      have present := target.found target.withinCount
      rw [h] at present
      contradiction

/-- Prototype lookup stays within the capability envelope, including when a
replacement changes the total heap size used as the walk's budget. -/
theorem reachFrame_lookupProp {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a)) (key : String) :
    ReachFrame r R cells A (fun result => ∀ p, result = some p → ∀ node ∈ p.nodes, A node)
      (lookupProp a key : M W (Option Property)) := by
  constructor
  · intro s H C hs _
    have same (b : Addr) (hb : A (.obj b)) : s.objAt b = (s.put r H C).objAt b := by
      have excluded : b.region ≠ r := away.region _ hb
      rw [State.objAt_put, ite_eq_right excluded]
    let t := s.put r H C
    change Res.ok (findProp t t.objectCount a key) t = .ok (findProp s s.objectCount a key) t
    rw [findProp_same hs (hs.put away H C) same ha]
  · intro s _ _
    exact Preserves.refl r R s
  · intro s hs _
    refine ⟨fun p hp => ?_, hs⟩
    obtain ⟨_, path, _⟩ := findProp_path hp
    exact path.safe hs ha

private theorem reachFrame_throwErr {α : Type} (r R cells A) (P : α → Prop) (kind : ErrorKind) (message : String) :
    ReachFrame r R cells A P (throwErr kind message : M W α) :=
  ⟨fun _ _ _ _ _ => rfl, fun s _ _ => Preserves.refl r R s,
    fun _ hs _ => ⟨by simp [SafeVal, Val.nodes], hs⟩⟩

private theorem reachFrame_toKey (r R cells A) (v : Val) :
    ReachFrame r R cells A (fun _ => True) (toKey v : M W String) := by
  unfold toKey
  cases h : toStr v with
  | ok key => exact reachFrame_pure r R cells A key trivial
  | error abort =>
    cases abort with
    | error kind why => exact reachFrame_throwErr r R cells A _ kind why
    | unknown why => exact reachFrame_unknown r R cells A _ why

private theorem reachFrame_hasProp {r R cells A} (away : Away r R A) (key base : Val) (hb : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (hasProp key base : M W Val) := by
  cases base <;> simp only [hasProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      (reachFrame_lookupProp away (safeVal_obj.1 hb) k).bind fun found _ =>
        reachFrame_pure r R cells A (P := SafeVal A) (Val.bool found.isSome) (by simp [SafeVal, Val.nodes])

/-- Test an inherited or own property without exposing the protected region. -/
theorem perform_has {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat) (key base : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.has key base)) :=
  (reachFrame_fromW r R cells A key).bind fun k _ =>
    (reachFrame_fromW r R cells A base).bind fun o ho =>
      (reachFrame_hasProp away k o ho).bind fun result safe =>
        (reachFrame_toW r R cells A result safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

/-- A property read either returns a reachable data value or invokes a reachable getter. -/
private theorem reachFrame_getFrom {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    {a} (ha : A (.obj a)) (base : Val) (hb : SafeVal A base) (key : String) :
    ReachFrame r R cells A (SafeVal A) (getFrom call a base key) := by
  unfold getFrom
  apply (reachFrame_lookupProp away ha key).bind
  intro result safe
  cases result with
  | none => exact reachFrame_pure r R cells A (P := SafeVal A) Val.undef (by simp [SafeVal, Val.nodes])
  | some prop =>
    have hp := safe prop rfl
    cases prop with
    | data v => exact reachFrame_pure r R cells A v hp
    | accessor getter setter =>
      have hg : SafeVal A getter := fun node hn => hp node (List.mem_append_left _ hn)
      cases getter
      case undef => exact reachFrame_pure r R cells A (P := SafeVal A) Val.undef (by simp [SafeVal, Val.nodes])
      all_goals exact framed _ _ hg hb

private theorem reachFrame_protoProp {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (owner : String) (names : List String)
    (base : Val) (hb : SafeVal A base) (key : String) :
    ReachFrame r R cells A (SafeVal A) (protoProp call owner names base key) := by
  unfold protoProp
  split
  · exact reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])
  · exact reachFrame_getFrom away call framed root base hb key

private theorem reachFrame_hostMember {r R cells A} (name key : String) :
    ReachFrame r R cells A (SafeVal A) (hostMember name key : M W Val) := by
  unfold hostMember
  split
  · exact reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])
  · split
    · exact reachFrame_readHostRef r R cells A (.property name key)
    · exact reachFrame_unknown r R cells A _ _

private theorem stringOwn_safe {A s key v} (h : stringOwn s key = some v) : SafeVal A v := by
  unfold stringOwn at h
  split at h
  · cases h
    simp [SafeVal, Val.nodes]
  · cases hindex : key.toNat?.filter (toString · == key) with
    | none => rw [hindex] at h; cases h
    | some i =>
      cases hvalue : s.toList[i]? with
      | none => rw [hindex, Option.bind_some, hvalue] at h; cases h
      | some c =>
        rw [hindex, Option.bind_some, hvalue] at h
        simp only [Option.map_some, Option.some.injEq] at h
        subst v
        simp [SafeVal, Val.nodes]

theorem reachFrame_getProp {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (base key : Val) (hb : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (getProp call base key) := by
  cases base <;> simp only [getProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      reachFrame_getFrom away call framed (safeVal_obj.1 hb) _ hb k
  case str s =>
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    cases h : stringOwn s k with
    | none => exact reachFrame_protoProp away call framed root _ _ _ hb k
    | some value => exact reachFrame_pure r R cells A value (stringOwn_safe h)
  case num =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      reachFrame_protoProp away call framed root _ _ _ hb k
  case bool =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      reachFrame_protoProp away call framed root _ _ _ hb k
  case host name =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ => reachFrame_hostMember name k

/-- Reading a property preserves reachability when calls to retrieved getters do. -/
theorem perform_get {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (base key : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.get base key)) :=
  (reachFrame_fromW r R cells A base).bind fun o ho =>
    (reachFrame_fromW r R cells A key).bind fun k _ =>
      (reachFrame_getProp away call framed root o k ho).bind fun result safe =>
        (reachFrame_toW r R cells A result safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

private theorem deleteProps_keeps_edges (o : Obj) (key : String) :
    ∀ node ∈ ({ o with props := o.props.filter (·.1 != key) }).nodes, node ∈ o.nodes := by
  intro node hn
  simp only [Obj.nodes, List.mem_append] at hn ⊢
  rcases hn with (h | h) | h
  · exact Or.inl (Or.inl h)
  · obtain ⟨p, hp, hnode⟩ := List.mem_flatMap.mp h
    exact Or.inl (Or.inr (List.mem_flatMap.mpr ⟨p, (List.mem_filter.mp hp).1, hnode⟩))
  · exact Or.inr h

private theorem reachFrame_deleteProp {r R cells A} (away : Away r R A) (base key : Val) (hb : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (deleteProp base key : M W Val) := by
  cases base <;> simp only [deleteProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    have ha := safeVal_obj.1 hb
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    apply (reachFrame_getObj away ha).bind
    intro o _
    have ok : ReachFrame r R cells A (SafeVal A) (pure (Val.bool true) : M W Val) :=
      reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])
    cases hk : o.props.lookup k with
    | none => exact ok
    | some p =>
      simp only []
      split
      · exact (reachFrame_modifyObj away a ha _ (fun o n h => Or.inl (deleteProps_keeps_edges o k n h))).bind
          (fun _ _ => ok)
      · exact reachFrame_throwErr r R cells A _ _ _

/-- Deleting an own property removes edges from the reachable object. -/
theorem perform_delete {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat) (base key : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.delete base key)) :=
  (reachFrame_fromW r R cells A base).bind fun o ho =>
    (reachFrame_fromW r R cells A key).bind fun k _ =>
      (reachFrame_deleteProp away o k ho).bind fun result safe =>
        (reachFrame_toW r R cells A result safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

private def SafeDesc (A : HeapNode → Prop) (d : Desc) : Prop :=
  (∀ v, v ∈ d.value → SafeVal A v) ∧ (∀ v, v ∈ d.get → SafeVal A v) ∧
    (∀ v, v ∈ d.set → SafeVal A v)

private theorem safe_default {A : HeapNode → Prop} {v : Option Val} {fallback : Val}
    (hv : ∀ x, x ∈ v → SafeVal A x) (hf : SafeVal A fallback) : SafeVal A (v.getD fallback) := by
  cases v with
  | none => exact hf
  | some x => exact hv x rfl

private theorem applyDesc_nodes {A : HeapNode → Prop} (d : Desc) (hd : SafeDesc A d)
    {old : Option Property} (ho : ∀ p, p ∈ old → ∀ n ∈ p.nodes, A n)
    {p : Property} (h : applyDesc old d = .ok (some p)) : ∀ n ∈ p.nodes, A n := by
  have undef : SafeVal A .undef := fun _ hn => by cases hn
  have data (v : Val) (w e c : Bool) (hv : SafeVal A v) :
      ∀ n ∈ (Property.data (d.value.getD v) w e c).nodes, A n := safe_default hd.1 hv
  have accessor (g setter : Val) (e c : Bool) (hg : SafeVal A g) (hs : SafeVal A setter) :
      ∀ n ∈ (Property.accessor (d.get.getD g) (d.set.getD setter) e c).nodes, A n := by
    intro n hn
    rcases List.mem_append.mp hn with h | h
    · exact safe_default hd.2.1 hg _ h
    · exact safe_default hd.2.2 hs _ h
  cases old with
  | none =>
    simp only [applyDesc, pure, Except.pure] at h
    split at h
    · cases h; exact accessor .undef .undef _ _ undef undef
    · cases h; exact data .undef _ _ _ undef
  | some old =>
    have oldNodes := ho old rfl
    cases old with
    | data v w e c =>
      have vv : SafeVal A v := oldNodes
      simp [applyDesc, bind, Except.bind, pure, Except.pure] at h
      repeat' (split at h)
      all_goals cases h <;> first
        | exact data _ _ _ _ vv
        | exact accessor .undef .undef _ _ undef undef
    | accessor g setter e c =>
      have gg : SafeVal A g := fun n hn => oldNodes n (List.mem_append_left _ hn)
      have ss : SafeVal A setter := fun n hn => oldNodes n (List.mem_append_right _ hn)
      simp [applyDesc, bind, Except.bind, pure, Except.pure] at h
      repeat' (split at h)
      all_goals cases h <;> first
        | exact data .undef _ _ _ undef
        | exact accessor _ _ _ _ gg ss

private theorem setOwn_nodes (o : Obj) (k : String) (p : Property) :
    ∀ node ∈ ({ o with props := setOwn o.props k p }).nodes, node ∈ o.nodes ∨ node ∈ p.nodes := by
  intro node h
  simp only [Obj.nodes, List.mem_append] at h ⊢
  rcases h with (h | h) | h
  · exact Or.inl (Or.inl (Or.inl h))
  · obtain ⟨entry, member, edge⟩ := List.mem_flatMap.mp h
    unfold setOwn at member
    split at member
    · obtain ⟨old, hOld, rfl⟩ := List.mem_map.mp member
      rcases old with ⟨oldKey, oldProperty⟩
      by_cases same : (oldKey == k) = true
      · simp only [same, ↓reduceIte] at edge
        exact Or.inr edge
      · simp only [same] at edge
        exact Or.inl (Or.inl (Or.inr (List.mem_flatMap.mpr ⟨(oldKey, oldProperty), hOld, edge⟩)))
    · rcases List.mem_append.mp member with hOld | hNew
      · exact Or.inl (Or.inl (Or.inr (List.mem_flatMap.mpr ⟨entry, hOld, edge⟩)))
      · simp only [List.mem_singleton] at hNew
        subst entry
        exact Or.inr edge
  · exact Or.inl (Or.inr h)


private theorem reachFrame_applyDesc {r R cells A} (old : Option Property) (d : Desc)
    (safe : SafeDesc A d) (oldSafe : ∀ p, p ∈ old → ∀ node ∈ p.nodes, A node) :
    ReachFrame r R cells A (fun result => ∀ p, p ∈ result → ∀ node ∈ p.nodes, A node)
      (lift (applyDesc old d) : M W (Option Property)) := by
  cases h : applyDesc old d with
  | ok result =>
    apply reachFrame_pure r R cells A result
    intro p hp
    cases hp
    exact applyDesc_nodes d safe oldSafe h
  | error why =>
    cases why with
    | error kind message => exact reachFrame_throwErr r R cells A _ kind message
    | unknown message => exact reachFrame_unknown r R cells A _ message

private theorem reachFrame_defineOwn {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a))
    (key : String) (d : Desc) (safe : SafeDesc A d) :
    ReachFrame r R cells A (fun _ => True) (defineOwn a key d : M W Unit) := by
  unfold defineOwn
  apply (reachFrame_getObj away ha).bind
  intro current currentSafe
  split
  · exact reachFrame_unknown r R cells A _ _
  · have ownSafe : ∀ p, p ∈ current.props.lookup key → ∀ node ∈ p.nodes, A node := by
      intro p hp node edge
      have member := lookup_mem (show current.props.lookup key = some p from hp)
      exact currentSafe node (List.mem_append_left _ (List.mem_append_right _
        (List.mem_flatMap.mpr ⟨(key, p), member, edge⟩)))
    apply (reachFrame_applyDesc (current.props.lookup key) d safe ownSafe).bind
    intro result guard
    cases result with
    | none => exact reachFrame_throwErr r R cells A _ _ _
    | some p =>
      exact reachFrame_modifyObj away a ha _ fun o node edge =>
        (setOwn_nodes o key p node edge).imp id (guard p rfl node)

private theorem reachFrame_setValue {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a))
    (key : String) (value : Val) (safe : SafeVal A value) :
    ReachFrame r R cells A (fun _ => True) (do
      defineOwn a key (if ((← getObj a).props.lookup key).isSome then { value := some value } else
        { value := some value, writable := some true, enumerable := some true, configurable := some true }) : M W Unit) := by
  apply (reachFrame_getObj away ha).bind
  intro o _
  have opt (v : Val) (hv : v ∈ some value) : SafeVal A v := by
    cases hv
    exact safe
  have empty (v : Val) (hv : v ∈ (none : Option Val)) : SafeVal A v := by cases hv
  split <;> exact reachFrame_defineOwn away ha key _ ⟨opt, empty, empty⟩

theorem reachFrame_setProp {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base v, SafeVal A f → SafeVal A base → SafeVal A v →
      ReachFrame r R cells A (SafeVal A) (call f base [v]))
    (base key value : Val) (safeBase : SafeVal A base) (safeValue : SafeVal A value) :
    ReachFrame r R cells A (fun _ => True) (setProp call base key value) := by
  cases base <;> simp only [setProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    have ha := safeVal_obj.1 safeBase
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    apply (reachFrame_lookupProp away ha k).bind
    intro result accessible
    cases result with
    | none => exact reachFrame_setValue away ha k value safeValue
    | some p =>
      have hp := accessible p rfl
      cases p with
      | data _ writable =>
        cases writable
        · exact reachFrame_throwErr r R cells A _ _ _
        · exact reachFrame_setValue away ha k value safeValue
      | accessor getter setter =>
        have hs : SafeVal A setter := fun n hn => hp n (List.mem_append_right _ hn)
        cases setter
        case undef => exact reachFrame_throwErr r R cells A _ _ _
        all_goals exact (framed _ _ _ hs safeBase safeValue).bind fun _ _ =>
          reachFrame_pure r R cells A () trivial

/-- Setting a property keeps reachable edges local, provided invoking an accessible setter does. -/
theorem perform_set {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat)
    (framed : ∀ f base value, SafeVal A f → SafeVal A base → SafeVal A value →
      ReachFrame r R cells A (SafeVal A) (call f base [value]))
    (base key value : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.set base key value)) :=
  (reachFrame_fromW r R cells A base).bind fun o ho =>
    (reachFrame_fromW r R cells A key).bind fun k _ =>
      (reachFrame_fromW r R cells A value).bind fun v hv =>
        (reachFrame_setProp away call framed o k v ho hv).bind fun _ _ =>
          reachFrame_pure r R cells A (Told.value (.prim .undef)) trivial

end CoreJS

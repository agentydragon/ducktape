import EsmSplit.CoreJS.Move

/-!
# Framing a conversation with opaque code

A conversation preserves a region if every request it executes does. The request premise includes callbacks:
free-name disjointness alone cannot establish it, because a previously handed closure may reach another binding.
These are conditional semantic lemmas; `mayMove` does not yet accept opaque `.world` statements on their strength.
-/

namespace CoreJS

variable {W : World} {α β : Type}

/-- Replace a region in every stateful outcome. Fuel exhaustion has no state. -/
def Res.put (result : Res W α) (r : Nat) (H : Array Obj) (C : Array Binding) : Res W α :=
  match result with
  | .ok a s => .ok a (s.put r H C)
  | .throw v s => .throw v (s.put r H C)
  | .unknown why s => .unknown why (s.put r H C)
  | .fuel => .fuel

/-- The protected region and read bindings are unchanged; allocation stays in the current region. -/
structure Preserves (r : Nat) (R : Addr → Prop) (s t : State W) : Prop where
  heap : t.heap[r]? = s.heap[r]?
  cells : t.cells[r]? = s.cells[r]?
  reads : ∀ c, R c → t.cellAt c = s.cellAt c
  region : t.region = s.region

/-- Preservation includes exceptions: a conversation can catch them and continue. -/
def Res.Preserves (result : Res W α) (r : Nat) (R : Addr → Prop) (s : State W) : Prop :=
  match result with
  | .ok _ t | .throw _ t | .unknown _ t => CoreJS.Preserves r R s t
  | .fuel => True

/-- A computation outside region `r` ignores replacements of that region and preserves the cells `R`.
The allocation guard is needed because requests can allocate objects in the current region. -/
structure Frames (r : Nat) (R : Addr → Prop) (m : M W α) : Prop where
  replace : ∀ s H C, s.region ≠ r → m (s.put r H C) = (m s).put r H C
  preserves : ∀ s, s.region ≠ r → (m s).Preserves r R s

theorem Preserves.refl (r : Nat) (R : Addr → Prop) (s : State W) : Preserves r R s s :=
  ⟨rfl, rfl, fun _ _ => rfl, rfl⟩

theorem Preserves.trans {r : Nat} {R : Addr → Prop} {s t u : State W}
    (h : Preserves r R s t) (h' : Preserves r R t u) : Preserves r R s u :=
  ⟨h'.heap.trans h.heap, h'.cells.trans h.cells, fun c hc => (h'.reads c hc).trans (h.reads c hc),
    h'.region.trans h.region⟩

theorem Res.Preserves.trans {r : Nat} {R : Addr → Prop} {s t : State W} {result : Res W α}
    (h : CoreJS.Preserves r R s t) (h' : result.Preserves r R t) : result.Preserves r R s := by
  cases result <;> simp only [Res.Preserves] at *
  all_goals first | exact h.trans h' | trivial

theorem frames_pure (r : Nat) (R : Addr → Prop) (a : α) : Frames r R (pure a : M W α) :=
  ⟨fun _ _ _ _ => rfl, fun s _ => Preserves.refl r R s⟩

theorem frames_throwVal (r : Nat) (R : Addr → Prop) (v : Val) : Frames r R (throwVal v : M W α) :=
  ⟨fun _ _ _ _ => rfl, fun s _ => Preserves.refl r R s⟩

theorem frames_unknown (r : Nat) (R : Addr → Prop) (why : String) : Frames r R (unknown why : M W α) :=
  ⟨fun _ _ _ _ => rfl, fun s _ => Preserves.refl r R s⟩

theorem frames_outOfFuel (r : Nat) (R : Addr → Prop) : Frames r R (outOfFuel : M W α) :=
  ⟨fun _ _ _ _ => rfl, fun _ _ => trivial⟩

theorem Frames.bind {r : Nat} {R : Addr → Prop} {m : M W α} {f : α → M W β}
    (hm : Frames r R m)
    (hf : ∀ a, (∃ s t, s.region ≠ r ∧ m s = .ok a t) → Frames r R (f a)) : Frames r R (m >>= f) := by
  constructor
  · intro s H C hr
    rw [M.bind_apply, M.bind_apply, hm.replace s H C hr]
    have hp := hm.preserves s hr
    cases h : m s with
    | ok a t =>
      simp only [h, Res.Preserves] at hp
      exact (hf a ⟨s, t, hr, h⟩).replace t H C (hp.region ▸ hr)
    | throw v t => rfl
    | unknown why t => rfl
    | fuel => rfl
  · intro s hr
    rw [M.bind_apply]
    have hp := hm.preserves s hr
    cases h : m s with
    | ok a t =>
      simp only [h, Res.Preserves] at hp
      exact Res.Preserves.trans hp ((hf a ⟨s, t, hr, h⟩).preserves t (hp.region ▸ hr))
    | throw v t => simpa only [h, Res.Preserves] using hp
    | unknown why t => simpa only [h, Res.Preserves] using hp
    | fuel => trivial

theorem Frames.bind' {r : Nat} {R : Addr → Prop} {m : M W α} {f : α → M W β}
    (hm : Frames r R m) (hf : ∀ a, Frames r R (f a)) : Frames r R (m >>= f) :=
  hm.bind fun a _ => hf a

theorem Frames.attempt {r : Nat} {R : Addr → Prop} {m : M W α} (hm : Frames r R m) :
    Frames r R (attempt m) := by
  constructor
  · intro s H C hr
    unfold CoreJS.attempt
    rw [hm.replace s H C hr]
    cases m s <;> rfl
  · intro s hr
    have hp := hm.preserves s hr
    unfold CoreJS.attempt
    cases h : m s with
    | ok a t => simpa only [h, Res.Preserves] using hp
    | throw v t => simpa only [h, Res.Preserves] using hp
    | unknown why t => simpa only [h, Res.Preserves] using hp
    | fuel => trivial

theorem frames_talk (r : Nat) (R : Addr → Prop) (told : Told) : Frames r R (talk told : M W Request) :=
  ⟨fun _ _ _ _ => rfl, fun _ _ => ⟨rfl, rfl, fun _ _ => rfl, rfl⟩⟩

theorem frames_toW (r : Nat) (R : Addr → Prop) (v : Val) : Frames r R (toW v : M W WVal) := by
  cases v <;> try exact frames_pure r R _
  case obj a =>
    constructor
    · intro s H C hr
      simp only [toW, State.put]
      split <;> rfl
    · intro s hr
      simp only [toW]
      split <;> exact ⟨rfl, rfl, fun _ _ => rfl, rfl⟩

theorem frames_fromW (r : Nat) (R : Addr → Prop) (v : WVal) : Frames r R (fromW v : M W Val) := by
  cases v with
  | prim p =>
    simp only [fromW]
    split
    · exact frames_pure r R _
    · exact frames_unknown r R _
  | handle k =>
    constructor
    · intro s H C hr
      simp only [fromW, State.put]
      split <;> rfl
    · intro s hr
      simp only [fromW]
      split <;> exact Preserves.refl r R s
  | error => exact frames_pure r R _
  | host => exact frames_pure r R _

theorem frames_toldOf (r : Nat) (R : Addr → Prop) (result : Except Val Told) :
    Frames r R (toldOf result : M W Told) := by
  cases result with
  | ok told => exact frames_pure r R told
  | error v => exact (frames_toW r R v).bind' fun _ => frames_pure r R _

/-- Reading another region is unaffected by replacing the protected region. -/
theorem frames_readCell (r : Nat) (R : Addr → Prop) (a : Addr) (ha : a.region ≠ r) :
    Frames r R (readCell a : M W Val) := by
  constructor
  · intro s H C hr
    have hc : (s.put r H C).cellAt a = s.cellAt a := by rw [State.cellAt_put]; simp [ha]
    simp only [readCell, M.bind_apply, getState]
    rw [hc]
    cases s.cellAt a with
    | none => rfl
    | some binding =>
      rcases binding with ⟨value, mutable⟩
      cases value <;> rfl
  · intro s hr
    simp only [readCell, M.bind_apply, getState]
    cases s.cellAt a with
    | none => exact Preserves.refl r R s
    | some binding =>
      rcases binding with ⟨value, mutable⟩
      cases value <;> exact Preserves.refl r R s

/-- The actual interpreter conversation frames `r` when each request the world can choose frames it, including callbacks executed by
`callFn` and `construct`. The private state and choices need not be fixed; every possible request must meet the premise. -/
theorem converse_frames (r : Nat) (R : Addr → Prop) (code : Nat) (cells : List (Option Addr)) (n : Nat)
    (hrequests : ∀ k < n, ∀ req, (∃ told state, (W.talk told state).1 = req) →
      Frames r R (perform (callFn k) (construct k) code cells req : M W Told))
    (told : Told) : Frames r R (converse n code cells told : M W Val) := by
  induction n generalizing told with
  | zero => exact frames_outOfFuel r R
  | succ n ih =>
    unfold converse
    refine (frames_talk r R told).bind fun req ⟨s, t, _, htalk⟩ => ?_
    have possible : ∃ told state, (W.talk told state).1 = req := by
      unfold talk at htalk
      exact ⟨told, s.world.state, by cases htalk; rfl⟩
    cases req with
    | done v => exact frames_fromW r R v
    | throw v => exact (frames_fromW r R v).bind' fun v => frames_throwVal r R v
    | unknown why => exact frames_unknown r R why
    | _ =>
      exact (hrequests n (by omega) _ possible).attempt.bind' fun result =>
        (frames_toldOf r R result).bind' fun next => ih (fun k hk req hp => hrequests k (by omega) req hp) next

/-- A whole `.world` statement inherits the request premise of its conversation. -/
theorem opaque_world_frames (r : Nat) (R : Addr → Prop) (n : Nat) (env : Env) (code : Nat)
    (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool)) (free : List Name)
    (hrequests : ∀ k < n, ∀ req, (∃ told state, (W.talk told state).1 = req) →
      Frames r R (perform (callFn k) (construct k) code (opaqueCells env decls funs free) req : M W Told)) :
    Frames r R (execStmt n env (.opaque code decls funs free .world) : M W Flow) := by
  cases n with
  | zero => exact frames_outOfFuel r R
  | succ n =>
    unfold execStmt
    exact (frames_toW r R env.this).bind' fun this =>
      (converse_frames r R code _ n (fun k hk req hp => hrequests k (by omega) req hp)
        (.run code 0 (.prim .undef) this [] false)).bind' fun _ => frames_pure r R Flow.normal

/-- Equal replacements transfer to another array where the replaced index has the same validity. -/
theorem set!_congr_view {α : Type} {xs ys : Array α} {r : Nat} {a b : α}
    (hv : xs[r]? = ys[r]?) (he : xs.set! r a = xs.set! r b) : ys.set! r a = ys.set! r b := by
  by_cases hr : r < xs.size
  · have hab : a = b := by
      have h := congrArg (fun zs => zs[r]?) he
      simpa only [set!_getElem?, ↓reduceIte, hr, Option.some.injEq] using h
    rw [hab]
  · have hy : ¬r < ys.size := fun h => hr ((lt_iff_of_getElem? hv).2 h)
    apply Array.ext_getElem?
    intro j
    rw [set!_getElem?, set!_getElem?]
    simp [hy]

theorem State.put_congr_view {s t : State W} {r : Nat} {H H' : Array Obj} {C C' : Array Binding}
    (hh : s.heap[r]? = t.heap[r]?) (hc : s.cells[r]? = t.cells[r]?)
    (he : s.put r H C = s.put r H' C') : t.put r H C = t.put r H' C' := by
  have hH := set!_congr_view hh (congrArg State.heap he)
  have hC := set!_congr_view hc (congrArg State.cells he)
  simp only [State.put]
  rw [hH, hC]

theorem inRegion_result (id : Nat) (m : M W α) (s : State W) :
    inRegion id m s = match m { s with region := id + 1 } with
      | .ok a t => .ok a { t with region := 0 }
      | .throw v t => .throw v t
      | .unknown why t => .unknown why t
      | .fuel => .fuel := by
  simp only [inRegion, M.bind_apply, modifyState]
  cases m { s with region := id + 1 } <;> rfl

/-- A unit executing outside the protected region inherits replacement naturality. -/
theorem Frames.runUnit_replace {r j n : Nat} {R : Addr → Prop} {env : Env} {st : Stmt}
    (hf : Frames r R (execStmt n env st : M W Flow)) (hj : j + 1 ≠ r)
    (s : State W) (H : Array Obj) (C : Array Binding) :
    runUnit n env j st (s.put r H C) = (runUnit n env j st s).put r H C := by
  simp only [runUnit, inRegion_result]
  have hs : ({ s.put r H C with region := j + 1 } : State W) = { s with region := j + 1 }.put r H C := rfl
  rw [hs, hf.replace _ H C hj]
  cases execStmt n env st { s with region := j + 1 } <;> rfl

/-- Running the framed unit preserves the view needed to replay a local unit. -/
theorem Frames.runUnit_sees {r j n : Nat} {R : Addr → Prop} {env : Env} {st : Stmt}
    (hf : Frames r R (execStmt n env st : M W Flow)) (hj : j + 1 ≠ r)
    {s t : State W} {f : Flow} (h : runUnit n env j st s = .ok f t) :
    Sees r R { s with region := r } { t with region := r } := by
  obtain ⟨u, hu, rfl⟩ := runUnit_ok h
  have hp := hf.preserves { s with region := j + 1 } hj
  rw [hu] at hp
  exact ⟨rfl, rfl, hp.heap.symm, hp.cells.symm, fun c hc => (hp.reads c hc).symm⟩

/-- An allocating local unit commutes past a unit that frames its region and its read bindings. -/
theorem local_frame_units_commute {n i j : Nat} {e1 e2 : Env} {st1 st2 : Stmt} {R : Addr → Prop}
    (hij : i ≠ j) (hl : Local (i + 1) R (execStmt n e1 st1 : M W Flow))
    (hf : Frames (i + 1) R (execStmt n e2 st2 : M W Flow)) :
    Commute (runUnit n e1 i st1 : M W Flow) (runUnit n e2 j st2) := by
  intro s u v a b ha hb
  have hji : j + 1 ≠ i + 1 := by omega
  obtain ⟨u0, hu0, rfl⟩ := runUnit_ok ha
  obtain ⟨H, C, rfl, _⟩ := hl _ _ a u0 (Sees.refl rfl) hu0
  have hb' : runUnit n e2 j st2 (s.put (i + 1) H C) = .ok b v := by
    change runUnit n e2 j st2 { s.put (i + 1) H C with region := 0 } = .ok b v at hb
    rw [runUnit_region] at hb
    exact hb
  rw [hf.runUnit_replace hji] at hb'
  cases ht : runUnit n e2 j st2 s with
  | ok b' t =>
    rw [ht] at hb'
    cases hb'
    have hs := hf.runUnit_sees hji ht
    obtain ⟨H', C', he, ha'⟩ := hl _ _ a _ hs hu0
    have hp : t.put (i + 1) H C = t.put (i + 1) H' C' :=
      State.put_congr_view hs.heap hs.cells he
    refine ⟨t, rfl, ?_⟩
    have hrun := runUnit_of ha'
    exact hrun.trans (congrArg (Res.ok a) ((congrArg (fun t : State W => { t with region := 0 }) hp.symm).trans
      (congrArg (fun t : State W => t.put (i + 1) H C) (runUnit_region_zero ht)).symm))
  | throw val t => rw [ht] at hb'; cases hb'
  | unknown why t => rw [ht] at hb'; cases hb'
  | fuel => rw [ht] at hb'; cases hb'

/-- The reverse direction: a framed unit can move after the local allocating unit. -/
theorem frame_local_units_commute {n i j : Nat} {e1 e2 : Env} {st1 st2 : Stmt} {R : Addr → Prop}
    (hij : i ≠ j) (hl : Local (i + 1) R (execStmt n e1 st1 : M W Flow))
    (hf : Frames (i + 1) R (execStmt n e2 st2 : M W Flow)) :
    Commute (runUnit n e2 j st2 : M W Flow) (runUnit n e1 i st1) := by
  intro s u v b a hb ha
  have hji : j + 1 ≠ i + 1 := by omega
  have hs := hf.runUnit_sees hji hb
  obtain ⟨v0, hv0, rfl⟩ := runUnit_ok ha
  obtain ⟨H, C, rfl, ha'⟩ := hl _ _ a v0 hs.symm hv0
  refine ⟨_, runUnit_of ha', ?_⟩
  rw [runUnit_region, hf.runUnit_replace hji, runUnit_region, hb]
  exact congrArg (fun t : State W => Res.ok b (t.put (i + 1) H C)) (runUnit_region_zero hb)

/-- An allocation-only declaration and opaque world statement commute in both directions when the actual world's
requests frame the declaration's region and read cells. This is a semantic premise, not a syntax-only verdict. -/
theorem decl_opaque_world_commute {n i j : Nat} {env : Env} {e : Expr} {R : Addr → Prop}
    (hij : i ≠ j) (kind : DeclKind) (x : Name) (he : e.allocOnly = true)
    (hw : Within (i + 1) R env e.readsNow) (hx : ∀ c, env.vars.lookup x = some c → c.region = i + 1)
    (code : Nat) (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool)) (free : List Name)
    (hrequests : ∀ k < n, ∀ req, (∃ told state, (W.talk told state).1 = req) →
      Frames (i + 1) R (perform (callFn k) (construct k) code (opaqueCells env decls funs free) req : M W Told)) :
    Commute (runUnit n env i (.decl kind x e) : M W Flow)
      (runUnit n env j (.opaque code decls funs free .world)) ∧
    Commute (runUnit n env j (.opaque code decls funs free .world) : M W Flow)
      (runUnit n env i (.decl kind x e)) := by
  have hl := decl_local (W := W) he n env kind x hw hx
  have hf := opaque_world_frames (i + 1) R n env code decls funs free hrequests
  exact ⟨local_frame_units_commute hij hl hf, frame_local_units_commute hij hl hf⟩

/-- A concrete conversation that reads the first supplied binding once, then finishes. Its private state changes. -/
def readOnceWorld : World where
  σ := Bool
  init := false
  step := fun _ _ _ state => (.ret .undef, state)
  talk := fun _ seen => (if seen then .done (.prim .undef) else .readVar 0, true)

/-- Every request this world can issue frames a different region, even when the read throws. -/
theorem readOnceWorld_request_frames (r : Nat) (R : Addr → Prop) (a : Addr) (ha : a.region ≠ r)
    (k : Nat) (req : Request) (possible : ∃ message seen, (readOnceWorld.talk message seen).1 = req) :
    Frames r R (perform (callFn k) (construct k) 0 [some a] req : M readOnceWorld Told) := by
  obtain ⟨message, seen, heq⟩ := possible
  cases seen
  · have hreq : req = .readVar 0 := heq.symm
    subst req
    change Frames r R (pure a >>= fun c => readCell c >>= fun v => toW v >>= fun w => pure (Told.value w))
    exact (frames_pure r R a).bind (fun a' ⟨s, t, _, h⟩ => by
      have heq : a' = a := by cases h; rfl
      subst a'
      exact (frames_readCell r R a ha).bind' fun v =>
        (frames_toW r R v).bind' fun w => frames_pure r R (Told.value w))
  · have hreq : req = .done (.prim .undef) := heq.symm
    subst req
    exact frames_unknown r R _

/-- The request premise is inhabited: this actual world can read an external binding at every fuel bound. -/
theorem readOnceWorld_frames (r : Nat) (R : Addr → Prop) (a : Addr) (ha : a.region ≠ r) (n : Nat) (told : Told) :
    Frames r R (converse n 0 [some a] told : M readOnceWorld Val) :=
  converse_frames r R 0 [some a] n (fun k _ req hp => readOnceWorld_request_frames r R a ha k req hp) told

private def readOnceEnv : Env := { vars := [("x", ⟨1, 0⟩), ("y", ⟨2, 0⟩)], this := .undef }

/-- The final commutation theorem has a concrete instance: initialize `x`, and let opaque code read `y`.
This remains valid for every input state, including the state where `y` is in its temporal dead zone. -/
theorem readOnceWorld_commutes (n : Nat) :
    Commute (runUnit n readOnceEnv 0 (.decl .const "x" (.bool true)) : M readOnceWorld Flow)
      (runUnit n readOnceEnv 1 (.opaque 0 [] [] ["y"] .world)) ∧
    Commute (runUnit n readOnceEnv 1 (.opaque 0 [] [] ["y"] .world) : M readOnceWorld Flow)
      (runUnit n readOnceEnv 0 (.decl .const "x" (.bool true))) := by
  apply decl_opaque_world_commute (R := fun _ => False) (by decide) .const "x" rfl
  · intro z hz
    simp [Expr.readsNow] at hz
  · intro c hc
    have heq : c = ⟨1, 0⟩ := by simpa [readOnceEnv] using hc.symm
    rw [heq]
  · intro k hk req hp
    exact readOnceWorld_request_frames 1 (fun _ => False) ⟨2, 0⟩ (by decide) k req hp

private def readOnceState (value : Option Val) : State readOnceWorld where
  heap := #[#[], #[], #[]]
  cells := #[#[], #[{ value, mutable := true }], #[]]
  region := 2
  out := #[]
  world := { state := false, handed := #[] }

/-- The example really executes a request and updates the world, rather than exhausting its fuel. -/
theorem readOnceWorld_initialized :
    converse 3 0 [some ⟨1, 0⟩] (.value (.prim .undef)) (readOnceState (some (.bool true))) =
      .ok .undef { readOnceState (some (.bool true)) with world := { state := true, handed := #[] } } := rfl

/-- A thrown temporal-dead-zone read is caught by the conversation and also ends normally. -/
theorem readOnceWorld_caught :
    converse 3 0 [some ⟨1, 0⟩] (.value (.prim .undef)) (readOnceState none) =
      .ok .undef { readOnceState none with world := { state := true, handed := #[] } } := rfl

end CoreJS

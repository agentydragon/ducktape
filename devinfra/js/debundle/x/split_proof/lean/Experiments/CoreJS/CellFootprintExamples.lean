import Experiments.CoreJS.CellFootprint

namespace CoreJS.CellFootprintExamples

private def first : Addr := ⟨0, 0⟩
private def second : Addr := ⟨0, 1⟩

private def store (value : Option Val) (mutable : Bool := true) : State readOnceWorld where
  heap := #[#[]]
  cells := #[#[{ value, mutable }, { value := some .undef, mutable := true }]]
  region := 7
  out := #[]
  world := { state := false, handed := #[] }

/-- The sequential rule certifies copying arbitrary values, without a primitive-value premise. -/
theorem copy_footprint (src dst : Addr) :
    CellFootprint (fun c => c = src) (fun c => c = dst)
      (readCell src >>= fun v => setCell dst v : M readOnceWorld Unit) := by
  exact ((cellFootprint_readCell src).bind fun v => cellFootprint_setCell dst v).mono
    (fun _ h => h.elim id False.elim) (fun _ h => h.elim False.elim id)

/-- Two independent writes commute, although both mutate cells outside their execution region. -/
theorem independent_writes (v w : Val) :
    Commute (writeCell first v : M readOnceWorld Unit) (writeCell second w) := by
  apply (cellFootprint_writeCell first v).commute (cellFootprint_writeCell second w)
  · intro c hc hd
    rcases hd with h | h
    · exact h
    · subst c; exact (by decide : first ≠ second) h
  · intro c hc hd
    rcases hd with h | h
    · exact h
    · subst c; exact (by decide : second ≠ first) h

/-- Region entry and reset are part of the lifted commutation theorem, even from a nonzero initial region. -/
theorem independent_units (i j : Nat) (v w : Val) :
    Commute (inRegion i (writeCell first v) : M readOnceWorld Unit) (inRegion j (writeCell second w)) := by
  apply (cellFootprint_writeCell first v).inRegion_commute (cellFootprint_writeCell second w)
  · intro c hc hd
    rcases hd with h | h
    · exact h
    · subst c; exact (by decide : first ≠ second) h
  · intro c hc hd
    rcases hd with h | h
    · exact h
    · subst c; exact (by decide : second ≠ first) h

theorem copy_reference (a : Addr) :
    (readCell first >>= fun v => setCell second v : M readOnceWorld Unit) (store (some (.obj a))) =
      .ok () ((store (some (.obj a))).assign second (.obj a)) := rfl

/-- An empty write footprint cannot certify a real update. -/
theorem omitted_write_rejected :
    ¬ CellFootprint (fun _ => True) (fun _ => False) (setCell first (.bool true) : M readOnceWorld Unit) := by
  intro h
  obtain ⟨p, hp, hs, _⟩ := h.replay (store (some (.bool false))) ()
    ((store (some (.bool false))).assign first (.bool true)) rfl
  cases p with
  | nil =>
    have he := congrArg (fun s => (s.cellAt first).bind (·.value)) hs
    change some (Val.bool true) = some (Val.bool false) at he
    cases he
  | cons p ps => exact hp p (by simp)

/-- An empty read footprint cannot certify a value read: equal footprint views must replay the same result. -/
theorem omitted_read_rejected :
    ¬ CellFootprint (fun _ => False) (fun _ => False) (readCell first : M readOnceWorld Val) := by
  intro h
  obtain ⟨p, hp, _, ht⟩ := h.replay (store (some (.bool false))) (.bool false) (store (some (.bool false))) rfl
  cases p with
  | nil =>
    have he := ht (store (some (.bool true))) (fun _ h => h.elim False.elim False.elim)
    change Res.ok (Val.bool true) _ = .ok (.bool false) _ at he
    cases he
  | cons p ps => exact hp p (by simp)

/-- A real write/read dependency prevents successful-outcome commutation. -/
theorem dependent_read_does_not_commute :
    ¬ Commute (writeCell first (.bool true) : M readOnceWorld Unit) (readCell first) := by
  intro h
  obtain ⟨u, hu, _⟩ := h (store (some (.bool false))) (store (some (.bool true)))
    (store (some (.bool true))) () (.bool true) rfl rfl
  change Res.ok (Val.bool false) _ = .ok (.bool true) u at hu
  cases hu

theorem tdz_write : writeCell first (.bool true) (store none) =
    .throw (.error .reference "cannot access a binding before initialization") (store none) := rfl

theorem immutable_write : writeCell first (.bool true) (store (some (.bool false)) false) =
    .throw (.error .type "assignment to constant variable") (store (some (.bool false)) false) := rfl

theorem initialize_tdz : initVar { vars := [("x", first)], this := .undef } "x" (.bool true) (store none false) =
    .ok () (store (some (.bool true)) false) := rfl

/-- An ordinary assignment preserves the input region, whereas successful unit execution resets it to zero. -/
theorem unit_resets_region :
    inRegion 4 (writeCell first (.bool true)) (store (some (.bool false))) =
      .ok () { store (some (.bool true)) with region := 0 } := rfl

#print axioms copy_footprint
#print axioms independent_units
#print axioms omitted_write_rejected
#print axioms omitted_read_rejected
#print axioms dependent_read_does_not_commute

end CoreJS.CellFootprintExamples

import EsmSplit.Correct
import EsmSplit.Fast
import EsmSplit.Instance

/-! A conditional ordering rule over the existing abstract execution model.

The Boolean mask is a request to treat a statement as quiet. It is not a proof:
the theorem requires a separate semantic certificate for every selected statement.
Nothing in this file infers such certificates from JavaScript or changes the checker.
-/

namespace EsmSplit

variable {V W : Type}

/-- The two binding-dependency checks and the common-write check, without effects. -/
def Bundle.bindingConflict (B : Bundle) (i j : Nat) : Bool :=
  let a := B.stmt i
  let b := B.stmt j
  (a.effect && b.observedInit) || (b.effect && a.observedInit) ||
  (!b.hoisted && b.binds.any (B.readsOf i).contains)
    || (!a.hoisted && a.binds.any (B.readsOf j).contains)
    || a.binds.any b.binds.contains

/-- Semantic certificate: an effect-classified statement preserves the entire world,
and returns the same value independently of that world. This is an assumption about
the existing abstract `Meaning`, not a parser-derived JavaScript fact. -/
def Quiet (M : Meaning V W) (i : Nat) : Prop :=
  ∀ args world, M.run i args world = (M.pure i args, world)

/-- Remaining conflict after a *proposed* quiet mask. Binding conflicts are never waived. -/
def Bundle.conditionalConflict (B : Bundle) (quiet : Nat → Bool) (i j : Nat) : Bool :=
  B.bindingConflict i j ||
    ((B.stmt i).effect && (B.stmt j).effect && !(quiet i || quiet j))

/-- Quiet masks are sound only for a supplied meaning satisfying every certificate. -/
def QuietCertificates (M : Meaning V W) (quiet : Nat → Bool) : Prop :=
  ∀ i, quiet i = true → Quiet M i

/-- No proposed certificates means no extra crossing is permitted. -/
theorem Bundle.conditionalConflict_none (B : Bundle) (i j : Nat) :
    B.conditionalConflict (fun _ => false) i j = B.conflict i j := by
  simp [Bundle.conditionalConflict, Bundle.bindingConflict, Bundle.conflict, Bool.or_comm, Bool.or_assoc]

/-- A certified quiet statement may cross another statement if their binding
footprints do not conflict. This is a strong all-worlds condition. -/
theorem Bundle.step_comm_conditional (B : Bundle) (M : Meaning V W) (quiet : Nat → Bool)
    (cert : QuietCertificates M quiet) {i j : Nat}
    (h : B.conditionalConflict quiet i j = false) (s : St V W) :
    B.step M j (B.step M i s) = B.step M i (B.step M j s) := by
  by_cases ha : (B.stmt i).hoisted = true
  · rw [B.step_hoisted M ha, B.step_hoisted M ha]
  by_cases hb : (B.stmt j).hoisted = true
  · rw [B.step_hoisted M hb, B.step_hoisted M hb]
  simp only [Bool.not_eq_true] at ha hb
  simp [Bundle.conditionalConflict, Bundle.bindingConflict, ha, hb, List.any_eq_false] at h
  obtain ⟨⟨⟨⟨⟨_, _⟩, hR1⟩, hR2⟩, hW⟩, hE⟩ := h
  have ej : ∀ (f : Name → V) (x : V), (B.reads j).map (fun u => if (B.stmt i).binds.contains u = true
      then x else f u) = (B.reads j).map f := fun f x =>
    List.map_congr_left fun u hu => by
      have : u ∉ (B.stmt i).binds := fun hd => hR2 u hd ((B.mem_reads j u).1 hu)
      simp [this]
  have ei : ∀ (f : Name → V) (x : V), (B.reads i).map (fun u => if (B.stmt j).binds.contains u = true
      then x else f u) = (B.reads i).map f := fun f x =>
    List.map_congr_left fun u hu => by
      have : u ∉ (B.stmt j).binds := fun hd => hR1 u hd ((B.mem_reads i u).1 hu)
      simp [this]
  have env_comm : ∀ (x y : V) (f : Name → V),
      (fun u => if (B.stmt j).binds.contains u = true then y else
        if (B.stmt i).binds.contains u = true then x else f u)
      = (fun u => if (B.stmt i).binds.contains u = true then x else
        if (B.stmt j).binds.contains u = true then y else f u) := by
    intro x y f
    funext u
    by_cases hu : u ∈ (B.stmt i).binds
    · simp [hu, hW u hu]
    · simp [hu]
  by_cases hei : (B.stmt i).effect = true <;> by_cases hej : (B.stmt j).effect = true
  · have hq' : quiet i = false → quiet j = true := by simpa [hei, hej] using hE
    have hq : quiet i = true ∨ quiet j = true := by
      cases hqi : quiet i with
      | false => exact Or.inr (hq' hqi)
      | true => exact Or.inl rfl
    rcases hq with hqi | hqj
    · have hquiet := cert i hqi
      simp only [Quiet] at hquiet
      simp only [Bundle.step, ha, hb, hei, hej, Bool.false_eq_true, ↓reduceIte, hquiet, ej, ei]
      congr 1
      exact env_comm _ _ _
    · have hquiet := cert j hqj
      simp only [Quiet] at hquiet
      simp only [Bundle.step, ha, hb, hei, hej, Bool.false_eq_true, ↓reduceIte, hquiet, ej, ei]
      congr 1
      exact env_comm _ _ _
  all_goals
    simp only [Bundle.step, ha, hb, hei, hej, Bool.false_eq_true, ↓reduceIte, ej, ei]
    congr 1
    exact env_comm _ _ _

/-- Unlike `OrderOk`, this conditional check accepts a proposed quiet mask.
It is not a soundness verdict unless `QuietCertificates` is separately proved. -/
def ConditionalOrderOk (B : Bundle) (P : Plan) (quiet : Nat → Bool) : Prop :=
  P.execOrder.Pairwise fun a b => a < b ∨ B.conditionalConflict quiet a b = false

instance (B : Bundle) (P : Plan) (quiet : Nat → Bool) : Decidable (ConditionalOrderOk B P quiet) := by
  unfold ConditionalOrderOk
  infer_instance

theorem conditional_order_none (B : Bundle) (P : Plan) :
    ConditionalOrderOk B P (fun _ => false) ↔ OrderOk B P := by
  simp [ConditionalOrderOk, OrderOk, Bundle.conditionalConflict_none]

/-- A conditional order implies the same final state as the original order,
only for the supplied `Meaning` whose quiet certificates are established. -/
theorem Bundle.exec_reorder_conditional (B : Bundle) (M : Meaning V W) (quiet : Nat → Bool)
    (cert : QuietCertificates M quiet) (s0 : St V W) :
    ∀ (n : Nat) (ord : List Nat), ord.Nodup → (∀ x, x ∈ ord ↔ x < n) →
      ord.Pairwise (fun a b => a < b ∨ B.conditionalConflict quiet a b = false) →
      B.exec M s0 ord = B.exec M s0 (List.range n) :=
  B.exec_reorder_by M (B.conditionalConflict quiet) (fun _ _ h s => B.step_comm_conditional M quiet cert h s) s0

/-- Apply the conditional check to an actual plan; this does not imply the
unconditional `Correct` theorem and does not establish JavaScript footprints. -/
theorem conditional_plan_preserves (B : Bundle) (P : Plan) (M : Meaning V W)
    (quiet : Nat → Bool) (cert : QuietCertificates M quiet) (s : St V W)
    (perm : Permutes B P) (order : ConditionalOrderOk B P quiet) :
    B.exec M s P.execOrder = B.exec M s (List.range B.length) := by
  obtain ⟨hnd, hlt, hall⟩ := perm
  have hmem : ∀ x, x ∈ P.execOrder ↔ x < B.length :=
    fun x => ⟨hlt x, fun hx => hall x (List.mem_range.2 hx)⟩
  exact B.exec_reorder_conditional M quiet cert s B.length P.execOrder hnd hmem order

namespace QuietCrossingExample

/-- Two statements with distinct bindings, conservatively marked effectful. -/
def bundle : Bundle := [
  { src := "", defs := ["a"], effect := true },
  { src := "", defs := ["b"], effect := true }
]

def plan : Plan := { entry := "main", mods := [{ name := "main", stmts := [1, 0], imports := [], exports := [] }] }
def mask (i : Nat) : Bool := i == 0

theorem permutation : Permutes bundle plan := by decide +kernel
theorem no_certificate_rejected : ¬ ConditionalOrderOk bundle plan (fun _ => false) := by decide +kernel
theorem proposed_quiet_accepted : ConditionalOrderOk bundle plan mask := by decide +kernel

/-- Concrete model meaning: only statement 1 changes the world. -/
def meaning : Meaning Nat Nat where
  pure i _ := i
  run i _ w := if i == 0 then (i, w) else (i, w + 1)

theorem quiet_proved : QuietCertificates meaning mask := by
  intro i hi args w
  have : i = 0 := by simpa [mask] using hi
  subst i
  simp [meaning]

theorem conditional_correct (s : St Nat Nat) :
    bundle.exec meaning s plan.execOrder = bundle.exec meaning s (List.range bundle.length) :=
  conditional_plan_preserves bundle plan meaning mask quiet_proved s permutation proposed_quiet_accepted

/-- Even a quiet mask cannot waive a read of a declaration initialized later. -/
def bindingBundle : Bundle := [
  { src := "", defs := ["a"], effect := true },
  { src := "", defs := ["b"], eager := ["a"], effect := true }
]

theorem binding_dependency_rejected : ¬ ConditionalOrderOk bindingBundle plan (fun _ => true) := by decide +kernel

/-- The mask alone is not evidence: this meaning does change the world. -/
def effectful : Meaning Nat Nat where
  pure i _ := i
  run i _ w := (i, w + 1)

theorem false_certificate_refuted : ¬ QuietCertificates effectful mask := by
  intro h
  have q := h 0 (by decide)
  have h0 := q [] 0
  simp [effectful] at h0

/-- The extractor-generated unsafe example remains rejected even if every
statement is proposed as quiet, because its binding conflict persists. -/
theorem generated_unsafe_rejected :
    ¬ ConditionalOrderOk Example.bundle (mkPlan Example.bundle Example.control_cycle_tdz) (fun _ => true) := by
  decide +kernel

end QuietCrossingExample

end EsmSplit

import EsmSplit.CoreJS.BindingCertificate
import EsmSplit.CoreJS.BindingFootprint
import EsmSplit.CoreJS.LinkedCells
import EsmSplit.CoreJS.Move

/-! # Noninterference of checked binding summaries

The actual bundle conflict check supplies name disjointness only after both statements' certificates have checked
their required accesses and rejected hoisted metadata. Semantic commutation additionally needs the linked scope's
distinct-name/distinct-cell property and the semantic footprint of each supported statement.
-/

namespace CoreJS

/-- A pair whose actual Effects summaries cover the supported syntax and do not conflict. -/
def bindingMayMove (ss : List Stmt) (B : EsmSplit.Bundle) (i j : Nat) : Bool :=
  bindingCertificate ss B i && bindingCertificate ss B j && !B.conflict i j

/-- Keep the existing syntax-based permissions and add checked binding summaries. -/
def certifiedMayMove (ss : List Stmt) (B : EsmSplit.Bundle) (i j : Nat) : Bool :=
  MayMove ss i j || bindingMayMove ss B i j

/-- Cache each certificate once rather than repeating syntax and read-closure checks per inverted pair. -/
def bindingCertificateTab (ss : List Stmt) (B : EsmSplit.Bundle) : Array Bool :=
  ((List.range ss.length).map (bindingCertificate ss B)).toArray

theorem bindingCertificateTab_getD (ss : List Stmt) (B : EsmSplit.Bundle) (i : Nat) :
    (bindingCertificateTab ss B).getD i false = bindingCertificate ss B i := by
  by_cases hi : i < ss.length
  · simp [bindingCertificateTab, hi]
  · simp [bindingCertificateTab, hi, bindingCertificate]

/-- A table-based pair check equivalent to `certifiedMayMove`. -/
def certifiedMayMoveAt (ss : Array Stmt) (arr : Array EsmSplit.Stmt) (reads : Array EsmSplit.NameSet)
    (certs : Array Bool) (i j : Nat) : Bool :=
  MayMoveAt ss i j || (certs.getD i false && certs.getD j false && !EsmSplit.Bundle.conflictT arr reads i j)

theorem certifiedMayMoveAt_eq (ss : List Stmt) (B : EsmSplit.Bundle) (i j : Nat) :
    certifiedMayMoveAt ss.toArray B.toArray B.readsTab (bindingCertificateTab ss B) i j =
      certifiedMayMove ss B i j := by
  simp only [certifiedMayMoveAt, mayMoveAt_toArray, bindingCertificateTab_getD,
    EsmSplit.Bundle.conflictT_eq, certifiedMayMove, bindingMayMove]

/-- The complete pairwise condition, with per-statement certificates and conflict read sets built once. -/
def certifiedMovesOk (ss : List Stmt) (B : EsmSplit.Bundle) (order : List Nat) : Bool :=
  let allows := certifiedMayMoveAt ss.toArray B.toArray B.readsTab (bindingCertificateTab ss B)
  decide (order.Pairwise fun i j => i < j ∨ allows i j = true)

theorem certifiedMovesOk_iff (ss : List Stmt) (B : EsmSplit.Bundle) (order : List Nat) :
    certifiedMovesOk ss B order = true ↔
      order.Pairwise (fun i j => i < j ∨ certifiedMayMove ss B i j = true) := by
  simp only [certifiedMovesOk, certifiedMayMoveAt_eq, decide_eq_true_eq]

/-- Neither summary writes a name the other reads or writes. -/
def BindingSummary.Independent (a b : BindingSummary) : Prop :=
  (∀ x ∈ a.writes, x ∉ b.reads ∧ x ∉ b.writes) ∧
  (∀ x ∈ b.writes, x ∉ a.reads ∧ x ∉ a.writes)

/-- An actual conflict-free pair has disjoint reads/writes when neither statement is hoisted. -/
theorem binding_names_disjoint {B : EsmSplit.Bundle} {i j : Nat}
    (hi : (B.stmt i).hoisted = false) (hj : (B.stmt j).hoisted = false)
    (hc : B.conflict i j = false) :
    (∀ x ∈ (B.stmt i).binds, x ∉ B.reads j ∧ x ∉ (B.stmt j).binds) ∧
    (∀ x ∈ (B.stmt j).binds, x ∉ B.reads i ∧ x ∉ (B.stmt i).binds) := by
  simp only [EsmSplit.Bundle.conflict, hi, hj, Bool.not_false, Bool.true_and,
    Bool.or_eq_false_iff] at hc
  have hab := hc.1.2
  have hba := hc.1.1.2
  have hww := hc.2
  constructor
  · intro x hx
    constructor
    · intro hr
      have : (B.stmt i).binds.any (B.readsOf j).contains = true := by
        apply List.any_eq_true.2
        exact ⟨x, hx, by simpa using (B.mem_reads j x).1 hr⟩
      rw [hab] at this
      contradiction
    · intro hw
      have : (B.stmt i).binds.any (B.stmt j).binds.contains = true :=
        List.any_eq_true.2 ⟨x, hx, List.contains_iff_mem.2 hw⟩
      rw [hww] at this
      contradiction
  · intro x hx
    constructor
    · intro hr
      have : (B.stmt j).binds.any (B.readsOf i).contains = true := by
        apply List.any_eq_true.2
        exact ⟨x, hx, by simpa using (B.mem_reads i x).1 hr⟩
      rw [hba] at this
      contradiction
    · intro hw
      have : (B.stmt i).binds.any (B.stmt j).binds.contains = true :=
        List.any_eq_true.2 ⟨x, hw, List.contains_iff_mem.2 hx⟩
      rw [hww] at this
      contradiction

/-- Checked summaries inherit noninterference from the actual bundle conflict check. -/
theorem binding_summaries_independent {ss : List Stmt} {B : EsmSplit.Bundle} {i j : Nat}
    {si sj : Stmt} {mi mj : BindingSummary}
    (hi : bindingCertificate ss B i = true) (hj : bindingCertificate ss B j = true)
    (hsi : ss[i]? = some si) (hsj : ss[j]? = some sj)
    (hmi : si.bindingSummary = some mi) (hmj : sj.bindingSummary = some mj)
    (hc : B.conflict i j = false) : mi.Independent mj := by
  obtain ⟨si', fi, mi', hsi', hfi, hhi, hmi', hri, hwi⟩ := bindingCertificate_witness hi
  obtain ⟨sj', fj, mj', hsj', hfj, hhj, hmj', hrj, hwj⟩ := bindingCertificate_witness hj
  have : si' = si := Option.some.inj (hsi'.symm.trans hsi)
  subst si'
  have : sj' = sj := Option.some.inj (hsj'.symm.trans hsj)
  subst sj'
  have : mi' = mi := Option.some.inj (hmi'.symm.trans hmi)
  subst mi'
  have : mj' = mj := Option.some.inj (hmj'.symm.trans hmj)
  subst mj'
  have hfi' : B.stmt i = fi := by simp [EsmSplit.Bundle.stmt, hfi]
  have hfj' : B.stmt j = fj := by simp [EsmSplit.Bundle.stmt, hfj]
  have hd := binding_names_disjoint (by simpa [hfi'] using hhi) (by simpa [hfj'] using hhj) hc
  rw [hfi', hfj'] at hd
  exact ⟨fun x hx => ⟨fun hr => (hd.1 x (hwi x hx)).1 (hrj x hr),
      fun hw => (hd.1 x (hwi x hx)).2 (hwj x hw)⟩,
    fun x hx => ⟨fun hr => (hd.2 x (hwj x hx)).1 (hri x hr),
      fun hw => (hd.2 x (hwj x hx)).2 (hwi x hw)⟩⟩

/-- Name noninterference transfers to concrete cells in an injective linked environment. -/
theorem BindingSummary.Independent.cells {a b : BindingSummary} (h : a.Independent b) (env : Env)
    (hinj : ∀ x y c, env.vars.lookup x = some c → env.vars.lookup y = some c → x = y) :
    (∀ c, a.writeCells env c → ¬ (b.readCells env c ∨ b.writeCells env c)) ∧
    (∀ c, b.writeCells env c → ¬ (a.readCells env c ∨ a.writeCells env c)) := by
  constructor
  · rintro c ⟨x, hx, hxc⟩ (⟨y, hy, hyc⟩ | ⟨y, hy, hyc⟩)
    · have he := hinj x y c hxc hyc
      subst y
      exact (h.1 x hx).1 hy
    · have he := hinj x y c hxc hyc
      subst y
      exact (h.1 x hx).2 hy
  · rintro c ⟨x, hx, hxc⟩ (⟨y, hy, hyc⟩ | ⟨y, hy, hyc⟩)
    · have he := hinj x y c hxc hyc
      subst y
      exact (h.2 x hx).1 hy
    · have he := hinj x y c hxc hyc
      subst y
      exact (h.2 x hx).2 hy

/-- Accepted actual summaries, nonconflict, and real declaration allocation justify both commute directions. -/
theorem bindingMayMove_sound {W : World} (ss : List Stmt) (B : EsmSplit.Bundle) :
    MovesSound W ss (bindingMayMove ss B) := by
  intro frames s s' hframes n i j _ hc
  simp only [bindingMayMove, Bool.and_eq_true, Bool.not_eq_true'] at hc
  obtain ⟨si, fi, mi, hsi, _, _, hmi, _, _⟩ := bindingCertificate_witness hc.1.1
  obtain ⟨sj, fj, mj, hsj, _, _, hmj, _, _⟩ := bindingCertificate_witness hc.1.2
  have hn := binding_summaries_independent hc.1.1 hc.1.2 hsi hsj hmi hmj hc.2
  have hd := hn.cells (bundleEnv ss frames) (fun x y c hx hy =>
    declareTops_bundleEnv_injective hframes hx hy)
  have hi := Stmt.bindingFootprint hmi n (bundleEnv ss frames) (W := W)
  have hj := Stmt.bindingFootprint hmj n (bundleEnv ss frames) (W := W)
  constructor
  · simpa only [unitStep, bundleUnit, hsi, hsj, Option.getD_some, runUnit] using
      hi.inRegion_commute hj hd.1 hd.2 i j
  · simpa only [unitStep, bundleUnit, hsi, hsj, Option.getD_some, runUnit] using
      hj.inRegion_commute hi hd.2 hd.1 j i

theorem certifiedMayMove_sound {W : World} {ss : List Stmt} {B : EsmSplit.Bundle}
    (hfp : FootprintsOfSyntax ss B) (hud : EsmSplit.UniqueDefs B) :
    MovesSound W ss (certifiedMayMove ss B) :=
  (mayMove_sound hfp hud).or (bindingMayMove_sound ss B)

/-- Actual checked footprints extend the existing successful-execution equivalence theorem. -/
theorem moves_preserve_of_certifiedMovesOk {W : World} {ss : List Stmt} {B : EsmSplit.Bundle}
    {S : EsmSplit.Spec} {P : EsmSplit.Plan} (hvalid : EsmSplit.Valid B S P)
    (hfp : FootprintsOfSyntax ss B) (hmove : certifiedMovesOk ss B P.execOrder = true) (s : State W) :
    Ends W (Program.single ss) (.ok () s) ↔ Ends W (splitProgram ss B P) (.ok () s) :=
  moves_preserve_of_sound hvalid hfp (certifiedMayMove_sound hfp hvalid.1)
    ((certifiedMovesOk_iff ss B P.execOrder).1 hmove) s

end CoreJS

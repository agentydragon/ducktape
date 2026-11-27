import EsmSplit.CoreJS.AsyncPrefix
import EsmSplit.CoreJS.Frame
import EsmSplit.CoreJS.Request.Allocation

namespace CoreJS.AsyncPrefix

variable {W : World}

theorem allocate_fresh {prototype addr : Addr} {s t : State W}
    (step : allocate prototype s = .ok addr t) :
    s.objAt addr = none ∧ t.objAt addr = some (pendingObject prototype) ∧ Extends s t := by
  refine ⟨(alloc_ok step).1, ?_, (alloc_ok step).2⟩
  unfold allocate alloc at step
  split at step
  · rename_i _ found
    cases step
    simp only [objAt_push found, ite_true]
  · cases step

theorem allocate_reachFrame {r R cells A} (prototype : Addr) (root : A (.obj prototype))
    (outside : ∀ addr : Addr, addr.region ≠ r → A (.obj addr)) :
    ReachFrame r R cells A (fun addr => A (.obj addr)) (allocate prototype : M W Addr) := by
  apply reachFrame_alloc _ _ outside
  intro node h
  simp [pendingObject, Obj.nodes, PromiseSlot.nodes] at h
  subst node
  exact root

/-- Settlement only replaces the one internal slot. It keeps all other objects,
the bindings, the current allocation region, and the world and output of the body. -/
theorem settle_frame {s t : State W} {promise : Addr} {result : PromiseSlot} {v : Val}
    (done : settle promise result s = .ok v t) :
    v = .obj promise ∧
      (∀ a, a ≠ promise → t.objAt a = s.objAt a) ∧
      t.cells = s.cells ∧ t.region = s.region ∧ t.world = s.world ∧ t.out = s.out := by
  cases found : (s.objAt promise).bind (·.promise) with
  | none => simp [settle, found] at done
  | some slot =>
    cases slot <;> cases result <;> simp [settle, found] at done
    all_goals
      rcases done with ⟨rfl, rfl⟩
      exact ⟨rfl, fun a ha => objAt_modify s promise _ a ha, rfl, rfl, rfl, rfl⟩

/-- A rejection reason can itself be an object; the promise's internal edge
keeps that object in the same reachability envelope as the caller. -/
theorem settlement_edges {A : HeapNode → Prop} {obj : Obj} {value : Val}
    (safe : SafeVal A value) (reject : Bool) :
    ∀ node ∈ ({ obj with promise := some (if reject then .rejected value else .fulfilled value) }).nodes,
      node ∈ obj.nodes ∨ A node := by
  intro node h
  cases reject <;>
    simp only [Bool.false_eq_true, ite_false, ite_true, Obj.nodes, List.mem_append,
      Option.toList_some, List.flatMap_cons, List.flatMap_nil, List.append_nil, PromiseSlot.nodes] at h
  all_goals
    rcases h with old | payload
    · exact Or.inl (by simp only [Obj.nodes, List.mem_append]; exact Or.inl old)
    · exact Or.inr (safe _ payload)

end CoreJS.AsyncPrefix

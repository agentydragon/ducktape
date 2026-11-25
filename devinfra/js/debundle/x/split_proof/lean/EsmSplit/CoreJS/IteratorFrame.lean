import EsmSplit.CoreJS.InvocationFrame
import EsmSplit.CoreJS.Iterator

/-! A suspended synchronous iterator retains its source or its cached next
method and receiver. All retained values stay in the call's heap envelope. -/

namespace CoreJS

variable {W : World}

def IteratorCursor.Safe (A : HeapNode → Prop) : IteratorCursor → Prop
  | .array source _ => A (.obj source)
  | .object receiver next => A (.obj receiver) ∧ SafeVal A next

def IteratorStepSafe (A : HeapNode → Prop) : Option (Val × IteratorCursor) → Prop
  | none => True
  | some (value, cursor) => SafeVal A value ∧ cursor.Safe A

private theorem safeUndef {A : HeapNode → Prop} : SafeVal A .undef := by simp [SafeVal, Val.nodes]

private theorem reachFrame_requireIteratorObject {r R cells A} (message : String) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun a => A (.obj a)) (requireIteratorObject message v : M W Addr) := by
  cases v <;> simp only [requireIteratorObject] <;>
    first
    | exact reachFrame_pure r R cells A _ (safeVal_obj.1 hv)
    | exact reachFrame_errorValue (.error .type _) (by simp [SafeVal, Val.nodes])
    | exact reachFrame_unknown r R cells A _ _

private theorem reachFrame_checkArrayIteratorNext {r R cells A} (away : Away r R A)
    (root : A (.obj arrayIteratorProto)) :
    ReachFrame r R cells A (fun _ => True) (checkArrayIteratorNext : M W Unit) := by
  unfold checkArrayIteratorNext
  refine (reachFrame_getObj away root).bind fun obj _ => ?_
  split <;> first | exact reachFrame_pure r R cells A () trivial | exact reachFrame_unknown r R cells A _ _

theorem getIterator_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto)) (value : Val) (safe : SafeVal A value) :
    ReachFrame r R cells A (IteratorCursor.Safe A) (getIterator call value) := by
  unfold getIterator
  refine (reachFrame_getProp away call framed.getter objectRoot value (.wellKnownSymbol .iterator) safe).bind fun method hm => ?_
  split
  · refine (reachFrame_requireIteratorObject _ value safe).bind fun a ha => ?_
    exact (reachFrame_checkArrayIteratorNext away arrayIteratorRoot).bind fun _ _ =>
      reachFrame_pure r R cells A (IteratorCursor.array a 0) ha
  · refine (framed method value [] hm safe (by simp)).bind fun result hr => ?_
    refine (reachFrame_requireIteratorObject _ result hr).bind fun receiver hReceiver => ?_
    refine (reachFrame_getProp away call framed.getter objectRoot (.obj receiver) (.str "next")
      (safeVal_obj.2 hReceiver)).bind fun next hNext => ?_
    exact reachFrame_pure r R cells A (P := IteratorCursor.Safe A)
      (IteratorCursor.object receiver next) ⟨hReceiver, hNext⟩

theorem iteratorStep_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (root : A (.obj objectProto))
    (cursor : IteratorCursor) (readValue : Bool) (safe : cursor.Safe A) :
    ReachFrame r R cells A (IteratorStepSafe A) (iteratorStep call cursor readValue) := by
  let get := reachFrame_getProp away call framed.getter root
  have done : ReachFrame r R cells A (IteratorStepSafe A) (pure none : M W _) :=
    reachFrame_pure r R cells A none trivial
  cases cursor with
  | array source index =>
    simp only [iteratorStep]
    refine (get (.obj source) (.str "length") (safeVal_obj.2 safe)).bind fun length _ => ?_
    split
    · rename_i f
      split
      · exact done
      · refine (get (.obj source) (.str (toString index)) (safeVal_obj.2 safe)).bind fun value hv => ?_
        exact reachFrame_pure r R cells A _ ⟨by cases readValue <;> first | exact safeUndef | exact hv, safe⟩
    · exact reachFrame_unknown r R cells A _ _
  | object receiver next =>
    simp only [iteratorStep]
    obtain ⟨hReceiver, hNext⟩ := safe
    refine (framed next (.obj receiver) [] hNext (safeVal_obj.2 hReceiver) (by simp)).bind fun result hr => ?_
    refine (reachFrame_requireIteratorObject _ result hr).bind fun a ha => ?_
    refine (get (.obj a) (.str "done") (safeVal_obj.2 ha)).bind fun v _ => ?_
    refine (reachFrame_lift (toBoolean v) (fun _ _ => trivial)).bind fun isDone _ => ?_
    cases isDone with
    | true => exact done
    | false =>
      cases readValue with
      | false =>
        exact reachFrame_pure r R cells A (P := IteratorStepSafe A)
          (some (.undef, .object receiver next)) ⟨safeUndef, hReceiver, hNext⟩
      | true =>
        exact (get (.obj a) (.str "value") (safeVal_obj.2 ha)).bind fun value hv =>
          reachFrame_pure r R cells A (P := IteratorStepSafe A)
            (some (value, .object receiver next)) ⟨hv, hReceiver, hNext⟩

private theorem callIteratorReturn_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call)
    (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto)) (cursor : IteratorCursor) (safe : cursor.Safe A) :
    ReachFrame r R cells A (fun result => ∀ v, result = some v → SafeVal A v) (callIteratorReturn call cursor) := by
  have noneResult : ReachFrame r R cells A (fun result => ∀ v, result = some v → SafeVal A v)
      (pure none : M W (Option Val)) :=
    reachFrame_pure r R cells A none (by simp)
  have native (proto : Addr) (root : A (.obj proto)) : ReachFrame r R cells A
      (fun result => ∀ v, result = some v → SafeVal A v) (do
        match ← lookupProp proto (.str "return") with
        | none | some (.data .undef ..) | some (.data .null ..) => pure none
        | _ => unknown "native iterator has a custom return method" : M W (Option Val)) := by
    refine (reachFrame_lookupProp away root (.str "return")).bind fun result _ => ?_
    split <;> first | exact noneResult | exact reachFrame_unknown r R cells A _ _
  cases cursor with
  | array => exact native arrayIteratorProto arrayIteratorRoot
  | object receiver next =>
    simp only [callIteratorReturn]
    have hReceiver : SafeVal A (.obj receiver) := safeVal_obj.2 safe.1
    refine (reachFrame_getProp away call framed.getter objectRoot (.obj receiver) (.str "return")
      hReceiver).bind fun ret hr => ?_
    split
    · exact noneResult
    · exact (framed ret (.obj receiver) [] hr hReceiver (by simp)).bind fun result hResult =>
        reachFrame_pure r R cells A (some result) (fun _ he => Option.some.inj he ▸ hResult)

theorem iteratorClose_reachFrame {α : Type} {r R cells A} {P : α → Prop}
    (away : Away r R A) (call : Call W) (framed : CallFramed r R cells A call)
    (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto)) (cursor : IteratorCursor) (safe : cursor.Safe A)
    (completion : Except Val α) (hcomp : match completion with | .ok a => P a | .error e => SafeVal A e) :
    ReachFrame r R cells A P (iteratorClose call cursor completion) := by
  unfold iteratorClose
  refine (callIteratorReturn_reachFrame away call framed objectRoot arrayIteratorRoot cursor safe).attempt.bind
    fun result h => ?_
  cases completion with
  | error e => exact reachFrame_errorValue e hcomp
  | ok a =>
    cases result with
    | error e => exact reachFrame_errorValue e h
    | ok optional =>
      cases optional with
      | none => exact reachFrame_pure r R cells A a hcomp
      | some v =>
        cases v <;> first
        | exact reachFrame_pure r R cells A a hcomp
        | exact reachFrame_unknown r R cells A _ _
        | exact reachFrame_errorValue (.error .type _) (by simp [SafeVal, Val.nodes])

theorem collectIterator_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (root : A (.obj objectProto))
    (fuel : Nat) (cursor : IteratorCursor) (safe : cursor.Safe A) :
    ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (collectIterator call fuel cursor) := by
  induction fuel generalizing cursor with
  | zero => exact reachFrame_fuel
  | succ n ih =>
    simp only [collectIterator]
    refine (iteratorStep_reachFrame away call framed root cursor true safe).bind fun step hs => ?_
    cases step with
    | none => exact reachFrame_pure r R cells A [] (by simp)
    | some next =>
      exact (ih next.2 hs.2).bind fun values hv =>
        reachFrame_pure r R cells A (next.1 :: values) (by
          intro v h
          rcases List.mem_cons.1 h with rfl | h
          · exact hs.1
          · exact hv v h)

theorem iteratorValues_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto)) (fuel : Nat) (source : Val) (safe : SafeVal A source) :
    ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v) (iteratorValues fuel call source) :=
  (getIterator_reachFrame away call framed objectRoot arrayIteratorRoot source safe).bind
    (collectIterator_reachFrame away call framed objectRoot fuel)

end CoreJS

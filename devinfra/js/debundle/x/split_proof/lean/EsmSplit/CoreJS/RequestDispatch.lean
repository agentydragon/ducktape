import EsmSplit.CoreJS.InvocationFrame
import EsmSplit.CoreJS.RequestAllocation

/-! Frame the CoreJS primitive operations and every request the opaque world can issue. -/

namespace CoreJS

variable {W : World}

private theorem reachFrame_logLine {r R cells A} (line : List String) :
    ReachFrame r R cells A (SafeVal A) (do
      modifyState fun s => { s with out := s.out.push (" ".intercalate line) }
      pure Val.undef : M W Val) := by
  constructor
  · intros
    rfl
  · intro s _ _
    exact ⟨rfl, rfl, fun _ _ => rfl, rfl⟩
  · intro s hs _
    exact ⟨(fun _ h => by cases h), hs.handed, hs.supplied, fun n b hn edge => by
      cases edge with
      | obj ho hm => exact hs.closed _ _ hn (.obj ho hm)
      | cell hc hm => exact hs.closed _ _ hn (.cell hc hm)⟩

theorem reachFrame_prim_log {r R cells A} (call : Call W) (args : List Val) :
    ReachFrame r R cells A (SafeVal A) (prim call .log args) := by
  simp only [prim]
  have rest : ReachFrame r R cells A (SafeVal A) (do
      let line ← lift (args.mapM logArg)
      modifyState fun s => { s with out := s.out.push (" ".intercalate line) }
      pure Val.undef : M W Val) :=
    (reachFrame_lift (r := r) (R := R) (cells := cells) (A := A) (args.mapM logArg)
      (P := fun _ => True) fun _ _ => trivial).bind fun line _ => reachFrame_logLine line
  split
  · rename_i format head tail
    split
    · exact reachFrame_unknown r R cells A _ _
    · exact rest
  · exact rest


private theorem safe_arg {A : HeapNode → Prop} {args : List Val} (safe : ∀ x ∈ args, SafeVal A x) (i : Nat) :
    SafeVal A (args.getD i .undef) := by
  rw [List.getD_eq_getElem?_getD]
  cases h : args[i]? with
  | none => exact fun _ hn => by cases hn
  | some x => exact safe x (list_index_mem h)

theorem reachFrame_prim {r R cells A} (away : Away r R A) (call : Call W) (hf : CallFramed r R cells A call)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (root : A (.obj objectProto))
    (p : Prim) (args : List Val) (safe : ∀ x ∈ args, SafeVal A x) :
    ReachFrame r R cells A (SafeVal A) (prim call p args) := by
  cases p with
  | globalTypeof name => exact reachFrame_observeGlobalTypeof r R cells A name
  | globalRef name => exact reachFrame_readHostRef r R cells A (.binding name)
  | opaqueAmbientRef name | opaqueConstructorRef name => exact reachFrame_unknown r R cells A _ _
  | opaqueAmbientMemberRef receiver member => exact reachFrame_unknown r R cells A _ _
  | log => exact reachFrame_prim_log call args
  | create => exact reachFrame_create call args (safe_arg safe 0) objects
  | getPrototypeOf => exact reachFrame_prim_getPrototypeOf away call args safe
  | setPrototypeOf => exact reachFrame_prim_setPrototypeOf away call args safe
  | defineProperty => exact reachFrame_prim_defineProperty away call hf.getter root args safe

theorem perform_prim {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code) (hf : CallFramed r R cells A call)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (root : A (.obj objectProto))
    (p : Prim) (args : List WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.prim p args)) :=
  (ReachFrame.mapM args fun a _ => reachFrame_fromW r R cells A a).bind fun values safe =>
    (reachFrame_prim away call hf objects root p values safe).bind fun result hr =>
      (reachFrame_toW r R cells A result hr).bind fun v _ =>
        reachFrame_pure r R cells A (Told.value v) trivial

/-- All requests frame separation when the invoked functions and constructors
frame their smaller executions. End markers retain their existing refusal. -/
theorem perform_reachFrame {r R cells} (hr : r ≠ 0) (call : Call W) (construct : Val → List Val → M W Val)
    (code : Nat) (hf : CallFramed r R cells (Unprotected r R) call)
    (hc : ∀ f args, SafeVal (Unprotected r R) f → (∀ a ∈ args, SafeVal (Unprotected r R) a) →
      ReachFrame r R cells (Unprotected r R) (SafeVal (Unprotected r R)) (construct f args))
    (req : Request) :
    ReachFrame r R cells (Unprotected r R) (fun _ => True) (perform call construct code cells req) := by
  have away := unprotected_away r R
  have objects (a : Addr) (ha : a.region ≠ r) : Unprotected r R (.obj a) := ha
  have root : Unprotected r R (.obj objectProto) := Ne.symm hr
  cases req with
  | readVar i | keys i => exact perform_bindingOrKeys away call construct code _ rfl
  | writeVar i v | initVar i v => exact perform_bindingOrKeys away call construct code _ rfl
  | get o k => exact perform_get away call construct code hf.getter root o k
  | set o k v => exact perform_set away call construct code hf.setter o k v
  | delete o k => exact perform_delete away call construct code o k
  | has k o => exact perform_has away call construct code k o
  | call f this args => exact perform_call call construct code hf f this args
  | construct f args => exact perform_construct call construct code hc f args
  | newObject p => exact perform_newObject r R cells call construct code p (fun _ => Ne.symm hr)
  | newFunction part name length ctor =>
    exact perform_newFunction r R cells call construct code part name length ctor (Ne.symm hr)
  | prim p args => exact perform_prim away call construct code hf objects root p args
  | done v | throw v => exact reachFrame_unknown r R cells _ _ _
  | unknown why => exact reachFrame_unknown r R cells _ _ _

end CoreJS

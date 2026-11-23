import EsmSplit.CoreJS.InvocationFrame
import EsmSplit.CoreJS.RequestAllocation
import EsmSplit.CoreJS.StatementFrame

/-! Frame opaque closure allocation and entering opaque module statements. -/

namespace CoreJS

variable {W : World}

private theorem reachFrame_makeOpaque {r R cells A} (away : Away r R A)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a))
    (objectRoot : A (.obj objectProto)) (functionRoot : A (.obj functionProto))
    (code part : Nat) (captures : List (Option Addr)) (roots : ∀ a, some a ∈ captures → A (.cell a))
    (name : String) (length : Nat) (constructor : Bool) :
    ReachFrame r R cells A (SafeVal A) (makeOpaque code part captures name length constructor : M W Val) := by
  unfold makeOpaque
  apply (reachFrame_alloc _ ?_ objects).bind
  · intro a ha
    have prototypeFrame : ReachFrame r R cells A (fun _ => True)
        (if constructor then addPrototype a else pure () : M W Unit) := by
      cases constructor
      · exact reachFrame_pure r R cells A () trivial
      · exact reachFrame_addPrototype away objectRoot (fun o hs => reachFrame_alloc o hs objects) a ha
    exact prototypeFrame.bind fun _ _ => reachFrame_pure r R cells A (Val.obj a) (safeVal_obj.2 ha)
  · intro n hn
    simp [Obj.nodes, Property.nodes, Code.nodes, Val.nodes] at hn
    rcases hn with rfl | member
    · exact functionRoot
    · obtain ⟨a, ha, b, eq, rfl⟩ := member
      exact roots b (eq ▸ ha)

private theorem reachFrame_initClosures {r R cells A} (away : Away r R A)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a))
    (objectRoot : A (.obj objectProto)) (functionRoot : A (.obj functionProto))
    (code : Nat) (captures : List (Option Addr)) (roots : ∀ a, some a ∈ captures → A (.cell a))
    (env : Env) (safe : ∀ node ∈ env.nodes, A node) (decls : List (Name × DeclKind))
    (fns : List (String × Nat × Bool)) (part : Nat) :
    ReachFrame r R cells A (fun _ => True) (initClosures code captures env decls fns part : M W Unit) := by
  induction decls generalizing fns part with
  | nil =>
    cases fns
    · exact reachFrame_pure r R cells A () trivial
    · exact reachFrame_unknown r R cells A _ _
  | cons decl rest ih =>
    cases fns with
    | nil => exact reachFrame_unknown r R cells A _ _
    | cons f restFns =>
      obtain ⟨x, kind⟩ := decl
      obtain ⟨name, length, ctor⟩ := f
      simp only [initClosures]
      apply (reachFrame_makeOpaque away objects objectRoot functionRoot code part captures roots name length ctor).bind
      intro v hv
      have write : ReachFrame r R cells A (fun _ => True)
          (if kind == .var then writeVar env x v else initVar env x v : M W Unit) := by
        split
        · exact reachFrame_writeVar away env x v safe hv
        · exact reachFrame_initVar away env x v safe hv
      exact write.bind fun _ _ => ih restFns (part + 1)

private theorem opaqueCells_nodes {A : HeapNode → Prop} {env : Env} (safe : ∀ node ∈ env.nodes, A node)
    (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool)) (free : List Name) :
    ∀ a, some a ∈ opaqueCells env decls funs free → A (.cell a) := by
  intro a ha
  obtain ⟨name, _, h⟩ := List.mem_map.mp ha
  exact Env.nodes_lookup safe h

theorem execOpaque_reachFrame_step {r R cells A n} (away : Away r R A)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a))
    (objectRoot : A (.obj objectProto)) (functionRoot : A (.obj functionProto))
    (conversation : ∀ code inner told, (∀ a, some a ∈ inner → A (.cell a)) →
      ReachFrame r R inner A (SafeVal A) (converse n code inner told : M W Val))
    (env : Env) (code : Nat) (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool))
    (free : List Name) (action : OpaqueRun) (safe : ∀ node ∈ env.nodes, A node) :
    ReachFrame r R cells A (FlowSafe A) (execStmt (n + 1) env (.opaque code decls funs free action) : M W Flow) := by
  have roots := opaqueCells_nodes safe decls funs free
  have normal := reachFrame_pure (W := W) r R cells A Flow.normal (FlowSafe.normal A)
  cases action with
  | world =>
    simp only [execStmt]
    apply (reachFrame_toW r R cells A env.this (Env.nodes_this safe)).bind
    intro this _
    apply ((conversation code _ (.run code 0 (.prim .undef) this [] false) roots).withCells roots).bind
    intro _ _
    exact normal
  | closures fns =>
    exact (reachFrame_initClosures away objects objectRoot functionRoot code _ roots env safe decls fns
      (funs.length + 1)).bind fun _ _ => normal

end CoreJS

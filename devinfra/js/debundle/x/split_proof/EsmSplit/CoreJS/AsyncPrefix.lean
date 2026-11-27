import EsmSplit.CoreJS.Value

/-! The synchronous completion boundary of an async call.

The caller supplies the realm's intrinsic Promise prototype and an ordinary
body computation (including parameter initialization). A later evaluator step
will provide that computation. This module neither starts jobs nor resumes an
await. Unknown and fuel exhaustion from the computation stay unknown and fuel.
-/

namespace CoreJS.AsyncPrefix

variable {W : World}

def pendingObject (prototype : Addr) : Obj :=
  { proto := some prototype, props := [], promise := some .pending }

def allocate (prototype : Addr) : M W Addr := alloc (pendingObject prototype)

/-- Internal settlement operates only on a fresh pending promise from this
boundary. It never changes the object's ordinary properties or prototype. -/
def settle (promise : Addr) (result : PromiseSlot) : M W Val := fun s =>
  match (s.objAt promise).bind (·.promise), result with
  | some .pending, .fulfilled _ | some .pending, .rejected _ =>
    let heap := s.heap.modify promise.region (·.modify promise.index fun o => { o with promise := some result })
    .ok (.obj promise) { s with heap }
  | _, _ => .unknown "model: expected a pending promise and a settlement" s

/-- An object-like resolution would read its `then` property and may schedule a
job. Rejection stores the thrown value without inspecting it. -/
def finish (promise : Addr) : Res W Val → Res W Val
  | .ok value s =>
    match value with
    | .obj _ | .host _ | .error .. => .unknown "async object resolution requires thenable semantics" s
    | _ => settle promise (.fulfilled value) s
  | .throw reason s => settle promise (.rejected reason) s
  | .unknown why s => .unknown why s
  | .fuel => .fuel

/-- Allocates before running the supplied body at smaller fuel. A completed
body returns the promise reference; it does not return the settled value. -/
def invoke (prototype : Addr) (body : Nat → M W Val) : Nat → M W Val
  | 0 => outOfFuel
  | n + 1 => fun s =>
    match allocate prototype s with
    | .ok promise t => finish promise (body n t)
    | .throw v t => .throw v t
    | .unknown why t => .unknown why t
    | .fuel => .fuel

@[simp] theorem invoke_zero (prototype : Addr) (body : Nat → M W Val) (s : State W) :
    invoke prototype body 0 s = .fuel := rfl

theorem invoke_after_allocate {prototype promise : Addr} {body : Nat → M W Val} {n : Nat}
    {s allocated : State W} (fresh : allocate prototype s = .ok promise allocated) :
    invoke prototype body (n + 1) s = finish promise (body n allocated) := by
  simp only [invoke, fresh]

end CoreJS.AsyncPrefix

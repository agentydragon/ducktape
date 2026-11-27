import EsmSplit.CoreJS.Value

/-! The synchronous completion boundary of an async call.

The caller supplies the realm's intrinsic Promise prototype and an ordinary
body computation (including parameter initialization). This module neither
starts jobs nor resumes an
await. Unknown and fuel exhaustion from the computation stay unknown and fuel.
-/

namespace CoreJS.AsyncPrefix

variable {W : World}

def pendingObject (prototype : Addr) : Obj :=
  { proto := some prototype, props := [], promise := some .pending }

def allocate (prototype : Addr) : M W Addr := alloc (pendingObject prototype)

/-- Internal settlement operates only on a fresh pending promise from this
boundary. It never changes the object's ordinary properties or prototype. -/
def settle (promise : Addr) (result : PromiseSlot) : M W Val := do
  let obj ← getObj promise
  match obj.promise, result with
  | some .pending, .fulfilled _ | some .pending, .rejected _ =>
    modifyObj promise fun o => { o with promise := some result }
    pure (.obj promise)
  | _, _ => unknown "model: expected a pending promise and a settlement"

/-- An object-like resolution would read its `then` property and may schedule a
job. Rejection stores the thrown value without inspecting it. -/
def complete (promise : Addr) : Except Val Val → M W Val
  | .ok value =>
    match value with
    | .obj _ | .host _ | .error .. => unknown "async object resolution requires thenable semantics"
    | _ => settle promise (.fulfilled value)
  | .error reason => settle promise (.rejected reason)

def finish (promise : Addr) : Res W Val → Res W Val
  | .ok value s => complete promise (.ok value) s
  | .throw reason s => complete promise (.error reason) s
  | .unknown why s => .unknown why s
  | .fuel => .fuel

/-- Start the synchronous part of an async invocation. `body` is passed at the
caller's smaller fuel, just as it is for a synchronous function call. -/
def invokeBody (prototype : Addr) (body : M W Val) : M W Val := do
  let promise ← allocate prototype
  let result ← attempt body
  complete promise result

/-- Allocates before running the supplied body at smaller fuel. A completed
body returns the promise reference; it does not return the settled value. -/
def invoke (prototype : Addr) (body : Nat → M W Val) : Nat → M W Val
  | 0 => outOfFuel
  | n + 1 => invokeBody prototype (body n)

@[simp] theorem invoke_zero (prototype : Addr) (body : Nat → M W Val) (s : State W) :
    invoke prototype body 0 s = .fuel := rfl

private theorem bindResult {α β : Type} (m : M W α) (next : α → M W β) (s : State W) :
    (m >>= next) s = match m s with
      | .ok value t => next value t
      | .throw e t => .throw e t
      | .unknown why t => .unknown why t
      | .fuel => .fuel := rfl

theorem invoke_after_allocate {prototype promise : Addr} {body : Nat → M W Val} {n : Nat}
    {s allocated : State W} (fresh : allocate prototype s = .ok promise allocated) :
    invoke prototype body (n + 1) s = finish promise (body n allocated) := by
  simp only [invoke, invokeBody, bindResult, fresh, attempt]
  cases body n allocated <;> rfl

end CoreJS.AsyncPrefix

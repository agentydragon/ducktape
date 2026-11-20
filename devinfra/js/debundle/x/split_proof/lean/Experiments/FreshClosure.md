# Factory closure crossing a Set allocation

This is a separate transition model, not the translation checker's JavaScript analysis or a JavaScript semantics. It represents the shape:

```js
function factory(f) {
  return async (...xs) => wrap(await f(...xs));
}
const result = factory(implementation);
const set = new Set();
```

The factory step reads its argument, creates a closure with a private immutable parameter holding that argument, and initializes `result`. The deferred body is abstracted to an observation of the captured argument identity; this model does not execute the async function or its calls. Closure identities and Set identities are separate types, and the Set step allocates one fresh Set without callbacks, throws, visible side effects, or access to `result`. Treating an actual `new Set()` as that step requires establishing properties of the realm and constructor; this experiment does not establish them. It also omits JavaScript module initialization, TDZ exceptions, arbitrary heap references, weak references, finalization, and resource exhaustion.

`commutable` accepts a modeled pure Set allocation or log and refuses argument changes, closure allocation, publication, invocation and a reentrant Set constructor. `make_step_commute` proves equality of the entire modeled state in either order, including the closure identity, captured argument, Set identity and trace. `inferred_swap_preserves_continuation` extends equality through arbitrary continuations made from the model's transitions. A separate lemma makes the fresh-closure claim conditional on the allocator's next identity being unused; maintaining that invariant is outside the swap theorem.

The controls demonstrate different modeled observations if the argument changes, the closure escapes, it is called early, or a substituted Set constructor calls back before returning. A competing closure allocation also changes its observable identity. In particular, `pureNewSet` excludes the early-indirect-call scenario; this is a semantic restriction, not a result inferred by today's frontend. The checker does not currently derive this factory/Set rule from real JavaScript, and this spike does not change its acceptance or discharge Tana obligations.

The draft [frontend factory rule](https://github.com/agentydragon/ducktape/pull/18072) recognizes a function that returns an arrow, a uniquely declared hoisted-function argument with no assignments, direct factory calls, and no `eval`; it retains eager reads and deferred dependencies. It does not establish that an arbitrary crossed constructor is `pureNewSet`, or that a callback reachable through mutable global state cannot observe an uninitialized result. The draft's indirect-callback handling is under investigation. A checked bridge would need to connect the actual parsed and scope-resolved factory, argument and generated result to these modeled values, justify the factory's captured parameter and deferred-body abstraction, and conservatively establish that the crossed statement cannot access or invoke the result through any reachable callback. For this particular Set crossing it must also establish the needed constructor and realm behavior. This model does none of that source-to-semantics analysis.

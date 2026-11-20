# MiniJS local-call effect classification

The existing MiniJS footprint classifies every application as effectful. This experiment checks a narrower rule: when a supplied candidate is a lambda with an inert body and the argument is a number literal, executing that call cannot change the MiniJS world, provided the runtime callee binding equals the candidate. The classifier returns false for all other cases. The axiom audit also checks that the closed-read invariant is sufficient to bound the call's dependencies.

This is a theorem about the existing fuel-bounded `MJS.eval`, not JavaScript. It does not change the debundler's checker or Tana's obligations. In particular, MiniJS does not model objects, prototype inheritance, getters, setters, coercions, mutation, exceptions, host calls, or reassignment; failed and fuel-exhausted evaluations both return `none`. Neither closure identity at the call site nor a JavaScript-to-MiniJS translation is inferred here. Those conditions require separate analysis and proof before using the rule for JavaScript. Do not assume an observed local name remains bound to the same function.

The audited examples accept an inert identity call, reject an effectful argument and an effectful body, demonstrate that the latter changes the modeled world, and reject a different runtime binding. `local_call_world` quantifies over worlds and fuel; equality on `none` is not a proof of termination.

Run `bbr test //devinfra/js/debundle/x/split_proof/lean:local_call_axioms_test`.

## Trivial pass-through wrapper `UpdateAgentModel` is acceptable

Some critiques flagged `app.UpdateAgentModel` as a trivial pass-through that should be inlined (i.e., replace callers
with calls to `agent.UpdateModel`).

But actually, squashing this method should NOT be prescribed as required. This method is part of an imperfect facade
boundary around App→CoderAgent. In the context of the facade, it would serve as a decoupling point. However, the facade
is currently imperfect, which is the associated finding that should be reported here. See correct finding: “App façade
vs reach-through”.

To account for this false positive, the definition has been updated with a note permitting facades.

import EsmSplit.CoreJS.Request.Frame

/-!
# Reachability frames for host calls and global observations

These operations take the world's actual next private state. The model passes no object handles to a host call,
and host results and ambient references carry no heap pointers.
-/

namespace CoreJS

variable {W : World}

theorem reachFrame_fromWorld (r R cells A) (result : HostResult) :
    ReachFrame r R cells A (SafeVal A) (fun s : State W => fromWorld s result) := by
  constructor
  · intro s H C _ _
    cases result <;> rfl
  · intro s _ _
    cases result <;> exact Preserves.refl r R s
  · intro s hs _
    cases result with
    | ret v | throw v =>
      exact ⟨by cases v <;> simp [SafeVal, Primitive.val, Val.nodes], hs⟩
    | error => exact ⟨by simp [SafeVal, Val.nodes], hs⟩
    | unknown => exact hs

/-- A world observation reads only the private state and leaves the handle table alone. Its
decoder runs on the actual state returned by that observation. -/
private theorem reachFrame_hostObservation (r R cells A) {γ : Type}
    (observe : W.σ → Option (γ × W.σ)) (decode : γ → M W Val)
    (hdecode : ∀ value, ReachFrame r R cells A (SafeVal A) (decode value)) (missing : String) (m : M W Val)
    (evaluates : ∀ s, m s = match observe s.world.state with
      | some (value, state) => decode value { s with world := { s.world with state } }
      | none => .unknown missing s) :
    ReachFrame r R cells A (SafeVal A) m := by
  constructor
  · intro s H C hs hr
    rw [evaluates, evaluates]
    change (match observe s.world.state with
      | some (value, state) => decode value { (s.put r H C) with world := { s.world with state } }
      | none => .unknown missing (s.put r H C)) = _
    cases h : observe s.world.state with
    | none => rfl
    | some result =>
      rcases result with ⟨value, state⟩
      exact (hdecode value).replace { s with world := { s.world with state } } H C
        (hs.world _ hs.handed) hr
  · intro s hs hr
    rw [evaluates]
    split
    · rename_i value state _
      let t := { s with world := { s.world with state } }
      have hupdate : Preserves r R s t := ⟨rfl, rfl, fun _ _ => rfl, rfl⟩
      exact Res.Preserves.trans hupdate ((hdecode value).preserves t (hs.world _ hs.handed) hr)
    · exact Preserves.refl r R s
  · intro s hs hr
    rw [evaluates]
    split
    · rename_i value state _
      exact (hdecode value).accessible { s with world := { s.world with state } } (hs.world _ hs.handed) hr
    · exact hs

theorem reachFrame_hostCall (r R cells A) (name : String) (this : Val) (args : List Val) :
    ReachFrame r R cells A (SafeVal A) (hostCall name this args : M W Val) := by
  unfold hostCall
  split
  · exact reachFrame_unknown r R cells A _ _
  · rename_i this args _
    exact reachFrame_hostObservation r R cells A (fun state => some (W.step name this args state))
      (fun result s => fromWorld s result) (reachFrame_fromWorld r R cells A) "" _ (fun _ => rfl)

theorem reachFrame_observeGlobalTypeof (r R cells A) (name : String) :
    ReachFrame r R cells A (SafeVal A) (observeGlobalTypeof name : M W Val) := by
  apply reachFrame_hostObservation r R cells A (W.globalTypeof name) (fun result s => fromWorld s result)
    (reachFrame_fromWorld r R cells A) s!"unsupported host typeof {name}"
  intro s
  unfold observeGlobalTypeof
  cases W.globalTypeof name s.world.state with
  | none => rfl
  | some result => cases result; rfl

theorem reachFrame_readHostRef (r R cells A) (ref : HostRef) :
    ReachFrame r R cells A (SafeVal A) (readHostRef ref : M W Val) := by
  let name := match ref with
    | .binding name => name
    | .property receiver key => s!"{receiver}.{key}"
  apply reachFrame_hostObservation r R cells A (W.hostRef ref) (fun value => pure value.val)
    (fun value => reachFrame_pure r R cells A value.val (by cases value <;> simp [SafeVal, HostValue.val, Val.nodes]))
    s!"unsupported host reference {name}"
  intro s
  unfold readHostRef
  cases W.hostRef ref s.world.state with
  | none => rfl
  | some result => cases result; rfl

end CoreJS

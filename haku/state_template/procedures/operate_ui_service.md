# Operate & evolve my UI service

The operator's interface to me is **my own UI service** — arbitrary, git-backed software I
own end-to-end, not a renderer baked into ducktape (base → _Your own UI service_).
"Operating it is half the job"; it's my **running-self garden**, co-equal with the
knowledge garden in `memory/`. Keep my UI's operational state, pipeline, and hard-won gotchas
in `memory/haku-ui.md` (create it as I learn them). This procedure is the **routine** — what I
do with it every run, and the bar for changing it.

## Standing pass — every run (cheap when healthy)

- **Evolution review — NOT optional.** Every pass, include thinking about whether to improve /
  update / add a function to the UI: it can take many forms and should take the one that's best
  at making things go well. Look at what's actually in front of the operator right now — the live
  items, the near deadlines, the active threads, the click-stream — and ask: **what form or new
  function would serve them best?** Then act: **build it** when the platform's healthy, else
  **queue a concrete entry** in a surface-evolution backlog under `plans/` and ship the
  highest-value one next. The bar is operator-effort-removed-per-click.
- **Health check.** Confirm the `haku-ui` Deployment is `1/1 Running` on the expected image and
  serving (`kubectl get pods -n <agent-namespace> -l app=haku-ui`; `kubectl logs` for a clean
  uvicorn start; a 302 from the Authentik-gated public URL is the redirect, healthy). A crashlooping
  pod or stale image is **self-inflicted and mine to fix before the run is done** — it's the only
  window the operator has into me.
- **Reduce the two-way channel.** The backend commits operator `responses/` (affordance input) and
  `intake/` (feedback notes) as they happen; folding those back is already part of the run. Treat
  **which affordances get used (and which never do)** as feedback on the UI itself, not just on what
  I surfaced — promote what's used, retire what's dead.
- **Tidy the namespace.** Delete throwaway probe pods/jobs (pod quota); reconcile what's actually
  running against what `k8s/` declares.

## Drive every change all the way to running in prod

**A change is not done when CI is green — it's done when it's live in the prod deployment and
verified serving.** CI-green != landed: a green build whose data was silently wrong still ships a
broken page. So after **every** change, follow it to running + prove it; don't hand off to "Flux
will get to it eventually."

**Two paths, by what changed:**

- **Code change** (`ui/**`, Dockerfile, workflow) → needs a rebuild + rollout:
  1. **Build** — poll the Forgejo Actions API until `test` then `build` are `success`. On
     failure, diagnose from run duration + the runner pod's status/events (I can't read the
     Forgejo Actions runner logs). My source (Dockerfile/workflow/app) → fix and re-push;
     runner/infra (operator-owned) → surface a finding, I can't patch.
  2. **Image automation** — confirm the `haku-ui` **ImagePolicy** `status.latestImage` resolved
     to the new tag and **ImageUpdateAutomation** pushed the `[skip ci]` bump commit
     (`kubectl get imagepolicy,imageupdateautomation -n <agent-namespace>`). A Forgejo→Flux image
     webhook pokes the `ImageRepository` scan **off-cycle on push**, so the new tag usually
     appears within seconds; if the webhook isn't wired the periodic scan (minutes) is the
     fallback.
  3. **Rollout** — wait for the workloads `Kustomization` `lastAppliedRevision` to reach that
     bump commit (`kubectl get kustomization -n flux-system <haku-workloads>`; I can't force a
     reconcile — Flux polls, ~1-2 min), then Deployment image == new tag and a fresh pod is
     `Running` (`kubectl rollout status deploy/haku-ui -n <agent-namespace>`).
- **Data change** (`items/`, `responses/`, `memory/improvements/`, anything the backend reads live
  from Forgejo) → **NO rebuild/rollout**: the running pod re-reads it from branch HEAD on each
  request. Just push, then verify. (CI doesn't even trigger — not under `ui/`.)

**Then PROVE it's serving** (both paths): exec the live pod and hit the affected endpoint, don't
assume:

```bash
POD=$(kubectl -n <agent-namespace> get pods -l app=haku-ui -o jsonpath='{.items[0].metadata.name}')
kubectl -n <agent-namespace> exec "$POD" -- python3 -c "import urllib.request,json; \
  print(json.load(urllib.request.urlopen('http://localhost:8080/api/meta'))['scan_time'])"
```

Check the actual new behavior/data (the counts, the new field, the new tab's payload), not just
`/healthz`. Only when the live response matches intent is the change done.

**Commit only source** — no `dist/`, no `node_modules/`. **Refactor to enable future work**:
extract a typed API layer before the tangle costs me; bake repeated rituals into scripts.

## Evolving the surface — the bar

The surface's purpose is identical to mine: **make this person's life as good as it can be.** So
every change is judged by _operator effort removed per click_, and the UI should become more
_this person's_ over time. Principles (base → _Your own UI service_):

- **One-click approval of pre-done work, not display of information.** The UI is where a dreaded
  multi-hour chore arrives as a one-click yes — inline links, action buttons, `claude_handoff`
  deep-links to write-capable agents, pre-composed replies behind compose deep-links. Don't render
  "you have 40 emails about X"; render the recommendation that already read them.
- **Calm by default; escalate only when warranted.** One big card when something is genuinely
  time-critical, the rest collapsed; adapt by who's looking and when.
- **Right medium, not always a list** — reach for a map+route, a co-editor, a capture/photo-drop,
  an elicitation widget that _gathers_ calibration signal, or a simulator **only when it removes
  more operator effort than a card would.** Richness has to earn its complexity.
- **Build bespoke surfaces per the operator's life.** The starter ships two person-agnostic
  surfaces (the items **Inbox** and the **Improvements** self-backlog). Beyond those, add a new tab
  (a `*.tsx`, a backend endpoint, a `View` entry) whenever a recurring part of the operator's life
  deserves its own shape — a shopping/kitchen board, a decision page, a tracker. Those live in this
  operator's `haku-state`, not in the ducktape starter.
- **Privileged actions route through the trusted shell.** Any control I draw is only a _request_;
  the operator's confirm and any real credential live in the console (`openLink` scheme/host-gates).
  I build freely — the perimeter, not my restraint, keeps it safe. I gain **no** new write reach:
  I still never act on the world; I own the surface that frames and hands off the work, and the
  service behind it. `haku-state` remains my only write.

Keep a running surface-evolution backlog under `plans/` and advance it on quiet runs (base → _A
quiet run is still useful_): the click-stream is already in `haku-state`; mine it to decide what
to build next.

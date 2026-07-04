# haku-ui image automation (Haku-owned)

Rolls the CI-built `haku-ui` image into the deployment, as **Haku's own** Flux image
automation — reconciled into `haku` by the workloads pipe (so Haku owns and can
evolve it, like the rest of its state). The cluster's privileged Flux controllers
(image-reflector, image-automation) reconcile these CRs; everything they touch — the
`haku/ui` image, `haku-state` — is already Haku's, so this grants no perimeter widening
(the constrained reconciler SA is widened only for these bounded `image.toolkit` /
`source.toolkit` kinds — **not** `Receiver`, which stays operator-owned).

- `image-repository.yaml` — scans `forgejo.example.com/haku/ui` (private; auth via the
  `haku-forgejo-registry-pull` Secret in `haku`).
- `image-policy.yaml` — newest `main-<utc14>-<sha>` tag, numeric.
- `image-update-automation.yaml` — writes the tag into `k8s/haku-ui/deployment.yaml` (the
  `{"$imagepolicy": "haku:haku-ui"}` marker) and commits it back to haku-state via
  the `haku-state-write` GitRepository.
- `haku-state-write-source.yaml` — authenticated haku-state checkout (the
  `haku-state-git-write` Secret) the automation pushes through.

Push-driven reconcile (on image publish) is provided by an **operator-owned** generic Flux
`Receiver` + Forgejo webhook that references this `haku:haku-ui` ImageRepository
— kept operator-side so Haku can't author a Receiver (a cross-namespace force-reconcile
primitive). Without it, the 5m ImageRepository poll still applies.

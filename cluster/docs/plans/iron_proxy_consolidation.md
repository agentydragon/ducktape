# Consolidating the iron-proxy deployments

Started 2026-08-09 as a probe: the cluster runs three iron-proxy Deployments and
would gain a fourth if `haku-sandbox` moves off mitmproxy — should they share one
definition? Resolved 2026-08-10; the shared definition now exists for the two in
`haku-egress-proxy`.

## What drove it

`public-coder-agent-proxy` — the same image in another namespace — was found
running as root with a mounted ServiceAccount token while both proxies here
carried full hardening (fixed in #3890). Copy-paste divergence, in the field
that matters most, on the most credential-dense of the three.

The duplication was never the problem. Drift was, and duplication is where it
hid.

## What was rejected, and why

**A kustomize Component.** The obvious mechanism, and wrong here twice over.
No `kind: Component` exists anywhere in `cluster/k8s`, and
`cluster/validation/kustomize.py` resolves only `resources`, `patches[].path`
and `configMapGenerator.files` — so component files would read as orphans to
`test_no_orphaned_files` unless the validator learned a new concept. More
fundamentally, a component is included once per kustomization, and both
Deployments live in **one** kustomization; emitting two differently-named
Deployments from one template would have meant splitting this directory into
sub-kustomizations.

**A base + overlays split.** Same directory split, plus renaming gymnastics —
strategic-merge cannot rename, so each overlay needs a JSON6902 `replace
/metadata/name` and label patches at three sites to keep Service selectors
matching.

**A consistency test instead of sharing** (assert the invariant fields are equal
across the iron Deployments, in the spirit of `test_egress_allowlists`). Sound,
and it would have caught the bug — but it detects drift where the patch below
prevents it, for about the same effort.

## What was done

One strategic-merge patch, `iron-proxy-common.yaml`, applied by this namespace's
`kustomization.yaml` through a **name-regex target** that reaches both
Deployments:

```yaml
- path: iron-proxy-common.yaml
  target:
    kind: Deployment
    name: haku-(claude-oauth|openclaw-spike)-proxy
```

No directory split, no new kustomize concept, no validator change — a patch with
a `target` selector already applies to many resources, which is what made the
split unnecessary.

The patch carries the pull secret, `automountServiceAccountToken: false`, the
pod and container `securityContext`, `args`, `resources`, and both volume
mounts. The axes that legitimately differ stay in the per-instance files: name,
labels, listener port, env secrets, config-map name, CA secret.

**Verified output-equivalent**: `kustomize build` before and after is
byte-identical — 24 resources, 1014 lines, zero diff.

**It is not a line-count win.** 179 lines became 176, because a patch re-states
the `spec.template.spec.containers` nesting to reach the fields it sets. The
earlier "~80 lines saved" estimate in this note was wrong. The win is that the
security posture exists once and cannot diverge from itself.

## Still open

- **`public-coder-agent-proxy` is not covered.** It lives in another namespace
  and another flux Kustomization, so this patch does not reach it and its
  hardening is still a second copy — the exact thing that drifted. Either point
  its kustomization at this file by relative path, or add the cross-namespace
  consistency test rejected above. The test is probably right for this case,
  since a cross-directory patch path is its own readability cost.
- **The fourth copy.** When `haku-sandbox` moves off mitmproxy, its iron proxy
  should be added to the target regex rather than written from scratch.
- **Merging the proxy configs.** Operator intent (2026-08-10) is to converge the
  per-instance `iron.yaml` files, and to let `haku-claude-sandbox` eventually
  reach the GitHub token and kube JWT through the same proxy rewrite. That
  relaxes the isolation currently enforced by separate listeners and per-proxy
  CNPs, so it is a deliberate policy change rather than a refactor — sequence it
  on its own, not as a side effect of deduplication.

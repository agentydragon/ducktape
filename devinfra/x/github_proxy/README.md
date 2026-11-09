# GitHub API observation proxy (parked)

Retired 2026-11-09: no cluster or host wiring remains. Mimir showed it inert — the cloud
batch-status block fired only on 2026-09-06/07 (627 requests, wyrm2), wyrm2 sent almost
nothing afterwards, and rugged's last traffic was 2026-10-14.

Layout:

- `central/`: the mitmproxy-based service and its tests.
- `host/`: workstation trust preparation script.
- `nix/`: Home Manager module and pinned public interception CA (not imported by any host).
- `k8s/`: hand-written kustomization inputs and the design README. Generated manifests,
  SOPS client credentials and the Flux image-automation entry were deleted.
- `../../../cluster/cdk8s/x/github_api_proxy/`: cdk8s generator and Flux Kustomization.
- `debug/github_graphql_exhaustion/`: the #5213 investigation.
- `../github_api_capture/`: capture reporting scripts.

Reviving needs: re-add the generator to `cluster/cdk8s/generate_manifests.py`, regenerate the
Flux and image-automation outputs, restore `image_targets.json` and
`cluster/cdk8s/external_secrets/config.py` entries, mint new client credentials, and import
the Nix module in `nix/home/home.nix`.

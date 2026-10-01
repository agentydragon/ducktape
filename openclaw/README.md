# OpenClaw images

`gateway.nix` builds the stable OpenClaw gateway from nix-openclaw's pinned
npm-package and source metadata, then applies the downstream dist patch and
install fixups described there.
**It is the single gateway derivation for both images**, so a change here lands in
both:

- `default.nix` — `git.allegedly.works/ducktape-ci/public-coder-agent`, the public-coder agent image;
  adds the proxy preload, the Matrix and Brave runtime plugins, and the
  command-line toolset.
- `../haku/openclaw_spike/default.nix` — the Haku spike image; adds its own proxy
  preload and tooling.
- `public_coder_agent/devbox/` — the public-coder agent's separate NixOS
  build/test VM and containerDisk recipe; its Kubernetes manifests stay under
  `cluster/k8s/agents/public-coder-agent/devbox/`.

Build either directly:

```bash
nix build .#openclaw-image
nix build .#haku-openclaw-spike-image
```

The OpenClaw release and npm dependency lock follow the pinned nix-openclaw
revision. Review the release-specific dist patch and build both images for each
gateway update.

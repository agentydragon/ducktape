# OpenClaw image

`gateway.nix` builds the stable OpenClaw gateway from nix-openclaw's pinned
npm-package and source metadata, then applies the downstream dist patch and
install fixups described there.
The public-coder agent consumes this gateway derivation:

- `default.nix` — `git.allegedly.works/ducktape-ci/public-coder-agent`, the public-coder agent image;
  adds the proxy preload, the Matrix and Brave runtime plugins, and the
  command-line toolset.
- `public_coder_agent/devbox/` — the public-coder agent's separate NixOS
  build/test VM and containerDisk recipe; its Kubernetes manifests stay under
  `cluster/parked/public-coder-agent/devbox/`.

Build either directly:

```bash
nix build .#openclaw-image
```

The OpenClaw release and npm dependency lock follow the pinned nix-openclaw
revision. Review the release-specific dist patch and build the image for each
gateway update.

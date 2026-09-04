# OpenClaw images

`gateway.nix` builds the stable OpenClaw gateway — nix-openclaw's npm-package
build, spliced with `npm_wrapper/` and repaired by the fixups described there.
**It is the single gateway derivation for both images**, so a change here lands in
both:

- `default.nix` — `ghcr.io/agentydragon/openclaw`, the public-coder agent image;
  adds the proxy preload, the Matrix and Brave runtime plugins, and the
  command-line toolset.
- `../haku/openclaw_spike/default.nix` — the Haku spike image; adds its own proxy
  preload and tooling.

Build either directly:

```bash
nix build .#openclaw-image
nix build .#haku-openclaw-spike-image
```

Regenerate `npm_wrapper/` when moving to a new release:

```bash
npm install openclaw@<ver> --package-lock-only --omit=dev --install-strategy=nested
```

`--install-strategy=nested` is load-bearing; `gateway.nix` explains why.

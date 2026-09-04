# comin — pull-based GitOps for NixOS machines

## Idea

Run [comin](https://github.com/nlewo/comin) (by nlewo, the nix2container author)
on the NixOS VM(s) — starting with `agent-box` — so that a `git push` to the
infra repo automatically reconciles the machine's NixOS configuration, in place,
with no CI step and no manual `nixos-rebuild`.

## Why

This is the mature, purpose-built answer to the property we kept hitting while
designing the codex pod (<../cluster/k8s/agents/x/codex-pod/README.md>): "change
the Nix definition, push, the running thing reconciles itself." At the
k8s-pod + nix-csi layer that needs custom glue (there's no off-the-shelf
Flux-image-automation analog for Nix store paths). At the **NixOS-system** layer,
comin already does it:

- pull mode — an agent runs **on the machine** and polls git, so no CI executes
  build/deploy commands and there's no push access from outside into the box;
- **in-place `nixos-rebuild switch`** on a new commit — no VM/pod restart, so a
  running codex/agent session survives a config change (unlike a pod roll).

The codex-pod exploration was partly an attempt to get _away_ from the VM, but
auto-reconcile-on-push is exactly a VM/comin strength that the pod shape throws
away. Worth doing on the VM regardless of what happens with the pod.

## How it works (grounded in the readme)

- **NixOS module**: `comin.nixosModules.comin` from the flake input; enable with
  `services.comin.enable = true`.
- **Config picks by hostname**: comin deploys
  `nixosConfigurations.<machine-hostname>` from the polled repo.
- **Remotes/branches**:

  ```nix
  services.comin = {
    enable = true;
    remotes = [{
      name = "origin";
      url = "https://git.allegedly.works/…/infra.git";
      branches.main.name = "main";
    }];
  };
  ```

  Polls ~every 60s; a new commit on the tracked branch deploys within ~a minute.

## Features worth remembering

- Flake **and** non-flake repos.
- **Testing branches** — try a change on a testing branch before it hits main
  (safe iteration on a live box).
- **Multiple git remotes** — poll several to avoid a single point of failure
  (e.g. Forgejo + a GitHub mirror).
- **Machine migration** — move a config from one machine to another.
- **Local remotes** — fast local iteration without pushing.
- **System profiles** — creates/deletes profiles → rollback path.
- **Prometheus metrics** exporter → wire into the existing monitoring stack.
- Optional **git commit signature checking** — only deploy signed commits.
- Also supports **nix-darwin**, so the same loop could drive a Mac later.

## Fit / open questions for our setup

- **Which repo does comin poll?** ducktape is the NixOS SSOT
  (`nix/nixos/hosts/agent-box/…`). Point comin at our Forgejo mirror of ducktape
  (+ optionally a second remote to avoid SPOF). Confirm the hostname matches the
  `nixosConfigurations` attr.
- **Auth** to a private repo from the box — see comin's `docs/authentication.md`.
- **Blast radius**: comin auto-switches on push to `main`/`devel`. Consider
  pointing it at a dedicated branch or using signature checking so an unrelated
  `devel` commit doesn't reconfigure the agent box unexpectedly. (This is the
  NixOS analog of the "roll only on relevant change" concern from the pod plan.)
- **Interaction with existing deploy flow**: today NixOS hosts are updated via
  `sudo nixos-rebuild switch` (see <../nix/README.md>). comin would make
  `agent-box` self-updating; decide whether other hosts follow or stay manual.

## References

- Repo: <https://github.com/nlewo/comin> (`readme.md`, `docs/` — howtos,
  advanced-config, authentication, generated-module-options, design).
- Matrix room: `#nixos-comin:matrix.org`.
- Related: <../cluster/k8s/agents/x/codex-pod/README.md> (the pod arc that
  surfaced this).

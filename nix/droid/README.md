# nix-on-droid (Phone)

Declarative Nix environment for Android via
[nix-on-droid](https://github.com/nix-community/nix-on-droid) (a fork of Termux with Nix
in place of Termux's own package manager). Same idea as `nix/home/` and `nix/nixos/` for
desktops: the phone's config is a file in this repo, so a reflash or a new phone is
"reinstall the app, run one command" instead of reconstructing everything by hand.

Currently just `pixel6` (see `nebula-mesh.json` for the device's other identity) with a
single `hello` package as a smoke test — intentionally the minimum that proves the
pipeline works. The actual point of this is wiring the phone into ActivityWatch: see
[cluster/k8s/TODO.md](../../cluster/k8s/TODO.md) for that item and
[cluster/docs/activitywatch/README.md](../../cluster/docs/activitywatch/README.md) for
how it works on desktops today. That isn't here yet — this config will eventually grow a
`ducktape.activitywatch.sync`-equivalent block the same way `nix/home/hosts/atlas.nix`
has one.

## Prerequisites

Install **nix-on-droid** from F-Droid (package `com.termux.nix`) — not the "Linux
Terminal" app (`com.android.virtualization.terminal`, a separate Debian VM via Android's
Virtualization Framework), which may already be installed for other reasons. nix-on-droid
is a Termux fork that runs as an ordinary Android app, sharing the host's network/process
space the normal way — so it can read another app's `127.0.0.1`-bound port, which the
ActivityWatch step will need to pull from aw-android's embedded server. Whether the Linux
Terminal app's VM can do the same is unconfirmed; nothing documented says it can, which is
why this setup doesn't use it.

## Bring-up

Inside the nix-on-droid app's terminal, after its own first-run bootstrap — no local
checkout needed, applies straight from GitHub:

```bash
nix-on-droid switch --flake github:agentydragon/ducktape?ref=devel#pixel6 --impure
```

Iterating on the config itself instead (edit locally, apply, push when it works):

```bash
git clone https://github.com/agentydragon/ducktape ~/ducktape
cd ~/ducktape
nix-on-droid switch --flake .#pixel6 --impure
```

`--impure` is required by nix-on-droid itself (hardcoded proot store paths), not specific
to this config.

## Verify

```bash
hello
# Hello, world!
```

## Adding a device

New `nix/droid/hosts/<name>.nix` plus a matching `nixOnDroidConfigurations.<name>`
entry in the root `flake.nix` (see `pixel6` for both).

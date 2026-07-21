# Can we restrict the haku-ci runner's GitHub egress to specific repos?

**Date:** 2026-07-21. **Context:** haku-ci CI-performance work; revisiting why the Bazel
`ducktape_haku` dependency uses an in-cluster Forgejo mirror instead of fetching GitHub
directly, and whether the egress proxy could allow only specific GitHub repos.

## Short answer

**Technically feasible, low value.** `haku-egress-proxy` is **mitmproxy** and ssl-bumps
`github.com`, so it can see the URL path and _could_ enforce a per-repo allowlist via an
addon — but a Bazel build legitimately fetches source from a large, shifting set of GitHub
orgs, so a repo allowlist would be brittle and only marginally tighter than the current
host-level allow.

## What was tested (live)

A throwaway pod in `haku-sandbox` routed through `haku-egress-proxy`, inspecting the TLS
handshake to `github.com`:

```text
subject: CN=github.com
issuer:  CN=haku-egress-proxy-root-ca      # the proxy's own CA, not GitHub's
< HTTP/2 200                                # git refs returned fine
github.com/bazelbuild/rules_python -> HTTP 200
```

- **GitHub is ssl-bumped, not tunnelled** — the leaf presented for `github.com` is issued by
  `haku-egress-proxy-root-ca`, so mitmproxy terminates the TLS and sees the full decrypted
  request (path included). Per-path/per-repo rules are therefore possible **at the proxy**
  (a mitmproxy addon inspecting `flow.request.path`, 403-ing the rest). The **Cilium FQDN**
  layer cannot — `toFQDNs: matchName: github.com` is all-or-nothing.
- **Non-`agentydragon` repos already pass** (`bazelbuild/rules_python` → 200): today's policy
  is host-level, no repo restriction.

## Why a repo allowlist is low-ROI

The build pulls GitHub source from many orgs — from `MODULE.bazel.lock` alone: `bazelbuild`,
`JetBrains`, `astral-sh`, `google`, `pinterest`, plus `agentydragon/rules_mypy` — and that's
an undercount (BCR module source archives resolve at fetch time). The set **changes on every
dependency / BCR bump**, so a repo allowlist is a moving target that silently breaks builds
when a new transitive dep appears. And `github.com` is one of ~a dozen allowed egress hosts
(npm, PyPI, nixos, ghcr, nodejs…), so path-locking GitHub alone doesn't meaningfully shrink a
prompt-injected build's exfil surface.

## Corollary: the in-cluster ducktape mirror is convention, not an access gate

The `ducktape_haku` `git_override` fetches from the in-cluster Forgejo mirror
(`forgejo-http.forgejo:3000/haku/ducktape.git`), which had been assumed to be about avoiding a
GitHub whitelist. It isn't:

- `github.com` is **already** in the egress allowlist (the BCR rulesets need it), and
  **ducktape is public** on GitHub — so the runner could fetch it directly with no credential
  and no allowlist change.
- The mirror exists for the **agent convention** (`tf/gitops/forgejo-agentydragon-repos`):
  all agents read the operator's repos from in-cluster Forgejo mirrors and propose changes via
  AGit PRs. It buys consistency + an in-cluster posture + determinism, **not** access control.

So if simplicity is ever wanted, the `git_override` can point at `github.com/agentydragon/
ducktape.git` and drop the mirror dependency + `DUCKTAPE_MIRROR_READ_TOKEN`. The heavier
efficiency lever is unrelated: that `git_override` clones the whole ~420 MB ducktape repo just
to `strip_prefix` out `haku/shared` (an `archive_override` or splitting `haku/shared` into its
own repo would slim that).

## Verdict

Host-level `github.com` allow is the pragmatic point on the curve. A per-repo mitmproxy addon
is possible but not worth the maintenance for the containment it buys; if tighter egress is a
real goal, the higher-leverage move is the opposite — mirror specific deps in-cluster and deny
GitHub from the runner entirely (heavy: implies self-hosting a BCR/git mirror).

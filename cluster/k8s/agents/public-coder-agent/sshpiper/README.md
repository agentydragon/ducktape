# public-coder-agent sshpiper

A terminating SSH bastion between the OpenClaw Agent Pod and its devbox VM
(<../devbox/>), running [sshpiper](https://github.com/tg123/sshpiper) with the `kubernetes`
plugin. Routes are `Pipe` resources; the CRD lives in <../../../sshpiper-crds/>.

Same credential split as <../proxy/>, applied to SSH: the Agent authenticates here with a key
that is worthless anywhere else, and this Pod holds the key that opens `coder@public-coder-devbox`.

```text
Agent Pod ──2222──▶ sshpiper ──22──▶ public-coder-devbox
 downstream key      mapping key      user: coder
```

**Deviation from the HTTP proxy's model:** iron-proxy substitutes a credential mid-stream. SSH
cannot be done that way — a publickey signature covers the session id, so nothing that merely
relays bytes can swap the key. sshpiper terminates the downstream session and re-originates the
upstream one with its own "mapping key", which is why the Agent pins _this_ Pod's host key rather
than the devbox's.

## Keys

Four, all ed25519, none shared with anything else:

| Key                        | Private half lives in                             | Public half                                                                 |
| -------------------------- | ------------------------------------------------- | --------------------------------------------------------------------------- |
| Agent → piper (downstream) | Agent Pod, from `../app/devbox-ssh-key.sops.yaml` | `authorized_keys_data` in the Pipe                                          |
| piper host key             | `host-key.sops.yaml`, this Pod only               | Agent's `known_hosts`                                                       |
| piper → devbox (mapping)   | `devbox-key.sops.yaml`, this Pod only             | `coder`'s `authorized_keys` in `nix/nixos/hosts/public-coder-devbox`        |
| devbox host key            | `<../devbox/ssh-host-key.sops.yaml>`, the VM only | `ssh_keys/public-coder-devbox-host.pub`, and `known_hosts_data` in the Pipe |

Rotating either host key breaks the pin that trusts it, so the pin moves in the same change.
Rotating the mapping key means updating the NixOS `authorized_keys` and rebuilding the VM image.

`known_hosts_data` is not optional dressing: leave it empty and sshpiper does not verify the
upstream host key at all. Both `_data` fields are base64.

The devbox offers exactly one host key type on purpose. sshpiper verifies whichever type it
negotiates and fails with a bare `Permission denied (publickey)` when that type is missing from
`known_hosts_data` ([tg123/sshpiper#554](https://github.com/tg123/sshpiper/issues/554)).

## Recordings

`SSHPIPERD_SCREEN_RECORDING_*` writes asciicast files to the PVC, one directory per connection:

```bash
kubectl exec -n public-coder-agent deploy/public-coder-agent-sshpiper -- ls /recordings
asciinema play /recordings/<conn_guid>/shell-channel-0.cast
```

**Gotcha:** this captures screen output, so it covers PTY sessions and misses a non-PTY
`ssh devbox <cmd>`. It is therefore not yet an equivalent of the `node_daemon_executions` row
`hostexec` writes per command, and the two doors are not yet interchangeable from an audit
standpoint. Nothing prunes the directory either.

## Known gaps

- **Nothing fences the devbox's own port 22.** No policy selects that endpoint, so Cilium
  default-allows ingress to it and the piper is the Agent's only route only because the Agent's
  `../app/networkpolicy-egress.yaml` says so. A CiliumNetworkPolicy on
  `kubevirt.io/domain: public-coder-devbox` would close the rest of the cluster out, and has to be
  written without breaking the Operator's `kubectl port-forward` (which arrives as node, not Pod,
  identity).
- **Recordings are unpruned and PTY-only**, as above.

## Adding a route

A `Pipe` in this namespace, plus an upstream key Secret of type `kubernetes.io/ssh-auth`. The
plugin runs without `--all-namespaces`, so only this namespace's Pipes are ever served.

```bash
kubectl get pipes -n public-coder-agent
```

The piper's ServiceAccount can read every Secret in the namespace — unavoidable, since a Pipe
names its upstream key by reference — which is why it runs as its own workload rather than as a
container in the proxy Pod that holds the GitHub PAT and Console bearer.

## Bumping the image

The image tag and `../../../sshpiper-crds/crd-pipes.yaml` are vendored from the same upstream tag;
move them together.

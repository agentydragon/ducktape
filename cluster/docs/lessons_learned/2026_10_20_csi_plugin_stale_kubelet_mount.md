# A CSI node plugin that predates a `/var/lib/kubelet` remount gives Pods bare directories

## Failure mechanism

On wyrm2 the OpenEBS LVM node plugin mounts each LV itself, in `NodePublishVolume`, at the Pod's
target path under `/var/lib/kubelet/pods/<uid>/volumes/kubernetes.io~csi/<pv>/mount`. Its container
reaches that tree through a `/var/lib/kubelet` hostPath with `mountPropagation: Bidirectional`.

The plugin Pod started at 2026-10-15 22:50Z. At 23:48Z `/var/lib/kubelet` became its own filesystem
(`/dev/vdl`, [#10634](https://github.com/agentydragon/ducktape/pull/10634) and
[#10661](https://github.com/agentydragon/ducktape/pull/10661)): systemd unmounted the old Pod
volume directories and mounted the new disk over `/var/lib/kubelet`. Neither the cutover comment nor
the PRs said to restart the CSI plugins.

The Ollama Pod kept working on its earlier private mount until the wyrm2 flap at 2026-10-20 03:14Z
evicted it. Its replacement was the first Pod to need a new mount from the plugin:

- The plugin logged `NodePublishVolume` success at 03:23:21Z, and the `llm-models` LV
  (`openebs-proxmox-hdd/pvc-0552e5a0-…`) was mounted at the target path, but only inside the plugin
  container's mount namespace (`/proc/<plugin pid>/mountinfo`). The host's mount namespace had no
  mount for it.
- Kubelet took the success at face value and bind-mounted the target path into the Pod: a plain
  directory on `/dev/vdl`. `link-ssd-models.sh` ran `mkdir -p /models/blobs` there at 03:23:22Z and
  linked the SSD weights, so the Pod went Ready with `/models` holding only `blobs/`.
- Ollama answered `404 model '…' not found` for every model; LiteLLM put the deployments into
  cooldown (429 for the Qwen routes).

The models were intact on the LV (registry present, 83.6% used). Why the plugin's mounts missed the
host is inferred, not observed: the plugin's hostPath bind kept pointing at the `/var/lib/kubelet`
directory as it was before the new disk was mounted over it. Restarting the plugin cured it.

Nothing in the stack objects to a bare directory. Kubelet does not check that a published volume's
target is a mountpoint, and inside the container `/models` is a bind mount, so `mountpoint -q`
passes too. The only tell is that `/models` sits on the kubelet disk (`df /models` showed
`/dev/vdl`, not the LV).

## Recovery

1. Delete the plugin Pod (`openebs/openebs-lvm-localpv-node-*` on the node); the replacement starts
   with the host's current `/var/lib/kubelet`.
2. Delete the Ollama Pod (strategy `Recreate`) so it publishes the volume again.

Models and the registry came back with no re-download. Afterwards a Codex turn on `agentplane-testing`
with `ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k` completed and Ollama logged a 200.

Do not re-run the setup Job (`setup-gpt-oss-v9`) to repair this: on a bare directory it pulls about
115 GB onto the kubelet disk instead of the LV.

## Whenever `/var/lib/kubelet` is remounted

Restart every CSI node plugin on that node (`openebs-lvm-localpv-node`, and any other DaemonSet
with a `/var/lib/kubelet` hostPath) before Pods that need new volumes are scheduled there. To check
a Pod's volume, look for its target path in the host's mounts (`grep <pv-name> /proc/1/mountinfo`
on the node); a missing line means the Pod is on a bare directory.

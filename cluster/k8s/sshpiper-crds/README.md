# sshpiper-crds

`Pipe` (`sshpiper.com/v1beta1`), the routing resource the
[sshpiper](https://github.com/tg123/sshpiper) `kubernetes` plugin watches. Vendored verbatim from
upstream's `plugin/kubernetes/crd.yaml` at the tag the deployed image is pinned to, so the schema
and the binding reading it move together.

Cluster-scoped and separate from any one consumer: the CRD outlives individual sshpiperd
deployments, and pruning it would delete every route in the cluster. The only consumer today is
<../agents/public-coder-agent/sshpiper/>.

Bumping the image means re-fetching this file at the new tag:

```bash
curl -sS -o cluster/k8s/sshpiper-crds/crd-pipes.yaml \
  https://raw.githubusercontent.com/tg123/sshpiper/<tag>/plugin/kubernetes/crd.yaml
```

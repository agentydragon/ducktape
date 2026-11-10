# VPA admission webhook with no endpoints

**Observed:** During the 2026-06-10 control-plane I/O investigation, the VPA
MutatingWebhookConfiguration remained registered while the admission-controller
Deployment had zero available replicas. API-server logs showed immediate
`operation not permitted` failures when Cilium rejected requests to the service
with no endpoints. The original investigation treated these as log noise, not as
the cause of the etcd latency.

**Current source:** `cluster/cdk8s/vpa.py` enables the VPA admission controller,
and `cluster/generated/vpa/vpa.k8s.yaml` is the generated HelmRelease. Neither
proves whether the webhook currently has ready endpoints or how the chart manages
its webhook configuration during an unavailable interval.

**Open question:** If the admission controller has no ready endpoints again,
confirm whether the API-server webhook configuration is removed or whether
admission requests still fail. Check the live Deployment, Service endpoints, and
MutatingWebhookConfiguration together before deciding whether a source change is
needed.

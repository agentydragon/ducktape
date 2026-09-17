# cdk8s generators: don't reach for raw ApiObject

Full design and worked examples: <../docs/cdk8s.md>.

**Never default to `cdk8s.ApiObject` + `JsonPatch` for a whole resource just because
the first typed builder you reach for doesn't cover it.** Before writing one:

1. **Core Kubernetes type** (`Namespace`, `Deployment`, `Service`, `ConfigMap`,
   `ServiceAccount`, `Role`, `Job`, ...): check whether `cdk8s_plus_33` already has a
   typed builder — it covers far more than the handful used so far. Don't assume one
   is missing; import it and try it, or grep its module for the class name, before
   falling back to anything else.
2. **CRD type** (anything with its own `apiVersion` group like `external-secrets.io`,
   `monitoring.coreos.com`, `gateway.networking.k8s.io`): set up real typed bindings
   via `cdk8s_import` (`devinfra/js/cdk8s_import.bzl`), the same way
   `//third_party/flux:kustomization`, `//third_party/prometheus_operator:servicemonitor`,
   `//third_party/gateway_api:httproute`, and `//third_party/external_secrets:externalsecret`
   already do: fetch the upstream CRD YAML verbatim via an `http_file` in
   `MODULE.bazel`, add a `third_party/<name>/BUILD.bazel` calling `cdk8s_import`,
   import the generated dataclasses. This is normal, expected effort for a new CRD,
   not a fallback path — it gets you real schema validation (synth-time errors on a
   bad field) instead of a raw dict that only fails at `kubectl apply` time, if it
   fails at all.

**The one legitimate use of the raw escape hatch** is patching a single field a typed
builder is missing on an object that's otherwise typed — never the whole resource.
Build the resource with its typed constructor, then reach into the specific
already-typed construct: `ApiObject.of(construct).add_json_patch(JsonPatch.add(path,
value))` (`cdk8s_plus_33` non-`ApiObject` constructs like `Deployment` manage one
internally) or `.add_json_patch(...)` directly (`cdk8s.ApiObject` subclasses, e.g.
CRD-generated classes). Example: `Deployment`'s `topologySpreadConstraints`
(`litellm_constructs.py`) — no typed builder exists for it (only an all-or-nothing
`spread: bool` auto-toggle), so it keeps the typed `Deployment` constructor for every
other field and patches only that one in.

A raw `ApiObject` replacing an entire resource is a shortcut that throws away real
validation for the whole object to avoid the CRD-import setup cost. Do the setup.

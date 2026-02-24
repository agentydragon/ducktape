# Legacy Helm Charts (Pending Migration)

These charts remain from the old k3s cluster. Each contains functionality not yet
reproduced in `cluster/k8s/` under Flux GitOps.

## Charts

### `ember/`

Agent with Matrix, rspcache, and Gitea PAT integration. Not yet migrated.

### `gitea/`

Wraps the upstream Gitea chart. Kept for the ember bootstrap Job (`job-ember-bootstrap.yaml`)
which provisions the `ember-bot` user and generates a PAT stored as a Kubernetes secret.

### `guacamole/`

Apache Guacamole remote desktop gateway. Not yet migrated.

### `matrix-stack/`

Umbrella chart for Matrix Synapse. Kept for ember-bot user provisioning not yet in the new cluster.

### `rspcache/`

OpenAI response cache proxy with admin dashboard and backing PostgreSQL. Not yet migrated.

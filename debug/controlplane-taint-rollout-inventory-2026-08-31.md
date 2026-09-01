# Control-plane taint rollout inventory

**Tracking:** GitHub #5361  
**Captured:** 2026-08-31T19:23:09-0700 local (2026-09-01T02:23:09Z UTC)  
**Source revision:** f3dd8b989c411ab45a5cef03b38d4c41f8ad9ba4 / f3dd8b98 2026-08-31 19:10:27 -0700 docs(cluster): track control-plane taint rollout (#5362)  
**Status:** preflight snapshot; refresh immediately before every rollout step

This is a metadata-only live snapshot. It does not authorize or perform taints,
drains, PV deletion, Talos changes, or workload migration.

## Decision

**Do not apply the all-control-plane taint yet.** The inventory is sufficient to
start owner-by-owner pre-taint work, but the rollout gate is currently HOLD:

- only `ovh-ns103656` carries `node-role.kubernetes.io/control-plane:NoSchedule`;
  `ovh-ns104952` and `ovh-ns104963` do not;
- a Pending `haku-openclaw-spike` VolSync pod exists;
- Flux has direct non-ready resources and/or dependency cascades recorded below;
- many current CP residents have no explicit control-plane toleration and need an
  owner decision before a restart or controller rollout.

The intended end state remains declarative Terraform/Talos
`allowSchedulingOnControlPlanes = false` for every control-plane configuration
path. `NoSchedule` must be used; never turn this into `NoExecute`.

## Control-plane and worker state

| Node | Role | Ready | Unschedulable | Current taint | All observed pod count | CP-pinned PV count |
| --- | --- | --- | --- | --- | ---: | ---: |
| ovh-ns103656 | control-plane | True | false | node-role.kubernetes.io/control-plane=:NoSchedule | 25 | 7 |
| ovh-ns104952 | control-plane | True | false | none | 81 | 10 |
| ovh-ns104963 | control-plane | True | false | none | 92 | 7 |

Worker and other node state:

```text
ovh-ns103656	control-plane	True	false	node-role.kubernetes.io/control-plane=:NoSchedule
ovh-ns104952	control-plane	True	false	
ovh-ns104963	control-plane	True	false	
ovh-ns102453	worker	True	false	
ovh-ns103711	worker	True	false	
optiplex	worker	True	false	
wyrm2	worker	True	false	
rugged	worker	True	false	node-role.kubernetes.io/roaming=true:NoSchedule
iguana	worker	Unknown	false	node-role.kubernetes.io/roaming=true:NoSchedule,node.kubernetes.io/unreachable=:NoSchedule,node.cilium.io/agent-not-ready=:NoSchedule,node.kubernetes.io/unreachable=:NoExecute
```

Worker capacity snapshot (capacity and allocatable):

```text
ovh-ns102453	8	32523952Ki	7950m	32028336Ki
ovh-ns103711	8	32523040Ki	7950m	32027424Ki
optiplex	6	16147040Ki	5950m	15651424Ki
wyrm2	32	98800756Ki	32	98698356Ki
rugged	8	32416880Ki	8	32314480Ki
```

Current metrics-server view:

```text
NAME           CPU(cores)   CPU(%)      MEMORY(bytes)   MEMORY(%)   
optiplex       1645m        27%         6022Mi          39%         
ovh-ns102453   1752m        22%         12747Mi         40%         
ovh-ns103656   1277m        16%         7261Mi          23%         
ovh-ns103711   3781m        47%         15560Mi         49%         
ovh-ns104952   2862m        36%         20880Mi         32%         
ovh-ns104963   1308m        16%         19033Mi         30%         
wyrm2          638m         1%          25590Mi         26%         
rugged         <unknown>    <unknown>   <unknown>       <unknown>   
iguana         <unknown>    <unknown>   <unknown>       <unknown>
```

## Full pod inventory

Columns are: node, namespace, pod, owner kind, owner, phase,
persistent-volume claims, explicit control-plane toleration, node selectors.

A `no` toleration is not automatically a migration decision: it means the pod
currently depends on the CP being untainted if it is restarted or rescheduled.
Completed/failed historical Pods remain in this full inventory but are excluded
from the first-pass action queues below.

```text
ovh-ns103656	clickhouse	chk-clickhouse-keeper-keeper-0-0-0	StatefulSet	chk-clickhouse-keeper-keeper-0-0	Running	keeper-data-chk-clickhouse-keeper-keeper-0-0-0	yes	storage.allegedly.works/tier=hdd;topology.kubernetes.io/zone=hil-ovh
ovh-ns103656	haku-mailbox	haku-mailbox-smtp-ingress-f74pv	DaemonSet	haku-mailbox-smtp-ingress	Running		no	topology.kubernetes.io/region=hil
ovh-ns103656	headlamp	headlamp-8bbff9d5f-6gj6g	ReplicaSet	headlamp-8bbff9d5f	Running		yes	topology.kubernetes.io/region=hil
ovh-ns103656	kube-system	cilium-c7fc5	DaemonSet	cilium	Running		no	kubernetes.io/os=linux
ovh-ns103656	kube-system	cilium-envoy-mmlwf	DaemonSet	cilium-envoy	Running		no	kubernetes.io/os=linux
ovh-ns103656	kube-system	coredns-75d98bc87d-w8vvn	ReplicaSet	coredns-75d98bc87d	Running		yes	kubernetes.io/os=linux
ovh-ns103656	kube-system	kube-apiserver-ovh-ns103656	Node	ovh-ns103656	Running		no	
ovh-ns103656	kube-system	kube-controller-manager-ovh-ns103656	Node	ovh-ns103656	Running		no	
ovh-ns103656	kube-system	kube-scheduler-ovh-ns103656	Node	ovh-ns103656	Running		no	
ovh-ns103656	kube-system	talos-cloud-controller-manager-865f94896d-rxnvp	ReplicaSet	talos-cloud-controller-manager-865f94896d	Running		yes	node-role.kubernetes.io/control-plane=
ovh-ns103656	kubevirt	virt-operator-7c7c8d8867-4jmlb	ReplicaSet	virt-operator-7c7c8d8867	Running		yes	topology.kubernetes.io/region=hil
ovh-ns103656	loki	loki-canary-94znb	DaemonSet	loki-canary	Running		no	
ovh-ns103656	loki	promtail-725b5	DaemonSet	promtail	Running		no	
ovh-ns103656	monitoring	alertmanager-monitoring-0	StatefulSet	alertmanager-monitoring	Running	db-alertmanager-monitoring-0	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns103656	monitoring	prometheus-node-exporter-72wsw	DaemonSet	prometheus-node-exporter	Running		no	kubernetes.io/os=linux
ovh-ns103656	node-feature-discovery	node-feature-discovery-worker-s7lg8	DaemonSet	node-feature-discovery-worker	Running		no	
ovh-ns103656	plaid-mcp	plaid-mcp-db-1	Cluster	plaid-mcp-db	Running	plaid-mcp-db-1	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns103656	seaweedfs	public-s3-74cbd95966-gbpmr	ReplicaSet	public-s3-74cbd95966	Running		yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns103656	seaweedfs	seaweedfs-master-2	StatefulSet	seaweedfs-master	Running		yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns103656	seaweedfs	seaweedfs-s3-75c6547846-mqs4l	ReplicaSet	seaweedfs-s3-75c6547846	Running		yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns103656	seaweedfs	seaweedfs-volume-hdd-2	StatefulSet	seaweedfs-volume-hdd	Running	mount0-seaweedfs-volume-hdd-2	yes	storage.allegedly.works/tier=hdd
ovh-ns103656	seaweedfs-csi-system	seaweedfs-csi-driver-controller-6c54c47bb-fw8dv	ReplicaSet	seaweedfs-csi-driver-controller-6c54c47bb	Running		yes	
ovh-ns103656	seaweedfs-csi-system	seaweedfs-csi-driver-mount-cr6sn	DaemonSet	seaweedfs-csi-driver-mount	Running		yes	
ovh-ns103656	seaweedfs-csi-system	seaweedfs-csi-driver-node-4542f	DaemonSet	seaweedfs-csi-driver-node	Running		yes	
ovh-ns103656	vector-talos-logs	vector-talos-logs-rpmds	DaemonSet	vector-talos-logs	Running		no	node-vendor=talos
ovh-ns104952	agent-workspaces	codex-h7z5m	Sandbox	codex-h7z5m	Running	workspace-codex-h7z5m	no	topology.kubernetes.io/region=hil;topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	airlock	airlock-55f7f7c94b-lgf8l	ReplicaSet	airlock-55f7f7c94b	Running		no	
ovh-ns104952	atuin	atuin-db-4	Cluster	atuin-db	Running	atuin-db-4	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	authentik	authentik-db-ovh-4	Cluster	authentik-db-ovh	Running	authentik-db-ovh-4	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	budget	fava-69d6d5b69b-t78jn	ReplicaSet	fava-69d6d5b69b	Running		no	
ovh-ns104952	cdi	cdi-deployment-5fdf9578fb-mqth7	ReplicaSet	cdi-deployment-5fdf9578fb	Running		no	topology.kubernetes.io/region=hil
ovh-ns104952	cdi	cdi-operator-6c9cdddcb-284xx	ReplicaSet	cdi-operator-6c9cdddcb	Running		no	topology.kubernetes.io/region=hil
ovh-ns104952	cli-proxy-api	aiquota-api-6c46d68c47-v9g8m	ReplicaSet	aiquota-api-6c46d68c47	Running		no	
ovh-ns104952	default	kubeapi-proxy-74bbc75fb6-x6646	ReplicaSet	kubeapi-proxy-74bbc75fb6	Running		no	
ovh-ns104952	docker-ci	docker-ci-5b4768d6bf-hbp7b	ReplicaSet	docker-ci-5b4768d6bf	Running		no	topology.kubernetes.io/region=hil
ovh-ns104952	external-secrets-system	external-secrets-6f86f5486f-vqgtn	ReplicaSet	external-secrets-6f86f5486f	Running		no	
ovh-ns104952	forgejo	forgejo-db-ssd-2	Cluster	forgejo-db-ssd	Running	forgejo-db-ssd-2	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	forgejo	forgejo-valkey-ovh-1	StatefulSet	forgejo-valkey-ovh	Running	forgejo-valkey-ovh-forgejo-valkey-ovh-1	yes	
ovh-ns104952	gatus	gatus-6898d65d56-vqzvs	ReplicaSet	gatus-6898d65d56	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	goldilocks	goldilocks-dashboard-7c588f9bfd-zbrjh	ReplicaSet	goldilocks-dashboard-7c588f9bfd	Running		no	
ovh-ns104952	grocy-sf	grocy-mcp-server-6b44c5c678-7p9hm	ReplicaSet	grocy-mcp-server-6b44c5c678	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	grocy-vallejo	grocy-mcp-server-64f9b6495c-nxsrb	ReplicaSet	grocy-mcp-server-64f9b6495c	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	ha-mcp	ha-mcp-67f877b4b-tj9sh	ReplicaSet	ha-mcp-67f877b4b	Running		no	
ovh-ns104952	haku-console	haku-console-654fb4447b-t6s4p	ReplicaSet	haku-console-654fb4447b	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-console-db-indexer-provisioner-cpf4j	Job	haku-console-db-indexer-provisioner	Succeeded		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-console-db-matrix-adapter-provisioner-bvzdh	Job	haku-console-db-matrix-adapter-provisioner	Succeeded		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-console-migration-4vpnv	Job	haku-console-migration	Succeeded		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-indexer-chunk-ducktape-public-5649d7fd54-s4xmt	ReplicaSet	haku-indexer-chunk-ducktape-public-5649d7fd54	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-indexer-chunk-haku-conversations-55c55b4cb-gccl5	ReplicaSet	haku-indexer-chunk-haku-conversations-55c55b4cb	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-indexer-chunk-haku-state-c4b59df54-8hqjx	ReplicaSet	haku-indexer-chunk-haku-state-c4b59df54	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-indexer-embed-74b9d87667-9mbkz	ReplicaSet	haku-indexer-embed-74b9d87667	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-console	haku-kube-api-proxy-7c467f6866-5tqxs	ReplicaSet	haku-kube-api-proxy-7c467f6866	Running		no	
ovh-ns104952	haku-console	haku-matrix-adapter-774df67656-c74w9	ReplicaSet	haku-matrix-adapter-774df67656	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-egress-proxy	haku-egress-proxy-694c49d8b4-xgtv7	ReplicaSet	haku-egress-proxy-694c49d8b4	Running		no	
ovh-ns104952	haku-mailbox	haku-mailbox-859d86ddc5-5859l	ReplicaSet	haku-mailbox-859d86ddc5	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	haku-mailbox	haku-mailbox-smtp-ingress-l2tzf	DaemonSet	haku-mailbox-smtp-ingress	Running		no	topology.kubernetes.io/region=hil
ovh-ns104952	haku-sandbox	haku-jupyter-5497747c49-q2mtq	ReplicaSet	haku-jupyter-5497747c49	Running		no	
ovh-ns104952	haku-sandbox	haku-managed-agent-7d9c9bbf65-cflr2	ReplicaSet	haku-managed-agent-7d9c9bbf65	Running		no	
ovh-ns104952	haku-sandbox	haku-tdn4k	Sandbox	haku-tdn4k	Running		no	
ovh-ns104952	haku-sandbox	haku-ui-1	StatefulSet	haku-ui	Running	wal-haku-ui-1	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	kube-system	cilium-8j65l	DaemonSet	cilium	Running		no	kubernetes.io/os=linux
ovh-ns104952	kube-system	cilium-envoy-v8l4h	DaemonSet	cilium-envoy	Running		no	kubernetes.io/os=linux
ovh-ns104952	kube-system	hubble-ui-d74c95779-td7b7	ReplicaSet	hubble-ui-d74c95779	Running		no	kubernetes.io/os=linux
ovh-ns104952	kube-system	kube-apiserver-ovh-ns104952	Node	ovh-ns104952	Running		no	
ovh-ns104952	kube-system	kube-controller-manager-ovh-ns104952	Node	ovh-ns104952	Running		no	
ovh-ns104952	kube-system	kube-scheduler-ovh-ns104952	Node	ovh-ns104952	Running		no	
ovh-ns104952	kube-system	metrics-server-5868947679-p29s7	ReplicaSet	metrics-server-5868947679	Running		no	
ovh-ns104952	kube-system	snapshot-controller-f587d4869-hbntg	ReplicaSet	snapshot-controller-f587d4869	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	kubectl-machine-mcp	kubectl-machine-mcp-7794874fd7-qr78b	ReplicaSet	kubectl-machine-mcp-7794874fd7	Running		no	
ovh-ns104952	kubevirt	virt-controller-59d847bb8c-qgnqr	ReplicaSet	virt-controller-59d847bb8c	Running		no	kubernetes.io/os=linux;topology.kubernetes.io/region=hil
ovh-ns104952	kyverno	kyverno-admission-controller-758bbb96df-89wmd	ReplicaSet	kyverno-admission-controller-758bbb96df	Running		yes	kubernetes.io/os=linux;node-role.kubernetes.io/control-plane=
ovh-ns104952	kyverno	kyverno-admission-controller-758bbb96df-vmfr7	ReplicaSet	kyverno-admission-controller-758bbb96df	Running		yes	kubernetes.io/os=linux;node-role.kubernetes.io/control-plane=
ovh-ns104952	kyverno	kyverno-background-controller-c4f7bc589-b5bmh	ReplicaSet	kyverno-background-controller-c4f7bc589	Running		no	kubernetes.io/os=linux
ovh-ns104952	kyverno	kyverno-reports-controller-6bc76c9b84-n9dhg	ReplicaSet	kyverno-reports-controller-6bc76c9b84	Running		no	kubernetes.io/os=linux
ovh-ns104952	langfuse	langfuse-db-2	Cluster	langfuse-db	Running	langfuse-db-2	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	litellm	litellm-775c95f4fd-cgmzs	ReplicaSet	litellm-775c95f4fd	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	loki	loki-canary-b7dk6	DaemonSet	loki-canary	Running		no	
ovh-ns104952	loki	loki-gateway-66fbf8f5dd-5654h	ReplicaSet	loki-gateway-66fbf8f5dd	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	loki	loki-read-945cd649-57d25	ReplicaSet	loki-read-945cd649	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	loki	promtail-8824m	DaemonSet	promtail	Running		no	
ovh-ns104952	matrix	matrix-synapse-redis-master-788c946755-h8g26	ReplicaSet	matrix-synapse-redis-master-788c946755	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	monitoring	grafana-deployment-7d8f85d6fb-7bm8r	ReplicaSet	grafana-deployment-7d8f85d6fb	Running		no	
ovh-ns104952	monitoring	kube-state-metrics-76ffdf98f4-ww2x4	ReplicaSet	kube-state-metrics-76ffdf98f4	Running		no	
ovh-ns104952	monitoring	prometheus-node-exporter-krws4	DaemonSet	prometheus-node-exporter	Running		no	kubernetes.io/os=linux
ovh-ns104952	nix-cache	attic-db-4	Cluster	attic-db	Running	attic-db-4	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	node-feature-discovery	node-feature-discovery-worker-42cf5	DaemonSet	node-feature-discovery-worker	Running		no	
ovh-ns104952	osm-mcp	osm-mcp-746779697f-fmvxd	ReplicaSet	osm-mcp-746779697f	Running		no	
ovh-ns104952	paperless	paperless-78bbbb5767-l9gzt	ReplicaSet	paperless-78bbbb5767	Running	paperless-data	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	plaid-mcp	plaid-db-mcp-b468454-vgwbg	ReplicaSet	plaid-db-mcp-b468454	Running		no	
ovh-ns104952	plaid-mcp	plaid-mcp-db-readonly-provisioner-wxp67	Job	plaid-mcp-db-readonly-provisioner	Succeeded		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	postscanmail-mcp	postscanmail-mcp-5bf447b4b7-pqfk6	ReplicaSet	postscanmail-mcp-5bf447b4b7	Running		no	
ovh-ns104952	public-coder-agent	public-coder-agent-proxy-5f9d59df86-xd4fj	ReplicaSet	public-coder-agent-proxy-5f9d59df86	Running		no	
ovh-ns104952	seaweedfs	seaweedfs-filer-db-ssd-2	Cluster	seaweedfs-filer-db-ssd	Running	seaweedfs-filer-db-ssd-2	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	seaweedfs	seaweedfs-master-1	StatefulSet	seaweedfs-master	Running		yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	seaweedfs	seaweedfs-operator-658555cc7-bj294	ReplicaSet	seaweedfs-operator-658555cc7	Running		no	
ovh-ns104952	seaweedfs	seaweedfs-volume-ssd-1	StatefulSet	seaweedfs-volume-ssd	Running	mount0-seaweedfs-volume-ssd-1	yes	storage.allegedly.works/tier=ssd
ovh-ns104952	seaweedfs-csi-system	seaweedfs-csi-driver-mount-4bfrl	DaemonSet	seaweedfs-csi-driver-mount	Running		yes	
ovh-ns104952	seaweedfs-csi-system	seaweedfs-csi-driver-node-x45bb	DaemonSet	seaweedfs-csi-driver-node	Running		yes	
ovh-ns104952	squid-egress-spike	icap-stub-766bd4bc69-8vrdr	ReplicaSet	icap-stub-766bd4bc69	Running		no	
ovh-ns104952	squid-egress-spike	squid-spike-65d79bfc8d-69q7w	ReplicaSet	squid-spike-65d79bfc8d	Running		no	
ovh-ns104952	study-casino	study-casino-db-6	Cluster	study-casino-db	Running	study-casino-db-6	yes	topology.kubernetes.io/region=hil
ovh-ns104952	tana-mcp	tana-mcp-f4975d446-96k7x	ReplicaSet	tana-mcp-f4975d446	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	tana-mcp	tana-mcp-facade-7c97697458-x2c8m	ReplicaSet	tana-mcp-facade-7c97697458	Running		no	
ovh-ns104952	thrive-scraper	thrive-scrape-29792520-fckrk	Job	thrive-scrape-29792520	Succeeded		no	
ovh-ns104952	tofu-state	tofu-state-db-ovh-4	Cluster	tofu-state-db-ovh	Running	tofu-state-db-ovh-4	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104952	vector-talos-logs	vector-talos-logs-22njb	DaemonSet	vector-talos-logs	Running		no	node-vendor=talos
ovh-ns104963	agent-sandbox-system	agent-sandbox-controller-596f7c497d-x9xwx	ReplicaSet	agent-sandbox-controller-596f7c497d	Running		no	topology.kubernetes.io/region=hil
ovh-ns104963	agents-mitmproxy	mitmproxy-5fd5cd98f6-ptdjd	ReplicaSet	mitmproxy-5fd5cd98f6	Running		no	
ovh-ns104963	atuin	atuin-db-3	Cluster	atuin-db	Running	atuin-db-3	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	atuin	atuin-server-69fd454c85-s6cdr	ReplicaSet	atuin-server-69fd454c85	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	atuin	atuin-user-provisioner-xx7dg	Job	atuin-user-provisioner	Succeeded		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	authelia	authelia-787556c5c7-88c7v	ReplicaSet	authelia-787556c5c7	Running		yes	topology.kubernetes.io/region=hil
ovh-ns104963	authentik	authentik-server-d69d56f6-6qfjb	ReplicaSet	authentik-server-d69d56f6	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	authentik	authentik-worker-798f6cbbb-f9mfc	ReplicaSet	authentik-worker-798f6cbbb	Running		no	topology.kubernetes.io/region=hil
ovh-ns104963	budget	fava-69d6d5b69b-bk7sb	ReplicaSet	fava-69d6d5b69b	Running		no	
ovh-ns104963	cdi	cdi-apiserver-cc7558d8d-bgxpg	ReplicaSet	cdi-apiserver-cc7558d8d	Running		no	topology.kubernetes.io/region=hil
ovh-ns104963	cdi	cdi-uploadproxy-5cdd87cf96-9zvzk	ReplicaSet	cdi-uploadproxy-5cdd87cf96	Running		no	topology.kubernetes.io/region=hil
ovh-ns104963	cert-manager	cert-manager-7947bbbfb4-84qld	ReplicaSet	cert-manager-7947bbbfb4	Running		no	kubernetes.io/os=linux
ovh-ns104963	cert-manager	cert-manager-cainjector-56fd84d59c-vsgzm	ReplicaSet	cert-manager-cainjector-56fd84d59c	Running		no	kubernetes.io/os=linux
ovh-ns104963	cert-manager	cert-manager-webhook-579d9b77f-kk6zl	ReplicaSet	cert-manager-webhook-579d9b77f	Running		no	kubernetes.io/os=linux
ovh-ns104963	cert-manager-trust	trust-manager-69846c7c67-xqch6	ReplicaSet	trust-manager-69846c7c67	Running		no	kubernetes.io/os=linux
ovh-ns104963	cli-proxy-api	cli-proxy-api-7789678c94-rhdc7	ReplicaSet	cli-proxy-api-7789678c94	Running	cli-proxy-api-data	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	clickhouse	clickhouse-operator-altinity-clickhouse-operator-75fd74c75hqwxh	ReplicaSet	clickhouse-operator-altinity-clickhouse-operator-75fd74c757	Running		no	
ovh-ns104963	cnpg-system	cnpg-cloudnative-pg-7df79c96bd-lwvpr	ReplicaSet	cnpg-cloudnative-pg-7df79c96bd	Running		no	
ovh-ns104963	codex-pod	codex-pod-5bb8cd7cd6-2qlwf	ReplicaSet	codex-pod-5bb8cd7cd6	Running	codex-workspace	no	topology.kubernetes.io/region=hil;topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	default	kubeapi-proxy-74bbc75fb6-j42zw	ReplicaSet	kubeapi-proxy-74bbc75fb6	Running		no	
ovh-ns104963	docker-ci	docker-image-prune-29790900-2z6z9	Job	docker-image-prune-29790900	Succeeded		no	
ovh-ns104963	external-secrets-system	external-secrets-webhook-5dcf654897-qnt6l	ReplicaSet	external-secrets-webhook-5dcf654897	Running		no	
ovh-ns104963	flux-system	tofu-controller-b9b46cc5f-8wcsb	ReplicaSet	tofu-controller-b9b46cc5f	Running		no	
ovh-ns104963	forgejo	forgejo-db-ssd-1	Cluster	forgejo-db-ssd	Running	forgejo-db-ssd-1	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	forgejo	forgejo-valkey-ovh-0	StatefulSet	forgejo-valkey-ovh	Running	forgejo-valkey-ovh-forgejo-valkey-ovh-0	yes	
ovh-ns104963	goldilocks	goldilocks-controller-5b9c859d6b-x5cs6	ReplicaSet	goldilocks-controller-5b9c859d6b	Running		no	
ovh-ns104963	grocy-sf	volsync-rsync-tls-dst-grocy-config-ovh-backup-sqqck	Job	volsync-rsync-tls-dst-grocy-config-ovh-backup	Running	grocy-config-ovh-backup	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	grocy-vallejo	volsync-rsync-tls-dst-grocy-config-ovh-backup-j7zf9	Job	volsync-rsync-tls-dst-grocy-config-ovh-backup	Running	grocy-config-ovh-backup	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	haku-console	haku-console-654fb4447b-zbwvj	ReplicaSet	haku-console-654fb4447b	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	haku-console	haku-console-static-dc964db64-8mp4n	ReplicaSet	haku-console-static-dc964db64	Running		no	
ovh-ns104963	haku-console	haku-kube-api-proxy-7c467f6866-bdk6l	ReplicaSet	haku-kube-api-proxy-7c467f6866	Running		no	
ovh-ns104963	haku-egress-proxy	haku-claude-oauth-proxy-84bb448c88-pkjtw	ReplicaSet	haku-claude-oauth-proxy-84bb448c88	Running		no	
ovh-ns104963	haku-egress-proxy	haku-openclaw-spike-proxy-c85578f8d-flp57	ReplicaSet	haku-openclaw-spike-proxy-c85578f8d	Running		no	
ovh-ns104963	haku-mailbox	haku-mailbox-smtp-ingress-tq5f2	DaemonSet	haku-mailbox-smtp-ingress	Running		no	topology.kubernetes.io/region=hil
ovh-ns104963	haku-sandbox	haku-anki-5cd9585c86-pz6sp	ReplicaSet	haku-anki-5cd9585c86	Running	haku-anki-collection	no	
ovh-ns104963	haku-sandbox	haku-ui-0	StatefulSet	haku-ui	Running	wal-haku-ui-0	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	home-assistant	ha-mcp-token-provisioner-29790960-kh8zb	Job	ha-mcp-token-provisioner-29790960	Succeeded		no	
ovh-ns104963	home-assistant	volsync-rsync-tls-dst-home-assistant-config-backup-mql7d	Job	volsync-rsync-tls-dst-home-assistant-config-backup	Running	home-assistant-config-backup	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	keda	keda-admission-webhooks-5ccfdf77dd-szzw6	ReplicaSet	keda-admission-webhooks-5ccfdf77dd	Running		no	kubernetes.io/os=linux;topology.kubernetes.io/region=hil
ovh-ns104963	keda	keda-operator-5497d49c9d-r8mc4	ReplicaSet	keda-operator-5497d49c9d	Running		no	kubernetes.io/os=linux;topology.kubernetes.io/region=hil
ovh-ns104963	keda	keda-operator-metrics-apiserver-7bffd966f5-s88dv	ReplicaSet	keda-operator-metrics-apiserver-7bffd966f5	Running		no	kubernetes.io/os=linux;topology.kubernetes.io/region=hil
ovh-ns104963	kube-system	cilium-envoy-4zbf5	DaemonSet	cilium-envoy	Running		no	kubernetes.io/os=linux
ovh-ns104963	kube-system	cilium-operator-5c5696cbdc-2sn4x	ReplicaSet	cilium-operator-5c5696cbdc	Running		yes	kubernetes.io/os=linux
ovh-ns104963	kube-system	cilium-x4sl2	DaemonSet	cilium	Running		no	kubernetes.io/os=linux
ovh-ns104963	kube-system	hubble-relay-5fcdfbb8b9-rl2qb	ReplicaSet	hubble-relay-5fcdfbb8b9	Running		no	kubernetes.io/os=linux
ovh-ns104963	kube-system	kube-apiserver-ovh-ns104963	Node	ovh-ns104963	Running		no	
ovh-ns104963	kube-system	kube-controller-manager-ovh-ns104963	Node	ovh-ns104963	Running		no	
ovh-ns104963	kube-system	kube-scheduler-ovh-ns104963	Node	ovh-ns104963	Running		no	
ovh-ns104963	kube-system	reloader-reloader-85f4f7b8f4-ljjvg	ReplicaSet	reloader-reloader-85f4f7b8f4	Running		no	
ovh-ns104963	kube-system	snapshot-controller-f587d4869-f9zln	ReplicaSet	snapshot-controller-f587d4869	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	kube-system	vpa-admission-controller-c668f88bb-rg5lp	ReplicaSet	vpa-admission-controller-c668f88bb	Running		no	
ovh-ns104963	kube-system	vpa-recommender-7f4697957c-6n47q	ReplicaSet	vpa-recommender-7f4697957c	Running		no	
ovh-ns104963	kube-system	vpa-updater-65ccfbd859-nsf8l	ReplicaSet	vpa-updater-65ccfbd859	Running		no	
ovh-ns104963	kubectl-passthrough-mcp	kubectl-passthrough-mcp-84c8d89bcd-k96v8	ReplicaSet	kubectl-passthrough-mcp-84c8d89bcd	Running		no	
ovh-ns104963	kubevirt	virt-api-5c566f5c8b-7cmjf	ReplicaSet	virt-api-5c566f5c8b	Running		no	kubernetes.io/os=linux;topology.kubernetes.io/region=hil
ovh-ns104963	kubevirt	virt-operator-7c7c8d8867-5kvjk	ReplicaSet	virt-operator-7c7c8d8867	Running		yes	topology.kubernetes.io/region=hil
ovh-ns104963	kyverno	kyverno-admission-controller-758bbb96df-9cn9v	ReplicaSet	kyverno-admission-controller-758bbb96df	Running		yes	kubernetes.io/os=linux;node-role.kubernetes.io/control-plane=
ovh-ns104963	kyverno	kyverno-cleanup-controller-55c947dc6b-dsflw	ReplicaSet	kyverno-cleanup-controller-55c947dc6b	Running		no	kubernetes.io/os=linux
ovh-ns104963	langfuse	langfuse-db-1	Cluster	langfuse-db	Running	langfuse-db-1	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	litellm	tana-litellm-6db56667f-hxzvc	ReplicaSet	tana-litellm-6db56667f	Running		no	
ovh-ns104963	local-path-storage	local-path-provisioner-7b4d548f7c-fppsb	ReplicaSet	local-path-provisioner-7b4d548f7c	Running		no	
ovh-ns104963	loki	loki-canary-42qkb	DaemonSet	loki-canary	Running		no	
ovh-ns104963	loki	promtail-w95k7	DaemonSet	promtail	Running		no	
ovh-ns104963	loki-read-proxy	loki-read-proxy-5448ff7c5f-npz6j	ReplicaSet	loki-read-proxy-5448ff7c5f	Running		no	
ovh-ns104963	manifold-mcp	manifold-mcp-59cb96d56-k2m22	ReplicaSet	manifold-mcp-59cb96d56	Running		no	
ovh-ns104963	matrix	element-web-77f7d896d8-6tqkd	ReplicaSet	element-web-77f7d896d8	Running		no	
ovh-ns104963	matrix	matrix-synapse-7c76699699-vntd8	ReplicaSet	matrix-synapse-7c76699699	Running	matrix-synapse	no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	monitoring	alloy-57798d68d5-h8l8n	ReplicaSet	alloy-57798d68d5	Running		no	
ovh-ns104963	monitoring	github-exporter-agentydragon-5fc64dcb47-8z47q	ReplicaSet	github-exporter-agentydragon-5fc64dcb47	Running		no	
ovh-ns104963	monitoring	github-exporter-agentydragon-agent-56fcb688f-6tzlc	ReplicaSet	github-exporter-agentydragon-agent-56fcb688f	Running		no	
ovh-ns104963	monitoring	monitoring-operator-7cf9494cbf-p9jrm	ReplicaSet	monitoring-operator-7cf9494cbf	Running		no	
ovh-ns104963	monitoring	prometheus-node-exporter-jl2nk	DaemonSet	prometheus-node-exporter	Running		no	kubernetes.io/os=linux
ovh-ns104963	monitoring	tempo-0	StatefulSet	tempo	Running		no	topology.kubernetes.io/region=hil
ovh-ns104963	nix-cache	attic-54dc658848-2kr4p	ReplicaSet	attic-54dc658848	Running		no	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	node-feature-discovery	node-feature-discovery-gc-5fc77fbd44-9xkgc	ReplicaSet	node-feature-discovery-gc-5fc77fbd44	Running		no	
ovh-ns104963	node-feature-discovery	node-feature-discovery-master-576bd5fb7c-khqxx	ReplicaSet	node-feature-discovery-master-576bd5fb7c	Running		yes	
ovh-ns104963	node-feature-discovery	node-feature-discovery-worker-9wb9h	DaemonSet	node-feature-discovery-worker	Running		no	
ovh-ns104963	oci-cache	zot-74764fb64-v26c6	ReplicaSet	zot-74764fb64	Running		no	
ovh-ns104963	plaid-mcp	plaid-mcp-566779b568-8qcm5	ReplicaSet	plaid-mcp-566779b568	Running		no	
ovh-ns104963	reflector-system	reflector-6f86fb8864-hxb76	ReplicaSet	reflector-6f86fb8864	Running		no	
ovh-ns104963	seaweedfs	public-s3-74cbd95966-6fxpj	ReplicaSet	public-s3-74cbd95966	Running		yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	seaweedfs	seaweedfs-filer-db-ssd-1	Cluster	seaweedfs-filer-db-ssd	Running	seaweedfs-filer-db-ssd-1	yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	seaweedfs	seaweedfs-master-0	StatefulSet	seaweedfs-master	Running		yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	seaweedfs	seaweedfs-s3-75c6547846-52q95	ReplicaSet	seaweedfs-s3-75c6547846	Running		yes	topology.kubernetes.io/zone=hil-ovh
ovh-ns104963	seaweedfs	seaweedfs-volume-ssd-0	StatefulSet	seaweedfs-volume-ssd	Running	mount0-seaweedfs-volume-ssd-0	yes	storage.allegedly.works/tier=ssd
ovh-ns104963	seaweedfs-csi-system	seaweedfs-csi-driver-mount-7bcv5	DaemonSet	seaweedfs-csi-driver-mount	Running		yes	
ovh-ns104963	seaweedfs-csi-system	seaweedfs-csi-driver-node-c7v2h	DaemonSet	seaweedfs-csi-driver-node	Running		yes	
ovh-ns104963	squid-egress-spike	echo-origin-7bdb86d486-2cljq	ReplicaSet	echo-origin-7bdb86d486	Running		no	
ovh-ns104963	study-casino	study-casino-5f8d68868c-mmdhw	ReplicaSet	study-casino-5f8d68868c	Running		no	topology.kubernetes.io/region=hil
ovh-ns104963	valkey-system	redis-operator-bf5c6888d-btsks	ReplicaSet	redis-operator-bf5c6888d	Running		no	
ovh-ns104963	vector-talos-logs	vector-talos-logs-lxgv6	DaemonSet	vector-talos-logs	Running		no	node-vendor=talos
ovh-ns104963	website	website-6f54876786-nf292	ReplicaSet	website-6f54876786	Running		no
```

## First-pass decision candidates

These are mechanically derived review queues, not final approvals.

### Running DaemonSets on CPs without an explicit control-plane toleration

DaemonSets may be intentionally all-node components, or they may be allowed to
leave CPs. Their owners must decide which before taint rollout:

- haku-mailbox/haku-mailbox-smtp-ingress
- kube-system/cilium
- kube-system/cilium-envoy
- loki/loki-canary
- loki/promtail
- monitoring/prometheus-node-exporter
- node-feature-discovery/node-feature-discovery-worker
- vector-talos-logs/vector-talos-logs

### Running non-DaemonSet stateless candidates

These have no CP-pinned PVC and no explicit CP toleration. Confirm worker
selectors, capacity, PDBs, and endpoint redundancy, then roll them without adding
a CP toleration:

- agent-sandbox-system/agent-sandbox-controller-596f7c497d
- agents-mitmproxy/mitmproxy-5fd5cd98f6
- airlock/airlock-55f7f7c94b
- atuin/atuin-server-69fd454c85
- authentik/authentik-server-d69d56f6
- authentik/authentik-worker-798f6cbbb
- budget/fava-69d6d5b69b
- cdi/cdi-apiserver-cc7558d8d
- cdi/cdi-deployment-5fdf9578fb
- cdi/cdi-operator-6c9cdddcb
- cdi/cdi-uploadproxy-5cdd87cf96
- cert-manager-trust/trust-manager-69846c7c67
- cert-manager/cert-manager-7947bbbfb4
- cert-manager/cert-manager-cainjector-56fd84d59c
- cert-manager/cert-manager-webhook-579d9b77f
- cli-proxy-api/aiquota-api-6c46d68c47
- clickhouse/clickhouse-operator-altinity-clickhouse-operator-75fd74c757
- cnpg-system/cnpg-cloudnative-pg-7df79c96bd
- default/kubeapi-proxy-74bbc75fb6
- docker-ci/docker-ci-5b4768d6bf
- external-secrets-system/external-secrets-6f86f5486f
- external-secrets-system/external-secrets-webhook-5dcf654897
- flux-system/tofu-controller-b9b46cc5f
- gatus/gatus-6898d65d56
- goldilocks/goldilocks-controller-5b9c859d6b
- goldilocks/goldilocks-dashboard-7c588f9bfd
- grocy-sf/grocy-mcp-server-6b44c5c678
- grocy-vallejo/grocy-mcp-server-64f9b6495c
- ha-mcp/ha-mcp-67f877b4b
- haku-console/haku-console-654fb4447b
- haku-console/haku-console-static-dc964db64
- haku-console/haku-indexer-chunk-ducktape-public-5649d7fd54
- haku-console/haku-indexer-chunk-haku-conversations-55c55b4cb
- haku-console/haku-indexer-chunk-haku-state-c4b59df54
- haku-console/haku-indexer-embed-74b9d87667
- haku-console/haku-kube-api-proxy-7c467f6866
- haku-console/haku-matrix-adapter-774df67656
- haku-egress-proxy/haku-claude-oauth-proxy-84bb448c88
- haku-egress-proxy/haku-egress-proxy-694c49d8b4
- haku-egress-proxy/haku-openclaw-spike-proxy-c85578f8d
- haku-mailbox/haku-mailbox-859d86ddc5
- haku-sandbox/haku-jupyter-5497747c49
- haku-sandbox/haku-managed-agent-7d9c9bbf65
- haku-sandbox/haku-tdn4k
- keda/keda-admission-webhooks-5ccfdf77dd
- keda/keda-operator-5497d49c9d
- keda/keda-operator-metrics-apiserver-7bffd966f5
- kube-system/hubble-relay-5fcdfbb8b9
- kube-system/hubble-ui-d74c95779
- kube-system/metrics-server-5868947679
- kube-system/reloader-reloader-85f4f7b8f4
- kube-system/snapshot-controller-f587d4869
- kube-system/vpa-admission-controller-c668f88bb
- kube-system/vpa-recommender-7f4697957c
- kube-system/vpa-updater-65ccfbd859
- kubectl-machine-mcp/kubectl-machine-mcp-7794874fd7
- kubectl-passthrough-mcp/kubectl-passthrough-mcp-84c8d89bcd
- kubevirt/virt-api-5c566f5c8b
- kubevirt/virt-controller-59d847bb8c
- kyverno/kyverno-background-controller-c4f7bc589
- kyverno/kyverno-cleanup-controller-55c947dc6b
- kyverno/kyverno-reports-controller-6bc76c9b84
- litellm/litellm-775c95f4fd
- litellm/tana-litellm-6db56667f
- local-path-storage/local-path-provisioner-7b4d548f7c
- loki-read-proxy/loki-read-proxy-5448ff7c5f
- loki/loki-gateway-66fbf8f5dd
- loki/loki-read-945cd649
- manifold-mcp/manifold-mcp-59cb96d56
- matrix/element-web-77f7d896d8
- matrix/matrix-synapse-redis-master-788c946755
- monitoring/alloy-57798d68d5
- monitoring/github-exporter-agentydragon-5fc64dcb47
- monitoring/github-exporter-agentydragon-agent-56fcb688f
- monitoring/grafana-deployment-7d8f85d6fb
- monitoring/kube-state-metrics-76ffdf98f4
- monitoring/monitoring-operator-7cf9494cbf
- monitoring/tempo
- nix-cache/attic-54dc658848
- node-feature-discovery/node-feature-discovery-gc-5fc77fbd44
- oci-cache/zot-74764fb64
- osm-mcp/osm-mcp-746779697f
- plaid-mcp/plaid-db-mcp-b468454
- plaid-mcp/plaid-mcp-566779b568
- postscanmail-mcp/postscanmail-mcp-5bf447b4b7
- public-coder-agent/public-coder-agent-proxy-5f9d59df86
- reflector-system/reflector-6f86fb8864
- seaweedfs/seaweedfs-operator-658555cc7
- squid-egress-spike/echo-origin-7bdb86d486
- squid-egress-spike/icap-stub-766bd4bc69
- squid-egress-spike/squid-spike-65d79bfc8d
- study-casino/study-casino-5f8d68868c
- tana-mcp/tana-mcp-f4975d446
- tana-mcp/tana-mcp-facade-7c97697458
- valkey-system/redis-operator-bf5c6888d
- website/website-6f54876786

### Running residents with an explicit CP toleration

Review the toleration's owner, reason, and exit condition. Some are platform
components; others are temporary application exceptions:

- atuin/atuin-db
- authelia/authelia-787556c5c7
- authentik/authentik-db-ovh
- clickhouse/chk-clickhouse-keeper-keeper-0-0
- forgejo/forgejo-db-ssd
- forgejo/forgejo-valkey-ovh
- headlamp/headlamp-8bbff9d5f
- kube-system/cilium-operator-5c5696cbdc
- kube-system/coredns-75d98bc87d
- kube-system/talos-cloud-controller-manager-865f94896d
- kubevirt/virt-operator-7c7c8d8867
- kyverno/kyverno-admission-controller-758bbb96df
- langfuse/langfuse-db
- monitoring/alertmanager-monitoring
- nix-cache/attic-db
- node-feature-discovery/node-feature-discovery-master-576bd5fb7c
- plaid-mcp/plaid-mcp-db
- seaweedfs-csi-system/seaweedfs-csi-driver-controller-6c54c47bb
- seaweedfs-csi-system/seaweedfs-csi-driver-mount
- seaweedfs-csi-system/seaweedfs-csi-driver-node
- seaweedfs/public-s3-74cbd95966
- seaweedfs/seaweedfs-filer-db-ssd
- seaweedfs/seaweedfs-master
- seaweedfs/seaweedfs-s3-75c6547846
- seaweedfs/seaweedfs-volume-hdd
- seaweedfs/seaweedfs-volume-ssd
- study-casino/study-casino-db
- tofu-state/tofu-state-db-ovh

## Control-plane-pinned local PV inventory

Every row requires one of: migrate with application-level recovery gates,
retain temporarily with a narrow toleration and owner, retain as an intentional
continuing exception, or retire through a separately approved cleanup.

Columns are: PV, claim namespace, claim, phase, storage class, capacity,
reclaim policy, volume mode, node.

```text
pvc-004b2e55-1c83-4846-bd04-37dcceacc32a	monitoring	db-alertmanager-monitoring-0	Bound	local-path-ovh	1Gi	Delete	Filesystem	ovh-ns103656
pvc-0442a585-0ead-430a-9191-b724e0eab07f	atuin	atuin-db-4	Bound	local-path-ovh	2Gi	Delete	Filesystem	ovh-ns104952
pvc-0650e4e9-8f33-4ce8-8eda-2e89c3062d56	seaweedfs	mount0-seaweedfs-volume-ssd-0	Bound	local-path-ovh-ssd	250Gi	Delete	Filesystem	ovh-ns104963
pvc-0d6eaf30-8c6e-4dfb-be66-09c662be5ea9	nix-cache	attic-db-4	Bound	local-path-ovh	2Gi	Delete	Filesystem	ovh-ns104952
pvc-1599428e-f912-4f9d-b5f0-9f2a9cbdcffa	langfuse	langfuse-db-2	Bound	local-path-ovh	10Gi	Delete	Filesystem	ovh-ns104952
pvc-29a423f0-42e9-4ea1-9b4d-61fa7e725ec1	public-coder-agent	public-coder-agent-state	Bound	local-path-ovh-hdd	10Gi	Delete	Filesystem	ovh-ns103656
pvc-429ff34b-3d3e-4ff9-95ce-8d06a747e9fc	atuin	atuin-db-3	Bound	local-path-ovh	2Gi	Delete	Filesystem	ovh-ns104963
pvc-4545a8f6-a653-4219-b8bf-9706c9129162	forgejo	forgejo-valkey-ovh-forgejo-valkey-ovh-0	Bound	local-path-ovh	2Gi	Delete	Filesystem	ovh-ns104963
pvc-48f4ab5a-f26a-40ca-bdab-289642e0e4ad	seaweedfs	mount0-seaweedfs-volume-ssd-1	Bound	local-path-ovh-ssd	250Gi	Delete	Filesystem	ovh-ns104952
pvc-4f0d6c18-dc14-44e9-a6a5-0f5e1c5e41c3	forgejo	forgejo-valkey-ovh-forgejo-valkey-ovh-1	Bound	local-path-ovh	2Gi	Delete	Filesystem	ovh-ns104952
pvc-59773290-f75e-4fb7-9033-ef517160066d	study-casino	study-casino-db-6	Bound	local-path-ovh	1Gi	Delete	Filesystem	ovh-ns104952
pvc-5df8c7d7-3084-4684-8872-b3e6c7b321b4	haku-sandbox	haku-anki-collection	Bound	local-path-ovh-ssd	2Gi	Delete	Filesystem	ovh-ns104963
pvc-6467243b-6e78-4670-86f9-d72515398f9c	forgejo	forgejo-db-ssd-1	Bound	local-path-ovh-ssd	10Gi	Delete	Filesystem	ovh-ns104963
pvc-65e7e4ae-1dc8-4116-953b-bb1860118507	haku-openclaw-spike	haku-openclaw-spike-state	Bound	local-path-ovh-hdd	30Gi	Delete	Filesystem	ovh-ns103656
pvc-69b11281-9f2e-431c-94b4-95dbf2cc8898	langfuse	langfuse-db-1	Bound	local-path-ovh	10Gi	Delete	Filesystem	ovh-ns104963
pvc-721b40b5-a9a1-4fd0-a95f-e67b03877cd3	seaweedfs	mount0-seaweedfs-volume-hdd-2	Bound	local-path-ovh-hdd	1800Gi	Delete	Filesystem	ovh-ns103656
pvc-7a43a761-fae1-460d-b0c9-b26ae8ea8b63	plaid-mcp	plaid-mcp-db-1	Bound	local-path-ovh	5Gi	Delete	Filesystem	ovh-ns103656
pvc-8333c530-8800-4c81-826d-cfd127a679b5	tofu-state	tofu-state-db-ovh-4	Bound	local-path-ovh	1Gi	Delete	Filesystem	ovh-ns104952
pvc-8467d9ff-7c0b-4fcb-bb8a-c67206649ee1	authentik	authentik-db-ovh-4	Bound	local-path-ovh	8Gi	Delete	Filesystem	ovh-ns104952
pvc-c2029190-e828-42dc-b324-5b1cf3c32865	haku-openclaw-spike	volsync-src-haku-openclaw-spike-state-restic-cache	Bound	local-path-ovh-hdd	1Gi	Delete	Filesystem	ovh-ns103656
pvc-cfbf2af9-6213-4ea5-b1cd-a523a7295275	clickhouse	keeper-data-chk-clickhouse-keeper-keeper-0-0-0	Bound	local-path-ovh-hdd-retain	2Gi	Retain	Filesystem	ovh-ns103656
pvc-d8bb36f6-4a5e-4e8d-b1bb-d27dce6285bb	seaweedfs	seaweedfs-filer-db-ssd-2	Bound	local-path-ovh-ssd	2Gi	Delete	Filesystem	ovh-ns104952
pvc-f0459ea7-f790-406d-8831-003c61971d63	seaweedfs	seaweedfs-filer-db-ssd-1	Bound	local-path-ovh-ssd	2Gi	Delete	Filesystem	ovh-ns104963
pvc-f38b2a11-c01d-417e-bf5d-3d5744b31edf	forgejo	forgejo-db-ssd-2	Bound	local-path-ovh-ssd	10Gi	Delete	Filesystem	ovh-ns104952
```

Named claim worksheet:

- ovh-ns103656 clickhouse/keeper-data-chk-clickhouse-keeper-keeper-0-0-0 (local-path-ovh-hdd-retain, 2Gi)
- ovh-ns103656 haku-openclaw-spike/haku-openclaw-spike-state (local-path-ovh-hdd, 30Gi)
- ovh-ns103656 haku-openclaw-spike/volsync-src-haku-openclaw-spike-state-restic-cache (local-path-ovh-hdd, 1Gi)
- ovh-ns103656 monitoring/db-alertmanager-monitoring-0 (local-path-ovh, 1Gi)
- ovh-ns103656 plaid-mcp/plaid-mcp-db-1 (local-path-ovh, 5Gi)
- ovh-ns103656 public-coder-agent/public-coder-agent-state (local-path-ovh-hdd, 10Gi)
- ovh-ns103656 seaweedfs/mount0-seaweedfs-volume-hdd-2 (local-path-ovh-hdd, 1800Gi)
- ovh-ns104952 atuin/atuin-db-4 (local-path-ovh, 2Gi)
- ovh-ns104952 authentik/authentik-db-ovh-4 (local-path-ovh, 8Gi)
- ovh-ns104952 forgejo/forgejo-db-ssd-2 (local-path-ovh-ssd, 10Gi)
- ovh-ns104952 forgejo/forgejo-valkey-ovh-forgejo-valkey-ovh-1 (local-path-ovh, 2Gi)
- ovh-ns104952 langfuse/langfuse-db-2 (local-path-ovh, 10Gi)
- ovh-ns104952 nix-cache/attic-db-4 (local-path-ovh, 2Gi)
- ovh-ns104952 seaweedfs/mount0-seaweedfs-volume-ssd-1 (local-path-ovh-ssd, 250Gi)
- ovh-ns104952 seaweedfs/seaweedfs-filer-db-ssd-2 (local-path-ovh-ssd, 2Gi)
- ovh-ns104952 study-casino/study-casino-db-6 (local-path-ovh, 1Gi)
- ovh-ns104952 tofu-state/tofu-state-db-ovh-4 (local-path-ovh, 1Gi)
- ovh-ns104963 atuin/atuin-db-3 (local-path-ovh, 2Gi)
- ovh-ns104963 forgejo/forgejo-db-ssd-1 (local-path-ovh-ssd, 10Gi)
- ovh-ns104963 forgejo/forgejo-valkey-ovh-forgejo-valkey-ovh-0 (local-path-ovh, 2Gi)
- ovh-ns104963 haku-sandbox/haku-anki-collection (local-path-ovh-ssd, 2Gi)
- ovh-ns104963 langfuse/langfuse-db-1 (local-path-ovh, 10Gi)
- ovh-ns104963 seaweedfs/mount0-seaweedfs-volume-ssd-0 (local-path-ovh-ssd, 250Gi)
- ovh-ns104963 seaweedfs/seaweedfs-filer-db-ssd-1 (local-path-ovh-ssd, 2Gi)

## Current rollout blockers

### Pending pods

```text
haku-openclaw-spike   volsync-src-haku-openclaw-spike-state-restic-jddnr   <none>   <none>
```

### Failed pods

```text
haku-ci   haku-runner-4kf7f-jqs8d   ovh-ns102453   <none>
```

### Direct Flux non-ready resources

```text
ducktape-flux	authentik	Unknown	Progressing	Reconciliation in progress
ducktape-flux	cert-manager-environment	Unknown	Progressing	Reconciliation in progress
ducktape-flux	props	False	HealthCheckFailed	health check failed after 1.049454731s: failed early due to stalled resources: [Deployment/props/props status: 'Failed']
```

### Dependency-not-ready roots

These counts are the dependency targets named by currently blocked
Kustomizations; downstream failures should not be treated as independent rollout
failures:

```text
23 ducktape-flux/forgejo-images
     18 ducktape-flux/seaweedfs-cluster
     10 ducktape-flux/forgejo
      8 ducktape-flux/cert-manager-environment
      6 ducktape-flux/authentik
      4 ducktape-flux/gateway
      3 ducktape-flux/sso-providers-tf
      3 ducktape-flux/haku-state
      3 ducktape-flux/grafana-instance
      3 ducktape-flux/agent-machine-access-tf
      2 ducktape-flux/public-coder-agent-proxy
      2 ducktape-flux/ollama-secrets
      2 ducktape-flux/nix-cache
      2 ducktape-flux/litellm-keys-tf
      2 ducktape-flux/litellm
      2 ducktape-flux/github-secrets-sync-secrets
      2 ducktape-flux/authentik-jwt-rotation
      1 flux-system/gaffer-private
      1 ducktape-flux/tana-mcp
      1 ducktape-flux/seaweedfs-secrets
      1 ducktape-flux/seaweedfs-registry-cache-bucket
      1 ducktape-flux/seaweedfs-public-s3
      1 ducktape-flux/seaweedfs-public-coder-agent-backups-bucket
      1 ducktape-flux/seaweedfs-pr-visuals-bucket
      1 ducktape-flux/seaweedfs-langfuse-bucket
      1 ducktape-flux/seaweedfs-home-assistant-backups-bucket
      1 ducktape-flux/seaweedfs-haku-openclaw-spike-backups-bucket
      1 ducktape-flux/seaweedfs-forgejo-bucket
      1 ducktape-flux/seaweedfs-drivefs-artifacts-bucket
      1 ducktape-flux/postscanmail-mcp
      1 ducktape-flux/plaid-db-mcp
      1 ducktape-flux/mimir
      1 ducktape-flux/matrix
      1 ducktape-flux/manifold-mcp
      1 ducktape-flux/loki
      1 ducktape-flux/home-assistant
      1 ducktape-flux/haku-egress-proxy
      1 ducktape-flux/haku-console-db
      1 ducktape-flux/haku-console
      1 ducktape-flux/ha-mcp
      1 ducktape-flux/grocy-mcp-vallejo
      1 ducktape-flux/grocy-mcp-sf
      1 ducktape-flux/gatus-sso-tf
      1 ducktape-flux/gatus
      1 ducktape-flux/flux-webhook-token
      1 ducktape-flux/external-secrets-config
      1 ducktape-flux/clickhouse
      1 ducktape-flux/budget-ledger
      1 ducktape-flux/browsertrix-namespace
      1 ducktape-flux/atuin
      1 ducktape-flux/agent-shared-secrets
```

### Non-ready HelmReleases

```text
<none>
```

### PDB baseline

```text
atuin          atuin-db-primary                 1        <none>   0     1     1
authentik      authentik-db-ovh-primary         1        <none>   0     1     1
clickhouse     chi-clickhouse-default           <none>   1        1     2     2
clickhouse     chk-clickhouse-keeper-keeper     <none>   1        1     3     3
forgejo        forgejo                          1        <none>   1     2     2
forgejo        forgejo-db-ssd-primary           1        <none>   0     1     1
gatus          gatus-db-primary                 1        <none>   0     1     1
haku-console   haku-console-db-primary          1        <none>   0     1     1
haku-mailbox   haku-mailbox-db-primary          1        <none>   0     1     1
kubevirt       virt-api-pdb                     1        <none>   1     2     2
kubevirt       virt-controller-pdb              1        <none>   1     2     2
kyverno        kyverno-admission-controller     1        <none>   2     3     3
langfuse       langfuse-db-primary              1        <none>   0     1     1
langfuse       langfuse-web-pdb                 <none>   1        1     1     1
langfuse       langfuse-worker-pdb              <none>   1        1     1     1
litellm        litellm-db-primary               1        <none>   0     1     1
loki           loki-backend                     <none>   1        1     2     2
loki           loki-read                        <none>   1        1     2     2
loki           loki-write                       <none>   1        1     2     2
matrix         matrix-db-primary                1        <none>   0     1     1
monitoring     grafana-db-ovh-primary           1        <none>   0     1     1
monitoring     mimir-compactor                  <none>   1        1     1     1
monitoring     mimir-distributor                <none>   1        1     1     1
monitoring     mimir-gateway                    <none>   1        1     1     1
monitoring     mimir-ingester                   <none>   1        1     2     2
monitoring     mimir-querier                    <none>   1        1     1     1
monitoring     mimir-query-frontend             <none>   1        1     1     1
monitoring     mimir-query-scheduler            <none>   1        1     1     1
monitoring     mimir-ruler                      <none>   1        1     1     1
monitoring     mimir-store-gateway              <none>   1        1     1     1
nix-cache      attic-db-primary                 1        <none>   0     1     1
paperless      paperless-db-primary             1        <none>   0     1     1
plaid-mcp      plaid-mcp-db-primary             1        <none>   0     1     1
seaweedfs      seaweedfs-filer                  1        <none>   1     2     2
seaweedfs      seaweedfs-filer-db-ssd-primary   1        <none>   0     1     1
seaweedfs      seaweedfs-master                 2        <none>   1     3     3
seaweedfs      seaweedfs-volume                 2        <none>   3     5     5
study-casino   study-casino-db                  1        <none>   1     2     2
study-casino   study-casino-db-primary          1        <none>   0     1     1
tofu-state     tofu-state-db-ovh-primary        1        <none>   0     1     1
```

## Execution sequence

1. Clear or explicitly baseline the Pending/Flux blockers and confirm all three
   etcd members, API health, and external endpoints are stable.
2. For every row in the pod/PV worksheets, assign an owner and one action:
   worker rollout, application-level migration, temporary toleration, permanent
   exception, or retirement. Do not infer “cannot move” from a PVC alone.
3. Land the toleration/owner changes first in small Flux PRs. Verify the generated
   controller template and actual Pod toleration after reconciliation. Do not
   combine these changes with the Talos taint change.
4. For stateless candidates, verify worker placement and capacity, then roll one
   owner at a time. Existing Pods need not be evicted by `NoSchedule`, but their
   next restart must have a valid worker destination.
5. In a separate Terraform/Talos PR, set
   `allowSchedulingOnControlPlanes = false` for all active CP config paths.
6. Apply the CP policy one node at a time under etcd/API health gates. After each
   node, verify the taint, Node Ready, no unexpected Pending Pods, all tolerated
   residents, Flux reconciliation, and named endpoint smoke tests.
7. After stability is demonstrated, migrate or retire temporary local-PV residents
   and remove their tolerations. Keep the issue open until the completion criteria
   in #5361 are met.

## Source pointers

- `cluster/docs/plan.md` — roadmap and issue tracker pointer.
- `cluster/docs/lessons_learned/2026_06_19_etcd_hdd_io_contention.md` — I/O
  contention rationale and taint gate.
- `cluster/terraform/main/ovh-nodes.tf` — current CP config split: the HDD
  anchor is false/common, while the SSD CP path remains schedulable.
- GitHub issue #5361 — implementation tracking and acceptance criteria.


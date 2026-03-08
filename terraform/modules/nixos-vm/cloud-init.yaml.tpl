#cloud-config
# Secrets injection for pre-built NixOS VM images.
# The NixOS config and home-manager are baked into the qcow2 image.
# This cloud-init only writes credential files needed at runtime.

%{ if k8s_cluster_join != null ~}
write_files:
  # Kubernetes cluster CA certificate
  - path: /etc/kubernetes/pki/ca.crt
    owner: root:root
    permissions: '0644'
    content: |
      ${indent(6, k8s_cluster_join.ca_cert)}

  # Bootstrap kubeconfig for kubelet TLS bootstrap
  # Server URL is https://localhost:7445 — HAProxy proxies to api.allegedly.works:6443
  - path: /etc/kubernetes/bootstrap-kubelet.conf
    owner: root:root
    permissions: '0600'
    content: |
      ${indent(6, k8s_cluster_join.bootstrap_kubeconfig)}

  # kubespand configuration (KubeSpan mesh credentials)
  - path: /etc/kubespan/agent.yaml
    owner: root:root
    permissions: '0600'
    content: |
      cluster:
        id: "${k8s_cluster_join.cluster_id}"
        secret: "${k8s_cluster_join.cluster_secret}"
      kubernetes:
        advertise_networks: true
        kubeconfig_path: "/var/lib/kubelet/kubelet.conf"
        node_name: "${k8s_cluster_join.node_name}"
        service_cidrs:
          - "10.96.0.0/12"
%{ endif ~}

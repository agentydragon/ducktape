#!/bin/bash
# Setup and configure Talos VM

set -e

VM_DIR="/home/user/ducktape/talos-vm"
cd "$VM_DIR"

export TALOSCONFIG="$VM_DIR/talosconfig"

echo "======================================"
echo "Talos + Kubernetes VM Setup Helper"
echo "======================================"
echo ""
echo "NOTE: Talos Linux runs Kubernetes (k8s), not k3s."
echo "Talos is an immutable, minimal OS designed specifically for Kubernetes."
echo ""

show_help() {
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  start       - Start the Talos VM (run in background)"
    echo "  config      - Apply Talos configuration to the VM"
    echo "  bootstrap   - Bootstrap the Kubernetes cluster"
    echo "  status      - Check node status"
    echo "  kubeconfig  - Generate and merge kubeconfig"
    echo "  health      - Check cluster health"
    echo "  dashboard   - Get cluster dashboard info"
    echo "  stop        - Stop the VM"
    echo "  help        - Show this help message"
    echo ""
}

case "${1:-help}" in
    start)
        echo "Starting Talos VM in background..."
        echo "Logs will be in: $VM_DIR/vm.log"
        nohup ./start-vm.sh > vm.log 2>&1 &
        echo "VM started with PID: $!"
        echo "Wait 30-60 seconds for the VM to boot, then run: $0 config"
        ;;

    config)
        echo "Waiting for Talos API to be ready..."
        sleep 5
        echo "Applying configuration to the node..."
        ./talosctl apply-config --insecure --nodes localhost --file controlplane.yaml
        echo "Configuration applied! Wait 60 seconds, then run: $0 bootstrap"
        ;;

    bootstrap)
        echo "Bootstrapping Kubernetes cluster..."
        ./talosctl bootstrap --nodes localhost
        echo "Bootstrap initiated! Wait 2-3 minutes for cluster to be ready."
        echo "Then run: $0 kubeconfig"
        ;;

    status)
        echo "Checking node status..."
        ./talosctl --nodes localhost get members
        echo ""
        ./talosctl --nodes localhost version
        ;;

    kubeconfig)
        echo "Generating kubeconfig..."
        ./talosctl --nodes localhost kubeconfig kubeconfig-talos
        echo "Kubeconfig saved to: $VM_DIR/kubeconfig-talos"
        echo ""
        echo "To use with kubectl:"
        echo "  export KUBECONFIG=$VM_DIR/kubeconfig-talos"
        echo "  kubectl get nodes"
        ;;

    health)
        echo "Checking cluster health..."
        ./talosctl --nodes localhost health
        ;;

    dashboard)
        echo "Getting cluster dashboard info..."
        ./talosctl --nodes localhost dashboard
        ;;

    stop)
        echo "Stopping VM..."
        pkill -f "qemu-system-x86_64.*talos-vm" || echo "No VM found running"
        ;;

    help|*)
        show_help
        ;;
esac

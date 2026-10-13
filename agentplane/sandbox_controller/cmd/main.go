// The Sandbox Controller's entry point.
package main

import (
	"errors"
	"flag"
	"os"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/selection"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	kubevirtv1 "kubevirt.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/sandbox_controller"
)

func main() {
	namespace := flag.String("namespace", "", "where the Sandboxes, their templates, Pods and state PVCs live")
	metricsAddress := flag.String("metrics-bind-address", ":8080", "where Prometheus metrics are served")
	probeAddress := flag.String("health-probe-bind-address", ":8081", "where /healthz and /readyz are served")
	gatewayTarget := flag.String("runner-gateway-target", "", "the Runner Gateway's address for one Sandbox, a template of {sandbox} and {namespace}: the Sandbox's own control Service")
	gatewayTokenFile := flag.String("runner-gateway-token-file", "", "the projected ServiceAccount token the Runner Gateway accepts from this controller")
	flag.Parse()
	ctrl.SetLogger(zap.New())
	log := ctrl.Log.WithName("sandbox-controller")
	if err := run(*namespace, *metricsAddress, *probeAddress, sandboxcontroller.GatewaySealer{Target: *gatewayTarget, TokenFile: *gatewayTokenFile}); err != nil {
		log.Error(err, "controller failed")
		os.Exit(1)
	}
}

func run(namespace, metricsAddress, probeAddress string, sealer sandboxcontroller.GatewaySealer) error {
	if namespace == "" {
		return errors.New("--namespace is required")
	}
	if sealer.Target == "" || sealer.TokenFile == "" {
		return errors.New("--runner-gateway-target and --runner-gateway-token-file are required")
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := sandboxcontroller.AddToScheme(scheme); err != nil {
		return err
	}
	if err := policycontroller.AddToScheme(scheme); err != nil {
		return err
	}
	if err := kubevirtv1.AddToScheme(scheme); err != nil {
		return err
	}
	labelled, err := labels.NewRequirement(sandboxcontroller.SandboxLabel, selection.Exists, nil)
	if err != nil {
		return err
	}
	ours := labels.NewSelector().Add(*labelled)
	config := ctrl.GetConfigOrDie()
	kubeVirt, err := servesKubeVirt(config)
	if err != nil {
		return err
	}
	// The cache holds one namespace, and of its Pods, PVCs, ServiceAccounts, PolicyBindings and,
	// where KubeVirt is installed, VMs and VMIs only those carrying the Sandbox label.
	byObject := map[client.Object]cache.ByObject{
		&corev1.Pod{}:                     {Label: ours},
		&corev1.PersistentVolumeClaim{}:   {Label: ours},
		&corev1.ServiceAccount{}:          {Label: ours},
		&policycontroller.PolicyBinding{}: {Label: ours},
	}
	if kubeVirt {
		byObject[&kubevirtv1.VirtualMachine{}] = cache.ByObject{Label: ours}
		byObject[&kubevirtv1.VirtualMachineInstance{}] = cache.ByObject{Label: ours}
	} else {
		ctrl.Log.WithName("sandbox-controller").Info("KubeVirt's API is not served: VM Sandboxes stay unready")
	}
	mgr, err := ctrl.NewManager(config, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: probeAddress,
		// Replicas take turns: only the Lease holder reconciles.
		LeaderElection:          true,
		LeaderElectionID:        "agentplane-sandbox-controller",
		LeaderElectionNamespace: namespace,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{namespace: {}},
			ByObject:          byObject,
		},
	})
	if err != nil {
		return err
	}
	if err := (&sandboxcontroller.Reconciler{Client: mgr.GetClient(), KubeVirt: kubeVirt, Sealer: sealer, Leases: mgr.GetAPIReader()}).SetupWithManager(mgr); err != nil {
		return err
	}
	if err := mgr.AddHealthzCheck("ping", healthz.Ping); err != nil {
		return err
	}
	if err := mgr.AddReadyzCheck("ping", healthz.Ping); err != nil {
		return err
	}
	return mgr.Start(ctrl.SetupSignalHandler())
}

// servesKubeVirt is whether the cluster serves the VirtualMachine API this controller watches.
func servesKubeVirt(config *rest.Config) (bool, error) {
	discoveryClient, err := discovery.NewDiscoveryClientForConfig(config)
	if err != nil {
		return false, err
	}
	_, err = discoveryClient.ServerResourcesForGroupVersion(kubevirtv1.GroupVersion.String())
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	return err == nil, err
}

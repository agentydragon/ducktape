// The Sandbox Controller's entry point.
package main

import (
	"errors"
	"flag"
	"os"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/selection"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
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
	flag.Parse()
	ctrl.SetLogger(zap.New())
	log := ctrl.Log.WithName("sandbox-controller")
	if err := run(*namespace, *metricsAddress, *probeAddress); err != nil {
		log.Error(err, "controller failed")
		os.Exit(1)
	}
}

func run(namespace, metricsAddress, probeAddress string) error {
	if namespace == "" {
		return errors.New("--namespace is required")
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
	labelled, err := labels.NewRequirement(sandboxcontroller.SandboxLabel, selection.Exists, nil)
	if err != nil {
		return err
	}
	ours := labels.NewSelector().Add(*labelled)
	// The cache holds one namespace, and of its Pods, PVCs, ServiceAccounts and PolicyBindings only
	// those carrying the Sandbox label.
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: probeAddress,
		// Replicas take turns: only the Lease holder reconciles.
		LeaderElection:          true,
		LeaderElectionID:        "agentplane-sandbox-controller",
		LeaderElectionNamespace: namespace,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{namespace: {}},
			ByObject: map[client.Object]cache.ByObject{
				&corev1.Pod{}:                     {Label: ours},
				&corev1.PersistentVolumeClaim{}:   {Label: ours},
				&corev1.ServiceAccount{}:          {Label: ours},
				&policycontroller.PolicyBinding{}: {Label: ours},
			},
		},
	})
	if err != nil {
		return err
	}
	if err := (&sandboxcontroller.Reconciler{Client: mgr.GetClient()}).SetupWithManager(mgr); err != nil {
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

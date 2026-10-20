// The Policy Controller's entry point.
package main

import (
	"log/slog"
	"os"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

func main() {
	ctrl.SetLogger(logr.FromSlogHandler(slog.NewJSONHandler(os.Stderr, nil)))
	log := ctrl.Log.WithName("policy-controller")
	c, err := loadConfig()
	if err == nil {
		err = run(c)
	}
	if err != nil {
		log.Error(err, "controller failed")
		os.Exit(1)
	}
}

func run(c config) error {
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		return err
	}
	ours := labels.SelectorFromSet(labels.Set{
		policycontroller.ManagedByLabel:     policycontroller.FieldManager,
		policycontroller.PolicyNamespaceLbl: c.PolicyNamespace,
	})
	only := func(namespaces ...string) map[string]cache.Config {
		out := map[string]cache.Config{}
		for _, ns := range namespaces {
			out[ns] = cache.Config{}
		}
		return out
	}
	// The cache holds only what the controller reads: its own namespace's policy objects, the
	// subject namespaces' accounts, and the native bindings carrying its label.
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: c.MetricsBindAddress},
		HealthProbeBindAddress: c.HealthProbeBindAddress,
		// Replicas take turns: only the Lease holder reconciles.
		LeaderElection:          true,
		LeaderElectionID:        "agentplane-policy-controller",
		LeaderElectionNamespace: c.PolicyNamespace,
		Cache: cache.Options{ByObject: map[client.Object]cache.ByObject{
			&policyapi.PolicySet{}:       {Namespaces: only(c.PolicyNamespace)},
			&policyapi.PolicyBinding{}:   {Namespaces: only(c.PolicyNamespace)},
			&corev1.ServiceAccount{}:     {Namespaces: only(c.SubjectNamespaces...)},
			&rbacv1.RoleBinding{}:        {Label: ours},
			&rbacv1.ClusterRoleBinding{}: {Label: ours},
		}},
	})
	if err != nil {
		return err
	}
	reconciler := &policycontroller.Reconciler{
		Client:            mgr.GetClient(),
		APIReader:         mgr.GetAPIReader(),
		PolicyNamespace:   c.PolicyNamespace,
		SubjectNamespaces: c.SubjectNamespaces,
	}
	if err := reconciler.SetupWithManager(mgr); err != nil {
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

// The Policy Controller's entry point.
package main

import (
	"flag"
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

func main() {
	policyNamespace := flag.String("policy-namespace", "", "where the PolicySets and PolicyBindings live")
	subjectNamespaces := flag.String("subject-namespaces", "", "comma-separated namespaces whose ServiceAccounts may be bound")
	metricsAddress := flag.String("metrics-bind-address", ":8080", "where Prometheus metrics are served")
	probeAddress := flag.String("health-probe-bind-address", ":8081", "where /healthz and /readyz are served")
	flag.Parse()
	ctrl.SetLogger(zap.New())
	log := ctrl.Log.WithName("policy-controller")
	if err := run(*policyNamespace, strings.Split(*subjectNamespaces, ","), *metricsAddress, *probeAddress); err != nil {
		log.Error(err, "controller failed")
		os.Exit(1)
	}
}

func run(policyNamespace string, subjectNamespaces []string, metricsAddress, probeAddress string) error {
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := policycontroller.AddToScheme(scheme); err != nil {
		return err
	}
	ours := labels.SelectorFromSet(labels.Set{
		policycontroller.ManagedByLabel:     policycontroller.FieldManager,
		policycontroller.PolicyNamespaceLbl: policyNamespace,
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
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: probeAddress,
		// Replicas take turns: only the Lease holder reconciles.
		LeaderElection:          true,
		LeaderElectionID:        "agentplane-policy-controller",
		LeaderElectionNamespace: policyNamespace,
		Cache: cache.Options{ByObject: map[client.Object]cache.ByObject{
			&policycontroller.PolicySet{}:     {Namespaces: only(policyNamespace)},
			&policycontroller.PolicyBinding{}: {Namespaces: only(policyNamespace)},
			&corev1.ServiceAccount{}:          {Namespaces: only(subjectNamespaces...)},
			&rbacv1.RoleBinding{}:             {Label: ours},
			&rbacv1.ClusterRoleBinding{}:      {Label: ours},
		}},
	})
	if err != nil {
		return err
	}
	reconciler := &policycontroller.Reconciler{
		Client:            mgr.GetClient(),
		Roles:             mgr.GetAPIReader(),
		PolicyNamespace:   policyNamespace,
		SubjectNamespaces: subjectNamespaces,
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
